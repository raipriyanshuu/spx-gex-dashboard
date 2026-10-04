"""
app.py - Streamlit entry point.   Run with:  streamlit run app.py

Local only: .streamlit/config.toml binds the server to localhost and turns
off Streamlit's usage-statistics telemetry. The only outbound requests this app
makes are two CBOE downloads (the option chain and SPX 1-minute price bars),
and only when you click "Refresh Data".
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import streamlit as st

import config
import data_fetcher
import exposures as exp_mod
import gex_calculator as gc
import visualizer as viz
import vol_metrics as vm

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
VOLUME_CAVEAT = (
    "Volume-weighted GEX is a rough intraday proxy, not true positioning: volume counts both the "
    "buyer and the seller of every trade and does not say whether positions were opened or closed. "
    "Open interest is prior-night and static intraday; volume only shows where activity is today."
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
    show_price = st.toggle("Show SPX price panel", value=True,
                           help="SPX 1-minute candles for the latest session, beside the GEX bars on the same "
                                "strike axis, so price can be read against the walls, flip and ±1σ lines.")
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
    mode = st.radio(
        "GEX weighting",
        options=list(config.WEIGHTING_MODES),
        format_func=lambda k: config.WEIGHTING_MODES[k],
        index=list(config.WEIGHTING_MODES).index(config.DEFAULT_WEIGHTING_MODE),
        help="Contract count that multiplies each contract's dollar gamma. Open interest = standard GEX "
             "(prior-night positions). Today's volume = where trading is happening now. " + VOLUME_CAVEAT,
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
    # Price bars are fetched alongside a successful chain download. A failure here
    # only hides the price panel; the GEX data is still updated. Old bars are
    # dropped rather than shown next to a newer chain.
    if "fetch_error" not in st.session_state:
        with st.spinner("Downloading SPX 1-minute price bars from CBOE..."):
            try:
                st.session_state["intraday"] = data_fetcher.fetch_spx_intraday()
                st.session_state.pop("intraday_error", None)
            except data_fetcher.DataFetchError as exc:
                st.session_state["intraday"] = None
                st.session_state["intraday_error"] = str(exc)

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
# Volume modes also keep zero-OI contracts that traded today (strikes opened today).
prepared, iv_report = gc.prepare_chain(snap.chain, snap.spot, r=r, q=q, keep_traded=mode != "oi")
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

# Weighting. With no volume in this selection (weekend, pre-market) the volume
# views would be empty, so fall back to open interest and say so.
sel_rows = prepared if selected is None else prepared[prepared["expiry"] == selected]
has_volume = bool((sel_rows["volume"] > 0).any())
if mode != "oi" and not has_volume:
    st.info("No contracts in this selection have traded today (volume is zero everywhere, as on weekends "
            "or before the open), so volume-weighted GEX can't be shown. Showing open-interest weighting.")
weight = "volume" if (mode == "volume" and has_volume) else "open_interest"
by_vol = weight == "volume"

res = gc.run_analysis(prepared, snap.spot, convention=convention, r=r, q=q,
                      expiry=selected, display_range_pct=range_pct, weight=weight)
res_vol = (gc.run_analysis(prepared, snap.spot, convention, r, q, selected, range_pct, weight="volume")
           if mode == "compare" and has_volume else None)

# Expected move: what options are pricing, from the ATM straddle / ATM IV.
moves = vm.expected_moves(snap.chain, snap.spot, r=r, q=q)
em = vm.pick(moves, selected)
if em is None:
    em_label = em_note = ""
else:
    when = f"{em.expiry:%a %d %b}" + (" - nearest expiry" if selected is None else "")
    em_label = f"±1σ expected move ({when})"
    em_note = viz.em_note(em, em_label, res)

# ---------------------------------------------------------------------------
# Summary panel
# ---------------------------------------------------------------------------
cols = st.columns(6)
card(cols[0], "SPX Spot", f"{res.spot:,.2f}", C["spot"])
vol_tag = " (by volume)" if by_vol else ""
vol_tip = (" Weighted by today's volume. " + VOLUME_CAVEAT) if by_vol else ""
card(cols[1], "Call Wall" + vol_tag, f"{res.call_wall:,.0f}" if res.call_wall else "n/a", C["call"],
     "Strike with the largest unsigned call dollar gamma." + vol_tip)
card(cols[2], "Put Wall" + vol_tag, f"{res.put_wall:,.0f}" if res.put_wall else "n/a", C["put"],
     "Strike with the largest unsigned put dollar gamma." + vol_tip)
card(cols[3], "Total Net GEX" + vol_tag, viz.fmt_usd(res.total_net_gex),
     C["call"] if res.total_net_gex >= 0 else C["put"],
     "$ of SPX dealers would trade per 1% move, under the assumed sign convention. " + SIGN_HELP + vol_tip)
card(cols[4], res.flip_label, f"{res.gamma_flip:,.2f}" if res.gamma_flip else "none in ±20%", C["flip"],
     "SPX level where total net GEX, recomputed at hypothetical spots, crosses zero (nearest to spot)." + vol_tip)
regime_color = C["call"] if res.regime.key == "positive" else C["put"]
card(cols[5], "Gamma Regime", res.regime.key.capitalize(), regime_color)

EM_HELP = ("What the options market is pricing, not a forecast. 1σ = spot × ATM IV × √T: about 68% of "
           "the implied distribution lies inside it. The ATM straddle is ≈0.8 × the 1σ move "
           "(expected absolute move), so the two are different numbers.")
em_cols = st.columns([2, 1], gap="small")
if em is None:
    card(em_cols[0], "Expected move (1σ)", "n/a", C["em_edge"],
         "No expiry in this selection has an ATM call and put that both have bid > 0 and ask > 0.")
else:
    em_scope = f"{em.expiry:%d %b}" + (" · nearest expiry" if selected is None else "")
    card(em_cols[0], f"Expected move (1σ) · {em_scope}",
         f"±{em.move_pts:,.1f} pts (±{em.move_pct:.2%}) → [{em.low:,.0f} – {em.high:,.0f}]",
         C["em_edge"], EM_HELP)
    card(em_cols[1], f"ATM straddle · {em.atm_strike:,.0f} strike",
         f"{em.straddle:,.2f} pts (±{em.straddle / res.spot:.2%})", C["em_edge"],
         "Call mid + put mid at the ATM strike: the straddle-based move, ≈0.8 × the 1σ move. " + EM_HELP)

base_scope = "all expirations combined" if selected is None else f"the {selected:%d %b %Y} expiry only"
scope = base_scope
scope +=", weighted by today's volume (rough intraday proxy, not positioning)" if by_vol else ""
if mode == "compare" and res_vol is not None:
    scope += " (open-interest weighting; the volume-weighted version is in the GEX by strike tab)"
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
t_strike, t_exp, t_prof, t_cv, t_quality, t_method = st.tabs(
    ["GEX by strike", "Per-expiry breakdown", "Gamma profile", "Charm & Vanna", "Data quality", "Methodology"]
)

with t_strike:
    title = "SPX Gamma Exposure by Strike - " + ("all expirations" if selected is None else f"{selected:%d %b %Y}")
    intraday: data_fetcher.IntradayBars | None = st.session_state.get("intraday")
    price = intraday.bars if (show_price and intraday is not None) else None
    if show_price and (perr := st.session_state.get("intraday_error")):
        st.warning("SPX price bars could not be downloaded, so the price panel is hidden "
                   f"(GEX data was still updated).\n\n{perr}")
    if res_vol is None:
        st.plotly_chart(viz.gex_strike_chart(res, show_net, show_agg, range_pct, title + (" - by volume" if by_vol else ""),
                                             bucket, em=em, em_label=em_label, note=em_note, price=price),
                        width="stretch")
    else:
        # Side by side on wide screens; Streamlit stacks columns on narrow ones.
        c_oi, c_vol = st.columns(2)
        for col_, r_, suffix in ((c_oi, res, "by open interest"), (c_vol, res_vol, "by volume")):
            note_ = viz.em_note(em, em_label, r_)
            col_.plotly_chart(viz.gex_strike_chart(r_, show_net, show_agg, range_pct, f"GEX {suffix}", bucket,
                                                   em=em, em_label=em_label, note=note_, price=price),
                              width="stretch")
    if price is not None:
        last = price["time"].iloc[-1]
        st.caption(f"Price panel: CBOE SPX 1-minute bars for {last:%a %d %b %Y}, last bar {last:%H:%M} ET "
                   f"(delayed ≥15 min). Levels are the current GEX snapshot drawn across the whole session; "
                   "walls and flip were not necessarily at these levels earlier in the day. Narrow the strike "
                   "range in the sidebar to zoom both panels.")
        st.markdown("**Open interest vs today's volume**")
        st.dataframe(viz.comparison_table(res, res_vol), hide_index=True, width="stretch")
    if mode != "oi" and has_volume:
        st.caption("ⓘ " + VOLUME_CAVEAT)

    with st.expander("Volume / OI: where today's activity is large relative to open interest",
                     expanded=mode != "oi"):
        vo = gc.volume_oi_ratios(sel_rows, config.VOLUME_OI_TOP_N)
        if vo.empty:
            st.info("No contracts with both open interest and volume today in this selection.")
        else:
            st.dataframe(pd.DataFrame({
                "Expiry": pd.to_datetime(vo["expiry"]).dt.strftime("%Y-%m-%d"),
                "Strike": vo["strike"].map("{:,.0f}".format),
                "Type": vo["type"].str.capitalize(),
                "Volume (contracts)": vo["volume"].map("{:,.0f}".format),
                "OI (contracts)": vo["open_interest"].map("{:,.0f}".format),
                "Volume / OI (×)": vo["ratio"].map("{:,.2f}".format),
            }), hide_index=True, width="stretch")
            st.caption(f"Top {config.VOLUME_OI_TOP_N} contracts by today's volume ÷ start-of-day open interest "
                       "(OI > 0 only). High ratios suggest new positions may be opening today. Volume alone "
                       "can't confirm it: it counts buyers and sellers and doesn't show opens vs closes.")

with t_exp:
    # Always computed across ALL expiries so near vs far-dated GEX can be compared.
    full = res if selected is None else gc.run_analysis(prepared, snap.spot, convention, r, q, None, range_pct,
                                                        weight=weight)
    st.plotly_chart(viz.expiry_breakdown_chart(full.by_expiry, max_exp), width="stretch")
    if by_vol:
        st.caption("Weighted by today's volume. " + VOLUME_CAVEAT)
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

    st.subheader("Expected move by expiry")
    if moves:
        emt = vm.to_frame(moves)
        st.dataframe(
            pd.DataFrame({
                "Expiry": pd.to_datetime(emt["expiry"]).dt.strftime("%Y-%m-%d"),
                "Root": emt["root"],
                "Days": emt["dte"].map("{:.1f}".format),
                "ATM strike": emt["atm_strike"].map("{:,.0f}".format),
                "ATM IV (%)": (emt["atm_iv"] * 100).map("{:.2f}".format),
                "Straddle (pts)": emt["straddle"].map("{:,.2f}".format),
                "1σ move (pts)": emt["move_pts"].map("±{:,.2f}".format),
                "1σ move (%)": (emt["move_pct"] * 100).map("±{:.2f}".format),
                "1σ low": emt["low"].map("{:,.2f}".format),
                "1σ high": emt["high"].map("{:,.2f}".format),
            }),
            hide_index=True, width="stretch",
        )
        st.caption("ATM strike = strike nearest spot with a call and a put that both have bid > 0 and ask > 0. "
                   "ATM IV = average of that call's and put's IV (CBOE feed, else back-solved from the mid). "
                   "1σ move = spot × ATM IV × √T (index points). Straddle = call mid + put mid, ≈0.8 × the 1σ "
                   "move. On dates with both roots, the PM-settled SPXW pair is used. These describe what "
                   "options are pricing now, not a forecast.")
    else:
        st.info("No expiry has an ATM call and put with two-sided quotes, so no expected move can be shown.")

with t_prof:
    st.plotly_chart(viz.gamma_profile_chart(res), width="stretch")
    if len(res.all_flips) > 1:
        nearest = sorted(res.all_flips, key=lambda f: abs(f - res.spot))[:8]
        st.caption(f"{len(res.all_flips)} zero crossings found within ±{config.FLIP_SEARCH_RANGE_PCT:.0%}; "
                   "nearest to spot: " + ", ".join(f"{f:,.0f}" for f in sorted(nearest))
                   + ". The headline flip is the one nearest spot. Many crossings usually mean "
                   "positioning is fragmented (often by short-dated expiries).")

with t_cv:
    # Same contracts as the GEX view: selected expiry, and the active weighting
    # (open interest in "Compare both").
    ex = exp_mod.compute_exposures(res.contracts, res.spot, convention, r, q, weight=res.weight)
    w_txt = config.GEX_WEIGHTINGS[res.weight]
    a_, b_ = st.columns(2)
    card(a_, "Net charm (CEX)", f"{viz.fmt_usd(ex.net_charm)} per day",
         C["call"] if ex.net_charm >= 0 else C["put"],
         "$ change in the assumed dealer delta per calendar day that passes, price and IV unchanged. "
         + SIGN_HELP)
    card(b_, "Net vanna (VEX)", f"{viz.fmt_usd(ex.net_vanna)} per vol point",
         C["call"] if ex.net_vanna >= 0 else C["put"],
         "$ change in the assumed dealer delta per 1 vol-point rise in implied volatility, price unchanged. "
         + SIGN_HELP)
    st.caption(f"Computed from {base_scope}, weighted by {w_txt}, under the assumed sign convention "
               f"({config.SIGN_CONVENTIONS[convention]['label']}). Units: $ of SPX delta.")

    cv_scope = "all expirations" if selected is None else f"{selected:%d %b %Y}"
    st.plotly_chart(viz.exposure_strike_chart(ex.by_strike, res, "charm", range_pct,
                                              f"SPX Charm Exposure by Strike - {cv_scope}", bucket,
                                              em=em, em_label=em_label, note=viz.em_note(em, em_label, res)),
                    width="stretch")
    sell_buy = "sell" if ex.net_charm >= 0 else "buy"
    st.markdown(
        f"**Charm** is how dealer hedges would shift as time passes with price (and IV) unchanged: option "
        f"deltas drift toward 0 or 1 as expiry approaches. The drift speeds up near expiry, so it matters "
        f"most into the close, on 0DTE and around monthly OPEX. Under the assumed convention, a **positive** "
        f"total means the dealer delta rises as time passes, so staying hedged means *selling* about that "
        f"much SPX exposure per day; a **negative** total means *buying*. Now: **{viz.fmt_usd(ex.net_charm)} "
        f"per day** → hedging would mean {sell_buy}ing roughly {viz.fmt_usd(abs(ex.net_charm), signed=False)} "
        f"of SPX per day if nothing else changed. This describes hedging needs under an assumption, not a "
        f"price forecast. T is floored at 30 minutes, so 0DTE charm stays finite but large."
    )

    st.plotly_chart(viz.exposure_strike_chart(ex.by_strike, res, "vanna", range_pct,
                                              f"SPX Vanna Exposure by Strike - {cv_scope}", bucket,
                                              em=em, em_label=em_label, note=viz.em_note(em, em_label, res)),
                    width="stretch")
    st.markdown(
        "**Vanna** is how dealer hedges would shift when implied volatility moves with price unchanged, for "
        "example as IV falls after a scheduled event. Under the assumed convention, a **positive** total means "
        "a 1-point IV *rise* raises the dealer delta (hedge: *sell*) and an IV *fall* lowers it (hedge: *buy*); "
        "a **negative** total means the reverse. Now: **"
        f"{viz.fmt_usd(ex.net_vanna)} per vol point**. This describes hedging sensitivity under an assumption, "
        "not a price forecast; real IV moves are rarely uniform across strikes."
    )
    if mode == "compare" and res_vol is not None:
        st.caption("'Compare both' is selected: charm and vanna here use open-interest weighting.")
    elif by_vol:
        st.caption(VOLUME_CAVEAT)

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
    if (bars_ := st.session_state.get("intraday")) is not None:
        st.caption(f"Price bars: {bars_.source_url} · {len(bars_.bars):,} bars · "
                   f"CBOE file timestamp {bars_.cboe_timestamp or 'n/a'}")

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

**GEX weighting:** open interest (default) is the standard GEX: prior-night positions, static
intraday. "Today's volume" runs the identical pipeline (same dollar-gamma formula and sign
assumption) with today's volume in place of open interest, so walls, profile, the zero-gamma level
and regime show where today's activity is concentrated. {VOLUME_CAVEAT} Buyer/seller classification
of individual trades is not attempted: the free feed only carries each contract's last trade.

**Charm & vanna exposure:** Black-Scholes vanna (∂Δ/∂σ) and charm (−∂Δ/∂T, per year) with the same
*r*, *q*, *T* and IV as gamma. VEX = sign × vanna × weight × 100 × S × 0.01 ($ of delta change per
1 vol point); CEX = sign × charm/365 × weight × 100 × S ($ of delta change per calendar day). Sign is
the same dealer assumption; weight follows the GEX weighting control.

**Expected move (amber band):** for each expiry, the ATM strike is the strike nearest spot where
both the call and the put have bid > 0 and ask > 0. ATM IV is the average of their IVs.
1σ move = S × ATM IV × √T in index points (same *T* as above); the band is spot ± that. The
straddle (call mid + put mid) is also shown: it is ≈0.8 × the 1σ move, not the same number. In the
"all expirations" view the band uses the nearest non-expired expiry. This is market pricing of
move size, not a forecast of direction or of the realised move.

**Limits:** open interest is start-of-day; quotes are delayed ≥15 minutes; the dealer position
is assumed, not observed. {NOT_A_FORECAST}
"""
    )
