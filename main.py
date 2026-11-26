import random


class stock:
    def __init__(self, name: str = "AAPL", price: float = 100.0, numberOfStock: float = 0):
        self.name = name
        self.price = price
        self.numberOfStock = numberOfStock


class exchange:
    def __init__(self):
        self.stocks = {}  # {"AAPL": 100.0}
        self.bids = []  # list of {"price": float, "amount": int, "player": Player}
        self.asks = []  # list of {"price": float, "amount": int, "player": Player}

    def submitBuyOrder(self, s, price, amount, player=None):
        self.bids.append({"price": price, "amount": amount, "player": player})
        self.bids.sort(key=lambda x: -x["price"])
        self._match(s)

    def submitSellOrder(self, s, price, amount, player=None):
        self.asks.append({"price": price, "amount": amount, "player": player})
        self.asks.sort(key=lambda x: x["price"])
        self._match(s)

    def _match(self, s):
        while self.bids and self.asks and self.bids[0]["price"] >= self.asks[0]["price"]:
            bid = self.bids[0]
            ask = self.asks[0]
            qty = min(bid["amount"], ask["amount"])
            trade_price = ask["price"]

            bid["amount"] -= qty
            ask["amount"] -= qty
            s.price = trade_price

            if bid["player"]:
                bid["player"].cash -= trade_price * qty
                bid["player"].add_stock(s, qty)
            if ask["player"]:
                ask["player"].cash += trade_price * qty
                ask["player"].add_stock(s, -qty)

            if bid["amount"] == 0:
                self.bids.pop(0)
            if ask["amount"] == 0:
                self.asks.pop(0)
        #update stock price in self.stocks
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
    def __init__(self, cash=10000.0, stock=None):
        self.cash = cash
        self.stock = [dict(item) for item in stock] if stock else []  # list of {"name": str, "qty": int}

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

    def buy(self, market, s, price, amount):
        if self.cash >= price * amount:
            market.submitBuyOrder(s, price, amount, self)

    def sell(self, market, s, price, amount):
        if self.get_qty(s) >= amount:
            market.submitSellOrder(s, price, amount, self)


if __name__ == "__main__":
    # create n players, and submit buy and sell at random.
    market = exchange()
    s = stock(name="AAPL", price=100.0, numberOfStock=1000)

    n = 10
    players = [Player(stock=[{"name": s.name, "qty": 50}]) for _ in range(n)]

    for _ in range(100):
        p = random.choice(players)
        price = round(random.uniform(95, 105), 2)
        amount = random.randint(1, 10)
        if random.choice([True, False]):
            p.buy(market, s, price, amount)
        else:
            p.sell(market, s, price, amount)

    market.listbidask(s)
    for i, p in enumerate(players):
        print(f"Player {i + 1}: cash={p.cash:.2f}, stock={p.stock}")
