"""The operator's commands on a synthetic V2 data folder."""

import contextlib
import io
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

import tenant_store
import v2_admin
from tenant_store import UserWorkspace

ISSUER = "https://cognito-idp.example.test/pool"


class AdminTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.data = Path(folder.name)
        self.database = self.data / tenant_store.DATABASE
        tenant_store.initialize_store(self.database)
        self.alice = tenant_store.provision_user(self.database, issuer=ISSUER, subject="sub-alice")
        self.bob = tenant_store.provision_user(self.database, issuer=ISSUER, subject="sub-bob")

    def admin(self, *argv: str) -> tuple[int, object, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = v2_admin.main(["--data", str(self.data), *argv])
        return code, json.loads(out.getvalue()) if out.getvalue() else None, err.getvalue()

    def test_registration_opens_only_with_finite_limits(self) -> None:
        code, _, said = self.admin("set", "registration_open", "1")
        self.assertEqual(code, 2)
        self.assertIn("refused", said)
        self.assertEqual(self.admin("set", "user_daily_units", "40")[0], 0)
        self.assertEqual(self.admin("set", "site_daily_units", "2000")[0], 0)
        code, settings, _ = self.admin("set", "registration_open", "1")
        self.assertEqual((code, settings["registration_open"]), (0, "1"))
        self.assertEqual(self.admin("set", "queue_limit", "-1")[0], 2)

    def test_the_kill_switch_stops_new_work(self) -> None:
        for key, value in (("user_daily_units", "40"), ("site_daily_units", "2000"), ("tasks_enabled", "0")):
            self.assertEqual(self.admin("set", key, value)[0], 0)
        with self.assertRaises(tenant_store.Refused) as refused:
            UserWorkspace(self.database, self.alice).submit_task("check_gaps", key="key-1", request={}, units=1,
                                                                 heavy="model")
        self.assertEqual(refused.exception.code, "tasks_paused")

    def test_accounts_are_listed_without_content_and_found_by_subject(self) -> None:
        code, accounts, _ = self.admin("accounts")
        self.assertEqual(code, 0)
        self.assertEqual({account["user_id"] for account in accounts}, {self.alice, self.bob})
        self.assertEqual(set(accounts[0]), {"user_id", "issuer", "subject", "state", "created_at", "deleted_at"})
        code, found, _ = self.admin("accounts", "--subject", "sub-bob")
        self.assertEqual([account["user_id"] for account in found], [self.bob])

    def test_disabling_ends_sessions_and_signing_in_is_refused(self) -> None:
        token, _ = tenant_store.create_session(self.database, self.alice, lifetime=timedelta(hours=1),
                                               idle=timedelta(minutes=30))
        self.assertEqual(self.admin("disable", self.alice)[0], 0)
        self.assertIsNone(tenant_store.resolve_session(self.database, token))
        with self.assertRaises(tenant_store.Refused):
            tenant_store.sign_in(self.database, issuer=ISSUER, subject="sub-alice")
        self.assertEqual(self.admin("enable", self.alice)[0], 0)
        self.assertEqual(tenant_store.sign_in(self.database, issuer=ISSUER, subject="sub-alice"), self.alice)
        self.assertEqual(self.admin("disable", "no-such-user")[0], 1)

    def test_deleting_needs_the_id_twice_and_writes_the_ledger_first(self) -> None:
        code, _, said = self.admin("delete", self.bob, "--confirm", self.alice)
        self.assertEqual(code, 2)
        self.assertEqual(tenant_store.read_deletions(self.data / tenant_store.DELETION_LEDGER), [])
        code, record, _ = self.admin("delete", self.bob, "--confirm", self.bob)
        self.assertEqual((code, record["user_id"]), (0, self.bob))
        ledger = tenant_store.read_deletions(self.data / tenant_store.DELETION_LEDGER)
        self.assertEqual([(entry["user_id"], entry["subject"]) for entry in ledger], [(self.bob, "sub-bob")])
        with self.assertRaises(tenant_store.Refused):
            tenant_store.sign_in(self.database, issuer=ISSUER, subject="sub-bob")
        states = {account["user_id"]: account["state"] for account in self.admin("accounts")[1]}
        self.assertEqual(states, {self.alice: "active", self.bob: "deleted"})

    def test_usage_shows_the_day_for_the_site_and_each_account(self) -> None:
        for key, value in (("user_daily_units", "40"), ("site_daily_units", "2000")):
            self.admin("set", key, value)
        UserWorkspace(self.database, self.alice).submit_task("check_gaps", key="key-1", request={}, units=3,
                                                             heavy="model")
        code, usage, _ = self.admin("usage")
        self.assertEqual(code, 0)
        self.assertEqual((usage["site"], usage["accounts"], usage["user_limit"], usage["site_limit"]),
                         (3, {self.alice: 3}, 40, 2000))
        self.assertEqual(self.admin("usage", "--day", "2000-01-01")[1]["site"], 0)


if __name__ == "__main__":
    unittest.main()
