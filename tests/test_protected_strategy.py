"""Failure and event scenarios for the optional risk layer."""
from __future__ import annotations

import unittest

from competition_strategy import HourSignal, StrategyState
from download_v2_data import SYMBOLS
from protected_strategy import (
    ProtectedState, Quote, cpi_releases_utc, on_bar, on_tick,
)


def held() -> ProtectedState:
    return ProtectedState(StrategyState(
        btc_held=True, btc_peak_close=100.0, alt_held=["NEARUSDT"],
        alt_peak_close={"NEARUSDT": 100.0},
        alt_entry_time={"NEARUSDT": "2026-10-12T00:00:00+00:00"}))


def quote(bid: float, ask: float, when: str) -> Quote:
    return Quote(bid, ask, when)


class ProtectedStrategyTests(unittest.TestCase):
    def test_official_cpi_time_and_pre_event_flatten(self):
        self.assertEqual(cpi_releases_utc()[-1].isoformat(),
                         "2026-10-14T12:30:00+00:00")
        state = held()
        result = on_tick(state, "2026-10-14T11:30:00Z", {}, 100_000)
        self.assertEqual([(x.symbol, x.side) for x in result.intents],
                         [("BTCUSDT", "SELL"), ("NEARUSDT", "SELL")])
        self.assertFalse(result.next_state.strategy.btc_held)
        self.assertEqual(result.next_state.strategy.alt_held, [])
        self.assertTrue(state.strategy.btc_held)  # Proposed state only.

    def test_live_bid_triggers_exit_before_15m_close(self):
        when = "2026-10-05T00:01:00Z"
        result = on_tick(held(), when,
                         {"BTCUSDT": quote(94, 94.1, when),
                          "NEARUSDT": quote(87, 87.1, when)}, 100_000)
        self.assertEqual({x.symbol for x in result.intents}, {"BTCUSDT", "NEARUSDT"})
        self.assertEqual(result.execute_not_before, "2026-10-05T00:01:01+00:00")
        self.assertIn("intrabar_btc_trailing_stop_5pct",
                      [x.reason for x in result.intents])

    def test_missing_one_daily_candidate_cannot_block_other_stop(self):
        when = "2026-10-05T00:00:00Z"
        rows = {s: HourSignal(0.0, 0.0, 30_000_000, 0, 0, 0)
                for s in SYMBOLS if s != "ADAUSDT"}
        closes = {s: 100.0 for s in SYMBOLS}
        closes["BTCUSDT"] = 94.0
        result = on_bar(held(), when, rows, closes,
                        {s: when for s in rows}, {s: when for s in SYMBOLS},
                        {}, 100_000, when)
        self.assertEqual([(x.symbol, x.side) for x in result.intents],
                         [("BTCUSDT", "SELL")])
        self.assertIn("stale_or_missing_1h:ADAUSDT", result.alerts)
        self.assertIn("new_entries_paused", result.alerts)

    def test_wide_spread_vetoes_new_buy_without_consuming_signal_state(self):
        when = "2026-10-05T04:00:00Z"
        row = HourSignal(0.03, 0.02, 30_000_000, 0, 0, 0)
        result = on_bar(ProtectedState(), when, {"BTCUSDT": row},
                        {"BTCUSDT": 100.0}, {"BTCUSDT": when},
                        {"BTCUSDT": when},
                        {"BTCUSDT": quote(99.0, 100.0, when)}, 100_000, when)
        self.assertFalse(result.intents)
        self.assertFalse(result.next_state.strategy.btc_held)
        self.assertIn("new_buy_vetoed_untrusted_quote:BTCUSDT", result.alerts)

    def test_fresh_narrow_quote_allows_buy_at_actual_evaluation_time(self):
        when = "2026-10-05T04:00:00Z"
        evaluated = "2026-10-05T04:00:03Z"
        row = HourSignal(0.03, 0.02, 30_000_000, 0, 0, 0)
        result = on_bar(ProtectedState(), when, {"BTCUSDT": row},
                        {"BTCUSDT": 100.0}, {"BTCUSDT": when},
                        {"BTCUSDT": when},
                        {"BTCUSDT": quote(100.0, 100.1, evaluated)},
                        100_000, evaluated, {"BTCUSDT": 99.0})
        self.assertEqual([x.side for x in result.intents], ["BUY"])
        self.assertEqual(result.execute_not_before, "2026-10-05T04:00:03+00:00")

    def test_vertical_15m_pump_vetoes_chasing_buy(self):
        when = "2026-10-05T04:00:00Z"
        row = HourSignal(0.20, 0.20, 30_000_000, 1, 0, 0)
        result = on_bar(ProtectedState(), when, {"BTCUSDT": row},
                        {"BTCUSDT": 120.0}, {"BTCUSDT": when},
                        {"BTCUSDT": when},
                        {"BTCUSDT": quote(120.0, 120.1, when)},
                        100_000, when, {"BTCUSDT": 100.0})
        self.assertFalse(result.intents)
        self.assertIn("new_buy_vetoed_abrupt_15m_move:BTCUSDT", result.alerts)

    def test_price_jumps_after_bar_close_vetoes_buy_even_with_narrow_spread(self):
        when = "2026-10-05T04:00:00Z"
        evaluated = "2026-10-05T04:00:02Z"
        row = HourSignal(0.03, 0.02, 30_000_000, 0, 0, 0)
        result = on_bar(ProtectedState(), when, {"BTCUSDT": row},
                        {"BTCUSDT": 100.0}, {"BTCUSDT": when},
                        {"BTCUSDT": when},
                        {"BTCUSDT": quote(105.0, 105.1, evaluated)},
                        100_000, evaluated, {"BTCUSDT": 99.0})
        self.assertFalse(result.intents)
        self.assertIn("new_buy_vetoed_quote_price_gap:BTCUSDT", result.alerts)

    def test_account_drawdown_flattens_and_halts(self):
        state = held()
        state.peak_equity = 100_000.0
        result = on_tick(state, "2026-10-05T00:01:00Z", {}, 87_000.0)
        self.assertEqual(len(result.intents), 2)
        self.assertEqual(result.next_state.paused_until, "2026-10-06T00:01:00+00:00")
        self.assertEqual(result.next_state.pause_reason, "portfolio_drawdown")

    def test_stale_or_malformed_quote_does_not_generate_order(self):
        result = on_tick(held(), "2026-10-05T00:01:00Z",
                         {"BTCUSDT": quote(1.0, 1.1, "bad-timestamp")}, 100_000)
        self.assertFalse(result.intents)
        self.assertIn("stale_or_missing_quote:BTCUSDT", result.alerts)

    def test_missing_equity_or_malformed_bar_time_still_allows_valid_stop(self):
        when = "2026-10-05T04:00:00Z"
        row = HourSignal(0.03, 0.02, 30_000_000, 0, 0, 0)
        result = on_bar(held(), when, {"BTCUSDT": row},
                        {"BTCUSDT": 94.0, "NEARUSDT": 100.0},
                        {"BTCUSDT": None},
                        {"BTCUSDT": when, "NEARUSDT": when},
                        {}, None, when)
        self.assertEqual([(x.symbol, x.side) for x in result.intents],
                         [("BTCUSDT", "SELL")])
        self.assertIn("account_equity_unavailable", result.alerts)

    def test_late_bar_suppresses_entry_but_preserves_exit(self):
        when = "2026-10-05T04:00:00Z"
        row = HourSignal(0.03, 0.02, 30_000_000, 0, 0, 0)
        result = on_bar(ProtectedState(), when, {"BTCUSDT": row},
                        {"BTCUSDT": 100.0}, {"BTCUSDT": when},
                        {"BTCUSDT": when}, {}, 100_000,
                        "2026-10-05T04:00:45Z")
        self.assertFalse(result.intents)
        self.assertIn("bar_processing_delay", result.alerts)
        self.assertEqual(result.execute_not_before, "2026-10-05T04:00:45+00:00")


if __name__ == "__main__":
    unittest.main()
