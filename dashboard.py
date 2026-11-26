import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import random
import threading
import time

from fastapi import Cookie, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
import uvicorn

from db import create_user, init_db, record_order
from main import CFG, Player, exchange, stock

_SIM_CFG = CFG.get("simulation", {})
_PLAYER_CFG = CFG.get("player", {})
_SERVER_CFG = CFG.get("server", {})


class LiveSimulation:
    """Manages a step-by-step live AAPL market simulation."""

    def __init__(self, n=None, duration=None):
        self.lock = threading.Lock()
        self.initial_n = _SIM_CFG.get("initial_players", 50) if n is None else n
        self.n = self.initial_n
        self.duration = _SIM_CFG.get("duration", None) if duration is None else duration
        self.running = True
        self.speed = 1.0
        self.current_user_id = None
        self.current_username = "YOU"
        init_db()
        self.reset()

    def reset(self):
        with self.lock:
            self.n = self.initial_n
            init_shares = _PLAYER_CFG.get("initial_shares", 50)
            self.market = exchange()
            self.stock = stock()
            self.players = [
                Player(id=i + 1, stock=[{"name": self.stock.name, "qty": init_shares}])
                for i in range(self.n)
            ]
            self.manual_player = Player(
                id="YOU", stock=[{"name": self.stock.name, "qty": init_shares}]
            )
            self.you_cost_basis = self.manual_player.cash + init_shares * self.stock.price
            self.next_times = [
                random.expovariate(max(p.epsilon, 0.05) * 2) for p in self.players
            ]
            self.t = 0.0
            self.market.history[self.stock.name] = [(0.0, self.stock.price)]

    def reset_players(self):
        with self.lock:
            self.n = self.initial_n
            init_shares = _PLAYER_CFG.get("initial_shares", 50)
            self.players = [
                Player(id=i + 1, stock=[{"name": self.stock.name, "qty": init_shares}])
                for i in range(self.n)
            ]
            self.next_times = [
                self.t + random.expovariate(max(p.epsilon, 0.05) * 2)
                for p in self.players
            ]
            self.market.bids = [b for b in self.market.bids if b["player"] is self.manual_player]
            self.market.asks = [a for a in self.market.asks if a["player"] is self.manual_player]
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

            while True:
                idx = min(range(self.n), key=lambda i: self.next_times[i])
                ev_t = self.next_times[idx]
                if ev_t > target_t:
                    break
                p = self.players[idx]
                p.act(self.market, self.stock, t=ev_t)
                self.next_times[idx] = ev_t + random.expovariate(max(p.epsilon, 0.05) * 2)

            self.t = target_t
            if self.duration is not None and self.t >= self.duration:
                self.market.history[self.stock.name].append((self.duration, self.stock.price))
                self.running = False

    def set_user(self, user_id: int, username: str, initial_cash: float):
        with self.lock:
            init_shares = _PLAYER_CFG.get("initial_shares", 50)
            self.current_user_id = user_id
            self.current_username = username
            self.manual_player = Player(
                id=username,
                cash=initial_cash,
                stock=[{"name": self.stock.name, "qty": init_shares}],
            )
            self.you_cost_basis = initial_cash + init_shares * self.stock.price

    def submit_manual_order(self, side, price, amount):
        with self.lock:
            if price <= 0 or amount <= 0:
                return False, "Price and amount must be positive."
            if side == "buy":
                if self.manual_player.cash < price * amount:
                    return False, "Insufficient cash."
                self.manual_player.buy(self.market, self.stock, price, amount, t=self.t)
                if self.current_user_id is not None:
                    record_order(
                        userid=self.current_user_id,
                        stock_name=self.stock.name,
                        side="buy",
                        time=round(self.t, 4),
                        order_price=price,
                        order_amount=amount,
                        user_balance=round(self.manual_player.cash, 2),
                    )
                return True, "Buy order submitted."
            if side == "sell":
                if not _PLAYER_CFG.get("allow_short_selling", True) and self.manual_player.get_qty(self.stock) < amount:
                    return False, "Insufficient shares."
                self.manual_player.sell(self.market, self.stock, price, amount, t=self.t)
                if self.current_user_id is not None:
                    record_order(
                        userid=self.current_user_id,
                        stock_name=self.stock.name,
                        side="sell",
                        time=round(self.t, 4),
                        order_price=price,
                        order_amount=amount,
                        user_balance=round(self.manual_player.cash, 2),
                    )
                return True, "Sell order submitted."
            return False, "Invalid side."

    def deposit(self, amount=1000000.0):
        with self.lock:
            self.manual_player.cash += amount
            self.you_cost_basis += amount
            return self.manual_player.cash

    def cancel_order(self, order_id):
        with self.lock:
            for book in (self.market.bids, self.market.asks):
                for i, order in enumerate(book):
                    if order["id"] == order_id and order["player"] is self.manual_player:
                        book.pop(i)
                        return True, f"Cancelled order #{order_id}."
            return False, "Order not found."

    def add_players(self, count=10, bias="random"):
        with self.lock:
            max_players = _SIM_CFG.get("max_players", 5000)
            allowed = max(0, max_players - len(self.players))
            count = min(count, allowed)
            if count <= 0:
                return self.n
            curr = self.stock.price
            init_shares = _PLAYER_CFG.get("initial_shares", 50)
            start_id = len(self.players) + 1
            for i in range(count):
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
                    id=start_id + i,
                    stock=[{"name": self.stock.name, "qty": init_shares}],
                    strategy=strategy,
                )
                self.players.append(p)
                self.next_times.append(
                    self.t + random.expovariate(max(p.epsilon, 0.05) * 2)
                )
            self.n = len(self.players)
            return self.n

    def inject_cash(self, amount):
        with self.lock:
            if amount <= 0:
                return False, "Amount must be positive."
            for p in self.players:
                p.cash += amount
            self.manual_player.cash += amount
            self.you_cost_basis += amount
            return True, f"Injected ${amount:,.2f} to all players."

    def inject_stock(self, qty=10):
        with self.lock:
            if qty <= 0:
                return False, "Quantity must be positive."
            for p in self.players:
                p.add_stock(self.stock, qty)
            self.manual_player.add_stock(self.stock, qty)
            self.you_cost_basis += qty * self.stock.price
            return True, f"Added {qty} {self.stock.name} shares to all players."

    def _build_candles(self, interval=60.0):
        interval = max(1.0, float(interval))
        buckets = {}
        for tx in self.market.transactions:
            b = int(tx["time"] // interval)
            buckets.setdefault(b, []).append(tx)

        max_bucket = int(self.t // interval)
        init_price = self.market.history.get(self.stock.name, [(0.0, 100.0)])[0][1]
        prev_close = init_price
        candles = []
        for b in range(max_bucket + 1):
            txs = buckets.get(b)
            if txs:
                o = txs[0]["price"]
                h = max(tx["price"] for tx in txs)
                l = min(tx["price"] for tx in txs)
                c = txs[-1]["price"]
                v = sum(tx["qty"] for tx in txs)
                prev_close = c
            else:
                o = h = l = c = prev_close
                v = 0
            candles.append({
                "time": b * interval,
                "open": round(o, 2),
                "high": round(h, 2),
                "low": round(l, 2),
                "close": round(c, 2),
                "volume": v,
            })
        return candles

    def get_state(self, interval=60.0):
        with self.lock:
            price = self.stock.price
            asks = [
                {
                    "id": a["id"],
                    "price": a["price"],
                    "amount": a["amount"],
                    "is_you": a["player"] is self.manual_player,
                }
                for a in self.market.asks[:10]
            ]
            bids = [
                {
                    "id": b["id"],
                    "price": b["price"],
                    "amount": b["amount"],
                    "is_you": b["player"] is self.manual_player,
                }
                for b in self.market.bids[:10]
            ]
            total_ask_qty = sum(a["amount"] for a in self.market.asks)
            total_bid_qty = sum(b["amount"] for b in self.market.bids)
            your_orders = [
                {"id": a["id"], "side": "ask", "price": a["price"], "amount": a["amount"]}
                for a in self.market.asks
                if a["player"] is self.manual_player
            ] + [
                {"id": b["id"], "side": "bid", "price": b["price"], "amount": b["amount"]}
                for b in self.market.bids
                if b["player"] is self.manual_player
            ]
            txs = list(reversed(self.market.transactions[-15:]))
            history = self.market.history.get(self.stock.name, [])
            if len(history) > 500:
                step = max(1, len(history) // 500)
                history = history[::step] + [history[-1]]
            candles = self._build_candles(interval=interval)

            leaderboard = []
            breakdown = {"bull": 0, "bear": 0, "both": 0}
            for p in self.players:
                stype = p.strategy["type"]
                if stype == "buy_below":
                    breakdown["bull"] += 1
                elif stype == "sell_above":
                    breakdown["bear"] += 1
                else:
                    breakdown["both"] += 1
                qty = p.get_qty(self.stock)
                leaderboard.append({
                    "id": p.id,
                    "strategy": stype,
                    "target": p.strategy["target_price"],
                    "eps": round(p.epsilon, 2),
                    "cash": round(p.cash, 2),
                    "qty": qty,
                    "value": round(p.cash + qty * price, 2),
                })
            leaderboard.sort(key=lambda x: -x["value"])

            you_qty = self.manual_player.get_qty(self.stock)
            return {
                "symbol": self.stock.name,
                "price": round(price, 2),
                "time": round(self.t, 2),
                "duration": self.duration,
                "running": self.running,
                "speed": self.speed,
                "player_count": self.n,
                "player_breakdown": breakdown,
                "tx_count": len(self.market.transactions),
                "history": history,
                "candles": candles,
                "asks": list(reversed(asks)),
                "bids": bids,
                "all_asks": [[a["price"], a["amount"]] for a in self.market.asks],
                "all_bids": [[b["price"], b["amount"]] for b in self.market.bids],
                "total_ask_qty": total_ask_qty,
                "total_bid_qty": total_bid_qty,
                "your_orders": your_orders,
                "transactions": txs,
                "leaderboard": leaderboard[:10],
                "you": {
                    "username": self.current_username,
                    "cash": round(self.manual_player.cash, 2),
                    "qty": you_qty,
                    "value": round(self.manual_player.cash + you_qty * price, 2),
                    "pnl": round((self.manual_player.cash + you_qty * price) - self.you_cost_basis, 2),
                    "pnl_pct": round((((self.manual_player.cash + you_qty * price) - self.you_cost_basis) / self.you_cost_basis) * 100, 2) if self.you_cost_basis else 0.0,
                },
            }


sim = LiveSimulation()


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
    price: float
    amount: int


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


@app.post("/api/login")
def login(req: LoginReq):
    username = req.username.strip()
    if not username:
        return JSONResponse({"ok": False, "message": "Username is required."}, status_code=400)
    if req.initial_amount <= 0:
        return JSONResponse({"ok": False, "message": "Initial amount must be positive."}, status_code=400)
    user = create_user(username, req.initial_amount)
    sim.set_user(user["id"], user["username"], user["initial_cash"])
    resp = JSONResponse({"ok": True, "user": user})
    resp.set_cookie(key="userid", value=str(user["id"]), httponly=True, samesite="lax")
    return resp


@app.post("/api/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("userid")
    return resp


@app.get("/", response_class=HTMLResponse)
def index(userid: str | None = Cookie(default=None)):
    if not userid or sim.current_user_id is None:
        return RedirectResponse(url="/login", status_code=302)
    html = Path(__file__).with_name("index.html").read_text(encoding="utf-8")
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/api/state")
def get_state(interval: float = 60.0):
    return sim.get_state(interval=interval)


@app.post("/api/start")
def start_sim():
    sim.running = True
    return {"ok": True}


@app.post("/api/pause")
def pause_sim():
    sim.running = False
    return {"ok": True}


@app.post("/api/reset")
def reset_sim():
    sim.reset()
    sim.running = True
    return {"ok": True}


@app.post("/api/speed")
def set_speed(req: SpeedReq):
    sim.speed = max(0.25, min(20.0, req.speed))
    return {"ok": True}


@app.post("/api/order")
def place_order(req: OrderReq):
    ok, msg = sim.submit_manual_order(req.side, req.price, req.amount)
    return JSONResponse({"ok": ok, "message": msg}, status_code=200 if ok else 400)


@app.post("/api/deposit")
def deposit(req: DepositReq):
    cash = sim.deposit(req.amount)
    return {"ok": True, "cash": cash, "message": f"Added ${req.amount:,.0f} to YOU."}


@app.post("/api/add_players")
def add_players(req: AddPlayersReq):
    total = sim.add_players(req.count, bias=req.bias)
    return {"ok": True, "total_players": total}


@app.post("/api/cancel")
def cancel_order(req: CancelReq):
    ok, msg = sim.cancel_order(req.order_id)
    return JSONResponse({"ok": ok, "message": msg}, status_code=200 if ok else 404)


@app.post("/api/inject")
def inject_cash(req: InjectReq):
    ok, msg = sim.inject_cash(req.amount)
    return JSONResponse({"ok": ok, "message": msg}, status_code=200 if ok else 400)


@app.post("/api/inject_stock")
def inject_stock(req: InjectStockReq):
    ok, msg = sim.inject_stock(req.qty)
    return JSONResponse({"ok": ok, "message": msg}, status_code=200 if ok else 400)


@app.post("/api/reset_players")
def reset_players():
    total = sim.reset_players()
    return {"ok": True, "total_players": total, "message": f"Reset to {total} players."}


@app.websocket("/ws")
async def ws_state(websocket: WebSocket, interval: float = 60.0):
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
            await websocket.send_json(sim.get_state(interval=state["interval"]))
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
