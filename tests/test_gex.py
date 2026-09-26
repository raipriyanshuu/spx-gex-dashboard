"""
Offline checks for the maths and parsing. No network needed.

Run from the project folder:   python -m unittest discover tests -v
"""

import os
import sys
import unittest
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import data_fetcher as dfh  # noqa: E402
import gex_calculator as gc  # noqa: E402


def synthetic_payload(spot=6600.0, now=None):
    """A chain in CBOE's exact JSON shape (fields as served by _SPX.json)."""
    now = now or datetime(2026, 9, 21, 12, 0, tzinfo=gc.ET)   # a Monday
    rng = np.random.default_rng(7)
    options = []
    for days in (1, 4, 11, 25, 53, 88):
        exp = (now + timedelta(days=days)).date()
        for k in np.arange(5600, 7600, 25):
            m = np.log(k / spot)
            iv = 0.14 - 0.25 * m + 0.9 * m * m          # simple skew
            round_bump = 3.0 if k % 100 == 0 else (1.6 if k % 50 == 0 else 1.0)
            for cp in "CP":
                # puts concentrated below spot, calls above, bigger at round strikes
                side = (k < spot) if cp == "P" else (k > spot)
                base = 3000 if side else 700
                oi = base * round_bump * np.exp(-abs(m) * 10) * rng.uniform(0.8, 1.2) * (1 + days / 30)
                sym = f"SPXW{exp:%y%m%d}{cp}{int(k * 1000):08d}"
                options.append({"option": sym, "bid": 1.0, "ask": 1.2, "iv": round(iv, 4),
                                "open_interest": round(oi), "volume": 0.0, "gamma": 0.0,
                                "last_trade_price": 1.1})
    # one contract with missing IV but a quote -> should be back-solved
    options.append({"option": f"SPXW{(now + timedelta(days=25)).date():%y%m%d}P06000000",
                    "bid": 9.8, "ask": 10.2, "iv": 0.0, "open_interest": 500.0})
    # one with missing IV and no bid -> should be excluded
    options.append({"option": f"SPXW{(now + timedelta(days=25)).date():%y%m%d}P05000000",
                    "bid": 0.0, "ask": 0.05, "iv": 0.0, "open_interest": 300.0})
    return {"timestamp": "2026-09-21 12:00:00", "data": {"current_price": spot, "options": options}}, now


class TestParsing(unittest.TestCase):
    def test_symbol(self):
        p = dfh.parse_option_symbols(pd.Series(["SPXW261016P05800000", "SPX261218C07125000"]))
        self.assertEqual(list(p["root"]), ["SPXW", "SPX"])
        self.assertEqual(p["expiry"].iloc[0], date(2026, 10, 16))
        self.assertEqual(list(p["type"]), ["put", "call"])
        self.assertEqual(list(p["strike"]), [5800.0, 7125.0])

    def test_payload(self):
        payload, _ = synthetic_payload()
        spot, chain, ts, notes = dfh.parse_payload(payload)
        self.assertEqual(spot, 6600.0)
        self.assertEqual(len(chain), len(payload["data"]["options"]))


class TestMath(unittest.TestCase):
    def test_gamma_matches_finite_difference(self):
        S, K, T, s = 6600.0, 6650.0, 20 / 365, 0.16
        h = 0.5
        fd = (gc.bs_price(S + h, K, T, s) - 2 * gc.bs_price(S, K, T, s) + gc.bs_price(S - h, K, T, s)) / h**2
        self.assertAlmostEqual(float(gc.bs_gamma(S, K, T, s)), float(fd), places=6)

    def test_implied_vol_roundtrip(self):
        for is_call in (True, False):
            p = gc.bs_price(6600, 6400, 0.1, 0.22, is_call=is_call)
            self.assertAlmostEqual(gc.implied_vol(p, 6600, 6400, 0.1, is_call=is_call), 0.22, places=5)

    def test_dollar_gamma_formula(self):
        self.assertAlmostEqual(gc.dollar_gamma(0.001, 6000.0, 10), 0.001 * 6000**2 * 0.01 * 100 * 10)

    def test_sign_convention(self):
        t = pd.Series(["call", "put"])
        self.assertEqual(list(gc.dealer_signs(t, "standard")), [1.0, -1.0])
        self.assertEqual(list(gc.dealer_signs(t, "inverted")), [-1.0, 1.0])

    def test_zero_crossing(self):
        lv = np.array([1.0, 2.0, 3.0, 4.0])
        self.assertEqual(gc.find_zero_crossings(lv, np.array([-2.0, -1.0, 1.0, 2.0])), [2.5])


class TestPipeline(unittest.TestCase):
    def test_end_to_end(self):
        payload, now = synthetic_payload()
        spot, chain, _, _ = dfh.parse_payload(payload)
        prepared, rep = gc.prepare_chain(chain, spot, now=now)
        self.assertEqual(rep.iv_backsolved, 1)
        self.assertEqual(rep.excluded_no_iv, 1)
        self.assertEqual(rep.expired, 0)
        res = gc.run_analysis(prepared, spot)
        # cumulative line ends at the total
        self.assertAlmostEqual(res.by_strike["net_gex_cumulative"].iloc[-1], res.total_net_gex, delta=1e-3)
        # aggregate profile at the actual spot equals the total at spot
        at_spot = gc.gamma_profile(res.contracts, [spot], "standard", 0.0, 0.0)[0]
        self.assertAlmostEqual(at_spot / res.total_net_gex, 1.0, places=9)
        self.assertIsNotNone(res.call_wall)
        self.assertIsNotNone(res.put_wall)
        # per-expiry totals add up to the combined total
        self.assertAlmostEqual(res.by_expiry["net_gex"].sum() / res.total_net_gex, 1.0, places=9)
        for e in prepared["expiry"].unique():
            gc.run_analysis(prepared, spot, expiry=e)


if __name__ == "__main__":
    unittest.main()
