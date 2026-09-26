"""
app.py - Streamlit entry point.   Run with:  streamlit run app.py

Local only: .streamlit/config.toml binds the server to localhost and turns
off Streamlit's usage-statistics telemetry. The only outbound request this app
makes is the CBOE chain download, and only when you click "Refresh Data".
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import streamlit as st

import config
import data_fetcher
import gex_calculator as gc
import visualizer as viz

st.set_page_config(page_title="SPX Gamma Exposure", page_icon="📊", layout="wide")

C = config.COLORS
SIGN_HELP = (
    "ASSUMPTION, not observed data: no public feed shows who holds each option. "
    "The standard convention assumes end-users are net sellers of calls and net buyers "
    "of puts, so dealers (liquidity providers on the other side) are long calls (+) and "
    "short puts (-). Change it in the sidebar."
)
NOT_A_FORECAST = (
    "GEX describes current (assumed) dealer positioning. It is not a price forecast; the "
    "link between dealer hedging and short-term SPX behaviour is a historically observed "
    "tendency, not a guarantee."
)

st.markdown(
    f"""
    <style>
      .block-container {{ padding-top: 3.2rem; }}
      .card {{ background:{C['panel']}; border:1px solid {C['grid']}; border-radius:10px;
               padding:12px 14px; height:100%; }}
      .card .lbl {{ color:{C['muted']}; font-size:0.78rem; text-transform:uppercase;
                    letter-spacing:.04em; }}
      .card .val {{ font-size:1.45rem; font-weight:650; margin-top:2px;
                    font-variant-numeric: tabular-nums; }}
      .regime {{ border-radius:10px; padding:12px 16px; margin-top:10px; border:1px solid; }}
      .stamp {{ color:{C['muted']}; font-size:0.85rem; }}
    </style>
    """,
    unsafe_allow_html=True,
)


def card(col, label: str, value: str, color: str | None = None, tip: str | None = None) -> None:
    title = f' title="{tip}"' if tip else ""
    icon = " ⓘ" if tip else ""
    col.markdown(
        f'<div class="card"{title}><div class="lbl">{label}{icon}</div>'
        f'<div class="val" style="color:{color or C["text"]}">{value}</div></div>',
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Sidebar controls
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("Controls")
    show_net = st.toggle("Show Net GEX (cumulative) line", value=True)
    show_agg = st.toggle("Show Aggregate GEX line", value=True)
    range_pct = st.slider("Strike range shown (± % of spot)", 1.0, 15.0,
                          config.DEFAULT_STRIKE_RANGE_PCT * 100, step=0.5) / 100.0
    bucket = st.select_slider("Bar width (points per bar)", options=[5, 10, 25, 50],
                              value=config.DEFAULT_STRIKE_BUCKET,
                              help="Groups SPX's 5-point strikes into wider bars for readability. "
                                   "Walls, flip and totals are always calculated on exact strikes.")
    max_exp = st.slider("Expirations in breakdown chart", 5, 40, config.DEFAULT_EXPIRIES_IN_BREAKDOWN)
    st.divider()
    st.subheader("Model inputs")
    convention = st.selectbox(
        "Dealer sign convention (assumption)",
        options=list(config.SIGN_CONVENTIONS),
        format_func=lambda k: config.SIGN_CONVENTIONS[k]["label"],
        index=list(config.SIGN_CONVENTIONS).index(config.DEFAULT_SIGN_CONVENTION),
        help=SIGN_HELP,
    )
    r = st.number_input("Risk-free rate (annual, decimal)", value=config.DEFAULT_RISK_FREE_RATE,
                        min_value=0.0, max_value=0.2, step=0.0025, format="%.4f")
    q = st.number_input("Dividend yield (annual, decimal)", value=config.DEFAULT_DIVIDEND_YIELD,
                        min_value=0.0, max_value=0.1, step=0.0025, format="%.4f")
    st.caption("Recalculating after changing these uses the chain you last fetched; "
               "it does not re-download.")

# ---------------------------------------------------------------------------
# Header + refresh
# ---------------------------------------------------------------------------
head_l, head_r = st.columns([3, 1])
head_l.title("SPX Gamma Exposure")
refresh = head_r.button("🔄 Refresh Data", type="primary", width="stretch",
                        help="Download a fresh SPX chain from CBOE and recalculate everything.")

if refresh:
    with st.spinner("Downloading SPX option chain from CBOE..."):
        try:
            st.session_state["snapshot"] = data_fetcher.fetch_spx_chain()
            st.session_state.pop("fetch_error", None)
        except data_fetcher.DataFetchError as exc:
            st.session_state["fetch_error"] = str(exc)

if err := st.session_state.get("fetch_error"):
    st.error(f"Refresh failed - nothing was updated.\n\n{err}")

snap: data_fetcher.ChainSnapshot | None = st.session_state.get("snapshot")
if snap is None:
    st.info("No data loaded yet. Click **Refresh Data** to download the current SPX chain "
            "from CBOE (delayed at least 15 minutes). Nothing is fetched until you click.")
    st.stop()

# Timestamp line: when *you* fetched, what CBOE stamped, and the built-in delay.
fetched_local = snap.fetched_at_utc.astimezone()
_off = fetched_local.strftime("%z")
tz_label = f"UTC{_off[:3]}:{_off[3:]}" if _off else "local time"
age_min = (datetime.now(timezone.utc) - snap.fetched_at_utc).total_seconds() / 60
st.markdown(
    f'<div class="stamp">Last fetched: <b>{fetched_local:%Y-%m-%d %H:%M:%S}</b> ({tz_label}, your PC clock) '
    f"({age_min:.0f} min ago at last page update) · CBOE file timestamp: <b>{snap.cboe_timestamp or 'n/a'}</b> "
    f"· CBOE quotes are delayed ≥15 min, so data is at least {15 + age_min:.0f} min old · "
    f"Open interest is start-of-day.</div>",
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------------------
# Calculations
# ---------------------------------------------------------------------------
prepared, iv_report = gc.prepare_chain(snap.chain, snap.spot, r=r, q=q)
if prepared.empty:
    st.error("No usable contracts (all expired, zero open interest, or missing IV).")
    st.stop()

expiries = sorted(prepared["expiry"].unique())
exp_meta = prepared.groupby("expiry").agg(dte=("dte", "min"), roots=("root", lambda s: "/".join(sorted(set(s)))))
COMBINED = "All expirations (combined)"
view = st.selectbox(
    "View",
    [COMBINED] + expiries,
    format_func=lambda e: e if e == COMBINED
    else f"{e:%a %d %b %Y} · {exp_meta.loc[e, 'roots']} · {exp_meta.loc[e, 'dte']:.1f} days",
)
selected = None if view == COMBINED else view

res = gc.run_analysis(prepared, snap.spot, convention=convention, r=r, q=q,
                      expiry=selected, display_range_pct=range_pct)

# ---------------------------------------------------------------------------
# Summary panel
# ---------------------------------------------------------------------------
cols = st.columns(6)
card(cols[0], "SPX Spot", f"{res.spot:,.2f}", C["spot"])
card(cols[1], "Call Wall", f"{res.call_wall:,.0f}" if res.call_wall else "n/a", C["call"],
     "Strike with the largest unsigned call dollar gamma")
card(cols[2], "Put Wall", f"{res.put_wall:,.0f}" if res.put_wall else "n/a", C["put"],
     "Strike with the largest unsigned put dollar gamma")
card(cols[3], "Total Net GEX", viz.fmt_usd(res.total_net_gex),
     C["call"] if res.total_net_gex >= 0 else C["put"],
     "$ of SPX dealers would trade per 1% move, under the assumed sign convention. " + SIGN_HELP)
card(cols[4], "Gamma Flip", f"{res.gamma_flip:,.2f}" if res.gamma_flip else "none in ±20%", C["flip"],
     "SPX level where total net GEX, recomputed at hypothetical spots, crosses zero (nearest to spot)")
regime_color = C["call"] if res.regime.key == "positive" else C["put"]
card(cols[5], "Gamma Regime", res.regime.key.capitalize(), regime_color)

scope = "all expirations combined" if selected is None else f"the {selected:%d %b %Y} expiry only"
st.markdown(
    f'<div class="regime" style="border-color:{regime_color};background:{regime_color}14">'
    f'<b style="color:{regime_color}">{res.regime.title}</b> - {res.regime.sentence}'
    f'<br><span class="stamp">Computed from {scope}. {NOT_A_FORECAST}</span></div>',
    unsafe_allow_html=True,
)
st.caption(f"ⓘ Sign convention in use: **{config.SIGN_CONVENTIONS[convention]['label']}**. {SIGN_HELP}")

# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------
t_strike, t_exp, t_prof, t_quality, t_method = st.tabs(
    ["GEX by strike", "Per-expiry breakdown", "Gamma profile", "Data quality", "Methodology"]
)

with t_strike:
    title = "SPX Gamma Exposure by Strike - " + ("all expirations" if selected is None else f"{selected:%d %b %Y}")
    st.plotly_chart(viz.gex_strike_chart(res, show_net, show_agg, range_pct, title, bucket), width="stretch")

with t_exp:
    # Always computed across ALL expiries so near vs far-dated GEX can be compared.
    full = res if selected is None else gc.run_analysis(prepared, snap.spot, convention, r, q, None, range_pct)
    st.plotly_chart(viz.expiry_breakdown_chart(full.by_expiry, max_exp), width="stretch")
    tbl = full.by_expiry.copy()
    tbl["expiry"] = pd.to_datetime(tbl["expiry"]).dt.strftime("%Y-%m-%d")
    for c_ in ("call_raw", "put_raw", "call_gex", "put_gex", "net_gex"):
        tbl[c_] = tbl[c_].map(viz.fmt_usd)
    tbl["share_of_gamma"] = (full.by_expiry["share_of_gamma"] * 100).map("{:.1f}%".format)
    tbl["dte"] = full.by_expiry["dte"].map("{:.1f}".format)
    st.dataframe(
        tbl[["expiry", "roots", "dte", "contracts", "call_oi", "put_oi", "call_raw", "put_raw",
             "net_gex", "share_of_gamma"]].rename(columns={
            "expiry": "Expiry", "roots": "Roots", "dte": "Days", "contracts": "Contracts",
            "call_oi": "Call OI", "put_oi": "Put OI", "call_raw": "Call $gamma (unsigned)",
            "put_raw": "Put $gamma (unsigned)", "net_gex": "Net GEX (signed)",
            "share_of_gamma": "Share of total gamma"}),
        hide_index=True, width="stretch",
    )

with t_prof:
    st.plotly_chart(viz.gamma_profile_chart(res), width="stretch")
    if len(res.all_flips) > 1:
        nearest = sorted(res.all_flips, key=lambda f: abs(f - res.spot))[:8]
        st.caption(f"{len(res.all_flips)} zero crossings found within ±{config.FLIP_SEARCH_RANGE_PCT:.0%}; "
                   "nearest to spot: " + ", ".join(f"{f:,.0f}" for f in sorted(nearest))
                   + ". The headline flip is the one nearest spot. Many crossings usually mean "
                   "positioning is fragmented (often by short-dated expiries).")

with t_quality:
    rep = iv_report
    a, b, c_, d_ = st.columns(4)
    a.metric("Contracts in feed", f"{rep.total_contracts:,}")
    b.metric("Used in GEX", f"{len(prepared):,}")
    c_.metric("IV from CBOE feed", f"{rep.iv_from_feed:,}")
    d_.metric("IV back-solved", f"{rep.iv_backsolved:,}")
    st.write(
        f"- Expired contracts removed: **{rep.expired:,}**\n"
        f"- Zero open interest removed (contribute no GEX): **{rep.zero_open_interest:,}**\n"
        f"- Excluded for missing IV with no usable two-sided quote: **{rep.excluded_no_iv:,}** "
        f"contracts, **{rep.excluded_oi:,.0f}** open interest"
    )
    if rep.iv_backsolved or rep.excluded_no_iv:
        st.warning("CBOE reported no IV for some contracts with open interest. Those were back-solved "
                   "from the bid/ask mid by inverting Black-Scholes, or excluded if that wasn't possible. "
                   "This mostly affects deep in/out-of-the-money or illiquid strikes.")
        st.dataframe(rep.detail, hide_index=True, width="stretch")
    else:
        st.success("CBOE supplied a positive IV for every contract with open interest; nothing was back-solved.")
    if snap.notes:
        st.info("Parser notes:\n\n" + "\n".join(f"- {n}" for n in snap.notes))
    st.caption(f"Source: {snap.source_url}")

with t_method:
    st.markdown(
        f"""
**Per contract:** Black-Scholes gamma with spot *S*, strike *K*, time to expiry *T* (calendar
time to 09:30 ET for AM-settled SPX, 16:00 ET for SPXW; floored at 30 minutes), risk-free rate
*r* = {r:.4f}, dividend yield *q* = {q:.4f}, and the contract's implied volatility.

**Dollar gamma** = gamma × S² × 0.01 × 100 × open interest: the $ of SPX that would be bought or
sold per 1% move.

**Sign (assumption):** {config.SIGN_CONVENTIONS[convention]['label']}. {SIGN_HELP}

**Call / Put Wall:** strike with the largest *unsigned* call / put dollar gamma.

**Net GEX (cumulative), cyan:** per-strike net GEX at the actual spot, summed from the lowest strike
upward. Its right-hand end equals total net GEX.

**Aggregate GEX, purple:** total net GEX recomputed as if SPX were trading at each strike, keeping
each contract's current IV (sticky-strike simplification).

**Gamma flip:** the level where that recomputed total crosses zero, searched on a
{config.FLIP_GRID_POINTS}-point grid across ±{config.FLIP_SEARCH_RANGE_PCT:.0%} of spot with linear
interpolation; the crossing nearest spot is shown.

**Limits:** open interest is start-of-day; quotes are delayed ≥15 minutes; the dealer position
is assumed, not observed. {NOT_A_FORECAST}
"""
    )
