# Billing app - Phase 1 + 2 (reminder page, customers, invoices, payments)

Read-only with respect to the router: it never changes profiles, secrets or firewall rules.
It does two things:
1. Shows each overdue customer a page with **their own** name, amount and due date, plus GCash instructions.
2. Gives you an admin page (customer list, search, edit balance/due date, import the Taoki export).

The PASSWORD column of the Taoki export is never read or stored. Addresses and coordinates are not imported either.

## Run it (laptop first, OPI later - same steps)
```
pip install flask            # Windows: py -m pip install flask
cp config.example.ini config.ini     # Windows: copy config.example.ini config.ini
# edit config.ini: secret_key (long random text), [page] section (GCash name/number, contact)
python manage.py set-password
python manage.py import-csv path/to/export.csv     # or use the Import page
python app.py
```
Admin: `http://<app-ip>:8080/admin`. The admin pages answer 404 to anyone in `blocked_nets`
(the overdue pool 10.69.0.0/23), so a blocked customer can't even see the login.

Test without the router: leave `mode = mock`, edit `mock_router.json` like `{"127.0.0.1": "USERNAME"}`,
open `http://127.0.0.1:8080/` - it shows that customer's page. Admin -> customer -> "Preview reminder page" also works.

Run the tests: `python -m unittest discover -s tests`

## Router changes on the RB4011 (all reversible)
Replace `<APP-IP>` with the laptop/OPI address. Do them one at a time and test between steps.

**0. Check what you have first**
```
/ip service print where name=api
/ip proxy access print detail
```

**1. Read-only API user, reachable only from the app**
```
/user group add name=billing-ro policy=read,api
/user add name=billing-ro group=billing-ro password="<LONG-RANDOM>" address=<APP-IP>/32
/ip service set api address=<APP-IP>/32 disabled=no
```
Put the same password in `config.ini` ([router] password), set `mode = api` and `host` = the RB4011 address, then run
`python manage.py router-test 10.69.1.254` (use the IP of a currently overdue session). It should print that session's username.
If something else already uses the API service, don't restrict its address without checking.

**2. Let overdue users reach the app (before Taoki's ACCEPT rule)**
```
/ip firewall filter add chain=forward action=accept protocol=tcp src-address-list=CHRC-BS-BLACKLIST dst-address=<APP-IP> dst-port=8080 comment="PAY: Allow reminder page" place-before=[find where comment="CHRC-BS-ACCEPT"]
```

**3. Don't masquerade traffic to the app (so it sees each customer's real IP)**
```
/ip firewall nat add chain=srcnat action=accept src-address=10.69.0.0/23 dst-address=<APP-IP> comment="PAY: no NAT to reminder server" place-before=[find where comment="CHRC-BS-SRCNAT"]
```
The app host must use the RB4011 as its default gateway so replies return through it.

**4. Point the redirect at the app** (note the current action-data first so you can roll back)
```
/ip proxy access print detail
/ip proxy access set [find where action=redirect] action-data="<APP-IP>:8080/remind"
```
Use port 8080, not 80: port 80 from overdue users is hijacked into the web proxy.

**Rollback**
```
/ip proxy access set [find where action=redirect] action-data="taokininam.com/portal.php?uname=KristineMR&tab=7"
/ip firewall nat remove [find where comment="PAY: no NAT to reminder server"]
/ip firewall filter remove [find where comment="PAY: Allow reminder page"]
/ip service set api disabled=yes
/user remove [find where name=billing-ro]
/user group remove [find where name=billing-ro]
```

## Troubleshooting
Admin -> Lookups lists the last 100 page visits with the IP the app saw.
- Client IP is the router's own address: step 3 isn't matching (rule order, or the app isn't behind the RB4011).
- Result `no-session`: no PPPoE session was found for that IP (try `router-test`).
- Result `not-in-db`: the PPPoE username isn't in the imported list (re-import).
- Result `router-error`: API port, user, password or allowed address is wrong.
If Taoki rewrites proxy rule 2 when it syncs, the redirect will revert; check `/ip proxy access print` after a day.

## Run on the OPI at boot (systemd)
`/etc/systemd/system/billing.service`:
```
[Unit]
Description=Billing reminder app
After=network-online.target
[Service]
WorkingDirectory=/opt/billing-app
ExecStart=/usr/bin/python3 app.py
Restart=always
User=billing
[Install]
WantedBy=multi-user.target
```
Then `systemctl enable --now billing`. Back up `billing.db` and `config.ini`; they hold customer names and phone numbers.

## Phase 2: invoices and payments
**Every month:** Admin -> Invoices. Pick the date ("everyone due on or before ..."), review the list, create. Each invoice adds the
customer's monthly fee to their balance, once per due date (it can't double-bill). Customers whose balance already covers a month
(for example one imported from Taoki) are left unticked.

**When someone pays:** open the customer -> *Record a payment*. GCash needs the reference number from the receipt, and the same
reference can't be recorded twice. Cash needs none. If the payment clears the balance, the next due date moves forward one month.
A partial payment lowers the balance and leaves the due date alone. Then print or screenshot the receipt (OR-000001 and so on).

**Mistakes:** payments and invoices are never deleted. *Void* them with a reason; the balance and due date go back, and the ledger
shows both lines. *Adjust balance* is for corrections only and also needs a reason.

**Reports:** Admin -> Payments shows totals by GCash/cash for any date range and downloads a CSV for your records.

**Running next to Taoki:** until Phase 3, Taoki still does the disconnecting and it doesn't know about payments recorded here.
Keep recording payments in Taoki too, or re-import its export with *Also replace balances and due dates* ticked (Import page).
Ordinary imports never overwrite balances, so your payments here are safe.

## Not built yet (Phase 3)
Automatic profile switching and disconnects from this app (so Taoki can be retired), SMS reminders.
