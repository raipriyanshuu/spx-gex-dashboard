"""
Offline checks for the maths and parsing. No network needed.

Run from the project folder:   python -m unittest discover tests -v
"""

import os
import sys
import unittest
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import data_fetcher as dfh  # noqa: E402
import exposures as ex  # noqa: E402
import gex_calculator as gc  # noqa: E402
import vol_metrics as vm  # noqa: E402


def synthetic_payload(spot=6600.0, now=None, with_volume=False):
    """
    A chain in CBOE's exact JSON shape (fields as served by _SPX.json).
    with_volume=True adds today's volume (from a separate RNG, so OI is unchanged)
    plus one zero-OI contract that traded today.
    """
    now = now or datetime(2026, 9, 21, 12, 0, tzinfo=gc.ET)   # a Monday
    rng = np.random.default_rng(7)
    vrng = np.random.default_rng(99)
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
                vol = round(oi * vrng.uniform(0.0, 3.0) * np.exp(-abs(m) * 20)) if with_volume else 0.0
                options.append({"option": sym, "bid": 1.0, "ask": 1.2, "iv": round(iv, 4),
                                "open_interest": round(oi), "volume": vol, "gamma": 0.0,
                                "last_trade_price": 1.1})
    if with_volume:
        # a strike opened today: volume but no open interest yet
        options.append({"option": f"SPXW{(now + timedelta(days=1)).date():%y%m%d}C06612500",
                        "bid": 5.0, "ask": 5.4, "iv": 0.13, "open_interest": 0.0, "volume": 4000.0})
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


class TestCharmVanna(unittest.TestCase):
    # (S, K, T, sigma, r, q): ATM, OTM call side, ITM call side, short-dated, longer-dated
    CASES = [(6600.0, 6600.0, 20 / 365, 0.16, 0.04, 0.013),
             (6600.0, 6800.0, 45 / 365, 0.14, 0.05, 0.02),
             (6600.0, 6300.0, 10 / 365, 0.22, 0.03, 0.015),
             (6600.0, 6625.0, 2 / 365, 0.12, 0.04, 0.013),
             (6600.0, 6000.0, 1.0, 0.25, 0.045, 0.012)]

    def test_vanna_matches_finite_difference_of_delta(self):
        h = 1e-4
        for S, K, T, s, r, q in self.CASES:
            for is_call in (True, False):
                fd = (gc.bs_delta(S, K, T, s + h, r, q, is_call) - gc.bs_delta(S, K, T, s - h, r, q, is_call)) / (2 * h)
                self.assertAlmostEqual(float(ex.bs_vanna(S, K, T, s, r, q)), float(fd), delta=1e-5)

    def test_charm_matches_finite_difference_of_delta(self):
        h = 1e-6
        for S, K, T, s, r, q in self.CASES:
            for is_call in (True, False):
                fd = -(gc.bs_delta(S, K, T + h, s, r, q, is_call) - gc.bs_delta(S, K, T - h, s, r, q, is_call)) / (2 * h)
                self.assertAlmostEqual(float(ex.bs_charm(S, K, T, s, r, q, is_call)), float(fd), delta=1e-5)

    def test_delta_put_call_parity(self):
        S, K, T, s, r, q = self.CASES[1]
        diff = gc.bs_delta(S, K, T, s, r, q, True) - gc.bs_delta(S, K, T, s, r, q, False)
        self.assertAlmostEqual(float(diff), np.exp(-q * T), places=12)

    def test_dollar_exposure_formulas(self):
        df = pd.DataFrame({"strike": [6650.0, 6550.0], "T": [0.05, 0.05], "iv": [0.15, 0.18],
                           "type": ["call", "put"], "open_interest": [1000.0, 2000.0], "volume": [10.0, 30.0]})
        S, r, q = 6600.0, 0.04, 0.013
        for weight in ("open_interest", "volume"):
            out = ex.add_exposure_columns(df, S, "standard", r, q, weight)
            for i, sign in enumerate((1.0, -1.0)):           # standard: calls +, puts -
                row = df.iloc[i]
                vanna = ex.bs_vanna(S, row["strike"], row["T"], row["iv"], r, q)
                charm = ex.bs_charm(S, row["strike"], row["T"], row["iv"], r, q, row["type"] == "call")
                self.assertAlmostEqual(out["vex"].iloc[i], sign * vanna * row[weight] * 100 * S * 0.01, places=6)
                self.assertAlmostEqual(out["cex"].iloc[i], sign * charm / 365 * row[weight] * 100 * S, places=6)

    def test_per_expiry_totals_sum_to_combined(self):
        payload, now = synthetic_payload(with_volume=True)
        spot, chain, _, _ = dfh.parse_payload(payload)
        prepared, _ = gc.prepare_chain(chain, spot, now=now, r=0.04, q=0.013, keep_traded=True)
        for weight in ("open_interest", "volume"):
            for conv in ("standard", "inverted"):
                res = gc.run_analysis(prepared, spot, conv, 0.04, 0.013, weight=weight)
                comb = ex.compute_exposures(res.contracts, spot, conv, 0.04, 0.013, weight)
                self.assertAlmostEqual(comb.by_expiry["net_vex"].sum() / comb.net_vanna, 1.0, places=9)
                self.assertAlmostEqual(comb.by_expiry["net_cex"].sum() / comb.net_charm, 1.0, places=9)
                self.assertAlmostEqual(comb.by_strike["net_vex"].sum() / comb.net_vanna, 1.0, places=9)
                vex = cex = 0.0
                for e in prepared["expiry"].unique():
                    r_e = gc.run_analysis(prepared, spot, conv, 0.04, 0.013, expiry=e, weight=weight)
                    one = ex.compute_exposures(r_e.contracts, spot, conv, 0.04, 0.013, weight)
                    vex, cex = vex + one.net_vanna, cex + one.net_charm
                self.assertAlmostEqual(vex / comb.net_vanna, 1.0, places=9)
                self.assertAlmostEqual(cex / comb.net_charm, 1.0, places=9)


class TestVolumeWeighting(unittest.TestCase):
    def setUp(self):
        payload, now = synthetic_payload(with_volume=True)
        self.spot, chain, _, _ = dfh.parse_payload(payload)
        self.prep_oi, _ = gc.prepare_chain(chain, self.spot, now=now)
        self.prep_all, self.rep_all = gc.prepare_chain(chain, self.spot, now=now, keep_traded=True)

    def test_keep_traded_keeps_zero_oi_with_volume(self):
        self.assertEqual(len(self.prep_all), len(self.prep_oi) + 1)
        self.assertTrue(((self.prep_all["open_interest"] == 0) & (self.prep_all["volume"] > 0)).any())

    def test_oi_weight_equals_existing_results_exactly(self):
        base = gc.run_analysis(self.prep_oi, self.spot)                       # pre-feature call
        for prep in (self.prep_oi, self.prep_all):
            res = gc.run_analysis(prep, self.spot, weight="open_interest")
            self.assertEqual(res.total_net_gex, base.total_net_gex)
            self.assertEqual(res.call_wall, base.call_wall)
            self.assertEqual(res.put_wall, base.put_wall)
            self.assertEqual(res.gamma_flip, base.gamma_flip)
            self.assertEqual(res.all_flips, base.all_flips)
            np.testing.assert_array_equal(res.profile_values, base.profile_values)
            np.testing.assert_array_equal(res.aggregate_at_strikes.to_numpy(), base.aggregate_at_strikes.to_numpy())
            pd.testing.assert_frame_equal(res.by_strike, base.by_strike)
            self.assertEqual(res.flip_label, "Gamma Flip")

    def test_volume_weighted_pipeline(self):
        res = gc.run_analysis(self.prep_all, self.spot, weight="volume")
        self.assertIsNotNone(res.call_wall)
        self.assertIsNotNone(res.put_wall)
        self.assertIsNotNone(res.gamma_flip)
        self.assertEqual(res.flip_label, "Zero gamma (by volume)")
        self.assertIn("zero-gamma level (by volume)", res.regime.sentence)
        # Same dollar-gamma formula with volume in place of OI, same dealer sign.
        c = res.contracts
        expect = (gc.dealer_signs(c["type"], "standard") * gc.bs_gamma(self.spot, c["strike"], c["T"], c["iv"])
                  * self.spot**2 * 0.01 * 100 * c["volume"]).sum()
        self.assertAlmostEqual(res.total_net_gex / expect, 1.0, places=12)
        self.assertAlmostEqual(res.by_strike["net_gex_cumulative"].iloc[-1], res.total_net_gex, delta=1e-3)
        at_spot = gc.gamma_profile(res.contracts, [self.spot], "standard", 0.0, 0.0, weight="volume")[0]
        self.assertAlmostEqual(at_spot / res.total_net_gex, 1.0, places=9)
        # the zero-OI contract traded today contributes to volume GEX
        self.assertIn(6612.5, set(res.contracts["strike"]))

    def test_zero_volume_raises(self):
        payload, now = synthetic_payload()                                    # volume 0 everywhere
        spot, chain, _, _ = dfh.parse_payload(payload)
        prep, _ = gc.prepare_chain(chain, spot, now=now, keep_traded=True)
        with self.assertRaises(ValueError):
            gc.run_analysis(prep, spot, weight="volume")

    def test_volume_oi_ratios(self):
        vo = gc.volume_oi_ratios(self.prep_all, 15)
        self.assertEqual(len(vo), 15)
        self.assertTrue((vo["open_interest"] > 0).all())
        self.assertTrue(vo["ratio"].is_monotonic_decreasing)
        np.testing.assert_allclose(vo["ratio"], vo["volume"] / vo["open_interest"])


class TestExpectedMove(unittest.TestCase):
    def test_one_sigma_equals_s_sigma_sqrt_t(self):
        payload, now = synthetic_payload()
        spot, chain, _, _ = dfh.parse_payload(payload)
        moves = vm.expected_moves(chain, spot, now=now)
        self.assertEqual(len(moves), 6)                     # one per expiry
        for m in moves:
            # Synthetic IV at the money (k = spot) is exactly 0.14 for both legs.
            self.assertEqual(m.atm_strike, 6600.0)
            self.assertAlmostEqual(m.atm_iv, 0.14, places=12)
            # absolute elapsed time (UTC), so expiries after the 1 Nov DST change are exact too
            secs = (gc.expiry_datetime_et("SPXW", m.expiry).astimezone(timezone.utc)
                    - now.astimezone(timezone.utc)).total_seconds()
            T = secs / config.SECONDS_PER_YEAR
            self.assertAlmostEqual(m.T, T, places=12)
            self.assertAlmostEqual(m.move_pts, spot * 0.14 * np.sqrt(T), places=9)
            self.assertAlmostEqual(m.low, spot - m.move_pts, places=9)
            self.assertAlmostEqual(m.high, spot + m.move_pts, places=9)
            self.assertAlmostEqual(m.straddle, 1.1 + 1.1, places=12)   # call mid + put mid
        self.assertEqual(vm.pick(moves, None).expiry, min(m.expiry for m in moves))

    def test_atm_needs_two_sided_call_and_put(self):
        payload, now = synthetic_payload()
        spot, chain, _, _ = dfh.parse_payload(payload)
        first = chain["expiry"].min()
        # Kill the bid on the 6600 call of the first expiry -> next-nearest strike is used.
        dead = (chain["expiry"] == first) & (chain["strike"] == 6600.0) & (chain["type"] == "call")
        chain.loc[dead, "bid"] = 0.0
        m = vm.pick(vm.expected_moves(chain, spot, now=now), first)
        self.assertEqual(m.atm_strike, 6575.0)               # nearest; ties go to the lower strike


if __name__ == "__main__":
    unittest.main()
