import configparser
import csv
import io
import ipaddress
import os
import secrets
import time
from datetime import timedelta
from datetime import date
from functools import wraps

from flask import (Flask, Response, abort, flash, g, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash

import db
import router as router_mod

BASE = os.path.dirname(os.path.abspath(__file__))


def load_config(path=None):
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(path or os.environ.get("BILLING_CONFIG") or os.path.join(BASE, "config.ini"))
    return cfg


def create_app(cfg=None):
    cfg = cfg or load_config()
    app = Flask(__name__)
    app.secret_key = cfg.get("server", "secret_key", fallback="dev")
    app.config["CFG"] = cfg
    dbpath = cfg.get("server", "database", fallback="billing.db")
    if not os.path.isabs(dbpath):
        dbpath = os.path.join(BASE, dbpath)
    app.config["DB"] = dbpath
    app.config["ROUTER"] = router_mod.make_router(cfg)
    db.connect(dbpath).close()
    blocked = [ipaddress.ip_network(n.strip(), strict=False)
               for n in cfg.get("admin", "blocked_nets", fallback="").split(",") if n.strip()]
    failures = {}

    def con():
        if "con" not in g:
            g.con = db.connect(app.config["DB"])
        return g.con

    @app.teardown_appcontext
    def close(_):
        c = g.pop("con", None)
        if c:
            c.close()

    def client_ip():
        return request.remote_addr or ""

    def admin_only(f):
        @wraps(f)
        def w(*a, **k):
            try:
                ip = ipaddress.ip_address(client_ip())
                if any(ip in n for n in blocked):
                    abort(404)
            except ValueError:
                pass
            if not session.get("admin"):
                return redirect(url_for("login"))
            return f(*a, **k)
        return w

    def csrf():
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(16)
        return session["csrf"]

    app.jinja_env.globals["csrf"] = csrf
    app.jinja_env.filters["peso"] = lambda v: ("-" if (v or 0) < 0 else "") + "₱{:,.2f}".format(abs(v or 0))

    @app.before_request
    def check_csrf():
        if request.method == "POST" and request.endpoint != "login":
            if not secrets.compare_digest(request.form.get("_csrf", ""), session.get("csrf", "x")):
                abort(400)

    def page_cfg():
        p = dict(cfg["page"]) if cfg.has_section("page") else {}
        return p

    # ---------- customer-facing reminder ----------
    def render_reminder(customer, preview=False):
        c = customer
        overdue_amt = (c["balance"] or 0) if c else 0
        return render_template("remind.html", c=c, p=page_cfg(), preview=preview,
                               amount=overdue_amt, today=date.today().isoformat())

    @app.route("/")
    @app.route("/remind")
    def remind():
        ip = client_ip()
        username, result = None, "no-session"
        try:
            username = app.config["ROUTER"].username_for_ip(ip)
        except Exception as e:  # router unreachable etc.
            result = "router-error: %s" % e
        customer = None
        if username:
            customer = con().execute("SELECT * FROM customers WHERE username=?", (username,)).fetchone()
            result = "ok" if customer else "not-in-db"
        db.log_lookup(con(), ip, username, result)
        resp = app.make_response(render_reminder(customer))
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.route("/healthz")
    def healthz():
        return "ok"

    # ---------- admin ----------
    @app.route("/admin/login", methods=["GET", "POST"])
    def login():
        try:
            ip = ipaddress.ip_address(client_ip())
            if any(ip in n for n in blocked):
                abort(404)
        except ValueError:
            pass
        if request.method == "POST":
            ip = client_ip()
            tries, until = failures.get(ip, (0, 0))
            if time.time() < until:
                flash("Too many attempts. Wait a minute.")
                return render_template("login.html"), 429
            h = cfg.get("admin", "password_hash", fallback="")
            if h and check_password_hash(h, request.form.get("password", "")):
                session.clear()
                session["admin"] = True
                failures.pop(ip, None)
                return redirect(url_for("customers"))
            tries += 1
            failures[ip] = (tries, time.time() + 60 if tries >= 5 else 0)
            time.sleep(1)
            flash("Wrong password.")
        return render_template("login.html")

    @app.route("/admin/logout", methods=["POST"])
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/admin")
    @admin_only
    def customers():
        q = request.args.get("q", "").strip()
        flt = request.args.get("f", "")
        rows = con().execute("SELECT * FROM customers ORDER BY last_name, first_name").fetchall()
        out = []
        for r in rows:
            od = db.is_overdue(r)
            if q and q.lower() not in (r["username"] + (r["first_name"] or "") + (r["last_name"] or "") + (r["phone"] or "")).lower():
                continue
            if flt == "overdue" and not od:
                continue
            out.append((r, od))
        total = len(rows)
        n_over = sum(1 for r in rows if db.is_overdue(r))
        owed = sum((r["balance"] or 0) for r in rows if (r["balance"] or 0) > 0)
        return render_template("customers.html", rows=out, q=q, f=flt, total=total,
                               n_over=n_over, owed=owed)

    def get_customer(cid):
        c = con().execute("SELECT * FROM customers WHERE id=?", (cid,)).fetchone()
        if not c:
            abort(404)
        return c

    def run(action, ok_msg=None):
        """Run a billing action; show its error text instead of a crash."""
        try:
            r = action()
            if ok_msg:
                flash(ok_msg)
            return r
        except db.BillingError as e:
            flash(str(e))
        return None

    @app.route("/admin/customer/<int:cid>", methods=["GET", "POST"])
    @admin_only
    def customer(cid):
        c = get_customer(cid)
        if request.method == "POST":
            due = db.parse_date(request.form.get("due_date"))
            note = request.form.get("note", "")[:500]
            if request.form.get("due_date", "").strip() and not due:
                flash("Due date not understood. Use YYYY-MM-DD.")
            else:
                con().execute("UPDATE customers SET due_date=?, billing_day=COALESCE(?, billing_day), note=? WHERE id=?",
                              (due, int(due[8:10]) if due else None, note, cid))
                con().commit()
                flash("Saved.")
            return redirect(url_for("customer", cid=cid))
        q = con().execute
        return render_template(
            "customer.html", c=c, overdue=db.is_overdue(c), today=date.today().isoformat(),
            payments=q("SELECT * FROM payments WHERE customer_id=? ORDER BY id DESC LIMIT 50", (cid,)).fetchall(),
            invoices=q("SELECT * FROM invoices WHERE customer_id=? ORDER BY id DESC LIMIT 24", (cid,)).fetchall(),
            ledger=q("SELECT * FROM ledger WHERE customer_id=? ORDER BY id DESC LIMIT 60", (cid,)).fetchall())

    @app.route("/admin/customer/<int:cid>/pay", methods=["POST"])
    @admin_only
    def pay(cid):
        get_customer(cid)
        f = request.form
        pid = run(lambda: db.record_payment(
            con(), cid, f.get("amount", ""), f.get("method", ""), f.get("reference", ""),
            f.get("received_on", ""), f.get("note", "")[:200], allow_duplicate=bool(f.get("again"))))
        if pid:
            return redirect(url_for("receipt", pid=pid))
        return redirect(url_for("customer", cid=cid))

    @app.route("/admin/customer/<int:cid>/adjust", methods=["POST"])
    @admin_only
    def adjust(cid):
        get_customer(cid)
        run(lambda: db.adjust_balance(con(), cid, db.parse_money(request.form.get("new_balance")),
                                      request.form.get("note", "")), "Balance adjusted.")
        return redirect(url_for("customer", cid=cid))

    @app.route("/admin/payment/<int:pid>/void", methods=["POST"])
    @admin_only
    def void_payment(pid):
        p = con().execute("SELECT * FROM payments WHERE id=?", (pid,)).fetchone()
        if not p:
            abort(404)
        run(lambda: db.void_payment(con(), pid, request.form.get("reason", "")), "Payment voided.")
        return redirect(url_for("customer", cid=p["customer_id"]))

    @app.route("/admin/invoice/<int:iid>/void", methods=["POST"])
    @admin_only
    def void_invoice(iid):
        i = con().execute("SELECT * FROM invoices WHERE id=?", (iid,)).fetchone()
        if not i:
            abort(404)
        run(lambda: db.void_invoice(con(), iid, request.form.get("reason", "")), "Invoice voided.")
        return redirect(url_for("customer", cid=i["customer_id"]))

    @app.route("/admin/receipt/<int:pid>")
    @admin_only
    def receipt(pid):
        p = con().execute("SELECT * FROM payments WHERE id=?", (pid,)).fetchone()
        if not p:
            abort(404)
        return render_template("receipt.html", p=p, c=get_customer(p["customer_id"]), pg=page_cfg())

    @app.route("/admin/billing", methods=["GET", "POST"])
    @admin_only
    def billing():
        through = db.parse_date(request.values.get("through")) or (date.today() + timedelta(days=7)).isoformat()
        if request.method == "POST":
            made = 0
            for cid in request.form.getlist("cid"):
                c = con().execute("SELECT * FROM customers WHERE id=?", (cid,)).fetchone()
                if c and c["due_date"] and c["due_date"] <= through and (c["mrc"] or 0) > 0:
                    if run(lambda: db.create_invoice(con(), c["id"], c["mrc"], c["due_date"],
                                                     "Monthly bill " + (c["plan"] or ""))):
                        made += 1
            flash("Created %d invoice(s)." % made)
            return redirect(url_for("billing", through=through))
        rows = db.invoice_preview(con(), through)
        return render_template("billing.html", rows=rows, through=through)

    def payment_rows(frm, to):
        return con().execute(
            "SELECT p.*, c.first_name, c.last_name, c.username FROM payments p JOIN customers c ON c.id=p.customer_id "
            "WHERE p.received_on BETWEEN ? AND ? ORDER BY p.received_on DESC, p.id DESC", (frm, to)).fetchall()

    def range_args():
        to = db.parse_date(request.args.get("to")) or date.today().isoformat()
        frm = db.parse_date(request.args.get("from")) or (date.today() - timedelta(days=6)).isoformat()
        return frm, to

    @app.route("/admin/payments")
    @admin_only
    def payments():
        frm, to = range_args()
        rows = payment_rows(frm, to)
        totals = {}
        for r in rows:
            if not r["voided"]:
                totals[r["method"]] = totals.get(r["method"], 0) + r["amount"]
        return render_template("payments.html", rows=rows, frm=frm, to=to, totals=totals,
                               grand=sum(totals.values()))

    @app.route("/admin/payments.csv")
    @admin_only
    def payments_csv():
        frm, to = range_args()
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["receipt", "date", "username", "name", "amount", "method", "reference", "voided", "note"])
        for r in payment_rows(frm, to):
            w.writerow(["OR-%06d" % r["id"], r["received_on"], r["username"],
                        ("%s %s" % (r["first_name"], r["last_name"])).strip(), "%.2f" % r["amount"],
                        r["method"], r["reference"], "yes" if r["voided"] else "", r["note"]])
        return Response(out.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=payments_%s_%s.csv" % (frm, to)})

    @app.route("/admin/preview/<int:cid>")
    @admin_only
    def preview(cid):
        c = con().execute("SELECT * FROM customers WHERE id=?", (cid,)).fetchone()
        if not c:
            abort(404)
        return render_reminder(c, preview=True)

    @app.route("/admin/import", methods=["GET", "POST"])
    @admin_only
    def import_view():
        result = None
        if request.method == "POST":
            f = request.files.get("file")
            if not f:
                flash("Choose the CSV file.")
            else:
                try:
                    result = db.import_csv(con(), f.read(), overwrite_money=bool(request.form.get('overwrite')))
                except ValueError as e:
                    flash(str(e))
        return render_template("import.html", result=result)

    @app.route("/admin/lookups")
    @admin_only
    def lookups():
        rows = con().execute("SELECT * FROM lookups ORDER BY id DESC LIMIT 100").fetchall()
        return render_template("lookups.html", rows=rows)

    return app


if __name__ == "__main__":
    cfg = load_config()
    application = create_app(cfg)
    application.run(host=cfg.get("server", "host", fallback="0.0.0.0"),
                    port=cfg.getint("server", "port", fallback=8080), threaded=True)
