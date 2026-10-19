"""Sponsor answers are worked out once, when words are saved, and pages only read them: V2's
shared answers for public postings and per-account answers for saved jobs, on real temporary
SQLite stores with synthetic postings. The analyser is the real local rule set wrapped by a
counting spy (this path has no model); a fault is injected only where a test says so."""
import contextlib
import hashlib
import io
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import listings
import sponsorship
import tenant_store
import v2_admin
import v2_backup
import web
from identity import Identity, OIDCSettings
from tenant_store import UserWorkspace
from tests.fake_identity import CLIENT_ID, DOMAIN, ISSUER, ORIGIN
from tests.test_cv import FakePrinter
from tests.test_listings import FakeBoards, posting
from tests.test_web import PROFILE, TOKEN, FakeDeepSeek
from tests.test_web_v2 import HOST, JD_TEXT, V2Case
from v2_flow import make_handlers
from web_v2 import V2Settings, create_v2_app

REAL = sponsorship.assess_sponsorship
SUPPORTED = "Requirements:\n- Python and SQL\nWe provide visa sponsorship."
REFUSED = "Requirements:\n- Figma\nWe cannot provide visa sponsorship."
BOARD = "https://job-boards.greenhouse.io/example"
SPONSOR_TABLES = ("public_sponsor_assessments", "user_sponsor_assessments")


class Analyser:
    """The real rules, with every call counted (thread-safe). ``fail`` names words the rules
    break on in this test: a stand-in for a fault, never for an answer."""

    def __init__(self, fail=None, delay=0.0):
        self.calls, self.lock = [], threading.Lock()
        self.fail, self.delay = fail or (lambda text: False), delay

    def __call__(self, text, **provenance):
        with self.lock:
            self.calls.append(text)
        time.sleep(self.delay)
        if self.fail(text):
            raise RuntimeError("synthetic fault in the rules")
        return REAL(text, **provenance)

    @contextlib.contextmanager
    def watching(self):
        """Every name the code could call the rules by, V1's included."""
        with patch.object(sponsorship, "assess_sponsorship", self), \
                patch.object(listings, "assess_sponsorship", self), patch.object(web, "assess_sponsorship", self):
            yield self


class Clock:
    def __init__(self):
        self.now = datetime.now(timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


def rows(database, table, where="1"):
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(f"SELECT * FROM {table} WHERE {where} ORDER BY 1, 2, 3")]
    finally:
        connection.close()


def dump(database, tables):
    connection = sqlite3.connect(database)
    try:
        return {name: sorted((tuple(row) for row in connection.execute(f"SELECT * FROM {name}")), key=repr)
                for name in tables}
    finally:
        connection.close()


def table_names(database):
    connection = sqlite3.connect(database)
    try:
        return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        connection.close()


class SavedAnswerCase(V2Case):
    def setUp(self):
        self.clock = Clock()
        clock = patch.object(tenant_store, "_clock", self.clock)
        clock.start()
        self.addCleanup(clock.stop)
        super().setUp()
        self.boards.jobs = [posting("1", "Python Intern", SUPPORTED, location="Boston"),
                            posting("2", "Designer", REFUSED),
                            posting("3", "Python Intern", SUPPORTED, location="Seattle")]

    def listing(self, client, headers, query=""):
        response = client.get("/api/listings" + query, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        return {item["job_id"]: item["sponsorship"] for group in response.json()["listings"]
                for item in group["postings"]}

    def review_listing(self, client, headers, job_id, key):
        response = client.post("/api/listings/start", json={"provider": "greenhouse", "board": "example",
                                                            "job_id": job_id, "review_only": True},
                               headers={**headers, "Idempotency-Key": key})
        task = self.settle(client, headers, response)
        self.assertEqual(task["status"], "succeeded", task)
        return task["result"]["job_id"]

    def paste(self, client, headers, text, key="paste", title="Backend Intern"):
        response = client.post("/api/jobs", json={"title": title, "text": text, "review_only": True},
                               headers={**headers, "Idempotency-Key": key})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["job_id"]

    def answer(self, client, headers, job_id):
        response = client.get(f"/api/jobs/{job_id}", headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["review"]["sponsorship"]

    def refresh(self, client, headers):
        response = client.post("/api/sources/greenhouse/example/refresh", headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()


class PublicAnswerTests(SavedAnswerCase):
    def test_first_read_assesses_each_text_once_and_reading_pages_assesses_nothing(self):
        alice, ah = self.browser("alice")
        bob, bh = self.browser("bob")
        with Analyser().watching() as spy:
            followed = alice.post("/api/sources", json={"link": BOARD}, headers=ah)
            self.assertEqual(followed.status_code, 200, followed.text)
            self.assertEqual(sorted(spy.calls), sorted([SUPPORTED, REFUSED]))  # three postings, two texts
            stored = dump(self.database, SPONSOR_TABLES)
            shown = self.listing(alice, ah)
            self.assertEqual({key: value["status"] for key, value in shown.items()},
                             {"1": "supported", "2": "not_supported", "3": "supported"})
            # One shared answer for the same words; each posting keeps its own source and capture time.
            self.assertEqual(shown["1"]["assessed_at"], shown["3"]["assessed_at"])
            self.assertEqual([shown[key]["source"] for key in ("1", "3")],
                             [f"{BOARD}/jobs/1", f"{BOARD}/jobs/3"])
            for _ in range(3):
                self.listing(alice, ah)
                self.listing(alice, ah, "?sponsorship=supported")
            job = self.review_listing(alice, ah, "1", "review-1")  # the same words alice already sees
            for _ in range(3):
                self.assertEqual(self.answer(alice, ah, job)["status"], "supported")
            # Bob follows the same board: the shared copy and its answers, nothing worked out again.
            self.assertEqual(bob.post("/api/sources", json={"link": BOARD}, headers=bh).status_code, 200)
            self.assertEqual(self.listing(bob, bh)["1"]["assessed_at"], shown["1"]["assessed_at"])
            self.assertEqual(len(spy.calls), 2)
        after = dump(self.database, SPONSOR_TABLES)
        self.assertEqual(after["public_sponsor_assessments"], stored["public_sponsor_assessments"])
        private = rows(self.database, "user_sponsor_assessments")
        self.assertEqual(len(private), 1)
        public = rows(self.database, "public_sponsor_assessments", "status = 'supported'")[0]
        self.assertEqual((private[0]["status"], private[0]["assessed_at"], private[0]["evidence"]),
                         (public["status"], public["assessed_at"], public["evidence"]))  # copied, not redone

    def test_an_unchanged_refresh_keeps_the_answer_and_moves_only_the_capture_time(self):
        client, headers = self.browser()
        client.post("/api/sources", json={"link": BOARD}, headers=headers)
        before = self.listing(client, headers)
        self.clock.advance(minutes=31)  # the shared copy is old enough to be read again
        with Analyser().watching() as spy:
            refreshed = self.refresh(client, headers)
            after = self.listing(client, headers)
        self.assertEqual(spy.calls, [])
        self.assertNotIn("cached", refreshed)
        self.assertEqual(self.boards.calls[-1], ("greenhouse", "example"))
        for key in ("1", "2", "3"):
            self.assertEqual((after[key]["status"], after[key]["assessed_at"], after[key]["evidence"]),
                             (before[key]["status"], before[key]["assessed_at"], before[key]["evidence"]))
            self.assertGreater(after[key]["captured_at"], before[key]["captured_at"])

    def test_changed_words_get_their_own_answer_and_a_saved_job_and_its_cv_keep_theirs(self):
        client, headers = self.ready_user()
        self.chat.requirement_lines = {"2、熟悉Python，了解SQL"}
        self.chat.plan = {"sections": ["education", "skills", "experience"], "entries": [], "reasons": []}
        original = JD_TEXT + "\nWe provide visa sponsorship."
        self.boards.jobs = [posting("1", "Python Intern", original)]
        client.post("/api/sources", json={"link": BOARD}, headers=headers)
        job = self.review_listing(client, headers, "1", "review")
        generated = client.post(f"/api/jobs/{job}/requirements/find", json={"expected_version": 0},
                                headers={**headers, "Idempotency-Key": "generate"})
        self.assertEqual(self.settle(client, headers, generated)["status"], "succeeded")
        saved = self.answer(client, headers, job)
        kept = ("jobs", "job_requirements", "materials", "material_facts", "approvals", "user_sponsor_assessments")
        before = dump(self.database, kept)
        self.assertTrue(before["materials"])
        changed = JD_TEXT + "\nWe cannot provide visa sponsorship for this role."
        self.boards.jobs = [posting("1", "Python Intern", changed)]
        self.clock.advance(minutes=31)
        with Analyser().watching() as spy:
            self.refresh(client, headers)
            listed = self.listing(client, headers)["1"]
            now = self.answer(client, headers, job)
        self.assertEqual(spy.calls, [changed])
        self.assertEqual(listed["status"], "not_supported")
        self.assertEqual(now, saved)  # the saved job's own words, answer, source and capture time
        self.assertEqual(saved["status"], "supported")
        self.assertEqual(dump(self.database, kept), before)
        # The shared answer for words no posting lists any more is gone; the saved job's copy stays.
        public = rows(self.database, "public_sponsor_assessments")
        self.assertEqual([row["status"] for row in public], ["not_supported"])

    def test_answers_survive_a_restart_with_nothing_kept_in_the_process(self):
        client, headers = self.browser()
        client.post("/api/sources", json={"link": BOARD}, headers=headers)
        job = self.paste(client, headers, REFUSED)
        before = (self.listing(client, headers), self.answer(client, headers, job))
        settings = OIDCSettings(issuer=ISSUER, client_id=CLIENT_ID, domain=DOMAIN, public_origin=ORIGIN,
                                client_secret="client-secret")
        with Analyser().watching() as spy:
            self.app = create_v2_app(
                database=self.database, identity=Identity(settings, self.database, post=self.cognito.post,
                                                          keys=self.cognito.keys),
                settings=V2Settings(public_host=HOST, public_origin=ORIGIN),
                handlers=make_handlers(printer=self.printer, posting=self.read_posting, page=self.read_page),
                model=self.chat, boards=self.boards, starter=[], ledger=self.root / "ledger.jsonl")
            restarted = TestClient(self.app, base_url=ORIGIN)
            restarted.__enter__()
            self.addCleanup(restarted.__exit__, None, None, None)
            client, headers = self.browser("alice")
            after = (self.listing(client, headers), self.answer(client, headers, job))
        self.assertEqual(spy.calls, [])
        self.assertEqual(after, before)


class FailureTests(SavedAnswerCase):
    def test_a_failed_public_assessment_is_not_an_answer_and_the_next_refresh_retries_it(self):
        client, headers = self.browser()
        broken = {"on": True}
        with Analyser(fail=lambda text: broken["on"] and "Figma" in text).watching() as spy:
            followed = client.post("/api/sources", json={"link": BOARD}, headers=headers)
            self.assertEqual(followed.status_code, 200, followed.text)
            self.assertEqual(followed.json()["added"]["open"], 3)  # the postings are saved all the same
            shown = self.listing(client, headers)
            self.assertEqual((shown["2"]["status"], shown["2"]["assessed_at"], shown["2"]["evidence"]),
                             ("not_assessed", None, []))
            self.assertEqual(set(self.listing(client, headers, "?sponsorship=unknown")), set())
            self.assertEqual(set(self.listing(client, headers, "?sponsorship=not_assessed")), {"2"})
            self.assertEqual(len(rows(self.database, "public_sponsor_assessments")), 1)
            calls = len(spy.calls)
            self.listing(client, headers)
            self.assertEqual(len(spy.calls), calls)  # reading never retries
            broken["on"] = False
            refreshed = self.refresh(client, headers)  # the shared copy is fresh: no board read, still a retry
            self.assertTrue(refreshed["cached"])
            self.assertEqual(refreshed["sponsorship"], {"assessed": 1, "failed": 0})
            self.assertEqual(self.listing(client, headers)["2"]["status"], "not_supported")
            self.assertEqual(spy.calls[calls:], [REFUSED])
            self.refresh(client, headers)
            self.assertEqual(spy.calls[calls:], [REFUSED])

    def test_a_failed_job_assessment_saves_the_job_and_an_explicit_check_recovers(self):
        client, headers = self.browser()
        broken = {"on": True}
        with Analyser(fail=lambda text: broken["on"]).watching() as spy:
            job = self.paste(client, headers, SUPPORTED)
            self.assertEqual(self.answer(client, headers, job)["status"], "not_assessed")
            self.assertEqual(spy.calls, [SUPPORTED])
            self.assertEqual(rows(self.database, "user_sponsor_assessments"), [])
            failed = client.post(f"/api/jobs/{job}/sponsorship", headers=headers)
            self.assertEqual((failed.status_code, failed.json()["code"]), (503, "sponsor_check_failed"))
            self.assertEqual(self.answer(client, headers, job)["status"], "not_assessed")
            broken["on"] = False
            checked = client.post(f"/api/jobs/{job}/sponsorship", headers=headers).json()["review"]["sponsorship"]
            self.assertEqual((checked["status"], checked["rule_version"]), ("supported", 1))
            self.assertEqual(client.post(f"/api/jobs/{job}/sponsorship", headers=headers).json()["review"]["sponsorship"],
                             checked)
            self.assertEqual(len(spy.calls), 3)  # the failure, the failure again, the recovery; never the repeat
            other, oh = self.browser("bob")
            self.assertEqual(other.post(f"/api/jobs/{job}/sponsorship", headers=oh).status_code, 404)
            self.assertEqual(client.post(f"/api/jobs/{job}/sponsorship").status_code, 403)  # no CSRF token

    def test_an_answer_that_does_not_quote_the_text_is_never_stored(self):
        database = self.database
        lying = lambda text, **_: {"status": "supported", "evidence": [
            {"text": "We sponsor every visa.", "start": 0, "end": 22, "signal": "supported"}]}
        with patch.object(sponsorship, "assess_sponsorship", lying):
            counts = tenant_store.store_public_board(database, "greenhouse", "example", "Example",
                                                     [posting("1", "Engineer", SUPPORTED)])
        self.assertEqual(counts["sponsorship"], {"assessed": 0, "failed": 1})
        self.assertEqual(rows(database, "public_sponsor_assessments"), [])


class ConcurrencyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.database = Path(temp.name) / "v2.db"
        tenant_store.initialize_store(self.database)

    def test_simultaneous_first_reads_assess_each_text_once(self):
        texts = [SUPPORTED, REFUSED, "Go and Rust."]
        board = [posting(str(index), "Engineer", text) for index, text in enumerate(texts)]
        elsewhere = [posting("9", "Engineer", SUPPORTED)]  # another board listing the same words
        start, errors = threading.Barrier(3), []

        def read(name, postings):
            try:
                start.wait()
                tenant_store.store_public_board(self.database, "greenhouse", name, name, postings)
            except Exception as exc:  # reported below
                errors.append(exc)

        with Analyser(delay=0.02).watching() as spy:
            threads = [threading.Thread(target=read, args=args)
                       for args in (("example", board), ("example", board), ("other", elsewhere))]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(spy.calls), sorted(texts))
        self.assertEqual(len(rows(self.database, "public_sponsor_assessments")), 3)

    def save(self, user, text=SUPPORTED, key="save"):
        return user.create_job({"text": text, "title": "Engineer", "source": None,
                                "captured_at": "2026-10-18T00:00:00+00:00"}, request_id=key)["job_id"]

    def users(self, *subjects, following=()):
        made = [UserWorkspace(self.database, tenant_store.provision_user(self.database, issuer=ISSUER, subject=subject))
                for subject in subjects]
        for user in made:
            if user.user_id in {made[index].user_id for index in following}:
                user.follow_board("greenhouse", "example", "Example")
        return made

    def test_followers_saving_public_words_before_the_board_assessment_runs_share_one_answer(self):
        """The board's postings are committed and its own assessment has not run yet, while two
        followers and one outsider save the same words."""
        first, second, outsider = self.users("a", "b", "outsider", following=(0, 1))
        reached, release = threading.Event(), threading.Event()
        board_assessment = tenant_store._assess_public

        def paused(path, hashes):
            reached.set()
            if not release.wait(10):
                raise TimeoutError("the test never released the board's assessment")
            return board_assessment(path, hashes)

        with Analyser().watching() as spy, patch.object(tenant_store, "_assess_public", paused), \
                ThreadPoolExecutor(max_workers=3) as pool:
            board = pool.submit(tenant_store.store_public_board, self.database, "greenhouse", "example", "Example",
                                [posting("1", "Engineer", SUPPORTED)])
            try:
                self.assertTrue(reached.wait(5))
                pool.submit(self.save, outsider).result(10)  # not following: assessed for them alone
                self.assertEqual((len(spy.calls), rows(self.database, "public_sponsor_assessments")), (1, []))
                for saved in [pool.submit(self.save, user) for user in (first, second)]:
                    saved.result(10)
                during = len(spy.calls)
            finally:
                release.set()
            result = board.result(10)
        self.assertEqual(during, 2)  # the outsider's own, and one shared answer made from the stored posting
        self.assertEqual(len(spy.calls), 2)
        self.assertEqual(result["sponsorship"], {"assessed": 0, "failed": 0})
        public = rows(self.database, "public_sponsor_assessments")
        self.assertEqual(len(public), 1)
        mine = {row["user_id"]: row for row in rows(self.database, "user_sponsor_assessments")}
        self.assertEqual(len(mine), 3)
        for user in (first, second):
            self.assertEqual((mine[user.user_id]["status"], mine[user.user_id]["evidence"], mine[user.user_id]["assessed_at"]),
                             (public[0]["status"], public[0]["evidence"], public[0]["assessed_at"]))

    def test_a_follower_check_failing_in_that_gap_saves_nothing_and_is_retried_from_the_posting(self):
        first, second = self.users("a", "b", following=(0, 1))
        with patch.object(tenant_store, "_assess_public", return_value={"assessed": 0, "failed": 0}):
            tenant_store.store_public_board(self.database, "greenhouse", "example", "Example",
                                            [posting("1", "Engineer", SUPPORTED)])
        with Analyser(fail=lambda text: True).watching() as broken:
            job = self.save(first)
        self.assertEqual(len(broken.calls), 1)
        self.assertEqual((rows(self.database, "public_sponsor_assessments"),
                          rows(self.database, "user_sponsor_assessments")), ([], []))
        with Analyser().watching() as spy:
            self.assertEqual(first.assess_job_sponsorship(job), "assessed")
            self.save(second)
            refreshed = tenant_store.store_public_board(self.database, "greenhouse", "example", "Example",
                                                        [posting("1", "Engineer", SUPPORTED)])
        self.assertEqual(spy.calls, [SUPPORTED])  # made once from the posting, then shared
        self.assertEqual(refreshed["sponsorship"], {"assessed": 0, "failed": 0})
        self.assertEqual(len(rows(self.database, "public_sponsor_assessments")), 1)
        self.assertEqual({row["status"] for row in rows(self.database, "user_sponsor_assessments")}, {"supported"})

    def test_simultaneous_checks_of_one_saved_job_assess_it_once(self):
        user = UserWorkspace(self.database, tenant_store.provision_user(self.database, issuer=ISSUER, subject="a"))
        with Analyser(fail=lambda text: True).watching():
            job = user.create_job({"text": SUPPORTED, "title": "Engineer", "source": None,
                                   "captured_at": "2026-10-18T00:00:00+00:00"}, request_id="r")
        start, outcomes = threading.Barrier(4), []

        def check():
            start.wait()
            outcomes.append(user.assess_job_sponsorship(job["job_id"]))

        with Analyser(delay=0.02).watching() as spy:
            threads = [threading.Thread(target=check) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
        self.assertEqual(spy.calls, [SUPPORTED])
        self.assertEqual(sorted(outcomes), ["assessed", "stored", "stored", "stored"])


class BackfillTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data = Path(temp.name) / "data"
        self.data.mkdir()
        self.database = self.data / tenant_store.DATABASE
        tenant_store.initialize_store(self.database)

    def admin(self, *args):
        with contextlib.redirect_stdout(io.StringIO()) as written, contextlib.redirect_stderr(io.StringIO()) as said:
            code = v2_admin.main(["--data", str(self.data), *args])
        return code, (json.loads(written.getvalue()) if code == 0 else said.getvalue())

    def test_a_text_that_keeps_failing_does_not_hold_up_public_or_private_ones_behind_it(self):
        texts = ["Synthetic alpha. We provide visa sponsorship.", "Synthetic beta. We provide visa sponsorship."]
        low, high = sorted(texts, key=lambda text: hashlib.sha256(text.encode()).hexdigest())
        with patch.object(tenant_store, "_assess_public", return_value={"assessed": 0, "failed": 0}):
            tenant_store.store_public_board(self.database, "greenhouse", "example", "Example",
                                            [posting("1", "Engineer", low), posting("2", "Engineer", high)])
        user = UserWorkspace(self.database, tenant_store.provision_user(self.database, issuer=ISSUER, subject="a"))
        mine = "Synthetic private internship. Visa sponsorship is available."
        with patch.object(tenant_store, "_sponsor_answer", return_value=None):
            user.create_job({"text": mine, "title": "Intern", "source": None,
                             "captured_at": "2026-10-18T00:00:00+00:00"}, request_id="mine")
        attempts = Counter()

        def first_always_fails(text):
            attempts[{low: "first", high: "second"}.get(text, "private")] += 1
            return text == low

        with Analyser(fail=first_always_fails).watching():
            first = tenant_store.backfill_sponsorship(self.database, limit=1)
            self.assertEqual((first["public"]["failed"], first["next"]), (1, f"public:{hashlib.sha256(low.encode()).hexdigest()}"))
            code, second = self.admin("sponsorship", "--limit", "1", "--after", first["next"])  # the operator goes on
            self.assertEqual((code, second["public"]["assessed"]), (0, 1))
            third = tenant_store.backfill_sponsorship(self.database, limit=1, after=second["next"])
            self.assertEqual((third["private"]["assessed"], third["next"]), (1, None))  # the end is reached
            self.assertEqual(attempts, Counter(first=1, second=1, private=1))  # one text per round, none starved
            self.assertEqual((third["public"]["remaining"], third["private"]["remaining"]), (1, 0))
            # A round from the start tries the failing text again, still one text per round.
            again = tenant_store.backfill_sponsorship(self.database, limit=1)
            self.assertEqual((again["public"]["failed"], again["public"]["remaining"], again["next"]), (1, 1, None))
            self.assertEqual(attempts, Counter(first=2, second=1, private=1))
            # A position the command never printed is refused before anything is worked on.
            for made_up in ("public:abc", "somewhere", "private:only-one-part"):
                code, said = self.admin("sponsorship", "--after", made_up)
                self.assertEqual(code, 2)
                self.assertIn("refused", said)
            self.assertEqual(attempts, Counter(first=2, second=1, private=1))
        self.assertEqual(len(rows(self.database, "public_sponsor_assessments")), 1)
        self.assertEqual([row["status"] for row in rows(self.database, "user_sponsor_assessments")], ["supported"])


class ListingTargetTests(SavedAnswerCase):
    def test_each_location_of_a_role_opens_the_job_saved_from_it(self):
        client, headers = self.browser()
        client.post("/api/sources", json={"link": BOARD}, headers=headers)
        job = self.review_listing(client, headers, "3", "seattle")  # the role's second location
        groups = client.get("/api/listings", headers=headers).json()["listings"]
        role = next(group for group in groups if group["title"] == "Python Intern")
        self.assertEqual([(item["location"], item["source"], item["started_job"]) for item in role["postings"]],
                         [("Boston", f"{BOARD}/jobs/1", None), ("Seattle", f"{BOARD}/jobs/3", job)])
        self.assertEqual(role["started_job"], job)  # the role still names a saved job, as before
        other = next(group for group in groups if group["title"] == "Designer")
        self.assertEqual(([item["started_job"] for item in other["postings"]], other["started_job"]), ([None], None))


class PrivacyTests(SavedAnswerCase):
    def test_pasted_words_stay_with_their_account_even_when_others_see_the_same_words(self):
        alice, ah = self.browser("alice")
        bob, bh = self.browser("bob")
        bob.post("/api/sources", json={"link": BOARD}, headers=bh)
        public = rows(self.database, "public_sponsor_assessments")
        self.clock.advance(minutes=5)
        with Analyser().watching() as spy:
            # Alice follows nothing: her paste of words bob's board lists is assessed for her alone.
            job = self.paste(alice, ah, SUPPORTED)
            mine = self.answer(alice, ah, job)
            secret = "Confidential role. We provide visa sponsorship to staff in Lisbon."
            other = self.paste(alice, ah, secret, key="paste-2", title="Private")
        self.assertEqual(spy.calls, [SUPPORTED, secret])
        self.assertEqual(rows(self.database, "public_sponsor_assessments"), public)  # nothing of hers is shared
        self.assertNotEqual(mine["assessed_at"], self.listing(bob, bh)["1"]["assessed_at"])
        self.assertEqual(bob.get(f"/api/jobs/{other}", headers=bh).status_code, 404)
        self.assertEqual(bob.post(f"/api/jobs/{other}/sponsorship", headers=bh).status_code, 404)
        owners = {row["user_id"] for row in rows(self.database, "user_sponsor_assessments")}
        self.assertEqual(len(owners), 1)  # alice's alone: bob saved no job
        alice_id = owners.pop()
        # Deleting her account takes her answers with it; the shared ones stay for bob.
        tenant_store.delete_account(self.database, alice_id, self.root / "ledger.jsonl")
        self.assertEqual(rows(self.database, "user_sponsor_assessments"), [])
        self.assertEqual(rows(self.database, "public_sponsor_assessments"), public)
        self.assertEqual(self.listing(bob, bh)["1"]["status"], "supported")


class RuleVersionTests(SavedAnswerCase):
    def test_new_rules_never_pass_off_old_answers_and_are_filled_in_a_bounded_way(self):
        client, headers = self.browser()
        client.post("/api/sources", json={"link": BOARD}, headers=headers)
        job = self.paste(client, headers, "We provide visa sponsorship. Kotlin.")
        with patch.object(sponsorship, "RULE_VERSION", 2), Analyser().watching() as spy:
            listed = self.listing(client, headers)
            self.assertEqual({value["status"] for value in listed.values()}, {"not_assessed"})
            self.assertEqual(self.answer(client, headers, job)["status"], "not_assessed")
            self.assertEqual(spy.calls, [])
            self.assertEqual({row["rule_version"] for row in rows(self.database, "public_sponsor_assessments")}, {1})
            first = tenant_store.backfill_sponsorship(self.database, limit=1)
            self.assertEqual((first["rule_version"], first["public"]["assessed"], first["public"]["remaining"],
                              first["private"]["remaining"]), (2, 1, 1, 1))
            rest = tenant_store.backfill_sponsorship(self.database, limit=100)
            self.assertEqual((rest["public"]["assessed"], rest["private"]["assessed"],
                              rest["public"]["remaining"], rest["private"]["remaining"]), (1, 1, 0, 0))
            self.assertEqual(len(spy.calls), 3)
            again = tenant_store.backfill_sponsorship(self.database, limit=100)
            self.assertEqual((again["public"]["assessed"], again["private"]["assessed"]), (0, 0))
            self.assertEqual(len(spy.calls), 3)
            listed = self.listing(client, headers)
            self.assertEqual({value["rule_version"] for value in listed.values()}, {2})
            self.assertEqual(self.answer(client, headers, job)["rule_version"], 2)
        # The newer answers replaced the older ones for the same words.
        self.assertEqual({row["rule_version"] for row in rows(self.database, "public_sponsor_assessments")}, {2})
        self.assertEqual({row["rule_version"] for row in rows(self.database, "user_sponsor_assessments")}, {2})


class UpgradeTests(unittest.TestCase):
    """A store the release before sponsor answers made: format 3 without their tables."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.database = self.data / tenant_store.DATABASE
        self.ledger = self.data / tenant_store.DELETION_LEDGER
        tenant_store.initialize_store(self.database)
        tenant_store.ensure_ledger(self.database, self.ledger)
        self.a = UserWorkspace(self.database, tenant_store.provision_user(self.database, issuer=ISSUER, subject="a"))
        self.b = UserWorkspace(self.database, tenant_store.provision_user(self.database, issuer=ISSUER, subject="b"))
        tenant_store.store_public_board(self.database, "greenhouse", "example", "Example",
                                        [posting("1", "Engineer", SUPPORTED), posting("2", "Designer", REFUSED)])
        self.a.follow_board("greenhouse", "example", "Example")
        self.a.import_facts([{"text": "Built Python services.", "type": "skill", "tags": ["Python"]}])
        self.job = self.a.create_job({"text": "We cannot sponsor visas.", "title": "Analyst", "source": None,
                                      "captured_at": "2026-10-01T00:00:00+00:00"}, request_id="old")["job_id"]
        self.b.create_job({"text": REFUSED, "title": "Designer", "source": None,
                           "captured_at": "2026-10-02T00:00:00+00:00"}, request_id="old")
        connection = sqlite3.connect(self.database)
        try:
            for table in SPONSOR_TABLES:
                connection.execute(f"DROP TABLE {table}")
            connection.commit()
        finally:
            connection.close()
        self.old_tables = sorted(table_names(self.database))
        self.assertFalse(set(SPONSOR_TABLES) & set(self.old_tables))

    def test_upgrade_adds_only_new_tables_repeatably_and_pages_wait_for_backfill(self):
        before = dump(self.database, self.old_tables)
        self.assertEqual(v2_backup.verify_database(self.database)["problems"], [])  # the install check, before
        with Analyser().watching() as spy:
            self.assertEqual(tenant_store.upgrade_store(self.database), list(SPONSOR_TABLES))
            tenant_store.initialize_store(self.database)  # as each start of the app runs it
            self.assertEqual(tenant_store.upgrade_store(self.database), [])
            self.assertEqual(dump(self.database, self.old_tables), before)
            self.assertEqual(spy.calls, [])
            # Saved before answers were kept: shown as not assessed, and reading does not assess.
            listed = self.a.ranking_rows([], lambda text: [])
            self.assertEqual({row["sponsorship"]["status"] for row in listed}, {"not_assessed"})
            self.assertIsNone(self.a.job_snapshot(self.job)["sponsorship"])
            self.assertEqual(spy.calls, [])
            output = self.admin("sponsorship", "--limit", "0")
            self.assertEqual((output["public"]["remaining"], output["private"]["remaining"]), (2, 2))
            self.assertEqual(spy.calls, [])
            output = self.admin("sponsorship", "--limit", "3")
            self.assertEqual((output["public"]["assessed"], output["private"]["assessed"],
                              output["private"]["remaining"]), (2, 1, 1))
            output = self.admin("sponsorship")
            self.assertEqual((output["private"]["assessed"], output["private"]["remaining"]), (1, 0))
            self.assertEqual(len(spy.calls), 4)
            self.assertEqual(self.admin("sponsorship")["private"]["assessed"], 0)
            self.assertEqual(len(spy.calls), 4)
        # posting_matches is the skill-word cache that ranking_rows has always refreshed on reading.
        kept = [name for name in self.old_tables if name != "posting_matches"]
        self.assertEqual(dump(self.database, kept), {name: before[name] for name in kept})
        self.assertEqual(self.a.job_snapshot(self.job)["sponsorship"]["status"], "not_supported")
        self.assertEqual(v2_backup.verify_database(self.database)["problems"], [])  # and after

    def test_a_backup_from_before_restores_and_deletes_again_with_this_release(self):
        archive = self.root / "before.tar.gz"
        self.assertEqual(v2_backup.create_backup(self.data, archive)["problems"], [])
        tenant_store.initialize_store(self.database)  # the live store moves on to this release
        tenant_store.backfill_sponsorship(self.database, limit=10)
        self.assertTrue(rows(self.database, "user_sponsor_assessments", f"user_id = '{self.a.user_id}'"))
        tenant_store.delete_account(self.database, self.a.user_id, self.ledger)
        self.assertEqual(rows(self.database, "user_sponsor_assessments", f"user_id = '{self.a.user_id}'"), [])
        restored = self.root / "restored"
        # The old copy has no sponsor tables, yet deleting the account again there must work.
        result = v2_backup.restore_backup(archive, restored, live_ledger=self.ledger)
        self.assertEqual((result["deleted_again"], result["problems"]), (1, []))
        tenant_store.initialize_store(restored / tenant_store.DATABASE)
        with self.assertRaises(tenant_store.NotFound):
            UserWorkspace(restored / tenant_store.DATABASE, self.a.user_id).list_jobs()
        self.assertEqual(len(UserWorkspace(restored / tenant_store.DATABASE, self.b.user_id).list_jobs()), 1)

    def test_answers_left_by_an_older_release_for_a_deleted_account_go_at_the_next_start(self):
        tenant_store.initialize_store(self.database)
        tenant_store.backfill_sponsorship(self.database, limit=10)
        # An older release deletes the account without knowing the table: its rows stay behind.
        connection = sqlite3.connect(self.database)
        try:
            connection.execute("UPDATE users SET active = 0, deleted_at = '2026-10-18T00:00:00+00:00' WHERE user_id = ?",
                               (self.a.user_id,))
            connection.commit()
        finally:
            connection.close()
        self.assertTrue(rows(self.database, "user_sponsor_assessments", f"user_id = '{self.a.user_id}'"))
        tenant_store.initialize_store(self.database)
        self.assertEqual(rows(self.database, "user_sponsor_assessments", f"user_id = '{self.a.user_id}'"), [])
        self.assertTrue(rows(self.database, "user_sponsor_assessments", f"user_id = '{self.b.user_id}'"))

    def admin(self, *args):
        with contextlib.redirect_stdout(io.StringIO()) as written:
            self.assertEqual(v2_admin.main(["--data", str(self.data), *args]), 0)
        return json.loads(written.getvalue())


class V1CompatibilityTests(unittest.TestCase):
    """The single-owner app keeps no answers: it works the local rules out on each read."""

    def test_v1_pages_show_answers_worked_out_on_reading_and_store_none(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "cv-profile.json").write_text(json.dumps(PROFILE), encoding="utf-8")
            boards = FakeBoards([posting("1", "Engineer", SUPPORTED), posting("2", "Designer", REFUSED)])
            app = web.create_app(facts_db=root / "workbench.db", jobs_root=root / "jobs", token=TOKEN,
                                 profile_path=root / "cv-profile.json", chat=FakeDeepSeek(), printer=FakePrinter(),
                                 starter=[], boards=boards)
            client = TestClient(app, base_url="http://127.0.0.1:8765", headers={"X-Workbench-Token": TOKEN})
            self.assertEqual(client.post("/api/sources", json={"link": BOARD}).status_code, 200)
            with Analyser().watching() as spy:
                ranked = client.get("/api/listings").json()["listings"]
                job = client.post("/api/jobs", json={"title": "Engineer", "text": REFUSED}).json()["job_id"]
                review = client.get(f"/api/jobs/{job}").json()["review"]["sponsorship"]
            statuses = {item["postings"][0]["job_id"]: item["sponsorship"] for item in ranked}
            self.assertEqual({key: value["status"] for key, value in statuses.items()},
                             {"1": "supported", "2": "not_supported"})
            self.assertEqual({value["assessed_at"] for value in statuses.values()}, {None})
            self.assertEqual((review["status"], review["rule_version"]), ("not_supported", 1))
            self.assertGreaterEqual(len(spy.calls), 3)  # worked out while reading, as before
            self.assertFalse(set(SPONSOR_TABLES) & table_names(root / "listings.db"))

    def test_v1_each_location_of_a_role_opens_the_job_started_from_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "cv-profile.json").write_text(json.dumps(PROFILE), encoding="utf-8")
            boards = FakeBoards([posting("1", "Engineer", SUPPORTED, location="Boston"),
                                 posting("3", "Engineer", SUPPORTED, location="Seattle")])

            def selected(board, job_id, provider="greenhouse"):  # the posting as its board returns it; no network
                return {"jd": dict(next(job for job in boards.jobs if job["job_id"] == job_id)), "source_status": "test"}

            app = web.create_app(facts_db=root / "workbench.db", jobs_root=root / "jobs", token=TOKEN,
                                 profile_path=root / "cv-profile.json", chat=FakeDeepSeek(), printer=FakePrinter(),
                                 starter=[], boards=boards, selected_posting=selected)
            client = TestClient(app, base_url="http://127.0.0.1:8765", headers={"X-Workbench-Token": TOKEN})
            self.assertEqual(client.post("/api/sources", json={"link": BOARD}).status_code, 200)
            started = client.post("/api/listings/start", json={"provider": "greenhouse", "board": "example",
                                                               "job_id": "3"})
            self.assertEqual(started.status_code, 200, started.text)
            role = client.get("/api/listings").json()["listings"][0]
            self.assertEqual([(item["location"], item["started_job"]) for item in role["postings"]],
                             [("Boston", None), ("Seattle", started.json()["job_id"])])
            self.assertEqual(role["started_job"], started.json()["job_id"])


if __name__ == "__main__":
    unittest.main()
