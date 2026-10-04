from __future__ import annotations

import copy
import fcntl
import json
import sqlite3
from contextlib import contextmanager
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

from competition_strategy import BTC
from protected_strategy import ProtectedState

D = Decimal
ZERO = D("0")


def dec(value):
    number = D(str(value))
    if not number.is_finite():
        raise ValueError("nonfinite amount")
    return number


def floor_quantity(quantity, precision):
    return max(ZERO, dec(quantity)).quantize(D(1).scaleb(-int(precision)), rounding=ROUND_DOWN)


@contextmanager
def process_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another bot/check/warmup process owns this STATE_DIR") from None
        yield


def initial_ledger(cash):
    total = dec(cash)
    if total <= 0:
        raise ValueError("initial cash must be positive")
    return {"core": {"cash": str(total * D("0.2"))},
            "alt": {"cash": str(total * D("0.8"))}, "positions": {}}


def sleeve_equity(ledger, sleeve, quotes):
    total = dec(ledger[sleeve]["cash"])
    for symbol, position in ledger["positions"].items():
        if position["sleeve"] == sleeve and dec(position["quantity"]) > 0:
            if symbol not in quotes:
                raise ValueError(f"missing valuation quote: {symbol}")
            total += dec(position["quantity"]) * dec(quotes[symbol].bid)
    return total


def holdings_state(proposed: dict, previous: dict, ledger: dict) -> dict:
    """Reconcile the speculative engine state to actual non-dust holdings."""
    out = copy.deepcopy(proposed)
    base, prior = out["strategy"], previous["strategy"]
    positions = {s: p for s, p in ledger["positions"].items()
                 if dec(p["quantity"]) > 0 and not p.get("dust", False)}
    base["btc_held"] = BTC in positions
    if BTC in positions:
        base["btc_peak_close"] = base.get("btc_peak_close") or prior.get("btc_peak_close")
        if base["btc_peak_close"] is None:
            raise ValueError("BTC fill has no entry peak")
    else:
        base["btc_peak_close"] = None
    base["alt_held"] = sorted(s for s, p in positions.items() if p["sleeve"] == "alt")
    for name in ("alt_peak_close", "alt_entry_time", "alt_soft_fail_days"):
        base[name] = {s: base[name].get(s, prior[name].get(s)) for s in base["alt_held"]
                      if s in base[name] or s in prior[name]}
    # Engine's cooldown is retained after a partial stop, while the remaining
    # position stays held. A rejected exit cannot erase its peak/entry metadata.
    ProtectedState.from_mapping(out)
    return out


def reconcile_wallet(ledger, wallet, rules):
    expected = {"USD": dec(ledger["core"]["cash"]) + dec(ledger["alt"]["cash"])}
    tolerances = {"USD": D("0.02")}
    for symbol, position in ledger["positions"].items():
        asset = symbol[:-4]
        expected[asset] = expected.get(asset, ZERO) + dec(position["quantity"])
        tolerances[asset] = max(D("0.00000001"), D(1).scaleb(-int(rules[symbol]["AmountPrecision"])) / 2)
    mismatches = []
    for asset in set(wallet) | set(expected):
        row = wallet.get(asset, {})
        total = dec(row.get("Free", 0)) + dec(row.get("Lock", 0))
        if dec(row.get("Lock", 0)) > tolerances.get(asset, D("0.00000001")):
            mismatches.append(f"locked balance {asset}")
        if abs(total - expected.get(asset, ZERO)) > tolerances.get(asset, D("0.00000001")):
            mismatches.append(f"balance mismatch {asset}: expected={expected.get(asset, ZERO)} actual={total}")
    if mismatches:
        raise ValueError("; ".join(mismatches))


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS account (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT);
            CREATE TABLE IF NOT EXISTS batches (id TEXT PRIMARY KEY, payload TEXT, status TEXT);
            CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, batch_id TEXT, sequence INTEGER,
                payload TEXT, status TEXT, order_id TEXT UNIQUE);
            CREATE TABLE IF NOT EXISTS events (sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                time TEXT, kind TEXT, payload TEXT);
        """)
        self.db.commit()

    def account(self):
        row = self.db.execute("SELECT payload FROM account WHERE id=1").fetchone()
        return json.loads(row[0]) if row else None

    def initialize(self, fingerprint, cash):
        existing = self.account()
        if existing:
            if existing.get("version") != 1:
                raise ValueError("unsupported persisted account version")
            ProtectedState.from_mapping(existing["state"])
            if existing["fingerprint"] != fingerprint:
                raise ValueError("saved account/config fingerprint differs; restore matching .env or use separate STATE_DIR")
            return
        blob = {"version": 1, "fingerprint": fingerprint, "ledger": initial_ledger(cash),
                "state": ProtectedState().to_dict()}
        with self.db:
            self.db.execute("INSERT INTO account VALUES (1,?)", (json.dumps(blob),))

    def _save_account(self, blob):
        self.db.execute("UPDATE account SET payload=? WHERE id=1", (json.dumps(blob),))

    def event(self, when, kind, payload):
        with self.db:
            self.db.execute("INSERT INTO events(time,kind,payload) VALUES (?,?,?)",
                            (str(when), kind, json.dumps(payload, ensure_ascii=False)))

    def prepare(self, key, decision, when):
        if self.pending():
            raise RuntimeError("cannot create a batch before recovering the pending batch")
        if self.db.execute("SELECT 1 FROM batches WHERE id=?", (key,)).fetchone():
            return False
        payload = {"previous": self.account()["state"], "decision": decision.to_dict(), "created_at": when}
        with self.db:
            self.db.execute("INSERT INTO batches VALUES (?,?, 'pending')", (key, json.dumps(payload)))
            for i, intent in enumerate(payload["decision"]["intents"]):
                order = {"intent": intent, "metrics": {"filled": "0", "notional": "0", "fee": "0"}}
                self.db.execute("INSERT INTO orders VALUES (?,?,?,?, 'planned',NULL)",
                                (f"{key}:{i}", key, i, json.dumps(order)))
            self.db.execute("INSERT INTO events(time,kind,payload) VALUES (?, 'decision', ?)",
                            (when, json.dumps(payload["decision"])))
        return True

    def pending(self):
        row = self.db.execute("SELECT id,payload FROM batches WHERE status='pending' ORDER BY rowid LIMIT 1").fetchone()
        return {"id": row["id"], **json.loads(row["payload"])} if row else None

    def orders(self, batch_id):
        rows = self.db.execute("SELECT * FROM orders WHERE batch_id=? ORDER BY sequence", (batch_id,)).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def known_ids(self):
        return {row[0] for row in self.db.execute("SELECT order_id FROM orders WHERE order_id IS NOT NULL")}

    def order(self, key):
        row = self.db.execute("SELECT * FROM orders WHERE id=?", (key,)).fetchone()
        return {**dict(row), "payload": json.loads(row["payload"])}

    def update_order(self, row, status, **fields):
        payload = {**row["payload"], **fields}
        order_id = str(fields["order_id"]) if fields.get("order_id") is not None else row["order_id"]
        with self.db:
            self.db.execute("UPDATE orders SET payload=?,status=?,order_id=? WHERE id=?",
                            (json.dumps(payload), status, order_id, row["id"]))

    def apply_fill(self, row, detail, rules, quote, when):
        """Cumulative exchange fills -> delta cash/coins, exactly once in one transaction."""
        from .api import pair
        row = self.order(row["id"])
        intent = row["payload"]["intent"]
        symbol, sleeve, side = intent["symbol"], intent["sleeve"], intent["side"]
        if detail.get("Pair") != pair(symbol) or detail.get("Side") != side or detail.get("Type") != "MARKET":
            raise ValueError("order identity differs from journal")
        if row["order_id"] and str(detail["OrderID"]) != row["order_id"]:
            raise ValueError("OrderID differs from journal")
        filled = dec(detail.get("FilledQuantity", 0))
        notional = dec(detail.get("UnitChange", 0))
        if filled > 0 and notional == 0:
            notional = filled * dec(detail.get("FilledAverPrice", 0))
        fee = dec(detail.get("CommissionChargeValue", 0))
        coin = str(detail.get("CommissionCoin", "USD"))
        if coin not in ("USD", symbol[:-4]) and fee != 0:
            raise ValueError("unsupported fee currency")
        previous = row["payload"]["metrics"]
        if previous.get("fee_coin", coin) != coin and dec(previous["fee"]) != 0:
            raise ValueError("fee currency changed")
        current = {"filled": str(filled), "notional": str(notional), "fee": str(fee), "fee_coin": coin}
        delta = {k: dec(current[k]) - dec(previous[k]) for k in ("filled", "notional", "fee")}
        requested = dec(row["payload"]["quantity"])
        status = str(detail.get("Status", "")).upper()
        if any(v < 0 for v in delta.values()) or filled > requested or (filled > 0 and notional <= 0):
            raise ValueError("invalid or regressing cumulative fill")
        if status == "FILLED" and filled <= 0:
            raise ValueError("FILLED response has no fill")
        if status not in ("FILLED", "CANCELED", "CANCELLED", "REJECTED", "PENDING", "PARTIALLY_FILLED", "NEW"):
            raise ValueError(f"unrecognized order status: {status}")
        blob = self.account()
        ledger = blob["ledger"]
        position = ledger["positions"].setdefault(symbol, {"sleeve": sleeve, "quantity": "0", "dust": False})
        if position["sleeve"] != sleeve:
            raise ValueError("position belongs to the other sleeve")
        cash, quantity = dec(ledger[sleeve]["cash"]), dec(position["quantity"])
        direction = 1 if side == "BUY" else -1
        cash -= direction * delta["notional"]
        quantity += direction * delta["filled"]
        if coin == "USD":
            cash -= delta["fee"]
        else:
            quantity -= delta["fee"]
        if cash < D("-0.000001") or quantity < D("-0.00000001"):
            raise ValueError("fill exceeds its sleeve cash or owned quantity")
        ledger[sleeve]["cash"] = str(max(ZERO, cash))
        position["quantity"] = str(max(ZERO, quantity))
        tradable = floor_quantity(quantity, rules["AmountPrecision"])
        position["dust"] = bool(side == "SELL" and quantity > 0 and
                                (tradable == 0 or tradable * dec(quote.bid) <= dec(rules["MiniOrder"])))
        batch = self.pending()
        blob["state"] = holdings_state(batch["decision"]["next_state"], batch["previous"], ledger)
        terminal = status in ("FILLED", "CANCELED", "CANCELLED", "REJECTED")
        payload = {**row["payload"], "metrics": current, "detail": detail, "balance_verified": False}
        # Even an already-applied identical fill can safely be replayed.
        with self.db:
            self._save_account(blob)
            self.db.execute("UPDATE orders SET payload=?,status=?,order_id=? WHERE id=?",
                            (json.dumps(payload), "done" if terminal else "known", str(detail["OrderID"]), row["id"]))
            if any(delta.values()):
                event = {"internal_id": row["id"], "intent": intent, "detail": detail,
                         "delta": {k: str(v) for k, v in delta.items()}, "ledger": ledger, "state": blob["state"]}
                self.db.execute("INSERT INTO events(time,kind,payload) VALUES (?, 'fill', ?)",
                                (when, json.dumps(event)))
        return terminal

    def finish(self, key):
        if any(row["status"] not in ("done", "rejected", "skipped") for row in self.orders(key)):
            raise RuntimeError("unresolved order prevents batch completion")
        batch, blob = self.pending(), self.account()
        blob["state"] = holdings_state(batch["decision"]["next_state"], batch["previous"], blob["ledger"])
        with self.db:
            self._save_account(blob)
            self.db.execute("UPDATE batches SET status='done' WHERE id=?", (key,))

    def save_idle_tick(self, decision, when):
        if decision.intents or self.pending():
            raise RuntimeError("idle tick cannot contain orders or overlap a batch")
        blob = self.account()
        previous = blob["state"]
        proposed = holdings_state(decision.next_state.to_dict(), previous, blob["ledger"])
        if proposed == previous:
            return
        blob["state"] = proposed
        with self.db:
            self._save_account(blob)
            if proposed["paused_until"] != previous["paused_until"]:
                self.db.execute("INSERT INTO events(time,kind,payload) VALUES (?, 'guard_state', ?)",
                                (when, json.dumps(proposed)))

    def export_events(self, path):
        with Path(path).open("w") as stream:
            for row in self.db.execute("SELECT sequence,time,kind,payload FROM events ORDER BY sequence"):
                stream.write(json.dumps({**dict(row), "payload": json.loads(row["payload"])}, ensure_ascii=False) + "\n")
