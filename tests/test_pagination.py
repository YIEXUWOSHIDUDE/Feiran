"""The page check before approval, in both apps: approving needs a real print of exactly the
version shown, counted as the final PDF prints (no watermark); a CV on more than one page is
approved only when the user accepts that; a failed print records nothing; any change to the CV
(an edit, a fit, preparing again) makes the check stale; a CV changed while it prints is never
given the old count. Printing is a controlled stand-in printer (no Chrome): page counts here
are what it was told to print, not a real layout."""
import unittest
from pathlib import Path
from unittest.mock import patch

from feiran.cv import PAGES_OVER, PAGES_UNCHECKED
from feiran.deepseek_client import DeepSeekError
from feiran import tenant_store
from tests import test_sponsor_ui, test_web, test_web_v2
from tests.test_guest_workspace import GuestCase


class ControlledPrinter:
    """Writes a PDF stub of ``pages`` pages, or nothing when ``fails``; keeps the HTML printed."""

    def __init__(self):
        self.pages, self.fails, self.html, self.calls, self.during = 1, False, None, 0, None

    def __call__(self, html_path, pdf_path):
        self.calls += 1
        self.html = Path(html_path).read_text(encoding="utf-8")
        if self.during:
            self.during()
        if self.fails:
            return  # Chrome wrote no PDF
        Path(pdf_path).write_bytes(b"%PDF-1.4\n" + b"".join(b"%d 0 obj << /Type /Page >>\n" % (n + 1)
                                                         for n in range(self.pages)) + b"%%EOF\n")


class V2PageCheckTests(test_web_v2.V2Case):
    def setUp(self):
        with patch.object(test_web_v2, "FakePrinter", ControlledPrinter):
            super().setUp()

    def ready(self):
        client, headers = self.ready_user()
        return client, headers, self.new_job(client, headers)

    def cv(self, client, headers, job_id):
        return client.get(f"/api/jobs/{job_id}", headers=headers).json()["cv"]["en"]

    def check(self, client, headers, job_id):
        return self.settle(client, headers, client.post(f"/api/jobs/{job_id}/cv/en/pages", headers=headers))

    def try_approve(self, client, headers, job_id, **extra):
        return client.post(f"/api/jobs/{job_id}/cv/en/approve", headers=headers,
                           json={"expected_content_sha256": self.cv(client, headers, job_id)["content_sha256"], **extra})

    def test_two_pages_are_shown_before_approval_and_need_the_users_acceptance(self):
        client, headers, job_id = self.ready()
        refused = self.try_approve(client, headers, job_id, allow_multiple_pages=True)
        self.assertEqual((refused.status_code, refused.json()["error"]), (409, PAGES_UNCHECKED))
        self.printer.pages = 2
        task = self.check(client, headers, job_id)
        self.assertEqual((task["status"], task["result"]["pages"]), ("succeeded", 2))
        self.assertNotIn('class="watermark"', self.printer.html)  # counted as the final PDF prints
        self.assertEqual(self.cv(client, headers, job_id)["pagination"]["pages"], 2)
        refused = self.try_approve(client, headers, job_id)
        self.assertEqual((refused.status_code, refused.json()["error"]), (409, PAGES_OVER))
        self.assertEqual(self.try_approve(client, headers, job_id, allow_multiple_pages=True).status_code, 200)
        # The same version checked again prints nothing new (the same task answers).
        calls = self.printer.calls
        self.assertEqual(self.check(client, headers, job_id)["task_id"], task["task_id"])
        self.assertEqual(self.printer.calls, calls)

    def test_one_page_is_approved_and_any_change_needs_another_check(self):
        client, headers, job_id = self.ready()
        self.assertEqual(self.check(client, headers, job_id)["result"]["pages"], 1)
        self.assertEqual(self.try_approve(client, headers, job_id).status_code, 200)
        line = self.cv(client, headers, job_id)["lines"][0]
        edited = client.post(f"/api/jobs/{job_id}/cv/en/lines/{line['block']}", headers=headers,
                             json={"text": line["text"] + " Updated.", "expected_text": line["text"]})
        self.assertEqual(edited.status_code, 200, edited.text)
        self.assertNotIn("pagination", self.cv(client, headers, job_id))
        self.assertEqual(self.try_approve(client, headers, job_id, allow_multiple_pages=True).json()["error"],
                         PAGES_UNCHECKED)
        # Preparing again (a new version) needs its own check too.
        self.check(client, headers, job_id)
        self.settle(client, headers, client.post(f"/api/jobs/{job_id}/cv/en/prepare", headers=headers))
        self.assertNotIn("pagination", self.cv(client, headers, job_id))

    def test_a_failed_print_records_nothing_and_cannot_approve(self):
        client, headers, job_id = self.ready()
        self.printer.fails = True
        task = self.check(client, headers, job_id)
        self.assertEqual((task["status"], task["error_code"]), ("failed", "pdf_failed"), task)
        self.assertNotIn("pagination", self.cv(client, headers, job_id))
        self.assertEqual(self.try_approve(client, headers, job_id, allow_multiple_pages=True).status_code, 409)
        self.printer.fails = False  # trying again prints again and works
        self.assertEqual(self.check(client, headers, job_id)["status"], "succeeded")
        self.assertEqual(self.try_approve(client, headers, job_id).status_code, 200)

    def test_a_cv_edited_while_it_prints_never_gets_the_old_count(self):
        client, headers, job_id = self.ready()
        line = self.cv(client, headers, job_id)["lines"][0]

        def edit():
            self.printer.during = None
            response = client.post(f"/api/jobs/{job_id}/cv/en/lines/{line['block']}", headers=headers,
                                   json={"text": line["text"] + " Updated.", "expected_text": line["text"]})
            self.assertEqual(response.status_code, 200, response.text)
        self.printer.during = edit
        self.assertEqual(self.check(client, headers, job_id)["status"], "superseded")
        self.assertNotIn("pagination", self.cv(client, headers, job_id))
        self.assertEqual(self.try_approve(client, headers, job_id).status_code, 409)

    def test_a_failed_reword_of_the_same_version_keeps_its_check(self):
        client, headers, job_id = self.ready()
        self.check(client, headers, job_id)
        self.chat.tailor_error = DeepSeekError("synthetic outage", reason="rate_limited")
        cv = self.cv(client, headers, job_id)
        task = self.settle(client, headers, client.post(f"/api/jobs/{job_id}/cv/en/tailor", headers=headers))
        self.assertEqual(task["status"], "failed", task)  # recorded its stages, published nothing
        after = self.cv(client, headers, job_id)
        self.assertEqual(after["content_sha256"], cv["content_sha256"])
        self.assertIn("rewording", [stage["stage"] for stage in after["stages"]])
        self.assertEqual(after["pagination"]["pages"], 1)
        self.assertEqual(self.try_approve(client, headers, job_id).status_code, 200)

    def test_another_user_cannot_check_or_read_the_check(self):
        client, headers, job_id = self.ready()
        other, other_headers = self.browser("bob")
        self.assertEqual(other.post(f"/api/jobs/{job_id}/cv/en/pages", headers=other_headers).status_code, 404)
        self.assertEqual(other.get(f"/api/jobs/{job_id}", headers=other_headers).status_code, 404)


class GuestPageCheckTests(GuestCase):
    def test_a_guest_checks_pages_without_agreeing_to_ai_and_it_is_a_print_run(self):
        client, headers, job_id = self.guest()
        task = self.settle(client, headers, client.post(f"/api/jobs/{job_id}/cv/en/pages", headers=headers))
        self.assertEqual(task["status"], "succeeded", task)
        guest = client.get("/api/me", headers=headers).json()["guest"]
        self.assertEqual((guest["prints_used"], guest["ai"]["used"], client.get("/api/me", headers=headers).json()["consents"]),
                         (1, 0, {}))
        self.assertEqual(self.chat.sent, [])


class V1PageCheckTests(test_web.WebTests):
    def setUp(self):
        with patch.object(test_web, "FakePrinter", ControlledPrinter):
            super().setUp()

    def printer_of(self):
        """The app's printer: the one ControlledPrinter the app was made with."""
        return next(cell.cell_contents for route in self.app.routes if getattr(route, "name", "") == "cv_pages"
                    for cell in (route.endpoint.__closure__ or ()) if isinstance(cell.cell_contents, ControlledPrinter))

    def test_the_single_user_app_needs_the_same_check(self):
        job_id = self.planned_job()
        printer = self.printer_of()
        shown = self.job(job_id)["cv"]["en"]["content_sha256"]
        approve = f"/api/jobs/{job_id}/cv/en/approve"
        refused = self.client.post(approve, headers=self.headers, json={"expected_content_sha256": shown})
        self.assertEqual((refused.status_code, refused.json()["error"]), (409, PAGES_UNCHECKED))
        printer.pages = 2
        checked = self.client.post(f"/api/jobs/{job_id}/cv/en/pages", headers=self.headers).json()["cv"]["en"]
        self.assertEqual(checked["pagination"]["pages"], 2)
        self.assertNotIn('class="watermark"', printer.html)
        refused = self.client.post(approve, headers=self.headers, json={"expected_content_sha256": shown})
        self.assertEqual(refused.json()["error"], PAGES_OVER)
        self.put_back_the_cut_line(job_id)  # a change: the check no longer holds
        now = self.job(job_id)["cv"]["en"]
        self.assertNotIn("pagination", now)
        self.assertEqual(self.client.post(approve, headers=self.headers, json={
            "expected_content_sha256": now["content_sha256"], "allow_multiple_pages": True}).json()["error"], PAGES_UNCHECKED)
        printer.fails = True
        failed = self.client.post(f"/api/jobs/{job_id}/cv/en/pages", headers=self.headers)
        self.assertGreaterEqual(failed.status_code, 400)
        self.assertNotIn("pagination", self.job(job_id)["cv"]["en"])
        printer.fails, printer.pages = False, 1
        self.client.post(f"/api/jobs/{job_id}/cv/en/pages", headers=self.headers)
        approved = self.client.post(approve, headers=self.headers, json={"expected_content_sha256": now["content_sha256"]})
        self.assertEqual(approved.json()["cv"]["en"]["head"], "approved")
        self.assertEqual(approved.json()["cv"]["en"]["pagination"]["pages"], 1)  # still about this version


@unittest.skipUnless(test_sponsor_ui.NODE, "Node is needed for frontend behavior checks")
class PageCheckUITests(unittest.TestCase):
    check_js = test_sponsor_ui.SponsorUITests.check_js

    def test_approval_waits_for_the_check_and_extra_pages_need_their_own_box(self):
        for mode in ("v1", "v2"):
            with self.subTest(mode=mode):
                self.check_js(r'''
run(`api=async(path,options)=>{__calls.push({path,...options}); return {};};`);
const base = {head:"draft",content_sha256:"c",lines:[],edits:[],stages:[]};
const unchecked = run(`cvBlock({job_id:"j",cv:{en:${JSON.stringify(base)}}},"en",async()=>{})`);
const approve = button(unchecked, "Approve this CV");
assert(approve.disabled);
assert(unchecked.textContent.includes("check how many pages this version prints on"));
await button(unchecked, "Check page count").fire("click");
assert.equal(sandbox.__calls.at(-1).path, "/api/jobs/j/cv/en/pages");
const two = run(`cvBlock({job_id:"j",cv:{en:${JSON.stringify({...base,pagination:{pages:2,checked_at:"2026-10-24T00:00:00Z"}})}}},"en",async()=>{})`);
assert(two.textContent.includes("prints on 2 pages"));
const boxes = nodes(two).filter(n => n.tagName === "input");
assert.equal(boxes.length, 2);
const [accept, read] = boxes;
read.checked = true; await read.fire("change");
assert(button(two, "Approve this CV").disabled);  // the extra pages are not accepted yet
accept.checked = true; await accept.fire("change");
assert(!button(two, "Approve this CV").disabled);
await button(two, "Approve this CV").fire("click");
assert.deepEqual(JSON.parse(sandbox.__calls.at(-1).body), {expected_content_sha256:"c", allow_multiple_pages:true});
const one = run(`cvBlock({job_id:"j",cv:{en:${JSON.stringify({...base,pagination:{pages:1,checked_at:"2026-10-24T00:00:00Z"}})}}},"en",async()=>{})`);
assert.equal(nodes(one).filter(n => n.tagName === "input").length, 1);
assert(one.textContent.includes("prints on 1 page"));
for (const key of ["Check page count", "Check pages again", "Before approving, check how many pages this version prints on.",
                   "The final PDF of this version prints on 1 page (checked {0}).", " I accept that this CV prints on {0} pages.",
                   "The final PDF of this version prints on {0} pages (checked {1}). Use Fit to one page above, or accept the extra pages below.",
                   "Check how many pages this version prints on before approving it.",
                   "This CV prints on more than one page. Fit it to one page, or accept the extra pages, then approve.",
                   "checking the page count"]) assert(sandbox.WorkbenchChinese[key], key);
''', mode=mode)


def load_tests(loader, standard_tests, pattern):
    """Only the tests written here: the classes reuse other modules' fixtures, not their tests."""
    suite = unittest.TestSuite()
    for case in (V2PageCheckTests, GuestPageCheckTests, V1PageCheckTests, PageCheckUITests):
        for name in loader.getTestCaseNames(case):
            if name in vars(case):
                suite.addTest(case(name))
    return suite


if __name__ == "__main__":
    unittest.main()
