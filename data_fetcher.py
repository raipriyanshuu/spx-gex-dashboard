"""
data_fetcher.py - pulls the full SPX option chain from CBOE's free delayed feed.

WHAT THE SOURCE IS
------------------
    GET https://cdn.cboe.com/api/global/delayed_quotes/options/_SPX.json

This is the JSON file that cboe.com's own delayed-quote pages (including the
"quote table download" page) load in the browser. A single unauthenticated GET
returns every listed SPX and SPXW contract: all strikes, all expirations,
calls and puts, with bid/ask, implied volatility, open interest, volume and
CBOE's own greeks. No session cookie, form post or HTML scraping is involved.

Caveats you should know:
  * It is an undocumented endpoint that powers CBOE's website, not an official,
    versioned developer API. It can change or disappear without notice.
  * Quotes are delayed by at least 15 minutes (CBOE's delayed-data policy).
  * Open interest is published once per day (start of day), so intraday OI
    changes are not visible anywhere in this feed.
  * CBOE's website terms of use govern this data. Personal, on-demand,
    low-frequency use (what this app does: one request per click) is the
    lowest-impact pattern; do not redistribute the data or poll it in a loop.

Nothing is cached: every call to fetch_spx_chain() makes a fresh HTTP request.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd
import requests

import config

# Option symbol format used by CBOE:  ROOT + YYMMDD + C/P + strike*1000 (8 digits)
#   e.g. SPXW261016P05800000  ->  SPXW, 2026-10-16, Put, 5800.0
SYMBOL_PATTERN = r"^(?P<root>[A-Z]+)(?P<yymmdd>\d{6})(?P<cp>[CP])(?P<strike>\d{8})$"


class DataFetchError(RuntimeError):
    """Raised when the chain cannot be downloaded or understood."""


@dataclass
class ChainSnapshot:
    """One fresh download of the SPX chain."""

    spot: float                       # SPX index level reported by the feed
    chain: pd.DataFrame               # one row per contract (see parse_payload)
    cboe_timestamp: str | None        # timestamp string CBOE put in the file
    fetched_at_utc: datetime          # when *this app* downloaded it
    source_url: str
    notes: list[str] = field(default_factory=list)


def parse_option_symbols(symbols: pd.Series) -> pd.DataFrame:
    """Vectorised parse of CBOE option symbols into root / expiry / type / strike."""
    parts = symbols.str.extract(SYMBOL_PATTERN)
    out = pd.DataFrame(index=symbols.index)
    out["root"] = parts["root"]
    out["expiry"] = pd.to_datetime(parts["yymmdd"], format="%y%m%d", errors="coerce").dt.date
    out["type"] = parts["cp"].map({"C": "call", "P": "put"})
    out["strike"] = pd.to_numeric(parts["strike"], errors="coerce") / 1000.0
    return out


def _num(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df.columns:
        return pd.Series(np.nan, index=df.index, dtype=float)
    return pd.to_numeric(df[col], errors="coerce")


def parse_payload(payload: dict[str, Any]) -> tuple[float, pd.DataFrame, str | None, list[str]]:
    """Turn CBOE's JSON into (spot, chain DataFrame, cboe_timestamp, notes)."""
    if not isinstance(payload, dict) or "data" not in payload:
        raise DataFetchError("Unexpected response: no 'data' key in CBOE JSON.")
    data = payload["data"] or {}
    notes: list[str] = []

    spot = None
    for key in ("current_price", "close", "prev_day_close"):
        val = data.get(key)
        try:
            val = float(val)
        except (TypeError, ValueError):
            continue
        if np.isfinite(val) and val > 0:
            spot = val
            if key != "current_price":
                notes.append(f"'current_price' missing; spot taken from '{key}'.")
            break
    if spot is None:
        raise DataFetchError("CBOE JSON has no usable SPX price (current_price/close).")

    options = data.get("options") or []
    if not options:
        raise DataFetchError("CBOE JSON contains no option contracts.")

    raw = pd.DataFrame(options)
    if "option" not in raw.columns:
        raise DataFetchError("CBOE JSON options are missing the 'option' symbol field.")

    parsed = parse_option_symbols(raw["option"].astype(str))
    chain = pd.DataFrame(
        {
            "symbol": raw["option"].astype(str),
            "root": parsed["root"],
            "expiry": parsed["expiry"],
            "type": parsed["type"],
            "strike": parsed["strike"],
            "bid": _num(raw, "bid"),
            "ask": _num(raw, "ask"),
            "last": _num(raw, "last_trade_price"),
            "iv_feed": _num(raw, "iv"),                 # decimal, e.g. 0.18 = 18%
            "open_interest": _num(raw, "open_interest").fillna(0.0),
            "volume": _num(raw, "volume").fillna(0.0),
            "gamma_cboe": _num(raw, "gamma"),           # CBOE's own gamma, kept for cross-checking only
        }
    )

    bad = chain["root"].isna() | chain["expiry"].isna() | chain["strike"].isna()
    if bad.any():
        notes.append(f"{int(bad.sum())} contract symbols could not be parsed and were dropped.")
    chain = chain[~bad]

    other_roots = ~chain["root"].isin(config.ALLOWED_ROOTS)
    if other_roots.any():
        notes.append(
            f"{int(other_roots.sum())} contracts with roots other than "
            f"{'/'.join(config.ALLOWED_ROOTS)} were dropped."
        )
    chain = chain[~other_roots].reset_index(drop=True)

    return spot, chain, payload.get("timestamp"), notes


def fetch_spx_chain() -> ChainSnapshot:
    """Download a fresh SPX chain from CBOE. Always hits the network."""
    headers = {
        # A normal browser-style request; the CDN rejects some library defaults.
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
        ),
        "Accept": "application/json",
        # Ask any intermediate cache for a fresh copy.
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    errors: list[str] = []
    for url in config.CBOE_CHAIN_URLS:
        try:
            resp = requests.get(url, headers=headers, timeout=config.HTTP_TIMEOUT_SECONDS)
            resp.raise_for_status()
            payload = resp.json()
        except (requests.RequestException, ValueError) as exc:
            errors.append(f"{url}: {exc}")
            continue

        spot, chain, cboe_ts, notes = parse_payload(payload)
        return ChainSnapshot(
            spot=spot,
            chain=chain,
            cboe_timestamp=cboe_ts,
            fetched_at_utc=datetime.now(timezone.utc),
            source_url=url,
            notes=notes,
        )

    raise DataFetchError("Could not download the SPX chain from CBOE.\n" + "\n".join(errors))


if __name__ == "__main__":
    # Quick manual check:  python data_fetcher.py
    snap = fetch_spx_chain()
    print(f"Source      : {snap.source_url}")
    print(f"CBOE time   : {snap.cboe_timestamp}")
    print(f"Spot        : {snap.spot:,.2f}")
    print(f"Contracts   : {len(snap.chain):,}")
    print(f"Expirations : {snap.chain['expiry'].nunique()}")
    print(snap.chain.head())
