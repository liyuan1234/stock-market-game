import asyncio
from collections import defaultdict
from contextlib import asynccontextmanager
import hashlib
import hmac
import heapq
import os
from pathlib import Path
import random
import secrets
import threading
import time
from typing import Optional

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import uvicorn

from db import (
    create_user,
    flush_db_batch,
    get_user,
    get_user_orders,
    init_db,
    record_order,
    save_user_state,
)
from main import CFG, Player, exchange, stock

_SIM_CFG = CFG.get("simulation", {})
_BOT_CFG = CFG.get("bot", {})
_PLAYER_CFG = CFG.get("player", {})
_SERVER_CFG = CFG.get("server", {})

STANDARD_INTERVALS = (1, 5, 10, 30, 60, 120, 300)
MAX_CANDLES = 240


def _load_session_secret() -> bytes:
    env_secret = os.environ.get("MARKET_SESSION_SECRET")
    if env_secret:
        return env_secret.encode("utf-8")
    secret_file = Path(__file__).with_name(".session_secret")
    if secret_file.exists():
        return secret_file.read_text(encoding="utf-8").strip().encode("utf-8")
    raw = secrets.token_hex(32)
    try:
        secret_file.write_text(raw, encoding="utf-8")
    except OSError:
        pass
    return raw.encode("utf-8")


_SESSION_SECRET = _load_session_secret()


def sign_user_cookie(user_id: int) -> str:
    payload = str(int(user_id))
    sig = hmac.new(_SESSION_SECRET, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_user_cookie(cookie_val: str | None) -> int | None:
    if not cookie_val or "." not in cookie_val:
        return None
    uid_part, sig_part = cookie_val.split(".", 1)
    if not uid_part.isdigit():
        return None
    expected = hmac.new(_SESSION_SECRET, uid_part.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig_part, expected):
        return None
    return int(uid_part)


class LiveSimulation:
    """Manages a step-by-step live market simulation with multi-user sessions."""

    def __init__(self, n=None, duration=None):
        self.lock = threading.RLock()
        self._db_lock = threading.Lock()
        self.initial_n = _SIM_CFG.get("initial_players", 50) if n is None else n
        self.n = self.initial_n
        self.duration = _SIM_CFG.get("duration", None) if duration is None else duration
        self.running = True
        self.speed = 1.0
        self.user_sessions: dict[int, dict] = {}
        self._player_to_uid: dict[Player, int] = {}
        self._ws_counts: dict[int, int] = defaultdict(int)
        self._state_version = 0
        self._shared_cache_ver = -1
        self._shared_cache: dict | None = None
        self._candle_cache: dict[tuple[int, int], list[dict]] = {}
        self._heap: list[tuple[float, int, Player]] = []
        self._heap_seq = 0
        self._candle_buckets: dict[int, dict[int, dict]] = {}
        self._last_close_by_iv: dict[int, dict[int, float]] = {}
        self._dirty_users: dict[int, tuple[float, int, float]] = {}
        self._dirty_orders: dict[int, tuple[int, float, str, float]] = {}
        self._pending_fills: dict[tuple[int, float], list] = {}
        self._pending_cancels: dict[int, float] = {}
        init_db()
        self.reset()

    def _flush_db(self):
        with self.lock:
            if not (self._dirty_users or self._dirty_orders or self._pending_fills or self._pending_cancels):
                return
            user_updates = [
                (round(c, 2), int(sh), round(cb, 2), uid)
                for uid, (c, sh, cb) in self._dirty_users.items()
            ]
            order_updates = [
                (int(fa), round(ep, 4), st, round(ub, 2), oid)
                for oid, (fa, ep, st, ub) in self._dirty_orders.items()
            ]
            fill_deltas = [
                (oid, row[0], row[1], row[2], row[3], price, row[4], row[5])
                for (oid, price), row in self._pending_fills.items()
            ]
            cancels = list(self._pending_cancels.items())
            self._dirty_users.clear()
            self._dirty_orders.clear()
            self._pending_fills.clear()
            self._pending_cancels.clear()

        with self._db_lock:
            flush_db_batch(user_updates, order_updates, fill_deltas, cancels)

    def _schedule_bot(self, bot: Player, base_t: float):
        if bot.order_freq <= 0:
            return  # order_freq 0: bot never places orders
        ev_t = base_t + random.expovariate(bot.order_freq * 2)
        heapq.heappush(self._heap, (ev_t, self._heap_seq, bot))
        self._heap_seq += 1

    def _init_candles(self):
        self._candle_buckets = {iv: {} for iv in STANDARD_INTERVALS}

    def _on_market_fill(self, tx: dict, bid: dict, ask: dict):
        # Incremental O(1) candle bucket updates across all standard timeframes
        price = tx["price"]
        qty = tx["qty"]
        t = tx["time"]
        for iv in STANDARD_INTERVALS:
            b = int(t // iv)
            bucket_map = self._candle_buckets[iv]
            c = bucket_map.get(b)
            if c is None:
                bucket_map[b] = {
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "volume": qty,
                }
                # Prune very old buckets beyond 2x MAX_CANDLES
                min_keep = b - MAX_CANDLES * 2
                if len(bucket_map) > MAX_CANDLES * 3:
                    for old_b in [k for k in bucket_map if k < min_keep]:
                        del bucket_map[old_b]
            else:
                if price > c["high"]:
                    c["high"] = price
                if price < c["low"]:
                    c["low"] = price
                c["close"] = price
                c["volume"] += qty

        # Queue fills & updated user balances in memory; flushed in a single batched DB transaction
        for order in (bid, ask):
            player = order.get("player")
            uid = self._player_to_uid.get(player) if player is not None else None
            if uid is not None:
                sess = self.user_sessions.get(uid)
                if sess:
                    self._dirty_users[uid] = (
                        player.cash,
                        player.get_qty(self.stock),
                        sess["cost_basis"],
                    )
                db_oid = order.get("db_order_id")
                if db_oid is not None and order["filled_qty"] > 0:
                    avg_exec = order["fill_notional"] / order["filled_qty"]
                    status = "filled" if order["amount"] == 0 else "partial"
                    self._dirty_orders[db_oid] = (
                        order["filled_qty"],
                        avg_exec,
                        status,
                        player.cash,
                    )
                    p_key = round(tx["price"], 2)
                    fkey = (db_oid, p_key)
                    prev = self._pending_fills.get(fkey)
                    if prev is None:
                        self._pending_fills[fkey] = [
                            uid,
                            self.stock.name,
                            order["side"],
                            tx["time"],
                            int(tx["qty"]),
                            round(player.cash, 2),
                        ]
                    else:
                        prev[3] = tx["time"]
                        prev[4] += int(tx["qty"])
                        prev[5] = round(player.cash, 2)
                    if sess:
                        sess["orders_version"] = sess.get("orders_version", 1) + 1

    def _on_market_cancel(self, order: dict):
        player = order.get("player")
        db_oid = order.get("db_order_id")
        if player is not None and db_oid is not None:
            self._pending_cancels[db_oid] = player.cash
            uid = self._player_to_uid.get(player)
            if uid is not None and uid in self.user_sessions:
                self.user_sessions[uid]["orders_version"] = self.user_sessions[uid].get("orders_version", 1) + 1

    def reset(self):
        with self.lock:
            if hasattr(self, "market") and self.market is not None:
                for resting in list(self.market.bids) + list(self.market.asks) + list(self.market.active_market_orders):
                    if resting.get("db_order_id") is not None:
                        self._on_market_cancel(resting)
            self.n = self.initial_n
            bot_shares = _BOT_CFG.get("initial_shares", 50)
            player_shares = _PLAYER_CFG.get("initial_shares", 0)
            self.market = exchange(
                on_fill=self._on_market_fill,
                on_cancel=self._on_market_cancel,
                max_matches_per_tick=40,
            )
            self.stock = stock()
            self.players = [
                Player(id=i + 1, stock=[{"name": self.stock.name, "qty": bot_shares}], is_bot=True)
                for i in range(self.n)
            ]
            self._heap = []
            self._heap_seq = 0
            for p in self.players:
                self._schedule_bot(p, 0.0)

            self.t = 0.0
            self._next_bot_id = self.n + 1
            self.market.history[self.stock.name] = [(0.0, self.stock.price)]
            self._init_candles()

            # Reset all active user sessions while preserving username & initial_cash
            self._player_to_uid.clear()
            allow_short = _PLAYER_CFG.get("allow_short_selling", True)
            for uid, sess in self.user_sessions.items():
                init_cash = sess["initial_cash"]
                p = Player(
                    id=sess["username"],
                    cash=init_cash,
                    stock=[{"name": self.stock.name, "qty": player_shares}],
                    is_bot=False,
                    allow_short_selling=allow_short,
                )
                cost_basis = round(init_cash + player_shares * self.stock.price, 2)
                sess["player"] = p
                sess["cost_basis"] = cost_basis
                self._player_to_uid[p] = uid
                self._dirty_users[uid] = (init_cash, player_shares, cost_basis)

            total_issued = self.n * bot_shares + len(self.user_sessions) * player_shares
            self.stock.numberOfStock = max(self.stock.numberOfStock, total_issued)
            self._state_version += 1
        self._flush_db()

    def connect_ws(self, user_id: int):
        with self.lock:
            self._ws_counts[user_id] = self._ws_counts.get(user_id, 0) + 1
            self._state_version += 1

    def disconnect_ws(self, user_id: int):
        with self.lock:
            cur = self._ws_counts.get(user_id, 0)
            if cur <= 1:
                self._ws_counts.pop(user_id, None)
            else:
                self._ws_counts[user_id] = cur - 1
            self._state_version += 1

    def reset_players(self):
        with self.lock:
            old_bots = set(self.players)
            self.market.cancel_player_orders(old_bots)
            for b in old_bots:
                b._active = False

            # Remove all bots; human players are untouched.
            self.n = 0
            self.players = []
            self._heap = []
            self._heap_seq = 0
            self._next_bot_id = 1
            self._state_version += 1
            return self.n

    def step(self, dt=0.25):
        with self.lock:
            if not self.running:
                return
            if self.duration is not None and self.t >= self.duration:
                self.running = False
                return

            target_t = self.t + dt * self.speed
            if self.duration is not None:
                target_t = min(self.duration, target_t)

            while self._heap and self._heap[0][0] <= target_t:
                ev_t, _, p = heapq.heappop(self._heap)
                if not p._active:
                    continue
                p.act(self.market, self.stock, t=ev_t)
                if p._active:
                    self._schedule_bot(p, ev_t)

            # Dynamically advance any large chunked market or crossed limit orders
            self.market.process_pending(self.stock, target_t)

            self.t = target_t
            if self.duration is not None and self.t >= self.duration:
                self.market.history[self.stock.name].append((self.duration, self.stock.price))
                self.running = False
            self._state_version += 1
        self._flush_db()

    def login_or_restore_user(self, username: str, initial_cash: float) -> dict:
        with self.lock:
            init_shares = _PLAYER_CFG.get("initial_shares", 0)
            with self._db_lock:
                user = create_user(
                    username=username,
                    initial_cash=initial_cash,
                    initial_shares=init_shares,
                    stock_price=self.stock.price,
                )
            uid = user["id"]
            if uid not in self.user_sessions:
                p = Player(
                    id=user["username"],
                    cash=user["cash"],
                    stock=[{"name": self.stock.name, "qty": user["shares"]}],
                    is_bot=False,
                    allow_short_selling=_PLAYER_CFG.get("allow_short_selling", True),
                )
                self.user_sessions[uid] = {
                    "user_id": uid,
                    "username": user["username"],
                    "initial_cash": user["initial_cash"],
                    "cost_basis": user["cost_basis"],
                    "player": p,
                    "orders_version": 1,
                }
                self._player_to_uid[p] = uid
                if init_shares > 0:
                    self.stock.numberOfStock += init_shares
                self._state_version += 1
            return user

    def ensure_user_session(self, user_id: int) -> dict | None:
        with self.lock:
            sess = self.user_sessions.get(user_id)
            if sess is not None:
                return sess
            with self._db_lock:
                user = get_user(user_id)
            if user is None:
                return None
            init_shares = _PLAYER_CFG.get("initial_shares", 0)
            cash = user["cash"] if user["cash"] is not None else user["initial_cash"]
            shares = user["shares"] if user["shares"] is not None else init_shares
            cost_basis = (
                user["cost_basis"]
                if user["cost_basis"] is not None
                else round(user["initial_cash"] + shares * self.stock.price, 2)
            )
            p = Player(
                id=user["username"],
                cash=cash,
                stock=[{"name": self.stock.name, "qty": shares}],
                is_bot=False,
                allow_short_selling=_PLAYER_CFG.get("allow_short_selling", True),
            )
            sess = {
                "user_id": user_id,
                "username": user["username"],
                "initial_cash": user["initial_cash"],
                "cost_basis": cost_basis,
                "player": p,
                "orders_version": 1,
            }
            self.user_sessions[user_id] = sess
            self._player_to_uid[p] = user_id
            self._state_version += 1
            return sess

    def submit_manual_order(self, user_id: int, side: str, price: float | None, amount: int, order_type: str = "limit"):
        with self.lock:
            sess = self.ensure_user_session(user_id)
            if sess is None:
                return False, "User session not found."
            player: Player = sess["player"]
            player.allow_short_selling = bool(_PLAYER_CFG.get("allow_short_selling", True))

            side = side.lower().strip()
            order_type = order_type.lower().strip()
            if side not in ("buy", "sell"):
                return False, "Invalid side."
            if order_type not in ("limit", "market"):
                return False, "Invalid order type."
            if amount <= 0:
                return False, "Amount must be a positive integer."

            if order_type == "market":
                ok, msg, order = self.market.submitMarketOrder(
                    self.stock,
                    side=side,
                    amount=amount,
                    player=player,
                    t=self.t,
                )
                if not ok or order is None:
                    return False, msg
                filled = order["filled_qty"]
                avg_exec = round(order["fill_notional"] / filled, 4) if filled > 0 else round(self.stock.price, 4)
                is_working = bool(order.get("is_active_market"))
                status = "partial" if is_working else ("filled" if filled == amount else "partial_cancelled")
                with self._db_lock:
                    db_oid = record_order(
                        userid=user_id,
                        stock_name=self.stock.name,
                        side=side,
                        time=round(self.t, 4),
                        order_price=round(avg_exec, 2),
                        order_amount=amount,
                        user_balance=round(player.cash, 2),
                        order_type="market",
                        filled_amount=filled,
                        exec_price=avg_exec,
                        status=status,
                    )
                order["db_order_id"] = db_oid
                for fp, (ft, fq) in order.get("fills_by_price", {}).items():
                    self._pending_fills[(db_oid, fp)] = [
                        user_id,
                        self.stock.name,
                        side,
                        ft,
                        int(fq),
                        round(player.cash, 2),
                    ]
                self._dirty_users[user_id] = (
                    player.cash,
                    player.get_qty(self.stock),
                    sess["cost_basis"],
                )
                sess["orders_version"] = sess.get("orders_version", 1) + 1
            else:
                # Limit order
                if price is None or price <= 0:
                    return False, "Price must be positive for limit orders."
                price = round(float(price), 2)

                if side == "buy":
                    ok, msg, order = player.buy(self.market, self.stock, price, amount, t=self.t)
                else:
                    ok, msg, order = player.sell(self.market, self.stock, price, amount, t=self.t)

                if not ok or order is None:
                    return False, msg

                filled = order["filled_qty"]
                avg_exec = round(order["fill_notional"] / filled, 4) if filled > 0 else 0.0
                status = "filled" if order["amount"] == 0 else ("partial" if filled > 0 else "open")
                with self._db_lock:
                    db_oid = record_order(
                        userid=user_id,
                        stock_name=self.stock.name,
                        side=side,
                        time=round(self.t, 4),
                        order_price=price,
                        order_amount=amount,
                        user_balance=round(player.cash, 2),
                        order_type="limit",
                        filled_amount=filled,
                        exec_price=avg_exec,
                        status=status,
                    )
                order["db_order_id"] = db_oid
                for fp, (ft, fq) in order.get("fills_by_price", {}).items():
                    self._pending_fills[(db_oid, fp)] = [
                        user_id,
                        self.stock.name,
                        side,
                        ft,
                        int(fq),
                        round(player.cash, 2),
                    ]
                self._dirty_users[user_id] = (
                    player.cash,
                    player.get_qty(self.stock),
                    sess["cost_basis"],
                )
                sess["orders_version"] = sess.get("orders_version", 1) + 1
                if filled > 0:
                    if order["amount"] > 0:
                        msg = f"{side.capitalize()} limit filling dynamically ({filled}/{amount} filled so far @ ${avg_exec:.2f})."
                    else:
                        msg = f"{side.capitalize()} limit filled {filled}/{amount} @ ${avg_exec:.2f}."
            self._state_version += 1

        self._flush_db()
        return True, msg

    def deposit(self, user_id: int, amount: float = 1000000.0):
        with self.lock:
            if amount <= 0:
                return False, 0.0, "Deposit amount must be positive."
            sess = self.ensure_user_session(user_id)
            if sess is None:
                return False, 0.0, "User session not found."
            player: Player = sess["player"]
            player.cash = round(player.cash + amount, 2)
            sess["cost_basis"] = round(sess["cost_basis"] + amount, 2)
            cash_now = player.cash
            shares_now = player.get_qty(self.stock)
            cb_now = sess["cost_basis"]
            uname = sess["username"]
            self._state_version += 1
        with self._db_lock:
            save_user_state(user_id, cash=cash_now, shares=shares_now, cost_basis=cb_now)
        return True, cash_now, f"Added ${amount:,.0f} to {uname}."

    def cancel_order(self, user_id: int, order_id: int):
        with self.lock:
            sess = self.ensure_user_session(user_id)
            if sess is None:
                return False, "User session not found."
            ok, _ = self.market.cancel_order(order_id, player=sess["player"])
            if ok:
                self._state_version += 1
        self._flush_db()
        if ok:
            return True, f"Cancelled order #{order_id}."
        return False, "Order not found."

    def add_players(self, count=10, bias="random", order_freq=None):
        with self.lock:
            if not _PLAYER_CFG.get("allow_adding_bots", True):
                return False, self.n, "Adding bots is disabled in config."
            max_players = _SIM_CFG.get("max_players", 3000)
            if count <= 0 or max_players <= 0:
                return True, self.n, f"Players: {self.n}"
            count = min(count, max_players)
            overflow = (len(self.players) + count) - max_players
            if overflow > 0:
                evicted = set(self.players[:overflow])
                for b in evicted:
                    b._active = False
                self.market.cancel_player_orders(evicted)
                self.players = self.players[overflow:]

            curr = self.stock.price
            bot_shares = _BOT_CFG.get("initial_shares", 50)
            for _ in range(count):
                if bias == "bull":
                    strategy = {
                        "target_price": round(curr + random.uniform(2.0, 10.0), 2),
                        "type": "buy_below",
                    }
                elif bias == "bear":
                    strategy = {
                        "target_price": round(max(0.01, curr - random.uniform(2.0, 10.0)), 2),
                        "type": "sell_above",
                    }
                else:
                    strategy = {
                        "target_price": round(max(0.01, curr + random.uniform(-5.0, 5.0)), 2),
                        "type": random.choice(["buy_below", "sell_above", "buy_sell"]),
                    }
                p = Player(
                    id=self._next_bot_id,
                    stock=[{"name": self.stock.name, "qty": bot_shares}],
                    strategy=strategy,
                    order_freq=order_freq,
                    is_bot=True,
                )
                self._next_bot_id += 1
                self.players.append(p)
                self._schedule_bot(p, self.t)

            if overflow < count:
                self.stock.numberOfStock += (count - max(0, overflow)) * bot_shares
            self.n = len(self.players)
            self._state_version += 1
            return True, self.n, f"Added {count} {bias} players."

    def inject_cash(self, amount):
        with self.lock:
            if amount <= 0:
                return False, "Amount must be positive."
            for p in self.players:
                p.cash = round(p.cash + amount, 2)
            for uid, sess in self.user_sessions.items():
                up = sess["player"]
                up.cash = round(up.cash + amount, 2)
                sess["cost_basis"] = round(sess["cost_basis"] + amount, 2)
                self._dirty_users[uid] = (up.cash, up.get_qty(self.stock), sess["cost_basis"])
            self._state_version += 1
        self._flush_db()
        return True, f"Injected ${amount:,.2f} to all players."

    def inject_stock(self, qty=10):
        with self.lock:
            if qty <= 0:
                return False, "Quantity must be positive."
            for p in self.players:
                p.add_stock(self.stock, qty)
            for uid, sess in self.user_sessions.items():
                up = sess["player"]
                up.add_stock(self.stock, qty)
                sess["cost_basis"] = round(sess["cost_basis"] + qty * self.stock.price, 2)
                self._dirty_users[uid] = (up.cash, up.get_qty(self.stock), sess["cost_basis"])
            total_recipients = len(self.players) + len(self.user_sessions)
            self.stock.numberOfStock += qty * total_recipients
            self._state_version += 1
        self._flush_db()
        return True, f"Added {qty} {self.stock.name} shares to all players."

    def _build_candles(self, interval=60.0):
        iv_int = int(round(max(1.0, float(interval))))
        init_price = self.market.history.get(self.stock.name, [(0.0, 100.0)])[0][1]
        max_bucket = int(self.t // iv_int)
        start_bucket = max(0, max_bucket - MAX_CANDLES + 1)

        if iv_int in self._candle_buckets:
            bucket_map = self._candle_buckets[iv_int]
        else:
            bucket_map = {}
            for tx in self.market.transactions:
                b = int(tx["time"] // iv_int)
                c = bucket_map.get(b)
                p = tx["price"]
                if c is None:
                    bucket_map[b] = {"open": p, "high": p, "low": p, "close": p, "volume": tx["qty"]}
                else:
                    c["high"] = max(c["high"], p)
                    c["low"] = min(c["low"], p)
                    c["close"] = p
                    c["volume"] += tx["qty"]

        prev_close = init_price
        if start_bucket > 0 and bucket_map:
            prior_keys = [k for k in bucket_map if k < start_bucket]
            if prior_keys:
                prev_close = bucket_map[max(prior_keys)]["close"]

        candles = []
        for b in range(start_bucket, max_bucket + 1):
            c = bucket_map.get(b)
            if c is not None:
                o, h, l, cl, v = c["open"], c["high"], c["low"], c["close"], c["volume"]
                prev_close = cl
            else:
                o = h = l = cl = prev_close
                v = 0
            candles.append({
                "time": b * iv_int,
                "open": round(o, 2),
                "high": round(h, 2),
                "low": round(l, 2),
                "close": round(cl, 2),
                "volume": v,
            })
        return candles

    def _get_shared_snapshot(self) -> dict:
        if self._shared_cache_ver == self._state_version and self._shared_cache is not None:
            return self._shared_cache

        price = self.stock.price
        top_asks = [
            {
                "id": a["id"],
                "price": a["price"],
                "amount": a["amount"],
                "player": a["player"],
                "trader": a["player"].id if (a["player"] is not None and not a["player"].is_bot) else None,
            }
            for a in self.market.asks[:10]
        ]
        top_bids = [
            {
                "id": b["id"],
                "price": b["price"],
                "amount": b["amount"],
                "player": b["player"],
                "trader": b["player"].id if (b["player"] is not None and not b["player"].is_bot) else None,
            }
            for b in self.market.bids[:10]
        ]
        total_ask_qty = sum(a["amount"] for a in self.market.asks)
        total_bid_qty = sum(b["amount"] for b in self.market.bids)

        ask_levels = defaultdict(int)
        for a in self.market.asks:
            ask_levels[a["price"]] += a["amount"]
        bid_levels = defaultdict(int)
        for b in self.market.bids:
            bid_levels[b["price"]] += b["amount"]

        txs = list(reversed(self.market.transactions[-15:]))
        leaderboard_raw = []
        breakdown = {"bull": 0, "bear": 0, "both": 0}
        bot_init_basis = (
            _BOT_CFG.get("initial_cash", 1000000.0)
            + _BOT_CFG.get("initial_shares", 50) * CFG.get("stock", {}).get("initial_price", 100.0)
        )
        for p in self.players:
            stype = p.strategy["type"]
            if stype == "buy_below":
                breakdown["bull"] += 1
            elif stype == "sell_above":
                breakdown["bear"] += 1
            else:
                breakdown["both"] += 1
            qty = p.get_qty(self.stock)
            val = round(p.cash + qty * price, 2)
            leaderboard_raw.append({
                "uid": None,
                "id": p.id,
                "is_bot": True,
                "is_online": False,
                "strategy": stype,
                "target": p.strategy["target_price"],
                "order_freq": round(p.order_freq, 3),
                "cash": round(p.cash, 2),
                "qty": qty,
                "value": val,
                "pnl": round(val - bot_init_basis, 2),
            })

        online_users = []
        for uid, u_sess in self.user_sessions.items():
            up = u_sess["player"]
            u_qty = up.get_qty(self.stock)
            u_cash = round(up.cash, 2)
            u_val = round(up.cash + u_qty * price, 2)
            u_basis = u_sess["cost_basis"]
            u_pnl = round(u_val - u_basis, 2)
            is_online = self._ws_counts.get(uid, 0) > 0
            if is_online:
                online_users.append(u_sess["username"])
            leaderboard_raw.append({
                "uid": uid,
                "id": u_sess["username"],
                "is_bot": False,
                "is_online": is_online,
                "strategy": "manual",
                "target": None,
                "order_freq": None,
                "cash": u_cash,
                "qty": u_qty,
                "value": u_val,
                "pnl": u_pnl,
            })

        leaderboard_raw.sort(key=lambda x: (-x["pnl"], 0 if not x["is_bot"] else 1, str(x["id"])))
        for idx, entry in enumerate(leaderboard_raw, start=1):
            entry["rank"] = idx

        self._candle_cache.clear()
        self._shared_cache = {
            "price": price,
            "top_asks": top_asks,
            "top_bids": top_bids,
            "total_ask_qty": total_ask_qty,
            "total_bid_qty": total_bid_qty,
            "all_asks": [[p, q] for p, q in list(ask_levels.items())[:200]],
            "all_bids": [[p, q] for p, q in list(bid_levels.items())[:200]],
            "txs": txs,
            "breakdown": breakdown,
            "leaderboard_raw": leaderboard_raw,
            "online_users": online_users,
        }
        self._shared_cache_ver = self._state_version
        return self._shared_cache

    def _get_cached_candles(self, interval=60.0) -> list[dict]:
        iv_int = int(round(max(1.0, float(interval))))
        key = (self._state_version, iv_int)
        cached = self._candle_cache.get(key)
        if cached is None:
            cached = self._build_candles(interval=iv_int)
            self._candle_cache[key] = cached
        return cached

    def get_state(self, user_id: int | None = None, interval=60.0):
        with self.lock:
            sess = self.ensure_user_session(user_id) if user_id is not None else None
            manual_player = sess["player"] if sess else None
            username = sess["username"] if sess else "YOU"
            cost_basis = sess["cost_basis"] if sess else 0.0

            shared = self._get_shared_snapshot()
            price = shared["price"]
            candles = self._get_cached_candles(interval=interval)

            asks = [
                {
                    "id": a["id"],
                    "price": a["price"],
                    "amount": a["amount"],
                    "is_you": manual_player is not None and a["player"] is manual_player,
                    "trader": a["trader"],
                }
                for a in shared["top_asks"]
            ]
            bids = [
                {
                    "id": b["id"],
                    "price": b["price"],
                    "amount": b["amount"],
                    "is_you": manual_player is not None and b["player"] is manual_player,
                    "trader": b["trader"],
                }
                for b in shared["top_bids"]
            ]

            your_orders = []
            if manual_player is not None:
                your_orders = (
                    [
                        {
                            "id": m["id"],
                            "side": m["side"],
                            "price": round(m["fill_notional"] / m["filled_qty"], 2) if m["filled_qty"] > 0 else round(price, 2),
                            "amount": m["amount"],
                        }
                        for m in self.market.active_market_orders
                        if m["player"] is manual_player
                    ]
                    + [
                        {"id": a["id"], "side": "ask", "price": a["price"], "amount": a["amount"]}
                        for a in self.market.asks
                        if a["player"] is manual_player
                    ]
                    + [
                        {"id": b["id"], "side": "bid", "price": b["price"], "amount": b["amount"]}
                        for b in self.market.bids
                        if b["player"] is manual_player
                    ]
                )

            if manual_player is not None:
                you_qty = manual_player.get_qty(self.stock)
                you_cash = round(manual_player.cash, 2)
                you_avail_cash = round(manual_player.available_cash(self.stock, price), 2)
                you_reserved_cash = round(manual_player.reserved_cash, 2)
                you_val = round(manual_player.cash + you_qty * price, 2)
                you_pnl = round(you_val - cost_basis, 2)
                you_pnl_pct = round((you_pnl / cost_basis) * 100, 2) if cost_basis else 0.0
            else:
                you_qty = 0
                you_cash = 0.0
                you_avail_cash = 0.0
                you_reserved_cash = 0.0
                you_val = 0.0
                you_pnl = 0.0
                you_pnl_pct = 0.0

            # Include top 10 overall plus all active/online human traders so concurrent users always see each other
            selected_rows = []
            seen_ranks = set()
            for row in shared["leaderboard_raw"][:10]:
                seen_ranks.add(row["rank"])
                selected_rows.append(row)
            for row in shared["leaderboard_raw"][10:]:
                if not row["is_bot"] and (row["uid"] == user_id or row["is_online"] or len(self.user_sessions) <= 15):
                    if row["rank"] not in seen_ranks:
                        seen_ranks.add(row["rank"])
                        selected_rows.append(row)

            leaderboard = [
                {
                    "rank": r["rank"],
                    "id": r["id"],
                    "is_you": (user_id is not None and r["uid"] == user_id),
                    "is_bot": r["is_bot"],
                    "is_online": r["is_online"],
                    "strategy": r["strategy"],
                    "target": r["target"],
                    "order_freq": r["order_freq"],
                    "cash": r["cash"],
                    "qty": r["qty"],
                    "value": r["value"],
                    "pnl": r["pnl"],
                }
                for r in selected_rows
            ]

            return {
                "symbol": self.stock.name,
                "price": round(price, 2),
                "total_shares": self.stock.numberOfStock,
                "market_cap": self.stock.market_cap,
                "time": round(self.t, 2),
                "duration": self.duration,
                "running": self.running,
                "speed": self.speed,
                "allow_adding_bots": bool(_PLAYER_CFG.get("allow_adding_bots", True)),
                "player_count": self.n,
                "human_count": len(self.user_sessions),
                "online_count": len(shared["online_users"]),
                "online_users": shared["online_users"],
                "player_breakdown": shared["breakdown"],
                "tx_count": self.market.total_tx_count,
                "candles": candles,
                "asks": list(reversed(asks)),
                "bids": bids,
                "all_asks": shared["all_asks"],
                "all_bids": shared["all_bids"],
                "total_ask_qty": shared["total_ask_qty"],
                "total_bid_qty": shared["total_bid_qty"],
                "your_orders": your_orders,
                "transactions": shared["txs"],
                "my_tx_version": sess.get("orders_version", 1) if sess else 0,
                "leaderboard": leaderboard,
                "you": {
                    "username": username,
                    "cash": you_cash,
                    "available_cash": you_avail_cash,
                    "reserved_cash": you_reserved_cash,
                    "qty": you_qty,
                    "value": you_val,
                    "pnl": you_pnl,
                    "pnl_pct": you_pnl_pct,
                },
            }


sim = LiveSimulation()


def _extract_bearer(authorization: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def resolve_user_id(
    userid: str | None = Cookie(default=None),
    x_session_token: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> int | None:
    explicit_token = x_session_token or _extract_bearer(authorization)
    if explicit_token:
        uid = verify_user_cookie(explicit_token)
        if uid is None or sim.ensure_user_session(uid) is None:
            return None
        return uid
    uid = verify_user_cookie(userid)
    if uid is None or sim.ensure_user_session(uid) is None:
        return None
    return uid


def require_user_id(uid: int | None = Depends(resolve_user_id)) -> int:
    if uid is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return uid


@asynccontextmanager
async def lifespan(app: FastAPI):
    stop_event = threading.Event()
    tick_dt = _SIM_CFG.get("tick_dt", 0.05)

    def ticker():
        while not stop_event.is_set():
            sim.step(dt=tick_dt)
            time.sleep(tick_dt)

    thread = threading.Thread(target=ticker, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop_event.set()
        thread.join(timeout=1.0)


app = FastAPI(title="AAPL Live Market Dashboard", lifespan=lifespan)

# --- Cross-origin support (static frontend on Vercel -> backend here) ------
# ALLOWED_ORIGINS is a comma-separated list of frontend origins, e.g.
#   ALLOWED_ORIGINS=https://stock-market-game-deploy.vercel.app
# An explicit list is required because CORS forbids "*" together with
# allow_credentials=True.
_origins_env = os.getenv("ALLOWED_ORIGINS", "").strip()
ALLOWED_ORIGINS = [o.strip().rstrip("/") for o in _origins_env.split(",") if o.strip()]
if ALLOWED_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# When the frontend is served from a different site (vercel.app) than the
# API (trycloudflare.com), the session cookie must be SameSite=None; Secure
# or the browser silently drops it. Left off by default so same-origin
# behaviour is unchanged.
_COOKIE_CROSS_SITE = os.getenv("CROSS_SITE_COOKIES", "0").strip() == "1"


class SpeedReq(BaseModel):
    speed: float = 1.0


class OrderReq(BaseModel):
    side: str
    price: float | None = None
    amount: int
    order_type: str = "limit"


class DepositReq(BaseModel):
    amount: float = 1000000.0


class AddPlayersReq(BaseModel):
    count: int = 10
    bias: str = "random"
    order_freq: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class CancelReq(BaseModel):
    order_id: int


class InjectReq(BaseModel):
    amount: float


class InjectStockReq(BaseModel):
    qty: int = 10


class LoginReq(BaseModel):
    username: str
    initial_amount: float = 1000000.0


@app.get("/login", response_class=HTMLResponse)
def login_page():
    html = Path(__file__).with_name("login.html").read_text(encoding="utf-8")
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/api/config")
def get_config():
    return {"sliders": CFG.get("sliders", {})}


@app.post("/api/login")
def login(req: LoginReq):
    username = req.username.strip()
    if not username:
        return JSONResponse({"ok": False, "message": "Username is required."}, status_code=400)
    if len(username) > 32:
        return JSONResponse({"ok": False, "message": "Username must be 32 characters or fewer."}, status_code=400)
    if req.initial_amount <= 0:
        return JSONResponse({"ok": False, "message": "Initial amount must be positive."}, status_code=400)
    user = sim.login_or_restore_user(username, req.initial_amount)
    token = sign_user_cookie(user["id"])
    resp = JSONResponse({"ok": True, "user": user, "token": token})
    resp.set_cookie(
        key="userid",
        value=token,
        httponly=True,
        samesite="none" if _COOKIE_CROSS_SITE else "lax",
        secure=_COOKIE_CROSS_SITE,
        path="/",
    )
    return resp


@app.get("/api/session")
def get_session_info(uid: int = Depends(require_user_id)):
    sess = sim.ensure_user_session(uid)
    token = sign_user_cookie(uid)
    resp = JSONResponse({
        "ok": True,
        "user_id": uid,
        "username": sess["username"] if sess else "",
        "token": token,
    })
    resp.set_cookie(
        key="userid",
        value=token,
        httponly=True,
        samesite="none" if _COOKIE_CROSS_SITE else "lax",
        secure=_COOKIE_CROSS_SITE,
        path="/",
    )
    return resp


@app.post("/api/logout")
def logout(
    userid: str | None = Cookie(default=None),
    x_session_token: str | None = Header(default=None),
):
    resp = JSONResponse({"ok": True})
    # Only clear shared browser cookie if no per-tab token was used or it matches the cookie owner
    if not x_session_token or verify_user_cookie(userid) == verify_user_cookie(x_session_token):
        resp.delete_cookie("userid", path="/")
    return resp


@app.get("/", response_class=HTMLResponse)
def index(userid: str | None = Cookie(default=None)):
    uid = verify_user_cookie(userid)
    if uid is None or sim.ensure_user_session(uid) is None:
        return RedirectResponse(url="/login?next=1", status_code=302)
    html = Path(__file__).with_name("index.html").read_text(encoding="utf-8")
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/api/state")
def get_state(interval: float = 60.0, uid: int = Depends(require_user_id)):
    return sim.get_state(user_id=uid, interval=interval)


@app.get("/api/my_transactions")
def get_my_transactions(uid: int = Depends(require_user_id)):
    with sim._db_lock:
        txs = get_user_orders(uid)
    return {"ok": True, "transactions": txs}


@app.post("/api/start")
def start_sim(uid: int = Depends(require_user_id)):
    sim.running = True
    return {"ok": True}


@app.post("/api/pause")
def pause_sim(uid: int = Depends(require_user_id)):
    sim.running = False
    return {"ok": True}


@app.post("/api/reset")
def reset_sim(uid: int = Depends(require_user_id)):
    sim.reset()
    sim.running = True
    return {"ok": True}


@app.post("/api/speed")
def set_speed(req: SpeedReq, uid: int = Depends(require_user_id)):
    sim.speed = max(0.25, min(20.0, req.speed))
    return {"ok": True}


@app.post("/api/order")
def place_order(req: OrderReq, uid: int = Depends(require_user_id)):
    ok, msg = sim.submit_manual_order(
        user_id=uid,
        side=req.side,
        price=req.price,
        amount=req.amount,
        order_type=req.order_type,
    )
    return JSONResponse({"ok": ok, "message": msg}, status_code=200 if ok else 400)


@app.post("/api/deposit")
def deposit(req: DepositReq, uid: int = Depends(require_user_id)):
    ok, cash, msg = sim.deposit(uid, req.amount)
    return JSONResponse({"ok": ok, "cash": cash, "message": msg}, status_code=200 if ok else 400)


@app.post("/api/add_players")
def add_players(req: AddPlayersReq, uid: int = Depends(require_user_id)):
    ok, total, msg = sim.add_players(req.count, bias=req.bias, order_freq=req.order_freq)
    return JSONResponse(
        {"ok": ok, "total_players": total, "message": msg},
        status_code=200 if ok else 403,
    )


@app.post("/api/cancel")
def cancel_order(req: CancelReq, uid: int = Depends(require_user_id)):
    ok, msg = sim.cancel_order(uid, req.order_id)
    return JSONResponse({"ok": ok, "message": msg}, status_code=200 if ok else 404)


@app.post("/api/inject")
def inject_cash(req: InjectReq, uid: int = Depends(require_user_id)):
    ok, msg = sim.inject_cash(req.amount)
    return JSONResponse({"ok": ok, "message": msg}, status_code=200 if ok else 400)


@app.post("/api/inject_stock")
def inject_stock(req: InjectStockReq, uid: int = Depends(require_user_id)):
    ok, msg = sim.inject_stock(req.qty)
    return JSONResponse({"ok": ok, "message": msg}, status_code=200 if ok else 400)


@app.post("/api/reset_players")
def reset_players(uid: int = Depends(require_user_id)):
    total = sim.reset_players()
    return {"ok": True, "total_players": total, "message": f"Reset to {total} players."}


@app.websocket("/ws")
async def ws_state(websocket: WebSocket, interval: float = 60.0, token: str | None = None):
    raw_tok = token or websocket.cookies.get("userid")
    uid = verify_user_cookie(raw_tok)
    if uid is None or sim.ensure_user_session(uid) is None:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    sim.connect_ws(uid)
    state = {"interval": interval}

    async def recv_loop():
        try:
            while True:
                msg = await websocket.receive_json()
                if "interval" in msg:
                    state["interval"] = float(msg["interval"])
        except Exception:
            pass

    reader = asyncio.create_task(recv_loop())
    try:
        while True:
            payload = await asyncio.to_thread(sim.get_state, uid, state["interval"])
            await websocket.send_json(payload)
            await asyncio.sleep(0.05)
    except WebSocketDisconnect:
        pass
    finally:
        reader.cancel()
        sim.disconnect_ws(uid)


if __name__ == "__main__":
    uvicorn.run(
        "dashboard:app",
        host=_SERVER_CFG.get("host", "0.0.0.0"),
        port=_SERVER_CFG.get("port", 5005),
        reload=True,
    )
