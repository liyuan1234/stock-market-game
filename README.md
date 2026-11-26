# Stock Market Game

A limit-order-book stock market simulator with autonomous trading agents and a live FastAPI + WebSocket dashboard.

![AAPL Live Market Dashboard](assets/dashboard.png)

## Features

- **Limit Order Book Exchange (`main.py`):** Price-time priority matching, partial fills, resting-order execution pricing, transaction log, and price history.
- **Autonomous Players:** Each player has a unique activity rate (`epsilon`) and a strategy (`buy_below`, `sell_above`, or `buy_sell` around a target price), with configurable short selling.
- **Live Dashboard (`dashboard.py` + `index.html`):**
  - Real-time WebSocket updates (`/ws`)
  - Line chart and Candlestick chart (`1s`, `5s`, `10s`, `30s`, `1m`, `2m`, `5m`) with volume bars and hover OHLCV readout
  - Order book table (with order cancellation) and bid/ask depth histogram + bid/ask % bar
  - Manual Buy/Sell trading, live P&L tracker, cash/stock injection, and Bull/Bear/Random player spawning
- **Configuration (`config.json`):** Centralizes stock, player, simulation, and server settings.

## Quick Start

```bash
python3 -m venv .venv
.venv/bin/pip install matplotlib "fastapi[standard]"
```

Run the live dashboard (with hot-reload):

```bash
.venv/bin/fastapi dev dashboard.py --port 5005
```

Then open `http://127.0.0.1:5005` in your browser (or `http://127.0.0.1:5005/docs` for the REST API).

To run a standalone simulation and save a matplotlib plot to `images/`:

```bash
.venv/bin/python3 main.py
```
