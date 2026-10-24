import csv
import io
import sqlite3
from datetime import date, datetime, timedelta

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account TEXT,
  username TEXT UNIQUE NOT NULL,
  first_name TEXT, last_name TEXT, phone TEXT, area TEXT,
  plan TEXT, mrc REAL DEFAULT 0,
  due_date TEXT, bill_date TEXT,
  balance REAL DEFAULT 0,
  state TEXT, profile_state TEXT,
  note TEXT DEFAULT '',
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS invoices (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  customer_id INTEGER NOT NULL, period TEXT NOT NULL, amount REAL NOT NULL,
  issued_on TEXT, status TEXT DEFAULT 'open', note TEXT DEFAULT '',
  UNIQUE(customer_id, period)
);
CREATE TABLE IF NOT EXISTS payments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  customer_id INTEGER NOT NULL, amount REAL NOT NULL, method TEXT NOT NULL,
  reference TEXT DEFAULT '', received_on TEXT NOT NULL, note TEXT DEFAULT '',
  created_at TEXT, voided INTEGER DEFAULT 0, void_reason TEXT DEFAULT '',
  due_before TEXT, due_after TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_gcash_ref ON payments(reference)
  WHERE method='gcash' AND voided=0 AND reference<>'';
CREATE INDEX IF NOT EXISTS ix_pay_cust ON payments(customer_id);
CREATE TABLE IF NOT EXISTS ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  customer_id INTEGER NOT NULL, ts TEXT, kind TEXT, delta REAL,
  balance_after REAL, ref TEXT DEFAULT '', note TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_led_cust ON ledger(customer_id);
CREATE TABLE IF NOT EXISTS lookups (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT, ip TEXT, username TEXT, result TEXT
);
"""

# Only these CSV columns are ever read. PASSWORD, addresses, coordinates etc. are ignored.
WANTED = {
    "ACCOUNT": "account", "USERNAME": "username", "FNAME": "first_name",
    "LNAME": "last_name", "PHONE": "phone", "AREA": "area", "PROFILE": "plan",
    "MRC": "mrc", "DUEDATE": "due_date", "BILLDATE": "bill_date",
    "BALANCE": "balance", "STATE": "state", "PROFILE_STATE": "profile_state",
}
DATE_FORMATS = ("%Y-%b-%d", "%Y-%m-%d", "%m/%d/%Y", "%d-%b-%Y", "%Y/%m/%d")


def connect(path):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    cols = [r["name"] for r in con.execute("PRAGMA table_info(customers)")]
    if "billing_day" not in cols:
        con.execute("ALTER TABLE customers ADD COLUMN billing_day INTEGER")
        con.commit()
    return con


def parse_date(s):
    s = (s or "").strip()
    if not s:
        return None
    for f in DATE_FORMATS:
        try:
            return datetime.strptime(s, f).date().isoformat()
        except ValueError:
            pass
    return None


def parse_money(s):
    s = (s or "").replace(",", "").replace("₱", "").strip()
    try:
        return float(s)
    except ValueError:
        return 0.0


def import_csv(con, raw, overwrite_money=False):
    """raw: bytes or str of the export. Returns (added, updated, skipped).

    Existing customers keep their balance and due date (they may hold payments recorded here)
    unless overwrite_money=True."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8-sig", "replace")
    first = raw.splitlines()[0] if raw.strip() else ""
    delim = "\t" if first.count("\t") >= first.count(",") else ","
    reader = csv.reader(io.StringIO(raw), delimiter=delim)
    header = next(reader, [])
    idx = {h.strip().upper(): i for i, h in enumerate(header)}
    if "USERNAME" not in idx:
        raise ValueError("No USERNAME column found - is this the customer export?")
    cols = {WANTED[h]: idx[h] for h in WANTED if h in idx}
    added = updated = skipped = 0
    now = datetime.now().isoformat(timespec="seconds")
    for row in reader:
        def get(k):
            i = cols.get(k)
            return row[i].strip() if i is not None and i < len(row) else ""
        user = get("username")
        if not user:
            skipped += 1
            continue
        vals = dict(
            account=get("account"), first_name=get("first_name"), last_name=get("last_name"),
            phone=get("phone"), area=get("area"), plan=get("plan"),
            mrc=parse_money(get("mrc")), due_date=parse_date(get("due_date")),
            bill_date=parse_date(get("bill_date")), balance=parse_money(get("balance")),
            state=get("state"), profile_state=get("profile_state"), updated_at=now,
        )
        exists = con.execute("SELECT id FROM customers WHERE username=?", (user,)).fetchone()
        vals["billing_day"] = int(vals["due_date"][8:10]) if vals["due_date"] else None
        if exists:
            if not overwrite_money:
                for k in ("balance", "due_date", "bill_date", "billing_day"):
                    vals.pop(k)
            sets = ",".join(k + "=?" for k in vals)
            con.execute("UPDATE customers SET %s WHERE id=?" % sets, list(vals.values()) + [exists["id"]])
            updated += 1
        else:
            keys = ",".join(["username"] + list(vals))
            q = ",".join("?" * (len(vals) + 1))
            con.execute("INSERT INTO customers(%s) VALUES (%s)" % (keys, q), [user] + list(vals.values()))
            added += 1
    con.commit()
    return added, updated, skipped


def is_overdue(c, today=None):
    today = (today or date.today()).isoformat()
    return (c["balance"] or 0) > 0 and bool(c["due_date"]) and c["due_date"] < today


def log_lookup(con, ip, username, result):
    con.execute("INSERT INTO lookups(ts,ip,username,result) VALUES (?,?,?,?)",
                (datetime.now().isoformat(timespec="seconds"), ip, username, result))
    con.execute("DELETE FROM lookups WHERE id <= (SELECT MAX(id) FROM lookups) - 500")
    con.commit()


# ---------------------------------------------------------------- Phase 2: money
from calendar import monthrange
import sqlite3

METHODS = ("gcash", "cash", "other")


class BillingError(Exception):
    pass


def money(x):
    return round(float(x or 0), 2)


def add_month(iso, day=None):
    y, m, d = (int(p) for p in iso.split("-"))
    day = day or d
    m += 1
    if m > 12:
        y, m = y + 1, 1
    return "%04d-%02d-%02d" % (y, m, min(day, monthrange(y, m)[1]))


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _customer(con, cid):
    c = con.execute("SELECT * FROM customers WHERE id=?", (cid,)).fetchone()
    if not c:
        raise BillingError("Customer not found.")
    return c


def _post(con, cid, kind, delta, ref="", note=""):
    """Change a customer's balance and write the ledger line. Caller commits."""
    c = _customer(con, cid)
    new = money((c["balance"] or 0) + delta)
    con.execute("UPDATE customers SET balance=?, updated_at=? WHERE id=?", (new, _now(), cid))
    con.execute("INSERT INTO ledger(customer_id,ts,kind,delta,balance_after,ref,note) VALUES (?,?,?,?,?,?,?)",
                (cid, _now(), kind, money(delta), new, ref, note))
    return new


def record_payment(con, cid, amount, method, reference="", received_on=None, note="",
                   advance_due=True, allow_duplicate=False):
    amount = money(amount)
    method = (method or "").lower()
    reference = " ".join((reference or "").split()).upper()
    received_on = parse_date(received_on) or date.today().isoformat()
    if not (0 < amount <= 1_000_000):
        raise BillingError("Enter an amount greater than zero.")
    if method not in METHODS:
        raise BillingError("Choose how it was paid.")
    if method == "gcash" and len(reference) < 6:
        raise BillingError("GCash payments need the reference number from the receipt.")
    c = _customer(con, cid)
    if not allow_duplicate:
        dup = con.execute(
            "SELECT 1 FROM payments WHERE customer_id=? AND amount=? AND method=? AND voided=0 "
            "AND created_at >= ?", (cid, amount, method,
                                    (datetime.now() - timedelta(minutes=2)).isoformat(timespec="seconds"))).fetchone()
        if dup:
            raise BillingError("The same payment was recorded a moment ago. Tick 'record again anyway' if it is a second payment.")
    before_bal = money(c["balance"])
    due_before = c["due_date"]
    due_after = due_before
    try:
        new_bal = _post(con, cid, "payment", -amount, reference or method, note)
        if advance_due and before_bal > 0 and new_bal <= 0 and due_before:
            due_after = add_month(due_before, c["billing_day"])
            con.execute("UPDATE customers SET due_date=? WHERE id=?", (due_after, cid))
        cur = con.execute(
            "INSERT INTO payments(customer_id,amount,method,reference,received_on,note,created_at,due_before,due_after)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (cid, amount, method, reference, received_on, note, _now(), due_before, due_after))
        # mark open invoices paid, oldest first, while the balance allows
        if new_bal <= 0:
            con.execute("UPDATE invoices SET status='paid' WHERE customer_id=? AND status='open'", (cid,))
        con.commit()
    except sqlite3.IntegrityError:
        con.rollback()
        raise BillingError("That GCash reference number was already recorded.")
    except Exception:
        con.rollback()
        raise
    return cur.lastrowid


def void_payment(con, pid, reason=""):
    p = con.execute("SELECT * FROM payments WHERE id=?", (pid,)).fetchone()
    if not p or p["voided"]:
        raise BillingError("Payment not found or already voided.")
    if not reason.strip():
        raise BillingError("Give a reason for voiding.")
    try:
        _post(con, p["customer_id"], "void", p["amount"], p["reference"] or p["method"], "Void: " + reason.strip())
        c = _customer(con, p["customer_id"])
        if p["due_after"] != p["due_before"] and c["due_date"] == p["due_after"]:
            con.execute("UPDATE customers SET due_date=? WHERE id=?", (p["due_before"], p["customer_id"]))
        con.execute("UPDATE payments SET voided=1, void_reason=? WHERE id=?", (reason.strip(), pid))
        con.commit()
    except Exception:
        con.rollback()
        raise


def adjust_balance(con, cid, new_balance, note):
    if not (note or "").strip():
        raise BillingError("Give a reason for the adjustment.")
    c = _customer(con, cid)
    delta = money(new_balance) - money(c["balance"])
    if delta == 0:
        return
    _post(con, cid, "adjust", delta, "", note.strip())
    con.commit()


def create_invoice(con, cid, amount, period, note=""):
    """One invoice per customer per due date. Returns id, or None if it already exists."""
    amount = money(amount)
    if amount <= 0:
        raise BillingError("Invoice amount must be above zero.")
    try:
        cur = con.execute("INSERT INTO invoices(customer_id,period,amount,issued_on,note) VALUES (?,?,?,?,?)",
                          (cid, period, amount, date.today().isoformat(), note))
    except sqlite3.IntegrityError:
        con.rollback()
        return None
    _post(con, cid, "invoice", amount, period, note or "Monthly bill")
    con.commit()
    return cur.lastrowid


def void_invoice(con, iid, reason=""):
    inv = con.execute("SELECT * FROM invoices WHERE id=?", (iid,)).fetchone()
    if not inv or inv["status"] == "void":
        raise BillingError("Invoice not found or already voided.")
    if not reason.strip():
        raise BillingError("Give a reason for voiding.")
    _post(con, inv["customer_id"], "void", -inv["amount"], inv["period"], "Void invoice: " + reason.strip())
    con.execute("UPDATE invoices SET status='void' WHERE id=?", (iid,))
    con.commit()


def invoice_preview(con, through):
    """Customers whose due date is on/before `through` and who have no invoice for that due date yet."""
    rows = []
    for c in con.execute("SELECT * FROM customers WHERE due_date IS NOT NULL AND due_date<=? AND mrc>0 "
                         "ORDER BY last_name, first_name", (through,)):
        if con.execute("SELECT 1 FROM invoices WHERE customer_id=? AND period=?", (c["id"], c["due_date"])).fetchone():
            continue
        already = (c["balance"] or 0) >= (c["mrc"] or 0) - 0.005
        rows.append((c, already))
    return rows
