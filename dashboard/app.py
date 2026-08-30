"""Live view of the agent: what it decided, why, and what it is carrying.

Runs two ways on purpose. With the Alpaca CLI installed it shows live account
state; without it — on Streamlit Community Cloud, say — it falls back to the
committed decision and run logs, which are the record the competition is judged
on anyway. Nothing here is required for trading; it reads, never writes.

    streamlit run dashboard/app.py
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import chain as ch
from agent import config, journal, strategy
from dashboard import theme

st.set_page_config(page_title="Options Alpha Agent", page_icon="📈",
                   layout="wide")


# ── data ─────────────────────────────────────────────────────────────────────

@st.cache_data(ttl=60)
def load_logs() -> tuple[pd.DataFrame, pd.DataFrame]:
    decisions = pd.DataFrame(journal.read(config.DECISIONS_LOG))
    runs = pd.DataFrame(journal.read(config.RUNS_LOG))
    return decisions, runs


@st.cache_data(ttl=30)
def load_live() -> dict | None:
    """Live account state, or None when the CLI is not available here."""
    try:
        from agent import cli
        return {
            "account": cli.account(),
            "positions": cli.positions(),
            "clock": cli.clock(),
        }
    except Exception:
        return None


def flatten_regime(decisions: pd.DataFrame) -> pd.DataFrame:
    """Lift the nested regime dict into columns."""
    if decisions.empty or "regime" not in decisions:
        return pd.DataFrame()
    rows = []
    for _, row in decisions.iterrows():
        regime = row.get("regime") or {}
        if not regime:
            continue
        rows.append({
            "timestamp": pd.to_datetime(row["timestamp"], errors="coerce"),
            "symbol": row["symbol"],
            "action": row["action"],
            "reason": row["reason"],
            "stance": regime.get("stance", ""),
            "ratio": regime.get("iv_rv_ratio"),
            "implied": regime.get("implied_vol"),
            "realized": regime.get("realized_vol"),
            "bias": regime.get("trend_bias", ""),
        })
    return pd.DataFrame(rows)


# ── charts ───────────────────────────────────────────────────────────────────

def equity_chart(runs: pd.DataFrame) -> go.Figure | None:
    """Equity over time. One series, so no legend — the title names it."""
    if runs.empty or "equity_after" not in runs:
        return None
    df = runs.dropna(subset=["equity_after"]).copy()
    if df.empty:
        return None
    df["when"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df = df.sort_values("when")

    start = float(df["equity_after"].iloc[0])
    end = float(df["equity_after"].iloc[-1])
    colour = theme.GOOD if end >= start else theme.BAD

    fig = go.Figure()
    fig.add_hline(y=100_000, line={"color": theme.GRID, "width": 1, "dash": "dot"},
                  annotation_text="start", annotation_position="right",
                  annotation_font={"color": theme.TEXT_MUTED, "size": 11})
    fig.add_trace(go.Scatter(
        x=df["when"], y=df["equity_after"], mode="lines+markers",
        line={"color": colour, "width": 2},
        marker={"size": 8, "color": colour,
                "line": {"width": 2, "color": theme.SURFACE}},
        hovertemplate="%{x|%d %b %H:%M}<br><b>$%{y:,.2f}</b><extra></extra>",
    ))
    fig.update_layout(**theme.base_layout(280, hovermode="x unified"))
    fig.update_yaxes(tickprefix="$", tickformat=",.0f")
    return fig


def regime_chart(scan: pd.DataFrame) -> go.Figure | None:
    """The signal itself: each symbol's IV/RV against the stand-aside band.

    A dot plot rather than bars — the value is a ratio around 1.0, not a
    magnitude from zero, and bars from a zero baseline would imply the wrong
    thing about what the number means.
    """
    if scan.empty:
        return None
    df = scan.dropna(subset=["ratio"]).drop_duplicates("symbol", keep="last")
    if df.empty:
        return None
    df = df.sort_values("ratio")

    fig = go.Figure()
    # The band where the agent does nothing — the majority outcome by design.
    fig.add_vrect(x0=strategy.CHEAP_RATIO, x1=strategy.RICH_RATIO,
                  fillcolor=theme.STAND_ASIDE, opacity=0.14, line_width=0,
                  annotation_text="stand aside", annotation_position="top",
                  annotation_font={"color": theme.TEXT_MUTED, "size": 11})

    for stance in ("buy_premium", "stand_aside", "sell_premium"):
        part = df[df["stance"] == stance]
        if part.empty:
            continue
        fig.add_trace(go.Scatter(
            x=part["ratio"], y=part["symbol"], mode="markers+text",
            marker={"size": 13, "color": theme.STANCE_COLOR[stance],
                    "line": {"width": 2, "color": theme.SURFACE}},
            text=[f" {v:.2f}" for v in part["ratio"]],
            textposition="middle right",
            textfont={"color": theme.TEXT_SECONDARY, "size": 11},
            name=theme.STANCE_LABEL[stance],
            hovertemplate=("<b>%{y}</b><br>IV/RV %{x:.3f}<extra>"
                           + theme.STANCE_LABEL[stance] + "</extra>"),
        ))

    fig.update_layout(**theme.base_layout(
        max(240, 42 * len(df)), showlegend=True,
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02,
                "x": 0, "font": {"color": theme.TEXT_SECONDARY}}))
    fig.update_xaxes(title_text="implied ÷ realized volatility",
                     title_font={"color": theme.TEXT_MUTED, "size": 11},
                     range=[min(0.6, df["ratio"].min() - 0.1),
                            max(1.6, df["ratio"].max() + 0.15)])
    fig.update_yaxes(title_text=None)
    return fig


# ── page ─────────────────────────────────────────────────────────────────────

decisions, runs = load_logs()
live = load_live()
scan = flatten_regime(decisions)

st.title("Options Alpha Agent")
st.caption(
    "Sells defined-risk credit spreads when options price more movement than "
    "the stock delivers, buys debit spreads when they price less, and stands "
    "aside in between — which is most of the time, by design."
)

# Stat row. A hero number beats a chart when there is one number that matters.
if live:
    account = live["account"]
    equity = float(account.get("equity") or 0)
    last = float(account.get("last_equity") or equity)
    positions = live["positions"]
    open_risk = sum(abs(float(p.get("cost_basis") or 0)) for p in positions)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Equity", f"${equity:,.2f}", f"{equity - last:+,.2f} today")
    c2.metric("Total return", f"{(equity / 100_000 - 1):+.2%}",
              help="against the $100,000 starting balance")
    c3.metric("Open legs", len(positions))
    c4.metric("Capital at risk", f"${open_risk:,.0f}",
              f"{open_risk / equity:.1%} of equity" if equity else None)

    market = "open" if live["clock"].get("is_open") else "closed"
    st.caption(f"Account `{account.get('account_number','—')}` · market {market}")
else:
    st.info(
        "Showing the committed logs — the Alpaca CLI is not available in this "
        "environment, so live account state is unavailable. Install it with "
        "`./scripts/install_cli.sh` to see equity and positions."
    )

st.divider()

left, right = st.columns([1, 1])

with left:
    st.subheader("Equity")
    fig = equity_chart(runs)
    if fig:
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.caption("No completed runs recorded yet.")

with right:
    st.subheader("Latest volatility scan")
    fig = regime_chart(scan)
    if fig:
        st.plotly_chart(fig, use_container_width=True)
        st.caption(
            f"Thresholds {strategy.CHEAP_RATIO:.2f} / {strategy.RICH_RATIO:.2f} "
            "were set a-priori and are not tuned on results."
        )
    else:
        st.caption("No regime readings recorded yet.")

st.divider()
st.subheader("Decisions")
st.caption(
    "Every symbol considered, not only the traded ones — the refusals are the "
    "larger part of what this strategy does."
)

if scan.empty:
    st.caption("No decisions recorded yet.")
else:
    table = scan.sort_values("timestamp", ascending=False).copy()
    table["when"] = table["timestamp"].dt.strftime("%d %b %H:%M")
    table["IV/RV"] = table["ratio"].map(lambda v: "—" if pd.isna(v) else f"{v:.3f}")
    table["stance"] = table["stance"].map(lambda s: theme.STANCE_LABEL.get(s, s or "—"))
    st.dataframe(
        table[["when", "symbol", "action", "IV/RV", "stance", "bias", "reason"]]
        .rename(columns={"when": "When", "symbol": "Symbol", "action": "Action",
                         "stance": "Stance", "bias": "Trend", "reason": "Reason"}),
        use_container_width=True, hide_index=True,
    )

if live and live["positions"]:
    st.divider()
    st.subheader("Open positions")
    rows = []
    for p in live["positions"]:
        symbol = p.get("symbol", "")
        try:
            underlying, expiration, kind, strike = ch.parse_occ(symbol)
            leg = f"{underlying} {expiration:%d %b} {strike:g} {kind}"
            dte = (expiration - date.today()).days
        except ValueError:
            leg, dte = symbol, None
        rows.append({
            "Leg": leg, "DTE": dte, "Qty": p.get("qty"),
            "Market value": float(p.get("market_value") or 0),
            "Unrealized": float(p.get("unrealized_pl") or 0),
            "Return": float(p.get("unrealized_plpc") or 0),
        })
    st.dataframe(
        pd.DataFrame(rows).style
          .format({"Market value": "${:,.2f}", "Unrealized": "${:,.2f}",
                   "Return": "{:+.2%}"}),
        use_container_width=True, hide_index=True,
    )
