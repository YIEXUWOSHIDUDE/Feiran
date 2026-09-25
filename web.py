"""Local web page for the workbench: a thin FastAPI layer over the existing modules.

The server only listens on 127.0.0.1. It also rejects any Host other than localhost
(DNS rebinding) and any /api request without the token embedded in the page at start,
so other websites open in the browser cannot read facts or trigger DeepSeek calls.
"""

import argparse
import json
import re
import secrets
import tempfile
import threading
from pathlib import Path
from typing import Callable, Iterable

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from cv import (
    CVError,
    approve_draft,
    build_draft,
    export_pdf,
    is_final_approval,
    print_with_chrome,
    rejected_lines,
    render_html,
    rewritten_lines,
    tailor_draft,
)
from deepseek_client import chat_json
from facts import DEFAULT_DATABASE, FactStoreError, confirm_facts, list_facts, parse_fact_refs
from job_search import (
    SearchError,
    fetch_board,
    fetch_selected,
    parse_board_link,
    prepare_pasted_jd,
    prepare_review_input,
)
from listings import (
    ListingsError,
    add_source,
    get_listing,
    initialize,
    list_sources,
    load_starter,
    ranked_listings,
    refresh_source,
    remove_source,
)
from matching import MatchingError, apply_match_decisions, propose_matches
from requirement_flow import (
    RequirementError,
    add_manual_requirements,
    apply_requirement_decisions,
    propose_requirements,
)
from review import build_report
from workspace import DEFAULT_ROOT, Workspace, WorkspaceError


WEB_DIR = Path(__file__).parent / "web"
DEFAULT_PROFILE = Path(".local/cv-profile.json")
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}
CANDIDATE_FIELDS = ("id", "text", "section", "status", "extraction_method")
CV_LANGUAGES = ("en", "zh")
QUERY_TOKEN_PATHS = ("/preview/", "/download/")


class ConfirmRequest(BaseModel):
    refs: list[str]


class NewJobRequest(BaseModel):
    title: str
    text: str
    company: str | None = None
    url: str | None = None
    location: str | None = None


class AddRequirementRequest(BaseModel):
    text: str


class RequirementDecisionRequest(BaseModel):
    confirm: list[str] = []
    exclude: list[str] = []


FALLBACK_PATH = re.compile(r"sections\[(\d+)\](?:\.entries\[(\d+)\])?\.(title|subtitle|location|dates)")
FIELD_NAMES = {"title": "name", "subtitle": "role or degree", "location": "location", "dates": "dates"}


def _describe_fallbacks(draft: dict) -> list[str]:
    """Turn profile paths such as sections[1].entries[0].title into words the user can act on."""
    labels = []
    for path in draft["language_fallbacks"]:
        match = FALLBACK_PATH.fullmatch(path)
        if path == "name":
            labels.append(f"Name: {draft['header']['name']}")
        elif path == "contact.location":
            labels.append(f"Location: {draft['header']['details'][0]['text']}")
        elif match and match[2] is None:
            section = draft["sections"][int(match[1])]
            labels.append(f"{section['kind'].capitalize()} heading: {section['title']}")
        elif match:
            section = draft["sections"][int(match[1])]
            value = section["entries"][int(match[2])][match[3]]
            labels.append(f"{section['kind'].capitalize()} {FIELD_NAMES[match[3]]}: {value}")
        else:
            labels.append(path)
    return labels


class AddSourceRequest(BaseModel):
    link: str


class StartListingRequest(BaseModel):
    provider: str
    board: str
    job_id: str


class MatchDecisionRequest(BaseModel):
    links: dict[str, str] = {}
    no_match: list[str] = []


def _matching_view(matches: dict, linked: dict | None) -> dict:
    """Each confirmed requirement with the facts offered for it and any saved decision."""
    texts = {item["id"]: item["text"] for item in matches["selected_requirements"]}
    decisions = (linked or {}).get("match_decisions", {})
    candidates: dict[str, list] = {}
    for item in matches["match_candidates"]:
        candidates.setdefault(item["requirement_id"], []).append({
            key: item[key] for key in
            ("fact_id", "fact_version", "fact_text", "fact_type", "fact_tags", "retrieval_basis")
        })
    return {"requirements": [
        {
            "requirement_id": summary["requirement_id"],
            "text": texts[summary["requirement_id"]],
            "candidates": candidates.get(summary["requirement_id"], []),
            "decision": decisions.get(summary["requirement_id"]),
        }
        for summary in matches["fact_matching"]["requirements"]
    ]}


def create_app(
    facts_db: Path = DEFAULT_DATABASE,
    jobs_root: Path = DEFAULT_ROOT,
    token: str | None = None,
    profile_path: Path = DEFAULT_PROFILE,
    chat: Callable[..., dict] = chat_json,
    printer: Callable[[Path, Path], None] = print_with_chrome,
    listings_db: Path | None = None,
    starter: Iterable[dict[str, str]] | None = None,
    boards: Callable[..., list[dict]] = fetch_board,
    selected_posting: Callable[..., dict] = fetch_selected,
) -> FastAPI:
    token = token or secrets.token_urlsafe(32)
    workspace = Workspace(jobs_root)
    # Next to the fact store by default, so tests with a temporary fact store stay temporary too.
    listings_db = Path(listings_db or Path(facts_db).parent / "listings.db")
    initialize(listings_db, load_starter() if starter is None else starter)
    start_lock = threading.Lock()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        host = (request.headers.get("host") or "").rsplit(":", 1)[0]
        if host not in ALLOWED_HOSTS:
            return JSONResponse({"error": "只接受本机访问"}, status_code=403)
        path = request.url.path
        # Frames and download links cannot send headers, so those read-only paths
        # carry the same per-start token in the query string instead.
        if path.startswith("/api/"):
            supplied = request.headers.get("x-workbench-token", "")
        elif path.startswith(QUERY_TOKEN_PATHS):
            supplied = request.query_params.get("token", "")
        else:
            supplied = token
        if not secrets.compare_digest(supplied, token):
            return JSONResponse({"error": "缺少或错误的访问令牌"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(FactStoreError)
    @app.exception_handler(WorkspaceError)
    @app.exception_handler(SearchError)
    @app.exception_handler(RequirementError)
    @app.exception_handler(MatchingError)
    @app.exception_handler(CVError)
    @app.exception_handler(ListingsError)
    @app.exception_handler(ValueError)
    async def domain_error(_request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=400)

    def require(job_id: str, step: str) -> dict:
        data = workspace.read(job_id, step)
        if data is None:
            raise WorkspaceError(f"请先完成上一步（缺少 {step}）")
        return data

    def job_view(job_id: str) -> dict:
        """What the page needs for one job: the JD and the current file of each step."""
        steps = workspace.state(job_id)
        jd = {key: value for key, value in require(job_id, "input")["jd"].items() if key != "raw_content"}
        view: dict = {"job_id": job_id, "steps": steps, "jd": jd}
        decided = workspace.read(job_id, "decided")
        requirements = decided or workspace.read(job_id, "candidates")
        if requirements:
            view["candidates"] = [
                {field: item.get(field) for field in CANDIDATE_FIELDS}
                for item in requirements["requirement_candidates"]
            ]
        if decided:
            view["selected_requirements"] = decided["selected_requirements"]
        matches = workspace.read(job_id, "matches")
        linked = workspace.read(job_id, "linked")
        if matches:
            view["matching"] = _matching_view(matches, linked)
        if linked:
            view["report"] = build_report(linked)
        view["cv"] = {language: cv_view(job_id, language) for language in CV_LANGUAGES}
        return view

    def check_language(language: str) -> str:
        if language not in CV_LANGUAGES:
            raise WorkspaceError(f"简历语言必须是：{', '.join(CV_LANGUAGES)}")
        return language

    def cv_head(job_id: str, language: str) -> tuple[str | None, dict | None]:
        """The newest CV file for a language; later steps always derive from earlier ones."""
        for step in ("approved", "tailored", "draft"):
            data = workspace.read(job_id, f"cv-{step}-{check_language(language)}")
            if data is not None:
                return step, data
        return None, None

    def cv_view(job_id: str, language: str) -> dict:
        head_step, head = cv_head(job_id, language)
        view: dict = {
            "head": head_step,
            "final_pdf": workspace.path(job_id, f"cv-final-{language}").exists(),
        }
        draft = workspace.read(job_id, f"cv-draft-{language}")
        tailored = workspace.read(job_id, f"cv-tailored-{language}")
        if draft:
            view["language_fallbacks"] = _describe_fallbacks(draft)
        if tailored:
            tailoring = tailored["tailoring"]
            view["tailoring"] = {key: tailoring.get(key) for key in ("model", "accepted", "rejected", "usage")}
            view["rewrites"] = rewritten_lines(tailored)
            view["rejected"] = rejected_lines(tailored)
        if head_step == "approved":
            view["approved_at"] = head["approval"]["approved_at"]
        return view

    def load_profile() -> dict:
        if not Path(profile_path).exists():
            raise CVError(f"缺少简历 profile：{profile_path}")
        return json.loads(Path(profile_path).read_text(encoding="utf-8"))

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (WEB_DIR / "index.html").read_text(encoding="utf-8").replace("__WORKBENCH_TOKEN__", token)

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    @app.get("/api/facts")
    def facts() -> dict:
        if not Path(facts_db).exists():
            return {"facts": []}
        return {"facts": list_facts(facts_db)}

    @app.post("/api/facts/confirm")
    def confirm(request: ConfirmRequest) -> dict:
        confirmed = confirm_facts(facts_db, parse_fact_refs(request.refs))
        return {"changed_count": sum(changed for _, changed in confirmed)}

    def started_jobs() -> dict[str, str]:
        """Official posting link -> the newest job already made from it."""
        started: dict[str, str] = {}
        for summary in workspace.jobs():
            if summary["source"]:
                started.setdefault(summary["source"], summary["job_id"])
        return started

    @app.get("/api/sources")
    def sources() -> dict:
        return {"sources": list_sources(listings_db)}

    @app.post("/api/sources")
    def follow_source(request: AddSourceRequest) -> dict:
        provider, board = parse_board_link(request.link)
        added = add_source(listings_db, provider, board, fetch=boards)
        return {"added": added, "sources": list_sources(listings_db)}

    @app.delete("/api/sources/{provider}/{board}")
    def unfollow_source(provider: str, board: str) -> dict:
        remove_source(listings_db, provider, board)
        return {"sources": list_sources(listings_db)}

    @app.post("/api/sources/{provider}/{board}/refresh")
    def refresh(provider: str, board: str) -> dict:
        return refresh_source(listings_db, provider, board, fetch=boards)

    @app.get("/api/listings")
    def listings(
        title: str = "", location: str = "", limit: int = 50, offset: int = 0, hide_senior: bool = False
    ) -> dict:
        ranked = ranked_listings(listings_db, facts_db, title, location, limit, offset, hide_senior)
        started = started_jobs()
        for group in ranked["listings"]:
            group["started_job"] = next(
                (started[item["source"]] for item in group["postings"] if item["source"] in started), None
            )
        return ranked

    @app.post("/api/listings/start")
    def start_listing(request: StartListingRequest) -> dict:
        # One job per posting: a second click, even a concurrent one, opens the first job.
        with start_lock:
            listing = get_listing(listings_db, request.provider, request.board, request.job_id)
            existing = started_jobs().get(listing["source"])
            if existing:
                return {"job_id": existing, "existing": True}
            selected = selected_posting(request.board, request.job_id, request.provider)
            selected["jd"]["company"] = selected["jd"].get("company") or listing["company"]
            review_input = prepare_review_input(selected)
            candidates = propose_requirements(review_input)
            job_id = workspace.create_job(review_input)
            workspace.write(job_id, "candidates", candidates)
            return {"job_id": job_id, "existing": False}

    @app.get("/api/jobs")
    def jobs() -> dict:
        return {"jobs": workspace.jobs()}

    @app.post("/api/jobs")
    def new_job(request: NewJobRequest) -> dict:
        selected = prepare_pasted_jd(request.text, request.title, request.company, request.url, request.location)
        review_input = prepare_review_input(selected)
        candidates = propose_requirements(review_input)
        job_id = workspace.create_job(review_input)
        workspace.write(job_id, "candidates", candidates)
        return {"job_id": job_id}

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str) -> dict:
        return job_view(job_id)

    @app.post("/api/jobs/{job_id}/requirements/add")
    def add_requirement(job_id: str, request: AddRequirementRequest) -> dict:
        added = add_manual_requirements(require(job_id, "candidates"), [request.text])
        workspace.write(job_id, "candidates", added)
        return job_view(job_id)

    @app.post("/api/jobs/{job_id}/requirements/decide")
    def decide_requirements(job_id: str, request: RequirementDecisionRequest) -> dict:
        decided = apply_requirement_decisions(
            require(job_id, "candidates"), set(request.confirm), set(request.exclude)
        )
        workspace.write(job_id, "decided", decided)
        return job_view(job_id)

    @app.post("/api/jobs/{job_id}/matches/propose")
    def propose_job_matches(job_id: str) -> dict:
        workspace.write(job_id, "matches", propose_matches(require(job_id, "decided"), facts_db))
        return job_view(job_id)

    @app.post("/api/jobs/{job_id}/matches/decide")
    def decide_job_matches(job_id: str, request: MatchDecisionRequest) -> dict:
        linked = apply_match_decisions(
            require(job_id, "matches"), facts_db, request.links, set(request.no_match)
        )
        workspace.write(job_id, "linked", linked)
        return job_view(job_id)

    @app.post("/api/jobs/{job_id}/cv/{language}/draft")
    def cv_draft(job_id: str, language: str) -> dict:
        draft = build_draft(
            load_profile(), facts_db, check_language(language), job=workspace.read(job_id, "linked")
        )
        workspace.write(job_id, f"cv-draft-{language}", draft)
        return job_view(job_id)

    @app.post("/api/jobs/{job_id}/cv/{language}/tailor")
    def cv_tailor(job_id: str, language: str) -> dict:
        draft = require(job_id, f"cv-draft-{check_language(language)}")
        tailored = tailor_draft(draft, facts_db, job=workspace.read(job_id, "linked"), chat=chat)
        workspace.write(job_id, f"cv-tailored-{language}", tailored)
        return job_view(job_id)

    @app.post("/api/jobs/{job_id}/cv/{language}/approve")
    def cv_approve(job_id: str, language: str) -> dict:
        head_step, head = cv_head(job_id, language)
        if head is None:
            raise WorkspaceError("请先生成简历草稿")
        if head_step == "approved":
            raise CVError("这份简历已经批准；如需修改请重新生成草稿")
        workspace.write(job_id, f"cv-approved-{language}", approve_draft(head, facts_db))
        return job_view(job_id)

    @app.post("/api/jobs/{job_id}/cv/{language}/export")
    def cv_export(job_id: str, language: str) -> dict:
        approved = require(job_id, f"cv-approved-{check_language(language)}")
        with tempfile.TemporaryDirectory(prefix="cv-web-") as directory:
            output = Path(directory) / "cv.pdf"
            export_pdf(approved, facts_db, output, printer=printer)
            data = output.read_bytes()
        workspace.write_bytes(job_id, f"cv-final-{language}", data)
        return job_view(job_id)

    @app.get("/preview/{job_id}/{language}", response_class=HTMLResponse)
    def cv_preview(job_id: str, language: str) -> str:
        head_step, head = cv_head(job_id, language)
        if head is None:
            raise WorkspaceError("请先生成简历草稿")
        try:
            final = head_step == "approved" and is_final_approval(head)
        except CVError:
            final = False
        return render_html(head, final=final)

    @app.get("/download/{job_id}/{language}.pdf")
    def cv_download(job_id: str, language: str) -> FileResponse:
        path = workspace.path(job_id, f"cv-final-{check_language(language)}")
        if not path.exists():
            raise WorkspaceError("还没有已批准的最终 PDF")
        return FileResponse(path, media_type="application/pdf", filename=f"CV-{language.upper()}.pdf")

    app.state.workspace = workspace
    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="在本机浏览器中使用岗位匹配与简历工作台")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--facts-db", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--jobs", type=Path, default=DEFAULT_ROOT, help="每个岗位一个文件夹")
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE, help="简历 profile JSON")
    args = parser.parse_args(argv)
    import uvicorn

    print(f"打开 http://127.0.0.1:{args.port}/ （只在本机可用；按 Ctrl+C 停止）")
    app = create_app(args.facts_db, args.jobs, profile_path=args.profile)
    uvicorn.run(app, host="127.0.0.1", port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
