"""Chart tokens, in one place so every figure is drawn against roles.

Palette slots are the validated categorical defaults (blue/orange), checked with
the data-viz validator in both modes: worst all-pairs CVD ΔE 24.7 light /
26.8 dark against a ≥8 target, normal-vision ΔE 33.6 / 31.8 against a ≥15 floor.
Stand-aside is deliberately *not* a series colour — it is the absence of an
action, and giving it a hue implies a third position the agent never takes.
"""

SURFACE = "#fcfcfb"
TEXT_PRIMARY = "#0b0b0b"
TEXT_SECONDARY = "#52514e"
TEXT_MUTED = "#8a8985"
GRID = "#e8e8e5"

#: Categorical slots 1 and 2 — the two things the agent actually does.
SELL_PREMIUM = "#2a78d6"      # blue
BUY_PREMIUM = "#eb6834"       # orange
STAND_ASIDE = "#b4b3ae"       # neutral: no position taken

#: Status colours, reserved and never reused as series.
GOOD = "#008300"
BAD = "#e34948"

STANCE_COLOR = {
    "sell_premium": SELL_PREMIUM,
    "buy_premium": BUY_PREMIUM,
    "stand_aside": STAND_ASIDE,
}
STANCE_LABEL = {
    "sell_premium": "Sell premium",
    "buy_premium": "Buy premium",
    "stand_aside": "Stand aside",
}


def base_layout(height: int = 320, **kwargs) -> dict:
    """Recessive axes and grid; the data carries the ink."""
    layout = {
        "height": height,
        "paper_bgcolor": SURFACE,
        "plot_bgcolor": SURFACE,
        "font": {"family": "system-ui, -apple-system, sans-serif",
                 "size": 12, "color": TEXT_SECONDARY},
        "margin": {"l": 8, "r": 16, "t": 8, "b": 8},
        "xaxis": {"gridcolor": GRID, "zeroline": False, "linecolor": GRID},
        "yaxis": {"gridcolor": GRID, "zeroline": False, "linecolor": GRID},
        "hoverlabel": {"bgcolor": SURFACE, "bordercolor": GRID,
                       "font": {"color": TEXT_PRIMARY, "size": 12}},
        "showlegend": False,
    }
    layout.update(kwargs)
    return layout
