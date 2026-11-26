from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import random
import threading
import time
from urllib.parse import urlparse

from main import Player, exchange, stock


class LiveSimulation:
    """Manages a step-by-step live AAPL market simulation."""

    def __init__(self, n=50, duration=600.0):
        self.lock = threading.Lock()
        self.n = n
        self.duration = duration
        self.running = True
        self.speed = 1.0
        self.reset()

    def reset(self):
        with self.lock:
            self.market = exchange()
            self.stock = stock(name="AAPL", price=100.0, numberOfStock=1000)
            self.players = [
                Player(id=i + 1, stock=[{"name": self.stock.name, "qty": 50}])
                for i in range(self.n)
            ]
            self.manual_player = Player(
                id="YOU", cash=10000.0, stock=[{"name": self.stock.name, "qty": 50}]
            )
            self.next_times = [
                random.expovariate(max(p.epsilon, 0.05) * 2) for p in self.players
            ]
            self.t = 0.0
            self.market.history[self.stock.name] = [(0.0, self.stock.price)]

    def step(self, dt=0.25):
        with self.lock:
            if not self.running or self.t >= self.duration:
                if self.t >= self.duration:
                    self.running = False
                return

            target_t = min(self.duration, self.t + dt * self.speed)
            while True:
                idx = min(range(self.n), key=lambda i: self.next_times[i])
                ev_t = self.next_times[idx]
                if ev_t > target_t:
                    break
                p = self.players[idx]
                p.act(self.market, self.stock, t=ev_t)
                self.next_times[idx] = ev_t + random.expovariate(max(p.epsilon, 0.05) * 2)

            self.t = target_t
            if self.t >= self.duration:
                self.market.history[self.stock.name].append((self.duration, self.stock.price))
                self.running = False

    def submit_manual_order(self, side, price, amount):
        with self.lock:
            if price <= 0 or amount <= 0:
                return False, "Price and amount must be positive."
            if side == "buy":
                if self.manual_player.cash < price * amount:
                    return False, "Insufficient cash."
                self.manual_player.buy(self.market, self.stock, price, amount, t=self.t)
                return True, "Buy order submitted."
            if side == "sell":
                if self.manual_player.get_qty(self.stock) < amount:
                    return False, "Insufficient shares."
                self.manual_player.sell(self.market, self.stock, price, amount, t=self.t)
                return True, "Sell order submitted."
            return False, "Invalid side."

    def get_state(self):
        with self.lock:
            price = self.stock.price
            asks = [
                {"price": a["price"], "amount": a["amount"]}
                for a in self.market.asks[:10]
            ]
            bids = [
                {"price": b["price"], "amount": b["amount"]}
                for b in self.market.bids[:10]
            ]
            txs = list(reversed(self.market.transactions[-15:]))
            history = self.market.history.get(self.stock.name, [])
            if len(history) > 500:
                step = max(1, len(history) // 500)
                history = history[::step] + [history[-1]]

            leaderboard = []
            for p in self.players:
                qty = p.get_qty(self.stock)
                leaderboard.append({
                    "id": p.id,
                    "strategy": p.strategy["type"],
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
                "tx_count": len(self.market.transactions),
                "history": history,
                "asks": list(reversed(asks)),
                "bids": bids,
                "transactions": txs,
                "leaderboard": leaderboard[:10],
                "you": {
                    "cash": round(self.manual_player.cash, 2),
                    "qty": you_qty,
                    "value": round(self.manual_player.cash + you_qty * price, 2),
                },
            }


HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>AAPL Live Market Dashboard</title>
  <style>
    :root {
      --bg: #0d1117; --card: #161b22; --border: #30363d;
      --text: #e6edf3; --muted: #8b949e; --green: #3fb950;
      --red: #f85149; --blue: #58a6ff;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg); color: var(--text); padding: 16px; }
    header { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 12px; background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 14px 18px; margin-bottom: 16px; }
    .ticker { display: flex; align-items: baseline; gap: 14px; }
    .ticker h1 { font-size: 20px; }
    .price { font-size: 26px; font-weight: 700; color: var(--blue); }
    .meta { color: var(--muted); font-size: 13px; }
    .controls { display: flex; gap: 8px; align-items: center; }
    button, select, input { background: #21262d; color: var(--text); border: 1px solid var(--border); border-radius: 6px; padding: 6px 12px; font-size: 13px; cursor: pointer; }
    button:hover { border-color: var(--blue); }
    .btn-buy { background: rgba(63,185,80,0.18); border-color: var(--green); color: var(--green); }
    .btn-sell { background: rgba(248,81,73,0.18); border-color: var(--red); color: var(--red); }
    .grid { display: grid; grid-template-columns: 2fr 1fr; gap: 16px; margin-bottom: 16px; }
    .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
    .card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 14px; }
    .card h2 { font-size: 14px; color: var(--muted); margin-bottom: 10px; text-transform: uppercase; letter-spacing: 0.5px; }
    canvas { width: 100%; height: 320px; display: block; }
    table { width: 100%; border-collapse: collapse; font-size: 12px; }
    th, td { text-align: right; padding: 4px 6px; border-bottom: 1px solid #21262d; }
    th:first-child, td:first-child { text-align: left; }
    th { color: var(--muted); font-weight: 600; }
    .ask { color: var(--red); }
    .bid { color: var(--green); }
    .spread { text-align: center; padding: 6px; margin: 4px 0; background: #21262d; border-radius: 4px; font-weight: 600; font-size: 13px; }
    .trade-form { display: flex; gap: 6px; margin-top: 10px; flex-wrap: wrap; align-items: center; }
    .trade-form input { width: 80px; cursor: text; }
    .status-msg { font-size: 12px; color: var(--muted); margin-top: 6px; }
    @media (max-width: 900px) { .grid, .grid-2 { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
  <header>
    <div class="ticker">
      <h1 id="symbol">AAPL</h1>
      <span class="price" id="price">$100.00</span>
      <span class="meta" id="sim-time">Time: 0.0s / 600s</span>
      <span class="meta" id="tx-count">Trades: 0</span>
    </div>
    <div class="controls">
      <button id="toggle-btn" onclick="toggleRun()">Pause</button>
      <button onclick="postAction('/api/reset')">Reset</button>
      <select id="speed-sel" onchange="setSpeed(this.value)">
        <option value="1">1x</option>
        <option value="2">2x</option>
        <option value="5">5x</option>
        <option value="10">10x</option>
      </select>
    </div>
  </header>

  <div class="grid">
    <div class="card">
      <h2>Live Price Chart</h2>
      <canvas id="chart"></canvas>
    </div>
    <div class="card">
      <h2>Order Book &amp; Manual Trade</h2>
      <table>
        <thead><tr><th>Side</th><th>Price</th><th>Amount</th></tr></thead>
        <tbody id="asks-body"></tbody>
      </table>
      <div class="spread" id="mid-price">Last: $100.00</div>
      <table>
        <tbody id="bids-body"></tbody>
      </table>
      <div class="trade-form">
        <input type="number" id="ord-price" step="0.01" value="100.00" placeholder="Price" />
        <input type="number" id="ord-qty" step="1" min="1" value="5" placeholder="Qty" />
        <button class="btn-buy" onclick="placeOrder('buy')">Buy</button>
        <button class="btn-sell" onclick="placeOrder('sell')">Sell</button>
      </div>
      <div class="status-msg" id="you-info">YOU: $10000.00 | 50 shares</div>
      <div class="status-msg" id="ord-msg"></div>
    </div>
  </div>

  <div class="grid-2">
    <div class="card">
      <h2>Recent Transactions</h2>
      <table>
        <thead><tr><th>ID</th><th>Time</th><th>Price</th><th>Qty</th><th>Buyer</th><th>Seller</th></tr></thead>
        <tbody id="tx-body"></tbody>
      </table>
    </div>
    <div class="card">
      <h2>Top Players</h2>
      <table>
        <thead><tr><th>Player</th><th>Strategy</th><th>Target</th><th>Eps</th><th>Cash</th><th>Qty</th><th>Value</th></tr></thead>
        <tbody id="lb-body"></tbody>
      </table>
    </div>
  </div>

  <script>
    let isRunning = true;

    async function postAction(url, body = {}) {
      const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      return res.json();
    }

    function toggleRun() {
      postAction(isRunning ? '/api/pause' : '/api/start');
    }

    function setSpeed(val) {
      postAction('/api/speed', { speed: Number(val) });
    }

    async function placeOrder(side) {
      const price = Number(document.getElementById('ord-price').value);
      const amount = Number(document.getElementById('ord-qty').value);
      const res = await postAction('/api/order', { side, price, amount });
      document.getElementById('ord-msg').textContent = res.message || '';
    }

    function drawChart(history) {
      const canvas = document.getElementById('chart');
      const dpr = window.devicePixelRatio || 1;
      const w = canvas.clientWidth;
      const h = canvas.clientHeight;
      canvas.width = w * dpr;
      canvas.height = h * dpr;
      const ctx = canvas.getContext('2d');
      ctx.scale(dpr, dpr);
      ctx.clearRect(0, 0, w, h);

      if (!history || history.length < 2) return;

      const pad = { top: 16, right: 56, bottom: 24, left: 12 };
      const cw = w - pad.left - pad.right;
      const ch = h - pad.top - pad.bottom;

      const prices = history.map(p => p[1]);
      const times = history.map(p => p[0]);
      const minP = Math.min(...prices) - 0.5;
      const maxP = Math.max(...prices) + 0.5;
      const spanP = maxP - minP || 1;
      const maxT = Math.max(times[times.length - 1], 1);

      ctx.strokeStyle = '#21262d';
      ctx.fillStyle = '#8b949e';
      ctx.font = '11px monospace';
      for (let i = 0; i <= 4; i++) {
        const y = pad.top + (ch * i) / 4;
        const val = maxP - (spanP * i) / 4;
        ctx.beginPath();
        ctx.moveTo(pad.left, y);
        ctx.lineTo(w - pad.right, y);
        ctx.stroke();
        ctx.fillText(val.toFixed(2), w - pad.right + 6, y + 4);
      }

      ctx.strokeStyle = '#58a6ff';
      ctx.lineWidth = 2;
      ctx.beginPath();
      history.forEach(([t, p], idx) => {
        const x = pad.left + (t / maxT) * cw;
        const y = pad.top + ((maxP - p) / spanP) * ch;
        if (idx === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.stroke();
    }

    async function refresh() {
      try {
        const res = await fetch('/api/state');
        const data = await res.json();
        isRunning = data.running;
        document.getElementById('toggle-btn').textContent = isRunning ? 'Pause' : 'Start';
        document.getElementById('symbol').textContent = data.symbol;
        document.getElementById('price').textContent = `$${data.price.toFixed(2)}`;
        document.getElementById('sim-time').textContent = `Time: ${data.time.toFixed(1)}s / ${data.duration}s`;
        document.getElementById('tx-count').textContent = `Trades: ${data.tx_count}`;
        document.getElementById('mid-price').textContent = `Last: $${data.price.toFixed(2)}`;
        document.getElementById('you-info').textContent =
          `YOU: $${data.you.cash.toFixed(2)} | ${data.you.qty} shares | Total: $${data.you.value.toFixed(2)}`;

        document.getElementById('asks-body').innerHTML = data.asks
          .map(a => `<tr class="ask"><td>Ask</td><td>${a.price.toFixed(2)}</td><td>${a.amount}</td></tr>`)
          .join('');
        document.getElementById('bids-body').innerHTML = data.bids
          .map(b => `<tr class="bid"><td>Bid</td><td>${b.price.toFixed(2)}</td><td>${b.amount}</td></tr>`)
          .join('');
        document.getElementById('tx-body').innerHTML = data.transactions
          .map(tx => `<tr><td>#${tx.id}</td><td>${tx.time.toFixed(2)}s</td><td>${tx.price.toFixed(2)}</td><td>${tx.qty}</td><td>${tx.buyer_id}</td><td>${tx.seller_id}</td></tr>`)
          .join('');
        document.getElementById('lb-body').innerHTML = data.leaderboard
          .map(p => `<tr><td>P${p.id}</td><td>${p.strategy}</td><td>${p.target.toFixed(2)}</td><td>${p.eps.toFixed(2)}</td><td>${p.cash.toFixed(2)}</td><td>${p.qty}</td><td>${p.value.toFixed(2)}</td></tr>`)
          .join('');

        drawChart(data.history);
      } catch (e) {}
    }

    setInterval(refresh, 250);
    refresh();
  </script>
</body>
</html>
"""


def make_handler(sim: LiveSimulation):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def _send_json(self, payload, status=200):
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/":
                body = HTML_PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/state":
                self._send_json(sim.get_state())
            else:
                self._send_json({"error": "Not found"}, status=404)

        def do_POST(self):
            path = urlparse(self.path).path
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length > 0 else b"{}"
            body = json.loads(raw.decode("utf-8") or "{}")

            if path == "/api/start":
                sim.running = True
                self._send_json({"ok": True})
            elif path == "/api/pause":
                sim.running = False
                self._send_json({"ok": True})
            elif path == "/api/reset":
                sim.reset()
                sim.running = True
                self._send_json({"ok": True})
            elif path == "/api/speed":
                sim.speed = max(0.25, min(20.0, float(body.get("speed", 1.0))))
                self._send_json({"ok": True})
            elif path == "/api/order":
                ok, msg = sim.submit_manual_order(
                    body.get("side"), float(body.get("price", 0)), int(body.get("amount", 0))
                )
                self._send_json({"ok": ok, "message": msg}, status=200 if ok else 400)
            else:
                self._send_json({"error": "Not found"}, status=404)

    return Handler


def run_ticker(sim: LiveSimulation):
    while True:
        sim.step(dt=0.25)
        time.sleep(0.25)


if __name__ == "__main__":
    sim = LiveSimulation(n=50, duration=600.0)
    threading.Thread(target=run_ticker, args=(sim,), daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", 5005), make_handler(sim))
    print("Dashboard running at http://127.0.0.1:5005 (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
