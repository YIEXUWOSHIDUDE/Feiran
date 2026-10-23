"""Fit to one page in both apps, with synthetic CVs and a stand-in printer: the single-user page
(web.py) and V2's task (v2_flow, tenant_store). What is checked: the CV prints on one page, each
line left out is listed and undone on its own, the profile and saved material stay as they were,
the job's own edits stay, an approval or final PDF of the CV before does not carry over, a CV
changed while it was being fitted is not overwritten, and a CV that cannot fit, or already fits,
is not changed."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import tenant_store
from facts import confirm_facts, import_facts, list_facts
from tests.fit_fixture import ADDED_BULLET, PROFILE, REQUIREMENTS, TEXTS
from tests.test_cv_fit import RowPrinter
from tests.test_web import TOKEN, FakeDeepSeek
from tests import test_sponsor_ui, test_web_v2
import web as web_module
from web import create_app

TYPES = {"ms": "education", "be": "education", "x1": "experience", "x2": "experience", "p1": "project",
         "p2": "project", "sk": "skill"}
JD = "Requirements:\n" + "\n".join(f"- {text}" for _id, text, _strength in REQUIREMENTS)


def shown_rows(view):
    return {line["block"]: line["text"] for line in view["lines"]}


class SingleUserFitTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.database = self.root / "workbench.db"
        self.profile = self.root / "cv-profile.json"
        self.profile.write_text(json.dumps(PROFILE), encoding="utf-8")
        import_facts(self.database, [{"id": fact_id, "text": text, "type": TYPES.get(fact_id.split("-")[1], "other"), "tags": []}
                                     for fact_id, text in TEXTS.items()])
        confirm_facts(self.database, [(fact_id, 1) for fact_id in TEXTS])
        self.chat = FakeDeepSeek()
        self.chat.requirement_lines = [text for _id, text, _strength in REQUIREMENTS]
        self.printer = RowPrinter()
        self.app = create_app(facts_db=self.database, jobs_root=self.root / "jobs", token=TOKEN, profile_path=self.profile,
                              chat=self.chat, printer=self.printer, starter=[])
        self.client = TestClient(self.app, base_url="http://127.0.0.1:8765", headers={"X-Workbench-Token": TOKEN})
        self.job_id = self.client.post("/api/jobs", json={"title": "Backend Software Engineer", "text": JD}).json()["job_id"]
        self.workspace = self.app.state.workspace

    def view(self):
        return self.client.get(f"/api/jobs/{self.job_id}").json()["cv"]["en"]

    def pages(self):
        view = self.view()
        html = self.client.get(f"/preview/{self.job_id}/en", params={"v": view["content_sha256"], "token": TOKEN})
        self.assertEqual(html.status_code, 200)
        return self.printer.count(html.text)

    def adopt_bullet(self):
        """The user adds the reported suggested bullet under the first internship, in this CV only."""
        self.chat.evidence = lambda request: [{"id": item["id"], "verdict": "none"} for item in request["requirements"]]
        self.chat.gap_suggestions = lambda ids: []
        gaps = self.client.post(f"/api/jobs/{self.job_id}/gaps").json()["gaps"]
        place = next(place["id"] for place in gaps["places"] if place["kind"] == "bullet")
        requirement = gaps["requirements"][-1]["requirement_id"]
        response = self.client.post(f"/api/jobs/{self.job_id}/gaps/{requirement}/write",
                                    json={"place": place, "text": ADDED_BULLET})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["cv"]["en"]["edits"][0]["block"]

    def test_two_pages_become_one_reversibly_without_touching_the_profile(self):
        self.assertEqual(self.view()["head"], "tailored")  # DeepSeek could not adjust it: no plan yet
        block = self.adopt_bullet()
        self.assertEqual(self.pages(), 2)
        profile, facts = self.profile.read_bytes(), list_facts(self.database)
        response = self.client.post(f"/api/jobs/{self.job_id}/cv/en/fit")
        self.assertEqual(response.status_code, 200, response.text)
        cv = response.json()["cv"]["en"]
        self.assertEqual((cv["head"], cv["fit"]["status"], cv["fit"]["pages"]), ("planned", "fitted", 1))
        self.assertEqual(self.pages(), 1)
        self.assertEqual(shown_rows(cv)[block], ADDED_BULLET)  # the adopted line stays, word for word
        fit_cuts = [change for change in cv["changes"] if change.get("fit")]
        self.assertEqual(len(fit_cuts), len(cv["fit"]["changes"]))
        self.assertTrue(fit_cuts)
        self.assertEqual((self.profile.read_bytes(), list_facts(self.database)), (profile, facts))
        # Undo one cut: that part is back, and the note about fitting no longer claims one page.
        undone = self.client.post(f"/api/jobs/{self.job_id}/cv/en/change",
                                  json={"change_id": fit_cuts[0]["id"], "undone": True}).json()["cv"]["en"]
        self.assertTrue(next(change for change in undone["changes"] if change["id"] == fit_cuts[0]["id"])["undone"])
        self.assertNotIn("fit", undone)
        self.assertGreater(len(undone["lines"]), len(cv["lines"]))
        redone = self.client.post(f"/api/jobs/{self.job_id}/cv/en/change",
                                  json={"change_id": fit_cuts[0]["id"], "undone": False}).json()["cv"]["en"]
        self.assertEqual(redone["lines"], cv["lines"])

    def test_an_approval_and_its_pdf_do_not_carry_over_and_one_that_fits_stays(self):
        self.adopt_bullet()
        before = self.view()
        self.client.post(f"/api/jobs/{self.job_id}/cv/en/approve", json={"expected_content_sha256": before["content_sha256"]})
        self.client.post(f"/api/jobs/{self.job_id}/cv/en/export")
        self.assertTrue(self.view()["final_pdf"])
        fitted = self.client.post(f"/api/jobs/{self.job_id}/cv/en/fit").json()["cv"]["en"]
        self.assertEqual((fitted["head"], fitted["final_pdf"]), ("planned", False))
        self.assertEqual(self.client.get(f"/download/{self.job_id}/en.pdf", params={"token": TOKEN}).status_code, 400)
        self.assertEqual(self.client.post(f"/api/jobs/{self.job_id}/cv/en/export").status_code, 400)
        # Approve and export the one-page CV; fitting it again changes nothing and keeps both.
        self.client.post(f"/api/jobs/{self.job_id}/cv/en/approve", json={"expected_content_sha256": fitted["content_sha256"]})
        self.client.post(f"/api/jobs/{self.job_id}/cv/en/export")
        again = self.client.post(f"/api/jobs/{self.job_id}/cv/en/fit").json()["cv"]["en"]
        self.assertEqual((again["head"], again["final_pdf"], again["fit"]["status"]), ("approved", True, "fits"))
        self.assertEqual(again["content_sha256"], fitted["content_sha256"])
        pdf = self.client.get(f"/download/{self.job_id}/en.pdf", params={"token": TOKEN})
        self.assertEqual((pdf.status_code, pdf.content.count(b"/Type /Page")), (200, 1))

    def test_no_edit_lands_while_the_cv_is_being_fitted(self):
        self.adopt_bullet()
        real = RowPrinter.__call__
        refused = []

        def other_tab(printer, html_path, pdf_path):
            if not refused:  # another tab tries to save an edit while this CV is being printed
                refused.append(self.client.post(f"/api/jobs/{self.job_id}/cv/en/lines/fact-pub-2", json={
                    "text": "J. Sample. Edited in another tab. 2024.", "expected_text": TEXTS["fact-pub-2"]}))
            real(printer, html_path, pdf_path)

        with patch.object(RowPrinter, "__call__", other_tab), patch.object(web_module, "CHANGE_WAIT_SECONDS", 0.2):
            response = self.client.post(f"/api/jobs/{self.job_id}/cv/en/fit")
        self.assertEqual((refused[0].status_code, refused[0].json()["error"]), (409, web_module.BUSY))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["cv"]["en"]["fit"]["status"], "fitted")
        # Tried again afterwards, the edit meets the fitted CV: a line it left out must be put back first.
        shown = "fact-pub-2" in shown_rows(response.json()["cv"]["en"])
        retried = self.client.post(f"/api/jobs/{self.job_id}/cv/en/lines/fact-pub-2", json={
            "text": "J. Sample. Edited in another tab. 2024.", "expected_text": TEXTS["fact-pub-2"]})
        self.assertEqual(retried.status_code, 200 if shown else 400, retried.text)

    def test_a_cv_that_cannot_fit_or_already_fits_is_not_changed(self):
        self.adopt_bullet()
        self.printer.rows = 12
        before = self.view()
        cv = self.client.post(f"/api/jobs/{self.job_id}/cv/en/fit").json()["cv"]["en"]
        self.assertEqual((cv["head"], cv["content_sha256"]), ("tailored", before["content_sha256"]))
        self.assertEqual(cv["fit"]["status"], "cannot_fit")
        self.assertEqual(cv["fit"]["kept"]["edited"], 1)
        self.printer.rows = 500
        cv = self.client.post(f"/api/jobs/{self.job_id}/cv/en/fit").json()["cv"]["en"]
        self.assertEqual((cv["head"], cv["fit"]["status"], cv["content_sha256"]), ("tailored", "fits", before["content_sha256"]))

    def test_preparing_again_keeps_the_jobs_edits_and_starts_from_the_usual_layout(self):
        block = self.adopt_bullet()
        self.client.post(f"/api/jobs/{self.job_id}/cv/en/fit")
        cv = self.client.post(f"/api/jobs/{self.job_id}/cv/en/prepare").json()["cv"]["en"]
        self.assertEqual(cv["head"], "tailored")
        self.assertNotIn("fit", cv)
        self.assertEqual(shown_rows(cv)[block], ADDED_BULLET)
        self.assertEqual(len(cv["lines"]), len(TEXTS) + 1)


class V2FitTests(test_web_v2.V2Case):
    """The fit runs as a task on the PDF queue and publishes through the store's guard."""

    def setUp(self):
        with patch.object(test_web_v2, "FakePrinter", RowPrinter):
            super().setUp()

    def cv(self, client, headers, job_id):
        return client.get(f"/api/jobs/{job_id}", headers=headers).json()["cv"]["en"]

    def two_pages(self, client, headers, job_id):
        """Make the stand-in printer find this CV one line too long for one page."""
        cv = self.cv(client, headers, job_id)
        html = client.get(f"/preview/{job_id}/en", params={"v": cv["content_sha256"]}, headers=headers).text
        self.printer.rows = 1
        used = self.printer.count(html)
        self.printer.rows = used - 1
        self.assertEqual(self.printer.count(html), 2)

    def profile_versions(self):
        with tenant_store.transaction(self.database) as connection:
            return [tuple(row) for row in connection.execute("SELECT * FROM profile_versions ORDER BY version")]

    def test_fitting_publishes_a_new_version_that_undoes_and_supersedes_the_approval(self):
        client, headers = self.ready_user()
        job_id = self.new_job(client, headers)
        cv = self.cv(client, headers, job_id)
        edited = next(line for line in cv["lines"] if line["section"] == "experience")
        response = client.post(f"/api/jobs/{job_id}/cv/en/lines/{edited['block']}", headers=headers,
                               json={"text": "Built and tested REST APIs in Python.", "expected_text": edited["text"]})
        self.assertEqual(response.status_code, 200, response.text)
        cv = self.cv(client, headers, job_id)
        client.post(f"/api/jobs/{job_id}/cv/en/approve", headers=headers, json={"expected_content_sha256": cv["content_sha256"]})
        self.assertEqual(self.settle(client, headers, client.post(f"/api/jobs/{job_id}/cv/en/export", headers=headers))["status"],
                         "succeeded")
        profile, facts = self.profile_versions(), client.get("/api/facts", headers=headers).json()
        self.two_pages(client, headers, job_id)
        task = self.settle(client, headers, client.post(f"/api/jobs/{job_id}/cv/en/fit", headers=headers))
        self.assertEqual((task["status"], task["result"]["fit"]), ("succeeded", "fitted"), task)
        fitted = self.cv(client, headers, job_id)
        self.assertEqual((fitted["head"], fitted["final_pdf"], fitted["fit"]["pages"]), ("planned", False, 1))
        self.assertEqual(shown_rows(fitted)[edited["block"]], "Built and tested REST APIs in Python.")
        self.assertEqual(len(fitted["lines"]), len(cv["lines"]) - fitted["fit"]["left_out"])
        self.assertEqual((self.profile_versions(), client.get("/api/facts", headers=headers).json()), (profile, facts))
        # The PDF of the CV approved before is not the current one; exporting needs a new approval.
        self.assertEqual(client.get(f"/download/{job_id}/en.pdf", headers=headers).status_code, 404)
        self.assertEqual(client.post(f"/api/jobs/{job_id}/cv/en/export", headers=headers).status_code, 409)
        cut = next(change for change in fitted["changes"] if change.get("fit"))
        undone = client.post(f"/api/jobs/{job_id}/cv/en/change", headers=headers,
                             json={"change_id": cut["id"], "undone": True}).json()["cv"]["en"]
        self.assertEqual(len(undone["lines"]), len(cv["lines"]))
        self.assertNotIn("fit", undone)  # about the version before

    def test_a_fit_never_lands_on_a_cv_changed_meanwhile_and_one_that_cannot_fit_is_kept(self):
        client, headers = self.ready_user()
        job_id = self.new_job(client, headers)
        cv = self.cv(client, headers, job_id)
        self.two_pages(client, headers, job_id)
        line = cv["lines"][0]
        real = RowPrinter.__call__

        def other_tab(printer, html_path, pdf_path):
            if not getattr(printer, "edited", False):
                printer.edited = True
                response = client.post(f"/api/jobs/{job_id}/cv/en/lines/{line['block']}", headers=headers,
                                       json={"text": "Edited in another tab.", "expected_text": line["text"]})
                self.assertEqual(response.status_code, 200, response.text)
            real(printer, html_path, pdf_path)

        with patch.object(RowPrinter, "__call__", other_tab):
            task = self.settle(client, headers, client.post(f"/api/jobs/{job_id}/cv/en/fit", headers=headers))
        self.assertEqual(task["status"], "superseded", task)
        after = self.cv(client, headers, job_id)
        self.assertEqual(shown_rows(after)[line["block"]], "Edited in another tab.")
        self.assertNotIn("fit", after)
        self.printer.rows = 1
        task = self.settle(client, headers, client.post(f"/api/jobs/{job_id}/cv/en/fit", headers=headers))
        self.assertEqual((task["status"], task["result"]["fit"], task["result"]["materials"]), ("succeeded", "cannot_fit", []))
        refused = self.cv(client, headers, job_id)
        self.assertEqual((refused["content_sha256"], refused["fit"]["status"]), (after["content_sha256"], "cannot_fit"))
        # The same CV asked again finds that answer; an already short CV is left as it is.
        again = client.post(f"/api/jobs/{job_id}/cv/en/fit", headers=headers).json()["task"]
        self.assertEqual(again["task_id"], task["task_id"])
        response = client.post(f"/api/jobs/{job_id}/cv/en/lines/{line['block']}", headers=headers,
                               json={"text": "Short line.", "expected_text": "Edited in another tab."})
        self.assertEqual(response.status_code, 200, response.text)
        short = self.cv(client, headers, job_id)
        self.printer.rows = 500
        task = self.settle(client, headers, client.post(f"/api/jobs/{job_id}/cv/en/fit", headers=headers))
        self.assertEqual((task["result"]["fit"], task["result"]["materials"]), ("fits", []))
        self.assertEqual(self.cv(client, headers, job_id)["content_sha256"], short["content_sha256"])
        self.assertEqual(self.cv(client, headers, job_id)["fit"]["status"], "fits")


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(test_sponsor_ui.NODE, "Node is needed for frontend behavior checks")
class FitPageTests(unittest.TestCase):
    check_js = test_sponsor_ui.SponsorUITests.check_js

    def test_the_button_asks_the_server_and_each_outcome_reads_in_both_languages(self):
        for mode in ("v1", "v2"):
            with self.subTest(mode=mode):
                self.check_js(r'''
run(`__refreshes=0; api=async(path,options)=>{__calls.push({path,...options}); return {};};
__view={job_id:"j",cv:{en:{head:"tailored",content_sha256:"c",stale:false,lines:[],edits:[],changes:[]}}};`);
const block=run('cvBlock(__view,"en",async()=>{__refreshes++})');
await button(block,"Fit to one page").fire("click");
assert.equal(sandbox.__calls.at(-1).path,"/api/jobs/j/cv/en/fit");
assert.equal(sandbox.__calls.at(-1).method,"POST");
assert.equal(sandbox.__refreshes,1);
const fitted=run('fitNote({status:"fitted",pages_before:2,pages:1,left_out:3})').textContent;
assert(fitted.includes("it printed on 2 pages, so 3 less related line(s) are left out"));
const refused=run('fitNote({status:"cannot_fit",pages_before:2,pages:2,tried:7,capped:true,kept:{matched:0,edited:1,one_per_entry:3}})').textContent;
assert(refused.includes("even with 7 less related line(s) left out (at most half of what it shows), this CV prints on 2 pages"));
assert(refused.includes("1 added or edited in this CV; 3 the one line a job or the skills keep"));
assert(refused.includes("Nothing was changed"));
assert(run('fitNote({status:"fits",pages:1})').textContent.includes("Prints on one page"));
assert.equal(run('fitNote(undefined)'),null);
run('document.documentElement={}; document.getElementById=(id)=>id==="app"?__app:id==="interface-language"?{}:null; NodeFilter={SHOW_ELEMENT:1,SHOW_TEXT:4}; document.createTreeWalker=()=>({nextNode:()=>null}); I18n.setLanguage("zh")');
assert(run('fitNote({status:"fitted",pages_before:2,pages:1,left_out:3})').textContent.includes("已压缩为一页"));
assert(run('String(I18n.serverText("Left out to fit one page: less related to this job\'s requirements than what stays."))').startsWith("为压缩为一页而省略"));
const stale=run('cvBlock({job_id:"j",cv:{en:{head:"tailored",content_sha256:"c",stale:true,fit:{status:"fits"},lines:[],edits:[]}}},"en",async()=>{})');
assert(!find(stale,n=>n.tagName==="button"&&n.textContent==="压缩为一页"));
assert(!stale.textContent.includes("打印为一页"));
''', mode=mode)
