# CURSOR_PLAN — sample program

Synthetic plan for a third-party repo that has no Warp files yet. Ticket ids, blockers, and gates are enough for `/warp-scan`.

## 0. Day 0 (human)

1. Protect `main`. Import tickets. Connect Jira and Bitbucket in Cursor.
2. Agents read this file, then the ticket, then the spec link on the ticket.

## 1. Waves

### W0 foundation — 3 tickets

- **platform**: API-01 ∥ API-02 ∥ UI-01

  **Gates before the next wave:**
  - *API-01* — boot: `make health` returns 200

### W1 features — 3 tickets

- **billing**: BILL-01 (after API-01, API-02)
- **web**: UI-02 (after UI-01) ∥ UI-03 (after UI-01, API-02)

  **Gates before the next wave:**
  - *BILL-01* — charge: fixture `fixtures/charge_ok.json` matches

### W2 ship — 1 ticket

- **release**: REL-01 (after BILL-01, UI-02, UI-03)

## 2. Ticket table

| id | size | locks | deps | summary |
|---|---|---|---|---|
| API-01 | M | services/api |  | Health and config load |
| API-02 | S | services/api/auth |  | Token middleware |
| UI-01 | L | apps/web |  | App shell |
| BILL-01 | L | services/billing | API-01, API-02 | Create charge |
| UI-02 | M | apps/web/billing | UI-01 | Billing page |
| UI-03 | S | apps/web/settings | UI-01, API-02 | Settings page |
| REL-01 | XL | . | BILL-01, UI-02, UI-03 | Release checklist |

Size S or M auto-merges after review. L and XL wait for approval. Locks that overlap do not run together.
