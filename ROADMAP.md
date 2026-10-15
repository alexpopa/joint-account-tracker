# Roadmap

Planned work, in priority order. Agents: check here before starting larger features.

## Phase 2: Statement reconciliation

**Problem.** SMS alerts are the only data source, and they're incomplete:

- some alerts never arrive, even with the alert threshold at $0,
- only Chase cards send alerts, so other cards have to be added by hand,
- refunds and adjustments show up inconsistently,
- restaurant alerts show the pre-tip amount, while the statement shows the final charge.

**Idea.** Once a month, download each card's statement as CSV or OFX/QFX (Chase:
Account → Download account activity) and import it on a new **Reconcile** page:

1. Parse the file into (date, merchant, amount) rows. Chase CSV columns are
   `Transaction Date, Post Date, Description, Category, Type, Amount, Memo`; OFX is
   a standard format with a stable `FITID` per transaction.
2. Match each statement row to an existing transaction: same card, same amount
   (or amount + tip), date within ±3 days, fuzzy merchant match. Auto-accept exact
   matches.
3. Show three lists: **matched** (nothing to do), **missing from the app** (one
   click to add, defaulting to Review), and **in the app but not on the statement**
   (likely pending, duplicate, or reversed).
4. Use the statement's final amount to correct tips and partial refunds.

Statements also cover non-Chase cards, so this would make SMS alerts a fast
early signal and the statement the source of truth. Keep everything local: the
user uploads the file, and nothing calls a bank API.

Design notes:

- Store a `statement_id` (e.g. OFX `FITID`) on matched transactions so a file can
  be re-imported safely.
- Fixture files in tests must be synthetic. Never commit real statements
  (`.gitignore` already blocks `*.csv`, `*.ofx`, `*.qfx`).

## Later ideas

- **Close a month.** Mark a month as settled after moving the money, so edits to
  that month warn first and totals you've already acted on can't change silently.
- **Category budgets.** Optional monthly target per category, shown as a meter on
  the Spending page.
- **Auto-import.** Run `import-messages` on a schedule (a second LaunchAgent with
  `StartInterval`) so the dashboard is always current.
- **Older Review backlog.** Months before May 2026 still have hundreds of rows in
  Review. Bulk actions (select many → status) would make clearing them practical.
