#!/usr/bin/env python3
"""Command-line helpers.

  python manage.py set-password            set the admin password
  python manage.py import-csv FILE         import the Taoki export (PASSWORD column is ignored)
  python manage.py router-test IP          ask the router which user has this IP
"""
import getpass
import re
import sys

from werkzeug.security import generate_password_hash

import app as appmod
import db


def set_password():
    p1 = getpass.getpass("New admin password: ")
    if len(p1) < 8 or p1 != getpass.getpass("Repeat: "):
        sys.exit("Passwords differ or are shorter than 8 characters.")
    h = generate_password_hash(p1)
    path = appmod.os.environ.get("BILLING_CONFIG") or appmod.os.path.join(appmod.BASE, "config.ini")
    text = open(path, encoding="utf-8").read()
    text = re.sub(r"(?m)^password_hash\s*=.*$", lambda m: "password_hash = " + h, text)
    open(path, "w", encoding="utf-8").write(text)
    print("Saved to", path)


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cmd = sys.argv[1]
    if cmd == "set-password":
        set_password()
    elif cmd == "import-csv" and len(sys.argv) == 3:
        cfg = appmod.load_config()
        a = appmod.create_app(cfg)
        con = db.connect(a.config["DB"])
        print("added %d, updated %d, skipped %d" % db.import_csv(con, open(sys.argv[2], "rb").read()))
    elif cmd == "router-test" and len(sys.argv) == 3:
        cfg = appmod.load_config()
        a = appmod.create_app(cfg)
        print(a.config["ROUTER"].username_for_ip(sys.argv[2]) or "(no active session for that IP)")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
