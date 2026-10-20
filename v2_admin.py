"""Operator commands for V2's data folder, run beside the app (on the host, in the released image).

    python v2_admin.py --data /data settings
    python v2_admin.py --data /data set user_daily_units 40
    python v2_admin.py --data /data quota tokens --user-daily 200000 --site-daily 2000000
    python v2_admin.py --data /data quota legacy
    python v2_admin.py --data /data test-unlimited enable USER_ID
    python v2_admin.py --data /data test-unlimited disable USER_ID
    python v2_admin.py --data /data accounts [--subject COGNITO_SUB]
    python v2_admin.py --data /data disable USER_ID
    python v2_admin.py --data /data enable USER_ID
    python v2_admin.py --data /data delete USER_ID --confirm USER_ID
    python v2_admin.py --data /data usage [--day 2026-10-01]
    python v2_admin.py --data /data sponsorship [--limit 500] [--after NEXT]

Nothing here signs anyone in or creates an account: an account belongs to one Cognito identity
(issuer and subject) and is made by its first verified sign-in, only while registration is open
and below max_accounts. Registration opens only once finite daily limits are set; tasks_enabled 0
stops new work at once (the kill switch), and the next model call of work already running too.
Disabling ends the account's sessions and stops its queued and running tasks; a model call
already under way may still be billed. Deleting removes the account's data and keeps a
tombstone in the database and in the deletion ledger beside it (written first), so neither
signing in again nor restoring an older backup reopens it. Output names accounts by internal ID
and Cognito subject only, never by CV content.

quota switches how daily limits count, in one step: "tokens" (prompt plus completion tokens of
each model call, as DeepSeek reports them; set with both daily token limits) or "legacy" (fixed
units per task, user_daily_units/site_daily_units, as before). A store without the setting is in
legacy mode, and nothing is ever converted from units to tokens. test-unlimited marks one account
(by internal ID, see accounts) as a test account with no daily limit: its calls are still metered
and count in the site's total; the kill switch, queue, deadlines and disabling still apply.

sponsorship works, with the local rules, on at most --limit saved texts that have no sponsor
answer under the current rules (postings and jobs saved before answers were kept, or after the
rules changed), and prints counts and "next". While "next" is not null, run it again with
--after set to that value: each round goes on where the last stopped, so a text that keeps
failing cannot hold up the others. Once "next" is null, a round without --after tries the
failed ones again. What is stored is never assessed again, and pages never assess anything.
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
    quota = commands.add_parser("quota", help="switch the quota mode (tokens needs both daily token limits)")
    quota.add_argument("mode", choices=tenant_store.QUOTA_MODES)
    quota.add_argument("--user-daily", type=int, help="tokens per account per UTC day (tokens mode)")
    quota.add_argument("--site-daily", type=int, help="tokens for the whole site per UTC day (tokens mode)")
    unlimited = commands.add_parser("test-unlimited", help="mark or unmark one account as an unlimited test account")
    unlimited.add_argument("action", choices=("enable", "disable"))
    unlimited.add_argument("user_id")
    listing = commands.add_parser("accounts", help="list accounts (ID, Cognito subject, state, dates)")
    listing.add_argument("--subject", help="only the account of this Cognito subject")
    for name in ("disable", "enable"):
        commands.add_parser(name, help=f"{name} one account").add_argument("user_id")
    delete = commands.add_parser("delete", help="delete one account's data for good")
    delete.add_argument("user_id")
    delete.add_argument("--confirm", required=True, help="the same user ID again")
    usage = commands.add_parser("usage", help="one UTC day: legacy units, metered model tokens, site and per account")
    usage.add_argument("--day")
    backfill = commands.add_parser("sponsorship", help="assess saved texts with no sponsor answer under the current rules")
    backfill.add_argument("--limit", type=int, default=500, help="at most this many texts (0 only counts)")
    backfill.add_argument("--after", help='go on after this position: the "next" a previous round printed')
    args = parser.parse_args(argv)
    database = args.data / DATABASE
    try:
        if args.command == "settings":
            result: object = tenant_store.get_settings(database)
        elif args.command == "set":
            tenant_store.set_setting(database, args.key, args.value)
            result = tenant_store.get_settings(database)
        elif args.command == "quota":
            result = tenant_store.configure_quota(database, args.mode, user_daily_tokens=args.user_daily,
                                                  site_daily_tokens=args.site_daily)
        elif args.command == "test-unlimited":
            tenant_store.upgrade_store(database)  # the tables, if the app has not started on this store yet
            result = tenant_store.set_test_unlimited(database, args.user_id, enabled=args.action == "enable")
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
        elif args.command == "sponsorship":
            tenant_store.upgrade_store(database)  # the tables, if the app has not started on this store yet
            result = tenant_store.backfill_sponsorship(database, limit=args.limit, after=args.after)
        else:
            tenant_store.upgrade_store(database)
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
