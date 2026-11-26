from datetime import datetime
import json
import os
from pathlib import Path
import random
import matplotlib.pyplot as plt


def load_config():
    cfg_path = Path(__file__).with_name("config.json")
    if cfg_path.exists():
        return json.loads(cfg_path.read_text(encoding="utf-8"))
    return {}


CFG = load_config()
_STOCK_CFG = CFG.get("stock", {})
_BOT_CFG = CFG.get("bot", CFG.get("player", {}))
_SIM_CFG = CFG.get("simulation", {})


class stock:
    def __init__(
        self,
        name: str = _STOCK_CFG.get("name", "AAPL"),
        price: float = _STOCK_CFG.get("initial_price", 100.0),
        numberOfStock: float = _STOCK_CFG.get("number_of_stock", 1000),
    ):
        self.name = name
        self.price = price
        self.numberOfStock = numberOfStock


class exchange:
    def __init__(self):
        self.stocks = {}  # {"AAPL": 100.0}
        self.bids = []  # list of {"id": int, "price": float, "amount": int, "player": Player}
        self.asks = []  # list of {"id": int, "price": float, "amount": int, "player": Player}
        self.transactions = []  # list of {"id", "time", "price", "qty", "buyer_id", "seller_id"}
        self.history = {}  # {"AAPL": [(time, price), ...]}
        self._order_id = 1
        self._tx_id = 1

    def submitBuyOrder(self, s, price, amount, player=None, t=0.0):
        self.bids.append({"id": self._order_id, "price": price, "amount": amount, "player": player})
        self._order_id += 1
        self.bids.sort(key=lambda x: (-x["price"], x["id"]))
        self._match(s, t)

    def submitSellOrder(self, s, price, amount, player=None, t=0.0):
        self.asks.append({"id": self._order_id, "price": price, "amount": amount, "player": player})
        self._order_id += 1
        self.asks.sort(key=lambda x: (x["price"], x["id"]))
        self._match(s, t)

    def _match(self, s, t=0.0):
        if s.name not in self.history:
            self.history[s.name] = [(0.0, s.price)]

        while self.bids and self.asks and self.bids[0]["price"] >= self.asks[0]["price"]:
            bid = self.bids[0]
            ask = self.asks[0]
            qty = min(bid["amount"], ask["amount"])
            # Execute at the resting (older) order's price
            trade_price = ask["price"] if ask["id"] < bid["id"] else bid["price"]

            bid["amount"] -= qty
            ask["amount"] -= qty
            s.price = trade_price

            if bid["player"]:
                bid["player"].cash -= trade_price * qty
                bid["player"].add_stock(s, qty)
            if ask["player"]:
                ask["player"].cash += trade_price * qty
                ask["player"].add_stock(s, -qty)

            self.transactions.append({
                "id": self._tx_id,
                "time": round(t, 4),
                "price": trade_price,
                "qty": qty,
                "buyer_id": bid["player"].id if bid["player"] else None,
                "seller_id": ask["player"].id if ask["player"] else None,
            })
            self._tx_id += 1
            self.history[s.name].append((t, s.price))

            if bid["amount"] == 0:
                self.bids.pop(0)
            if ask["amount"] == 0:
                self.asks.pop(0)
        # update stock price in self.stocks
        self.stocks[s.name] = s.price
    def listbidask(self, s):
        print(f"Price: {s.price:.2f}")
        print("Asks:")
        for ask in reversed(self.asks):
            print(f"  ({ask['price']:.2f}, {ask['amount']})")
        print("Bids:")
        for bid in self.bids:
            print(f"  ({bid['price']:.2f}, {bid['amount']})")


class Player:
    def __init__(self, id=None, cash=None, stock=None, epsilon=None, strategy=None):
        self.id = id
        self.cash = _BOT_CFG.get("initial_cash", 1000000.0) if cash is None else cash
        self.stock = [dict(item) for item in stock] if stock else []  # list of {"name": str, "qty": int}
        self.epsilon = random.uniform(0, 1) if epsilon is None else epsilon
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

    def get_qty(self, s):
        for item in self.stock:
            if item["name"] == s.name:
                return item["qty"]
        return 0

    def add_stock(self, s, qty):
        for item in self.stock:
            if item["name"] == s.name:
                item["qty"] += qty
                return
        self.stock.append({"name": s.name, "qty": qty})

    def buy(self, market, s, price, amount, t=0.0):
        if self.cash >= price * amount:
            market.submitBuyOrder(s, price, amount, self, t=t)

    def sell(self, market, s, price, amount, t=0.0):
        if _BOT_CFG.get("allow_short_selling", True) or self.get_qty(s) >= amount:
            market.submitSellOrder(s, price, amount, self, t=t)

    def act(self, market, s, t=0.0):
        """Submit an order around current price according to the player's strategy."""
        target = self.strategy["target_price"]
        stype = self.strategy["type"]
        p_spread = _BOT_CFG.get("price_spread", 2.0)
        price = round(max(0.01, s.price + random.uniform(-p_spread, p_spread)), 2)
        amount = random.randint(
            _BOT_CFG.get("min_order_qty", 1),
            _BOT_CFG.get("max_order_qty", 10),
        )

        if stype in ("buy_below", "buy_sell") and s.price < target:
            self.buy(market, s, price, amount, t=t)
        elif stype in ("sell_above", "buy_sell") and s.price > target:
            self.sell(market, s, price, amount, t=t)


def run_simulation(n=None, duration=600.0):
    """Run one AAPL market simulation and return (history, final_price, tx_count)."""
    if n is None:
        n = _SIM_CFG.get("initial_players", 50)
    init_shares = _BOT_CFG.get("initial_shares", 50)
    market = exchange()
    s = stock()
    players = [Player(id=i + 1, stock=[{"name": s.name, "qty": init_shares}]) for i in range(n)]

    next_times = [random.expovariate(max(p.epsilon, 0.05) * 2) for p in players]
    market.history[s.name] = [(0.0, s.price)]

    while True:
        idx = min(range(n), key=lambda i: next_times[i])
        t = next_times[idx]
        if t > duration:
            break

        p = players[idx]
        p.act(market, s, t=t)

        next_times[idx] = t + random.expovariate(max(p.epsilon, 0.05) * 2)

    market.history[s.name].append((duration, s.price))
    return market.history[s.name], s.price, len(market.transactions)


def plot_price_history(history, symbol, output_dir="images"):
    """Plot price against time and save to output_dir with timestamp."""
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
