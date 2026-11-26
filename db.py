from datetime import datetime, timezone
from pathlib import Path
import sqlite3

DB_PATH = Path(__file__).with_name("market.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                initial_cash REAL NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                userid INTEGER NOT NULL,
                stock_name TEXT NOT NULL,
                side TEXT NOT NULL,
                time REAL NOT NULL,
                order_price REAL NOT NULL,
                order_amount INTEGER NOT NULL,
                user_balance REAL NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (userid) REFERENCES users(id)
            )
            """
        )


def create_user(username: str, initial_cash: float):
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO users (username, initial_cash, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET initial_cash = excluded.initial_cash
            """,
            (username, initial_cash, now),
        )
        row = conn.execute(
            "SELECT id, username, initial_cash FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        return dict(row)


def record_order(
    userid: int,
    stock_name: str,
    side: str,
    time: float,
    order_price: float,
    order_amount: int,
    user_balance: float,
):
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO orders (userid, stock_name, side, time, order_price, order_amount, user_balance, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (userid, stock_name, side, time, order_price, order_amount, user_balance, now),
        )
