"""
config.py - every tunable constant in one place.

Nothing in here is secret; change values freely.
"""

# ---------------------------------------------------------------------------
# Data source
# ---------------------------------------------------------------------------
# CBOE's public delayed-quotes JSON. This is the same file that the
# cboe.com "Delayed Quotes" / quote-table pages load in your browser.
# It is a plain HTTP GET: no login, no cookies, no form post, no HTML
# scraping. It is NOT a documented, versioned public API, so CBOE can
# change or restrict it without notice. Two hostnames serve it; we try
# them in order.
CBOE_CHAIN_URLS = [
    "https://cdn.cboe.com/api/global/delayed_quotes/options/_SPX.json",
    "https://cdn-api.cboe.com/api/global/delayed_quotes/options/_SPX.json",
]
HTTP_TIMEOUT_SECONDS = 30

# Option roots we keep. SPX = AM-settled monthlies, SPXW = PM-settled weeklies/dailies.
ALLOWED_ROOTS = ("SPX", "SPXW")

# ---------------------------------------------------------------------------
# Contract / model constants
# ---------------------------------------------------------------------------
CONTRACT_MULTIPLIER = 100          # SPX index options: $100 x index points
DEFAULT_RISK_FREE_RATE = 0.0       # annualised, continuous. User can change in the sidebar.
DEFAULT_DIVIDEND_YIELD = 0.0       # annualised, continuous.

SECONDS_PER_YEAR = 365.0 * 24 * 3600

# Floor on time-to-expiry. As T -> 0, Black-Scholes gamma for an at-the-money
# option goes to infinity, so a same-day (0DTE) contract a few seconds before
# the close would dominate everything. 30 minutes is a common practical floor.
MIN_T_YEARS = 30 * 60 / SECONDS_PER_YEAR

# Bounds used when back-solving implied volatility from a quote.
IV_SOLVE_LOW = 1e-4
IV_SOLVE_HIGH = 10.0               # 1000% - anything above this is treated as unsolvable

# ---------------------------------------------------------------------------
# Dealer sign convention  (ASSUMPTION - not observed data)
# ---------------------------------------------------------------------------
# No public feed tells us who is long or short each contract. Every GEX model
# therefore ASSUMES a dealer position. The sign below is what each contract
# type contributes to *dealer* net gamma.
#
# "standard" (default) - the convention used by SqueezeMetrics' 2017 GEX
#   white paper and most public GEX tools:
#     * Calls  -> +1 : end-users are assumed to be net SELLERS of calls
#       (covered-call / overwriting programs), so dealers, as liquidity
#       providers taking the other side, are assumed LONG calls = long gamma.
#     * Puts   -> -1 : end-users are assumed to be net BUYERS of puts
#       (portfolio hedging), so dealers are assumed SHORT puts = short gamma.
#
# "inverted" - calls negative, puts positive. Provided because it was the
#   convention written in the original project brief. Note that a dealer who is
#   short BOTH calls and puts would be short gamma on both, so no single
#   "short everything" story produces opposite signs; pick the one you believe.
SIGN_CONVENTIONS = {
    "standard": {
        "label": "Standard - dealers long calls (+), short puts (-)",
        "call": +1.0,
        "put": -1.0,
    },
    "inverted": {
        "label": "Inverted - calls (-), puts (+)",
        "call": -1.0,
        "put": +1.0,
    },
}
DEFAULT_SIGN_CONVENTION = "standard"

# ---------------------------------------------------------------------------
# GEX weighting: which contract count multiplies dollar gamma
# ---------------------------------------------------------------------------
# Open interest is prior-night and static intraday. Today's volume shows where
# activity is now, but counts buyers AND sellers and does not say whether a
# trade opened or closed a position, so volume weighting is a rough proxy.
GEX_WEIGHTINGS = {"open_interest": "open interest", "volume": "today's volume"}
WEIGHTING_MODES = {                 # sidebar "GEX weighting" choices
    "oi": "Open interest",          # default, the standard GEX
    "volume": "Today's volume",
    "compare": "Compare both",
}
DEFAULT_WEIGHTING_MODE = "oi"
VOLUME_OI_TOP_N = 15                # rows in the Volume / OI table

# ---------------------------------------------------------------------------
# Gamma-flip search / display
# ---------------------------------------------------------------------------
FLIP_SEARCH_RANGE_PCT = 0.20       # search hypothetical spots within +/-20% of spot
FLIP_GRID_POINTS = 401             # resolution of that search (~0.1% steps)
DEFAULT_STRIKE_RANGE_PCT = 0.035   # strikes shown on the chart: +/-3.5% of spot
DEFAULT_STRIKE_BUCKET = 10         # group 5-pt strikes into 10-pt bars on the main chart   # strikes shown on the chart: +/-6% of spot
DEFAULT_EXPIRIES_IN_BREAKDOWN = 15

# ---------------------------------------------------------------------------
# Colours (dark trading-dashboard palette)
# ---------------------------------------------------------------------------
COLORS = {
    "bg": "#0b0f17",
    "panel": "#131a26",
        "grid": "#1f2937",
    "grid_soft": "rgba(148,163,184,0.08)",
    "zone_pos": "rgba(22,199,132,0.05)",    # background tint where gamma is positive
    "zone_neg": "rgba(234,57,67,0.06)",     # background tint where gamma is negative
    "text": "#e6edf3",
    "muted": "#8b98a9",
       "call": "#16c784",       # green
    "put": "#ea3943",        # red    # red
    "net_line": "#22d3ee",   # cyan  - Net GEX (cumulative)
        "agg_line": "#b388ff",   # soft purple - Aggregate GEX
    "spot": "#4f9dff",       # blue
    "flip": "#d6a4ff",       # light purple, dashed
    "em_edge": "#f5b84a",                   # amber - expected-move (1 sigma) band edges
    "em_band": "rgba(245,184,74,0.07)",     # amber, faint - expected-move band fill
}
