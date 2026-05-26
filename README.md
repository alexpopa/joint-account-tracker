# Joint Expense Tracker

Local-only Mac app for importing Chase SMS purchase alerts from Apple Messages, classifying joint expenses, and exporting monthly reimbursement totals.

## Privacy Model

- Runs locally on your Mac.
- Does not use Plaid, Chase APIs, cloud sync, or external services.
- Serves the web app on `127.0.0.1:8787` by default.
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
python -m joint_expense_tracker web
```

Open [http://127.0.0.1:8787](http://127.0.0.1:8787).

## Commands

```bash
python -m joint_expense_tracker init
python -m joint_expense_tracker diagnose-messages
python -m joint_expense_tracker import-messages
python -m joint_expense_tracker export --month 2026-05
python -m joint_expense_tracker web
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
5. Enter a manual joint amount for `Split`.
6. View monthly totals and export CSVs.

## Tests

```bash
pytest
```
