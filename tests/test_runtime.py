from __future__ import annotations

import hashlib
import hmac
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd

from backtest import build_features
from competition_strategy import BTC, HourSignal, StrategyConfig, advance, iso, utc_time
from protected_strategy import GuardConfig, ProtectedDecision, ProtectedState, Quote, on_tick
from roostoo_bot.api import APIError, AmbiguousOrder, RateLimiter, Roostoo, canonical
from roostoo_bot.config import Config
from roostoo_bot.execution import Blocked, Executor
from roostoo_bot.feed import Feed, Snapshot, boundary, hour_features
from roostoo_bot.runner import Runner, decide, restore_missed_peaks
from roostoo_bot.store import Store, dec, floor_quantity, process_lock, reconcile_wallet

NOW = utc_time("2026-10-05T04:00:02Z")
RULE = {"AmountPrecision": 5, "MiniOrder": 1, "CanTrade": True, "Unit": "USD"}


def config(root, mode="observe"):
    return Config(mode, "test-key" if mode != "observe" else "", "test-secret", root, root / "logs",
                  "2026-05-01T00:00:00+00:00", "protected", StrategyConfig(), GuardConfig())


def wallet(ledger):
    out = {"USD": {"Free": str(dec(ledger["core"]["cash"]) + dec(ledger["alt"]["cash"])), "Lock": 0}}
    for s, p in ledger["positions"].items():
        out[s[:-4]] = {"Free": p["quantity"], "Lock": 0}
    return out


def decision(state=None):
    result = advance((state or ProtectedState()).strategy, "2026-10-05T04:00:00Z",
                     {BTC: HourSignal(.1, .03, 30_000_000, 0, 0, 0)}, {BTC: 100})
    candidate = ProtectedState.from_mapping((state or ProtectedState()).to_dict())
    candidate.strategy = result.next_state
    return ProtectedDecision(result.decision_at, result.execute_not_before, result.intents, candidate)


class FakeAPI:
    def __init__(self, store=None):
        self.store, self.time = store, NOW
        self.sent, self.details, self.recent = [], {}, []
        self.canceled = []
        self.failure = None

    def now(self):
        return self.time

    def quotes(self):
        return {BTC: Quote(100, 100.1, iso(self.time)), "NEARUSDT": Quote(10, 10.01, iso(self.time))}

    def balance(self):
        return wallet(self.store.account()["ledger"])

    def orders(self, order_id=None, **kwargs):
        return [self.details[order_id]] if order_id else self.recent

    def cancel(self, order_id):
        self.canceled.append(order_id)
        return {"Success": True, "CanceledList": [order_id]}

    def place(self, symbol, side, quantity, before_send):
        before_send()
        self.sent.append((symbol, side, quantity))
        if self.failure:
            raise self.failure
        q, price = dec(quantity), Decimal("100.1")
        detail = {"Pair": "BTC/USD", "Side": side, "Type": "MARKET", "OrderID": "51",
                  "Status": "FILLED", "Quantity": quantity, "FilledQuantity": quantity,
                  "FilledAverPrice": str(price), "UnitChange": str(q * price),
                  "CommissionCoin": "USD", "CommissionChargeValue": str(q * price * Decimal(".001")),
                  "CreateTimestamp": int(self.time.timestamp() * 1000)}
        self.details["51"] = detail
        self.recent = [detail]
        return detail


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cfg = config(self.root)
        self.store = Store(self.root / "observe.sqlite")
        self.store.initialize(self.cfg.fingerprint, 100_000)
        self.api = FakeAPI(self.store)
        self.ex = Executor(self.cfg, self.api, self.store, {BTC: RULE, "NEARUSDT": RULE}, lambda *a, **k: None,
                           sleep=lambda delay: setattr(self.api, "time", self.api.time + timedelta(seconds=delay)))
        self.ex.quotes = self.api.quotes()
        self.ex.validate_balance()

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def prepare(self):
        self.store.prepare("bar:2026-10-05T04:00:00+00:00", decision(), iso(NOW))
        return self.store.orders(self.store.pending()["id"])[0]

    def live(self):
        self.cfg = replace(self.cfg, mode="test", api_key="test-key")
        self.ex.cfg = self.cfg
        self.ex.wallet = wallet(self.store.account()["ledger"])

    def test_observe_never_calls_order_api_and_keeps_two_sleeves(self):
        self.prepare()
        self.ex.run_pending()
        blob = self.store.account()
        self.assertEqual(self.api.sent, [])
        self.assertEqual(dec(blob["ledger"]["alt"]["cash"]), Decimal("80000"))
        self.assertTrue(blob["state"]["strategy"]["btc_held"])
        self.assertLess(dec(blob["ledger"]["core"]["cash"]), Decimal("20000"))
        self.assertIsNone(self.store.pending())
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM events WHERE kind='fill'").fetchone()[0], 1)

    def test_restart_preserves_sleeve_cash_and_refuses_different_key_or_variant(self):
        self.prepare()
        self.ex.run_pending()
        expected = self.store.account()
        self.store.db.close()
        self.store = Store(self.root / "observe.sqlite")
        self.store.initialize(self.cfg.fingerprint, 999_999)
        self.assertEqual(self.store.account(), expected)
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            self.store.initialize(replace(self.cfg, variant="baseline").fingerprint, 100_000)

    def test_timeout_never_resubmits_and_unique_match_recovers(self):
        self.live()
        self.prepare()
        self.api.failure = AmbiguousOrder("timeout")
        with self.assertRaises(Blocked):
            self.ex.run_pending()
        self.assertEqual(len(self.api.sent), 1)
        with self.assertRaisesRegex(Blocked, "0 matching"):
            self.ex.run_pending()
        self.assertEqual(len(self.api.sent), 1)
        row = self.store.orders(self.store.pending()["id"])[0]
        qty = row["payload"]["quantity"]
        self.api.recent = [{"Pair": "BTC/USD", "Side": "BUY", "Type": "MARKET", "OrderID": "51",
                            "Status": "FILLED", "Quantity": qty, "FilledQuantity": qty,
                            "FilledAverPrice": "100.1", "UnitChange": str(dec(qty) * Decimal("100.1")),
                            "CommissionCoin": "USD", "CommissionChargeValue": "0",
                            "CreateTimestamp": int(NOW.timestamp() * 1000)}]
        self.ex.run_pending()
        self.assertEqual(len(self.api.sent), 1)
        self.assertTrue(self.store.account()["state"]["strategy"]["btc_held"])

    def test_two_matching_orders_remain_blocked(self):
        self.live()
        row = self.prepare()
        self.store.update_order(row, "submitting", quantity="1", sent_at=int(NOW.timestamp() * 1000), seen_ids=[])
        detail = {"Pair": "BTC/USD", "Side": "BUY", "Type": "MARKET", "Quantity": "1",
                  "CreateTimestamp": int(NOW.timestamp() * 1000)}
        self.api.recent = [{**detail, "OrderID": "1"}, {**detail, "OrderID": "2"}]
        with self.assertRaisesRegex(Blocked, "2 matching"):
            self.ex.run_pending()
        self.assertFalse(self.api.sent)

    def test_explicit_rejection_does_not_commit_phantom_position(self):
        self.live()
        self.prepare()
        self.api.failure = APIError("insufficient balance")
        self.ex.run_pending()
        self.assertFalse(self.store.account()["state"]["strategy"]["btc_held"])
        self.assertEqual(dec(self.store.account()["ledger"]["core"]["cash"]), Decimal("20000"))

    def test_partial_fills_apply_only_cumulative_delta_even_after_restart(self):
        row = self.prepare()
        self.store.update_order(row, "known", quantity="10", order_id="51")
        row = self.store.order(row["id"])
        partial = {"OrderID": "51", "Pair": "BTC/USD", "Side": "BUY", "Type": "MARKET", "Status": "PENDING",
                   "FilledQuantity": "4", "FilledAverPrice": "100", "UnitChange": "400",
                   "CommissionCoin": "USD", "CommissionChargeValue": ".4"}
        self.assertFalse(self.store.apply_fill(row, partial, RULE, self.ex.quotes[BTC], iso(NOW)))
        saved = self.store.account()
        self.store.apply_fill(row, partial, RULE, self.ex.quotes[BTC], iso(NOW))  # deliberately stale caller row
        self.assertEqual(self.store.account(), saved)
        self.store.db.close()
        self.store = Store(self.root / "observe.sqlite")
        final = {**partial, "Status": "FILLED", "FilledQuantity": "10", "UnitChange": "1000", "CommissionChargeValue": "1"}
        self.store.apply_fill(row, final, RULE, self.ex.quotes[BTC], iso(NOW))
        self.assertEqual(dec(self.store.account()["ledger"]["core"]["cash"]), Decimal("18999"))
        self.assertEqual(dec(self.store.account()["ledger"]["positions"][BTC]["quantity"]), Decimal("10"))
        self.assertEqual(dec(self.store.account()["ledger"]["alt"]["cash"]), Decimal("80000"))

    def test_partial_sell_preserves_held_metadata_and_base_asset_fee(self):
        self.prepare()
        self.ex.run_pending()
        state = ProtectedState.from_mapping(self.store.account()["state"])
        stop = on_tick(state, NOW + timedelta(seconds=4), {BTC: Quote(94, 94.1, iso(NOW + timedelta(seconds=4)))}, 99_000)
        self.store.prepare("tick:stop", stop, iso(NOW))
        row = self.store.orders("tick:stop")[0]
        original = dec(self.store.account()["ledger"]["positions"][BTC]["quantity"])
        self.store.update_order(row, "known", quantity=str(original), order_id="52")
        detail = {"OrderID": "52", "Pair": "BTC/USD", "Side": "SELL", "Type": "MARKET", "Status": "CANCELED",
                  "FilledQuantity": "1", "FilledAverPrice": "94", "UnitChange": "94",
                  "CommissionCoin": "BTC", "CommissionChargeValue": ".001"}
        self.store.apply_fill(row, detail, RULE, Quote(94, 94.1, iso(NOW)), iso(NOW))
        self.store.finish("tick:stop")
        blob = self.store.account()
        self.assertTrue(blob["state"]["strategy"]["btc_held"])
        self.assertEqual(blob["state"]["strategy"]["btc_peak_close"], 100)
        self.assertEqual(dec(blob["ledger"]["positions"][BTC]["quantity"]), original - Decimal("1.001"))

    def test_known_pending_order_cannot_send_remaining_batch(self):
        self.live()
        row = self.prepare()
        self.store.update_order(row, "known", quantity="1", order_id="51")
        self.api.details["51"] = {"OrderID": "51", "Pair": "BTC/USD", "Side": "BUY", "Type": "MARKET",
                                  "Status": "PENDING", "FilledQuantity": "0", "UnitChange": "0"}
        with self.assertRaisesRegex(Blocked, "pending"):
            self.ex.run_pending()
        self.assertFalse(self.api.sent)
        self.assertIsNotNone(self.store.pending())

    def test_quantity_rounding_minimum_and_deadline(self):
        self.assertEqual(floor_quantity(".1234599", 5), Decimal(".12345"))
        self.prepare()
        self.api.time = NOW + timedelta(seconds=40)
        self.ex.quotes = self.api.quotes()
        self.ex.run_pending()
        self.assertFalse(self.api.sent)
        self.assertFalse(self.store.account()["state"]["strategy"]["btc_held"])

    def test_wallet_divergence_and_locked_funds_fail_closed(self):
        w = wallet(self.store.account()["ledger"])
        reconcile_wallet(self.store.account()["ledger"], w, {BTC: RULE})
        w["BTC"] = {"Free": "1", "Lock": "0"}
        with self.assertRaisesRegex(ValueError, "mismatch BTC"):
            reconcile_wallet(self.store.account()["ledger"], w, {BTC: RULE})
        del w["BTC"]
        w["USD"] = {"Free": "99999", "Lock": "1"}
        with self.assertRaisesRegex(ValueError, "locked"):
            reconcile_wallet(self.store.account()["ledger"], w, {BTC: RULE})

    def test_same_bar_and_concurrent_process_cannot_duplicate_execution(self):
        self.prepare()
        self.ex.run_pending()
        self.assertFalse(self.store.prepare("bar:2026-10-05T04:00:00+00:00", decision(), iso(NOW)))
        with process_lock(self.root / "run.lock"):
            with self.assertRaisesRegex(RuntimeError, "another bot"):
                with process_lock(self.root / "run.lock"):
                    pass

    def test_two_alt_entries_use_only_the_alt_sleeve(self):
        from download_v2_data import SYMBOLS
        at = utc_time("2026-10-05T00:00:00Z")
        rows = {s: HourSignal(0, 0, 30_000_000, 0, 0, 0) for s in SYMBOLS}
        rows["ETHUSDT"] = HourSignal(.2, .03, 30_000_000, 1, 0, 0)
        rows["NEARUSDT"] = HourSignal(.15, .03, 30_000_000, 1, 0, 0)
        result = advance(ProtectedState().strategy, at, rows, {s: 10 for s in SYMBOLS})
        proposed = ProtectedDecision(result.decision_at, result.execute_not_before, result.intents,
                                     ProtectedState(strategy=result.next_state))
        self.api.time = at + timedelta(seconds=2)
        self.ex.quotes = {"ETHUSDT": Quote(10, 10.001, iso(self.api.time)),
                          "NEARUSDT": Quote(10, 10.001, iso(self.api.time))}
        self.ex.rules["ETHUSDT"] = RULE
        self.store.prepare("bar:" + iso(at), proposed, iso(self.api.time))
        self.ex.run_pending()
        blob = self.store.account()
        self.assertEqual(blob["state"]["strategy"]["alt_held"], ["ETHUSDT", "NEARUSDT"])
        self.assertEqual(dec(blob["ledger"]["core"]["cash"]), Decimal("20000"))
        for symbol in ("ETHUSDT", "NEARUSDT"):
            self.assertEqual(blob["ledger"]["positions"][symbol]["sleeve"], "alt")
            self.assertGreater(dec(blob["ledger"]["positions"][symbol]["quantity"]), Decimal("3900"))
        self.assertGreaterEqual(dec(blob["ledger"]["alt"]["cash"]), 0)

    def test_balance_failure_after_fill_cannot_resend_or_finish_until_verified(self):
        self.live()
        self.prepare()
        original_balance = self.api.balance
        def mismatched():
            w = original_balance()
            w["USD"]["Free"] = str(dec(w["USD"]["Free"]) + 1)
            return w
        self.api.balance = mismatched
        for _ in range(2):
            with self.assertRaisesRegex(Blocked, "balance mismatch"):
                self.ex.run_pending()
        self.assertEqual(len(self.api.sent), 1)
        self.assertIsNotNone(self.store.pending())
        self.api.balance = original_balance
        self.ex.run_pending()
        self.assertIsNone(self.store.pending())
        self.assertEqual(len(self.api.sent), 1)

    def test_old_pending_market_order_cancels_once_then_waits_for_confirmation(self):
        self.live()
        row = self.prepare()
        self.store.update_order(row, "known", quantity="1", order_id="51",
                                sent_at=int((NOW - timedelta(minutes=3)).timestamp() * 1000))
        self.api.details["51"] = {"OrderID": "51", "Pair": "BTC/USD", "Side": "BUY", "Type": "MARKET",
                                  "Status": "PENDING", "FilledQuantity": "0", "UnitChange": "0"}
        for _ in range(2):
            with self.assertRaises(Blocked):
                self.ex.run_pending()
        self.assertEqual(self.api.canceled, ["51"])
        self.api.details["51"]["Status"] = "CANCELED"
        self.ex.run_pending()
        self.assertIsNone(self.store.pending())
        self.assertFalse(self.api.sent)

    def test_base_asset_fee_leaves_dust_in_ledger_without_phantom_holding(self):
        row = self.prepare()
        self.store.update_order(row, "known", quantity="1", order_id="51")
        buy = {"OrderID": "51", "Pair": "BTC/USD", "Side": "BUY", "Type": "MARKET", "Status": "FILLED",
               "FilledQuantity": "1", "FilledAverPrice": "100", "UnitChange": "100",
               "CommissionCoin": "BTC", "CommissionChargeValue": ".000001"}
        self.store.apply_fill(row, buy, RULE, self.ex.quotes[BTC], iso(NOW))
        self.store.finish(self.store.pending()["id"])
        state = ProtectedState.from_mapping(self.store.account()["state"])
        stop = on_tick(state, NOW, {BTC: Quote(94, 94.1, iso(NOW))}, 99_000)
        self.store.prepare("tick:dust", stop, iso(NOW))
        row = self.store.orders("tick:dust")[0]
        self.store.update_order(row, "known", quantity=".99999", order_id="52")
        sell = {**buy, "OrderID": "52", "Side": "SELL", "FilledQuantity": ".99999",
                "FilledAverPrice": "94", "UnitChange": "93.99906", "CommissionCoin": "USD", "CommissionChargeValue": "0"}
        self.store.apply_fill(row, sell, RULE, Quote(94, 94.1, iso(NOW)), iso(NOW))
        self.store.finish("tick:dust")
        blob = self.store.account()
        self.assertEqual(dec(blob["ledger"]["positions"][BTC]["quantity"]), Decimal(".000009"))
        self.assertTrue(blob["ledger"]["positions"][BTC]["dust"])
        self.assertFalse(blob["state"]["strategy"]["btc_held"])
        reconcile_wallet(blob["ledger"], wallet(blob["ledger"]), {BTC: RULE})

    def test_full_runner_observe_loop_warms_decides_and_commits_a_simulated_fill(self):
        from unittest.mock import patch
        self.api.exchange_info = lambda: {BTC: RULE, "NEARUSDT": RULE}
        self.api.clock = lambda: NOW.timestamp()
        self.api.last_sync = NOW.timestamp()
        at = utc_time("2026-10-05T04:00:00Z")
        snap = Snapshot(hourly={BTC: HourSignal(.1, .03, 30_000_000, 0, 0, 0)},
                        closes={BTC: 100}, previous={BTC: 100}, hour_at={BTC: iso(at)}, close_at={BTC: iso(at)})
        class FakeFeed:
            frames = {BTC: object()}
            def refresh(self, now):
                return {}
            def snapshot(self, when):
                return snap
        events = []
        runner = Runner(self.cfg, self.api, self.store, FakeFeed(), lambda kind, **data: events.append((kind, data)))
        with patch("roostoo_bot.runner.time.sleep", lambda delay: None):
            runner.run(once=True)
        self.assertFalse(self.api.sent)
        self.assertTrue(self.store.account()["state"]["strategy"]["btc_held"])
        self.assertTrue(any(kind == "once_complete" for kind, _ in events))
        self.assertTrue(any(kind == "order_observed" and data["simulated"] for kind, data in events))


class APITests(unittest.TestCase):
    def test_balance_accepts_current_spot_envelope_and_legacy_wallet(self):
        class Limiter:
            def acquire(self):
                pass
        expected = {"USD": {"Free": 50000, "Lock": 0, "PendingOrders": 0, "ShortCollateral": 0}}
        for envelope in ({"Success": True, "SpotWallet": expected, "MarginWallet": {}},
                         {"Success": True, "Wallet": expected}):
            api = Roostoo(config(Path("/tmp"), "test"), Limiter(), send=lambda *a: envelope)
            self.assertEqual(api.balance(), expected)
        api = Roostoo(config(Path("/tmp"), "test"), Limiter(),
                       send=lambda *a: {"Success": True, "SpotWallet": expected, "MarginWallet": {"USD": {"Free": 1}}})
        with self.assertRaisesRegex(APIError, "margin wallet"):
            api.balance()
        api.send = lambda *a: {"Success": True}
        with self.assertRaisesRegex(APIError, "no usable"):
            api.balance()

    def test_official_signature_and_wire_body(self):
        payload = {"timestamp": 1580774512000, "pair": "BNB/USD", "quantity": 2000, "side": "BUY", "type": "MARKET"}
        secret = "S1XP1e3UZj6A7H5fATj0jNhqPxxdSJYdInClVN65XAbvqqMKjVHjA7PZj4W12oep"
        self.assertEqual(hmac.new(secret.encode(), canonical(payload).encode(), hashlib.sha256).hexdigest(),
                         "20b7fd5550b67b3bf0c1684ed0f04885261db8fdabd38611e9e6af23c19b7fff")
        calls = []
        class Limiter:
            def acquire(self):
                pass
        def send(method, url, headers, body, timeout):
            calls.append((method, url, headers, body))
            return {"Success": True, "OrderDetail": {"OrderID": 1}}
        cfg = config(Path("/tmp"), "test")
        api = Roostoo(cfg, Limiter(), send, clock=lambda: 1580774512)
        api.place(BTC, "BUY", "1.00000", lambda: None)
        method, url, headers, body = calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(body.decode(), "pair=BTC/USD&quantity=1.00000&side=BUY&timestamp=1580774512000&type=MARKET")
        self.assertEqual(headers["MSG-SIGNATURE"], hmac.new(cfg.secret.encode(), body, hashlib.sha256).hexdigest())

    def test_order_timeout_one_attempt_query_can_retry_and_observe_blocks_signed(self):
        calls = []
        class Limiter:
            def acquire(self):
                calls.append("slot")
        def send(*args):
            raise TimeoutError()
        api = Roostoo(config(Path("/tmp"), "test"), Limiter(), send, sleep=lambda t: None)
        with self.assertRaises(AmbiguousOrder):
            api.place(BTC, "BUY", "1", lambda: None)
        self.assertEqual(len(calls), 1)
        with self.assertRaises(APIError):
            api.orders(order_id="1")
        self.assertEqual(len(calls), 4)
        api.config = config(Path("/tmp"))
        with self.assertRaisesRegex(APIError, "observe"):
            api.balance()

    def test_rate_window_is_shared_by_retries_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = [1000.0]
            stamps = []
            def sleep(delay):
                clock[0] += delay
            path = Path(directory) / "rates.sqlite"
            limit = RateLimiter(path, limit=3, clock=lambda: clock[0], sleep=sleep)
            for _ in range(3):
                limit.acquire()
                stamps.append(clock[0])
            limit.db.close()
            limit = RateLimiter(path, limit=3, clock=lambda: clock[0], sleep=sleep)
            limit.acquire()
            self.assertGreater(clock[0], stamps[0] + 60)
            limit.db.close()


class FeatureTests(unittest.TestCase):
    def test_live_features_equal_research_and_cannot_look_into_future(self):
        rng = np.random.default_rng(91)
        count = 210 * 60
        index = pd.date_range("2026-05-01", periods=count, freq="min", tz="UTC")
        close = 100 * np.exp(rng.normal(0, .0005, count).cumsum())
        minute = pd.DataFrame({"open": close, "high": close * 1.001, "low": close * .999,
                               "close": close, "volume": 10, "quote_volume": close * 10}, index=index)
        research, _ = build_features(minute)
        live = hour_features(research[["open", "high", "low", "close", "volume", "quote_volume"]])
        for column in ("chan", "zone", "wyckoff"):
            pd.testing.assert_series_equal(live[column], research[column])
        self.assertAlmostEqual(live.ret7d.iloc[-1], research.close.iloc[-1] / research.close.iloc[-169] - 1)
        self.assertAlmostEqual(live.liq24.iloc[-1], research.quote_volume.iloc[-24:].sum())
        prefix = hour_features(research.iloc[:185][["open", "high", "low", "close", "volume", "quote_volume"]])
        pd.testing.assert_frame_equal(prefix, live.iloc[:185])

    def test_partial_candle_excluded_and_real_timestamp_required(self):
        with tempfile.TemporaryDirectory() as directory:
            feed = Feed(config(Path(directory)), fetch=lambda *a: [])
            opening = int(utc_time("2026-10-05T04:00:00Z").timestamp() * 1000)
            row = [opening, "100", "101", "99", "100", "1", opening + 900_000 - 1, "100"]
            self.assertEqual(feed.put(BTC, "15m", [row], opening + 1000), 0)
            self.assertEqual(feed.put(BTC, "15m", [row], opening + 900_000), 1)
            self.assertNotIn(BTC, feed.snapshot(utc_time("2026-10-05T04:00:00Z")).closes)
            self.assertIn(BTC, feed.snapshot(utc_time("2026-10-05T04:15:00Z")).closes)
            row[0] *= 1000
            with self.assertRaises(ValueError):
                feed.put(BTC, "15m", [row], opening + 900_000)

    def test_stale_or_missing_feature_set_vetoes_entries_but_valid_stop_still_works(self):
        cfg = config(Path("/tmp"))
        state = ProtectedState.from_mapping(decision().next_state.to_dict())
        at = utc_time("2026-10-05T04:15:00Z")
        snapshot = Snapshot(closes={BTC: 94}, close_at={BTC: iso(at)})
        result = decide(cfg, state, at, snapshot, {}, 100_000, at + timedelta(seconds=40))
        self.assertEqual([(x.symbol, x.side) for x in result.intents], [(BTC, "SELL")])
        self.assertIn("new_entries_paused", result.alerts)

    def test_baseline_keeps_its_original_entry_signals(self):
        cfg = replace(config(Path("/tmp")), variant="baseline")
        at = utc_time("2026-10-05T04:00:00Z")
        snapshot = Snapshot(hourly={BTC: HourSignal(.1, .03, 30_000_000, 0, 0, 0)},
                            closes={BTC: 100}, previous={BTC: 100}, hour_at={BTC: iso(at)}, close_at={BTC: iso(at)})
        result = decide(cfg, ProtectedState(), at, snapshot, {}, 100_000, NOW)
        self.assertEqual(result.intents, decision().intents)
        self.assertEqual(result.next_state.strategy.to_dict(), decision().next_state.strategy.to_dict())

    def test_restart_restores_intervening_close_peak_and_refuses_missing_history(self):
        state = decision().next_state
        at = utc_time("2026-10-05T04:45:00Z")
        bars = pd.DataFrame({"close": [110, 120, 100]}, index=pd.date_range("2026-10-05T04:15:00Z", at, freq="15min"))
        class FakeFeed:
            def frame(self, *args):
                return bars
        restored = restore_missed_peaks(state, FakeFeed(), at)
        self.assertEqual(restored.strategy.btc_peak_close, 120)
        self.assertEqual(state.strategy.btc_peak_close, 100)
        result = on_tick(restored, at, {BTC: Quote(100, 100.1, iso(at))}, 100_000)
        self.assertEqual([o.side for o in result.intents], ["SELL"])
        bars = bars.iloc[[0, 2]]
        with self.assertRaisesRegex(Blocked, "incomplete missed-bar"):
            restore_missed_peaks(state, FakeFeed(), at)

    def test_frozen_sources_match_manifest(self):
        root = Path(__file__).resolve().parents[1]
        manifest = json.loads((root / "STRATEGY_MANIFEST.json").read_text())
        for name, digest in manifest["files"].items():
            self.assertEqual(hashlib.sha256((root / name).read_bytes()).hexdigest(), digest, name)


if __name__ == "__main__":
    unittest.main()
