# Joint Expense Tracker

Local-only Mac app for importing Chase SMS purchase alerts from Apple Messages, classifying joint expenses, and exporting monthly reimbursement totals.

## Privacy Model

- Runs locally on your Mac.
- Does not use Plaid, Chase APIs, cloud sync, or external services.
- For this workspace, launch the web app for Tailscale access on port `8787`.
- Stores app data in `~/.joint-expense-tracker/`.
- Copies `chat.db`, `chat.db-wal`, and `chat.db-shm` into `~/.joint-expense-tracker/messages_snapshot/` before querying.
- Opens the copied Messages database read-only.
- Never writes to Apple Messages or the live `~/Library/Messages/chat.db`.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m joint_expense_tracker init
python -m joint_expense_tracker import-messages
python -m joint_expense_tracker web --host 0.0.0.0 --port 8787
```

Open `http://<tailscale-ip-or-hostname>:8787` from a Tailscale-connected device, or [http://127.0.0.1:8787](http://127.0.0.1:8787) on this Mac.

## Commands

```bash
python -m joint_expense_tracker init
python -m joint_expense_tracker diagnose-messages
python -m joint_expense_tracker import-messages
python -m joint_expense_tracker export --month 2026-05
python -m joint_expense_tracker web --host 0.0.0.0 --port 8787
```

## Full Disk Access

macOS may block access to Messages. If diagnosis or import fails:

System Settings -> Privacy & Security -> Full Disk Access -> enable Terminal, or the specific Python/IDE app being used.

Then retry:

```bash
python -m joint_expense_tracker diagnose-messages
```

The diagnostic command prints whether `chat.db` exists, whether copying succeeded, and recent sender IDs with dollar-message counts. It does not dump full message text by default.

## Data Files

- App database: `~/.joint-expense-tracker/joint_expenses.sqlite`
- Messages snapshot: `~/.joint-expense-tracker/messages_snapshot/`
- CSV exports: `~/.joint-expense-tracker/exports/`

## Workflow

1. Import Chase SMS alerts from Messages.
2. New transactions default to `Review` unless a rule classifies them.
3. Map card last4 values to friendly names on Accounts.
4. Classify transactions as `Joint`, `Personal`, `Split`, `Review`, or `Ignored`.
5. Classify gifts, dinners, and other spending for your wife as `Wife`; it remains included in Personal totals.
6. Enter a manual joint amount for `Split`.
7. View monthly totals and export CSVs.

## Tests

```bash
pytest
```

### Personal subcategories

In Transactions, edit a Personal transaction and choose a Personal subcategory from the dropdown. Food and Video games are included to start; choose “+ Add new subcategory…” to create a name for reuse. You can also assign a subcategory when manually adding a Personal transaction, filter Transactions by it, and include it in CSV exports.

Personal rules can assign subcategories to new imports. Existing Personal rules have an inline subcategory field and Save button. Updating a rule applies to future imports; existing transactions can be edited separately. Choosing “No subcategory” removes the assignment. Switching a transaction away from Personal clears its subcategory. Present and the Needs Review choices retain their existing behavior.

### Monthly trends

Open Trends to compare transaction counts and tracked amounts for every status by month. The default range covers twelve months through the latest transaction; choose From and To to change it. Missing months show zero transactions. Joint, Split, and Her use joint amounts; other statuses use original purchase amounts. Expand monthly detail for the underlying values.

### Venmo display label

The dashboard, transaction screens, and Trends display the stored `Her` status as Venmo. Existing records, form values, rules, and CSV exports retain `Her`; reimbursement calculations are unchanged.
