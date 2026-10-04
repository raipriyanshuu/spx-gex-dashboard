"""
exposures.py - charm and vanna exposure (CEX / VEX) by strike and expiry.

WHAT THESE ARE (AND AREN'T)
---------------------------
Both describe how the delta of the ASSUMED dealer position (dealer_signs())
would drift, and so how much hedging would be needed to stay delta-neutral,
when something other than price changes:

  * Charm  = how delta drifts as TIME passes, price and IV unchanged. Grows
    sharply as expiry approaches: matters most into the close, on 0DTE and
    around monthly OPEX.
  * Vanna  = how delta changes when IMPLIED VOLATILITY moves, price unchanged
    (e.g. IV falling after a scheduled event).

They describe current positioning under an assumption, not a price forecast.

Black-Scholes with continuous dividend yield q and rate r (matching bs_gamma):
    d1 = (ln(S/K) + (r - q + sigma^2/2) T) / (sigma sqrt(T)),   d2 = d1 - sigma sqrt(T)
    vanna      = dDelta/dsigma = -e^(-qT) phi(d1) d2 / sigma            (calls = puts)
    charm_call = -dDelta/dT    =  q e^(-qT) N(d1)  - e^(-qT) phi(d1) [2(r-q)T - d2 sigma sqrt(T)] / (2T sigma sqrt(T))
    charm_put  = -dDelta/dT    = -q e^(-qT) N(-d1) - e^(-qT) phi(d1) [2(r-q)T - d2 sigma sqrt(T)] / (2T sigma sqrt(T))
(charm is per year of time passing).

Dollar exposures per contract, with the dealer sign ASSUMPTION applied:
    VEX = sign * vanna * weight * 100 * S * 0.01     $ of delta change per 1 vol point
    CEX = sign * (charm / 365) * weight * 100 * S    $ of delta change per calendar day
weight = open interest or today's volume (same choice as GEX). A positive
total means the assumed dealer delta rises, so staying hedged would mean
selling that much SPX exposure; negative means buying.

T is floored at config.MIN_T_YEARS exactly as for gamma, so 0DTE charm stays
finite (but large) into the close.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.stats import norm

import config
from gex_calculator import bs_d1, dealer_signs

_SQRT_2PI = np.sqrt(2.0 * np.pi)


# ---------------------------------------------------------------------------
# Black-Scholes second-order greeks (vectorised with numpy broadcasting)
# ---------------------------------------------------------------------------
def bs_vanna(S, K, T, sigma, r=0.0, q=0.0):
    """dDelta/dsigma (per 1.00 of vol, i.e. per 100 vol points); identical for calls and puts."""
    S, K, T, sigma = (np.asarray(x, dtype=float) for x in (S, K, T, sigma))
    d1 = bs_d1(S, K, T, sigma, r, q)
    d2 = d1 - sigma * np.sqrt(T)
    pdf = np.exp(-0.5 * d1**2) / _SQRT_2PI
    return -np.exp(-q * T) * pdf * d2 / sigma


def bs_charm(S, K, T, sigma, r=0.0, q=0.0, is_call=True):
    """-dDelta/dT: delta drift per YEAR of time passing. `is_call` may be an array."""
    S, K, T, sigma = (np.asarray(x, dtype=float) for x in (S, K, T, sigma))
    sqrt_t = np.sqrt(T)
    d1 = bs_d1(S, K, T, sigma, r, q)
    d2 = d1 - sigma * sqrt_t
    disc = np.exp(-q * T)
    pdf = np.exp(-0.5 * d1**2) / _SQRT_2PI
    common = disc * pdf * (2 * (r - q) * T - d2 * sigma * sqrt_t) / (2 * T * sigma * sqrt_t)
    return np.where(is_call, q * disc * norm.cdf(d1) - common, -q * disc * norm.cdf(-d1) - common)


# ---------------------------------------------------------------------------
# Dollar exposures and aggregation
# ---------------------------------------------------------------------------
def add_exposure_columns(df: pd.DataFrame, spot: float, convention: str, r: float, q: float,
                         weight: str = "open_interest") -> pd.DataFrame:
    out = df.copy()
    w = out[weight].to_numpy(float) * config.CONTRACT_MULTIPLIER * spot
    out["vanna"] = bs_vanna(spot, out["strike"], out["T"], out["iv"], r, q)
    out["charm"] = bs_charm(spot, out["strike"], out["T"], out["iv"], r, q, out["type"].to_numpy() == "call")
    # Sign applied here is an ASSUMED dealer position - see dealer_signs().
    sign = dealer_signs(out["type"], convention)
    out["vex"] = sign * out["vanna"] * w * 0.01        # $ delta change per 1 vol point
    out["cex"] = sign * out["charm"] / 365.0 * w       # $ delta change per calendar day
    return out


@dataclass
class ExposureResult:
    contracts: pd.DataFrame
    by_strike: pd.DataFrame     # strike, call/put/net VEX and CEX
    by_expiry: pd.DataFrame     # expiry, dte, net VEX and CEX
    net_vanna: float            # $ of delta change per 1 vol point
    net_charm: float            # $ of delta change per calendar day
    weight: str


def compute_exposures(contracts: pd.DataFrame, spot: float, convention: str = config.DEFAULT_SIGN_CONVENTION,
                      r: float = config.DEFAULT_RISK_FREE_RATE, q: float = config.DEFAULT_DIVIDEND_YIELD,
                      weight: str = "open_interest") -> ExposureResult:
    """
    VEX / CEX for the contracts of a GexResult (already filtered to the selected
    expiry and to weight > 0 by run_analysis()), so the selection matches the GEX tab.
    """
    df = add_exposure_columns(contracts, spot, convention, r, q, weight)
    is_call = df["type"] == "call"
    g = pd.DataFrame({
        "strike": df["strike"],
        "call_vex": np.where(is_call, df["vex"], 0.0), "put_vex": np.where(~is_call, df["vex"], 0.0),
        "call_cex": np.where(is_call, df["cex"], 0.0), "put_cex": np.where(~is_call, df["cex"], 0.0),
    })
    by_strike = g.groupby("strike", sort=True).sum()
    by_strike["net_vex"] = by_strike["call_vex"] + by_strike["put_vex"]
    by_strike["net_cex"] = by_strike["call_cex"] + by_strike["put_cex"]
    by_expiry = df.groupby("expiry", sort=True).agg(dte=("dte", "min"), net_vex=("vex", "sum"),
                                                    net_cex=("cex", "sum"))
    return ExposureResult(
        contracts=df, by_strike=by_strike.reset_index(), by_expiry=by_expiry.reset_index(),
        net_vanna=float(df["vex"].sum()), net_charm=float(df["cex"].sum()), weight=weight,
    )
