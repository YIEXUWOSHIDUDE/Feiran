"""Operator commands for V2's data folder, run beside the app (on the host, in the released image).

    python v2_admin.py settings --data /data
    python v2_admin.py set user_daily_units 40 --data /data
    python v2_admin.py accounts --data /data [--subject COGNITO_SUB]
    python v2_admin.py disable USER_ID --data /data
    python v2_admin.py enable USER_ID --data /data
    python v2_admin.py delete USER_ID --confirm USER_ID --data /data
    python v2_admin.py usage --data /data [--day 2026-10-01]

Nothing here signs anyone in or creates an account: an account belongs to one Cognito identity
(issuer and subject) and is made by its first verified sign-in, only while registration is open
and below max_accounts. Registration opens only once finite daily limits are set; tasks_enabled 0
stops new work at once (the kill switch). Disabling ends the account's sessions and stops its
queued and running tasks; a model call already under way may still be billed. Deleting removes
the account's data and keeps a tombstone in the database and in the deletion ledger beside it
(written first), so neither signing in again nor restoring an older backup reopens it. Output
names accounts by internal ID and Cognito subject only, never by CV content.
"""

import argparse
import json
import os
import sys
from pathlib import Path

import tenant_store
from tenant_store import DATABASE, NotFound, StoreError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", type=Path, default=Path(os.environ.get("WORKBENCH_DATA", ".local")))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("settings", help="show the site settings")
    change = commands.add_parser("set", help="change one site setting")
    change.add_argument("key", choices=tenant_store.SETTING_KEYS)
    change.add_argument("value")
    listing = commands.add_parser("accounts", help="list accounts (ID, Cognito subject, state, dates)")
    listing.add_argument("--subject", help="only the account of this Cognito subject")
    for name in ("disable", "enable"):
        commands.add_parser(name, help=f"{name} one account").add_argument("user_id")
    delete = commands.add_parser("delete", help="delete one account's data for good")
    delete.add_argument("user_id")
    delete.add_argument("--confirm", required=True, help="the same user ID again")
    usage = commands.add_parser("usage", help="units reserved on one day (UTC), site and per account")
    usage.add_argument("--day")
    args = parser.parse_args(argv)
    database = args.data / DATABASE
    try:
        if args.command == "settings":
            result: object = tenant_store.get_settings(database)
        elif args.command == "set":
            tenant_store.set_setting(database, args.key, args.value)
            result = tenant_store.get_settings(database)
        elif args.command == "accounts":
            result = [account for account in tenant_store.list_accounts(database)
                      if args.subject is None or account["subject"] == args.subject]
        elif args.command in ("disable", "enable"):
            tenant_store.set_user_active(database, args.user_id, active=args.command == "enable")
            result = {"user_id": args.user_id, "state": args.command + "d"}
        elif args.command == "delete":
            if args.confirm != args.user_id:
                print("refused: --confirm must repeat the user ID", file=sys.stderr)
                return 2
            record = tenant_store.delete_account(database, args.user_id, args.data / tenant_store.DELETION_LEDGER)
            result = {"user_id": record["user_id"], "deleted_at": record["deleted_at"]}
        else:
            result = tenant_store.usage_on(database, args.day)
    except NotFound as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    except StoreError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
