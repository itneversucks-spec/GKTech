import json
import os
import socket
import sqlite3
import sys
import tempfile
import threading
import unittest
from configparser import ConfigParser

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import app as appmod
import db
import routeros
from werkzeug.security import generate_password_hash

HEADER = ("ID\tACCOUNT\tFNAME\tLNAME\tADDRESS\tCOORDINATES\tAREA\tPHONE\tEMAIL\tOVERRIDE\t"
          "INSTALLATION DATE\tREMARKS\tLAST DUEDATE\tSERVICE\tPROFILE\tUSERNAME\tPASSWORD\tLOCAL_IP\t"
          "REMOTE_IP\tHADDRESS\tPRODUCTS\tMRC\tSTARTDATE\tDUEDATE\tBILLDATE\tBALANCE\tBILLING\tCLIMIT\t"
          "STATE\tPROFILE_STATE\tOLT\tPON\tNAP\tPORT\tONU\tMAC\tREMARKS1\tREMARKS2\tVIDEO\tAGENT\tPAYOUT")
R1 = ("1\t427225\tMaggie\tAAAAA\tSan Vicente\t17.5,120.3\tLubong\t\t\t\t2022-10-19\t\t2026-Sep-30\tpppoe\tPlan799\tAAAAA\t"
      "SECRETPW1\t\t\t\tPlan799 (up to 40mbps)\t799\t2026-May-30\t2026-Oct-30\t2026-Nov-20\t799\tActive\t300\tEnabled\tPlan799")
R2 = ("2\t459157\tRoman\tRRRRR\tSan Vicente\t17.5,120.3\tLubong\t\t\t\t2022-10-19\t\t2026-Sep-30\tpppoe\tPlan599\tRRRRR\t"
      "SECRETPW2\t\t\t\tPlan599 (up to 10mbps)\t599\t2026-Jul-30\t2026-Oct-30\t2026-Oct-25\t0\tActive\t300\tEnabled\tPlan599")
CSV = "\n".join([HEADER, R1, R2] + ["\t" * 41] * 5) + "\n"


def make_app(tmp, pw="correct horse"):
    mock = os.path.join(tmp, "mock.json")
    json.dump({"10.69.1.10": "AAAAA", "10.69.1.11": "RRRRR", "10.69.1.12": "ZZZZZ"}, open(mock, "w"))
    cfg = ConfigParser(interpolation=None)
    cfg.read_dict({
        "server": {"secret_key": "t", "database": os.path.join(tmp, "t.db")},
        "admin": {"password_hash": generate_password_hash(pw), "blocked_nets": "10.69.0.0/23"},
        "router": {"mode": "mock", "mock_file": mock},
        "page": {"business_name": "TestISP", "gcash_name": "Juan D", "gcash_number": "0917 000 0000",
                 "contact": "Call 0917", "extra_note": "Send receipt."},
    })
    a = appmod.create_app(cfg)
    a.config["TESTING"] = True
    return a


class Importer(unittest.TestCase):
    def test_import_ignores_password_and_blank_rows(self):
        with tempfile.TemporaryDirectory() as t:
            con = db.connect(os.path.join(t, "x.db"))
            self.assertEqual(db.import_csv(con, CSV), (2, 0, 5))
            self.assertEqual(db.import_csv(con, CSV.encode()), (0, 2, 5))
            row = con.execute("SELECT * FROM customers WHERE username='AAAAA'").fetchone()
            self.assertEqual((row["balance"], row["mrc"], row["due_date"]), (799.0, 799.0, "2026-10-30"))
            self.assertEqual(row["bill_date"], "2026-11-20")
            dump = " ".join(str(v) for r in con.execute("SELECT * FROM customers") for v in tuple(r))
            self.assertNotIn("SECRETPW", dump)
            self.assertNotIn("San Vicente", dump)

    def test_bad_file(self):
        with tempfile.TemporaryDirectory() as t:
            con = db.connect(os.path.join(t, "x.db"))
            with self.assertRaises(ValueError):
                db.import_csv(con, "a,b\n1,2\n")


class Web(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.app = make_app(self.t.name)
        con = db.connect(self.app.config["DB"])
        db.import_csv(con, CSV)
        con.close()

    def tearDown(self):
        self.t.cleanup()

    def get(self, ip, path="/"):
        return self.app.test_client().get(path, environ_overrides={"REMOTE_ADDR": ip})

    def test_reminder_shows_own_account_only(self):
        r = self.get("10.69.1.10").get_data(as_text=True)
        self.assertIn("Hi Maggie", r)
        self.assertIn("₱799.00", r)
        self.assertIn("0917 000 0000", r)
        self.assertNotIn("Roman", r)
        self.assertNotIn("RRRRR", r)

    def test_paid_customer_gets_no_amount(self):
        r = self.get("10.69.1.11").get_data(as_text=True)
        self.assertIn("Hi Roman", r)
        self.assertIn("don't see a balance", r)

    def test_unknown_and_unmatched(self):
        self.assertIn("couldn't match", self.get("10.69.1.99").get_data(as_text=True))
        self.assertIn("couldn't match", self.get("10.69.1.12").get_data(as_text=True))  # session but not in db

    def test_admin_blocked_from_overdue_pool(self):
        self.assertEqual(self.get("10.69.1.10", "/admin").status_code, 404)
        self.assertEqual(self.get("10.69.1.10", "/admin/login").status_code, 404)

    def test_admin_requires_login_and_csrf(self):
        r = self.get("192.168.1.5", "/admin")
        self.assertEqual(r.status_code, 302)
        c = self.app.test_client()
        env = {"REMOTE_ADDR": "192.168.1.5"}
        bad = c.post("/admin/login", data={"password": "nope"}, environ_overrides=env)
        self.assertIn("Wrong password", bad.get_data(as_text=True))
        ok = c.post("/admin/login", data={"password": "correct horse"}, environ_overrides=env)
        self.assertEqual(ok.status_code, 302)
        page = c.get("/admin", environ_overrides=env).get_data(as_text=True)
        self.assertIn("Maggie", page)
        self.assertEqual(c.post("/admin/customer/1", data={"balance": "5"}, environ_overrides=env).status_code, 400)
        with c.session_transaction() as s:
            tok = s["csrf"]
        r = c.post("/admin/customer/1", data={"due_date": "2026-Oct-01", "note": "x", "_csrf": tok},
                   environ_overrides=env)
        self.assertEqual(r.status_code, 302)
        self.assertIn("Due 2026-10-01", self.get("10.69.1.10").get_data(as_text=True))
        self.assertIn("Hi Maggie", c.get("/admin/preview/1", environ_overrides=env).get_data(as_text=True))

    def test_overdue_logic(self):
        from datetime import date
        con = db.connect(self.app.config["DB"])
        a = con.execute("SELECT * FROM customers WHERE username='AAAAA'").fetchone()
        self.assertFalse(db.is_overdue(a, date(2026, 10, 24)))
        self.assertTrue(db.is_overdue(a, date(2026, 10, 31)))
        b = con.execute("SELECT * FROM customers WHERE username='RRRRR'").fetchone()
        self.assertFalse(db.is_overdue(b, date(2027, 1, 1)))


class Phase2(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.app = make_app(self.t.name)
        self.con = db.connect(self.app.config["DB"])
        db.import_csv(self.con, CSV)
        self.a = self.con.execute("SELECT id FROM customers WHERE username='AAAAA'").fetchone()["id"]
        self.r = self.con.execute("SELECT id FROM customers WHERE username='RRRRR'").fetchone()["id"]

    def tearDown(self):
        self.con.close()
        self.t.cleanup()

    def cust(self, cid):
        return self.con.execute("SELECT * FROM customers WHERE id=?", (cid,)).fetchone()

    def test_payment_clears_balance_and_advances_due(self):
        pid = db.record_payment(self.con, self.a, 799, "gcash", "gc 1234 5678", "2026-10-24")
        c = self.cust(self.a)
        self.assertEqual((c["balance"], c["due_date"]), (0.0, "2026-11-30"))
        p = self.con.execute("SELECT * FROM payments WHERE id=?", (pid,)).fetchone()
        self.assertEqual(p["reference"], "GC 1234 5678")

    def test_partial_payment_keeps_due_date(self):
        db.record_payment(self.con, self.a, 300, "cash")
        c = self.cust(self.a)
        self.assertEqual((c["balance"], c["due_date"]), (499.0, "2026-10-30"))

    def test_gcash_reference_unique_and_required(self):
        db.record_payment(self.con, self.a, 100, "gcash", "REF-AAAA1")
        with self.assertRaises(db.BillingError):
            db.record_payment(self.con, self.r, 100, "gcash", "ref-aaaa1")
        with self.assertRaises(db.BillingError):
            db.record_payment(self.con, self.r, 100, "gcash", "")
        self.assertEqual(self.cust(self.r)["balance"], 0.0)

    def test_double_click_guard(self):
        db.record_payment(self.con, self.a, 50, "cash")
        with self.assertRaises(db.BillingError):
            db.record_payment(self.con, self.a, 50, "cash")
        db.record_payment(self.con, self.a, 50, "cash", allow_duplicate=True)
        self.assertEqual(self.cust(self.a)["balance"], 699.0)

    def test_bad_amounts_rejected(self):
        for amt in (0, -5, "abc", 2_000_000):
            with self.assertRaises(db.BillingError):
                db.record_payment(self.con, self.a, db.parse_money(amt) if amt == "abc" else amt, "cash")

    def test_void_payment_restores_balance_and_due(self):
        pid = db.record_payment(self.con, self.a, 799, "cash")
        with self.assertRaises(db.BillingError):
            db.void_payment(self.con, pid, "")
        db.void_payment(self.con, pid, "typo")
        c = self.cust(self.a)
        self.assertEqual((c["balance"], c["due_date"]), (799.0, "2026-10-30"))
        with self.assertRaises(db.BillingError):
            db.void_payment(self.con, pid, "again")
        kinds = [r["kind"] for r in self.con.execute("SELECT kind FROM ledger WHERE customer_id=? ORDER BY id", (self.a,))]
        self.assertEqual(kinds, ["payment", "void"])

    def test_invoice_once_per_due_date_and_preview(self):
        self.assertEqual(db.add_month("2026-01-30", 30), "2026-02-28")
        rows = db.invoice_preview(self.con, "2026-10-31")
        flags = {c["username"]: already for c, already in rows}
        self.assertEqual(flags, {"AAAAA": True, "RRRRR": False})  # AAAAA already owes a month
        iid = db.create_invoice(self.con, self.r, 599, "2026-10-30")
        self.assertIsNotNone(iid)
        self.assertIsNone(db.create_invoice(self.con, self.r, 599, "2026-10-30"))
        self.assertEqual(self.cust(self.r)["balance"], 599.0)
        db.void_invoice(self.con, iid, "wrong plan")
        self.assertEqual(self.cust(self.r)["balance"], 0.0)

    def test_import_does_not_overwrite_money_unless_asked(self):
        db.record_payment(self.con, self.a, 799, "cash")
        db.import_csv(self.con, CSV)
        self.assertEqual(self.cust(self.a)["balance"], 0.0)
        db.import_csv(self.con, CSV, overwrite_money=True)
        self.assertEqual(self.cust(self.a)["balance"], 799.0)

    def test_adjust_needs_reason(self):
        with self.assertRaises(db.BillingError):
            db.adjust_balance(self.con, self.a, 0, "")
        db.adjust_balance(self.con, self.a, 100, "correction")
        self.assertEqual(self.cust(self.a)["balance"], 100.0)

    def test_web_flow_pay_receipt_report_csv(self):
        c = self.app.test_client()
        env = {"REMOTE_ADDR": "192.168.1.5"}
        c.post("/admin/login", data={"password": "correct horse"}, environ_overrides=env)
        c.get("/admin", environ_overrides=env)
        with c.session_transaction() as s:
            tok = s["csrf"]
        bad = c.post("/admin/customer/%d/pay" % self.a, data={"amount": "799", "method": "gcash", "reference": "", "_csrf": tok},
                     environ_overrides=env)
        self.assertEqual(bad.status_code, 302)
        self.assertEqual(self.cust(self.a)["balance"], 799.0)  # rejected: no GCash reference
        ok = c.post("/admin/customer/%d/pay" % self.a, data={"amount": "799", "method": "gcash", "reference": "GC99887766",
                                                             "received_on": "2026-10-24", "_csrf": tok}, environ_overrides=env)
        self.assertIn("/admin/receipt/", ok.headers["Location"])
        rec = c.get(ok.headers["Location"], environ_overrides=env).get_data(as_text=True)
        self.assertIn("GC99887766", rec)
        self.assertIn("2026-11-30", rec)
        self.assertIn("OR-000001", rec)
        rep = c.get("/admin/payments?from=2026-10-01&to=2026-10-31", environ_overrides=env).get_data(as_text=True)
        self.assertIn("₱799.00", rep)
        csvr = c.get("/admin/payments.csv?from=2026-10-01&to=2026-10-31", environ_overrides=env)
        self.assertIn("GC99887766", csvr.get_data(as_text=True))
        self.assertIn("text/csv", csvr.headers["Content-Type"])
        # paid customer's reminder page no longer asks for money
        self.assertIn("don't see a balance", self.app.test_client().get("/", environ_overrides={"REMOTE_ADDR": "10.69.1.10"}).get_data(as_text=True))
        # pages render
        for path in ("/admin/billing", "/admin/customer/%d" % self.a, "/admin/import"):
            self.assertEqual(c.get(path, environ_overrides=env).status_code, 200, path)
        # blocked pool can't reach any of it
        for path in ("/admin/billing", "/admin/payments", "/admin/receipt/1", "/admin/payments.csv"):
            self.assertIn(self.app.test_client().get(path, environ_overrides={"REMOTE_ADDR": "10.69.1.10"}).status_code, (404, 302))


class FakeRouter(threading.Thread):
    """Tiny RouterOS API server: /login then /ppp/active/print with ?address=."""

    def __init__(self):
        super().__init__(daemon=True)
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(5)
        self.port = self.srv.getsockname()[1]

    def run(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            api = routeros.Api.__new__(routeros.Api)
            api.sock = conn
            try:
                while True:
                    s = api._read_sentence()
                    if s[0] == "/login":
                        ok = "=name=billing-ro" in s and "=password=pw" in s
                        api._write_sentence(["!done"] if ok else ["!trap", "=message=cannot log in"])
                        if not ok:
                            api._write_sentence(["!done"])
                    elif s[0] == "/ppp/active/print":
                        want = [w for w in s if w.startswith("?address=")]
                        if want == ["?address=10.69.1.10"]:
                            api._write_sentence(["!re", "=name=AAAAA", "=address=10.69.1.10", "=" + "x" * 200 + "=1"])
                        api._write_sentence(["!done"])
            except Exception:
                conn.close()


class RouterApi(unittest.TestCase):
    def test_lookup_over_wire(self):
        fr = FakeRouter()
        fr.start()
        rows = routeros.query("127.0.0.1", fr.port, "billing-ro", "pw", "/ppp/active/print", {"address": "10.69.1.10"})
        self.assertEqual(rows[0]["name"], "AAAAA")
        self.assertEqual(routeros.query("127.0.0.1", fr.port, "billing-ro", "pw", "/ppp/active/print", {"address": "1.1.1.1"}), [])
        with self.assertRaises(routeros.RouterOSError):
            routeros.query("127.0.0.1", fr.port, "billing-ro", "wrong", "/ppp/active/print")
        import router
        r = router.ApiRouter("127.0.0.1", fr.port, "billing-ro", "pw")
        self.assertEqual(r.username_for_ip("10.69.1.10"), "AAAAA")
        self.assertIsNone(r.username_for_ip("10.69.1.55"))

    def test_length_encoding_roundtrip(self):
        for n in (0, 5, 127, 128, 300, 16383, 16384, 70000, 2097152):
            enc = routeros._encode_len(n)
            a = routeros.Api.__new__(routeros.Api)
            a._read = lambda k, buf=[enc]: (lambda r: (buf.__setitem__(0, buf[0][k:]), r)[1])(buf[0][:k])
            self.assertEqual(a._read_len(), n)


if __name__ == "__main__":
    unittest.main()
