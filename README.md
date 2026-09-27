# Altab Tech

A Django store-management system for a connected tech shop: a staff workspace, barcode-ready POS, and a customer storefront sharing one inventory database across branches and warehouses. BDT currency, Asia/Dhaka timezone. Cash-on-delivery online sales, double-entry journal with VAT, settlements, and a hardened deployment path.

## Run on this Windows workspace

A portable Python runtime and all dependencies are installed under `.runtime/` (ignored by Git).

```powershell
.\start.ps1
```

- Workspace: http://127.0.0.1:8000/
- Storefront: http://127.0.0.1:8000/shop/
- Staff administration: http://127.0.0.1:8000/admin/
- Health check: http://127.0.0.1:8000/healthz/

The local demo was initialized with username `owner` and password `AltabDemo!2026`. These are development credentials; change them before exposing this app to anyone. Sample data is fictional and lives in `db.sqlite3`. `seed_demo` refuses to populate a non-empty catalog and never overwrites an existing password.

## Standard setup

Python 3.12 or newer.

```bash
python -m venv .venv
# Activate .venv using your platform's activation command.
python -m pip install -r requirements.txt
python manage.py migrate
python manage.py seed_demo --password "choose-a-local-demo-password"
python manage.py runserver
```

For a clean business database, skip `seed_demo`, run `createsuperuser`, and create products, customers, and suppliers from the workspace. Create the groups listed under **Roles** in Django admin if you did not seed.

## Implemented

### Selling

- Responsive workspace with live dashboard, 7/30-day revenue chart, channel totals, recent orders, low-stock alerts, and bestsellers.
- POS search, USB/Bluetooth keyboard-mode scanning, quantities, registered/walk-in customers, fixed order discounts, browser-local held carts, and printable receipts/PDF.
- **Tenders:** cash, card, bKash, Nagad, bank, store credit, and exchange credit, in any combination (split payment). Cash overpayment is returned as change. Account customers can leave a balance **on account** up to their credit limit; the balance is collected later from Accounting.
- **VAT:** configurable rate (default 15%), inclusive or exclusive pricing, per-product exemption, BIN on receipts. Sales credit *Sales revenue* net of VAT and *VAT payable* for the tax; online orders recognise VAT at shipping.
- **Pricing rules:** wholesale price per product applied automatically to wholesale-tier customers; minimum selling price enforced for cashiers (managers may go lower); per-line price override at the till.
- **Partial returns and exchanges** from the order detail: choose quantities per line, resellable or damaged, refund now (cash/card/mobile/bank/store credit) or later via Accounting. Returns reverse VAT and COGS proportionally and cancel any unpaid balance before creating a refund liability. An exchange turns the returned value into *exchange credit* applied at the POS to the replacement sale.
- **Store credit** ledger per customer (refunds, manual adjustments, redemptions) with a *Store credit liability* account.
- **Cash-drawer shifts:** opening float, cash drops, petty-cash payouts, float top-ups, expected-vs-counted close with a Z report and a *Cash over / short* posting. Optionally required before any sale.
- **Offline POS:** a service worker caches the workspace shell and last state; sales made without connectivity are queued on the device with idempotent request IDs and replayed automatically when the connection returns.
- **Receipt printer & cash drawer:** ESC/POS over Web Serial (Chrome/Edge) with 58/80 mm paper, auto-print after sale, and drawer kick. Not tested against physical hardware; the browser print dialog remains the fallback.
- Code 128-B label generation and printing for printable ASCII barcodes.
- Storefront catalog with search, category chips, collapsible price/brand/specification facets, in-stock filter, sorting, product details, cart, COD checkout, and private UUID order tracking. Prices and stock are validated on the server.
- **Reservation expiry:** unconfirmed online orders hold stock for a configurable window (Settings → Store & appearance, 1–168 h, default 24 h). Overdue reservations are released automatically — on staff writes, by the `expire_reservations --watch` worker, and reported as `Expired` on tracking/orders views even before the sweep runs.
- Fulfilment: Pending → Confirmed → Packed → Shipped → Delivered; pre-shipping cancellation; full resellable return from Delivered/Completed.

### Catalog and stock

- **Locations:** any number of stores and warehouses (Settings → Locations). Each product keeps a stock level per location; product totals are the sum. One location is the POS default and one fulfils online orders; staff can be pinned to a branch so their POS always sells from it, otherwise cashiers pick the branch on the POS screen. Availability shown at the till and in the storefront is per location.
- **Transfers:** draft → dispatch (stock leaves the source) → receive at the destination in one or more parts, with lost/damaged units written off to *Inventory loss*. Dispatched transfers can be cancelled back to the source.
- **Stock counts:** start a count for a location (optionally one category); expected quantities are frozen, staff enter counted quantities (with a scan-to-increment filter), save progress, and complete to post gains/losses. Uncounted lines are left alone.
- **Purchase orders:** many lines per order, a receiving location, expected date and note; goods received in several deliveries (each a numbered GRN), weighted-average cost updated per receipt, *Accounts payable* accrued on receipt, and returns to the supplier that reduce stock and the payable. Orders cancel only before any receipt.
- Products with unique SKU/barcode, brand, category, variant, supplier, photo, cost, price, opening stock, reorder threshold, and active flag. **Editing** changes descriptive fields only — on-hand stock goes through reasoned adjustments and average cost through purchase receipts. Inactive products leave the POS and storefront; every edit stores a before/after diff in the activity log. Order lines snapshot name/SKU/variant so receipts survive later edits.
- Category-specific specifications (Settings → Category filters) assigned per product and exposed as storefront facets.
- Suppliers, reasoned adjustments per location, and a stock movement history with location and running balance.
- Shared stock: online reservations reduce availability at the fulfilment location; POS decrements the selling branch; shipping consumes reservations; cancellation and expiry release them; returns restore stock where the sale happened.

### Money

- Paired debit/credit journal entries for opening stock, sales, COGS, purchases, adjustments, returns, expenses, and every settlement. Account balances, operating result, and a streaming sales CSV export with paid/refunded/due columns.
- **Settlements** (Accounting → Collections, payments & refunds): customer collections on any sale with a balance due, supplier payments up to the received-minus-returned value of a purchase order, and customer refunds on returned sales — partial or full, with method and reference. Each posting is idempotent (client-generated request ID), bounded by the outstanding balance with database constraints, and corrected only through a reversing entry with a mandatory reason. Returns split the reversal between receivable and a *Customer refunds payable* liability for money already collected.

### Store customization and customer features

- Settings → Store & appearance: name, address, contact details, storefront headline and welcome message, logo, primary/accent colours with presets, light/dark/device theme, announcement speed, reservation timeout. Branding applies to the workspace, storefront, login, receipts, and labels.
- Settings → Offers: scheduled percentage/fixed discounts for all products, one category, or one product, with banners on `/shop/#offers`. Online only; the best eligible offer wins and offers never stack. Checkout recomputes prices on the server and stores the actual selling price per line.
- Settings → Announcements: ordered, scheduled, linked scrolling messages with a pause control and reduced-motion support.
- Guest reviews (1–5 stars) with owner/manager moderation; only approved reviews count. Session wishlist with a dedicated `/shop/#wishlist` view.
- Image uploads (logo, offer banners, product photos) accept PNG/JPEG/WebP up to 3 MB / 16 MP and are re-encoded to PNG under `media/`.

### Operations and security

- Staff authentication with failed-login throttling, CSRF protection (JSON errors on `/api/`), backend-enforced role actions, financial data restricted to finance roles, `HttpOnly`/`SameSite` cookies, HSTS/secure-cookie/SSL-redirect in production, clickjacking and MIME-sniffing protection.
- Rate limits per client address on checkout, order tracking, reviews, wishlist, and uploads (configurable; shared across workers when `DJANGO_CACHE_URL` points at Redis).
- Activity log (Settings → Activity): every mutation, settlement, product diff, sign-in, sign-out, and failed sign-in with address and browser; searchable with cursor paging. Accountants see this tab only.
- `/healthz/` liveness endpoint, structured console logging, `backup_store` / `restore_store` commands (see **Backups**), read-only Django admin over every record.

## Roles

Users must have `is_staff` and an assigned group. Superusers have full access.

| Group | Permitted actions |
|---|---|
| Owner / Manager | All workspace actions, store settings, finance visibility, settlements, activity log |
| Cashier | POS sales, drawer shifts, customer creation and contact edits |
| Inventory Manager | Product creation/editing, photos and pricing rules, adjustments, suppliers, purchase orders, receiving and supplier returns, transfers, stock counts |
| Accountant | Finance visibility, expenses, settlements, activity log, shift history |

Returns, exchanges, price overrides below the minimum, customer credit limits, pricing tiers, store-credit adjustments, and location/staff-location administration are Owner/Manager actions.

All staff share read access to the operational catalog, orders, stock, suppliers, and customers. Per-record permissions are a later phase.

## Inventory and accounting behavior

All mutations run inside database transactions; on SQLite, transactions start in `IMMEDIATE` mode so concurrent writers queue instead of failing. Sales aggregate duplicate lines and use a conditional stock update to prevent negative availability. A database constraint prevents reserved stock exceeding on-hand stock; settlement constraints prevent paid or refunded amounts exceeding what is owed.

Online checkout reserves goods and recognizes revenue at shipping (debit accounts receivable, credit sales). POS sales debit each tender's account (cash, bank/mobile, store credit liability) and any balance left on account debits receivables; VAT is moved from sales revenue to *VAT payable*. Collections debit cash or bank/mobile and credit receivables. Purchases credit accounts payable; supplier payments debit it. Returns reverse sales revenue and VAT — the uncollected part against receivables, the collected part into *Customer refunds payable* — restore inventory at line cost when resellable, and refunds clear the liability. Drawer variances post to *Cash over / short*; petty-cash payouts create expenses. Tender types are recorded only; nothing talks to a payment provider.

Amounts use Decimal on the server. Inventory uses weighted average cost rounded to two decimals; order lines retain sale-time costs. Operating result is revenue less COGS and recorded expenses, before tax and inventory gains/losses. This is not a tax-compliant financial statement suite. Dashboard/order/customer summaries use the most recent 1,000 orders; journal balances and the export use all records.

## Hardware

1. Configure a USB/Bluetooth barcode scanner in keyboard/HID mode with an Enter suffix.
2. Open Point of sale and focus the search field; scanning a known barcode adds it to the cart.
3. Print receipts and labels through the browser's print dialog. Printer paper sizing and cash-drawer control require hardware-specific configuration; no physical hardware has been tested.

Held carts live in the browser tab's session storage, do not reserve stock, and are revalidated when sold. The POS requires connectivity to the server.

## Validation

```powershell
.runtime\python\python.exe manage.py check
.runtime\python\python.exe manage.py test store
npm run check
```

91 tests cover transactional rollback, reservations and expiry, order lifecycle and returns, receiving and average cost, role enforcement, private data, server-owned pricing, CSRF, invalid input, offers/announcements/filters/reviews/wishlist, product editing and snapshots, settlement idempotency/bounds/reversals, activity search and sign-in auditing, rate limits, the health endpoint, the CSV export, a backup → restore round trip, VAT inclusive/exclusive math, split tenders and change, dues and credit limits, store credit, wholesale and price floors, offline replay, partial returns, exchanges, drawer shifts, per-location levels, transfers with write-offs, stock counts, multi-line purchase orders with partial receipts, and supplier returns.

Browser verification runs against a seeded server. The core smoke test uses the development server on port 8000 and leaves its audit records in the demo database:

```bash
npm install
npx playwright-core install chromium
npm run test:browser
```

The customization and operations suites use an isolated snapshot on port 8001. Stop that server before recreating the snapshot:

```powershell
.runtime\python\python.exe scripts\prepare-browser-test.py
$env:DJANGO_DB_PATH = "$PWD\.runtime\commerce-browser.sqlite3"
.runtime\python\python.exe manage.py runserver 127.0.0.1:8001 --noreload
# In another terminal:
npm run test:commerce
npm run test:operations
npm run test:selling
npm run test:inventory
```

Together they cover location administration, transfers, branch-scoped POS stock, stock counts, purchase orders with partial receipts and returns, drawer shifts, split tenders, on-account sales, partial returns, exchanges, store credit, Z reports, VAT settings, branding persistence, logo and product-photo upload, themes, offer targeting and discounted checkout, announcements, facets, wishlist, review moderation, product editing with locked stock, the reservation deadline, COD collection and reversal, activity search with diffs, the health check, mobile layouts, and reduced motion. Set `DEMO_PASSWORD` if you chose another password; `BASE_URL` overrides the target. Clear `DJANGO_DB_PATH` before starting the normal server in that terminal.

## Deployment

The app is configured entirely through environment variables; `.env.example` lists every one with defaults. Minimum for production:

| Variable | Purpose |
|---|---|
| `DJANGO_DEBUG=0` | Production mode (also enables secure cookies, HSTS, SSL redirect) |
| `DJANGO_SECRET_KEY` | Long random string; the server refuses to start with the development key |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated hostnames |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | `https://` origins that may submit forms |
| `DJANGO_BEHIND_PROXY=1` | Trust `X-Forwarded-Proto`/`-For` from your TLS-terminating proxy |
| `DJANGO_DB_PATH` or `DJANGO_DB_ENGINE=postgresql` + `DJANGO_DB_*` | Where data lives |
| `DJANGO_MEDIA_ROOT`, `DJANGO_STATIC_ROOT` | Upload and collected-static folders |

**Windows host:** set the variables for the service account, then `.\serve.ps1`. It runs `check --deploy`, `migrate`, `collectstatic`, starts the reservation worker, and serves with Waitress on `ALTAB_LISTEN` (default `127.0.0.1:8000`). Register it as a service with NSSM or Task Scheduler (run at startup, restart on failure).

**Linux host:** `pip install gunicorn`, then `python manage.py migrate && python manage.py collectstatic --noinput`, `gunicorn config.wsgi:application --bind 127.0.0.1:8000 --workers 3`, and a second unit for `python manage.py expire_reservations --watch`. With more than one worker process, set `DJANGO_CACHE_URL=redis://…` (and `pip install redis`) so rate limits are shared.

**Reverse proxy:** terminate TLS in nginx/Caddy/IIS, forward to the app, serve `MEDIA_ROOT` at `/media/` directly (never through Django in production), set `client_max_body_size 4m`, and forward `X-Forwarded-Proto` and `X-Forwarded-For`. Static files are served by WhiteNoise with hashed names and long cache headers. Point your uptime monitor at `/healthz/`.

**Database:** SQLite runs in WAL mode with `IMMEDIATE` transactions and is appropriate for a single shop on one host; keep the file on local disk. Use PostgreSQL for multi-host deployments or heavy concurrent online traffic — the code uses `SELECT … FOR UPDATE` and conditional updates that work on both engines, but the PostgreSQL path has not been load-tested here.

**LAN-only counter without TLS:** `DJANGO_HTTPS=0` disables secure cookies, HSTS, and the HTTPS redirect. Use it only behind a firewall with no internet exposure.

**Sessions** last `DJANGO_SESSION_HOURS` (default 12). Password policy: minimum 10 characters, not common, not numeric-only.

### Backups

```powershell
python manage.py backup_store --destination D:\altab-backups --keep 30
```

Writes `altab-backup-<UTC timestamp>.zip` containing a consistent SQLite snapshot (taken with the online backup API, safe while the server runs), every file under `MEDIA_ROOT`, and a manifest with row counts and applied migrations. `--keep` prunes older archives. Schedule it nightly with Task Scheduler or cron and copy the folder off-host. For PostgreSQL use `pg_dump` plus a copy of `MEDIA_ROOT`.

Restore (server and worker stopped):

```powershell
python manage.py restore_store D:\altab-backups\altab-backup-20260919T020000Z.zip --yes
python manage.py migrate
```

The command validates the archive (`PRAGMA integrity_check`, migration table present), keeps the replaced database and media folder beside the new ones with a `.before-restore-<timestamp>` suffix, and refuses paths that escape the media folder. The round trip is exercised by the test suite. Rehearse a restore on a staging copy after each schema change.

## Not included

These need external providers, hardware, or product decisions and were deliberately left out rather than stubbed:

- POS hardware beyond ESC/POS serial printers (customer displays, scales, weighing barcodes).
- Tax/VAT, chart-of-accounts administration, manual journals, opening-balance UI, bank reconciliation, balance sheet, cash flow, fiscal periods.
- Customer accounts and cross-device wishlists, verified-purchase reviews, compare, coupons/loyalty, blog/pages, shipping zones and charges.
- Courier adapters (Pathao/Steadfast/RedX), shipping labels, payment gateways, email/SMS delivery, chat, QR tracking, analytics tags.
- Multi-factor authentication, per-record permissions, bulk product import, serial/batch/expiry tracking.
