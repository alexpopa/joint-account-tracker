from __future__ import annotations

import argparse

import uvicorn

from .db import init_db
from .importer import diagnose_messages, import_messages
from .services import export_month


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m joint_expense_tracker")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("import-messages")
    sub.add_parser("diagnose-messages")
    export_parser = sub.add_parser("export")
    export_parser.add_argument("--month", required=True)
    web_parser = sub.add_parser("web")
    web_parser.add_argument("--host", default="127.0.0.1")
    web_parser.add_argument("--port", type=int, default=8787)

    args = parser.parse_args()
    if args.command == "init":
        init_db()
        print("Initialized ~/.joint-expense-tracker/joint_expenses.sqlite")
    elif args.command == "import-messages":
        init_db()
        result = import_messages()
        if result.error:
            print(f"Import finished with error: {result.error}")
        print(f"Messages seen: {result.messages_seen}")
        print(f"Messages imported: {result.messages_imported}")
    elif args.command == "diagnose-messages":
        init_db()
        for line in diagnose_messages():
            print(line)
    elif args.command == "export":
        init_db()
        transaction_path, summary_path = export_month(args.month)
        print(f"Transactions CSV: {transaction_path}")
        print(f"Summary CSV: {summary_path}")
    elif args.command == "web":
        init_db()
        uvicorn.run("joint_expense_tracker.web:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
