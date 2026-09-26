"""
gex_calculator.py - Black-Scholes gamma, dollar gamma, GEX aggregation,
gamma flip, call/put walls and regime classification.

WHAT GEX IS (AND ISN'T)
-----------------------
GEX here estimates how much delta hedging dealers would need to do for a 1%
move in SPX, GIVEN an assumed dealer position (see apply_dealer_sign). It
describes current positioning under that assumption. It is not a forecast.
For index options such as SPX, published research has documented a tendency
for dealer hedging flows to be associated with dampened (positive gamma) or
amplified (negative gamma) short-term price moves near these levels; that is
a historical tendency, not a guarantee about what price will do next.

Units: every GEX figure is dollars of underlying that dealers would buy/sell
per 1% move in SPX:
    dollar_gamma = gamma * S^2 * 0.01 * contract_multiplier * open_interest
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from scipy.optimize import brentq
from scipy.stats import norm

import config

ET = ZoneInfo("America/New_York")
_SQRT_2PI = np.sqrt(2.0 * np.pi)


# ---------------------------------------------------------------------------
# Black-Scholes primitives (vectorised with numpy broadcasting)
# ---------------------------------------------------------------------------
def bs_d1(S, K, T, sigma, r=0.0, q=0.0):
    return (np.log(S / K) + (r - q + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))


def bs_gamma(S, K, T, sigma, r=0.0, q=0.0):
    """Black-Scholes gamma (identical for calls and puts)."""
    S, K, T, sigma = (np.asarray(x, dtype=float) for x in (S, K, T, sigma))
    d1 = bs_d1(S, K, T, sigma, r, q)
    pdf = np.exp(-0.5 * d1**2) / _SQRT_2PI
    return np.exp(-q * T) * pdf / (S * sigma * np.sqrt(T))


def bs_price(S, K, T, sigma, r=0.0, q=0.0, is_call=True):
    d1 = bs_d1(S, K, T, sigma, r, q)
    d2 = d1 - sigma * np.sqrt(T)
    if is_call:
        return S * np.exp(-q * T) * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)
    return K * np.exp(-r * T) * norm.cdf(-d2) - S * np.exp(-q * T) * norm.cdf(-d1)


def implied_vol(price, S, K, T, r=0.0, q=0.0, is_call=True) -> float:
    """Back-solve IV from an option price with Brent's method. NaN if unsolvable."""
    if not (np.isfinite(price) and price > 0 and T > 0):
        return np.nan
    lo, hi = config.IV_SOLVE_LOW, config.IV_SOLVE_HIGH
    f = lambda s: bs_price(S, K, T, s, r, q, is_call) - price  # noqa: E731
    try:
        f_lo, f_hi = f(lo), f(hi)
        if f_lo > 0 or f_hi < 0:  # price below intrinsic or above any sane vol
            return np.nan
        return float(brentq(f, lo, hi, xtol=1e-6, maxiter=200))
    except (ValueError, RuntimeError):
        return np.nan


def dollar_gamma(gamma, S, open_interest):
    """$ of underlying traded per 1% move: gamma * S^2 * 0.01 * multiplier * OI."""
    return gamma * np.asarray(S, dtype=float) ** 2 * 0.01 * config.CONTRACT_MULTIPLIER * open_interest


# ---------------------------------------------------------------------------
# Chain preparation: time to expiry + implied volatility
# ---------------------------------------------------------------------------
def expiry_datetime_et(root: str, expiry: date) -> datetime:
    """SPX (AM-settled monthlies) settle at the open; SPXW settle at the 4pm close."""
    t = time(9, 30) if root == "SPX" else time(16, 0)
    return datetime.combine(expiry, t, tzinfo=ET)


@dataclass
class IVReport:
    """How implied volatility was obtained for each contract. Shown in the UI."""

    total_contracts: int = 0
    expired: int = 0
    zero_open_interest: int = 0
    iv_from_feed: int = 0
    iv_backsolved: int = 0
    excluded_no_iv: int = 0
    excluded_oi: float = 0.0
    detail: pd.DataFrame = field(default_factory=pd.DataFrame)


def prepare_chain(
    chain: pd.DataFrame,
    spot: float,
    now: datetime | None = None,
    r: float = config.DEFAULT_RISK_FREE_RATE,
    q: float = config.DEFAULT_DIVIDEND_YIELD,
) -> tuple[pd.DataFrame, IVReport]:
    """
    Add T (years) and a usable IV to each contract; drop what can't be used.

    IV policy:
      1. Use CBOE's own IV when it is a positive finite number.
      2. Otherwise (CBOE reports 0 / blank), back-solve IV from the bid/ask
         mid-price by inverting Black-Scholes. This mostly affects deep ITM/OTM
         or illiquid strikes; every such contract is counted in the report.
      3. If there is no two-sided quote, or the mid is outside no-arbitrage
         bounds, the contract is excluded and its open interest reported.
    """
    now = (now or datetime.now(ET)).astimezone(ET)
    rep = IVReport(total_contracts=len(chain))
    df = chain.copy()

    df["expiry_dt"] = [expiry_datetime_et(r_, e) for r_, e in zip(df["root"], df["expiry"])]
    secs = np.array([(e - now).total_seconds() for e in df["expiry_dt"]], dtype=float)
    expired = secs <= 0
    rep.expired = int(expired.sum())
    df = df[~expired].copy()
    df["T"] = np.maximum(secs[~expired] / config.SECONDS_PER_YEAR, config.MIN_T_YEARS)
    df["dte"] = secs[~expired] / 86400.0

    no_oi = df["open_interest"] <= 0
    rep.zero_open_interest = int(no_oi.sum())
    df = df[~no_oi].copy()

    iv = df["iv_feed"].to_numpy(dtype=float)
    feed_ok = np.isfinite(iv) & (iv > 0)
    rep.iv_from_feed = int(feed_ok.sum())
    df["iv"] = np.where(feed_ok, iv, np.nan)
    df["iv_source"] = np.where(feed_ok, "CBOE feed", "")

    detail_rows = []
    for idx in df.index[~feed_ok]:
        row = df.loc[idx]
        bid, ask = row["bid"], row["ask"]
        mid = (bid + ask) / 2 if (bid > 0 and ask > 0) else np.nan
        solved = implied_vol(mid, spot, row["strike"], row["T"], r, q, row["type"] == "call")
        if np.isfinite(solved):
            df.at[idx, "iv"] = solved
            df.at[idx, "iv_source"] = "Back-solved from mid"
            rep.iv_backsolved += 1
            status = "Back-solved from mid"
        else:
            rep.excluded_no_iv += 1
            rep.excluded_oi += float(row["open_interest"])
            status = "Excluded: no IV and no usable two-sided quote"
        detail_rows.append(
            {
                "symbol": row["symbol"], "expiry": row["expiry"], "type": row["type"],
                "strike": row["strike"], "open_interest": row["open_interest"],
                "bid": bid, "ask": ask, "iv_used": solved, "status": status,
            }
        )
    rep.detail = pd.DataFrame(detail_rows)
    df = df[np.isfinite(df["iv"])].reset_index(drop=True)
    return df, rep


# ---------------------------------------------------------------------------
# Signing (ASSUMPTION) and aggregation
# ---------------------------------------------------------------------------
def dealer_signs(types: pd.Series, convention: str) -> np.ndarray:
    """
    ASSUMPTION, NOT OBSERVED FACT: the dealer position sign for each contract.

    No public data reveals who actually holds each contract. The "standard"
    convention assumes end-users are structurally net sellers of calls
    (overwriting) and net buyers of puts (hedging); dealers, as liquidity
    providers, take the other side -> dealers long calls (+), short puts (-).
    See config.SIGN_CONVENTIONS to switch conventions.
    """
    conv = config.SIGN_CONVENTIONS[convention]
    return np.where(types.to_numpy() == "call", conv["call"], conv["put"])


def add_gex_columns(df: pd.DataFrame, spot: float, convention: str, r: float, q: float) -> pd.DataFrame:
    out = df.copy()
    out["gamma"] = bs_gamma(spot, out["strike"], out["T"], out["iv"], r, q)
    out["dollar_gamma"] = dollar_gamma(out["gamma"], spot, out["open_interest"])  # unsigned
    # Sign applied here is an ASSUMED dealer position - see dealer_signs().
    out["sign"] = dealer_signs(out["type"], convention)
    out["gex"] = out["sign"] * out["dollar_gamma"]
    return out


def aggregate_by_strike(df: pd.DataFrame) -> pd.DataFrame:
    is_call = df["type"] == "call"
    g = pd.DataFrame(
        {
            "strike": df["strike"],
            "call_raw": np.where(is_call, df["dollar_gamma"], 0.0),
            "put_raw": np.where(~is_call, df["dollar_gamma"], 0.0),
            "call_gex": np.where(is_call, df["gex"], 0.0),
            "put_gex": np.where(~is_call, df["gex"], 0.0),
            "call_oi": np.where(is_call, df["open_interest"], 0.0),
            "put_oi": np.where(~is_call, df["open_interest"], 0.0),
        }
    )
    by = g.groupby("strike", sort=True).sum()
    by["net_gex"] = by["call_gex"] + by["put_gex"]
    # "Net GEX (cumulative)": running sum of per-strike net GEX from the lowest
    # strike upward, all evaluated at the ACTUAL current spot.
    by["net_gex_cumulative"] = by["net_gex"].cumsum()
    return by.reset_index()


def aggregate_by_expiry(df: pd.DataFrame) -> pd.DataFrame:
    is_call = df["type"] == "call"
    g = pd.DataFrame(
        {
            "expiry": df["expiry"],
            "root": df["root"],
            "dte": df["dte"],
            "call_raw": np.where(is_call, df["dollar_gamma"], 0.0),
            "put_raw": np.where(~is_call, df["dollar_gamma"], 0.0),
            "call_gex": np.where(is_call, df["gex"], 0.0),
            "put_gex": np.where(~is_call, df["gex"], 0.0),
            "call_oi": np.where(is_call, df["open_interest"], 0.0),
            "put_oi": np.where(~is_call, df["open_interest"], 0.0),
        }
    )
    by = g.groupby("expiry", sort=True).agg(
        roots=("root", lambda s: "/".join(sorted(set(s)))),
        dte=("dte", "min"),
        contracts=("root", "size"),
        call_oi=("call_oi", "sum"),
        put_oi=("put_oi", "sum"),
        call_raw=("call_raw", "sum"),
        put_raw=("put_raw", "sum"),
        call_gex=("call_gex", "sum"),
        put_gex=("put_gex", "sum"),
    )
    by["net_gex"] = by["call_gex"] + by["put_gex"]
    total_abs = (by["call_raw"] + by["put_raw"]).sum()
    by["share_of_gamma"] = (by["call_raw"] + by["put_raw"]) / total_abs if total_abs else 0.0
    return by.reset_index()


# ---------------------------------------------------------------------------
# Gamma profile (hypothetical spots), flip, walls, regime
# ---------------------------------------------------------------------------
def gamma_profile(df: pd.DataFrame, spot_levels, convention: str, r: float, q: float, chunk: int = 64) -> np.ndarray:
    """
    Total net GEX recomputed as if SPX were at each hypothetical spot level.
    Each contract keeps its current IV (a "sticky-strike" simplification) and
    the dealer sign ASSUMPTION from dealer_signs().
    """
    levels = np.asarray(spot_levels, dtype=float)
    K = df["strike"].to_numpy(float)[None, :]
    T = df["T"].to_numpy(float)[None, :]
    iv = df["iv"].to_numpy(float)[None, :]
    w = (dealer_signs(df["type"], convention) * df["open_interest"].to_numpy(float)
         * 0.01 * config.CONTRACT_MULTIPLIER)[None, :]
    out = np.empty(len(levels))
    for i in range(0, len(levels), chunk):
        S = levels[i:i + chunk, None]
        out[i:i + chunk] = (bs_gamma(S, K, T, iv, r, q) * w).sum(axis=1) * S[:, 0] ** 2
    return out


def find_zero_crossings(levels: np.ndarray, values: np.ndarray) -> list[float]:
    """Linear-interpolated points where the profile changes sign."""
    crossings = []
    for i in range(len(values) - 1):
        a, b = values[i], values[i + 1]
        if a == 0:
            crossings.append(float(levels[i]))
        elif a * b < 0:
            crossings.append(float(levels[i] - a * (levels[i + 1] - levels[i]) / (b - a)))
    return crossings


@dataclass
class Regime:
    key: str        # "positive" | "negative"
    title: str
    sentence: str


def classify_regime(total_net_gex: float, spot: float, flip: float | None,
                    near_pct: float = 0.005) -> Regime:
    """
    Regime = sign of total net GEX at the actual spot (which is exactly where the
    gamma profile is evaluated at spot). The flip adds context: how far spot is
    from the level where that sign would change.
    """
    positive = total_net_gex > 0
    if flip is None:
        where = "No gamma flip was found within ±20% of spot, so the sign is unlikely to change on a normal move."
    else:
        dist = spot - flip
        where = (f"Spot is {abs(dist):,.0f} points ({abs(dist) / spot:.2%}) "
                 f"{'above' if dist > 0 else 'below'} the gamma flip at {flip:,.2f}.")
    near = flip is not None and abs(spot - flip) / spot < near_pct
    if positive:
        return Regime(
            "positive",
            "Positive gamma zone (mean-reverting)" + (" - close to the flip" if near else ""),
            "Under the assumed dealer positioning, market makers hedge by selling into rallies "
            "and buying dips, a pattern that has historically tended to dampen short-term SPX "
            "swings. " + where,
        )
    return Regime(
        "negative",
        "Negative gamma zone (trending)" + (" - close to the flip" if near else ""),
        "Under the assumed dealer positioning, market makers hedge by selling into declines "
        "and buying into rallies, a pattern that has historically tended to amplify short-term "
        "SPX moves. " + where,
    )


@dataclass
class GexResult:
    spot: float
    contracts: pd.DataFrame
    by_strike: pd.DataFrame
    by_expiry: pd.DataFrame
    total_net_gex: float
    call_wall: float | None
    put_wall: float | None
    gamma_flip: float | None
    all_flips: list[float]
    profile_levels: np.ndarray
    profile_values: np.ndarray
    aggregate_at_strikes: pd.Series     # profile evaluated at each strike (for the chart line)
    regime: Regime


def run_analysis(
    prepared: pd.DataFrame,
    spot: float,
    convention: str = config.DEFAULT_SIGN_CONVENTION,
    r: float = config.DEFAULT_RISK_FREE_RATE,
    q: float = config.DEFAULT_DIVIDEND_YIELD,
    expiry: date | None = None,
    display_range_pct: float = config.DEFAULT_STRIKE_RANGE_PCT,
) -> GexResult:
    """Full pipeline for either all expiries combined (expiry=None) or one expiry."""
    df = prepared if expiry is None else prepared[prepared["expiry"] == expiry]
    if df.empty:
        raise ValueError("No contracts with open interest and IV for this selection.")

    df = add_gex_columns(df, spot, convention, r, q)
    by_strike = aggregate_by_strike(df)
    by_expiry = aggregate_by_expiry(df)
    total = float(df["gex"].sum())

    # Walls: largest RAW (unsigned) dollar gamma, calls and puts separately.
    call_wall = float(by_strike.loc[by_strike["call_raw"].idxmax(), "strike"]) if by_strike["call_raw"].max() > 0 else None
    put_wall = float(by_strike.loc[by_strike["put_raw"].idxmax(), "strike"]) if by_strike["put_raw"].max() > 0 else None

    # Gamma flip: fine grid of hypothetical spots, then the zero crossing nearest spot.
    levels = np.linspace(spot * (1 - config.FLIP_SEARCH_RANGE_PCT),
                         spot * (1 + config.FLIP_SEARCH_RANGE_PCT), config.FLIP_GRID_POINTS)
    profile = gamma_profile(df, levels, convention, r, q)
    flips = find_zero_crossings(levels, profile)
    flip = min(flips, key=lambda x: abs(x - spot)) if flips else None

    # "Aggregate GEX" line: total GEX recomputed as if each displayed strike were spot.
    lo, hi = spot * (1 - display_range_pct), spot * (1 + display_range_pct)
    shown = by_strike.loc[(by_strike["strike"] >= lo) & (by_strike["strike"] <= hi), "strike"].to_numpy()
    agg = pd.Series(gamma_profile(df, shown, convention, r, q) if len(shown) else [], index=shown, dtype=float)

    return GexResult(
        spot=spot, contracts=df, by_strike=by_strike, by_expiry=by_expiry,
        total_net_gex=total, call_wall=call_wall, put_wall=put_wall,
        gamma_flip=flip, all_flips=flips, profile_levels=levels, profile_values=profile,
        aggregate_at_strikes=agg, regime=classify_regime(total, spot, flip),
    )
