"""Behavioral checks for the frozen competition decision engine."""
from __future__ import annotations

import unittest

from competition_strategy import HourSignal, StrategyConfig, StrategyState, advance
from download_v2_data import SYMBOLS


def signal(ret7d=0.0, ret72=0.0, liq24=30_000_000, chan=0, zone=0, wyckoff=0):
    return HourSignal(ret7d, ret72, liq24, chan, zone, wyckoff)


class StrategyTests(unittest.TestCase):
    def test_btc_entry_stop_cooldown_and_one_second_boundary(self):
        state = StrategyState()
        entry = advance(state, "2026-09-15T04:00:00Z",
                        {"BTCUSDT": signal(ret7d=0.03)}, {"BTCUSDT": 100.0})
        self.assertFalse(state.btc_held)  # No speculative state mutation.
        self.assertEqual(entry.execute_not_before, "2026-09-15T04:00:01+00:00")
        self.assertEqual([(x.side, x.entry_fraction_of_sleeve,
                           x.initial_sleeve_fraction_of_account) for x in entry.intents],
                         [("BUY", 1.0, 0.2)])

        stopped = advance(entry.next_state, "2026-09-15T04:15:00Z", {},
                          {"BTCUSDT": 95.0})
        self.assertEqual([(x.side, x.reason) for x in stopped.intents],
                         [("SELL", "btc_trailing_stop_5pct")])
        self.assertEqual(stopped.next_state.btc_cooldown_until,
                         "2026-09-15T08:15:00+00:00")
        blocked = advance(stopped.next_state, "2026-09-15T08:00:00Z",
                          {"BTCUSDT": signal(ret7d=0.03)}, {"BTCUSDT": 95.0})
        self.assertFalse(blocked.intents)
        resumed = advance(blocked.next_state, "2026-09-15T12:00:00Z",
                          {"BTCUSDT": signal(ret7d=0.03)}, {"BTCUSDT": 96.0})
        self.assertEqual([x.side for x in resumed.intents], ["BUY"])

    def test_alt_relative_strength_stop_and_daily_cooldown(self):
        rows = {s: signal() for s in SYMBOLS}
        rows["BTCUSDT"] = signal(ret7d=0.01)
        rows["NEARUSDT"] = signal(ret7d=0.20, ret72=0.05, chan=1)
        closes = {s: 100.0 for s in SYMBOLS}
        entered = advance(StrategyState(), "2026-09-15T00:00:00Z", rows, closes)
        self.assertIn("NEARUSDT", entered.next_state.alt_held)
        self.assertEqual([(x.entry_fraction_of_sleeve,
                           x.initial_sleeve_fraction_of_account)
                          for x in entered.intents if x.symbol == "NEARUSDT"],
                         [(0.5, 0.8)])

        stopped = advance(entered.next_state, "2026-09-15T00:15:00Z", {},
                          {"BTCUSDT": 100.0, "NEARUSDT": 87.0})
        self.assertEqual([x.side for x in stopped.intents], ["SELL"])
        self.assertEqual(stopped.next_state.alt_held, [])
        blocked = advance(stopped.next_state, "2026-09-16T00:00:00Z", rows, closes)
        self.assertNotIn("NEARUSDT", blocked.next_state.alt_held)
        resumed = advance(blocked.next_state, "2026-09-17T00:00:00Z", rows, closes)
        self.assertIn("NEARUSDT", resumed.next_state.alt_held)

    def test_daily_requires_complete_closed_bar_snapshot(self):
        with self.assertRaisesRegex(ValueError, "all 21"):
            advance(StrategyState(), "2026-09-15T00:00:00Z",
                    {"BTCUSDT": signal()}, {"BTCUSDT": 100.0})

    def test_reprocessing_bar_rejected(self):
        first = advance(StrategyState(), "2026-09-15T00:15:00Z", {},
                        {"BTCUSDT": 100.0})
        with self.assertRaisesRegex(ValueError, "already processed"):
            advance(first.next_state, "2026-09-15T00:15:00Z", {},
                    {"BTCUSDT": 100.0})

    def test_rank_hysteresis_delays_only_soft_exit_and_survives_state_reload(self):
        rows = {s: signal() for s in SYMBOLS}
        rows["BTCUSDT"] = signal(ret7d=0.01)
        rows["NEARUSDT"] = signal(ret7d=0.10, ret72=0.03, chan=1)
        closes = {s: 100.0 for s in SYMBOLS}
        config = StrategyConfig(rank_exit_confirmation_days=2)
        entered = advance(StrategyState(), "2026-09-15T00:00:00Z", rows, closes, config)
        self.assertEqual(entered.next_state.alt_held, ["NEARUSDT"])
        self.assertEqual([x.entry_fraction_of_sleeve for x in entered.intents
                          if x.symbol == "NEARUSDT"], [0.5])

        displaced = dict(rows)
        for symbol in ("ETHUSDT", "SOLUSDT", "ZECUSDT", "XRPUSDT", "SUIUSDT"):
            displaced[symbol] = signal(ret7d=0.25, ret72=0.04, chan=1)
        frozen = advance(entered.next_state, "2026-09-17T00:00:00Z", displaced, closes)
        self.assertIn("NEARUSDT", [x.symbol for x in frozen.intents if x.side == "SELL"])
        retained = advance(entered.next_state, "2026-09-17T00:00:00Z", displaced,
                           closes, config)
        self.assertIn("NEARUSDT", retained.next_state.alt_held)
        self.assertEqual(retained.next_state.alt_soft_fail_days["NEARUSDT"], 1)
        restored = StrategyState.from_mapping(retained.next_state.to_dict())
        exited = advance(restored, "2026-09-18T00:00:00Z", displaced, closes, config)
        self.assertNotIn("NEARUSDT", exited.next_state.alt_held)
        self.assertNotIn("NEARUSDT", exited.next_state.alt_soft_fail_days)
        self.assertIn("NEARUSDT", [x.symbol for x in exited.intents if x.side == "SELL"])

        bad = dict(displaced)
        bad["NEARUSDT"] = signal(ret7d=-0.03, ret72=0.03, chan=-1)
        immediate = advance(restored, "2026-09-18T00:00:00Z", bad, closes, config)
        self.assertNotIn("NEARUSDT", immediate.next_state.alt_held)

    def test_single_structure_warning_is_confirmed_but_zone_failure_exits(self):
        rows = {s: signal() for s in SYMBOLS}
        rows["BTCUSDT"] = signal(ret7d=0.01)
        rows["NEARUSDT"] = signal(ret7d=0.10, ret72=0.03, chan=1)
        closes = {s: 100.0 for s in SYMBOLS}
        config = StrategyConfig(structure_exit_confirmation_days=2)
        entered = advance(StrategyState(), "2026-09-15T00:00:00Z", rows, closes, config)
        warning = dict(rows)
        warning["NEARUSDT"] = signal(ret7d=0.10, ret72=0.03, chan=1, wyckoff=-1)
        old = advance(entered.next_state, "2026-09-16T00:00:00Z", warning, closes)
        self.assertNotIn("NEARUSDT", old.next_state.alt_held)
        held = advance(entered.next_state, "2026-09-16T00:00:00Z", warning,
                       closes, config)
        self.assertIn("NEARUSDT", held.next_state.alt_held)
        self.assertEqual(held.next_state.alt_soft_fail_days["NEARUSDT"], 1)
        failed = advance(held.next_state, "2026-09-17T00:00:00Z", warning,
                         closes, config)
        self.assertNotIn("NEARUSDT", failed.next_state.alt_held)

        bad_zone = dict(rows)
        bad_zone["NEARUSDT"] = signal(ret7d=0.10, chan=1, zone=-1)
        immediate = advance(held.next_state, "2026-09-17T00:00:00Z", bad_zone,
                            closes, config)
        self.assertNotIn("NEARUSDT", immediate.next_state.alt_held)

        # A rank miss followed by a structure warning is two consecutive soft
        # failures, even though the cause changed between daily checks.
        rank_miss = dict(rows)
        for symbol in ("ETHUSDT", "SOLUSDT", "ZECUSDT", "XRPUSDT", "SUIUSDT"):
            rank_miss[symbol] = signal(ret7d=0.25, ret72=0.04, chan=1)
        combined = StrategyConfig(2, 2)
        first = advance(entered.next_state, "2026-09-17T00:00:00Z",
                        rank_miss, closes, combined)
        second = advance(first.next_state, "2026-09-18T00:00:00Z",
                         warning, closes, combined)
        self.assertNotIn("NEARUSDT", second.next_state.alt_held)


if __name__ == "__main__":
    unittest.main()
