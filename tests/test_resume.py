"""Going back after signing in again, or after reopening a page, to the very control the user was
at: the job or batch the address names (never another one), its CV language, its part of the page
and the CV's control they last worked with (data-step), or the one that now follows it; work still
running shows its own note. Nothing is pressed or redone. Where the user was rides through sign-in
only as ?resume=area.language.step, each part from a fixed list. Synthetic pages; the server's
answers are the tests' stand-ins, no model."""
import json
import unittest
from urllib.parse import quote

from feiran.recent_work import PlaceError, clean_place
from tests import test_web_v2
from tests.test_recovery import PAGE, job_place
from tests.test_sponsor_ui import NODE, SponsorUITests


class StepRuleTests(unittest.TestCase):
    def test_a_step_is_one_of_the_cvs_own_controls_and_only_for_a_job(self):
        self.assertEqual(clean_place(job_place("j1", step="tailor"))["step"], "tailor")
        for bad in (job_place("j1", step="delete_account"), job_place("j1", step="../x"),
                    {"kind": "batch", "jobs": ["j1"], "area": "batch", "step": "pages"}):
            with self.subTest(bad=bad), self.assertRaises(PlaceError):
                clean_place(bad)


class V2StepTests(test_web_v2.V2Case):
    def test_the_step_is_kept_and_answered_for_the_account_only(self):
        client, headers = self.ready_user("alice")
        job_id = self.new_job(client, headers)
        saved = client.post("/api/recent", headers=headers, json={"place": job_place(job_id, step="approve"),
                                                                    "action": "click-approve", "ago_ms": 0})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(client.get("/api/recent", headers=headers).json()["place"]["step"], "approve")
        self.assertEqual(client.post("/api/recent", headers=headers, json={"place": job_place(job_id, step="rm")}).status_code, 400)
        bob, bob_headers = self.ready_user("bob")
        self.assertEqual(bob.get("/api/recent", headers=bob_headers).json(), {"place": None, "gone": False})


class V2DownloadSignInTests(test_web_v2.V2Case):
    """Found by Codex: a final PDF's link, followed when the session had ended, stopped on a JSON
    error page instead of signing in and coming back to the download."""

    def test_a_pdf_link_followed_after_the_session_ended_signs_in_and_comes_back_to_its_download(self):
        client, headers = self.ready_user("alice")
        job_id = self.new_job(client, headers)
        self.assertEqual(self.approve(client, headers, job_id).status_code, 200)
        self.settle(client, headers, client.post(f"/api/jobs/{job_id}/cv/en/export", headers=headers))
        navigate = {"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
        self.assertEqual(client.get(f"/download/{job_id}/en.pdf", headers=navigate).content[:4], b"%PDF")  # signed in: as before
        anonymous = test_web_v2.TestClient(self.app, base_url=test_web_v2.ORIGIN)
        followed = anonymous.get(f"/download/{job_id}/en.pdf", headers=navigate, follow_redirects=False)
        self.assertEqual(followed.status_code, 303)
        back = "/?resume=cv.en.download#job/" + job_id
        self.assertEqual(followed.headers["location"], "/login?return_to=" + quote(back, safe=""))
        started = anonymous.get(followed.headers["location"], follow_redirects=False)
        signed_in = anonymous.get("/auth/callback", params=self.cognito.authorize(started.headers["location"], "alice"),
                                  follow_redirects=False)
        self.assertEqual((signed_in.status_code, signed_in.headers["location"]), (303, back))  # the page, not the file
        # The page's own requests, other paths and other words stay a plain 401, as before.
        later = test_web_v2.TestClient(self.app, base_url=test_web_v2.ORIGIN)
        for path, extra in [(f"/download/{job_id}/en.pdf", {}), (f"/download/{job_id}/fr.pdf", navigate),
                            (f"/download/{job_id}/en.pdf.exe", navigate), ("/download/batch.zip", navigate),
                            (f"/preview/{job_id}/en", navigate)]:
            answer = later.get(path, headers=extra, follow_redirects=False)
            self.assertEqual(answer.status_code, 401, path)
        self.assertEqual(later.post(f"/download/{job_id}/en.pdf", headers=navigate, follow_redirects=False).status_code, 401)


BOOT = PAGE + r'''
// startPage reads the kept place with request() (fetch), the job with api(): both answered here.
sandbox.__recentAnswer = {status: 200, body: {place: null, gone: false}};
sandbox.fetch = async (path, options = {}) => {
  if (path === "/api/recent" && !options.method) {
    const answer = sandbox.__recentAnswer;
    if (answer === "network") throw new Error("offline");
    return {ok: answer.status === 200, status: answer.status, json: async () => answer.body};
  }
  posts.push({path, ...options, body: JSON.parse(options.body)});
  return {ok: true, status: 200, json: async () => ({saved: true})};
};
run('fetch = window.fetch');
sandbox.document.querySelectorAll = () => [];  // the nav links route() marks
sandbox.history = {replaceState: (state, title, url) => { sandbox.__replaced = url; }};
run('history = window.history');
sandbox.__view.cv.en = cv({head: "draft"});
sandbox.__view.cv.zh = cv({head: "draft", content_sha256: "b".repeat(64)});
const settled = () => new Promise(resolve => later(resolve, 20));
const shown = () => { const node = run('keptShown'); return node ? node.textContent : null; };
const boot = async (hash, search = "") => {
  sandbox.location.hash = hash; sandbox.location.search = search; sandbox.location.pathname = "/";
  run('pageGeneration += 1');
  await run('startPage()');
  await settled();
};
'''


@unittest.skipUnless(NODE, "Node is needed for frontend behavior checks")
class ResumeUITests(unittest.TestCase):
    check_js = SponsorUITests.check_js

    def test_back_from_signing_in_to_the_same_job_its_part_and_control(self):
        # Found by Codex: back from sign-in (/#job/id) went to the job's top and then saved that,
        # forgetting where the user was in it.
        self.check_js(BOOT + r'''
sandbox.__recentAnswer = {status: 200, body: {place: {kind: "job", jobs: ["job-1"], language: "en", area: "cv",
  step: "tailor", seen: {}}, gone: false}};
await boot("#job/job-1");
assert.deepEqual([run('place.area'), run('place.language'), run('place.step')], ["cv", "en", "tailor"]);
assert.equal(shown(), "Reword with DeepSeek");  // shown, never pressed
assert(!sandbox.__calls.some(call => call.method === "POST"));
''')

    def test_the_session_ending_carries_where_the_user_was_through_sign_in(self):
        self.check_js(BOOT + r'''
await boot("#job/job-1");
run('workingIn("cv", "zh", {step: "approve"})');
// Where api() sends the page when any request of it finds the session ended.
assert.equal(run('signInAgainAddress()'), "/login?return_to=" + encodeURIComponent("/?resume=cv.zh.approve#job/job-1"));
// On another address than the place (a link the user followed when it ended): no hint at all.
sandbox.location.hash = "#job/job-2";
assert.equal(run('signInAgainAddress()'), "/login?return_to=" + encodeURIComponent("/#job/job-2"));
''')

    def test_the_hint_back_from_sign_in_is_used_once_and_only_from_its_lists(self):
        self.check_js(BOOT + r'''
await boot("#job/job-1", "?resume=cv.zh.tailor");
assert.deepEqual([run('place.area'), run('place.language'), run('place.step')], ["cv", "zh", "tailor"]);
assert(run('OTHER_OPEN.has("job-1")'));
const zh = find(app, n => n.attributes?.class === "cv-block" && n.attributes["data-language"] === "zh");
assert(find(zh, n => n === run('keptShown')));  // the Chinese CV's own control
assert.equal(sandbox.__replaced, "/#job/job-1");  // taken out of the address: a reload does not repeat it
for (const [search, expected] of [["?resume=cv.zh.rm", ["cv", "zh", null]], ["?resume=javascript:alert(1)", [null, "en", null]],
                                  ["?resume=cv.zh.tailor.extra", [null, "en", null]], ["?resume=..", [null, "en", null]],
                                  ["?resume=https://evil.example", [null, "en", null]]]) {
  run('place = null; OTHER_OPEN.clear()');
  await boot("#job/job-1", search);
  assert.deepEqual([run('place.area'), run('place.language'), run('place.step')], expected, search);
}
''')

    def test_a_link_to_another_job_wins_and_a_late_or_failed_read_changes_nothing(self):
        self.check_js(BOOT + r'''
sandbox.__recentAnswer = {status: 200, body: {place: {kind: "job", jobs: ["job-1"], language: "zh", area: "gaps", step: "fit", seen: {}}, gone: false}};
await boot("#job/job-2");
assert.deepEqual([run('place.jobs[0]'), run('place.area'), run('place.step')], ["job-2", null, null]);
assert.equal(run('keptShown'), null);
for (const answer of [{status: 500, body: {}}, {status: 401, body: {}}, "network"]) {
  sandbox.__recentAnswer = answer;
  run('place = null');
  await boot("#job/job-1");
  assert.deepEqual([run('place.jobs[0]'), run('place.area')], ["job-1", null]);  // it simply opens
  assert(!String(sandbox.location.href || "").includes("/login"));  // reading the place never sends to sign-in
}
// The user goes elsewhere while the place is read: that wins.
let release;
sandbox.__recentAnswer = {status: 200, body: {place: {kind: "job", jobs: ["job-1"], language: "en", area: "gaps", step: null, seen: {}}, gone: false}};
const slow = sandbox.fetch;
sandbox.fetch = (path, options) => path === "/api/recent" && !options?.method ? new Promise(r => { release = () => r(slow(path, options)); }) : slow(path, options);
run('fetch = window.fetch');
sandbox.location.hash = "#job/job-1"; sandbox.location.search = "";
const starting = run('startPage()');
sandbox.location.hash = "#job/job-2";
release(); await starting; await settled();
assert.equal(run('restoring'), null);
''')

    def test_going_away_and_back_while_the_place_is_read_drops_the_start_restore(self):
        # Found by Codex (aba-repro): A→B→A during a slow read, then work on A; the late answer
        # still put back the sign-in hint (zh/cv/pages) over it and drew A again.
        self.check_js(BOOT + r'''
let release;
const answer = {status: 200, body: {place: {kind: "job", jobs: ["job-1"], language: "zh", area: "cv", step: "pages", seen: {}}, gone: false}};
const quick = sandbox.fetch;
sandbox.fetch = (path, options) => path === "/api/recent" && !options?.method
  ? new Promise((resolve) => { release = () => resolve({ok: true, status: 200, json: async () => answer.body}); })
  : quick(path, options);
run('fetch = window.fetch');
sandbox.location.hash = "#job/job-1"; sandbox.location.search = "?resume=cv.zh.pages"; sandbox.location.pathname = "/";
const starting = run('startPage()');
sandbox.location.hash = "#job/job-2"; run('route()'); await settled();  // A → B
sandbox.location.hash = "#job/job-1"; run('route()'); await settled();  // → A again
run('workingIn("requirements", "en")');  // and the user works on A
const drawn = sandbox.__calls.filter(call => call.path === "/api/jobs/job-1").length;
release(); await starting; await settled();
assert.deepEqual([run('place.jobs[0]'), run('place.area'), run('place.language'), run('place.step')], ["job-1", "requirements", "en", null]);
assert.equal(run('restoring'), null);
assert.equal(sandbox.__calls.filter(call => call.path === "/api/jobs/job-1").length, drawn);  // not drawn again
assert.equal(run('keptShown'), null);
''')

    def test_the_control_shown_is_the_one_that_follows_now_or_the_running_work(self):
        self.check_js(BOOT + r'''
// The PDF was created meanwhile: its download is shown, not the export that is done.
sandbox.__view.cv.en = cv({head: "approved", approved_at: "2026-10-20T00:00:00Z", final_pdf: true});
sandbox.__recentAnswer = {status: 200, body: {place: {kind: "job", jobs: ["job-1"], language: "en", area: "cv", step: "export", seen: {}}, gone: false}};
await boot("#job/job-1");
assert.equal(shown(), "Download final PDF");
// Still running: its own note is what is shown; nothing is marked or redone.
sandbox.__view.cv.en = cv({head: "draft"});
sandbox.__view.tasks = [{task_id: "t1", operation: "tailor_cv", status: "running"}];
sandbox.__recentAnswer.body.place.step = "tailor";
run('place = null');
await boot("#job/job-1");
assert(/Working on it: rewording your CV/.test(shown()));
assert(!sandbox.__calls.some(call => call.method === "POST"));
''')

    def test_a_step_is_kept_while_working_in_the_same_part_and_reopening_the_job_keeps_it(self):
        self.check_js(BOOT + r'''
await boot("#job/job-1");
run('workingIn("cv", "en", {step: "fit"})');
assert.equal(run('place.step'), "fit");
run('workingIn("cv", "en")');  // another click in the same CV keeps the control
assert.equal(run('place.step'), "fit");
run('workingIn("requirements", null)');  // another part of the page: no control
assert.equal(run('place.step'), null);
run('workingIn("cv", "en", {step: "pages"})');
// Opening the same job again from the list in this tab keeps where the user was in it.
sandbox.location.hash = "#jobs"; run('pageGeneration += 1');
sandbox.location.hash = "#job/job-1"; run('pageGeneration += 1');
await run('renderJob("job-1")');
assert.deepEqual([run('place.area'), run('place.step')], ["cv", "pages"]);
// Another job starts afresh.
sandbox.location.hash = "#job/job-2"; run('pageGeneration += 1');
await run('renderJob("job-2")');
assert.deepEqual([run('place.area'), run('place.step')], [null, null]);
''')

    def test_download_all_after_the_session_ended_signs_in_and_comes_back_to_the_batch(self):
        # Found by Codex for the batch's own download request: it only said "Please sign in again".
        self.check_js(BOOT + r'''
const view = (id) => ({job_id: id, language: "en", jd: {title: id}, tasks: [], cv: {en: cv({head: "approved",
  approved_at: "2026-10-20T00:00:00Z", final_pdf: true, download_name: id + ".pdf"}), zh: {head: null}}});
run(`api = async (path) => { const id = path.split("/")[3]; return ${JSON.stringify(view("X"))}; }`);
await boot("#batch/j1,j2");
const asked = [];
sandbox.fetch = async (path) => { asked.push(path); return {ok: false, status: 401, json: async () => ({error: "Please sign in again"})}; };
run('fetch = window.fetch');
await button(app, "Download all").fire("click");
await settled();
assert.equal(asked.length, 1);  // asked once; nothing more, and nothing again after signing in
assert(asked[0].startsWith("/download/X/en.pdf?v="));
assert.equal(sandbox.location.href, "/login?return_to=" + encodeURIComponent("/?resume=batch.-.-#batch/j1,j2"));
''')

    def test_a_batch_named_in_the_address_comes_back_as_a_batch(self):
        self.check_js(BOOT + r'''
const view = (id) => ({job_id: id, language: "en", jd: {title: id}, tasks: [], cv: {en: cv({}), zh: {head: null}}});
run(`api = async (path) => (${JSON.stringify(view("X"))})`);
sandbox.__recentAnswer = {status: 200, body: {place: {kind: "batch", jobs: ["j1", "j2"], language: null, area: "batch", step: null, seen: {}}, gone: false}};
await boot("#batch/j1,j2", "?resume=batch.-.tailor");
assert.deepEqual([run('place.kind'), run('place.area'), run('place.step')], ["batch", "batch", null]);
''')


def load_tests(loader, standard_tests, pattern):
    """Only the tests written here: the classes reuse other modules' fixtures, not their tests."""
    suite = unittest.TestSuite()
    for case in (StepRuleTests, V2StepTests, V2DownloadSignInTests, ResumeUITests):
        for name in loader.getTestCaseNames(case):
            if name in vars(case):
                suite.addTest(case(name))
    return suite


if __name__ == "__main__":
    unittest.main()
