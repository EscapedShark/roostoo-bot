from __future__ import annotations

import hashlib
import time
from decimal import Decimal

from competition_strategy import BTC, iso, utc_time
from protected_strategy import ProtectedState, on_tick

from .api import APIError, AmbiguousOrder, LocalSkip, pair
from .store import ZERO, dec, floor_quantity, reconcile_wallet, sleeve_equity


class Blocked(RuntimeError):
    """Preserve the journal and pause further execution until reconciled."""


class Executor:
    def __init__(self, config, api, store, rules, log, sleep=time.sleep):
        self.cfg, self.api, self.store, self.rules = config, api, store, rules
        self.log, self.sleep = log, sleep
        self.quotes, self.wallet = {}, None
        self.balance_at = None
        self.seen_ids = set(store.known_ids())

    def refresh_quotes(self):
        self.quotes = self.api.quotes()
        return self.quotes

    def validate_balance(self):
        if not self.cfg.live:
            self.balance_at = self.api.now()
            return
        wallet = self.api.balance()
        try:
            reconcile_wallet(self.store.account()["ledger"], wallet, self.rules)
        except ValueError as exc:
            raise Blocked(str(exc)) from None
        self.wallet, self.balance_at = wallet, self.api.now()

    def equity(self):
        now = self.api.now()
        if self.cfg.live and (self.balance_at is None or (now - self.balance_at).total_seconds() > 35):
            return None
        ledger = self.store.account()["ledger"]
        for symbol, position in ledger["positions"].items():
            if dec(position["quantity"]) and (symbol not in self.quotes or
                    not self.quotes[symbol].is_fresh(now, self.cfg.guard)):
                return None
        try:
            return float(sleeve_equity(ledger, "core", self.quotes) + sleeve_equity(ledger, "alt", self.quotes))
        except ValueError:
            return None

    def quantity(self, row):
        intent = row["payload"]["intent"]
        symbol, sleeve, side = intent["symbol"], intent["sleeve"], intent["side"]
        quote = self.quotes.get(symbol)
        if quote is None or not quote.is_fresh(self.api.now(), self.cfg.guard):
            raise LocalSkip("missing or stale sizing quote")
        ledger = self.store.account()["ledger"]
        position = ledger["positions"].get(symbol, {})
        if side == "SELL":
            quantity = dec(position.get("quantity", 0))
            if self.wallet:
                quantity = min(quantity, dec(self.wallet.get(symbol[:-4], {}).get("Free", 0)))
            price = dec(quote.bid)
        else:
            current = ProtectedState.from_mapping(self.store.account()["state"]).strategy
            if sleeve == "alt" and symbol not in current.alt_held and len(current.alt_held) >= 2:
                raise LocalSkip("failed exit left both alt slots occupied")
            if position and dec(position["quantity"]) > 0 and not position.get("dust"):
                raise LocalSkip("position already held")
            if self.cfg.live and (self.balance_at is None or
                    (self.api.now() - self.balance_at).total_seconds() > 35):
                raise LocalSkip("balance reconciliation is stale")
            target = sleeve_equity(ledger, sleeve, self.quotes) * dec(intent["entry_fraction_of_sleeve"])
            budget = min(target, dec(ledger[sleeve]["cash"]))
            if self.wallet:
                budget = min(budget, dec(self.wallet.get("USD", {}).get("Free", 0)))
            price = dec(quote.ask)
            quantity = budget / (price * (1 + dec(self.cfg.fee_reserve)))
        rule = self.rules[symbol]
        quantity = floor_quantity(quantity, rule["AmountPrecision"])
        if quantity <= 0 or quantity * price <= dec(rule["MiniOrder"]):
            raise LocalSkip("rounded quantity is below the exchange minimum")
        return format(quantity, "f")

    def _before_send(self, row, batch):
        now = self.api.now()
        intent = row["payload"]["intent"]
        symbol, quote = intent["symbol"], self.quotes[intent["symbol"]]
        if not quote.is_fresh(now, self.cfg.guard):
            raise LocalSkip("quote expired while waiting for API capacity")
        if intent["side"] == "BUY":
            if batch["id"].startswith("bar:") and (now - utc_time(batch["decision"]["decision_at"])).total_seconds() > 30:
                raise LocalSkip("entry decision is over 30 seconds old")
            if self.cfg.variant == "protected":
                state = ProtectedState.from_mapping(self.store.account()["state"])
                risk = on_tick(state, now, self.quotes, self.equity(), config=self.cfg.guard)
                if risk.intents or risk.next_state.paused_until or "cpi_event_window" in risk.alerts or self.equity() is None:
                    raise LocalSkip("risk guard changed before submission")
                reference = row["payload"].get("reference_close")
                if quote.spread_bps > self.cfg.guard.max_new_buy_spread_bps or (reference and
                        abs(quote.ask / float(reference) - 1) > self.cfg.guard.max_new_buy_quote_deviation):
                    raise LocalSkip("buy quote changed beyond guard limits")
        # The SUBMITTING state is durable before the HTTP request is made.
        self.store.update_order(row, "submitting", sent_at=int(now.timestamp() * 1000),
                                seen_ids=sorted(self.seen_ids | self.store.known_ids()))

    def _paper_fill(self, row):
        intent = row["payload"]["intent"]
        quote = self.quotes[intent["symbol"]]
        price = dec(row["payload"].get("paper_price", quote.ask if intent["side"] == "BUY" else quote.bid))
        quantity = dec(row["payload"]["quantity"])
        value = price * quantity
        return {"OrderID": "paper-" + hashlib.sha256(row["id"].encode()).hexdigest()[:24],
                "Pair": pair(intent["symbol"]), "Side": intent["side"], "Type": "MARKET",
                "Status": "FILLED", "Quantity": str(quantity), "FilledQuantity": str(quantity),
                "FilledAverPrice": str(price), "CoinChange": str(quantity), "UnitChange": str(value),
                "CommissionCoin": "USD", "CommissionChargeValue": str(value * dec(self.cfg.paper_fee)),
                "CreateTimestamp": int(self.api.now().timestamp() * 1000), "Simulated": True}

    def _find_ambiguous(self, row):
        if not self.cfg.live:
            # Deterministic paper executions can be reconstructed without network side effects.
            return self._paper_fill(row)
        matches = []
        payload, intent = row["payload"], row["payload"]["intent"]
        sent = payload.get("sent_at")
        if sent is None:
            raise Blocked("submission timestamp is missing")
        for detail in self.api.orders(limit="100"):
            stamp = int(detail.get("CreateTimestamp", 0))
            if (str(detail.get("OrderID")) not in set(payload.get("seen_ids", [])) and
                str(detail.get("OrderID")) not in self.store.known_ids() and
                detail.get("Pair") == pair(intent["symbol"]) and detail.get("Side") == intent["side"] and
                detail.get("Type") == "MARKET" and dec(detail.get("Quantity", 0)) == dec(payload["quantity"]) and
                sent - 2000 <= stamp <= sent + 120_000):
                matches.append(detail)
        if len(matches) != 1:
            raise Blocked(f"ambiguous submission: {len(matches)} matching orders; request will not be resent")
        self.log("order_recovered", internal_id=row["id"], order_id=matches[0]["OrderID"])
        return matches[0]

    def run_pending(self):
        batch = self.store.pending()
        if not batch:
            return True
        for original in self.store.orders(batch["id"]):
            row = original
            if row["status"] == "done" and not row["payload"].get("balance_verified"):
                self.validate_balance()
                self.store.update_order(row, "done", balance_verified=True)
            if row["status"] in ("done", "skipped", "rejected"):
                continue
            if row["status"] == "planned":
                delay = (utc_time(batch["decision"]["execute_not_before"]) - self.api.now()).total_seconds()
                if delay > 0:
                    self.sleep(delay)
                try:
                    quote = self.quotes.get(row["payload"]["intent"]["symbol"])
                    if quote is None or (self.api.now() - utc_time(quote.observed_at)).total_seconds() > 2.5:
                        self.refresh_quotes()
                    quantity = self.quantity(row)
                    q = self.quotes[row["payload"]["intent"]["symbol"]]
                    self.store.update_order(row, "planned", quantity=quantity,
                                            paper_price=q.ask if row["payload"]["intent"]["side"] == "BUY" else q.bid)
                    row = next(r for r in self.store.orders(batch["id"]) if r["id"] == row["id"])
                    if self.cfg.live:
                        detail = self.api.place(row["payload"]["intent"]["symbol"],
                                                row["payload"]["intent"]["side"], quantity,
                                                lambda: self._before_send(row, batch))
                    else:
                        self._before_send(row, batch)
                        detail = self._paper_fill(row)
                except LocalSkip as exc:
                    self.store.update_order(row, "skipped", error=str(exc))
                    self.log("order_skipped", internal_id=row["id"], reason=str(exc))
                    continue
                except AmbiguousOrder as exc:
                    # The journal is SUBMITTING, not a reusable planned order.
                    self.log("submission_ambiguous", internal_id=row["id"], error=str(exc))
                    raise Blocked(str(exc)) from None
                except APIError as exc:
                    # Only an explicit API Success=false is a definite order rejection.
                    # A failed public quote refresh happened before any order was sent.
                    self.store.update_order(row, "rejected", error=str(exc))
                    self.log("order_rejected", internal_id=row["id"], error=str(exc))
                    continue
                self.store.update_order(row, "known", order_id=detail["OrderID"], detail=detail)
                row = next(r for r in self.store.orders(batch["id"]) if r["id"] == row["id"])
            elif row["status"] == "submitting":
                detail = self._find_ambiguous(row)
                self.store.update_order(row, "known", order_id=detail["OrderID"], detail=detail)
                row = next(r for r in self.store.orders(batch["id"]) if r["id"] == row["id"])
            elif row["status"] == "known":
                if self.cfg.live:
                    matches = self.api.orders(order_id=row["order_id"])
                    if len(matches) != 1:
                        raise Blocked("known OrderID could not be queried uniquely")
                    detail = matches[0]
                else:
                    detail = row["payload"].get("detail") or self._paper_fill(row)
            else:
                raise Blocked(f"unrecognized journal status: {row['status']}")
            symbol = row["payload"]["intent"]["symbol"]
            try:
                terminal = self.store.apply_fill(row, detail, self.rules[symbol], self.quotes[symbol], iso(self.api.now()))
            except (KeyError, ValueError) as exc:
                raise Blocked(f"fill reconciliation failed: {exc}") from None
            self.seen_ids.add(str(detail["OrderID"]))
            self.log("order_observed", internal_id=row["id"], order_id=detail["OrderID"],
                     symbol=symbol, side=detail["Side"], status=detail["Status"],
                     filled=detail.get("FilledQuantity"), price=detail.get("FilledAverPrice"),
                     simulated=not self.cfg.live)
            if not terminal:
                fresh = self.store.order(row["id"])
                sent_at = fresh["payload"].get("sent_at", detail.get("CreateTimestamp", 0))
                if self.cfg.live and sent_at and int(self.api.now().timestamp() * 1000) - int(sent_at) >= 120_000 \
                        and not fresh["payload"].get("cancel_requested"):
                    self.store.update_order(fresh, "known", cancel_requested=True)
                    try:
                        self.api.cancel(detail["OrderID"])
                        self.log("pending_cancel_requested", order_id=detail["OrderID"])
                    except APIError as exc:
                        self.log("pending_cancel_unconfirmed", order_id=detail["OrderID"], error=str(exc))
                # Serialize the whole batch behind this potentially partially-filled order.
                raise Blocked(f"order {detail['OrderID']} is pending; no further order will be sent")
            self.validate_balance()
            self.store.update_order(self.store.order(row["id"]), "done", balance_verified=True)
        self.store.finish(batch["id"])
        return True
