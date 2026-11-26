from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

DB_PATH = Path(__file__).with_name("market.db")


@contextmanager
def get_conn():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        with conn:
            yield conn


def init_db():
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                initial_cash REAL NOT NULL,
                cash REAL,
                shares INTEGER DEFAULT 0,
                cost_basis REAL,
                created_at TEXT NOT NULL
            )
            """
        )
        user_cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "cash" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN cash REAL")
            conn.execute("UPDATE users SET cash = initial_cash WHERE cash IS NULL")
        if "shares" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN shares INTEGER DEFAULT 0")
        if "cost_basis" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN cost_basis REAL")
            conn.execute("UPDATE users SET cost_basis = initial_cash WHERE cost_basis IS NULL")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                userid INTEGER NOT NULL,
                stock_name TEXT NOT NULL,
                side TEXT NOT NULL,
                order_type TEXT NOT NULL DEFAULT 'limit',
                time REAL NOT NULL,
                order_price REAL NOT NULL,
                order_amount INTEGER NOT NULL,
                filled_amount INTEGER NOT NULL DEFAULT 0,
                exec_price REAL NOT NULL DEFAULT 0.0,
                status TEXT NOT NULL DEFAULT 'open',
                user_balance REAL NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (userid) REFERENCES users(id)
            )
            """
        )
        order_cols = {r["name"] for r in conn.execute("PRAGMA table_info(orders)").fetchall()}
        if "order_type" not in order_cols:
            conn.execute("ALTER TABLE orders ADD COLUMN order_type TEXT NOT NULL DEFAULT 'limit'")
        if "filled_amount" not in order_cols:
            conn.execute("ALTER TABLE orders ADD COLUMN filled_amount INTEGER NOT NULL DEFAULT 0")
        if "exec_price" not in order_cols:
            conn.execute("ALTER TABLE orders ADD COLUMN exec_price REAL NOT NULL DEFAULT 0.0")
        if "status" not in order_cols:
            conn.execute("ALTER TABLE orders ADD COLUMN status TEXT NOT NULL DEFAULT 'open'")


def create_user(username: str, initial_cash: float, initial_shares: int = 0, stock_price: float = 100.0):
    now = datetime.now(timezone.utc).isoformat()
    cost_basis = round(initial_cash + initial_shares * stock_price, 2)
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id, username, initial_cash, cash, shares, cost_basis FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO users (username, initial_cash, cash, shares, cost_basis, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (username, initial_cash, initial_cash, initial_shares, cost_basis, now),
            )
            row = conn.execute(
                "SELECT id, username, initial_cash, cash, shares, cost_basis FROM users WHERE username = ?",
                (username,),
            ).fetchone()
            return dict(row)

        user = dict(existing)
        if user.get("cash") is None:
            user["cash"] = user["initial_cash"]
        if user.get("shares") is None:
            user["shares"] = initial_shares
        if user.get("cost_basis") is None:
            user["cost_basis"] = round(user["initial_cash"] + user["shares"] * stock_price, 2)
        return user


def get_user(user_id: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, username, initial_cash, cash, shares, cost_basis FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None


def save_user_state(
    user_id: int,
    cash: float,
    shares: int,
    cost_basis: float,
    initial_cash: float | None = None,
):
    with get_conn() as conn:
        if initial_cash is not None:
            conn.execute(
                "UPDATE users SET initial_cash = ?, cash = ?, shares = ?, cost_basis = ? WHERE id = ?",
                (round(initial_cash, 2), round(cash, 2), int(shares), round(cost_basis, 2), user_id),
            )
        else:
            conn.execute(
                "UPDATE users SET cash = ?, shares = ?, cost_basis = ? WHERE id = ?",
                (round(cash, 2), int(shares), round(cost_basis, 2), user_id),
            )


def record_order(
    userid: int,
    stock_name: str,
    side: str,
    time: float,
    order_price: float,
    order_amount: int,
    user_balance: float,
    order_type: str = "limit",
    filled_amount: int = 0,
    exec_price: float = 0.0,
    status: str = "open",
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO orders (
                userid, stock_name, side, order_type, time,
                order_price, order_amount, filled_amount, exec_price, status,
                user_balance, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                userid,
                stock_name,
                side,
                order_type,
                time,
                order_price,
                order_amount,
                filled_amount,
                exec_price,
                status,
                user_balance,
                now,
            ),
        )
        return int(cur.lastrowid)


def record_order_fill(
    db_order_id: int,
    filled_amount: int,
    exec_price: float,
    status: str,
    user_balance: float,
):
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE orders
            SET filled_amount = ?, exec_price = ?, status = ?, user_balance = ?
            WHERE id = ?
            """,
            (int(filled_amount), round(exec_price, 4), status, round(user_balance, 2), db_order_id),
        )


def record_order_cancel(db_order_id: int, user_balance: float):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT filled_amount FROM orders WHERE id = ?",
            (db_order_id,),
        ).fetchone()
        if not row:
            return
        status = "partial_cancelled" if row["filled_amount"] > 0 else "cancelled"
        conn.execute(
            "UPDATE orders SET status = ?, user_balance = ? WHERE id = ?",
            (status, round(user_balance, 2), db_order_id),
        )
