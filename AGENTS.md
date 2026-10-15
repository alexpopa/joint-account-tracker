# Agent Guide

Instructions for any coding agent (Claude Code, Codex, Cursor, etc.) working in
this repo. Humans should start with `README.md`; this file covers how the code
works and the rules for changing it. Planned work is in `ROADMAP.md`.

## What this app does

A single-user, local web app on the owner's Mac. It reads Chase purchase SMS
alerts out of Apple Messages, lets the owner tag each purchase, and computes:

1. how much to pull from the shared joint bank account each month,
2. how much to request from their partner via Venmo, and
3. where the money went, by category, for joint and personal spending.

Correct money totals are the point of the app. Treat any change that touches
amounts, statuses, month bucketing, or the dashboard/spending/export math as
high-risk: add or update a test for it.

## Statuses and money semantics

Statuses are stored as plain strings in `transactions.status`, and the UI shows
them exactly as stored (`config.STATUSES`). Old databases used `Her` and `Wife`;
migration 4 converted them.

| Status | Meaning | `joint_amount` | Counts as |
|---|---|---|---|
| `Joint` | Shared expense; reimburse from joint account | `amount + tip_amount` | joint spending |
| `Split` | Partly shared | user-entered, clamped `0..amount` | joint part → joint; rest (incl. tip) → personal |
| `Venmo` | Partner used the owner's card for herself | `amount + tip_amount` | neither; it's paid back |
| `Personal` | Owner's own spending | `0` | personal (`amount + tip_amount`) |
| `Review` | Default for new imports | `0` | nothing |
| `Ignored` | Card payments, deposits, refunds, noise | `0` | nothing |

**Splitting into two rows** (`services.split_transaction`) is separate from the `Split`
status. A review card's Split box cuts one purchase in two. The typed amount is tagged
with whichever button was tapped. The rest, `amount + tip − part`, becomes a new
`Review` row with the same merchant, date, card, and alert text. Neither row keeps a
tip, so the two always add up to the full charge. The original row keeps the
`source_guid`; the new row has none. Both rows get a note (`Split $X of $Y` /
`Rest of split …`). `unsplit_transaction` reverses it (the card's Undo).
Migration 6 adds `split_undo`: each new split records the exact parent/remainder IDs,
original amount and note, and snapshots of both pieces. Undo requires that recorded
pair and unchanged row contents (except `updated_at`), restores the saved amount,
and consumes the record. Browser-supplied tip amounts cannot change the restoration.
Older splits without a record must be edited manually; never infer a pair from
matching dates, alert text, or notes. Split pieces
are skipped when suggesting "last time". Split rows no longer match their alert's
amount, so a later refund for the full amount won't auto-pair with them.
The `Split` status (one row, joint part + personal rest) still exists for older data
and the Edit page.

**Presents** for the partner are `Personal` with the category `Present`
(`config.PRESENT`). "Present" is accepted as a status shortcut (the quick button,
rules) and stored as Personal + Present by `services.resolve_status`.

The per-row math is `rules.joint_amount_for_status`. Totals are defined once as SQL
fragments in `services.py` (`JOINT_SPEND_SQL`, `PERSONAL_SPEND_SQL`):

- Joint reimbursement = Σ `joint_amount` for `Joint` + `Split`
- Venmo request = Σ `joint_amount` for `Venmo`
- Personal spending = Σ `amount + tip_amount` for `Personal`, plus the non-joint part of `Split`
- Present spending = Σ `amount + tip_amount` for `Personal` with category `Present`

## Categories

Joint, Split, and Personal transactions can carry one category (Groceries,
Video games, ...). `config.CATEGORIZED_STATUSES` controls which statuses keep one;
moving a row to any other status clears it. Categories are a flat shared list, so
"Dining" can appear on both the joint and the personal side.

For historical reasons the storage names still say "subcategory". Code and UI say
"category":

| Concept | Stored as |
|---|---|
| a transaction's category | `transactions.subcategory` |
| a rule's category | `rules.default_subcategory` |
| the list of category names | table `personal_subcategories` (case-insensitive) |

The Spending page (`services.spending_data`) groups by category and shows
"Uncategorized" for rows without one.

## Code map

```
joint_expense_tracker/
  __main__.py   CLI: init, import-messages, diagnose-messages, export, backup, web
  config.py     Paths, timezone, statuses + labels, categorized statuses, starter categories
  db.py         Connection helper, schema, numbered migrations, backups, settings, categories
  importer.py   Snapshot-copies Messages chat.db, selects candidate SMS, inserts alerts
  parser.py     Regex parsing of alert text: amount, merchant, last4, default status, credits
  rules.py      Status/joint-amount math, tip math, rule matching on import
  services.py   Transaction writes, refund pairing, dashboard + spending aggregation, CSV export
  charts.py     Geometry for the server-rendered SVG trend chart
  web.py        FastAPI routes; Jinja templates; plain HTML forms with POST → 303 redirect
  templates/    Pages, plus partials: icons.html, month_switcher.html, category_input.html, category_script.html
  static/       styles.css (theme tokens on :root / [data-theme="dark"]; status colors --s-*; chart --series-*)
tests/          pytest suite; conftest.py isolates all data in a temp dir
launchd/        LaunchAgent plist that keeps the web app running on login
```

Data flow for an import: `importer.import_messages` takes the daily backup, copies
`chat.db` (+ WAL/SHM) into `messages_snapshot/`, opens the copy read-only, selects
incoming messages matching the sender/keyword filters from Settings, and calls
`parser.parse_alert` on each. `services.insert_alert` skips already-imported
messages, fills in the account name, applies rules, and inserts the row.
Candidates are selected newest-first, then inserted oldest-first. After the whole
batch is in, the importer calls `services.pair_refund` for each new credit, so a
purchase or re-charge from the same batch is already visible.

Behavior that's easy to get wrong:

- **Rules apply only at import time**, only to purchases (never to auto-Ignored
  payments or credits), and never retroactively. The first match by `priority`
  wins.
- **Deduplication** is by the permanent Messages `guid` (unique index), not the
  `ROWID`, which can be reused after a Messages database rebuild.
- **Dates are Eastern time** (`config.APP_TIMEZONE`). Imported rows store an ISO
  timestamp with offset (`2026-09-30T21:30:00-04:00`); manual rows store
  `YYYY-MM-DDT12:00:00`. `month` is derived from the local date.
- **Refund pairing** (`services.pair_refund`): a "pending credit from X" alert is
  matched to the latest same-merchant, same-amount, not-yet-Ignored purchase in the
  prior 60 days. Both are Ignored, whatever the purchase was tagged as (the owner's
  choice, even if it lowers a settled month). The purchase's note records its
  previous status. Exception: a same-merchant, same-amount charge within a day
  *after* the credit means a card hold was released and re-charged, so nothing is
  paired.
- **Amounts are floats** (`REAL`), rounded to cents on write and in SQL sums.

## Database migrations

`db.init_db` runs on every CLI command and on web startup. It:

1. creates any missing tables (`SCHEMA` is the current shape),
2. adds late-arriving columns (`_ensure_columns`),
3. runs pending entries of `db.MIGRATIONS` in order, tracking progress in
   `PRAGMA user_version`. It backs up the database first, and each migration runs
   in its own transaction.

To change the schema or fix data: update `SCHEMA` for fresh databases, **append**
a function to `MIGRATIONS` for existing ones, and add a test that runs it against
an old-shaped database (see `tests/test_migrations_and_import.py`). Never edit or
reorder migrations that have shipped. Never drop or rewrite user data without the
owner's explicit approval.

## Data and privacy rules

- The real database is `~/.joint-expense-tracker/joint_expenses.sqlite`. It holds
  months of manual classification work, so **don't write to it from scripts or
  experiments**. Read-only queries are fine. To try something risky, copy it
  (`sqlite3 SRC ".backup DST"`) into a temp directory and point
  `JOINT_EXPENSE_TRACKER_HOME` at that directory.
- Backups: `~/.joint-expense-tracker/backups/`. One per day on import (the newest
  30 are kept), plus labeled ones before migrations and from `backup`.
- Never write to `~/Library/Messages/chat.db`. Only read the snapshot copy.
- Never commit databases, CSV exports, message snapshots, or logs. `.gitignore`
  covers these.

## Tooling

- Python **3.15**, managed by [uv](https://docs.astral.sh/uv/). Dependencies are in
  `pyproject.toml` with exact versions locked in `uv.lock`. `uv sync` creates
  `.venv`.
- Tests: `uv run pytest`, or a focused run like `uv run pytest tests/test_categories.py`.
  Warnings are errors (`filterwarnings = error`), so fix deprecations rather than
  silencing them.
- Lint and format: `uv run ruff check .` and `uv run ruff format .`. Both must be
  clean before you finish.
- `.venv/bin/python -m ...` works the same as `uv run python -m ...`.

Test conventions:

- `tests/conftest.py` sets `JOINT_EXPENSE_TRACKER_HOME` to a temp dir before the
  package is imported and asserts it took effect, so tests can't reach real data.
- Use the fixtures: `conn` (open connection to a fresh migrated DB), `db_path`,
  and `client` (FastAPI `TestClient` on a fresh DB).
- Parser changes need a test with a realistic alert string. Copy a real Chase
  alert's wording but change the merchant and amount.
- Changes to totals need a test asserting the dashboard/spending numbers.

## Launch command

This app is meant to be reachable over Tailscale. Don't start it with the
default loopback-only bind unless the owner explicitly asks for local-only mode.

Check whether it's already listening:

```sh
lsof -nP -iTCP:8787 -sTCP:LISTEN
```

Normally the LaunchAgent is running it. If no code changes need loading, use that
process for checks. To load code changes, restart it:

```sh
launchctl kickstart -k gui/$(id -u)/com.alexandrupopa.joint-expense-tracker
```

Only start one by hand when nothing is listening:

```sh
uv run python -m joint_expense_tracker web --host 0.0.0.0 --port 8787
```

Before saying the app is up, verify the listener is `*:8787`, not only
`127.0.0.1:8787`.

The LaunchAgent runs `.venv/bin/python`. Importing from Messages requires macOS
Full Disk Access for the **real** interpreter behind that symlink (currently
`~/.local/share/uv/python/cpython-3.15.0-macos-aarch64-none/bin/python3.15`). If
the Python version changes (new venv, `uv python upgrade`), Full Disk Access must
be granted again. A failed import shows the exact path to grant on the Dashboard.
Logs go to `launchd/manual-web*.log`. If you edit the repo's plist, copy it to
`~/Library/LaunchAgents/` and re-bootstrap.

## Browser verification

Use a browser tool for visual checks when one is available. Otherwise, take
screenshots with Playwright driving the installed Chrome, which needs no browser
download:

```sh
uv run --no-project --with playwright python your_script.py   # chromium.launch(channel="chrome")
```

Headless Chrome's `--window-size` can't go narrower than about 500px, so use
Playwright device emulation (e.g. `p.devices["iPhone 13"]`) for phone layouts.
Check both desktop and phone widths after UI changes.

## UI notes

- Mobile first: the owner mostly uses a phone. Check iPhone width first, then desktop.
- Dark is the default theme. `base.html` sets `data-theme` before paint from
  `localStorage.theme` (`dark` | `light` | `auto`). Use the CSS tokens and never
  hard-code colors. Give status-colored elements `data-status="<Status>"`; that sets `--c`.
- The menu (`#menu`) and bottom sheets (`.sheet`) use the HTML `popover` attribute, so
  they open without JS.
- Review cards live on the Dashboard (`#review`). `/review` only redirects there.
  Each card is one form: tip and split boxes, then one swipeable row of status
  buttons, then one swipeable row of categories. Whichever button is tapped sends the
  tip, split, and category with it. With JS the card posts to the
  classify route with `fetch` (`Accept: application/json` returns `rest_id` for splits)
  and slides away. Undo posts `status=Review`, or `/unsplit` after a split. Without JS
  it's a plain POST → 303.
- Keep the UI free of explanatory text: no hints under buttons and no page blurbs.
  This app has one user who knows what everything means.
- `render()` in `web.py` adds the menu items and the review count (top-bar chip) to every page.

## Conventions

- Keep the app server-rendered: FastAPI + Jinja + HTML forms, small inline JS
  only where needed, and no frontend build step. Charts are SVG computed in
  `charts.py`.
- Use parameterized SQL (`?` placeholders) for all values.
- New statuses or labels go in `config.py`. Never hard-code a label in a template.
- Keep `README.md` user-facing (setup, workflow, features). Put implementation
  notes here and future plans in `ROADMAP.md`.
- Commit messages: short imperative summary, as in `git log`.
