from contextlib import closing, contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3

DB_PATH = Path(os.environ.get("MARKET_DB_PATH") or Path(__file__).with_name("market.db"))


@contextmanager
def get_conn():
    with closing(sqlite3.connect(DB_PATH, timeout=30.0)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
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

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                userid INTEGER NOT NULL,
                stock_name TEXT NOT NULL,
                side TEXT NOT NULL,
                time REAL NOT NULL,
                fill_price REAL NOT NULL,
                fill_qty INTEGER NOT NULL,
                user_balance REAL NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (order_id) REFERENCES orders(id),
                FOREIGN KEY (userid) REFERENCES users(id)
            )
            """
        )
        # Consolidate any legacy duplicate (order_id, fill_price) rows before enforcing unique index
        conn.execute("UPDATE fills SET fill_price = ROUND(fill_price, 2)")
        dup_check = conn.execute(
            "SELECT 1 FROM fills GROUP BY order_id, fill_price HAVING COUNT(*) > 1 LIMIT 1"
        ).fetchone()
        if dup_check is not None:
            conn.execute(
                """
                CREATE TEMP TABLE _dedup_fills AS
                SELECT MIN(id) AS keep_id, order_id, userid, stock_name, side,
                       MAX(time) AS time, fill_price, SUM(fill_qty) AS fill_qty,
                       user_balance, MAX(created_at) AS created_at
                FROM fills
                GROUP BY order_id, fill_price
                """
            )
            conn.execute("DELETE FROM fills")
            conn.execute(
                """
                INSERT INTO fills (id, order_id, userid, stock_name, side, time, fill_price, fill_qty, user_balance, created_at)
                SELECT keep_id, order_id, userid, stock_name, side, time, fill_price, fill_qty, user_balance, created_at
                FROM _dedup_fills
                """
            )
            conn.execute("DROP TABLE _dedup_fills")

        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_fills_order_price ON fills(order_id, fill_price)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_fills_userid ON fills(userid)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_userid ON orders(userid)")


def create_user(
    username: str,
    initial_cash: float,
    initial_shares: int = 0,
    stock_price: float = 100.0,
    allow_existing: bool = False,
):
    now = datetime.now(timezone.utc).isoformat()
    cost_basis = round(initial_cash + initial_shares * stock_price, 2)
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id, username, initial_cash, cash, shares, cost_basis FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        if existing is not None and allow_existing:
            user = dict(existing)
            if user.get("cash") is None:
                user["cash"] = user["initial_cash"]
            if user.get("shares") is None:
                user["shares"] = initial_shares
            if user.get("cost_basis") is None:
                user["cost_basis"] = round(user["initial_cash"] + user["shares"] * stock_price, 2)
            return user

        chosen_name = username
        if existing is not None:
            suffix = 2
            while True:
                candidate = f"{username[:26]}#{suffix}"
                taken = conn.execute("SELECT 1 FROM users WHERE username = ?", (candidate,)).fetchone()
                if taken is None:
                    chosen_name = candidate
                    break
                suffix += 1

        conn.execute(
            """
            INSERT INTO users (username, initial_cash, cash, shares, cost_basis, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (chosen_name, initial_cash, initial_cash, initial_shares, cost_basis, now),
        )
        row = conn.execute(
            "SELECT id, username, initial_cash, cash, shares, cost_basis FROM users WHERE username = ?",
            (chosen_name,),
        ).fetchone()
        return dict(row)


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


def record_fill(
    order_id: int,
    userid: int,
    stock_name: str,
    side: str,
    time: float,
    fill_price: float,
    fill_qty: int,
    user_balance: float,
):
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO fills (order_id, userid, stock_name, side, time, fill_price, fill_qty, user_balance, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(order_id, fill_price) DO UPDATE SET
                fill_qty = fills.fill_qty + excluded.fill_qty,
                time = excluded.time,
                user_balance = excluded.user_balance
            """,
            (order_id, userid, stock_name, side, time, round(fill_price, 2), int(fill_qty), round(user_balance, 2), now),
        )


def flush_db_batch(
    user_updates: list[tuple],
    order_updates: list[tuple],
    fill_deltas: list[tuple],
    cancels: list[tuple],
):
    if not (user_updates or order_updates or fill_deltas or cancels):
        return
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        if user_updates:
            conn.executemany(
                "UPDATE users SET cash = ?, shares = ?, cost_basis = ? WHERE id = ?",
                user_updates,
            )
        if order_updates:
            conn.executemany(
                """
                UPDATE orders
                SET filled_amount = ?, exec_price = ?, status = ?, user_balance = ?
                WHERE id = ?
                """,
                order_updates,
            )
        if fill_deltas:
            conn.executemany(
                """
                INSERT INTO fills (order_id, userid, stock_name, side, time, fill_price, fill_qty, user_balance, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(order_id, fill_price) DO UPDATE SET
                    fill_qty = fills.fill_qty + excluded.fill_qty,
                    time = excluded.time,
                    user_balance = excluded.user_balance
                """,
                [(*row, now) for row in fill_deltas],
            )
        for db_order_id, user_balance in cancels:
            row = conn.execute(
                "SELECT filled_amount FROM orders WHERE id = ?",
                (db_order_id,),
            ).fetchone()
            if row:
                status = "partial_cancelled" if row["filled_amount"] > 0 else "cancelled"
                conn.execute(
                    "UPDATE orders SET status = ?, user_balance = ? WHERE id = ?",
                    (status, round(user_balance, 2), db_order_id),
                )


def get_user_orders(userid: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, stock_name, side, order_type, time,
                   order_price, order_amount, filled_amount, exec_price,
                   status, user_balance, created_at
            FROM orders
            WHERE userid = ?
            ORDER BY id DESC
            """,
            (userid,),
        ).fetchall()
        fill_rows = conn.execute(
            """
            SELECT order_id, time, fill_price, fill_qty, user_balance
            FROM fills
            WHERE userid = ?
            ORDER BY id ASC
            """,
            (userid,),
        ).fetchall()
        fills_by_order: dict[int, list[dict]] = {}
        for fr in fill_rows:
            fills_by_order.setdefault(fr["order_id"], []).append({
                "time": fr["time"],
                "fill_price": fr["fill_price"],
                "fill_qty": fr["fill_qty"],
                "user_balance": fr["user_balance"],
            })
        orders = []
        for r in rows:
            d = dict(r)
            d["fills"] = fills_by_order.get(d["id"], [])
            orders.append(d)
        return orders
