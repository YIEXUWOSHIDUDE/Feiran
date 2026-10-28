"""Download all on a batch: the PDFs the page shows ready, one after another, each only for the
version the page showed (?v= on the single-PDF route, checked with the PDF in one read: 409
otherwise), under the server's own name for it. The set is fixed at the click; one queue at a
time whatever redraws; the first failure (signed out, changed, network) stops the rest and the page
says how many were handed to the browser; leaving the page stops it; nothing is retried. No ZIP is
asked for. Synthetic jobs; the model and the printer are the tests' stand-ins (FakeDeepSeek,
FakePrinter)."""
import unittest

from feiran import tenant_store, web
from tests import test_web, test_web_v2
from tests.test_sponsor_ui import NODE, SponsorUITests


class V2VersionedPdfTests(test_web_v2.V2Case):
    def printed(self, client, headers):
        job_id = self.new_job(client, headers)
        self.assertEqual(self.approve(client, headers, job_id).status_code, 200)
        self.settle(client, headers, client.post(f"/api/jobs/{job_id}/cv/en/export", headers=headers))
        return job_id, client.get(f"/api/jobs/{job_id}", headers=headers).json()["cv"]["en"]["content_sha256"]

    def test_a_pdf_is_sent_only_for_the_version_the_page_showed(self):
        client, headers = self.ready_user("alice")
        job_id, sha = self.printed(client, headers)
        plain = client.get(f"/download/{job_id}/en.pdf", headers=headers)  # the single link, as before
        self.assertEqual((plain.status_code, plain.content[:4]), (200, b"%PDF"))
        same = client.get(f"/download/{job_id}/en.pdf", params={"v": sha}, headers=headers)
        self.assertEqual((same.content, same.headers["content-disposition"]), (plain.content, plain.headers["content-disposition"]))
        other = client.get(f"/download/{job_id}/en.pdf", params={"v": "f" * 64}, headers=headers)
        self.assertEqual((other.status_code, other.json()["error"]), (409, tenant_store.PDF_CHANGED))
        for bad in ("", "abc", "A" * 64, "g" * 64):
            self.assertEqual(client.get(f"/download/{job_id}/en.pdf", params={"v": bad}, headers=headers).status_code, 400, bad)
        # Another account learns nothing, not even whether the version is right.
        bob, bob_headers = self.ready_user("bob")
        self.assertEqual(bob.get(f"/download/{job_id}/en.pdf", params={"v": sha}, headers=bob_headers).status_code, 404)
        # Edited since: the version the page showed is refused, never the new one sent in its place.
        line = client.get(f"/api/jobs/{job_id}", headers=headers).json()["cv"]["en"]["lines"][0]
        client.post(f"/api/jobs/{job_id}/cv/en/lines/{line['block']}", headers=headers,
                    json={"text": line["text"] + " Updated.", "expected_text": line["text"]})
        self.assertEqual(client.get(f"/download/{job_id}/en.pdf", params={"v": sha}, headers=headers).status_code, 409)
        self.assertEqual(client.get(f"/download/{job_id}/en.pdf", headers=headers).status_code, 404)  # as before


class V1VersionedPdfTests(test_web.WebTests):
    def test_the_single_user_app_checks_the_version_with_the_pdf_at_one_moment(self):
        job_id = self.planned_job()
        sha = self.job(job_id)["cv"]["en"]["content_sha256"]
        self.assertEqual(self.approve(job_id, sha).status_code, 200)
        self.client.post(f"/api/jobs/{job_id}/cv/en/export", headers=self.headers)
        token = {"token": test_web.TOKEN}
        plain = self.client.get(f"/download/{job_id}/en.pdf", params=token)
        self.assertEqual(plain.status_code, 200, plain.text)
        self.assertEqual(self.client.get(f"/download/{job_id}/en.pdf", params={**token, "v": sha}).content, plain.content)
        self.assertEqual(self.client.get(f"/download/{job_id}/en.pdf", params={**token, "v": "f" * 64}).status_code, 409)
        self.assertEqual(self.client.get(f"/download/{job_id}/en.pdf", params={**token, "v": "x"}).status_code, 400)
        # Read inside the change boundary, with the facts and profiles it is checked against.
        self.assertTrue(web.SNAPSHOT_READS.fullmatch(f"/download/{job_id}/en.pdf"))


PAGE = r'''
const later = sandbox.setTimeout;
sandbox.setTimeout = (action, delay) => delay === 2000 || delay === 60000 || delay === 800 ? 0 : later(action, delay === 400 ? 0 : delay);
const ready = (name, sha) => ({head: "approved", content_sha256: sha, stale: false, final_pdf: true, approved_at: "2026-10-20T00:00:00Z",
  pagination: {pages: 1, checked_at: "2026-10-20T00:00:00Z"}, download_name: name});
sandbox.__views = {
  a: {job_id: "a", language: "en", jd: {title: "Alpha", company: "Acme"}, tasks: [], cv: {en: ready("CV-Acme-Alpha-EN-a-vaaaaaaaa.pdf", "a".repeat(64)), zh: {head: null}}},
  b: {job_id: "b", language: "zh", jd: {title: "后端工程师", company: "示例公司"}, tasks: [], cv: {en: {head: null}, zh: ready("CV-示例公司-后端工程师-ZH-b-vbbbbbbbb.pdf", "b".repeat(64))}},
  c: {job_id: "c", language: "en", jd: {title: "Gamma", company: "Acme"}, tasks: [], cv: {en: ready("CV-Acme-Gamma-EN-c-vcccccccc.pdf", "c".repeat(64)), zh: {head: null}}},
  d: {job_id: "d", language: "en", jd: {title: "Delta"}, tasks: [], cv: {en: {head: "planned", content_sha256: "d".repeat(64), stale: false, final_pdf: false}, zh: {head: null}}},
};
run(`api = async (path) => { const id = decodeURIComponent(path.split("/")[3]);
  if (!__views[id]) throw new Error("Job not found"); return JSON.parse(JSON.stringify(__views[id])); }`);
// What the browser is handed: each link clicked, with the name it saves under.
const handed = [];
const create = sandbox.document.createElement;
sandbox.document.createElement = (tag) => { const node = create(tag); if (tag === "a") { node.click = () => handed.push(node.attributes.download); node.remove = () => {}; } return node; };
sandbox.document.body = {append() {}};
sandbox.URL.createObjectURL = () => "blob:synthetic";
sandbox.URL.revokeObjectURL = () => {};
// The server's answers: a PDF named as the server names it, or what a test puts in __answer[path].
const fetched = [];
sandbox.__answer = {};
sandbox.fetch = async (url) => {
  fetched.push(url);
  const path = url.split("?")[0];
  const special = sandbox.__answer[path];
  if (special) return special();
  const [, , job, file] = path.split("/");
  const language = file.slice(0, 2);
  const name = sandbox.__views[job].cv[language].download_name;
  return {ok: true, status: 200, json: async () => ({}), blob: async () => new Blob(["%PDF-synthetic"]),
    headers: {get: (h) => h === "Content-Disposition" ? `attachment; filename="ascii.pdf"; filename*=UTF-8''${encodeURIComponent(name)}` : null}};
};
run('fetch = window.fetch');
sandbox.document.querySelectorAll = () => [];
const settle = () => new Promise(resolve => later(resolve, 20));
const until = async (test) => { for (let i = 0; i < 200 && !test(); i += 1) await settle(); assert(test()); };
const open = async (ids) => { sandbox.location.hash = "#batch/" + ids; run('pageGeneration += 1'); await run(`renderBatch("${ids}")`); };
const note = () => { const n = find(app, n => ["warning", "ok-text"].includes(n.attributes?.class) && /Handed/.test(n.textContent)); return n ? n.textContent : null; };
'''


@unittest.skipUnless(NODE, "Node is needed for frontend behavior checks")
class DownloadAllUITests(unittest.TestCase):
    check_js = SponsorUITests.check_js

    def test_every_ready_pdf_one_after_another_under_its_own_name_and_version(self):
        for mode in ("v2", "local"):
            with self.subTest(mode=mode):
                self.check_js(PAGE + r'''
await open("a,b,c,d");
assert(app.textContent.includes("3/4 downloadable"));
assert(app.textContent.includes("Your browser may ask to allow this site to download several files."));
assert(!/ZIP/.test(app.textContent));
await button(app, "Download all").fire("click");
assert("disabled" in button(app, "Downloading 0/3…").attributes);
await until(() => note());
const token = MODE === "v2" ? "" : "&token=synthetic-token";
assert.deepEqual(fetched, ["/download/a/en.pdf?v=" + "a".repeat(64) + token, "/download/b/zh.pdf?v=" + "b".repeat(64) + token,
                           "/download/c/en.pdf?v=" + "c".repeat(64) + token]);
assert.deepEqual(handed, ["CV-Acme-Alpha-EN-a-vaaaaaaaa.pdf", "CV-示例公司-后端工程师-ZH-b-vbbbbbbbb.pdf", "CV-Acme-Gamma-EN-c-vcccccccc.pdf"]);
assert.equal(note(), "Handed all 3 ready PDF(s) to the browser.");  // handed over, never "saved"
assert(button(app, "Download all"));
assert(!fetched.some(url => url.includes("batch.zip")));
assert(!sandbox.__calls?.some?.(call => call.method === "POST"));
''', mode=mode)

    def test_a_cv_changed_meanwhile_or_a_network_failure_stops_the_rest_and_says_how_many(self):
        self.check_js(PAGE + r'''
await open("a,b,c");
sandbox.__answer["/download/b/zh.pdf"] = async () => ({ok: false, status: 409,
  json: async () => ({error: "This CV changed after the page showed it; reload the page, then download again."})});
await button(app, "Download all").fire("click");
await until(() => note());
assert.equal(fetched.length, 2);  // c is never asked for
assert.deepEqual(handed, ["CV-Acme-Alpha-EN-a-vaaaaaaaa.pdf"]);
assert.equal(note(), "Handed 1 of 3 PDF(s) to the browser, then stopped: This CV changed after the page showed it; reload the page, then download again.");
// A network failure on the first: none handed, said so; clicking again starts afresh, nothing is retried by itself.
fetched.length = 0; handed.length = 0;
sandbox.__answer = {"/download/a/en.pdf": async () => { throw new Error("offline"); }};
await button(app, "Download all").fire("click");
await until(() => /Handed 0/.test(note() || ""));
assert.equal(note(), "Handed 0 of 3 PDF(s) to the browser, then stopped: the connection failed");
assert.equal(fetched.length, 1);
await settle(); await settle();
assert.equal(fetched.length, 1);
''')

    def test_nothing_ready_means_no_button_and_a_missing_job_still_lets_the_ready_ones_go(self):
        self.check_js(PAGE + r'''
await open("d");
assert(!button(app, "Download all"));
assert(app.textContent.includes("Nothing is ready to download yet."));
await open("a,gone");
assert(app.textContent.includes("A job in this batch is not available."));
assert(app.textContent.includes("1/1 downloadable"));  // the unreadable job is not counted as anything
assert(!app.textContent.includes("Your browser may ask"));  // one file: no such question
await button(app, "Download all").fire("click");
await until(() => note());
assert.deepEqual(fetched, ["/download/a/en.pdf?v=" + "a".repeat(64)]);
assert.equal(note(), "Handed all 1 ready PDF(s) to the browser.");
''')

    def test_one_queue_whatever_clicks_and_redraws_and_leaving_the_page_stops_it(self):
        self.check_js(PAGE + r'''
await open("a,b,c");
let releaseB;
sandbox.__answer["/download/b/zh.pdf"] = () => new Promise((resolve) => { releaseB = () => {
  delete sandbox.__answer["/download/b/zh.pdf"]; resolve(sandbox.fetch("/download/b/zh.pdf")); fetched.pop(); }; });
const first = button(app, "Download all");
await first.fire("click");
await first.fire("click");  // the same button again
await until(() => releaseB);
await button(app, "Reload the batch").fire("click");  // a redraw while it runs
await settle();
assert("disabled" in button(app, "Downloading 1/3…").attributes);
assert(!button(app, "Download all"));
releaseB();
await until(() => note());
assert.equal(fetched.length, 3);
assert.equal(handed.length, 3);
// Leaving the page while a file is on its way: it is not handed over, the rest is not asked for.
fetched.length = 0; handed.length = 0;
let releaseA;
sandbox.__answer["/download/a/en.pdf"] = () => new Promise((resolve) => { releaseA = () => {
  delete sandbox.__answer["/download/a/en.pdf"]; resolve(sandbox.fetch("/download/a/en.pdf")); fetched.pop(); }; });
await button(app, "Download all").fire("click");
await until(() => releaseA);
sandbox.location.hash = "#jobs"; run('pageGeneration += 1');
releaseA();
await settle(); await settle(); await settle();
assert.equal(handed.length, 0);
assert.equal(fetched.length, 1);
''')


@unittest.skipUnless(NODE, "Node is needed for frontend behavior checks")
class LateAnswerTests(unittest.TestCase):
    check_js = SponsorUITests.check_js

    def test_a_late_sign_in_answer_after_the_user_left_does_not_take_them_anywhere(self):
        # Found in review by Codex: the first file still on its way, the user opens another job;
        # its late 401 still sent the new page to sign-in.
        for back in (False, True):  # left for another job; left and came back (A→B→A)
            with self.subTest(back=back):
                self.check_js(PAGE + r'''
sandbox.location.href = "unchanged";
await open("a,b");
let answer401;
sandbox.__answer["/download/a/en.pdf"] = () => new Promise((resolve) => { answer401 = () => resolve(
  {ok: false, status: 401, json: async () => ({error: "Please sign in again"})}); });
await button(app, "Download all").fire("click");
await until(() => answer401);
sandbox.location.hash = "#job/c"; run('pageGeneration += 1');
if (BACK) { sandbox.location.hash = "#batch/a,b"; run('pageGeneration += 1'); }
answer401();
await settle(); await settle(); await settle();
assert.equal(sandbox.location.href, "unchanged");  // not sent to sign-in
assert.equal(fetched.length, 1);  // and nothing more asked for
assert.equal(handed.length, 0);
'''.replace("BACK", "true" if back else "false"))


def load_tests(loader, standard_tests, pattern):
    """Only the tests written here: the classes reuse other modules' fixtures, not their tests."""
    suite = unittest.TestSuite()
    for case in (V2VersionedPdfTests, V1VersionedPdfTests, DownloadAllUITests, LateAnswerTests):
        for name in loader.getTestCaseNames(case):
            if name in vars(case):
                suite.addTest(case(name))
    return suite


if __name__ == "__main__":
    unittest.main()
