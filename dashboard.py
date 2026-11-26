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

from fastapi import Cookie, Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
import uvicorn

from db import (
    create_user,
    get_user,
    get_user_orders,
    init_db,
    record_fill,
    record_order,
    record_order_cancel,
    record_order_fill,
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
        self.initial_n = _SIM_CFG.get("initial_players", 50) if n is None else n
        self.n = self.initial_n
        self.duration = _SIM_CFG.get("duration", None) if duration is None else duration
        self.running = True
        self.speed = 1.0
        self.user_sessions: dict[int, dict] = {}
        self._player_to_uid: dict[Player, int] = {}
        self._heap: list[tuple[float, int, Player]] = []
        self._heap_seq = 0
        self._candle_buckets: dict[int, dict[int, dict]] = {}
        self._last_close_by_iv: dict[int, dict[int, float]] = {}
        init_db()
        self.reset()

    def _schedule_bot(self, bot: Player, base_t: float):
        ev_t = base_t + random.expovariate(max(bot.epsilon, 0.05) * 2)
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

        # Persist fills & updated user balances to SQLite for manual players
        for order in (bid, ask):
            player = order.get("player")
            uid = self._player_to_uid.get(player) if player is not None else None
            if uid is not None:
                sess = self.user_sessions.get(uid)
                if sess:
                    save_user_state(
                        uid,
                        cash=player.cash,
                        shares=player.get_qty(self.stock),
                        cost_basis=sess["cost_basis"],
                    )
                db_oid = order.get("db_order_id")
                if db_oid is not None and order["filled_qty"] > 0:
                    avg_exec = order["fill_notional"] / order["filled_qty"]
                    status = "filled" if order["amount"] == 0 else "partial"
                    record_order_fill(
                        db_oid,
                        filled_amount=order["filled_qty"],
                        exec_price=avg_exec,
                        status=status,
                        user_balance=player.cash,
                    )
                    record_fill(
                        order_id=db_oid,
                        userid=uid,
                        stock_name=self.stock.name,
                        side=order["side"],
                        time=tx["time"],
                        fill_price=tx["price"],
                        fill_qty=tx["qty"],
                        user_balance=player.cash,
                    )
                    if sess:
                        sess["orders_version"] = sess.get("orders_version", 1) + 1

    def _on_market_cancel(self, order: dict):
        player = order.get("player")
        db_oid = order.get("db_order_id")
        if player is not None and db_oid is not None:
            record_order_cancel(db_oid, user_balance=player.cash)
            uid = self._player_to_uid.get(player)
            if uid is not None and uid in self.user_sessions:
                self.user_sessions[uid]["orders_version"] = self.user_sessions[uid].get("orders_version", 1) + 1

    def reset(self):
        with self.lock:
            if hasattr(self, "market") and self.market is not None:
                for resting in list(self.market.bids) + list(self.market.asks):
                    if resting.get("db_order_id") is not None:
                        self._on_market_cancel(resting)
            self.n = self.initial_n
            bot_shares = _BOT_CFG.get("initial_shares", 50)
            player_shares = _PLAYER_CFG.get("initial_shares", 0)
            self.market = exchange(on_fill=self._on_market_fill, on_cancel=self._on_market_cancel)
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
                save_user_state(uid, cash=init_cash, shares=player_shares, cost_basis=cost_basis)

            total_issued = self.n * bot_shares + len(self.user_sessions) * player_shares
            self.stock.numberOfStock = max(self.stock.numberOfStock, total_issued)

    def reset_players(self):
        with self.lock:
            old_bots = set(self.players)
            self.market.cancel_player_orders(old_bots)
            for b in old_bots:
                b._active = False

            self.n = self.initial_n
            bot_shares = _BOT_CFG.get("initial_shares", 50)
            self.players = [
                Player(id=i + 1, stock=[{"name": self.stock.name, "qty": bot_shares}], is_bot=True)
                for i in range(self.n)
            ]
            self._heap = []
            self._heap_seq = 0
            for p in self.players:
                self._schedule_bot(p, self.t)
            self._next_bot_id = self.n + 1
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

            self.t = target_t
            if self.duration is not None and self.t >= self.duration:
                self.market.history[self.stock.name].append((self.duration, self.stock.price))
                self.running = False

    def login_or_restore_user(self, username: str, initial_cash: float) -> dict:
        with self.lock:
            init_shares = _PLAYER_CFG.get("initial_shares", 0)
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
            return user

    def ensure_user_session(self, user_id: int) -> dict | None:
        with self.lock:
            sess = self.user_sessions.get(user_id)
            if sess is not None:
                return sess
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
                avg_exec = round(order["fill_notional"] / filled, 4) if filled > 0 else 0.0
                status = "filled" if filled == amount else "partial_cancelled"
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
                for ft, fp, fq in order.get("fills", []):
                    record_fill(
                        order_id=db_oid,
                        userid=user_id,
                        stock_name=self.stock.name,
                        side=side,
                        time=ft,
                        fill_price=fp,
                        fill_qty=fq,
                        user_balance=player.cash,
                    )
                save_user_state(
                    user_id,
                    cash=player.cash,
                    shares=player.get_qty(self.stock),
                    cost_basis=sess["cost_basis"],
                )
                sess["orders_version"] = sess.get("orders_version", 1) + 1
                return True, msg

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
            for ft, fp, fq in order.get("fills", []):
                record_fill(
                    order_id=db_oid,
                    userid=user_id,
                    stock_name=self.stock.name,
                    side=side,
                    time=ft,
                    fill_price=fp,
                    fill_qty=fq,
                    user_balance=player.cash,
                )
            sess["orders_version"] = sess.get("orders_version", 1) + 1
            if filled > 0:
                msg = f"{side.capitalize()} limit filled {filled}/{amount} @ ${avg_exec:.2f}."
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
            save_user_state(
                user_id,
                cash=player.cash,
                shares=player.get_qty(self.stock),
                cost_basis=sess["cost_basis"],
            )
            return True, player.cash, f"Added ${amount:,.0f} to {sess['username']}."

    def cancel_order(self, user_id: int, order_id: int):
        with self.lock:
            sess = self.ensure_user_session(user_id)
            if sess is None:
                return False, "User session not found."
            ok, _ = self.market.cancel_order(order_id, player=sess["player"])
            if ok:
                return True, f"Cancelled order #{order_id}."
            return False, "Order not found."

    def add_players(self, count=10, bias="random"):
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
                    is_bot=True,
                )
                self._next_bot_id += 1
                self.players.append(p)
                self._schedule_bot(p, self.t)

            if overflow < count:
                self.stock.numberOfStock += (count - max(0, overflow)) * bot_shares
            self.n = len(self.players)
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
                save_user_state(
                    uid,
                    cash=up.cash,
                    shares=up.get_qty(self.stock),
                    cost_basis=sess["cost_basis"],
                )
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
                save_user_state(
                    uid,
                    cash=up.cash,
                    shares=up.get_qty(self.stock),
                    cost_basis=sess["cost_basis"],
                )
            total_recipients = len(self.players) + len(self.user_sessions)
            self.stock.numberOfStock += qty * total_recipients
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

    def get_state(self, user_id: int | None = None, interval=60.0):
        with self.lock:
            sess = self.ensure_user_session(user_id) if user_id is not None else None
            manual_player = sess["player"] if sess else None
            username = sess["username"] if sess else "YOU"
            cost_basis = sess["cost_basis"] if sess else 0.0

            price = self.stock.price
            asks = [
                {
                    "id": a["id"],
                    "price": a["price"],
                    "amount": a["amount"],
                    "is_you": manual_player is not None and a["player"] is manual_player,
                }
                for a in self.market.asks[:10]
            ]
            bids = [
                {
                    "id": b["id"],
                    "price": b["price"],
                    "amount": b["amount"],
                    "is_you": manual_player is not None and b["player"] is manual_player,
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

            your_orders = []
            if manual_player is not None:
                your_orders = [
                    {"id": a["id"], "side": "ask", "price": a["price"], "amount": a["amount"]}
                    for a in self.market.asks
                    if a["player"] is manual_player
                ] + [
                    {"id": b["id"], "side": "bid", "price": b["price"], "amount": b["amount"]}
                    for b in self.market.bids
                    if b["player"] is manual_player
                ]

            txs = list(reversed(self.market.transactions[-15:]))
            history = self.market.history.get(self.stock.name, [])
            if len(history) > 500:
                stride = max(1, (len(history) - 1) // 500)
                sampled = history[:-1:stride]
                history = sampled + [history[-1]]
            candles = self._build_candles(interval=interval)

            leaderboard = []
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
                leaderboard.append({
                    "id": p.id,
                    "is_you": False,
                    "strategy": stype,
                    "target": p.strategy["target_price"],
                    "eps": round(p.epsilon, 2),
                    "cash": round(p.cash, 2),
                    "qty": qty,
                    "value": val,
                    "pnl": round(val - bot_init_basis, 2),
                })

            if manual_player is not None:
                you_qty = manual_player.get_qty(self.stock)
                you_cash = round(manual_player.cash, 2)
                you_avail_cash = round(manual_player.available_cash(self.stock, price), 2)
                you_reserved_cash = round(manual_player.reserved_cash, 2)
                you_val = round(manual_player.cash + you_qty * price, 2)
                you_pnl = round(you_val - cost_basis, 2)
                you_pnl_pct = round((you_pnl / cost_basis) * 100, 2) if cost_basis else 0.0
                leaderboard.append({
                    "id": sess["username"],
                    "is_you": True,
                    "strategy": "manual",
                    "target": None,
                    "eps": None,
                    "cash": you_cash,
                    "qty": you_qty,
                    "value": you_val,
                    "pnl": you_pnl,
                })
            else:
                you_qty = 0
                you_cash = 0.0
                you_avail_cash = 0.0
                you_reserved_cash = 0.0
                you_val = 0.0
                you_pnl = 0.0
                you_pnl_pct = 0.0

            leaderboard.sort(key=lambda x: -x["pnl"])

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
                "player_breakdown": breakdown,
                "tx_count": self.market.total_tx_count,
                "history": history,
                "candles": candles,
                "asks": list(reversed(asks)),
                "bids": bids,
                "all_asks": [[p, q] for p, q in list(ask_levels.items())[:200]],
                "all_bids": [[p, q] for p, q in list(bid_levels.items())[:200]],
                "total_ask_qty": total_ask_qty,
                "total_bid_qty": total_bid_qty,
                "your_orders": your_orders,
                "transactions": txs,
                "my_tx_version": sess.get("orders_version", 1) if sess else 0,
                "leaderboard": leaderboard[:10],
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


def require_user_id(userid: str | None = Cookie(default=None)) -> int:
    uid = verify_user_cookie(userid)
    if uid is None or sim.ensure_user_session(uid) is None:
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
    resp = JSONResponse({"ok": True, "user": user})
    resp.set_cookie(
        key="userid",
        value=sign_user_cookie(user["id"]),
        httponly=True,
        samesite="lax",
    )
    return resp


@app.post("/api/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("userid")
    return resp


@app.get("/", response_class=HTMLResponse)
def index(userid: str | None = Cookie(default=None)):
    uid = verify_user_cookie(userid)
    if uid is None or sim.ensure_user_session(uid) is None:
        return RedirectResponse(url="/login", status_code=302)
    html = Path(__file__).with_name("index.html").read_text(encoding="utf-8")
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/api/state")
def get_state(interval: float = 60.0, uid: int = Depends(require_user_id)):
    return sim.get_state(user_id=uid, interval=interval)


@app.get("/api/my_transactions")
def get_my_transactions(uid: int = Depends(require_user_id)):
    return {"ok": True, "transactions": get_user_orders(uid)}


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
    ok, total, msg = sim.add_players(req.count, bias=req.bias)
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
async def ws_state(websocket: WebSocket, interval: float = 60.0):
    uid = verify_user_cookie(websocket.cookies.get("userid"))
    if uid is None or sim.ensure_user_session(uid) is None:
        await websocket.close(code=1008)
        return

    await websocket.accept()
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
            await websocket.send_json(sim.get_state(user_id=uid, interval=state["interval"]))
            await asyncio.sleep(0.05)
    except WebSocketDisconnect:
        pass
    finally:
        reader.cancel()


if __name__ == "__main__":
    uvicorn.run(
        "dashboard:app",
        host=_SERVER_CFG.get("host", "127.0.0.1"),
        port=_SERVER_CFG.get("port", 5005),
        reload=True,
    )
