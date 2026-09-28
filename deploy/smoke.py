"""Run the whole workflow once inside the container: synthetic data, a scripted stand-in for
DeepSeek, and real PDFs printed by the container's own Chromium.

    python deploy/smoke.py create --data /data --out /data/smoke   # build it all and check the PDFs
    python deploy/smoke.py verify --data /data                     # later, in a new container

`create` goes from an uploaded CV to an approved, exported PDF: upload a CV, confirm its facts,
create a job (its CV is drafted, reworded and adjusted by itself), check what the CV shows for
each requirement, approve that exact CV and export it. It also prints a Chinese CV from the
synthetic examples. Each PDF is checked for its text, its fonts and its page count. `verify`
checks that what `create` left is all still there and still consistent.
"""

import argparse
import json
import re
import sys
import tempfile
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from pypdf import PdfReader  # noqa: E402

from cv import build_draft, export_pdf, is_final_approval  # noqa: E402
from cv_import import STRUCTURE_RULES  # noqa: E402
from cv_plan import PLAN_RULES  # noqa: E402
from facts import confirm_facts, import_facts  # noqa: E402
from gaps import EVIDENCE_RULES, SUGGEST_RULES  # noqa: E402
from requirement_flow import FIND_RULES  # noqa: E402
from test_cv_import import minimal_pdf  # noqa: E402
from web import create_app  # noqa: E402

TOKEN = "smoke-test-token"
HEADERS = {"X-Workbench-Token": TOKEN}
EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
LINUX = sys.platform.startswith("linux")
JD = "Requirements:\n- Experience building REST APIs\n- Hands-on Docker"
CV_LINES = [
    (72, 740, "ALEX EXAMPLE"), (72, 726, "Los Angeles, CA | 000-000-0000 | alex@example.com"),
    (72, 700, "EXPERIENCE"), (72, 686, "Example Corp"), (430, 686, "Chengdu, China"),
    (72, 672, "Software Intern"), (430, 672, "Jun 2025 - Aug 2025"),
    (72, 658, "- Built REST APIs for an internal tool."), (72, 630, "SKILLS"), (72, 616, "Languages: Python, Java"),
]
STRUCTURE = {"sections": [
    {"kind": "experience", "heading": 3, "entries": [
        {"title": [4, 1], "location": [4, 2], "subtitle": [5, 1], "dates": [5, 2],
         "facts": [{"lines": [6], "tags": ["REST APIs"]}]}]},
    {"kind": "skills", "heading": 7, "entries": [{"facts": [{"lines": [8], "tags": ["Python", "Java"]}]}]},
]}
CONTACT = {"name": "Alex Example", "location": "Los Angeles, CA", "phone": "000-000-0000",
           "email": "alex@example.com", "links": [{"label": "github.com/alex-example", "url": "github.com/alex-example"}]}


class ScriptedDeepSeek:
    """Answers each kind of request the way DeepSeek would, without the network."""

    def __call__(self, messages, model, effort):
        system, request = messages[0]["content"], json.loads(messages[-1]["content"])
        if system == STRUCTURE_RULES:
            content = STRUCTURE
        elif system == FIND_RULES:
            content = {"requirements": [{"line": line["n"], "kind": "required"}
                                        for line in request["lines"] if line["text"].startswith("- ")]}
        elif system.startswith("You rewrite resume lines"):
            content = {"lines": [{"fact_id": line["fact_id"], "text": line["text"]} for line in request["lines"]]}
        elif system == PLAN_RULES:
            content = {"sections": [section["section"] for section in request["resume"]], "entries": [], "reasons": []}
        elif system == EVIDENCE_RULES:
            lines = [line for entry in request["resume"] for line in [entry.get("role"), *entry["lines"]] if line]
            content = {"requirements": [
                {"id": item["id"], "verdict": "supported",
                 "sets": [[line["id"]] for line in lines if "REST APIs" in line["text"]]}
                if "REST APIs" in item["text"] else {"id": item["id"], "verdict": "none"}
                for item in request["requirements"]]}
        elif system == SUGGEST_RULES:
            content = {"suggestions": []}
        else:
            raise AssertionError(f"unexpected request: {system[:60]}")
        return {"model": "scripted", "content": content, "usage": {}}


def client_for(data: Path) -> TestClient:
    app = create_app(facts_db=data / "workbench.db", jobs_root=data / "jobs", token=TOKEN,
                     profile_path=data / "cv-profile.json", chat=ScriptedDeepSeek(), starter=[])
    return TestClient(app, base_url="http://127.0.0.1:8765")


def ok(response, what: str) -> dict:
    if response.status_code != 200:
        raise SystemExit(f"FAILED {what}: HTTP {response.status_code} {response.text[:300]}")
    return response.json()


def pdf_facts(path: Path) -> dict:
    """What a PDF holds: its pages, its text with spacing removed, and the fonts it embeds. Text
    comes out of a PDF with some CJK characters as look-alike radicals (⼈ for 人); NFKC maps
    them back."""
    reader = PdfReader(str(path))
    text = unicodedata.normalize("NFKC", "".join(page.extract_text() or "" for page in reader.pages))
    fonts = sorted({str(font.get_object().get("/BaseFont")) for page in reader.pages
                    for font in (page.get("/Resources", {}).get("/Font", {}) or {}).values()})
    return {"pages": len(reader.pages), "text": re.sub(r"\s+", "", text), "fonts": fonts}


def check(condition: bool, what: str) -> None:
    print(("ok   " if condition else "FAIL ") + what, flush=True)
    if not condition:
        raise SystemExit(1)


def create(data: Path, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    client = client_for(data)
    check(client.get("/healthz").json() == {"status": "ok"}, "health endpoint answers ok")
    upload = ok(client.post("/api/cv/upload", content=minimal_pdf(CV_LINES),
                            headers={**HEADERS, "Content-Type": "application/pdf"}), "upload the CV")
    ok(client.post(f"/api/cv/uploads/{upload['upload_id']}/save", json=CONTACT, headers=HEADERS), "save the CV")
    facts = ok(client.get("/api/facts", headers=HEADERS), "list facts")["facts"]
    ok(client.post("/api/facts/confirm", json={"refs": [f"{fact['id']}@{fact['version']}" for fact in facts]},
                   headers=HEADERS), "confirm the facts")
    job = ok(client.post("/api/jobs", json={"title": "Backend Intern", "company": "Example Co", "text": JD},
                         headers=HEADERS), "create a job")["job_id"]
    view = ok(client.get(f"/api/jobs/{job}", headers=HEADERS), "open the job")
    check(len(view["selected_requirements"]) == 2, "two requirements found")
    check(view["cv"]["en"]["head"] == "planned", "CV drafted, reworded and adjusted by itself")
    gaps = ok(client.post(f"/api/jobs/{job}/gaps", headers=HEADERS), "check the requirements")["gaps"]
    statuses = {item["text"]: item["status"] for item in gaps["requirements"]}
    check(statuses == {"Experience building REST APIs": "shown", "Hands-on Docker": "none"},
          f"what the CV shows for each requirement: {statuses}")
    ok(client.post(f"/api/jobs/{job}/cv/en/approve", headers=HEADERS), "approve the CV")
    view = ok(client.post(f"/api/jobs/{job}/cv/en/export", headers=HEADERS), "export the PDF")
    check(view["cv"]["en"]["final_pdf"], "final PDF created")
    download = client.get(f"/download/{job}/en.pdf?token={TOKEN}")
    check(download.status_code == 200 and download.content.startswith(b"%PDF"), "final PDF downloads")
    english = out / "cv-en-final.pdf"
    english.write_bytes(download.content)
    en = pdf_facts(english)
    check(en["pages"] == 1, f"English CV fits one page ({en['pages']})")
    for line in ("Built REST APIs for an internal tool.", "Languages: Python, Java", "Example Corp"):
        check(re.sub(r"\s+", "", line).casefold() in en["text"].casefold(), f"English CV shows {line!r}")
    check("DRAFT" not in en["text"], "approved CV has no watermark")
    if LINUX:  # the fonts the image installs; a Mac prints with its own
        check(any("Liberation" in font for font in en["fonts"]), f"English CV uses Liberation Sans: {en['fonts']}")

    # A Chinese CV from the synthetic examples, printed the same way.
    with tempfile.TemporaryDirectory() as scratch:
        database = Path(scratch) / "facts.db"
        items = json.loads((EXAMPLES / "synthetic_cv_facts.json").read_text(encoding="utf-8"))["facts"]
        import_facts(database, items)
        confirm_facts(database, [(item["id"], 1) for item in items])
        profile = json.loads((EXAMPLES / "synthetic_cv_profile.json").read_text(encoding="utf-8"))
        chinese = out / "cv-zh-draft.pdf"
        chinese.unlink(missing_ok=True)
        export_pdf(build_draft(profile, database, "zh"), database, chinese)
    zh = pdf_facts(chinese)
    check(1 <= zh["pages"] <= 2, f"Chinese CV is one or two pages ({zh['pages']})")
    for word in ("示例候选人", "教育背景", "草稿"):
        check(word in zh["text"], f"Chinese CV shows {word}")
    if LINUX:
        check(any("NotoSansCJK" in font for font in zh["fonts"]), f"Chinese CV uses Noto Sans CJK: {zh['fonts']}")
    (out / "summary.json").write_text(json.dumps({"job": job, "statuses": statuses, "english": {**en, "text": None},
                                                  "chinese": {**zh, "text": None}}, ensure_ascii=False, indent=1))


def verify(data: Path) -> None:
    client = client_for(data)
    facts = ok(client.get("/api/facts", headers=HEADERS), "list facts")["facts"]
    check(len(facts) == 2 and all(fact["status"] == "confirmed" for fact in facts), "both facts still confirmed")
    profile = json.loads((data / "cv-profile.json").read_text(encoding="utf-8"))
    check(profile["name"] == "Alex Example", "CV profile still there")
    jobs = ok(client.get("/api/jobs", headers=HEADERS), "list jobs")["jobs"]
    check(len(jobs) == 1, "the job is still there")
    view = ok(client.get(f"/api/jobs/{jobs[0]['job_id']}", headers=HEADERS), "open the job")
    check(view["cv"]["en"]["head"] == "approved" and view["cv"]["en"]["final_pdf"], "approved CV and final PDF still there")
    approved = json.loads((data / "jobs" / jobs[0]["job_id"] / "cv-approved-en.json").read_text(encoding="utf-8"))
    check(is_final_approval(approved), "the approval still matches the CV's content")
    check(not view["gaps"].get("outdated") and not view["gaps"]["stale"], "the requirement check is still current")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=("create", "verify"))
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path(tempfile.gettempdir()) / "smoke")
    args = parser.parse_args()
    if args.action == "create":
        create(args.data, args.out)
    else:
        verify(args.data)
    print(f"{args.action}: all checks passed", flush=True)


if __name__ == "__main__":
    main()
