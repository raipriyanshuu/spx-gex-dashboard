"""
visualizer.py - Plotly figures for the dashboard (dark trading theme).

Main chart layout:
  * Horizontal bars (bottom x-axis): per-strike call GEX and put GEX, signed by
    the assumed dealer position (standard convention: calls right/green,
    puts left/red).
  * Two lines (top x-axis, same strike y-axis):
      - cyan  "Net GEX (cumulative)": running sum of per-strike net GEX at the
        actual current spot.
      - purple "Aggregate GEX": total GEX recomputed as if each strike were spot.
  * Horizontal reference lines: Spot (blue), Call Wall (green), Put Wall (red),
    Gamma Flip (dashed purple), each labelled with its exact level.
  * Optional +/-1 sigma expected-move band (faint amber, dotted edges labelled
    with their levels). It shows what options are pricing, not a forecast.
Both x-axes are symmetric around zero so their zero points line up.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.graph_objects as go

import config
from gex_calculator import GexResult
from vol_metrics import ExpectedMove, walls_vs_range

C = config.COLORS
FONT = "Inter, Segoe UI, Roboto, Helvetica Neue, Arial, sans-serif"


def fmt_usd(x: float | None, signed: bool = True) -> str:
    """$1.23B / $456.7M / $12.3K, with an explicit sign."""
    if x is None or not np.isfinite(x):
        return "n/a"
    sign = "-" if x < 0 else ("+" if signed and x > 0 else "")
    a = abs(x)
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            return f"{sign}${a / div:,.2f}{suf}"
    return f"{sign}${a:,.0f}"


def _base_layout(fig: go.Figure, height: int) -> None:
    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor=C["bg"],
        plot_bgcolor=C["bg"],
        font=dict(family=FONT, color=C["text"], size=12),
        height=height,
        margin=dict(l=70, r=30, t=70, b=110),
        legend=dict(orientation="h", yanchor="top", y=-0.09, xanchor="center", x=0.5,
                    bgcolor="rgba(0,0,0,0)", font=dict(size=12)),
        hoverlabel=dict(bgcolor=C["panel"], bordercolor=C["grid"], font=dict(family=FONT, size=12)),
    )


def _unit(values) -> tuple[float, str]:
    """Pick $B / $M / $K so axis ticks read like a trading screen (not SI 'G')."""
    m = float(np.nanmax(np.abs(values))) if len(values) else 0.0
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if m >= div:
            return div, suf
    return 1.0, ""


def _sym_range(values: np.ndarray, pad: float = 1.12) -> list[float]:
    m = float(np.nanmax(np.abs(values))) if len(values) else 1.0
    m = m if m > 0 else 1.0
    return [-m * pad, m * pad]


GEX_BUCKET_COLS = ["call_gex", "put_gex", "net_gex", "call_oi", "put_oi", "call_vol", "put_vol"]


def _bucket_strikes(d: pd.DataFrame, bucket: float, cols: list[str] = GEX_BUCKET_COLS) -> pd.DataFrame:
    """Group 5-point SPX strikes into wider buckets so bars are thick enough to read."""
    if bucket <= 5:
        out = d.copy()
        out["label"] = [f"{k:,.0f}" for k in out["strike"]]
        return out
    b = d.copy()
    b["strike"] = (np.floor(b["strike"] / bucket) * bucket) + bucket / 2   # bar sits at bucket centre
    out = b.groupby("strike", as_index=False)[cols].sum()
    out["label"] = [f"{k - bucket / 2:,.0f}-{k + bucket / 2 - 5:,.0f}" for k in out["strike"]]
    return out


def _title_with_note(title: str, note: str) -> str:
    """Title plus an optional muted note; the note may hold several lines joined by <br>."""
    if not note:
        return title
    return f"{title}<br><span style='font-size:12px;color:{C['muted']}'>{note}</span>"


def em_note(em: ExpectedMove | None, em_label: str, res: GexResult) -> str:
    """Two-line chart note: what the amber band is, and whether each wall sits inside it."""
    if em is None:
        return ""
    return f"Amber band = {em_label}.<br>" + walls_vs_range(em, res.call_wall, res.put_wall)


def _top_margin(note: str) -> int:
    return 120 + (18 * (note.count("<br>") + 1) if note else 0)


def _right_margin(res: GexResult) -> int:
    """Room for the right-margin tags; widened for the longer 'Zero gamma (by volume)' label."""
    return max(170, 7 * len(f"{res.flip_label}  0,000.00") + 24)


def _place_labels(levels: list[float], lo: float, hi: float, min_gap: float) -> list[float]:
    """
    Label y-positions for ascending `levels`: pushed apart by at least min_gap,
    first upward, then (if the stack runs past the top) back down, so close
    levels never overprint and no label leaves the plotted range.
    """
    placed: list[float] = []
    for y in levels:
        placed.append(max(y, placed[-1] + min_gap) if placed else y)
    for i in range(len(placed) - 1, -1, -1):
        cap = hi if i == len(placed) - 1 else placed[i + 1] - min_gap
        placed[i] = min(placed[i], cap)
    return placed


def _add_reference_lines(fig: go.Figure, res: GexResult, lo: float, hi: float,
                         em: ExpectedMove | None = None, em_label: str = "") -> None:
    """
    Horizontal reference lines across the plot (Spot, walls, flip), plus the
    optional +/-1 sigma expected-move band, with labels as tags in the right
    margin. Shared by the GEX chart and the charm/vanna charts.
    """
    refs = [
        (res.call_wall, "Call Wall", C["call"], "solid", 0),
        (res.spot, "Spot", C["spot"], "solid", 2),
        (res.gamma_flip, res.flip_label, C["flip"], "dash", 2),
        (res.put_wall, "Put Wall", C["put"], "solid", 0),
    ]
    if em is not None:
        # Shaded band between the 1-sigma edges (clipped to the visible strikes).
        y0, y1 = max(em.low, lo), min(em.high, hi)
        if y0 < y1:
            fig.add_hrect(y0=y0, y1=y1, fillcolor=C["em_band"], line_width=0, layer="below")
        refs += [(em.high, "+1σ", C["em_edge"], "dot", 2), (em.low, "−1σ", C["em_edge"], "dot", 2)]
        # Legend entry for the band (shapes have no legend item of their own).
        fig.add_trace(go.Scatter(
            x=[None], y=[None], mode="markers", name=em_label or "±1σ expected move",
            marker=dict(symbol="square", size=12, color=C["em_edge"], opacity=0.5), hoverinfo="skip",
        ))
    refs = sorted((r for r in refs if r[0] is not None and lo <= r[0] <= hi), key=lambda r: r[0])
    placed = _place_labels([r[0] for r in refs], lo, hi, (hi - lo) * 0.045)
    for (y, name, color, dash, dec), ly in zip(refs, placed):
        fig.add_shape(type="line", xref="paper", x0=0, x1=1, yref="y", y0=y, y1=y,
                      line=dict(color=color, width=2 if dash == "solid" else 1.8, dash=dash), layer="above")
        fig.add_annotation(
            xref="paper", x=1.008, xanchor="left", yref="y", y=ly, yanchor="middle",
            text=f"<b>{name}</b>  {y:,.{dec}f}", showarrow=False, align="left",
            font=dict(color=C["bg"], size=12, family=FONT),
            bgcolor=color, bordercolor=color, borderpad=4,
        )


def gex_strike_chart(
    res: GexResult,
    show_net_line: bool = True,
    show_agg_line: bool = True,
    range_pct: float = config.DEFAULT_STRIKE_RANGE_PCT,
    title: str = "SPX Gamma Exposure by Strike",
    bucket: float = config.DEFAULT_STRIKE_BUCKET,
    em: ExpectedMove | None = None,
    em_label: str = "",
    note: str = "",
) -> go.Figure:
    """`em`: optional expected move drawn as a +/-1 sigma band; `note`: one line under the title."""
    lo, hi = res.spot * (1 - range_pct), res.spot * (1 + range_pct)
    raw = res.by_strike[(res.by_strike["strike"] >= lo) & (res.by_strike["strike"] <= hi)]
    d = _bucket_strikes(raw, bucket)

    custom = np.column_stack(
        [
            d["label"],
            [fmt_usd(v) for v in d["call_gex"]],
            [fmt_usd(v) for v in d["put_gex"]],
            [fmt_usd(v) for v in d["net_gex"]],
            [f"{v:,.0f}" for v in d["call_oi"]],
            [f"{v:,.0f}" for v in d["put_oi"]],
            [f"{v:,.0f}" for v in d["call_vol"]],
            [f"{v:,.0f}" for v in d["put_vol"]],
        ]
    ) if len(d) else None
    hover = (
        "<b>Strike %{customdata[0]}</b><br>"
        "<span style='color:" + C["call"] + "'>Call GEX</span>: %{customdata[1]}<br>"
        "<span style='color:" + C["put"] + "'>Put GEX</span>: %{customdata[2]}<br>"
        "<b>Net GEX: %{customdata[3]}</b><br>"
        "Call OI %{customdata[4]} | Put OI %{customdata[5]}<br>"
        "Call vol %{customdata[6]} | Put vol %{customdata[7]}"
        "<extra></extra>"
    )
    by_vol = " · weighted by today's volume" if res.weight == "volume" else ""

    bar_extent = np.concatenate([d["call_gex"].to_numpy(), d["put_gex"].to_numpy()]) if len(d) else np.array([0.0])
    bdiv, bsuf = _unit(bar_extent)
    line_extent = []
    if show_net_line and len(raw):
        line_extent.append(raw["net_gex_cumulative"].to_numpy())
    if show_agg_line and len(res.aggregate_at_strikes):
        line_extent.append(res.aggregate_at_strikes.to_numpy())
    line_extent = np.concatenate(line_extent) if line_extent else np.array([0.0])
    ldiv, lsuf = _unit(line_extent)

    fig = go.Figure()

    # 1) Regime background: tint the strike range above/below the flip by the sign
    #    the gamma profile has on that side (works for either sign convention).
    if res.gamma_flip is not None and lo < res.gamma_flip < hi:
        above = np.interp(min(res.gamma_flip * 1.01, hi), res.profile_levels, res.profile_values)
        up_col, dn_col = (C["zone_pos"], C["zone_neg"]) if above > 0 else (C["zone_neg"], C["zone_pos"])
        fig.add_hrect(y0=res.gamma_flip, y1=hi, fillcolor=up_col, line_width=0, layer="below")
        fig.add_hrect(y0=lo, y1=res.gamma_flip, fillcolor=dn_col, line_width=0, layer="below")

    # 2) Bars
    for col, name, color in (("call_gex", "Call GEX", C["call"]), ("put_gex", "Put GEX", C["put"])):
        fig.add_trace(go.Bar(
            y=d["strike"], x=d[col] / bdiv, orientation="h", name=name,
            marker=dict(color=color, line=dict(width=0), cornerradius=2), opacity=0.92,
            width=bucket * 0.82 if bucket > 5 else None,
            customdata=custom, hovertemplate=hover,
        ))

    # 3) Lines (top axis). Smoothed, and the aggregate line drawn slightly softer
    #    so it doesn't overpower the bars.
    if show_agg_line and len(res.aggregate_at_strikes):
        agg = res.aggregate_at_strikes
        fig.add_trace(go.Scatter(
            y=agg.index, x=agg.values / ldiv, xaxis="x2", mode="lines",
            name="Aggregate GEX (spot = strike)", opacity=0.9,
            line=dict(color=C["agg_line"], width=2.2, shape="spline", smoothing=0.6),
            customdata=[fmt_usd(v) for v in agg.values],
            hovertemplate="If SPX were at %{y:,.0f}<br>Total GEX: %{customdata}<extra></extra>",
        ))
    if show_net_line and len(raw):
        fig.add_trace(go.Scatter(
            y=raw["strike"], x=raw["net_gex_cumulative"] / ldiv, xaxis="x2", mode="lines",
            name="Net GEX (cumulative)",
            line=dict(color=C["net_line"], width=2.6, shape="spline", smoothing=0.6),
            customdata=[fmt_usd(v) for v in raw["net_gex_cumulative"]],
            hovertemplate="Strike %{y:,.0f}<br>Net GEX (cumulative): %{customdata}<extra></extra>",
        ))

    # 4) Expected-move band + reference lines (labels as right-margin tags).
    _add_reference_lines(fig, res, lo, hi, em, em_label)

    n_rows = max(len(d), 1)
    height = int(np.clip(n_rows * 17 + 260, 820, 1400))
    _base_layout(fig, height=height)
    fig.update_layout(
        title=dict(text=_title_with_note(title, note), x=0.005, y=0.985, yanchor="top", font=dict(size=18)),
        margin=dict(l=80, r=_right_margin(res), t=_top_margin(note), b=110),
        barmode="relative",
        bargap=0.18,
        hovermode="closest",
        font=dict(size=13),
        xaxis=dict(title=dict(text=f"GEX per strike  (${bsuf} per 1% SPX move{by_vol})", font=dict(size=13)),
                   range=_sym_range(bar_extent / bdiv, pad=1.08), tickformat=",.2~f",
                   gridcolor=C["grid_soft"], zeroline=True, zerolinecolor=C["muted"], zerolinewidth=1.5,
                   ticks="outside", tickcolor=C["grid"]),
        xaxis2=dict(title=dict(text=f"Net GEX (cumulative) / Aggregate GEX  (${lsuf} per 1% move)",
                               font=dict(color=C["muted"], size=12)),
                    overlaying="x", side="top", range=_sym_range(line_extent / ldiv, pad=1.08),
                    tickmode="auto", nticks=8, tickformat=",.0f", showgrid=False, zeroline=False,
                    tickfont=dict(color=C["muted"])),
        yaxis=dict(title=dict(text="Strike", font=dict(size=13)), range=[lo, hi],
                   gridcolor=C["grid_soft"], tickformat=",.0f", dtick=25 if (hi - lo) <= 700 else 50,
                   ticks="outside", tickcolor=C["grid"], tickfont=dict(size=12)),
        legend=dict(font=dict(size=13), itemsizing="constant", y=-0.07),
    )
    return fig


EXPOSURE_KINDS = {
    # kind: (column prefix, display name, unit text for axis / hover)
    "charm": ("cex", "Charm", "delta change per calendar day"),
    "vanna": ("vex", "Vanna", "delta change per 1 vol point"),
}


def exposure_strike_chart(
    by_strike: pd.DataFrame,
    res: GexResult,
    kind: str,
    range_pct: float = config.DEFAULT_STRIKE_RANGE_PCT,
    title: str = "",
    bucket: float = config.DEFAULT_STRIKE_BUCKET,
    em: ExpectedMove | None = None,
    em_label: str = "",
    note: str = "",
) -> go.Figure:
    """
    Charm or vanna exposure by strike (exposures.ExposureResult.by_strike), on the
    same strike axis, range, bucketing and reference lines as gex_strike_chart().
    Bars: call and put contributions; diamonds: net per strike.
    """
    pre, name, unit = EXPOSURE_KINDS[kind]
    cols = [f"call_{pre}", f"put_{pre}", f"net_{pre}"]
    lo, hi = res.spot * (1 - range_pct), res.spot * (1 + range_pct)
    raw = by_strike[(by_strike["strike"] >= lo) & (by_strike["strike"] <= hi)]
    d = _bucket_strikes(raw, bucket, cols)

    extent = np.concatenate([d[c].to_numpy() for c in cols]) if len(d) else np.array([0.0])
    div, suf = _unit(extent)
    custom = np.column_stack([d["label"]] + [[fmt_usd(v) for v in d[c]] for c in cols]) if len(d) else None
    hover = (
        "<b>Strike %{customdata[0]}</b><br>"
        f"<span style='color:{C['call']}'>Call {name.lower()}</span>: %{{customdata[1]}}<br>"
        f"<span style='color:{C['put']}'>Put {name.lower()}</span>: %{{customdata[2]}}<br>"
        f"<b>Net: %{{customdata[3]}}</b> ({unit})<extra></extra>"
    )

    fig = go.Figure()
    for col, label, color in ((cols[0], f"Call {name.lower()}", C["call"]), (cols[1], f"Put {name.lower()}", C["put"])):
        fig.add_trace(go.Bar(
            y=d["strike"], x=d[col] / div, orientation="h", name=label,
            marker=dict(color=color, line=dict(width=0), cornerradius=2), opacity=0.92,
            width=bucket * 0.82 if bucket > 5 else None, customdata=custom, hovertemplate=hover,
        ))
    fig.add_trace(go.Scatter(
        y=d["strike"], x=d[cols[2]] / div, mode="markers", name=f"Net {name.lower()} per strike",
        marker=dict(symbol="diamond", size=7, color=C["net_line"], line=dict(color=C["bg"], width=1)),
        customdata=custom, hovertemplate=hover,
    ))
    _add_reference_lines(fig, res, lo, hi, em, em_label)

    height = int(np.clip(max(len(d), 1) * 14 + 260, 640, 1100))
    _base_layout(fig, height=height)
    fig.update_layout(
        title=dict(text=_title_with_note(title or f"SPX {name} Exposure by Strike", note),
                   x=0.005, y=0.985, yanchor="top", font=dict(size=17)),
        margin=dict(l=80, r=_right_margin(res), t=_top_margin(note) - 40, b=110),
        barmode="relative", bargap=0.18, hovermode="closest",
        xaxis=dict(title=dict(text=f"{name} exposure per strike  (${suf} of {unit})", font=dict(size=13)),
                   range=_sym_range(extent / div, pad=1.08), tickformat=",.2~f",
                   gridcolor=C["grid_soft"], zeroline=True, zerolinecolor=C["muted"], zerolinewidth=1.5,
                   ticks="outside", tickcolor=C["grid"]),
        yaxis=dict(title=dict(text="Strike", font=dict(size=13)), range=[lo, hi],
                   gridcolor=C["grid_soft"], tickformat=",.0f", dtick=25 if (hi - lo) <= 700 else 50,
                   ticks="outside", tickcolor=C["grid"], tickfont=dict(size=12)),
        legend=dict(font=dict(size=12), itemsizing="constant", y=-0.08),
    )
    return fig


def expiry_breakdown_chart(by_expiry: pd.DataFrame, max_expiries: int) -> go.Figure:
    d = by_expiry.sort_values("expiry").head(max_expiries).copy()
    div, suf = _unit(np.concatenate([d["call_gex"], d["put_gex"]]).astype(float))
    labels = [f"{e:%b %d}<br>{dte:.0f}d" for e, dte in zip(pd.to_datetime(d["expiry"]), d["dte"])]
    fig = go.Figure()
    for col, name, color in (("call_gex", "Call GEX", C["call"]), ("put_gex", "Put GEX", C["put"])):
        fig.add_trace(go.Bar(
            x=labels, y=d[col] / div, name=name, marker=dict(color=color, line=dict(width=0)), opacity=0.9,
            customdata=[fmt_usd(v) for v in d[col]],
            hovertemplate="%{x}<br>" + name + ": %{customdata}<extra></extra>",
        ))
    fig.add_trace(go.Scatter(
        x=labels, y=d["net_gex"] / div, name="Net GEX", mode="markers",
        marker=dict(symbol="diamond", size=10, color=C["net_line"], line=dict(color=C["bg"], width=1)),
        customdata=[fmt_usd(v) for v in d["net_gex"]],
        hovertemplate="%{x}<br>Net GEX: %{customdata}<extra></extra>",
    ))
    _base_layout(fig, height=440)
    fig.update_layout(
        barmode="relative",
        title=dict(text=f"GEX by expiration (nearest {len(d)})", x=0, font=dict(size=15)),
        yaxis=dict(title=f"${suf} per 1% SPX move", tickformat=",.2~f", gridcolor=C["grid"],
                   zeroline=True, zerolinecolor=C["muted"]),
        xaxis=dict(gridcolor=C["grid"]),
    )
    return fig


def comparison_table(oi: GexResult, vol: GexResult | None) -> pd.DataFrame:
    """Walls, flip and net GEX under each weighting, side by side (for 'Compare both')."""
    def col(res: GexResult | None) -> list[str]:
        if res is None:
            return ["n/a (no volume)"] * 4
        return [f"{res.call_wall:,.0f}" if res.call_wall else "n/a",
                f"{res.put_wall:,.0f}" if res.put_wall else "n/a",
                f"{res.gamma_flip:,.2f}" if res.gamma_flip else "none in ±20%",
                fmt_usd(res.total_net_gex)]
    return pd.DataFrame({
        "Metric": ["Call Wall (strike)", "Put Wall (strike)", "Gamma flip / zero gamma (SPX level)",
                   "Total net GEX ($ per 1% SPX move)"],
        "Open interest": col(oi),
        "Today's volume": col(vol),
    })


def gamma_profile_chart(res: GexResult) -> go.Figure:
    x, y_raw = res.profile_levels, res.profile_values
    div, suf = _unit(y_raw)
    y = y_raw / div
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=x, y=np.where(y >= 0, y, 0), fill="tozeroy", mode="none",
                             fillcolor="rgba(34,197,94,0.18)", hoverinfo="skip", showlegend=False))
    fig.add_trace(go.Scatter(x=x, y=np.where(y < 0, y, 0), fill="tozeroy", mode="none",
                             fillcolor="rgba(239,68,68,0.18)", hoverinfo="skip", showlegend=False))
    fig.add_trace(go.Scatter(
        x=x, y=y, mode="lines", name="Total GEX at hypothetical spot",
        line=dict(color=C["agg_line"], width=2.4),
        customdata=[fmt_usd(v) for v in y_raw],
        hovertemplate="If SPX = %{x:,.0f}<br>Total GEX: %{customdata}<extra></extra>",
    ))
    fig.add_vline(x=res.spot, line=dict(color=C["spot"], width=1.6),
                  annotation=dict(text=f"<b>Spot {res.spot:,.2f}</b>", font=dict(color=C["spot"])),
                  annotation_position="top left")
    if res.gamma_flip is not None:
        fig.add_vline(x=res.gamma_flip, line=dict(color=C["flip"], width=1.6, dash="dash"),
                      annotation=dict(text=f"<b>{res.flip_label} {res.gamma_flip:,.2f}</b>",
                                      font=dict(color=C["flip"])),
                      annotation_position="bottom right")
    by_vol = " (weighted by today's volume)" if res.weight == "volume" else ""
    _base_layout(fig, height=420)
    fig.update_layout(
        title=dict(text=f"Gamma profile: total net GEX vs hypothetical SPX level{by_vol}", x=0, font=dict(size=15)),
        xaxis=dict(title="Hypothetical SPX level", tickformat=",.0f", gridcolor=C["grid"]),
        yaxis=dict(title=f"Total net GEX (${suf} per 1% move)", tickformat=",.2~f", gridcolor=C["grid"],
                   zeroline=True, zerolinecolor=C["muted"]),
        showlegend=False,
    )
    return fig