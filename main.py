import bisect
from datetime import datetime
import heapq
import json
import os
from pathlib import Path
import random


def load_config():
    cfg_path = Path(__file__).with_name("config.json")
    if cfg_path.exists():
        return json.loads(cfg_path.read_text(encoding="utf-8"))
    return {}


CFG = load_config()
_STOCK_CFG = CFG.get("stock", {})
_BOT_CFG = CFG.get("bot", CFG.get("player", {}))
_PLAYER_CFG = CFG.get("player", {})
_SIM_CFG = CFG.get("simulation", {})

SHORT_INITIAL_MARGIN_RATE = 0.50
SHORT_MAINT_MARGIN_RATE = 0.25
MAX_TRANSACTIONS = 5000
MAX_HISTORY_POINTS = 2000
MAX_BOT_ORDERS_PER_SIDE = 2
BOT_ORDER_TTL = 60.0


class stock:
    def __init__(
        self,
        name: str = _STOCK_CFG.get("name", "AAPL"),
        price: float = _STOCK_CFG.get("initial_price", 100.0),
        numberOfStock: int = _STOCK_CFG.get("number_of_stock", 1000),
    ):
        self.name = name
        self.price = float(price)
        self.numberOfStock = int(numberOfStock)

    @property
    def market_cap(self) -> float:
        return round(self.price * self.numberOfStock, 2)


class exchange:
    def __init__(self, on_fill=None, on_cancel=None, max_matches_per_tick: int | None = None):
        self.stocks = {}  # {"AAPL": 100.0}
        self.bids = []  # sorted by (-price, id)
        self.asks = []  # sorted by (price, id)
        self._bid_keys = []
        self._ask_keys = []
        self.active_market_orders = []  # large market orders filling dynamically across ticks
        self.transactions = []  # recent transactions (bounded by MAX_TRANSACTIONS)
        self.total_tx_count = 0
        self.history = {}  # {"AAPL": [(time, price), ...]}
        self._order_id = 1
        self._tx_id = 1
        self.on_fill = on_fill
        self.on_cancel = on_cancel
        self.max_matches_per_tick = max_matches_per_tick

    def _release_order_escrow(self, order):
        player = order.get("player")
        if not player:
            return
        if order.get("side") == "buy":
            rem_cash = order.get("reserved_cash", 0.0)
            if rem_cash > 0:
                player.reserved_cash = max(0.0, round(player.reserved_cash - rem_cash, 8))
                order["reserved_cash"] = 0.0
        elif order.get("side") == "sell":
            s_name = order.get("stock_name")
            rem_shares = order.get("reserved_shares", 0)
            if s_name and rem_shares > 0:
                cur = player.reserved_shares.get(s_name, 0)
                player.reserved_shares[s_name] = max(0, cur - rem_shares)
                order["reserved_shares"] = 0
            rem_margin = order.get("reserved_margin", 0.0)
            if rem_margin > 0:
                player.reserved_cash = max(0.0, round(player.reserved_cash - rem_margin, 8))
                order["reserved_margin"] = 0.0

    def _pop_bid(self, idx: int):
        self._bid_keys.pop(idx)
        return self.bids.pop(idx)

    def _pop_ask(self, idx: int):
        self._ask_keys.pop(idx)
        return self.asks.pop(idx)

    def _append_history(self, s_name: str, t: float, price: float):
        hist = self.history.setdefault(s_name, [(0.0, price)])
        hist.append((round(t, 4), price))
        if len(hist) > MAX_HISTORY_POINTS:
            # Compress older half by keeping every 2nd point, preserving start and recent detail
            half = MAX_HISTORY_POINTS // 2
            self.history[s_name] = [hist[0]] + hist[1:half:2] + hist[half:]

    def _record_fill(self, s, bid, ask, qty: int, trade_price: float, t: float):
        bid["amount"] -= qty
        ask["amount"] -= qty
        bid["filled_qty"] = bid.get("filled_qty", 0) + qty
        ask["filled_qty"] = ask.get("filled_qty", 0) + qty
        bid["fill_notional"] = bid.get("fill_notional", 0.0) + trade_price * qty
        ask["fill_notional"] = ask.get("fill_notional", 0.0) + trade_price * qty
        p_key = round(trade_price, 2)
        t_val = round(t, 4)
        for ord_obj in (bid, ask):
            fbp = ord_obj.setdefault("fills_by_price", {})
            prev = fbp.get(p_key)
            if prev is None:
                fbp[p_key] = [t_val, qty]
            else:
                prev[0] = t_val
                prev[1] += qty
        s.price = round(trade_price, 4)

        buyer = bid.get("player")
        seller = ask.get("player")

        if buyer:
            # Release proportional reserved cash (at bid's limit price), then deduct actual trade cost
            if bid.get("reserved_cash", 0.0) > 0:
                orig_rem = bid["amount"] + qty
                release = bid["reserved_cash"] if bid["amount"] == 0 else round(bid["reserved_cash"] * (qty / orig_rem), 8)
                bid["reserved_cash"] = max(0.0, round(bid["reserved_cash"] - release, 8))
                buyer.reserved_cash = max(0.0, round(buyer.reserved_cash - release, 8))
            buyer.cash = round(buyer.cash - trade_price * qty, 6)
            buyer.add_stock(s, qty)

        if seller:
            # Release reserved long shares first, then proportional short margin
            if ask.get("reserved_shares", 0) > 0:
                used_shares = min(qty, ask["reserved_shares"])
                ask["reserved_shares"] -= used_shares
                cur_res = seller.reserved_shares.get(s.name, 0)
                seller.reserved_shares[s.name] = max(0, cur_res - used_shares)
            if ask.get("reserved_margin", 0.0) > 0:
                orig_rem = ask["amount"] + qty
                release_m = ask["reserved_margin"] if ask["amount"] == 0 else round(ask["reserved_margin"] * (qty / orig_rem), 8)
                ask["reserved_margin"] = max(0.0, round(ask["reserved_margin"] - release_m, 8))
                seller.reserved_cash = max(0.0, round(seller.reserved_cash - release_m, 8))
            seller.cash = round(seller.cash + trade_price * qty, 6)
            seller.add_stock(s, -qty)

        tx = {
            "id": self._tx_id,
            "time": round(t, 4),
            "price": round(trade_price, 2),
            "qty": qty,
            "buyer_id": buyer.id if buyer else None,
            "seller_id": seller.id if seller else None,
        }
        self._tx_id += 1
        self.total_tx_count += 1
        self.transactions.append(tx)
        if len(self.transactions) > MAX_TRANSACTIONS:
            del self.transactions[: len(self.transactions) - MAX_TRANSACTIONS]

        self._append_history(s.name, t, round(s.price, 2))
        self.stocks[s.name] = round(s.price, 2)

        if self.on_fill:
            self.on_fill(tx, bid, ask)

    def submitBuyOrder(self, s, price, amount, player=None, t=0.0, db_order_id=None, reserved_cash=None):
        price = round(float(price), 2)
        amount = int(amount)
        if reserved_cash is None and player is not None:
            reserved_cash = round(price * amount, 6)
            player.reserved_cash = round(player.reserved_cash + reserved_cash, 8)
        order = {
            "id": self._order_id,
            "side": "buy",
            "stock_name": s.name,
            "price": price,
            "amount": amount,
            "orig_amount": amount,
            "filled_qty": 0,
            "fill_notional": 0.0,
            "player": player,
            "time": t,
            "reserved_cash": float(reserved_cash or 0.0),
            "db_order_id": db_order_id,
        }
        self._order_id += 1
        key = (-price, order["id"])
        idx = bisect.bisect_right(self._bid_keys, key)
        self._bid_keys.insert(idx, key)
        self.bids.insert(idx, order)
        self._match(s, t)
        return order

    def submitSellOrder(
        self,
        s,
        price,
        amount,
        player=None,
        t=0.0,
        db_order_id=None,
        reserved_shares=None,
        reserved_margin=0.0,
    ):
        price = round(float(price), 2)
        amount = int(amount)
        if reserved_shares is None and player is not None:
            avail_long = player.available_shares(s)
            reserved_shares = min(amount, avail_long)
            if reserved_shares > 0:
                player.reserved_shares[s.name] = player.reserved_shares.get(s.name, 0) + reserved_shares
        order = {
            "id": self._order_id,
            "side": "sell",
            "stock_name": s.name,
            "price": price,
            "amount": amount,
            "orig_amount": amount,
            "filled_qty": 0,
            "fill_notional": 0.0,
            "player": player,
            "time": t,
            "reserved_shares": int(reserved_shares or 0),
            "reserved_margin": float(reserved_margin or 0.0),
            "db_order_id": db_order_id,
        }
        self._order_id += 1
        key = (price, order["id"])
        idx = bisect.bisect_right(self._ask_keys, key)
        self._ask_keys.insert(idx, key)
        self.asks.insert(idx, order)
        self._match(s, t)
        return order

    def _match(self, s, t=0.0, max_matches: int | None = None):
        if s.name not in self.history:
            self.history[s.name] = [(0.0, s.price)]

        limit = self.max_matches_per_tick if max_matches is None else max_matches
        matches = 0

        while self.bids and self.asks and self.bids[0]["price"] >= self.asks[0]["price"]:
            if limit is not None and matches >= limit:
                break
            bid = self.bids[0]
            ask = self.asks[0]

            # Self-Trade Prevention (STP): cancel the older resting order
            if bid["player"] is not None and bid["player"] is ask["player"]:
                if ask["id"] < bid["id"]:
                    cancelled = self._pop_ask(0)
                else:
                    cancelled = self._pop_bid(0)
                self._release_order_escrow(cancelled)
                if self.on_cancel:
                    self.on_cancel(cancelled)
                continue

            qty = min(bid["amount"], ask["amount"])
            trade_price = ask["price"] if ask["id"] < bid["id"] else bid["price"]

            self._record_fill(s, bid, ask, qty, trade_price, t)
            matches += 1

            if bid["amount"] == 0:
                self._release_order_escrow(bid)
                self._pop_bid(0)
            if ask["amount"] == 0:
                self._release_order_escrow(ask)
                self._pop_ask(0)

        self.stocks[s.name] = round(s.price, 2)

    def _step_market_order(self, s, temp_order: dict, t: float, max_matches: int | None = None) -> tuple[bool, str | None]:
        """Execute up to max_matches fills for a market order. Returns (done, stop_reason)."""
        side = temp_order["side"]
        player = temp_order.get("player")
        matches = 0

        if side == "buy":
            while temp_order["amount"] > 0 and self.asks:
                if max_matches is not None and matches >= max_matches:
                    return False, None
                ask = self.asks[0]
                if player is not None and ask["player"] is player:
                    cancelled = self._pop_ask(0)
                    self._release_order_escrow(cancelled)
                    if self.on_cancel:
                        self.on_cancel(cancelled)
                    continue
                trade_price = ask["price"]
                max_qty = min(temp_order["amount"], ask["amount"])
                if player is not None:
                    spendable = player.spendable_for_buy(s, trade_price)
                    affordable = int(spendable // trade_price)
                    if affordable <= 0:
                        return True, "Insufficient available cash."
                    max_qty = min(max_qty, affordable)
                self._record_fill(s, temp_order, ask, max_qty, trade_price, t)
                matches += 1
                if ask["amount"] == 0:
                    self._release_order_escrow(ask)
                    self._pop_ask(0)
            return True, None

        if side == "sell":
            while temp_order["amount"] > 0 and self.bids:
                if max_matches is not None and matches >= max_matches:
                    return False, None
                bid = self.bids[0]
                if player is not None and bid["player"] is player:
                    cancelled = self._pop_bid(0)
                    self._release_order_escrow(cancelled)
                    if self.on_cancel:
                        self.on_cancel(cancelled)
                    continue
                trade_price = bid["price"]
                max_qty = min(temp_order["amount"], bid["amount"])
                if player is not None:
                    sellable, reason = player.max_sellable_qty(s, trade_price, max_qty)
                    if sellable <= 0:
                        return True, reason
                    max_qty = min(max_qty, sellable)
                self._record_fill(s, bid, temp_order, max_qty, trade_price, t)
                matches += 1
                if bid["amount"] == 0:
                    self._release_order_escrow(bid)
                    self._pop_bid(0)
            return True, None

        return True, "Invalid order side."

    def process_pending(self, s, t: float):
        """Advance any chunked market orders and remaining crossed limit orders."""
        if self.active_market_orders:
            still_active = []
            for m_ord in self.active_market_orders:
                done, _ = self._step_market_order(s, m_ord, t, max_matches=self.max_matches_per_tick)
                if not done and m_ord["amount"] > 0:
                    still_active.append(m_ord)
                elif m_ord["amount"] > 0 and self.on_cancel:
                    # Exhausted liquidity or buying power before full fill
                    self.on_cancel(m_ord)
            self.active_market_orders = still_active
        self._match(s, t)

    def submitMarketOrder(self, s, side: str, amount: int, player=None, t=0.0, db_order_id=None):
        amount = int(amount)
        if amount <= 0:
            return False, "Amount must be positive.", None
        if side not in ("buy", "sell"):
            return False, "Invalid order side.", None
        if side == "buy" and not self.asks:
            return False, "No asks available in order book.", None
        if side == "sell" and not self.bids:
            return False, "No bids available in order book.", None
        if s.name not in self.history:
            self.history[s.name] = [(0.0, s.price)]

        temp_order = {
            "id": self._order_id,
            "side": side,
            "stock_name": s.name,
            "price": 0.0,
            "amount": amount,
            "orig_amount": amount,
            "filled_qty": 0,
            "fill_notional": 0.0,
            "player": player,
            "time": t,
            "reserved_cash": 0.0,
            "reserved_shares": 0,
            "reserved_margin": 0.0,
            "db_order_id": db_order_id,
            "is_active_market": False,
        }
        self._order_id += 1

        done, stop_reason = self._step_market_order(s, temp_order, t, max_matches=self.max_matches_per_tick)
        filled = temp_order["filled_qty"]
        if filled == 0 and done:
            default_err = f"No liquidity available to fill market {side}."
            return False, stop_reason or default_err, None

        avg_p = temp_order["fill_notional"] / filled if filled > 0 else s.price
        if not done and temp_order["amount"] > 0:
            temp_order["is_active_market"] = True
            self.active_market_orders.append(temp_order)
            msg = f"Market {side} filling dynamically ({filled}/{amount} filled so far @ ${avg_p:.2f})."
            return True, msg, temp_order

        msg = (
            f"Market {side} filled {filled}/{amount} @ ${avg_p:.2f}."
            if filled < amount
            else f"Market {side} filled {filled} @ ${avg_p:.2f}."
        )
        return True, msg, temp_order

    def cancel_order(self, order_id: int, player=None):
        for i, order in enumerate(self.active_market_orders):
            if order["id"] == order_id and (player is None or order["player"] is player):
                removed = self.active_market_orders.pop(i)
                removed["is_active_market"] = False
                if self.on_cancel:
                    self.on_cancel(removed)
                return True, removed
        for book, keys in ((self.bids, self._bid_keys), (self.asks, self._ask_keys)):
            for i, order in enumerate(book):
                if order["id"] == order_id and (player is None or order["player"] is player):
                    keys.pop(i)
                    removed = book.pop(i)
                    self._release_order_escrow(removed)
                    if self.on_cancel:
                        self.on_cancel(removed)
                    return True, removed
        return False, None

    def cancel_player_orders(self, players_set, side=None):
        if not isinstance(players_set, (set, frozenset)):
            players_set = {players_set}
        for i in range(len(self.active_market_orders) - 1, -1, -1):
            if self.active_market_orders[i]["player"] in players_set and (side is None or self.active_market_orders[i]["side"] == side):
                removed = self.active_market_orders.pop(i)
                removed["is_active_market"] = False
                if self.on_cancel:
                    self.on_cancel(removed)
        targets = (
            [(self.bids, self._bid_keys)]
            if side == "buy"
            else [(self.asks, self._ask_keys)]
            if side == "sell"
            else [(self.bids, self._bid_keys), (self.asks, self._ask_keys)]
        )
        for book, keys in targets:
            i = len(book) - 1
            while i >= 0:
                if book[i]["player"] in players_set:
                    keys.pop(i)
                    removed = book.pop(i)
                    self._release_order_escrow(removed)
                    if self.on_cancel:
                        self.on_cancel(removed)
                i -= 1

    def prune_bot_orders(self, player, now_t: float, max_per_side: int = MAX_BOT_ORDERS_PER_SIDE, ttl: float = BOT_ORDER_TTL):
        for book, keys in ((self.bids, self._bid_keys), (self.asks, self._ask_keys)):
            own_indices = [i for i, o in enumerate(book) if o["player"] is player]
            if not own_indices:
                continue
            # Remove expired orders first, and keep at most max_per_side newest orders
            own_by_age = sorted(own_indices, key=lambda idx: book[idx]["id"])
            to_remove = set()
            for idx in own_by_age:
                if now_t - book[idx]["time"] > ttl:
                    to_remove.add(idx)
            remaining = [idx for idx in own_by_age if idx not in to_remove]
            if len(remaining) > max_per_side:
                for idx in remaining[: len(remaining) - max_per_side]:
                    to_remove.add(idx)
            for idx in sorted(to_remove, reverse=True):
                keys.pop(idx)
                removed = book.pop(idx)
                self._release_order_escrow(removed)

    def reference_price(self, s) -> float:
        if self.bids and self.asks:
            return round((self.bids[0]["price"] + self.asks[0]["price"]) / 2.0, 2)
        if self.bids and self.bids[0]["price"] > s.price:
            return self.bids[0]["price"]
        if self.asks and self.asks[0]["price"] < s.price:
            return self.asks[0]["price"]
        return s.price

    def listbidask(self, s):
        print(f"Price: {s.price:.2f}")
        print("Asks:")
        for ask in reversed(self.asks):
            print(f"  ({ask['price']:.2f}, {ask['amount']})")
        print("Bids:")
        for bid in self.bids:
            print(f"  ({bid['price']:.2f}, {bid['amount']})")


class Player:
    def __init__(
        self,
        id=None,
        cash=None,
        stock=None,
        order_freq=None,
        strategy=None,
        is_bot: bool = True,
        allow_short_selling=None,
    ):
        self.id = id
        self.is_bot = is_bot
        default_cash = _BOT_CFG.get("initial_cash", 1000000.0) if is_bot else _PLAYER_CFG.get("initial_cash", 1000000.0)
        self.cash = float(default_cash if cash is None else cash)
        self.stock = [dict(item) for item in stock] if stock else []
        self.reserved_cash = 0.0
        self.reserved_shares = {}
        self._active = True
        if allow_short_selling is None:
            cfg_src = _BOT_CFG if is_bot else _PLAYER_CFG
            self.allow_short_selling = bool(cfg_src.get("allow_short_selling", True))
        else:
            self.allow_short_selling = bool(allow_short_selling)
        self.order_freq = random.uniform(0.05, 1) if order_freq is None else order_freq
        init_price = _STOCK_CFG.get("initial_price", 100.0)
        spread = _BOT_CFG.get("target_price_range", 5.0)
        self.strategy = (
            dict(strategy)
            if strategy is not None
            else {
                "target_price": round(random.uniform(init_price - spread, init_price + spread), 2),
                "type": random.choice(["buy_below", "sell_above", "buy_sell"]),
            }
        )

    def __hash__(self):
        return object.__hash__(self)

    def get_qty(self, s) -> int:
        s_name = s.name if hasattr(s, "name") else str(s)
        for item in self.stock:
            if item["name"] == s_name:
                return int(item["qty"])
        return 0

    def add_stock(self, s, qty: int):
        s_name = s.name if hasattr(s, "name") else str(s)
        for item in self.stock:
            if item["name"] == s_name:
                item["qty"] += int(qty)
                return
        self.stock.append({"name": s_name, "qty": int(qty)})

    def available_shares(self, s) -> int:
        qty = self.get_qty(s)
        s_name = s.name if hasattr(s, "name") else str(s)
        return max(0, qty - self.reserved_shares.get(s_name, 0))

    def short_locked_cash(self, s=None, mark_price: float | None = None) -> float:
        total_lock = 0.0
        for item in self.stock:
            if item["qty"] < 0:
                p = mark_price if (s is not None and item["name"] == s.name and mark_price is not None) else (s.price if s is not None and item["name"] == s.name else 100.0)
                # 100% short proceeds + 50% initial margin locked against cash
                total_lock += abs(item["qty"]) * p * (1.0 + SHORT_INITIAL_MARGIN_RATE)
        return total_lock

    def available_cash(self, s=None, mark_price: float | None = None) -> float:
        return max(0.0, round(self.cash - self.reserved_cash - self.short_locked_cash(s, mark_price), 6))

    def spendable_for_buy(self, s, order_price: float) -> float:
        qty = self.get_qty(s)
        free_cash = self.available_cash(s, s.price)
        if qty < 0:
            # Covering short shares can utilize the locked short proceeds + margin for those shares
            short_credit = abs(qty) * s.price * (1.0 + SHORT_INITIAL_MARGIN_RATE)
            return max(0.0, min(self.cash - self.reserved_cash, free_cash + short_credit))
        return free_cash

    def equity(self, s) -> float:
        return round(self.cash + self.get_qty(s) * s.price, 2)

    def max_sellable_qty(self, s, order_price: float, desired_qty: int) -> tuple[int, str]:
        avail_long = self.available_shares(s)
        if avail_long >= desired_qty:
            return desired_qty, ""
        if not self.allow_short_selling:
            return avail_long, "Insufficient available shares (short selling disabled)."
        short_needed = desired_qty - avail_long
        unit_margin = max(order_price, s.price) * SHORT_INITIAL_MARGIN_RATE
        free_cash = self.available_cash(s, s.price)
        if unit_margin <= 0:
            return desired_qty, ""
        max_short = int(free_cash // unit_margin)
        if max_short <= 0 and avail_long == 0:
            return 0, "Insufficient margin for short sale."
        return avail_long + min(short_needed, max_short), "Insufficient margin for full short quantity."

    def buy(self, market, s, price, amount, t=0.0, db_order_id=None):
        price = round(float(price), 2)
        amount = int(amount)
        if price <= 0 or amount <= 0:
            return False, "Price and amount must be positive.", None
        cost = round(price * amount, 6)
        if self.spendable_for_buy(s, price) + 1e-9 < cost:
            return False, "Insufficient available cash.", None
        self.reserved_cash = round(self.reserved_cash + cost, 8)
        order = market.submitBuyOrder(
            s,
            price,
            amount,
            self,
            t=t,
            db_order_id=db_order_id,
            reserved_cash=cost,
        )
        return True, "Buy limit order submitted.", order

    def sell(self, market, s, price, amount, t=0.0, db_order_id=None):
        price = round(float(price), 2)
        amount = int(amount)
        if price <= 0 or amount <= 0:
            return False, "Price and amount must be positive.", None
        avail_long = self.available_shares(s)
        long_part = min(amount, avail_long)
        short_part = amount - long_part
        req_margin = 0.0
        if short_part > 0:
            if not self.allow_short_selling:
                return False, "Insufficient available shares.", None
            req_margin = round(max(price, s.price) * SHORT_INITIAL_MARGIN_RATE * short_part, 6)
            if self.available_cash(s, s.price) + 1e-9 < req_margin or self.equity(s) <= req_margin:
                return False, "Insufficient margin for short sale.", None

        if long_part > 0:
            self.reserved_shares[s.name] = self.reserved_shares.get(s.name, 0) + long_part
        if req_margin > 0:
            self.reserved_cash = round(self.reserved_cash + req_margin, 8)

        order = market.submitSellOrder(
            s,
            price,
            amount,
            self,
            t=t,
            db_order_id=db_order_id,
            reserved_shares=long_part,
            reserved_margin=req_margin,
        )
        return True, "Sell limit order submitted.", order

    def act(self, market, s, t=0.0):
        """Submit an order around reference price according to the bot's strategy."""
        market.prune_bot_orders(self, now_t=t)

        qty = self.get_qty(s)
        # Automatic margin cover if deeply short and approaching maintenance threshold
        if qty < 0 and self.equity(s) < abs(qty) * s.price * SHORT_MAINT_MARGIN_RATE:
            cover_qty = max(1, min(abs(qty), _BOT_CFG.get("max_order_qty", 10)))
            market.submitMarketOrder(s, "buy", cover_qty, player=self, t=t)
            return

        ref = market.reference_price(s)
        target = self.strategy["target_price"]
        stype = self.strategy["type"]
        p_spread = _BOT_CFG.get("price_spread", 2.0)

        # Gentle target adaptation prevents permanent market freeze after extreme price shocks
        if abs(ref - target) > p_spread * 2.5:
            target = round(max(0.05, target + 0.02 * (ref - target)), 2)
            self.strategy["target_price"] = target

        amount = random.randint(
            _BOT_CFG.get("min_order_qty", 1),
            _BOT_CFG.get("max_order_qty", 10),
        )

        if stype == "buy_below":
            if ref <= target:
                price = round(max(0.01, ref + random.uniform(-p_spread * 0.6, p_spread)), 2)
                self.buy(market, s, price, amount, t=t)
            elif random.random() < 0.15:
                # Passive value bid near target keeps bid book alive
                bid_anchor = round(max(0.01, (ref + target) * 0.5 + random.uniform(-p_spread, 0)), 2)
                self.buy(market, s, bid_anchor, amount, t=t)
        elif stype == "sell_above":
            if ref >= target:
                price = round(max(0.01, ref + random.uniform(-p_spread, p_spread * 0.6)), 2)
                self.sell(market, s, price, amount, t=t)
            elif random.random() < 0.15:
                ask_anchor = round(max(0.01, (ref + target) * 0.5 + random.uniform(0, p_spread)), 2)
                self.sell(market, s, ask_anchor, amount, t=t)
        else:
            # buy_sell: two-sided market maker skewed by fundamental target
            diff = target - ref
            buy_prob = max(0.15, min(0.85, 0.5 + diff / max(p_spread * 4.0, 1.0)))
            if random.random() < buy_prob:
                price = round(max(0.01, ref + random.uniform(-p_spread * 0.75, p_spread * 0.75)), 2)
                self.buy(market, s, price, amount, t=t)
            else:
                price = round(max(0.01, ref + random.uniform(-p_spread * 0.75, p_spread * 0.75)), 2)
                self.sell(market, s, price, amount, t=t)


def run_simulation(n=None, duration=600.0):
    """Run one AAPL market simulation and return (history, final_price, tx_count)."""
    if n is None:
        n = _SIM_CFG.get("initial_players", 50)
    init_shares = _BOT_CFG.get("initial_shares", 50)
    market = exchange()
    s = stock()
    s.numberOfStock = max(s.numberOfStock, n * init_shares)
    players = [Player(id=i + 1, stock=[{"name": s.name, "qty": init_shares}], is_bot=True) for i in range(n)]

    market.history[s.name] = [(0.0, s.price)]
    heap = []
    seq = 0
    for p in players:
        if p.order_freq <= 0:
            continue
        ev_t = random.expovariate(p.order_freq * 2)
        heapq.heappush(heap, (ev_t, seq, p))
        seq += 1

    while heap:
        t, _, p = heapq.heappop(heap)
        if t > duration:
            break
        p.act(market, s, t=t)
        next_t = t + random.expovariate(p.order_freq * 2)
        heapq.heappush(heap, (next_t, seq, p))
        seq += 1

    market.history[s.name].append((duration, s.price))
    return market.history[s.name], s.price, market.total_tx_count


def plot_price_history(history, symbol, output_dir="images"):
    """Plot price against time and save to output_dir with timestamp."""
    import matplotlib.pyplot as plt

    os.makedirs(output_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = os.path.join(output_dir, f"{symbol.lower()}_{timestamp}.png")

    times = [t for t, _ in history]
    prices = [p for _, p in history]

    plt.figure(figsize=(8, 4))
    plt.plot(times, prices)
    plt.title(f"{symbol} Price vs Time")
    plt.xlabel("Time (s)")
    plt.ylabel("Price")
    plt.grid(True)
    plt.tight_layout()
    plt.savefig(out_path, dpi=150)
    plt.close()
    return out_path


if __name__ == "__main__":
    history, final_price, tx_count = run_simulation(n=50, duration=300.0)
    print(f"Final price: {final_price:.2f}, transactions: {tx_count}")

    saved_path = plot_price_history(history, "AAPL")
    print(f"Saved plot to {saved_path}")
