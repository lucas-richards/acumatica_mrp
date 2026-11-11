# Westridge MRP — Purchasing Planner

An internal, read-only purchasing planner. For each purchased component it works out:
- how much the finished goods using it consume per month
- whether current supply lasts through the vendor's lead time
- when to order and how much

Data comes from Acumatica's own records (BOMs, stock items, vendors, purchase orders, invoices, inventory issues, sales orders) through the REST API. The app never writes to Acumatica in phase 1.

This is a separate Django project from westridgeApp. It has its own venv, its own PostgreSQL database, its own mod_wsgi daemon process, and its own users and cookies. It is served at `/mrp` on the same Apache.

## Project layout

```
config/       Django settings (env-driven), urls, wsgi
planning/     Pure-Python MRP maths: no Django imports, fully unit tested
acumatica/    Read-only REST client + entity mapping (units, multi-level BOM explosion)
mrp/          Django app: models, views/templates (HTMX), management commands
sample_data/  Sample dataset (also the JSON import format reference)
deploy/       Apache snippet + instructions, create_db.sql, cron example
docs/         What is read from Acumatica and which screen rights are needed
```

Later phases (draft POs, production orders, SKUs, BOMs) will be new Django apps beside `mrp/`. They will reuse `acumatica/` and `planning/`.

## Setup (development)

Requires Python 3.10, the same version mod_wsgi on this server is built for.

```bash
cd /home/bc/westridgeApp_mrp
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env     # then edit; for quick local work set DEBUG=True
.venv/bin/python manage.py migrate
.venv/bin/python manage.py seed_demo
.venv/bin/python manage.py createsuperuser
.venv/bin/python manage.py runserver 0.0.0.0:8001
```

If `DEBUG=True` and `DATABASE_URL` is unset, the app falls back to a local SQLite file (`dev.sqlite3`), which is handy for a quick look. Production always uses PostgreSQL.

Open http://localhost:8001/ (westridgeApp's dev server uses 8000).

### Users and roles

Everything requires a login. Each user needs one of two groups (they are created by the migrations):
- **viewer**: can see the plan, pick a vendor, filter the table by item or status, expand rows and export CSV.
- **planner**: everything a viewer can do, plus choose which monthly usage drives an item, type a custom usage, and press "Refresh from Acumatica".

Superusers count as planners. Add users and groups in `/admin/`. Users here are separate from westridgeApp users.

### Inventory level chart

The small graph icon next to each SKU opens a panel under the item, titled "2 year graph inventory levels", with the item's inventory level for the last two years as a line chart. For an item that affects finished goods, the panel shows them on the left (the same table as the finished-goods button) and the chart on the right; the icon and the finished-goods button open and close the same panel. The data comes from westridgeApp's planner endpoint (`/planner/json/`, the one the Planner > Inventory Level tab uses), which is served by the same Apache host, so the numbers match that tab. westridgeApp has to be running. Set `MRP_INVENTORY_HISTORY_URL` to a full URL if the endpoint lives elsewhere. Chart.js 4.4.1 is bundled in `static/mrp/` and is only loaded the first time a chart is opened.

## Environment variables

All configuration comes from the environment or from `.env` in the project root. `.env` is gitignored, so never commit it. `.env.example` lists every variable.

| Variable | Purpose |
|---|---|
| `SECRET_KEY` | Django secret key (required unless `DEBUG=True`) |
| `DEBUG` | `True` only for development |
| `ALLOWED_HOSTS` | Comma-separated, e.g. `10.1.1.32,localhost` |
| `CSRF_TRUSTED_ORIGINS` | e.g. `http://10.1.1.32` |
| `DATABASE_URL` | `postgres://mrp_user:...@localhost:5432/mrp` |
| `STATIC_URL` | `/mrp/static/` under Apache (default `/static/` for runserver) |
| `COOKIE_PATH` | `/mrp` under Apache |
| `SECURE_COOKIES` | `True` once the site is on HTTPS |
| `ACUMATICA_BASE_URL`, `ACUMATICA_TENANT`, `ACUMATICA_BRANCH`, `ACUMATICA_USERNAME`, `ACUMATICA_PASSWORD` | Read-only REST API login |
| `ACUMATICA_ENDPOINT`, `ACUMATICA_MANUFACTURING_ENDPOINT` | REST endpoints (default `Default/24.200.001`, `MANUFACTURING/24.200.001`) |
| `ACUMATICA_SALES_ORDER_TYPES` | Sales order types counted as demand (default `SO`) |
| `ACUMATICA_AVAILABLE_GI` | Generic Inquiry with the available quantity (default `LR-Available Qty`) |
| `ACUMATICA_ITEM_PLANNING_GI` | Generic Inquiry with each item's planning method and source (default `IN-StockItem`) |
| `ACUMATICA_RECEIVING_GI` / `ACUMATICA_RECEIVING_LOCATION` | Generic Inquiry with the quantity per location (default `DB-StorageDetailsByItemWarehouseLocation`) and the receiving location (default `REC-INS`) |
| `EMAIL_HOST` (`smtp.fastmail.com`), `EMAIL_PORT` (465), `EMAIL_USE_SSL`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD` | Fastmail SMTP with an app-specific password |
| `DEFAULT_FROM_EMAIL` | Sender address |
| `MRP_REMINDER_TO` | Comma-separated recipients of vendor reminders |
| `MRP_SITE_URL` | Link used in emails, e.g. `http://10.1.1.32/mrp` |

## Data

### Sample data

`manage.py seed_demo` loads 5 vendors, 11 components and 5 finished goods. Their dates are shifted so they are relative to today. The page lists everything to purchase in one table. Add `--as-of 2026-10-29` to reproduce the sample dates exactly.

### Import from files

```bash
.venv/bin/python manage.py import_data --json my_data.json
.venv/bin/python manage.py import_data --csv-dir exports/
```

The JSON shape is the same as `sample_data/sample_data.json`. For CSV, use one file per dataset with a header row of field names:
- `vendors.csv`
- `stock_items.csv`
- `on_hand.csv`
- `open_po_lines.csv`
- `bom.csv`
- `item_vendors.csv`
- `po_history.csv`
- `demand.csv`, with columns `sku,source,month,qty`. `source` is one of `trailing_invoices`, `open_sales_orders` or `forecast`.

Each dataset in the file fully replaces what is stored. If any row is invalid, nothing changes.

### Sync from Acumatica

```bash
.venv/bin/python manage.py sync_acumatica                 # demand source from settings
.venv/bin/python manage.py sync_acumatica --all-demand    # pull all three demand sources
```

The sync reads everything first and then replaces the local tables in one transaction. If anything fails, such as a network error, a missing column or a bad value, the previous data stays and the error is recorded as a failed sync run (visible in the admin). Planners can also start the same sync with **Refresh from Acumatica**; it runs as a separate background process and the page shows a "Refreshing…" banner until it finishes.

`docs/acumatica_access.md` explains exactly what is read, how it is filtered and converted, and which Acumatica screen rights the user needs.

### Lead times

The lead time is what Acumatica has: the **item-vendor** lead time, else the **vendor default**. There are no overrides in the planner. An item with neither is flagged "Lead time missing" and planned with the 7-day minimum.

### Which usage drives an item

Each row shows three monthly usages: the 2-year average, the recent (6 month) average, and a custom value a planner can type in. The one in use is highlighted and drives everything for that item: months of supply, status, the suggested quantity and the months left. Planners click another value to switch, and typing a custom value selects it. Items start on the recent usage. Choices are remembered per item.

## Planning logic

The logic lives in `planning/mrp.py`. A month is 30 days.

- usage/month = trailing average of the component's inventory issues (lines of type Issue, plus kit assemblies; components sold straight to a customer on an Invoice line are left out); for a bought finished good, invoices + kit breakdowns (no issues)
- on open orders = Σ over finished goods that use it of (open sales order qty with no production order × qty per unit)
- lead time months = lead time days ÷ 30, but never less than 7 days (the red line)
- demand in lead time = usage × lead time months
- supply position = on hand + on order − on open orders
- months of supply = supply ÷ usage
- target (the reorder point) = lead time + time to prepare the order, where preparing takes a third of the lead time and at least 7 days. A lead time of a week or less gives 14 days; 3 months gives 4 months; 6 months gives 8
- needed = usage × target − supply
- suggested qty = needed rounded up to the lot size (0 if needed ≤ 0). When an order is needed it is never less than 1.2 × usage × lead time months (the lead time's demand plus 20%), whatever is in stock
- order-by date = today + (months of supply − lead time months) × 30 days, rounded down to a whole day. "Now" if it is today or earlier.
- status:
  - **Stockout risk** if months of supply < lead time
  - **Order soon** if months of supply < target
  - otherwise **Covered**
- months left when an order arrives = months of supply − lead time months, from current supply only (not counting the suggested order), for an order placed today. Negative means stock runs out that many months before an order could arrive.
- on hand = Acumatica's available quantity when accessible, otherwise quantity on hand
- components whose Inventory Planning Source is Manufacturing are not on the purchasing plan
- the page can show **Components** (one row per purchased component) or **Finished goods** (one row per finished good; expand it to see its purchased components)

Edge cases:
- No usage means infinite cover and no order-by date. It is flagged.
- Missing lead time, vendor, BOM or FG demand never crashes the plan. Each is flagged in the table.

FG demand is a swappable function (`planning/demand.py`):
- `trailing_invoices` is the default: the average of the last 6 full months.
- `open_sales_orders`
- `forecast`

## Reminder emails (optional, not on the page)

The reminder section was removed from the page. The command still exists if you want emails later:

`send_order_reminders` checks each vendor's earliest order-by date for components that need ordering. If that date is within the reminder window (30 days by default, stored in plan settings) or already past, it emails `MRP_REMINDER_TO`. A vendor is not emailed again within 7 days unless it becomes more urgent (Reminder active → Order now).

`--weekly` also sends the Monday digest to subscribed users. `--dry-run` prints the emails instead of sending them.

## Tests

```bash
DEBUG=True .venv/bin/python manage.py test                                  # everything
.venv/bin/python -m unittest discover -s planning -t .                      # planning only, no Django
.venv/bin/python -m unittest discover -s acumatica -t .                     # Acumatica client and mapping only
```

`planning/tests/test_sample_data.py` checks that the sample data reproduces every number on the mockup.

## Deployment (Apache + mod_wsgi, beside westridgeApp)

See **`deploy/APACHE_INSTRUCTIONS.md`**. In short:
1. Create the database with `deploy/create_db.sql`.
2. Fill in `.env`.
3. Run `migrate` and `collectstatic`.
4. Paste `deploy/apache-mrp.conf` into the existing vhost, above westridgeApp's WSGI lines.
5. Run `apachectl configtest` and reload Apache.
6. Add the cron lines from `deploy/cron.example`.

westridgeApp's code, venv, database and Apache lines stay as they are.
