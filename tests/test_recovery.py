"""Going back to where the user last worked (feiran.recent_work): the kept place's shape, both
apps' /api/recent (one place per account, only the account's own jobs, nothing but IDs, a
language, a part of the page and CV versions), what deleting an account, ending a guest
workspace, upgrading and backing up do with it, and the page: going back only when opened with
no address, once, to the language and part kept, with one line when a CV changed since; the one
next step marked from the state as it is; saving a moment later, never for a page already left,
and never over a newer place when an older tab closes. Synthetic accounts and jobs; the model and
the printer are the tests' stand-ins (FakeDeepSeek, FakePrinter), never a real model."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from feiran import recent_work, tenant_store, v2_backup, web
from feiran.recent_work import PlaceError, clean_place, still_there
from tests import test_guest_workspace, test_web, test_web_v2
from tests.test_sponsor_ui import NODE, SponsorUITests

A, B = "a" * 64, "b" * 64


def job_place(job_id, **fields):
    return {"kind": "job", "jobs": [job_id], "language": "en", "area": "cv", "seen": {}} | fields


class PlaceRuleTests(unittest.TestCase):
    def test_a_place_holds_only_ids_a_language_an_area_and_versions(self):
        kept = clean_place({"kind": "batch", "jobs": ["j1", "j2"], "language": None, "area": "batch",
                            "seen": {"j1": {"en": A, "zh": None}, "j2": {}}})
        self.assertEqual(kept, {"kind": "batch", "jobs": ["j1", "j2"], "language": None, "area": "batch",
                                "seen": {"j1": {"en": A}}})
        self.assertEqual(clean_place({"kind": "job", "jobs": ["j1"]})["seen"], {})
        for bad in [None, [], {"kind": "job", "jobs": []}, {"kind": "other", "jobs": ["j1"]},
                    {"kind": "job", "jobs": ["j1", "j2"]}, {"kind": "batch", "jobs": ["j1", "j1"]},
                    {"kind": "batch", "jobs": [f"j{n}" for n in range(21)]}, {"kind": "job", "jobs": ["../x"]},
                    job_place("j1", language="fr"), job_place("j1", area="anywhere"),
                    job_place("j1", seen={"j2": {"en": A}}), job_place("j1", seen={"j1": {"en": "not-a-hash"}}),
                    job_place("j1", seen={"j1": {"fr": A}}), job_place("j1", text="a CV line"),
                    job_place("j1", seen={"j1": {"en": A.upper()}})]:
            with self.subTest(bad=bad), self.assertRaises(PlaceError):
                clean_place(bad)

    def test_only_jobs_still_there_are_answered(self):
        place = {"kind": "batch", "jobs": ["j1", "j2"], "language": None, "area": "batch",
                 "seen": {"j1": {"en": A}, "j2": {"en": B}}}
        self.assertEqual(still_there(place, lambda jobs: {"j2"}),
                         {"place": {**place, "jobs": ["j2"], "seen": {"j2": {"en": B}}}, "gone": False})
        self.assertEqual(still_there(place, lambda jobs: set()), {"place": None, "gone": True})
        self.assertEqual(still_there({"kind": "nonsense"}, lambda jobs: set(jobs)), {"place": None, "gone": False})
        self.assertEqual(still_there(None, lambda jobs: set(jobs)), {"place": None, "gone": False})


class V2RecentTests(test_web_v2.V2Case):
    def post(self, client, headers, place, **extra):
        return client.post("/api/recent", headers=headers, json={"place": place, **extra})

    def counts(self):
        with tenant_store.transaction(self.database) as connection:
            return {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in ("tasks", "materials", "approvals", "artifacts", "model_calls", "quota_usage")}

    def test_a_place_is_kept_per_account_and_reading_or_saving_it_starts_nothing(self):
        client, headers = self.ready_user("alice")
        job_id = self.new_job(client, headers)
        self.assertEqual(client.get("/api/recent", headers=headers).json(), {"place": None, "gone": False})
        before, sent, usage = self.counts(), len(self.chat.sent), client.get("/api/me", headers=headers).json()["usage"]
        sha = client.get(f"/api/jobs/{job_id}", headers=headers).json()["cv"]["en"]["content_sha256"]
        place = job_place(job_id, language="en", area="cv", seen={job_id: {"en": sha}})
        saved = self.post(client, headers, place)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertTrue(saved.json()["saved"])
        self.assertEqual(client.get("/api/recent", headers=headers).json(), {"place": place, "gone": False})
        for _ in range(3):
            client.get("/api/recent", headers=headers)
        self.assertEqual((self.counts(), len(self.chat.sent), client.get("/api/me", headers=headers).json()["usage"]),
                         (before, sent, usage))
        with tenant_store.transaction(self.database) as connection:
            stored = connection.execute("SELECT place FROM recent_work").fetchall()
        self.assertEqual(len(stored), 1)
        kept = json.loads(stored[0]["place"])
        self.assertEqual(set(kept), {"place", "acted_at", "action"})  # IDs, language, area, versions, an action: no text
        self.assertEqual(kept["place"], place)
        # Another account sees none of it, and cannot name alice's job.
        bob, bob_headers = self.ready_user("bob")
        self.assertEqual(bob.get("/api/recent", headers=bob_headers).json(), {"place": None, "gone": False})
        refused = self.post(bob, bob_headers, job_place(job_id))
        self.assertEqual((refused.status_code, refused.json()["error"]), (404, "Not found, or not yours"))
        unknown = self.post(bob, bob_headers, job_place("0" * 32))
        self.assertEqual(unknown.text, refused.text)  # someone else's job looks like no job
        self.assertEqual(client.get("/api/recent", headers=headers).json()["place"], place)

    def test_sign_in_and_the_page_token_are_needed(self):
        client, headers = self.ready_user("alice")
        job_id = self.new_job(client, headers)
        anonymous = test_web_v2.TestClient(self.app, base_url=test_web_v2.ORIGIN)
        self.assertEqual(anonymous.get("/api/recent").status_code, 401)
        self.assertEqual(anonymous.post("/api/recent", json={"place": job_place(job_id)}).status_code, 401)
        self.assertEqual(client.post("/api/recent", json={"place": job_place(job_id)}).status_code, 403)  # no CSRF token
        self.assertEqual(self.post(client, headers, {"kind": "job", "jobs": ["x"], "text": "line"}).status_code, 400)

    def test_a_page_closing_never_puts_back_an_older_place(self):
        client, headers = self.ready_user("alice")
        first = self.new_job(client, headers)
        created = client.post("/api/jobs", headers=headers, json={
            "title": "Data Intern", "company": "Other Co", "text": test_web_v2.JD_TEXT + "\n- 熟悉数据分析"})
        second = self.settle(client, headers, created)["result"]["job_id"]
        old = self.post(client, headers, job_place(first)).json()
        newer = self.post(client, headers, job_place(second, area="gaps")).json()  # another tab, later
        closing = self.post(client, headers, job_place(first, area="requirements"), unless_changed_since=old["updated_at"])
        self.assertEqual(closing.json(), {"place": None, "updated_at": newer["updated_at"], "saved": False})
        self.assertEqual(client.get("/api/recent", headers=headers).json()["place"]["jobs"], [second])
        same = self.post(client, headers, job_place(second, area="cv"), unless_changed_since=newer["updated_at"])
        self.assertTrue(same.json()["saved"])
        self.assertEqual(client.get("/api/recent", headers=headers).json()["place"]["area"], "cv")

    def test_the_latest_action_wins_whatever_order_saves_arrive_in_timed_by_the_server(self):
        # Found in review by Codex: an older tab with a save still waiting, closed after a newer
        # tab saved, put its older place back. Each save names its action and how long ago it was
        # (the page's steady clock); the server times it on its own clock, so no device's date and
        # time decides, and the latest action stays.
        client, headers = self.ready_user("alice")
        first = self.new_job(client, headers)
        created = client.post("/api/jobs", headers=headers, json={
            "title": "Data Intern", "company": "Other Co", "text": test_web_v2.JD_TEXT + "\n- 熟悉数据分析"})
        second = self.settle(client, headers, created)["result"]["job_id"]
        newer = self.post(client, headers, job_place(second, area="gaps"), action="tab-b-opened", ago_ms=0).json()
        late = self.post(client, headers, job_place(first, area="requirements"), action="tab-a-clicked", ago_ms=5_000)
        self.assertEqual(late.json(), {"place": None, "updated_at": newer["updated_at"], "saved": False})
        self.assertEqual(client.get("/api/recent", headers=headers).json()["place"]["jobs"], [second])
        # The same action saved again (the page closing, with the versions it last showed) stands,
        # however long its request took: it keeps its first time.
        again = self.post(client, headers, job_place(second, area="gaps", seen={second: {"en": B}}),
                          action="tab-b-opened", ago_ms=60_000)
        self.assertTrue(again.json()["saved"])
        self.assertEqual(client.get("/api/recent", headers=headers).json()["place"]["seen"], {second: {"en": B}})
        # Another device acting now wins, whatever its clock says: only elapsed time is sent.
        self.assertTrue(self.post(client, headers, job_place(first), action="phone-opened", ago_ms=0).json()["saved"])
        # A very old action counts as a week old at most, and loses to anything recent.
        self.assertFalse(self.post(client, headers, job_place(second), action="old-tab-click", ago_ms=10 ** 12).json()["saved"])
        for body, status in (({"action": "x", "ago_ms": 0}, 400), ({"action": "tab-c-click", "ago_ms": -1}, 400),
                             ({"action": "tab-c-click"}, 400), ({"ago_ms": 0}, 400),
                             ({"action": "tab-c-click", "ago_ms": 1.5}, 422)):
            self.assertEqual(self.post(client, headers, job_place(second), **body).status_code, status, body)
        # A place stored by the first version of this (no action) gives way to any action.
        with tenant_store.transaction(self.database, write=True) as connection:
            connection.execute("UPDATE recent_work SET place = ?", (json.dumps(job_place(first)),))
        self.assertEqual(client.get("/api/recent", headers=headers).json()["place"]["jobs"], [first])
        self.assertTrue(self.post(client, headers, job_place(second), action="old-tab-click", ago_ms=10 ** 12).json()["saved"])

    def test_jobs_that_are_gone_are_left_out_and_a_place_with_none_says_so(self):
        client, headers = self.ready_user("alice")
        job_id = self.new_job(client, headers)
        user_id = next(account["user_id"] for account in tenant_store.list_accounts(self.database))
        with tenant_store.transaction(self.database, write=True) as connection:  # as if a job were gone
            connection.execute("INSERT INTO recent_work VALUES (?, ?, ?)", (user_id, json.dumps(
                {"kind": "batch", "jobs": ["f" * 32, job_id], "language": None, "area": "batch",
                 "seen": {"f" * 32: {"en": A}}}), "2026-01-01"))
        self.assertEqual(client.get("/api/recent", headers=headers).json()["place"]["jobs"], [job_id])
        with tenant_store.transaction(self.database, write=True) as connection:
            connection.execute("UPDATE recent_work SET place = ?", (json.dumps(job_place("f" * 32)),))
        self.assertEqual(client.get("/api/recent", headers=headers).json(), {"place": None, "gone": True})
        with tenant_store.transaction(self.database, write=True) as connection:
            connection.execute("UPDATE recent_work SET place = ?", ('{"not": "a place"}',))
        self.assertEqual(client.get("/api/recent", headers=headers).json(), {"place": None, "gone": False})

    def test_deleting_an_account_deletes_its_place_and_a_backup_keeps_only_live_ones(self):
        alice, alice_headers = self.ready_user("alice")
        self.post(alice, alice_headers, job_place(self.new_job(alice, alice_headers)))
        bob, bob_headers = self.ready_user("bob")
        bob_job = self.new_job(bob, bob_headers)
        self.post(bob, bob_headers, job_place(bob_job))
        archive = self.root / "backups" / "before.tar.gz"
        v2_backup.create_backup(self.root, archive)
        deleted = bob.post("/api/account/delete", headers=bob_headers, json={"confirm": "DELETE"})
        self.assertEqual(deleted.status_code, 200, deleted.text)
        with tenant_store.transaction(self.database) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM recent_work").fetchone()[0], 1)
        self.assertEqual(v2_backup.verify_database(self.database)["problems"], [])
        restored = self.root / "restored"
        v2_backup.restore_backup(archive, restored, live_ledger=self.root / "ledger.jsonl")
        with tenant_store.transaction(restored / tenant_store.DATABASE) as connection:
            rows = connection.execute("SELECT place FROM recent_work").fetchall()
        self.assertEqual(len(rows), 1)  # alice's; bob was deleted again from the ledger
        self.assertNotIn(bob_job, rows[0]["place"])


class UpgradeTests(unittest.TestCase):
    def test_an_older_store_gets_the_table_once_and_loses_deleted_accounts_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "v2.db"
            tenant_store.initialize_store(path)
            connection = sqlite3.connect(path)
            connection.execute("DROP TABLE recent_work")  # as a release before it made the store
            connection.commit()
            connection.close()
            self.assertEqual(tenant_store.upgrade_store(path), ["recent_work"])
            self.assertEqual(tenant_store.upgrade_store(path), [])  # as often as it is run
            with tenant_store.transaction(path, write=True) as connection:
                connection.execute("INSERT INTO users(user_id, issuer, subject, active, created_at, deleted_at) "
                                   "VALUES ('gone', 'i', 's', 0, '2026', '2026')")
                connection.execute("INSERT INTO recent_work VALUES ('gone', '{}', '2026')")  # left by an older release
            tenant_store.upgrade_store(path)
            with tenant_store.transaction(path) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM recent_work").fetchone()[0], 0)


class GuestRecentTests(test_guest_workspace.GuestCase):
    def test_a_guest_keeps_its_own_place_and_it_ends_with_the_workspace(self):
        client, headers, job_id = self.guest()
        saved = client.post("/api/recent", headers=headers, json={"place": job_place(job_id)})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(client.get("/api/recent", headers=headers).json()["place"]["jobs"], [job_id])
        user, user_headers = self.ready_user("carol")
        theirs = self.new_job(user, user_headers)
        self.assertEqual(client.post("/api/recent", headers=headers, json={"place": job_place(theirs)}).status_code, 404)
        self.assertEqual(user.post("/api/recent", headers=user_headers, json={"place": job_place(job_id)}).status_code, 404)
        self.assertEqual(client.post("/guest/end", headers={**headers, "Origin": test_web_v2.ORIGIN}).status_code, 200)
        with tenant_store.transaction(self.database) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM recent_work").fetchone()[0], 0)
        other, other_headers, other_job = self.guest()
        self.assertEqual(other.get("/api/recent", headers=other_headers).json(), {"place": None, "gone": False})
        self.assertNotEqual(other_job, job_id)


class V1RecentTests(test_web.WebTests):
    def test_the_single_user_app_keeps_a_place_in_its_data_folder(self):
        job_id = self.planned_job()
        path = Path(self.directory.name) / "recent-work.json"
        self.assertEqual(self.client.get("/api/recent", headers=self.headers).json(), {"place": None, "gone": False})
        sent = len(self.chat.sent)
        saved = self.client.post("/api/recent", headers=self.headers, json={"place": job_place(job_id, language="zh")})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(self.client.get("/api/recent", headers=self.headers).json()["place"]["language"], "zh")
        self.assertEqual(set(json.loads(path.read_text())), {"place", "acted_at", "action", "updated_at"})
        self.assertEqual(len(self.chat.sent), sent)
        self.assertEqual(self.client.post("/api/recent", headers=self.headers,
                                          json={"place": job_place("20260101-000000-abcdef")}).status_code, 404)
        self.assertEqual(self.client.post("/api/recent", json={"place": job_place(job_id)}).status_code, 403)  # no token
        stale = self.client.post("/api/recent", headers=self.headers, json={"place": job_place(job_id, area="gaps"),
                                                                             "unless_changed_since": "earlier"})
        self.assertFalse(stale.json()["saved"])
        older = self.client.post("/api/recent", headers=self.headers, json={"place": job_place(job_id, area="gaps"),
                                                                             "action": "older-tab-click", "ago_ms": 60_000})
        self.assertFalse(older.json()["saved"])  # an older action than the place kept, as in V2
        self.assertEqual(self.client.get("/api/recent", headers=self.headers).json()["place"]["language"], "zh")
        self.assertTrue(web.OWN_STORE.fullmatch("/api/recent"))  # never waits behind a change of the CV
        path.write_text(json.dumps({"place": job_place("20260101-000000-abcdef")}))
        self.assertEqual(self.client.get("/api/recent", headers=self.headers).json(), {"place": None, "gone": True})


PAGE = r'''
const later = sandbox.setTimeout;
const timers = [];
sandbox.setTimeout = (action, delay) => { if (delay === 800) { timers.push(action); return timers.length; } return delay === 2000 ? 0 : later(action, delay); };
sandbox.location.replace = (hash) => { sandbox.location.hash = hash; };
const posts = [];
sandbox.fetch = async (path, options) => { posts.push({path, ...options, body: JSON.parse(options.body)});
  return {ok: true, status: 200, json: async () => ({saved: true, updated_at: "stamp-" + posts.length})}; };
run('fetch = window.fetch');
const cv = (fields) => ({head: "planned", content_sha256: "a".repeat(64), stale: false, final_pdf: false, ...fields});
sandbox.__view = {job_id: "job-1", language: "en", cv_languages: ["en", "zh"], jd: {title: "Role", text: "Text", captured_at: "2026-10-20"},
  requirements_version: 1, tasks: [], cv: {en: cv({}), zh: cv({content_sha256: "b".repeat(64)})}};
run(`api = async (path, options = {}) => { __calls.push({path, ...options});
  if (path === "/api/recent") return __recent;
  return JSON.parse(JSON.stringify(__view)); };
 requirementsPanel = () => el("section", {"data-area": "requirements"}, "requirements");
 gapsPanel = () => el("section", {"data-area": "gaps"}, "gaps");
 cvLineEditor = () => null;`);
'''


@unittest.skipUnless(NODE, "Node is needed for frontend behavior checks")
class RecoveryUITests(unittest.TestCase):
    check_js = SponsorUITests.check_js

    def test_opening_with_no_address_goes_back_to_the_kept_job_its_language_and_part_once(self):
        self.check_js(PAGE + r'''
sandbox.__recent = {place: {kind: "job", jobs: ["job-1"], language: "zh", area: "cv",
  seen: {"job-1": {en: "a".repeat(64), zh: "c".repeat(64)}}}, gone: false};
sandbox.location.hash = "";
await run('openDefault()');
assert.equal(sandbox.location.hash, "#job/job-1");
run('pageGeneration += 1');
await run('renderJob("job-1")');
// The Chinese CV it was on is open, and one line says which CV changed since (only zh did).
assert(run('OTHER_OPEN.has("job-1")'));
const notes = nodes(app).filter(n => n.attributes?.class === "warning place-note");
assert.equal(notes.length, 1);
assert.equal(notes[0].textContent, "This CV changed since you were last here; the current version is shown.");
const zhBlock = find(app, n => n.attributes?.class === "cv-block" && n.attributes["data-language"] === "zh");
assert(find(zhBlock, n => n === notes[0]));  // in the CV that changed, where the page goes back to
assert.deepEqual(JSON.parse(JSON.stringify(run('place'))), {kind: "job", jobs: ["job-1"], language: "zh", area: "cv",
  seen: {"job-1": {en: "a".repeat(64), zh: "b".repeat(64)}}});
assert(sandbox.__calls.every(call => !call.method || call.path === "/api/recent"));  // nothing started, nothing changed
// Once: refreshing the page (a poll) neither restores again nor repeats anything.
assert.equal(run('restoring'), null);
''')

    def test_an_address_the_user_gave_or_went_to_wins_over_the_kept_place(self):
        self.check_js(PAGE + r'''
let answer;
sandbox.__recent = new Promise((resolve) => { answer = resolve; });
sandbox.location.hash = "";
const opening = run('openDefault()');
sandbox.location.hash = "#job/job-2"; run('pageGeneration += 1');  // the user goes elsewhere meanwhile
answer({place: {kind: "job", jobs: ["job-1"], language: "en", area: "cv", seen: {}}, gone: false});
await opening;
assert.equal(sandbox.location.hash, "#job/job-2");
assert.equal(run('restoring'), null);
// An explicit address never looks at the kept place at all.
sandbox.__calls.length = 0;
sandbox.location.hash = "#job/job-1"; run('pageGeneration += 1');
await run('renderJob("job-1")');
assert(!sandbox.__calls.some(call => call.path === "/api/recent"));
assert(!find(app, n => n.attributes?.class === "warning place-note"));
''')

    def test_a_kept_job_that_is_gone_opens_the_jobs_list_and_a_failure_opens_as_before(self):
        self.check_js(PAGE + r'''
sandbox.__recent = {place: null, gone: true};
sandbox.location.hash = "";
await run('openDefault()');
assert.equal(sandbox.location.hash, "#jobs");
assert.equal(String(run('flash')[0]), "The job you were last working on is no longer available.");
run(`api = async (path) => { if (path === "/api/recent") throw new Error("HTTP 500"); return {facts: []}; }`);
sandbox.location.hash = ""; run('pageGeneration += 1; flash = null');
await run('openDefault()');
assert.equal(sandbox.location.hash, "#facts");  // as before: no profile yet
''')

    def test_a_place_is_saved_once_a_moment_later_and_never_for_a_page_already_left(self):
        self.check_js(PAGE + r'''
sandbox.location.hash = "#job/job-1"; run('pageGeneration += 1');
await run('renderJob("job-1")');
run('workingIn("requirements", null)');
run('workingIn("cv", "zh")');
assert.equal(timers.length, 3);  // each change restarts the one pending save
await timers[2]();
await new Promise(resolve => later(resolve, 0));
assert.equal(posts.length, 1);
assert.deepEqual(posts[0].body.place, {kind: "job", jobs: ["job-1"], language: "zh", area: "cv",
  seen: {"job-1": {en: "a".repeat(64), zh: "b".repeat(64)}}});
assert.equal(typeof posts[0].body.action, "string");
assert(posts[0].body.ago_ms >= 0 && posts[0].body.ago_ms < 60000);  // elapsed time, never a date
// The same place is not saved twice; a poll showing a new version saves nothing by itself.
run('workingIn("cv", "zh")');
sandbox.__view.cv.en.content_sha256 = "d".repeat(64);
run('sawVersions("job-1", __view.cv)');
assert.equal(timers.length, 3);
// The page closing sends what it last showed, with the time of the user's last action on it (so
// it stands only if no later action was saved from another tab), kept alive past the page.
run('flushPlace()');
await new Promise(resolve => later(resolve, 0));
assert.equal(posts.length, 2);
assert.equal(posts[1].body.action, posts[0].body.action);  // the same action, so it stands over itself
assert.equal(posts[1].keepalive, true);
assert.equal(posts[1].body.place.seen["job-1"].en, "d".repeat(64));
// A save still pending when the user has gone to another page is dropped.
run('workingIn("gaps", null)');
sandbox.location.hash = "#jobs";
await timers[timers.length - 1]();
await new Promise(resolve => later(resolve, 0));
assert.equal(posts.length, 2);
''')

    def test_the_page_drawing_the_second_language_open_is_no_choice_of_the_users(self):
        # Found in review by Codex: a details drawn open fires "toggle" by itself in a browser,
        # which put zh/cv over the en/requirements the page had gone back to.
        self.check_js(PAGE + r'''
sandbox.__recent = {place: {kind: "job", jobs: ["job-1"], language: "en", area: "requirements", seen: {}}, gone: false};
sandbox.location.hash = "";
await run('openDefault()');
run('pageGeneration += 1');
await run('renderJob("job-1")');
const details = find(app, n => n.tagName === "details" && n.attributes?.class === "other-language");
assert("open" in details.attributes);  // the Chinese CV is there, drawn open
details.open = true;
await details.fire("toggle");  // as the browser fires it for a details drawn open
assert.equal(run('place.language'), "en");
assert.equal(run('place.area'), "requirements");
// The user's own click on it is their choice: closing it is back to English, opening it Chinese.
const summary = find(details, n => n.tagName === "summary");
await summary.fire("click");
assert.deepEqual([run('place.language'), run('place.area')], ["en", "cv"]);
details.open = false;
await summary.fire("click");
assert.deepEqual([run('place.language'), run('place.area')], ["zh", "cv"]);
''')

    def test_a_language_answer_that_comes_after_the_user_left_changes_nothing(self):
        # Found in review by Codex: choosing a language, then opening job B before the answer
        # came, put zh/cv over B's place.
        self.check_js(PAGE + r'''
sandbox.__view.cv_languages = ["en", "zh"];
let answer;
run(`api = async (path, options = {}) => { __calls.push({path, ...options});
  if (path.endsWith("/language")) return __languageAnswer;
  const view = JSON.parse(JSON.stringify(__view)); view.job_id = path.split("/")[3]; return view; }`);
sandbox.__languageAnswer = new Promise((resolve) => { answer = resolve; });
sandbox.location.hash = "#job/job-1"; run('pageGeneration += 1');
await run('renderJob("job-1")');
const select = find(app, n => n.tagName === "select");
select.value = "zh";
const choosing = select.fire("change");
sandbox.location.hash = "#job/job-2"; run('pageGeneration += 1');
await run('renderJob("job-2")');
run('workingIn("requirements", "en")');
answer({});
await choosing;
await new Promise(resolve => later(resolve, 0));
assert.deepEqual([run('place.jobs[0]'), run('place.language'), run('place.area')], ["job-2", "en", "requirements"]);
// The same choice on the job still open does count.
const again = find(app, n => n.tagName === "select");
sandbox.__languageAnswer = Promise.resolve({});
again.value = "zh";
await again.fire("change");
assert.deepEqual([run('place.jobs[0]'), run('place.language'), run('place.area')], ["job-2", "zh", "cv"]);
''')

    def test_answers_out_of_order_never_undo_a_newer_save_and_a_pending_first_save_keeps_what_was_seen(self):
        self.check_js(PAGE + r'''
const answers = [];
sandbox.fetch = (path, options) => new Promise((resolve) => {
  posts.push({path, ...options, body: JSON.parse(options.body)});
  answers.push(() => resolve({ok: true, status: 200, json: async () => ({saved: true})}));
});
sandbox.location.hash = "#job/job-1"; run('pageGeneration += 1');
await run('renderJob("job-1")');
// The first save is still on its way when a poll shows a new version and the page closes:
// what it last showed still goes (the edge case Codex noted).
await timers[0]();
sandbox.__view.cv.en.content_sha256 = "d".repeat(64);
run('sawVersions("job-1", __view.cv)');
run('flushPlace()');
assert.equal(posts.length, 2);
assert.equal(posts[1].body.place.seen["job-1"].en, "d".repeat(64));
assert.equal(posts[1].body.action, posts[0].body.action);
// The answers come back newest first: the older answer does not stand for the newer save, so
// nothing newer is sent again or taken back.
answers[1](); await new Promise(resolve => later(resolve, 0));
answers[0](); await new Promise(resolve => later(resolve, 0));
run('flushPlace()');
assert.equal(posts.length, 2);
''')

    def test_the_marked_next_step_follows_the_cv_state_and_never_a_running_or_optional_one(self):
        self.check_js(r'''
const cv = (fields) => ({head: "planned", content_sha256: "a".repeat(64), stale: false, final_pdf: false, ...fields});
const marked = (state, tasks = []) => {
  const block = run('cvBlock')({job_id: "j", tasks, cv: {en: state}}, "en", async () => {});
  return nodes(block).filter(n => (n.attributes?.class || "").split(" ").includes("next-step")).map(n => n.textContent);
};
run('cvLineEditor = () => null');
assert.deepEqual(marked({head: null}), ["Prepare this CV"]);
assert.deepEqual(marked({head: null, stages: [{stage: "draft", status: "failed", message: "Not prepared: x"}]}), ["Try again"]);
assert.deepEqual(marked(cv({stale: true})), ["Start over"]);
assert.deepEqual(marked(cv({head: "draft"})), ["Check page count"]);
assert.deepEqual(marked(cv({pagination: {pages: 1, checked_at: "2026-10-20T00:00:00Z"}})), ["Approve this CV"]);
assert.deepEqual(marked(cv({head: "approved", approved_at: "2026-10-20T00:00:00Z"})), ["Create final PDF"]);
assert.deepEqual(marked(cv({head: "approved", approved_at: "2026-10-20T00:00:00Z", final_pdf: true})), ["Download final PDF"]);
// While work on the job runs nothing is marked; a superseded or failed task is not running.
assert.deepEqual(marked(cv({head: "draft"}), [{task_id: "t", operation: "tailor_cv", status: "running"}]), []);
assert.deepEqual(marked(cv({head: "draft"}), [{task_id: "t", operation: "tailor_cv", status: "superseded"}]), ["Check page count"]);
assert.deepEqual(marked(cv({head: "draft"}), [{task_id: "t", operation: "tailor_cv", status: "failed"}]), ["Check page count"]);
// Rewording and adjusting stay optional: never the marked step.
assert(!marked(cv({head: "draft"})).some(label => /Reword|Adjust/.test(label)));
''')

    def test_on_return_the_page_shows_work_as_it_is_now_and_repeats_nothing(self):
        self.check_js(PAGE + r'''
const polls = [];
const keep = sandbox.setTimeout;
sandbox.setTimeout = (action, delay) => { if (delay === 2000) { polls.push(action); return polls.length; } return keep(action, delay); };
const visit = async () => {
  sandbox.__recent = {place: {kind: "job", jobs: ["job-1"], language: "en", area: "cv",
    seen: {"job-1": {en: "a".repeat(64)}}}, gone: false};
  sandbox.location.hash = ""; run('pageGeneration += 1');
  await run('openDefault()');
  run('pageGeneration += 1');
  await run('renderJob("job-1")');
};
const marked = () => nodes(app).filter(n => (n.attributes?.class || "").split(" ").includes("next-step")).map(n => n.textContent);
// Still running: shown as running, polled, and no step marked.
sandbox.__view.tasks = [{task_id: "t1", operation: "tailor_cv", status: "running"}];
await visit();
assert(app.textContent.includes("Working on it: rewording your CV."));
assert.equal(polls.length, 1);
assert.deepEqual(marked(), []);
// Finished meanwhile, as a new version: the poll shows it, the next step is marked, nothing is sent again.
sandbox.__view.tasks = [{task_id: "t1", operation: "tailor_cv", status: "succeeded"}];
sandbox.__view.cv.en = {...sandbox.__view.cv.en, head: "tailored", content_sha256: "e".repeat(64)};
await polls[0]();
await new Promise(resolve => keep(resolve, 0));
assert.deepEqual(marked(), ["Check page count", "Check page count"]);
assert(!sandbox.__calls.some(call => call.method === "POST"));
// Failed while away: the failure is said once, with its own button; nothing is retried by itself.
sandbox.__view.tasks = [{task_id: "t2", operation: "plan_cv", status: "failed", message: "DeepSeek is busy right now. Try again in a minute."}];
await visit();
assert(app.textContent.includes("Not finished: adjusting your CV."));
// Interrupted by a restart: said as such (V1's own note), nothing is redone.
sandbox.__view.tasks = []; sandbox.__view.interrupted = {step: "cv-planned-en"};
await visit();
assert(app.textContent.includes("The workbench stopped while preparing your CV"));
assert(!sandbox.__calls.some(call => call.method === "POST"));
// A PDF made for the version seen then is never offered once the CV changed: the new one is to be checked.
sandbox.__view.interrupted = null;
sandbox.__view.cv.en = {head: "planned", content_sha256: "f".repeat(64), stale: false, final_pdf: false};
await visit();
assert(!nodes(app).some(n => n.tagName === "a" && (n.attributes.href || "").startsWith("/download/")));
assert.equal(find(app, n => n.attributes?.class === "warning place-note").textContent,
  "This CV changed since you were last here; the current version is shown.");
''')

    def test_a_kept_batch_says_how_many_cvs_changed_and_saves_itself_as_a_batch(self):
        self.check_js(r'''
const later = sandbox.setTimeout;
sandbox.setTimeout = (action, delay) => delay === 800 || delay === 2000 ? 0 : later(action, delay);
const cv = (sha) => ({head: "planned", content_sha256: sha, stale: false, final_pdf: false});
sandbox.__views = {"j1": {job_id: "j1", language: "en", jd: {title: "One"}, tasks: [], cv: {en: cv("a".repeat(64)), zh: {head: null}}},
                   "j2": {job_id: "j2", language: "en", jd: {title: "Two"}, tasks: [], cv: {en: cv("b".repeat(64)), zh: {head: null}}}};
run(`api = async (path) => JSON.parse(JSON.stringify(__views[path.split("/")[3]]))`);
sandbox.location.hash = "#batch/j1,j2";
run('restoring = {kind: "batch", jobs: ["j1", "j2"], language: null, area: "batch", hash: "#batch/j1,j2", seen: {j1: {en: "a".repeat(64)}, j2: {en: "e".repeat(64)}}}');
await run('renderBatch("j1,j2")');
const note = find(app, n => n.attributes?.class === "warning place-note");
assert.equal(note.textContent, "1 CV(s) in this batch changed since you were last here; their current versions are shown.");
assert.equal(run('place.kind'), "batch");
assert.deepEqual(JSON.parse(JSON.stringify(run('place.seen'))), {j1: {en: "a".repeat(64)}, j2: {en: "b".repeat(64)}});
''')


def load_tests(loader, standard_tests, pattern):
    """Only the tests written here: the classes reuse other modules' fixtures, not their tests."""
    suite = unittest.TestSuite()
    for case in (PlaceRuleTests, V2RecentTests, UpgradeTests, GuestRecentTests, V1RecentTests, RecoveryUITests):
        for name in loader.getTestCaseNames(case):
            if name in vars(case):
                suite.addTest(case(name))
    return suite


if __name__ == "__main__":
    unittest.main()
