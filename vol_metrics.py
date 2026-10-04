"""
vol_metrics.py - expected-move bands from the at-the-money straddle and ATM IV.

WHAT THIS IS (AND ISN'T)
------------------------
The expected move describes what the options market is currently PRICING for
the size of an SPX move by a given expiry. It is not a forecast of direction
or of the realised move: historically SPX has finished inside its 1-sigma
implied range more often than not, but by no means always.

Two related numbers are reported per expiry, in SPX index points:

  * 1-sigma move (IV-based) = S * ATM_IV * sqrt(T)
        One standard deviation of the lognormal-ish distribution implied by
        the ATM volatility. Under a normal approximation, about 68% of the
        implied probability lies inside spot +/- this amount.
  * Straddle-based move = ATM call mid + ATM put mid
        The market price of the ATM straddle. For a normal distribution the
        expected ABSOLUTE move is sqrt(2/pi) * sigma ~= 0.8 * sigma, so the
        straddle is roughly 0.8x the 1-sigma move. They are NOT the same thing.

Inputs come only from the chain already downloaded (no extra requests). T and
IV are computed with the same helpers prepare_chain() uses, so the IV here is
CBOE's IV when it is positive, otherwise back-solved from the bid/ask mid.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime

import numpy as np
import pandas as pd

import config
import gex_calculator as gc

# When an expiry date has both roots (third-Friday monthlies), prefer the
# PM-settled SPXW contract: its settlement matches the expiry date's close,
# and it is usually the more liquid of the two.
ROOT_PREFERENCE = ("SPXW", "SPX")


@dataclass
class ExpectedMove:
    expiry: date
    root: str
    dte: float              # calendar days to settlement
    T: float                # years, exactly as prepare_chain() computes it
    atm_strike: float
    call_mid: float         # index points
    put_mid: float          # index points
    straddle: float         # index points (= straddle-based move)
    atm_iv: float           # decimal, mean of the ATM call and put IV
    move_pts: float         # 1-sigma move, index points
    move_pct: float         # 1-sigma move as a fraction of spot
    low: float              # spot - move_pts
    high: float             # spot + move_pts


def _atm_pair(group: pd.DataFrame, spot: float, T: float, r: float, q: float):
    """
    Nearest-to-spot strike that has BOTH a call and a put with bid > 0 and
    ask > 0, and for which both IVs are available. Returns
    (strike, call_row, put_row, call_iv, put_iv) or None.
    """
    quoted = group[(group["bid"] > 0) & (group["ask"] > 0)]
    calls = quoted[quoted["type"] == "call"].drop_duplicates("strike").set_index("strike")
    puts = quoted[quoted["type"] == "put"].drop_duplicates("strike").set_index("strike")
    for k in sorted(calls.index.intersection(puts.index), key=lambda k: (abs(k - spot), k)):
        c, p = calls.loc[k], puts.loc[k]
        c_iv, _ = gc.contract_iv(c["iv_feed"], c["bid"], c["ask"], spot, k, T, r, q, True)
        p_iv, _ = gc.contract_iv(p["iv_feed"], p["bid"], p["ask"], spot, k, T, r, q, False)
        if np.isfinite(c_iv) and np.isfinite(p_iv):
            return k, c, p, c_iv, p_iv
    return None


def expected_moves(
    chain: pd.DataFrame,
    spot: float,
    now: datetime | None = None,
    r: float = config.DEFAULT_RISK_FREE_RATE,
    q: float = config.DEFAULT_DIVIDEND_YIELD,
) -> list[ExpectedMove]:
    """
    One ExpectedMove per non-expired expiry, sorted by expiry. Takes the RAW
    chain (as parsed by data_fetcher) because the ATM pair only needs a
    two-sided quote; zero-open-interest contracts are not excluded here.
    Expiries without a usable ATM call/put pair are skipped.
    """
    now = (now or datetime.now(gc.ET)).astimezone(gc.ET)
    if chain.empty:
        return []
    secs = gc.seconds_to_expiry(chain["root"], chain["expiry"], now)
    live = chain[secs > 0].assign(_secs=secs[secs > 0])

    out: list[ExpectedMove] = []
    for expiry, by_exp in live.groupby("expiry", sort=True):
        for root in ROOT_PREFERENCE:
            g = by_exp[by_exp["root"] == root]
            if g.empty:
                continue
            secs_root = float(g["_secs"].iloc[0])
            T = float(gc.years_to_expiry(secs_root))
            pair = _atm_pair(g, spot, T, r, q)
            if pair is None:
                continue
            k, c, p, c_iv, p_iv = pair
            call_mid, put_mid = gc.mid_price(c["bid"], c["ask"]), gc.mid_price(p["bid"], p["ask"])
            atm_iv = (c_iv + p_iv) / 2
            move = spot * atm_iv * np.sqrt(T)
            out.append(ExpectedMove(
                expiry=expiry, root=root, dte=secs_root / 86400.0, T=T, atm_strike=float(k),
                call_mid=float(call_mid), put_mid=float(put_mid), straddle=float(call_mid + put_mid),
                atm_iv=float(atm_iv), move_pts=float(move), move_pct=float(move / spot),
                low=float(spot - move), high=float(spot + move),
            ))
            break
    return out


def to_frame(moves: list[ExpectedMove]) -> pd.DataFrame:
    return pd.DataFrame([asdict(m) for m in moves])


def pick(moves: list[ExpectedMove], expiry: date | None) -> ExpectedMove | None:
    """The selected expiry's move, or for expiry=None the nearest non-expired one."""
    if not moves:
        return None
    if expiry is None:
        return moves[0]
    return next((m for m in moves if m.expiry == expiry), None)


def walls_vs_range(em: ExpectedMove, call_wall: float | None, put_wall: float | None) -> str:
    """One line: is each wall inside or outside the 1-sigma range?"""
    parts = []
    for name, level in (("Call Wall", call_wall), ("Put Wall", put_wall)):
        if level is None:
            continue
        where = "inside" if em.low <= level <= em.high else "outside"
        parts.append(f"{name} {level:,.0f} is {where}")
    if not parts:
        return ""
    return " · ".join(parts) + f" the 1σ range {em.low:,.0f} – {em.high:,.0f}."
