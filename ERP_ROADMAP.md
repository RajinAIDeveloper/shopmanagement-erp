# ERP implementation and acceptance record

Default configuration: BDT; configurable 15% VAT inclusive, product exemptions;
unconfirmed online reservations expire after 24 hours. Confirmed and packed orders retain stock.

Rollback snapshot: `.runtime/snapshots/workspace-20260919T155632Z.zip` contains
source, existing local edits, media, a consistent SQLite backup and a hash manifest.
No competing project process was found and source hashes stayed unchanged during snapshot.
An open editor's unsaved changes cannot be detected this way.

| Phase | Scope | Acceptance status |
|---|---|---|
| 1 Selling | VAT, tenders, dues, partial returns/exchanges, credit, shifts, offline queue, serial printer | Existing implementation: backend and browser tests pass; migrations 5–6 applied. Hardware unverified. |
| 2 Inventory | Locations, transfers, counts, multiple purchase lines/receipts/returns | Existing implementation: backend and browser tests pass; migrations 7–9 applied. |
| 3 Accounting | Chart, dated ledger, manual/opening journals and reversal, statements, aging, fiscal periods, reconciliation | In progress |
| 4 Customers | Accounts, saved lists, comparison, CRM, coupons, loyalty, shipping charges, customer statements | Pending |
| 5 Integrations | Manual and HTTP courier/payment providers, COD settlement, notifications, labels/QR, chat/analytics settings | Pending |
| 6 Security / operations / catalog | MFA/sessions/permissions, imports, tracking, content pages, reporting, observability/load checks | Pending |

91 baseline Django tests passed. Phase 1 and 2 browser suites passed on an isolated
database. The baseline's live database was missing migrations 5–9; these have now
been applied after the snapshot. Each later phase requires migrations, relevant
backend tests and a browser workflow before moving on. Live provider delivery,
production load and physical hardware cannot be marked verified by mocked tests.
