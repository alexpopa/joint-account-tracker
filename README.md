# Joint Expense Tracker

Local Mac web app that imports Chase purchase SMS alerts from Apple Messages and lets
you tag each purchase. It tells you how much to pull from the joint bank account each
month, how much to request via Venmo, and where the money went by category.

Coding agents: read [`AGENTS.md`](AGENTS.md) before changing anything. Planned work is
in [`ROADMAP.md`](ROADMAP.md).

## Privacy Model

- Runs locally on your Mac. Reachable from your other devices over Tailscale on port `8787`.
- Doesn't use Plaid, Chase APIs, cloud sync, or other external services.
- Stores app data in `~/.joint-expense-tracker/`.
- Copies `chat.db`, `chat.db-wal`, and `chat.db-shm` into `~/.joint-expense-tracker/messages_snapshot/` before querying.
- Opens the copied Messages database read-only.
- Never writes to Apple Messages or the live `~/Library/Messages/chat.db`.

## Setup

Requires [uv](https://docs.astral.sh/uv/) (`brew install uv`). uv installs the right
Python (3.15) and the dependencies:

```bash
uv sync
uv run python -m joint_expense_tracker init
uv run python -m joint_expense_tracker import-messages
uv run python -m joint_expense_tracker web --host 0.0.0.0 --port 8787
```

Open `http://<tailscale-ip-or-hostname>:8787` from a Tailscale-connected device, or [http://127.0.0.1:8787](http://127.0.0.1:8787) on this Mac.

### Run on login (LaunchAgent)

`launchd/com.alexandrupopa.joint-expense-tracker.plist` keeps the web app running.
Install it once:

```bash
cp launchd/com.alexandrupopa.joint-expense-tracker.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.alexandrupopa.joint-expense-tracker.plist
```

Restart it after code changes:

```bash
launchctl kickstart -k gui/$(id -u)/com.alexandrupopa.joint-expense-tracker
```

Logs are written to `launchd/manual-web.log` and `launchd/manual-web.err.log`.

## Full Disk Access

macOS only lets programs with Full Disk Access read Messages. The app runs the Python
interpreter behind `.venv/bin/python`. Find its real path with:

```bash
python3 -c "import os; print(os.path.realpath('.venv/bin/python'))"
```

Then go to System Settings → Privacy & Security → Full Disk Access, click **+**, press
⌘⇧G, paste that path, add it, and restart the LaunchAgent. If an import fails, the
Dashboard shows the exact path to grant. Upgrading Python means granting it again.

To check access:

```bash
uv run python -m joint_expense_tracker diagnose-messages
```

The diagnostic command prints whether `chat.db` exists, whether copying succeeded, and recent sender IDs with dollar-message counts. It doesn't print full message text.

## Commands

```bash
uv run python -m joint_expense_tracker init
uv run python -m joint_expense_tracker diagnose-messages
uv run python -m joint_expense_tracker import-messages
uv run python -m joint_expense_tracker export --month 2026-05
uv run python -m joint_expense_tracker backup
uv run python -m joint_expense_tracker web --host 0.0.0.0 --port 8787
```

## Data Files and Backups

- App database: `~/.joint-expense-tracker/joint_expenses.sqlite`
- Backups: `~/.joint-expense-tracker/backups/`
- Messages snapshot: `~/.joint-expense-tracker/messages_snapshot/`
- CSV exports: `~/.joint-expense-tracker/exports/`

The database is the only copy of your tagging work. The app backs it up automatically
once a day (on the first import), keeps the last 30 daily copies, and also backs up
before any database upgrade. Run `backup` for an extra copy anytime. To restore, stop
the LaunchAgent, copy a backup over `joint_expenses.sqlite`, and start it again.
Time Machine also covers this folder if it's enabled.

## Statuses

| Status | Use it for | Counts toward |
|---|---|---|
| Joint | Shared household expenses | Joint reimbursement (amount + tip) |
| Split | Partly shared purchases | Joint (the part you enter), Personal (the rest) |
| Venmo | Your wife used your card for herself | Venmo request (amount + tip) |
| Personal | Your own spending | Personal spending |
| Review | Not decided yet (default for new imports) | Nothing |
| Ignored | Card payments, deposits, refunds, noise | Nothing; hidden by default |

**Presents** are Personal spending with the category **Present**. The Present button
sets both in one click, and the Dashboard shows the Present total on its own.

## Categories

Joint, Split, and Personal purchases can have a category (Groceries, Dining, Video
games, ...). That powers the **Spending** page: "we spent $X on groceries from the joint
account," "I spent $Y on video games." Pick a category on a review card, on the Edit
page, or with a rule. Add or delete categories in
Settings. Deleting one removes it from any transactions and rules that use it.

## Spending page

For any month, Spending shows:

- the joint, personal, and Venmo totals, with the change from the previous month,
- the joint vs. personal split,
- joint and personal spending broken down by category (including Present),
- a single 12-month chart of joint vs. personal spending. Click a category to see just its trend.

## Workflow

1. Import alerts (Dashboard → Import). The Dashboard shows the result of the last
   import, including what to do if it failed.
2. New purchases start in Review unless a rule classifies them.
3. Map card last4 values to friendly names on Accounts.
4. Tag purchases at the top of the Dashboard. Each one waiting for review is a card.
   Type a tip if there is one, pick a category if you want, then tap Joint, Personal,
   Present, Venmo, or Ignored. If you tagged that merchant before, its usual button
   comes first, marked ↺. Every tag can be undone from the message at the bottom.

   **Splitting:** type an amount in the Split box and tap a button. That amount gets
   that tag, and the rest becomes a new card to tag on its own. The tip is added to
   the total first. Example: $100 dinner, $20 tip, Split 70, tap Joint → $70 Joint and
   a new $50 card. Tag that one Personal, Venmo, or anything else.
   Split Undo only rejoins the recorded pair, provided neither piece has changed.
   It restores the original purchase amount and note. Splits made before this
   safeguard was added must be corrected manually.
5. Add any purchase that didn't send an alert with **Add** on Transactions.
6. Check the Dashboard totals (tap one to copy it), move the money, and look at Spending.

The app is dark by default. Switch to Light or Auto (follow the phone) at the bottom of
the menu. On iPhone, Safari → Share → Add to Home Screen makes it open like an app.

### Refunds

Refunds are handled automatically. When a refund alert ("pending credit from …")
arrives, the app finds the matching purchase (same merchant, same amount, within the
previous 60 days) and marks both Ignored, even if you'd already tagged the purchase.
The purchase's note says when it was refunded and what it used to be. This can lower
a month you've already settled.

One exception: if the same charge shows up again within a day after the "refund," that
was a card hold being released and re-charged, so nothing is ignored.

### Rules

Rules match text in the merchant and/or raw alert and set a status, an optional
category, and an optional split for new imports. Lower priority numbers run first and
the first match wins. Rules don't change transactions that were already imported, and
they never apply to card payments or refunds.

## Development

```bash
uv run pytest          # tests
uv run ruff check .    # lint
uv run ruff format .   # format
```
