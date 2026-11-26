import contextlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import db
import dashboard
from main import Player, exchange, run_simulation, stock


class TestExchangeAndEscrow(unittest.TestCase):
    def setUp(self):
        self.market = exchange()
        self.s = stock(name="AAPL", price=100.0, numberOfStock=1000)

    def test_price_time_priority_and_resting_price(self):
        seller1 = Player(id="S1", cash=1000.0, stock=[{"name": "AAPL", "qty": 20}], is_bot=False)
        seller2 = Player(id="S2", cash=1000.0, stock=[{"name": "AAPL", "qty": 20}], is_bot=False)
        buyer = Player(id="B1", cash=5000.0, stock=[], is_bot=False)

        seller1.sell(self.market, self.s, 100.0, 5, t=1.0)
        seller2.sell(self.market, self.s, 100.0, 5, t=2.0)

        # Buyer crosses at $105; must execute at resting ask price ($100) in FIFO order
        ok, _, order = buyer.buy(self.market, self.s, 105.0, 7, t=3.0)
        self.assertTrue(ok)
        self.assertEqual(order["filled_qty"], 7)
        self.assertAlmostEqual(buyer.cash, 5000.0 - 700.0)
        self.assertAlmostEqual(buyer.reserved_cash, 0.0)
        self.assertEqual(buyer.get_qty(self.s), 7)
        self.assertEqual(seller1.get_qty(self.s), 15)
        self.assertEqual(seller2.get_qty(self.s), 18)
        self.assertEqual(self.market.asks[0]["amount"], 3)

    def test_buy_escrow_prevents_double_spend_and_restores_on_cancel(self):
        buyer = Player(id="B1", cash=1000.0, stock=[], is_bot=False)
        ok1, _, ord1 = buyer.buy(self.market, self.s, 100.0, 6, t=1.0)
        self.assertTrue(ok1)
        # Equity stays $1000 while order rests, but available cash drops to $400
        self.assertAlmostEqual(buyer.equity(self.s), 1000.0)
        self.assertAlmostEqual(buyer.available_cash(self.s), 400.0)

        # Second order exceeding remaining $400 available cash is rejected
        ok2, msg2, _ = buyer.buy(self.market, self.s, 100.0, 5, t=2.0)
        self.assertFalse(ok2)
        self.assertIn("Insufficient available cash", msg2)

        # Cancelling restores full available cash
        cancelled, _ = self.market.cancel_order(ord1["id"], player=buyer)
        self.assertTrue(cancelled)
        self.assertAlmostEqual(buyer.available_cash(self.s), 1000.0)
        self.assertAlmostEqual(buyer.reserved_cash, 0.0)

    def test_sell_share_escrow_when_short_selling_disabled(self):
        seller = Player(
            id="S1",
            cash=1000.0,
            stock=[{"name": "AAPL", "qty": 10}],
            is_bot=False,
            allow_short_selling=False,
        )
        ok1, _, ord1 = seller.sell(self.market, self.s, 105.0, 7, t=1.0)
        self.assertTrue(ok1)
        self.assertEqual(seller.available_shares(self.s), 3)

        # Cannot oversell beyond remaining 3 shares
        ok2, _, _ = seller.sell(self.market, self.s, 106.0, 5, t=2.0)
        self.assertFalse(ok2)

        # Cancel restores share availability
        self.market.cancel_order(ord1["id"], player=seller)
        self.assertEqual(seller.available_shares(self.s), 10)

    def test_short_margin_and_cover(self):
        shorter = Player(
            id="SH",
            cash=1000.0,
            stock=[],
            is_bot=False,
            allow_short_selling=True,
        )
        buyer = Player(id="B", cash=50000.0, stock=[], is_bot=False)
        buyer.buy(self.market, self.s, 100.0, 100, t=1.0)

        # With $1,000 cash and 50% initial margin, max short at $100 is 20 shares ($1,000 margin)
        ok_too_big, _, _ = shorter.sell(self.market, self.s, 100.0, 25, t=2.0)
        self.assertFalse(ok_too_big)

        ok_valid, _, _ = shorter.sell(self.market, self.s, 100.0, 15, t=2.0)
        self.assertTrue(ok_valid)
        self.assertEqual(shorter.get_qty(self.s), -15)
        self.assertAlmostEqual(shorter.cash, 2500.0)
        # Covering the 15 short shares should succeed using locked short proceeds
        seller = Player(id="S", cash=1000.0, stock=[{"name": "AAPL", "qty": 20}], is_bot=False)
        self.market.cancel_player_orders(buyer)
        seller.sell(self.market, self.s, 100.0, 15, t=3.0)
        ok_cover, _, _ = shorter.buy(self.market, self.s, 100.0, 15, t=4.0)
        self.assertTrue(ok_cover)
        self.assertEqual(shorter.get_qty(self.s), 0)
        self.assertAlmostEqual(shorter.cash, 1000.0)

    def test_self_trade_prevention_cancels_resting_order(self):
        trader = Player(id="T1", cash=10000.0, stock=[{"name": "AAPL", "qty": 20}], is_bot=False)
        trader.sell(self.market, self.s, 100.0, 5, t=1.0)
        self.assertEqual(len(self.market.asks), 1)

        # Crossing own ask cancels resting ask instead of generating a wash trade
        trader.buy(self.market, self.s, 101.0, 5, t=2.0)
        self.assertEqual(self.market.total_tx_count, 0)
        self.assertEqual(len(self.market.asks), 0)
        self.assertEqual(len(self.market.bids), 1)
        self.assertEqual(trader.available_shares(self.s), 20)

    def test_market_orders(self):
        s1 = Player(id="S1", cash=1000.0, stock=[{"name": "AAPL", "qty": 10}], is_bot=False)
        s2 = Player(id="S2", cash=1000.0, stock=[{"name": "AAPL", "qty": 10}], is_bot=False)
        buyer = Player(id="B1", cash=2000.0, stock=[], is_bot=False)

        s1.sell(self.market, self.s, 100.0, 4, t=1.0)
        s2.sell(self.market, self.s, 102.0, 6, t=2.0)

        ok, msg, order = self.market.submitMarketOrder(self.s, "buy", 6, player=buyer, t=3.0)
        self.assertTrue(ok)
        self.assertEqual(order["filled_qty"], 6)
        self.assertAlmostEqual(buyer.cash, 2000.0 - (4 * 100.0 + 2 * 102.0))
        self.assertIn("Market buy filled 6", msg)

    def test_simulation_recovers_from_extreme_price_shock_and_zero_bots(self):
        # Zero bots must not crash
        hist0, _, tx0 = run_simulation(n=0, duration=5.0)
        self.assertEqual(tx0, 0)
        self.assertGreaterEqual(len(hist0), 2)

        # Extreme price shock well above [95, 105] must still generate trades (no deadlock)
        sim_inst = dashboard.LiveSimulation(n=30)
        sim_inst.stock.price = 180.0
        sim_inst.step(dt=15.0)
        self.assertGreater(sim_inst.market.total_tx_count, 0)


class TestDashboardSessionAndPersistence(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test_market.db"
        self.db_patch = patch.object(db, "DB_PATH", self.db_path)
        self.db_patch.start()
        db.init_db()
        self.sim = dashboard.LiveSimulation(n=10)
        self.sim.running = False
        self.sim_patch = patch.object(dashboard, "sim", self.sim)
        self.sim_patch.start()
        self.client = TestClient(dashboard.app)

    def tearDown(self):
        self.sim_patch.stop()
        self.db_patch.stop()
        self.tmpdir.cleanup()

    def test_auth_protection_and_multiuser_isolation_and_reset(self):
        # Unauthenticated state request is rejected
        r_unauth = self.client.get("/api/state")
        self.assertEqual(r_unauth.status_code, 401)

        # Forged unsigned cookie is rejected
        self.client.cookies.set("userid", "1.forgedsig")
        self.assertEqual(self.client.get("/api/state").status_code, 401)

        # Login Alice with custom initial amount
        c_alice = TestClient(dashboard.app)
        r_login_a = c_alice.post("/api/login", json={"username": "<Alice>", "initial_amount": 50000.0})
        self.assertEqual(r_login_a.status_code, 200)

        # Login Bob in separate client
        c_bob = TestClient(dashboard.app)
        r_login_b = c_bob.post("/api/login", json={"username": "Bob", "initial_amount": 80000.0})
        self.assertEqual(r_login_b.status_code, 200)

        state_a = c_alice.get("/api/state").json()
        state_b = c_bob.get("/api/state").json()
        self.assertEqual(state_a["you"]["username"], "<Alice>")
        self.assertAlmostEqual(state_a["you"]["cash"], 50000.0)
        self.assertEqual(state_b["you"]["username"], "Bob")
        self.assertAlmostEqual(state_b["you"]["cash"], 80000.0)

        # Alice & Bob trade with each other; SQLite records actual fills
        r_sell = c_bob.post("/api/order", json={"side": "sell", "price": 101.0, "amount": 10, "order_type": "limit"})
        self.assertEqual(r_sell.status_code, 200)
        r_buy = c_alice.post("/api/order", json={"side": "buy", "amount": 10, "order_type": "market"})
        self.assertEqual(r_buy.status_code, 200)

        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            rows = conn.execute("SELECT side, order_type, filled_amount, status FROM orders ORDER BY id").fetchall()
        self.assertEqual(rows[0], ("sell", "limit", 10, "filled"))
        self.assertEqual(rows[1], ("buy", "market", 10, "filled"))

        # Reset preserves Alice's username and $50,000 initial_amount instead of reverting to YOU / $1M
        c_alice.post("/api/reset")
        after_reset = c_alice.get("/api/state").json()
        self.assertEqual(after_reset["you"]["username"], "<Alice>")
        self.assertAlmostEqual(after_reset["you"]["cash"], 50000.0)

    def test_player_short_config_independent_of_bot_short_config(self):
        c = TestClient(dashboard.app)
        c.post("/api/login", json={"username": "Trader", "initial_amount": 100000.0})
        with patch.dict(dashboard._PLAYER_CFG, {"allow_short_selling": True}), patch.dict(
            dashboard._BOT_CFG, {"allow_short_selling": False}
        ):
            res = c.post("/api/order", json={"side": "sell", "price": 105.0, "amount": 5, "order_type": "limit"})
            self.assertEqual(res.status_code, 200)
            state = c.get("/api/state").json()
            self.assertEqual(len(state["your_orders"]), 1)

    def test_my_transactions_endpoint_and_version_tracking(self):
        self.assertEqual(self.client.get("/api/my_transactions").status_code, 401)

        c_alice = TestClient(dashboard.app)
        c_bob = TestClient(dashboard.app)
        c_alice.post("/api/login", json={"username": "AliceTx", "initial_amount": 100000.0})
        c_bob.post("/api/login", json={"username": "BobTx", "initial_amount": 100000.0})

        s0 = c_alice.get("/api/state").json()
        v0 = s0["my_tx_version"]
        self.assertEqual(c_alice.get("/api/my_transactions").json()["transactions"], [])

        # Alice posts resting sell limit; version bumps and order is open
        c_alice.post("/api/order", json={"side": "sell", "price": 102.0, "amount": 8, "order_type": "limit"})
        s1 = c_alice.get("/api/state").json()
        self.assertGreater(s1["my_tx_version"], v0)
        txs1 = c_alice.get("/api/my_transactions").json()["transactions"]
        self.assertEqual(len(txs1), 1)
        self.assertEqual(txs1[0]["status"], "open")
        self.assertEqual(txs1[0]["filled_amount"], 0)

        # Bob partially fills 5 of Alice's 8 shares; Alice's my_tx_version bumps automatically
        c_bob.post("/api/order", json={"side": "buy", "amount": 5, "order_type": "market"})
        s2 = c_alice.get("/api/state").json()
        self.assertGreater(s2["my_tx_version"], s1["my_tx_version"])
        txs2 = c_alice.get("/api/my_transactions").json()["transactions"]
        self.assertEqual(txs2[0]["status"], "partial")
        self.assertEqual(txs2[0]["filled_amount"], 5)
        self.assertAlmostEqual(txs2[0]["exec_price"], 102.0)

        # Bob only sees Bob's own transaction, not Alice's
        bob_txs = c_bob.get("/api/my_transactions").json()["transactions"]
        self.assertEqual(len(bob_txs), 1)
        self.assertEqual(bob_txs[0]["side"], "buy")
        self.assertEqual(bob_txs[0]["order_type"], "market")

        # Reset cancels Alice's remaining open shares in DB and bumps version
        c_alice.post("/api/reset")
        s3 = c_alice.get("/api/state").json()
        self.assertGreater(s3["my_tx_version"], s2["my_tx_version"])
        txs3 = c_alice.get("/api/my_transactions").json()["transactions"]
        self.assertEqual(txs3[0]["status"], "partial_cancelled")


if __name__ == "__main__":
    unittest.main()
