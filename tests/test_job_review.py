"""Offline acceptance of sponsor evidence and the review-before-generation boundary."""
import copy
import unittest
from datetime import timedelta
from unittest.mock import Mock, patch

import tenant_store
from job_review import build_job_review
from listings import ListingsError, group_postings
from task_runner import TaskContext
from tests.test_task_runner import RunnerCase
from tests.test_web_v2 import V2Case, JD_TEXT
from tests.test_listings import posting
from sponsorship import assess_sponsorship, shown
from tenant_store import UserWorkspace


def answered(row):
    """A posting row as ranked_listings (V1) or ranking_rows (V2) hands it over: with the answer
    for its words and its own source and capture time."""
    return {**row, "sponsorship": shown(assess_sponsorship(row["text"]), source=row["source"],
                                        captured_at=row["last_seen_at"])}


class ReviewEvidenceTests(unittest.TestCase):
    def test_all_current_material_supports_exact_job_quotes(self):
        jd = {"text": "Requirements:\nBuild Python services.\nSQL is useful.\nWe cannot provide visa sponsorship.",
              "source": "https://example.org/jobs/1", "captured_at": "2026-10-18T00:00:00Z"}
        facts = [{"id": "fact-python", "version": 2, "status": "confirmed",
                  "text": "Built a Python service.", "tags": ["Python"]},
                 {"id": "fact-sql", "version": 1, "status": "pending",
                  "text": "Used SQL.", "tags": ["SQL"]}]
        before = copy.deepcopy(facts)
        review = build_job_review(jd, facts, assess_sponsorship(jd["text"]))
        self.assertEqual(review["sponsorship"]["status"], "not_supported")
        self.assertEqual((review["confirmed_fact_count"], review["pending_fact_count"]), (1, 1))
        self.assertEqual(review["fact_count"], 2)
        self.assertEqual([item["tag"] for item in review["skill_matches"]], ["Python", "SQL"])
        hint = review["skill_matches"][0]
        self.assertIn(hint["job_quote"], jd["text"])
        self.assertEqual(hint["facts"][0], {"id": "fact-python", "version": 2, "text": "Built a Python service."})
        self.assertEqual(facts, before)
        facts[0]["status"] = "pending"
        self.assertEqual(len(build_job_review(jd, facts, None)["skill_matches"]), 2)
        # No answer is "not assessed", with this job's own provenance; never "unknown".
        missing = build_job_review(jd, facts, None)["sponsorship"]
        self.assertEqual((missing["status"], missing["evidence"], missing["source"], missing["captured_at"]),
                         ("not_assessed", [], jd["source"], jd["captured_at"]))

    def test_status_filters_apply_before_pagination_and_do_not_merge_different_policy(self):
        rows = []
        texts = ["We provide visa sponsorship.", "We cannot provide visa sponsorship.", "Python."]
        for index, text in enumerate(texts):
            row = posting(str(index), "Engineer", text)
            row.update(text_hash=str(index), last_seen_at=row["captured_at"], matched=["Python"])
            rows.append(answered(row))
        all_rows = group_postings(rows, 1)
        self.assertEqual(all_rows["total"], 3)
        self.assertEqual({item["sponsorship"]["status"] for item in all_rows["listings"]},
                         {"supported", "not_supported", "unknown"})
        unknown = group_postings(rows, 1, limit=1, sponsorship="unknown")
        self.assertEqual((unknown["total"], len(unknown["listings"])), (1, 1))
        self.assertEqual(unknown["listings"][0]["postings"][0]["job_id"], "2")
        with self.assertRaises(ListingsError):
            group_postings(rows, 1, sponsorship="probably")
        unassessed = {**posting("4", "Analyst", "We provide visa sponsorship."), "text_hash": "4",
                      "last_seen_at": "2026-10-19T00:00:00Z", "matched": []}
        waiting = group_postings(rows + [unassessed], 1, sponsorship="not_assessed")
        self.assertEqual([item["postings"][0]["job_id"] for item in waiting["listings"]], ["4"])
        self.assertEqual(group_postings(rows + [unassessed], 1, sponsorship="supported")["total"], 1)
        other_city = answered({**rows[0], "job_id": "3", "location": "Boston",
                               "source": "https://example.org/jobs/3", "last_seen_at": "2026-10-19T00:00:00Z"})
        grouped = group_postings([rows[0], other_city], 1)["listings"][0]
        self.assertEqual(len(grouped["postings"]), 2)
        self.assertEqual(grouped["postings"][1]["sponsorship"]["source"], other_city["source"])
        self.assertEqual(grouped["postings"][1]["sponsorship"]["captured_at"], other_city["last_seen_at"])


class ReviewFlowTests(V2Case):
    BODY = {"title": "Backend Intern", "text": JD_TEXT + "\nWe cannot provide visa sponsorship."}

    def counts(self):
        with tenant_store.transaction(self.database) as connection:
            return {name: connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                    for name in ("jobs", "tasks", "job_requirements", "materials", "quota_usage")}

    def units(self, task):
        with tenant_store.transaction(self.database) as connection:
            return connection.execute("SELECT units FROM tasks WHERE task_id = ?", (task["task_id"],)).fetchone()[0]

    def review(self, client, headers, key="review", **changes):
        return client.post("/api/jobs", json={**self.BODY, "review_only": True, **changes},
                           headers={**headers, "Idempotency-Key": key})

    def test_paste_review_needs_no_consent_quota_or_model_and_deduplicates(self):
        client, headers = self.browser()
        with tenant_store.transaction(self.database, write=True) as connection:
            connection.execute("DELETE FROM settings WHERE key IN ('user_daily_units', 'site_daily_units')")
        with patch.object(self.app.state.runner, "model", side_effect=AssertionError("No model while reviewing")) as model:
            saved = self.review(client, headers)
            self.assertEqual(saved.status_code, 200, saved.text)
            job_id = saved.json()["job_id"]
            view = client.get("/api/jobs/" + job_id, headers=headers).json()
            self.assertEqual(view["review"]["sponsorship"]["status"], "not_supported")
            self.assertEqual(view["requirements_version"], 0)
            self.assertFalse(view["cv"]["en"]["head"])
            again = self.review(client, headers, key="another-action")
            self.assertEqual(again.json()["job_id"], job_id)
            self.assertEqual(self.review(client, headers, text="Different words").status_code, 409)
            self.assertEqual(self.review(client, headers, review_only=False).status_code, 409)
            self.assertEqual(self.counts(), {"jobs": 1, "tasks": 0, "job_requirements": 0, "materials": 0, "quota_usage": 0})
            self.assertEqual(client.post("/api/jobs", json={**self.BODY, "review_only": True}).status_code, 403)
            model.assert_not_called()

    def test_explicit_prepare_is_the_consent_boundary(self):
        client, headers = self.browser()
        job = self.review(client, headers).json()["job_id"]
        response = client.post(f"/api/jobs/{job}/requirements/find", json={"expected_version": 0}, headers=headers)
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["code"], "consent_required")
        self.assertEqual(self.counts()["job_requirements"], 0)
        self.assertEqual(len(self.chat.sent), 0)

    def test_review_then_generate_reuses_profile_and_old_paste_identity(self):
        client, headers = self.ready_user()
        self.chat.requirement_lines = {"2、熟悉Python，了解SQL"}
        self.chat.plan = {"sections": ["education", "skills", "experience"], "entries": [], "reasons": []}
        sent = len(self.chat.sent)
        saved = self.review(client, headers)
        job = saved.json()["job_id"]
        view = client.get("/api/jobs/" + job, headers=headers).json()
        self.assertGreater(view["review"]["confirmed_fact_count"], 0)
        self.assertEqual(len(self.chat.sent), sent)
        self.assertIsNone(view["cv"]["en"]["head"])
        response = client.post(f"/api/jobs/{job}/requirements/find", json={"expected_version": 0},
                               headers={**headers, "Idempotency-Key": "generate"})
        task = self.settle(client, headers, response)
        self.assertEqual(task["status"], "succeeded", task)
        self.assertGreater(len(self.chat.sent), sent)
        generated = client.get("/api/jobs/" + job, headers=headers).json()
        self.assertEqual(generated["cv"]["en"]["head"], "planned")
        # A legacy caller omitting review_only still finds the same content identity.
        old = client.post("/api/jobs", json=self.BODY, headers={**headers, "Idempotency-Key": "legacy"})
        self.assertEqual(old.json()["job_id"], job)
        self.assertEqual(len(client.get("/api/jobs", headers=headers).json()["jobs"]), 1)

    def test_url_and_listing_reads_are_zero_unit_and_keep_tenant_boundaries(self):
        a, ah = self.browser("alice")
        b, bh = self.browser("bob")
        a.post("/api/sources", json={"link": "https://job-boards.greenhouse.io/example"}, headers=ah)
        with tenant_store.transaction(self.database, write=True) as connection:
            connection.execute("DELETE FROM settings WHERE key IN ('user_daily_units', 'site_daily_units')")
        row = {"provider": "greenhouse", "board": "example", "job_id": "1", "review_only": True}
        before = self.counts()
        with patch.object(self.app.state.runner, "model", side_effect=AssertionError("No paid calls")) as model:
            forbidden = b.post("/api/listings/start", json=row, headers=bh)
            self.assertEqual(forbidden.status_code, 404)
            response = a.post("/api/listings/start", json=row, headers={**ah, "Idempotency-Key": "read-listing"})
            task = self.settle(a, ah, response)
            self.assertEqual((task["operation"], task["status"], self.units(task)), ("read_listing", "succeeded", 0))
            job = task["result"]["job_id"]
            foreign = b.post(f"/api/jobs/{job}/requirements/find", json={"expected_version": 0}, headers=bh)
            self.assertEqual(foreign.status_code, 404)
            # Same request key cannot change mode, even after the job has been saved.
            changed = a.post("/api/listings/start", json={**row, "review_only": False},
                             headers={**ah, "Idempotency-Key": "read-listing"})
            self.assertEqual(changed.status_code, 409, changed.text)
            url = {"url": "https://job-boards.greenhouse.io/example/jobs/1", "review_only": True}
            response = b.post("/api/jobs/from-url", json=url, headers={**bh, "Idempotency-Key": "read-url"})
            other = self.settle(b, bh, response)
            self.assertEqual((other["operation"], other["status"], self.units(other)), ("read_job_url", "succeeded", 0))
            self.assertNotEqual(other["result"]["job_id"], job)
            changed = b.post("/api/jobs/from-url", json={**url, "url": url["url"][:-1] + "2"},
                             headers={**bh, "Idempotency-Key": "read-url"})
            self.assertEqual(changed.status_code, 409, changed.text)
            changed_mode = b.post("/api/jobs/from-url", json={**url, "review_only": False},
                                  headers={**bh, "Idempotency-Key": "read-url"})
            self.assertEqual(changed_mode.status_code, 409, changed_mode.text)
            model.assert_not_called()
        after = self.counts()
        self.assertEqual(after["quota_usage"], before["quota_usage"])
        self.assertEqual((after["job_requirements"], after["materials"]), (0, 0))

    def test_read_tasks_respect_pause_and_sponsor_filter_reads_updated_text(self):
        client, headers = self.browser()
        self.boards.jobs[0]["text"] += "\nWe provide visa sponsorship."
        client.post("/api/sources", json={"link": "https://job-boards.greenhouse.io/example"}, headers=headers)
        ranked = client.get("/api/listings?sponsorship=supported", headers=headers).json()
        self.assertEqual(ranked["total"], 1)
        self.assertEqual(ranked["listings"][0]["sponsorship"]["status"], "supported")
        self.assertEqual(client.get("/api/listings?sponsorship=unknown", headers=headers).json()["total"], 1)
        self.assertEqual(client.get("/api/listings?sponsorship=bogus", headers=headers).status_code, 400)
        tenant_store.store_public_board(self.database, "greenhouse", "example", "Example",
                                        [posting("1", "Python Intern", "Python. We cannot provide visa sponsorship.")])
        refreshed = client.get("/api/listings?sponsorship=not_supported", headers=headers).json()
        self.assertEqual(refreshed["total"], 1)
        tenant_store.set_setting(self.database, "tasks_enabled", "0")
        response = client.post("/api/jobs/from-url",
                               json={"url": "https://job-boards.greenhouse.io/example/jobs/1", "review_only": True},
                               headers=headers)
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["code"], "tasks_paused")


class FreeTaskTests(RunnerCase):
    def test_zero_unit_task_cannot_call_model_even_if_a_handler_tries(self):
        user = self.users[0]
        task = user.submit_task("read_job_url", key="free", request={}, units=0, heavy="model")
        claimed = tenant_store.claim_tasks(self.database, total=1, pdf=1, deadline=timedelta(minutes=1))[0]
        model = Mock()
        context = TaskContext(self.database, user, task["task_id"], claimed["token"],
                              "read_job_url", {}, None, model)
        with self.assertRaises(tenant_store.StoreError):
            context.chat([], model="unused", effort="unused")
        model.assert_not_called()
