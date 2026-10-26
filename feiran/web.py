"""Local web page for the workbench: a thin FastAPI layer over the existing modules.

The server only listens on 127.0.0.1. It also rejects any Host other than localhost
(DNS rebinding) and any /api request without the token embedded in the page at start,
so other websites open in the browser cannot read facts or trigger DeepSeek calls.
"""

import argparse
from functools import lru_cache
import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator, Mapping, NamedTuple

import anyio
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.routing import Match
from pydantic import BaseModel

from feiran import batch_zip
from feiran.download_name import pdf_disposition, pdf_filename
from feiran.cv import (
    CVError,
    _profile_hash,
    verify_draft,
    approve_draft,
    build_draft,
    content_fingerprint,
    export_pdf,
    is_final_approval,
    print_with_chrome,
    profile_languages,
    rejected_lines,
    render_html,
    rendered_pages,
    PAGES_OVER,
    PAGES_UNCHECKED,
    rewritten_lines,
    tailor_draft,
)
from feiran.cv_fit import fit_outcome
from feiran.cv_import import MAX_PDF_BYTES, CVImportError, build_profile, read_pdf, structure_cv
from feiran.cv_status import (
    CANDIDATE_FIELDS,
    REASONS,
    STAGES,
    STAGE_FAILED,
    adjusted as _adjusted,
    describe_fallbacks as _describe_fallbacks,
    explained as _explained,
    job_language as _job_language,
    reworded as _reworded,
    stage_entry as _stage,
    stage_failure as _stage_failure,
)
from feiran.cv_preparation import prepare_steps
from feiran.cv_edit import (
    StaleEdit,
    adopt_for,
    already_saved,
    carry_over,
    change_line,
    edits_view,
    lines_view,
    mark_saved,
    save_plan,
    save_target,
    set_edit,
    shown_places,
    with_adoptions,
    without_edits,
)
from feiran.cv_plan import plan_draft, set_change
from feiran.deepseek_client import DeepSeekError, chat_json
from feiran.facts import (DEFAULT_DATABASE, FactStoreError, confirm_facts, fact_source, import_facts, list_facts,
                   parse_fact_refs, revise_fact)
from feiran.gaps import _gap, coverage, decline_gap, find_gaps, suggest_gap, unusable
from feiran.job_search import (
    SearchError,
    fetch_board,
    fetch_selected,
    parse_board_link,
    parse_posting_link,
    prepare_pasted_jd,
    prepare_review_input,
)
from feiran.job_review import build_job_review
from feiran.jd_diagnostics import jd_diagnostics
from feiran.job_url import fetch_job_url, normalize_url, require_complete_jd
from feiran.listings import (
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
from feiran.matching import MatchingError, apply_match_decisions, first_candidates, propose_matches
from feiran.privacy import private_terms
from feiran.public_access import OwnerAccess
from feiran.requirement_flow import (
    RequirementError,
    add_manual_requirements,
    apply_requirement_decisions,
    propose_requirements,
)
from feiran.review import build_report
from feiran import run_log
from feiran.sponsorship import assess_sponsorship
from feiran.workspace import (
    DEFAULT_ROOT,
    JOB_ID,
    DataFormatError,
    Workspace,
    WorkspaceError,
    claim_data_format,
    make_folder,
    remove_durably,
    remove_leftovers,
    write_atomically,
)


WEB_DIR = Path(__file__).resolve().parent.parent / "web"
DEFAULT_PROFILE = Path(".local/cv-profile.json")
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}
# Written once into the data volume when it is set up. With --require-data the app starts only
# on a folder holding it, so a volume that failed to mount never becomes an empty new workspace.
DATA_MARKER = ".workbench-data"
access_log = logging.getLogger("workbench.access")
stage_log = logging.getLogger("workbench.stages")
CV_LANGUAGES = ("en", "zh")
QUERY_TOKEN_PATHS = ("/preview/", "/download/")
READ_ONLY_METHODS = ("GET", "HEAD", "OPTIONS")
# HTTP's own methods: a request's method is logged only if it is one of these.
HTTP_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "CONNECT", "TRACE"})
# Changes to their own store only, so they neither wait for nor hold up the others: the followed
# companies and their postings (a SQLite database of their own, with its own transactions) and a
# new CV upload (its own new file; saving it, which changes facts and the profile, does wait).
OWN_STORE = re.compile(r"/api/sources(/.*)?|/api/cv/upload")
# Reads that must see the facts, the CV profiles and the jobs at one moment: they wait for a
# change being made and hold the next one out until they are done, as a change does.
SNAPSHOT_READS = re.compile(r"/download/batch\.zip")
# How long a change waits for the one before it (DeepSeek can take a minute) before the page is
# told to try again.
CHANGE_WAIT_SECONDS = 90
BUSY = "Another change is still being made (DeepSeek can take a minute). Try again in a moment."
PAGE_OUT_OF_DATE = "This page is out of date; reload it"
# Changes that span the fact store, the CV profile and a job's files: saving an uploaded CV, and
# saving a line of a job's CV to the profile ("add_line": how earlier releases adopted a line, kept
# so a notice they left is still shown). Each has a file in unfinished/ while it runs, which stays
# as the notice if it does not finish.
OPERATIONS = ("save_cv", "add_line", "save_line")
UNFINISHED = "unfinished"
NOTICE_ID = re.compile(r"[0-9a-f]{16}")
UPLOAD_KEEP_SECONDS = 24 * 3600


class StalePage(Exception):
    """The page acted on a check it no longer shows (another tab switched the CV language or
    changed the requirements): answered 409, before anything is written."""


class ConfirmRequest(BaseModel):
    refs: list[str]


class NewJobRequest(BaseModel):
    title: str
    text: str
    company: str | None = None
    url: str | None = None
    location: str | None = None


class JobURLRequest(BaseModel):
    url: str


class AddRequirementRequest(BaseModel):
    text: str


class RequirementDecisionRequest(BaseModel):
    confirm: list[str] = []
    exclude: list[str] = []


CV_CHANGED = "This CV changed after the page showed it (another tab or window changed it). Read it again below, then approve."
PREVIEW_CHANGED = ("<!doctype html><meta charset=\"utf-8\"><p>This CV changed after the page showed it. "
                   "Reload the page to read the current version.</p>")


class AddSourceRequest(BaseModel):
    link: str


class StartListingRequest(BaseModel):
    provider: str
    board: str
    job_id: str


class GapRequest(BaseModel):
    """The CV language of the check the page showed. The page sends it (with the check's version,
    which this app does not keep); a request without it acts on the job's check as it is."""
    language: str | None = None


class WriteLineRequest(GapRequest):
    place: str
    text: str


class ChangeRequest(BaseModel):
    change_id: str
    undone: bool = True


class LineEditRequest(BaseModel):
    """New words for one line of a job's CV, and the words the page showed for it."""
    text: str
    expected_text: str


class EditUndoRequest(BaseModel):
    undone: bool = True


class SaveLineRequest(BaseModel):
    expected_text: str


class DismissRequest(BaseModel):
    """The notice the user has read, by the id the page was given."""

    id: str


class ApproveRequest(BaseModel):
    """The fingerprint of the CV the page showed; only that CV can be approved. A CV whose page
    check found more than one page is approved only with ``allow_multiple_pages``."""

    expected_content_sha256: str
    allow_multiple_pages: bool = False


class ContactLink(BaseModel):
    label: str = ""
    url: str


class CVLanguageRequest(BaseModel):
    language: str


class EditFactRequest(BaseModel):
    expected_version: int
    text: str
    tags: list[str]


class SaveCVRequest(BaseModel):
    """The name and contact details as the user checked them; they never came from DeepSeek."""

    name: str
    location: str = ""
    phone: str = ""
    email: str = ""
    links: list[ContactLink] = []


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


def data_problem(folder: Path) -> str | None:
    """Why ``folder`` is not a usable data volume, or None."""
    if not folder.is_dir():
        return f"数据目录不存在：{folder}"
    if not (folder / DATA_MARKER).is_file():
        return f"数据目录中没有 {DATA_MARKER}：数据卷可能没有挂载"
    if not os.access(folder, os.W_OK):
        return f"数据目录不可写：{folder}"
    return None


class ServerSettings(NamedTuple):
    host: str
    port: int
    facts_db: Path
    jobs: Path
    profile: Path
    data: Path
    require_data: bool
    json_logs: bool = False


def server_settings(argv: list[str] | None, environ: Mapping[str, str]) -> ServerSettings:
    """Where to listen and keep data. One data folder (--data or WORKBENCH_DATA) holds everything;
    the older per-file options still win when given. Environment variables configure a container."""
    parser = argparse.ArgumentParser(description="在本机浏览器中使用岗位匹配与简历工作台")
    parser.add_argument("--host", default=environ.get("WORKBENCH_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(environ.get("WORKBENCH_PORT", "8765")))
    parser.add_argument("--data", type=Path, default=Path(environ.get("WORKBENCH_DATA", ".local")),
                        help="数据目录：事实库、简历 profile、岗位文件夹都在这里")
    parser.add_argument("--facts-db", type=Path)
    parser.add_argument("--jobs", type=Path, help="每个岗位一个文件夹")
    parser.add_argument("--profile", type=Path, help="简历 profile JSON")
    parser.add_argument("--require-data", action="store_true", default=environ.get("WORKBENCH_REQUIRE_DATA") == "1",
                        help=f"数据目录必须是含 {DATA_MARKER} 的数据卷，否则不启动")
    parser.add_argument("--json-logs", action="store_true", default=environ.get("WORKBENCH_LOG_FORMAT") == "json",
                        help="日志每行一个 JSON 对象（容器里用；错误只记类型和位置）")
    args = parser.parse_args(argv)
    return ServerSettings(
        host=args.host, port=args.port, data=args.data, require_data=args.require_data, json_logs=args.json_logs,
        facts_db=args.facts_db or args.data / DEFAULT_DATABASE.name,
        jobs=args.jobs or args.data / DEFAULT_ROOT.name,
        profile=args.profile or args.data / DEFAULT_PROFILE.name,
    )


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
    url_posting: Callable[[str], dict] = fetch_job_url,
) -> FastAPI:
    owner_access = OwnerAccess.from_environment()
    allowed_hosts = ALLOWED_HOSTS | ({owner_access.host} if owner_access else set())
    token = token or secrets.token_urlsafe(32)
    # Before anything reads or changes the data, wherever the options put it.
    claim_data_format(Path(profile_path).parent, also=(Path(facts_db).parent, Path(jobs_root).parent))
    workspace = Workspace(jobs_root)
    # A change the last run was making when it stopped is finished or undone before anything is served.
    for change in workspace.recover():
        run_log.event(logging.getLogger("workbench"), "change_undone", level=logging.WARNING, **change)
    uploads = Path(profile_path).parent / "cv-uploads"
    unfinished = Path(profile_path).parent / UNFINISHED
    make_folder(unfinished)
    for folder in (Path(profile_path).parent, Path(profile_path).parent / "profile-history", uploads, unfinished):
        remove_leftovers(folder)

    running: set[str] = set()  # this process's actions under way: theirs are not notices yet

    def read_notices() -> list[dict]:
        """Actions that did not finish, oldest first, each with the id the page dismisses it by.
        A record that cannot be read is shown as an unknown action and kept for a person to see."""
        notices = []
        for path in unfinished.glob("*.json"):
            if not NOTICE_ID.fullmatch(path.stem) or path.is_symlink() or path.stem in running:
                continue
            try:
                notice = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                continue  # it has just finished
            except (OSError, ValueError):
                notice = None
            if not isinstance(notice, dict) or notice.get("kind") not in OPERATIONS:
                notice = {"kind": "unknown"}
            notices.append({**notice, "id": path.stem})
        return sorted(notices, key=lambda notice: str(notice.get("started_at") or ""))

    @contextmanager
    def operation(kind: str, job_id: str | None = None, key: str | None = None, **shown: str | None) -> Iterator[None]:
        """A change that spans the fact store, the CV profile and a job's files, which no single
        journal covers. It has its own file in unfinished/, named after exactly this action (its
        kind, job and key: the uploaded PDF's hash, or the requirement and the line), written
        before it starts and removed once it is done. If the workbench stops, or a store fails
        partway, the file stays and is the notice the page shows, with what is ``shown`` in it.
        So no other action, not even another line for the same requirement, settles or replaces
        it; only the same action done again, or the user, does. A refusal comes before anything
        is written and leaves things as they were. Nothing is replayed: repeating is safe, since
        a saved CV's lines are reused and a line is never added twice."""
        name = hashlib.sha256(json.dumps([kind, job_id, key]).encode("utf-8")).hexdigest()[:16]
        path = unfinished / f"{name}.json"
        earlier = path.exists()  # the same action stopped before: its notice stays until this one is done
        running.add(name)
        try:
            if not earlier:
                write_atomically(path, json.dumps({"kind": kind, "job_id": job_id, "key": key, **shown,
                                                   "started_at": datetime.now(timezone.utc).isoformat()},
                                                  ensure_ascii=False).encode("utf-8"))
            try:
                yield
            except (OSError, sqlite3.Error, FactStoreError):
                raise  # part of the action may be done: the file stays as the notice
            except Exception:
                if not earlier:
                    remove_durably(path)
                raise
            remove_durably(path)
        finally:
            running.discard(name)

    # Next to the fact store by default, so tests with a temporary fact store stay temporary too.
    listings_db = Path(listings_db or Path(facts_db).parent / "listings.db")
    initialize(listings_db, load_starter() if starter is None else starter)
    changes = asyncio.Lock()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.waiting_changes = 0

    # Declared first, so it runs inside the host and token check below.
    @app.middleware("http")
    async def one_change_at_a_time(request: Request, call_next):
        """Requests that change the facts, the CV profile or a job's files run one at a time, so
        two tabs or a double click never write the same files at the same moment. Reading never
        waits here, except a batch download: it checks many CVs against the facts and profiles,
        which a change could otherwise edit between two of them (one job's CV checked before the
        edit, the next after), and so serve a mix that never existed."""
        if ((request.method in READ_ONLY_METHODS and not SNAPSHOT_READS.fullmatch(request.url.path))
                or OWN_STORE.fullmatch(request.url.path)):
            return await call_next(request)
        app.state.waiting_changes += 1  # how many changes wait here now (the event loop's thread only)
        try:
            await asyncio.wait_for(changes.acquire(), CHANGE_WAIT_SECONDS)
        except TimeoutError:
            return JSONResponse({"error": BUSY}, status_code=409)
        finally:
            app.state.waiting_changes -= 1
        try:
            # A cancelled request (a closed tab, a stopping server) does not stop its handler, which
            # runs on in a thread; the shield keeps the lock held until the handler has finished.
            with anyio.CancelScope(shield=True):
                return await call_next(request)
        finally:
            changes.release()

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        host = (request.headers.get("host") or "").rsplit(":", 1)[0]
        if host not in allowed_hosts:
            return JSONResponse({"error": "只接受本机访问"}, status_code=403)
        if owner_access:
            refused = owner_access.refusal(request)
            if refused is not None:
                return refused
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
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        return response

    def route_of(request: Request) -> dict[str, str]:
        """What a request was for, in the app's own words: its route (/api/jobs/{job_id}) and a job
        ID of the app's own form, never the path it was sent to, which the sender chose (and whose
        query carries the page token for previews and downloads)."""
        route = request.scope.get("route")
        if route is None:  # refused before routing: found without running anything
            route = next((item for item in app.router.routes if item.matches(request.scope)[0] == Match.FULL), None)
        seen = {"route": getattr(route, "path", None) or "(no route)"}
        job_id = request.path_params.get("job_id") or (request.scope.get("path_params") or {}).get("job_id")
        if isinstance(job_id, str) and JOB_ID.fullmatch(job_id):
            seen["job_id"] = job_id
        return seen

    def method_of(request: Request) -> str:
        """The request's method, if it is one of HTTP's own; any other word the sender chose."""
        return request.method if request.method in HTTP_METHODS else "(other)"

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        """One line per request: its route, status and time. A passing health check is left out
        (the container asks every 30 seconds). The request's id goes on every line logged while
        it runs, the server's own line about a failure included, and back to the browser."""
        started, current = time.monotonic(), secrets.token_hex(8)
        run_log.request_id.set(current)  # this request's task only; the server logs a failure in it too
        try:
            response = await call_next(request)
        except Exception:
            run_log.event(access_log, "request", level=logging.ERROR, error=True, method=method_of(request),
                          **route_of(request), status=500, duration_ms=run_log.elapsed_ms(started))
            raise
        seen = route_of(request)
        if not (seen["route"] == "/healthz" and response.status_code == 200):
            run_log.event(access_log, "request", level=logging.ERROR if response.status_code >= 500 else logging.INFO,
                          method=method_of(request), **seen, status=response.status_code,
                          duration_ms=run_log.elapsed_ms(started))
        response.headers["X-Request-Id"] = current
        return response

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> PlainTextResponse:
        """An error nobody expected: a bare 500, with the request's id so the page can quote it
        (the log line about it carries the same id); never the error's words."""
        current = run_log.request_id.get()
        return PlainTextResponse("Internal Server Error", status_code=500,
                                 headers={"X-Request-Id": current} if current else None)

    @app.get("/healthz")
    def health() -> JSONResponse:
        """Whether the data folder is there and writable; no model is called and nothing personal is shown."""
        folder = Path(facts_db).parent
        usable = folder.is_dir() and os.access(folder, os.W_OK)
        return JSONResponse({"status": "ok" if usable else "unavailable"}, status_code=200 if usable else 503)

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

    @app.exception_handler(StalePage)
    @app.exception_handler(StaleEdit)
    async def stale_page(_request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=409)

    @app.exception_handler(sqlite3.OperationalError)
    async def database_busy(_request: Request, exc: sqlite3.OperationalError) -> JSONResponse:
        """SQLite waits up to 30 seconds for another writer; after that the page is told to retry.
        Any other database error stays an error."""
        if "locked" in str(exc) or "busy" in str(exc):
            return JSONResponse({"error": BUSY}, status_code=409)
        raise exc

    def require(job_id: str, step: str) -> dict:
        data = workspace.read(job_id, step)
        if data is None:
            raise WorkspaceError(f"请先完成上一步（缺少 {step}）")
        return data

    def complete_jd(job_id: str) -> None:
        """Before model work on a job: a web page's summary alone is refused (job_url.require_complete_jd)."""
        require_complete_jd(require(job_id, "input").get("jd"))

    def job_view(job_id: str) -> dict:
        """What the page needs for one job: the JD and the current file of each step, all read
        while no step is being moved or replaced, so they agree with each other."""
        with workspace.lock:
            return job_view_locked(job_id)

    @lru_cache(maxsize=128)
    def sponsor_for_text(text: str) -> dict:
        # Per-app cache; a changed JD is a new key. Rule changes load on server restart.
        return assess_sponsorship(text)

    def job_view_locked(job_id: str) -> dict:
        steps = workspace.state(job_id)
        jd = {key: value for key, value in require(job_id, "input")["jd"].items() if key != "raw_content"}
        # Reuse analysis for unchanged saved descriptions. V2 persists it when saving.
        view: dict = {
            "job_id": job_id, "steps": steps, "jd": jd, "jd_diagnostics": jd_diagnostics(jd),
            "language": job_cv_language(jd["text"], job_id), "cv_languages": cv_languages(),
            "review": build_job_review(jd, list_facts(facts_db) if facts_db.exists() else [],
                                       sponsor_for_text(jd["text"])),
        }
        decided = workspace.read(job_id, "decided")
        requirements = decided or workspace.read(job_id, "candidates")
        if requirements:
            view["extraction"] = _explained(requirements.get("requirement_extraction"))
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
        interrupted = workspace.read_note(job_id, "interrupted")
        if interrupted:
            view["interrupted"] = interrupted
        stopped = [notice for notice in read_notices() if notice.get("job_id") == job_id]
        if stopped:
            view["interrupted_operations"] = stopped
        gaps = workspace.read(job_id, "gaps")
        if gaps:
            view["gaps"] = gaps_view(job_id, gaps) if gaps.get("language") == view["language"] else {"outdated": True}
        return view

    def gaps_view(job_id: str, gaps: dict) -> dict:
        """What the CV shows for each requirement, worked out from the CV as it is now, as far as
        the check still stands for the job's requirements (gaps.coverage, the rule V2 uses too). A
        check in an older format, or for a CV that no longer exists, is only marked out of date,
        so the page checks again."""
        _, head = cv_head(job_id, gaps.get("language", "en"))
        if head is None:
            return {"outdated": True}
        confirmed = [(fact["id"], fact["version"]) for fact in list_facts(facts_db) ] \
            if Path(facts_db).exists() else []
        shown = coverage(gaps, head, confirmed, workspace.read(job_id, "decided"))
        if shown.get("outdated"):
            return {"outdated": True}
        return {**shown, "requirements": with_adoptions(shown["requirements"], head),
                "language": gaps["language"], "created_at": gaps["created_at"],
                "places": [{key: place[key] for key in ("id", "kind", "where")} for place in shown_places(head)],
                "evidence_check": _explained(gaps.get("evidence_check")), "suggesting": _explained(gaps.get("suggesting"))}

    def acted_check(job_id: str, shown: GapRequest | None) -> dict:
        """The job's check the page acts on. Refused (409, nothing written) when the page showed a
        check of the other CV language, and when the check no longer applies to the job's
        requirements (gaps.unusable). This app keeps one check per job: checking the other
        language moves it to the job's history."""
        gaps = require(job_id, "gaps")
        if shown is not None and shown.language is not None and shown.language != gaps.get("language"):
            raise StalePage(PAGE_OUT_OF_DATE)
        reason = unusable(gaps, gaps.get("language"), workspace.read(job_id, "decided"))
        if reason == "requirements_changed":
            raise StalePage("The job's requirements changed after this check; check again before adding from it")
        if reason:
            raise StalePage("This check is out of date; check again first")
        return gaps

    def check_language(language: str) -> str:
        if language not in CV_LANGUAGES:
            raise WorkspaceError(f"简历语言必须是：{', '.join(CV_LANGUAGES)}")
        return language

    def cv_head(job_id: str, language: str) -> tuple[str | None, dict | None]:
        """The newest CV file for a language; later steps always derive from earlier ones."""
        for step in ("approved", "planned", "tailored", "draft"):
            data = workspace.read(job_id, f"cv-{step}-{check_language(language)}")
            if data is not None:
                return step, data
        return None, None

    def editable_cv(job_id: str, language: str) -> tuple[str, dict]:
        """The job's CV to change in a language, and its step: the newest prepared step, the one
        an approval, if any, was made of. Writing it moves that approval and the final PDF to the
        job's history, so a changed CV is approved again before it is final."""
        for step in ("planned", "tailored", "draft"):
            name = f"cv-{step}-{check_language(language)}"
            document = workspace.read(job_id, name)
            if document is not None:
                return name, document
        raise WorkspaceError("请先准备这个岗位的简历")

    def edit_cv(job_id: str, language: str, change: Callable[[dict], tuple[dict, bool]]) -> None:
        """One change of the job's own CV in a language (cv_edit; see editable_cv). Nothing is
        written when nothing changed; the profile and other jobs are never touched."""
        name, document = editable_cv(job_id, language)
        updated, changed = change(document)
        if changed:
            workspace.write(job_id, name, updated)

    def cv_view(job_id: str, language: str) -> dict:
        head_step, head = cv_head(job_id, language)
        stale = False
        if head:
            try:
                require_current_cv(head, language)
            except (CVError, FactStoreError):
                stale = True
        view: dict = {
            "head": head_step,
            "content_sha256": content_fingerprint(head) if head else None,
            "final_pdf": not stale and workspace.path(job_id, f"cv-final-{language}").exists(),
            "stale": stale,
        }
        draft = workspace.read(job_id, f"cv-draft-{language}")
        if draft:
            view["language_fallbacks"] = _describe_fallbacks(draft)
        status = workspace.read_note(job_id, f"cv-status-{language}")
        if status and status.get("draft_created_at") == (draft or {}).get("created_at"):
            view["stages"] = status["stages"]
        if head and "tailoring" in head:
            tailoring = head["tailoring"]
            view["tailoring"] = {key: tailoring.get(key) for key in ("model", "accepted", "rejected", "usage")}
            view["rewrites"] = rewritten_lines(head)
            view["rejected"] = rejected_lines(head)
        if head and "plan" in head:
            undone = set(head["plan"]["undone"])
            view["changes"] = [{**item, "undone": item["id"] in undone} for item in head["plan"]["changes"]]
        if head:
            view["edits"] = edits_view(head)
            view["lines"] = lines_view(head)
        if head_step == "approved":
            view["approved_at"] = head["approval"]["approved_at"]
        fit = workspace.read_note(job_id, f"cv-fit-{language}")
        if head and fit and fit.get("content_sha256") == view["content_sha256"]:
            view["fit"] = {key: value for key, value in fit.items() if key != "content_sha256"}
        pages = workspace.read_note(job_id, f"cv-pages-{language}")
        if head and not stale and pages and pages.get("content_sha256") == view["content_sha256"]:
            view["pagination"] = {key: pages[key] for key in ("pages", "checked_at")}
        if view["final_pdf"] and head_step == "approved":  # the name its download (and a batch ZIP) gives it
            jd = (workspace.read(job_id, "input") or {}).get("jd", {})
            view["download_name"] = pdf_filename(company=jd.get("company"), title=jd.get("title"), language=language,
                                                 job_id=job_id, version=view["content_sha256"])
        return view

    def profile_file(language: str | None = None, *, writing: bool = False) -> Path:
        base = Path(profile_path)
        if language is None:
            return base
        check_language(language)
        separate = base.with_name(f"{base.stem}.{language}{base.suffix}")
        if separate.exists():
            return separate
        if base.exists():
            try:
                available = profile_languages(json.loads(base.read_text(encoding="utf-8")))
            except (OSError, ValueError) as exc:
                raise CVError("无法读取简历资料", reason="profile_unreadable") from exc
            if language in available and (not writing or available == [language]):
                return base
            return separate
        return base if writing else separate

    def load_profile(language: str | None = None) -> dict:
        path = profile_file(language)
        if not path.exists():
            raise CVError("请先上传这个语言的简历", reason="no_profile")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CVError("无法读取简历资料", reason="profile_unreadable") from exc

    def save_profile(profile: dict, language: str | None = None, *, replace_language: bool = False) -> None:
        """Replace only this language, keeping the previous contents in profile-history."""
        path = profile_file(language, writing=replace_language)
        if path.exists():
            history = path.parent / "profile-history"
            history.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
            raw = path.read_bytes()
            try:
                readable = isinstance(json.loads(raw.decode("utf-8")), dict)
            except ValueError:
                readable = False
            write_atomically(history / f"{path.stem}-{stamp}.{'json' if readable else 'broken'}", raw)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_atomically(path, (json.dumps(profile, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))

    def profile_is_current(head: dict, language: str) -> bool:
        try:
            return head.get("profile_sha256") == _profile_hash(load_profile(language))
        except CVError:
            return False

    def require_current_cv(head: dict, language: str) -> None:
        if not profile_is_current(head, language):
            raise CVError("简历资料已更新，请重新准备并审核这份简历")
        verify_draft(head, facts_db)

    # The page exists to help the user present their best CV for each job, not to grade them.
    # Everything up to the CV review happens by itself: the requirements DeepSeek (or the
    # heading rules) finds all count, and the CV in the posting's language is drafted, reworded
    # and adjusted to them. The user can undo any change; approving the CV is never automatic.
    def known_private_terms() -> list[str]:
        """What requests must mask: the name, contact details, schools and employers of the
        current CV in every language, and of every earlier one, since an older fact can still
        name a past employer."""
        path = Path(profile_path)
        terms: set[str] = set()
        for source in [path, *(path.with_name(f"{path.stem}.{lang}{path.suffix}") for lang in CV_LANGUAGES),
                       *sorted((path.parent / "profile-history").glob("*.json"))]:
            if not source.exists():
                continue
            try:
                terms.update(private_terms(json.loads(source.read_text(encoding="utf-8"))))
            except (OSError, ValueError, AttributeError) as exc:
                # Unsure what is private: send nothing rather than guess.
                if source == path:
                    raise CVError(f"无法读取简历 profile：{source.name}", reason="profile_unreadable") from exc
                error = CVError(f"无法读取 profile 备份：{source.name}", reason="backup_unreadable")
                error.file = source.name
                raise error from exc
        return sorted(terms, key=len, reverse=True)

    def log_stage(job_id: str, language: str, stage: dict, started: float) -> dict:
        """One line for a stage that ran: how it went and why, never its message or the CV."""
        run_log.event(stage_log, "stage", level=logging.INFO if stage["status"] == "done" else logging.WARNING,
                      job_id=job_id, language=language, stage=stage["stage"], status=stage["status"],
                      reason=stage["reason_code"], duration_ms=run_log.elapsed_ms(started))
        return stage

    def record_stages(job_id: str, language: str, stages: list[dict]) -> None:
        """How the latest preparation went, tied to the draft it produced (None if none)."""
        draft = workspace.read(job_id, f"cv-draft-{language}")
        workspace.write_note(job_id, f"cv-status-{language}", {
            "draft_created_at": draft["created_at"] if draft else None,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "stages": sorted(stages, key=lambda item: STAGES.index(item["stage"])),
        })

    def update_stage(job_id: str, language: str, stage: dict, not_run: tuple[str, ...] = ()) -> None:
        """Replace one stage after a retry; stages built on its old result are marked not run."""
        draft = workspace.read(job_id, f"cv-draft-{language}")
        status = workspace.read_note(job_id, f"cv-status-{language}")
        if not draft or not status or status.get("draft_created_at") != draft["created_at"]:
            status = {"stages": [_stage("draft", "done", "Built from your saved material.")]}
        kept = [item for item in status["stages"] if item["stage"] not in (stage["stage"], *not_run)]
        again = [_stage(name, "skipped", f"{STAGE_FAILED[name]} since the last change; use the button below.")
                 for name in not_run]
        record_stages(job_id, language, [*kept, stage, *again])

    def prepare_cv(job_id: str, language: str) -> None:
        """Draft, reword and adjust one language's CV for this job, recording how each stage
        went. When DeepSeek cannot reword or adjust, the CV stays at the last stage that worked
        and the page says why, so a usable CV is never mistaken for a tailored one. The job's own
        changes of the CV until now (adopted and edited lines) go on again, word for word, after
        what the model did (cv_edit.carry_over); the model sees the CV without them."""
        complete_jd(job_id)
        job = require(job_id, "decided")
        previous = cv_head(job_id, check_language(language))[1]
        started = time.monotonic()
        try:
            if not Path(facts_db).exists():
                raise CVError("还没有事实库", reason="no_facts")
            head = build_draft(load_profile(check_language(language)), facts_db, language, job=job)
        except (CVError, FactStoreError) as exc:
            # An earlier CV, if any, stays as it was; the page says it is not up to date.
            earlier = workspace.read(job_id, f"cv-draft-{language}") is not None
            failure = log_stage(job_id, language, _stage_failure("draft", exc), started)
            if earlier:
                failure.update(output_available=True, message=failure["message"] + " The CV below is from the last "
                               "time it could be prepared.")
            record_stages(job_id, language, [failure, *(
                _stage(name, "skipped", f"{STAGE_FAILED[name]}: the CV could not be drafted.", output=earlier)
                for name in ("rewording", "layout"))])
            raise
        workspace.write(job_id, f"cv-draft-{language}", carry_over(previous, head))
        stages = [log_stage(job_id, language, _stage("draft", "done", "Built from your saved material."), started)]
        for step in prepare_steps(head, facts_db, job, chat=chat, private=known_private_terms(),
                                  previous=previous, tailor=tailor_draft, plan=plan_draft):
            if step.document is not None:
                workspace.write(job_id, f"cv-{step.kind}-{language}", step.document)
            stages.append(log_stage(job_id, language, step.status, step.started))
        record_stages(job_id, language, stages)

    def cv_languages() -> list[str]:
        available = []
        for language in CV_LANGUAGES:
            try:
                load_profile(language)
                available.append(language)
            except CVError as exc:
                if exc.reason != "no_profile":
                    raise
        return available

    def job_cv_language(jd_text: str, job_id: str | None = None) -> str:
        available = cv_languages()
        chosen = workspace.read_note(job_id, "cv-language") if job_id else None
        wanted = chosen.get("language") if chosen else _job_language(jd_text)
        return wanted if wanted in available else (available or ["en"])[0]

    def prepare_cv_quietly(job_id: str) -> None:
        """The CV for this posting in a language the resume is written in. Without a profile
        or confirmed facts yet, the CV panel's Prepare button shows what is missing."""
        try:
            prepare_cv(job_id, job_cv_language(require(job_id, "input")["jd"]["text"], job_id))
        except (CVError, FactStoreError, OSError, ValueError):
            pass

    def match_automatically(job_id: str) -> None:
        """Optional talking points: the confirmed fact that best speaks to each requirement."""
        if not Path(facts_db).exists():
            raise FactStoreError("还没有事实库；请先导入并确认事实")
        complete_jd(job_id)
        matches = propose_matches(require(job_id, "decided"), facts_db, chat=chat, private=known_private_terms())
        workspace.write(job_id, "matches", matches)
        links, no_match = first_candidates(matches)
        linked = apply_match_decisions(matches, facts_db, links, no_match, decided_by="auto")
        workspace.write(job_id, "linked", linked)

    def prepare_automatically(job_id: str, kept: dict[str, str] | None = None) -> None:
        """Keep the user's own decisions (requirement ID -> status), count every other found
        requirement, then prepare the CV. What is counted this way is recorded as decided
        automatically, never as reviewed."""
        candidates = require(job_id, "candidates")
        ids = {item["id"] for item in candidates["requirement_candidates"]}
        kept = {key: status for key, status in (kept or {}).items() if key in ids}
        confirmed = {key for key, status in kept.items() if status == "confirmed"}
        excluded = {key for key, status in kept.items() if status == "excluded"}
        if not ids - excluded:
            return
        decided = candidates
        if confirmed or excluded:
            decided = apply_requirement_decisions(decided, confirmed, excluded)
        if ids - confirmed - excluded:
            decided = apply_requirement_decisions(decided, ids - confirmed - excluded, set(), decided_by="auto")
        workspace.write(job_id, "decided", decided)
        prepare_cv_quietly(job_id)

    def create_prepared_job(review_input: dict) -> str:
        require_complete_jd(review_input.get("jd"))
        candidates = propose_requirements(review_input, chat=chat)
        job_id = workspace.create_job(review_input)
        workspace.write(job_id, "candidates", candidates)
        prepare_automatically(job_id)
        return job_id

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (WEB_DIR / "index.html").read_text(encoding="utf-8").replace("__WORKBENCH_TOKEN__", token)

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    @app.get("/api/facts")
    def facts() -> dict:
        unfinished = [notice for notice in read_notices() if not notice.get("job_id")]
        stopped = {"interrupted": unfinished} if unfinished else {}
        if not Path(facts_db).exists():
            return {"facts": [], **stopped}
        return {"facts": list_facts(facts_db), **stopped}

    @app.get("/api/cv/languages")
    def available_cv_languages() -> dict:
        return {"languages": cv_languages()}

    @app.post("/api/facts/{fact_id}/edit")
    def edit_fact(fact_id: str, request: EditFactRequest) -> dict:
        updated, changed = revise_fact(facts_db, fact_id, text=request.text, tags=request.tags,
                                       expected_version=request.expected_version)
        return {"fact": updated, "changed": changed}

    @app.post("/api/facts/confirm")
    def confirm(request: ConfirmRequest) -> dict:
        confirmed = confirm_facts(facts_db, parse_fact_refs(request.refs))
        return {"changed_count": sum(changed for _, changed in confirmed)}

    # An uploaded CV is read here; DeepSeek only sees its lines without the name and contact
    # details. What it proposes waits in cv-uploads/ until the user checks the details and saves.

    def upload_path(upload_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{16}", upload_id):
            raise CVImportError("找不到这次上传；请重新上传 PDF")
        return uploads / f"{upload_id}.json"

    def propose_cv(data: bytes, language: str | None = None) -> dict:
        pdf = read_pdf(data)
        proposal = structure_cv(pdf["lines"], chat, links=pdf["links"])
        if language is not None:
            proposal["language"] = check_language(language)
        proposal["source_sha256"] = hashlib.sha256(data).hexdigest()  # names this PDF if saving is cut short
        upload_id = secrets.token_hex(8)
        uploads.mkdir(parents=True, exist_ok=True)
        # An upload holds contact details; one neither saved nor cancelled is not kept for long.
        for forgotten in uploads.glob("*.json"):
            try:
                stale = time.time() - forgotten.stat().st_mtime > UPLOAD_KEEP_SECONDS
            except FileNotFoundError:
                continue  # another upload, tidying at the same time (uploads wait for nothing)
            if stale:
                forgotten.unlink(missing_ok=True)
        write_atomically(uploads / f"{upload_id}.json", json.dumps(proposal, ensure_ascii=False).encode("utf-8"))
        known = {fact["text"] for fact in list_facts(facts_db)} if Path(facts_db).exists() else set()
        for section in proposal["sections"]:
            for entry in section["entries"]:
                for fact in entry["facts"]:
                    fact["known"] = fact["text"] in known
        return {"upload_id": upload_id, "has_profile": Path(profile_path).exists(), **proposal}

    @app.post("/api/cv/upload")
    async def upload_cv(request: Request, language: str | None = None) -> dict:
        if language is not None:
            check_language(language)
        data = bytearray()
        async for chunk in request.stream():
            data += chunk
            if len(data) > MAX_PDF_BYTES:
                raise CVImportError(f"PDF 不能超过 {MAX_PDF_BYTES // 1_000_000} MB")
        return await run_in_threadpool(propose_cv, bytes(data), language)

    @app.post("/api/cv/uploads/{upload_id}/save")
    def save_uploaded_cv(upload_id: str, request: SaveCVRequest) -> dict:
        """Import the CV's lines as pending facts and make it the CV layout, old one backed up."""
        path = upload_path(upload_id)
        try:
            proposal = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise CVImportError("找不到这次上传；请重新上传 PDF") from None
        with operation("save_cv", key=proposal.get("source_sha256")):
            profile, items, reused = build_profile(proposal, request.model_dump(), facts_db)
            if items:
                import_facts(facts_db, items)
            language = proposal.get("language")
            if language:
                # The user chooses the document language; names are not language detectors.
                profile["name"] = {language: profile["name"]}
            save_profile(profile, language, replace_language=True)
            path.unlink(missing_ok=True)
        return {"imported": len(items), "reused": reused}

    @app.delete("/api/cv/uploads/{upload_id}")
    def cancel_upload(upload_id: str) -> dict:
        upload_path(upload_id).unlink(missing_ok=True)
        return {"cancelled": True}

    @app.post("/api/notices/dismiss")
    def dismiss_notice(request: DismissRequest) -> dict:
        """The user has read a notice about an action that did not finish."""
        if not NOTICE_ID.fullmatch(request.id):
            raise ValueError("没有这条提示")
        remove_durably(unfinished / f"{request.id}.json")
        return {"dismissed": True}

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
        title: str = "", location: str = "", limit: int = 50, offset: int = 0, hide_senior: bool = False, region: str = "all",
        sponsorship: str = "all"
    ) -> dict:
        ranked = ranked_listings(listings_db, facts_db, title, location, limit, offset, hide_senior, region, sponsorship)
        started = started_jobs()
        for group in ranked["listings"]:
            for item in group["postings"]:  # each location opens the job started from that posting
                item["started_job"] = started.get(item["source"])
            group["started_job"] = next((item["started_job"] for item in group["postings"] if item["started_job"]), None)
        return ranked

    @app.post("/api/listings/start")
    def start_listing(request: StartListingRequest) -> dict:
        # One job per posting: a second click opens the first job. A concurrent one waits for the
        # first to finish (changes run one at a time), then finds it.
        listing = get_listing(listings_db, request.provider, request.board, request.job_id)
        existing = started_jobs().get(listing["source"])
        if existing:
            return {"job_id": existing, "existing": True}
        selected = selected_posting(request.board, request.job_id, request.provider)
        selected["jd"]["company"] = selected["jd"].get("company") or listing["company"]
        return {"job_id": create_prepared_job(prepare_review_input(selected)), "existing": False}

    @app.get("/api/jobs")
    def jobs() -> dict:
        return {"jobs": workspace.jobs()}

    @app.post("/api/jobs")
    def new_job(request: NewJobRequest) -> dict:
        selected = prepare_pasted_jd(request.text, request.title, request.company, request.url, request.location)
        return {"job_id": create_prepared_job(prepare_review_input(selected))}

    @app.post("/api/jobs/from-url")
    def new_job_from_url(request: JobURLRequest) -> dict:
        url = normalize_url(request.url)
        try:
            identity = parse_posting_link(url)
        except SearchError:
            identity = None
        # Identity ignores tracking queries, host aliases and application-page suffixes.
        # Mutating requests already share the app's storage lock.
        for summary in workspace.jobs():
            jd = (workspace.read(summary["job_id"], "input") or {}).get("jd", {})
            if ((identity and (jd.get("provider"), jd.get("board"), jd.get("job_id")) == identity) or
                    url in (jd.get("requested_url"), jd.get("source"))):
                return {"job_id": summary["job_id"], "existing": True}
        if identity:
            provider, board, posting_id = identity
            selected = selected_posting(board, posting_id, provider)
        else:
            selected = url_posting(url)
        if not selected["jd"].get("text", "").strip():
            raise SearchError("岗位接口未返回 JD 正文，请使用手动粘贴。")
        existing = started_jobs().get(selected["jd"].get("source"))
        if existing:
            return {"job_id": existing, "existing": True}
        selected["jd"]["requested_url"] = url
        return {"job_id": create_prepared_job(prepare_review_input(selected)), "existing": False}

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str) -> dict:
        return job_view(job_id)

    @app.post("/api/jobs/{job_id}/requirements/add")
    def add_requirement(job_id: str, request: AddRequirementRequest) -> dict:
        decided = workspace.read(job_id, "decided") or {}
        # What the user decided stays; only they ever exclude a line.
        kept = {
            item["id"]: item["status"] for item in decided.get("requirement_candidates", [])
            if item.get("decided_by") == "user" or item.get("status") == "excluded"
        }
        candidates = require(job_id, "candidates")
        added = add_manual_requirements(candidates, [request.text])
        workspace.write(job_id, "candidates", added)
        for item in added["requirement_candidates"][len(candidates["requirement_candidates"]):]:
            kept[item["id"]] = "confirmed"  # the user added it as a requirement
        prepare_automatically(job_id, kept)
        return job_view(job_id)

    @app.post("/api/jobs/{job_id}/requirements/find")
    def find_requirements(job_id: str) -> dict:
        complete_jd(job_id)
        workspace.write(job_id, "candidates", propose_requirements(require(job_id, "input"), chat=chat))
        prepare_automatically(job_id)
        return job_view(job_id)

    @app.post("/api/jobs/{job_id}/requirements/decide")
    def decide_requirements(job_id: str, request: RequirementDecisionRequest) -> dict:
        decided = apply_requirement_decisions(
            require(job_id, "candidates"), set(request.confirm), set(request.exclude)
        )
        workspace.write(job_id, "decided", decided)
        prepare_cv_quietly(job_id)
        return job_view(job_id)

    @app.post("/api/jobs/{job_id}/matches/propose")
    def propose_job_matches(job_id: str) -> dict:
        match_automatically(job_id)
        return job_view(job_id)

    @app.post("/api/jobs/{job_id}/matches/decide")
    def decide_job_matches(job_id: str, request: MatchDecisionRequest) -> dict:
        linked = apply_match_decisions(
            require(job_id, "matches"), facts_db, request.links, set(request.no_match)
        )
        workspace.write(job_id, "linked", linked)
        return job_view(job_id)

    @app.post("/api/jobs/{job_id}/language")
    def choose_cv_language(job_id: str, request: CVLanguageRequest) -> dict:
        language = check_language(request.language)
        if language not in cv_languages():
            raise CVError("请先上传这个语言的简历", reason="no_profile")
        require(job_id, "input")
        workspace.write_note(job_id, "cv-language", {"language": language})
        return job_view(job_id)

    @app.post("/api/jobs/{job_id}/cv/{language}/draft")
    def cv_draft(job_id: str, language: str) -> dict:
        draft = build_draft(
            load_profile(check_language(language)), facts_db, language, job=workspace.read(job_id, "decided")
        )
        workspace.write(job_id, f"cv-draft-{language}", carry_over(cv_head(job_id, language)[1], draft))
        return job_view(job_id)

    @app.post("/api/jobs/{job_id}/cv/{language}/tailor")
    def cv_tailor(job_id: str, language: str) -> dict:
        """Reword again from the draft as prepared; the job's own changes go on again after."""
        complete_jd(job_id)
        draft = without_edits(require(job_id, f"cv-draft-{check_language(language)}"))
        previous = cv_head(job_id, language)[1]
        started = time.monotonic()
        try:
            tailored = tailor_draft(draft, facts_db, job=workspace.read(job_id, "decided"), chat=chat,
                                    private=known_private_terms())
        except CVError as exc:
            earlier = workspace.read(job_id, f"cv-tailored-{language}") is not None
            update_stage(job_id, language, log_stage(job_id, language, _stage_failure("rewording", exc, earlier), started))
            raise
        workspace.write(job_id, f"cv-tailored-{language}", carry_over(previous, tailored))
        update_stage(job_id, language, log_stage(job_id, language, _reworded(tailored), started), not_run=("layout",))
        return job_view(job_id)

    @app.post("/api/jobs/{job_id}/gaps")
    def check_gaps(job_id: str, shown: GapRequest | None = None) -> dict:
        """What the CV, as it is now, has behind each requirement, with suggestions for what
        nothing shows. A suggestion declined before stays declined. A page showing the CV language
        another tab has switched away from is out of date: its check would not be shown."""
        language = job_cv_language(require(job_id, "input")["jd"]["text"], job_id)
        if shown is not None and shown.language is not None and shown.language != language:
            raise StalePage(PAGE_OUT_OF_DATE)
        _, head = cv_head(job_id, language)
        if head is None:
            raise WorkspaceError("请先准备这个岗位的简历，再检查缺口")
        complete_jd(job_id)
        workspace.write(job_id, "gaps", find_gaps(require(job_id, "decided"), head, facts_db, chat,
                                                  private=known_private_terms(), previous=workspace.read(job_id, "gaps")))
        return job_view(job_id)

    @app.post("/api/jobs/{job_id}/gaps/{requirement_id}/suggest", response_model=None)
    def suggest_entry(job_id: str, requirement_id: str, shown: GapRequest | None = None) -> dict | JSONResponse:
        """Complete wording for one requirement the check found missing or only related, made
        when the user asks, with the CV as it is now. A complete answer is saved and reused; a
        failed or empty one saves nothing, so the next click asks again."""
        gaps = acted_check(job_id, shown)
        _, head = cv_head(job_id, gaps.get("language", "en"))
        if head is None:
            raise WorkspaceError("请先准备这个岗位的简历")
        complete_jd(job_id)
        try:
            updated = suggest_gap(gaps, require(job_id, "decided"), head, facts_db, requirement_id, chat,
                                  private=known_private_terms())
        except DeepSeekError as exc:  # the kind of failure, never DeepSeek's own words
            return JSONResponse({"error": REASONS.get(exc.reason) or REASONS["bad_response"]}, status_code=502)
        if updated != gaps:
            workspace.write(job_id, "gaps", updated)
        return job_view(job_id)

    def action_key(http: Request) -> str | None:
        """The page's key for the user's action (Idempotency-Key), if it sent one: the same action
        again changes nothing more (cv_edit)."""
        supplied = http.headers.get("idempotency-key")
        if supplied is not None and not (0 < len(supplied) <= 128 and supplied.isprintable()):
            raise ValueError("Invalid Idempotency-Key")
        return supplied

    # Adopting puts the words on this job's CV in the check's language, and nowhere else: nothing
    # is prepared again, and the profile changes only when the user saves a line to it (below).

    @app.post("/api/jobs/{job_id}/gaps/{requirement_id}/accept")
    def accept_suggestion(http: Request, job_id: str, requirement_id: str, shown: GapRequest | None = None) -> dict:
        """The user adopts the suggestion as it is, into this CV only (cv_edit.adopt_for)."""
        gaps = acted_check(job_id, shown)
        row, key = _gap(gaps, requirement_id), action_key(http)
        edit_cv(job_id, gaps["language"], lambda document: adopt_for(document, row, request=key))
        return job_view(job_id)

    @app.post("/api/jobs/{job_id}/gaps/{requirement_id}/write")
    def write_own_line(http: Request, job_id: str, requirement_id: str, request: WriteLineRequest) -> dict:
        """The user's own words for a requirement (or the suggestion as they edited it), adopted
        word for word into this CV only."""
        gaps = acted_check(job_id, request)
        row, key = _gap(gaps, requirement_id), action_key(http)
        edit_cv(job_id, gaps["language"], lambda document: adopt_for(
            document, row, place=request.place, text=request.text, request=key))
        return job_view(job_id)

    # -- the job CV's own lines: edit, undo or redo, save to the profile ---------------------

    @app.post("/api/jobs/{job_id}/cv/{language}/lines/{block}")
    def cv_edit_line(http: Request, job_id: str, language: str, block: str, request: LineEditRequest) -> dict:
        """New words for one line of this CV only; refused (409) when the line no longer reads
        what the page showed."""
        key = action_key(http)
        edit_cv(job_id, language, lambda document: change_line(
            document, block, request.text, expected_text=request.expected_text, request=key))
        return job_view(job_id)

    @app.post("/api/jobs/{job_id}/cv/{language}/edits/{edit_id}")
    def cv_set_edit(http: Request, job_id: str, language: str, edit_id: str, request: EditUndoRequest) -> dict:
        key = action_key(http)
        edit_cv(job_id, language, lambda document: set_edit(document, edit_id, request.undone, request=key))
        return job_view(job_id)

    @app.post("/api/jobs/{job_id}/cv/{language}/lines/{block}/save")
    def cv_save_line(job_id: str, language: str, block: str, request: SaveLineRequest) -> dict:
        """The user saves one line of this CV to their profile, for their other CVs too
        (cv_edit.save_plan): a pending saved entry, never a confirmed one. Checked before anything
        is written; the facts, the profile and this CV are then written in turn, with a notice that
        stays if that is cut short (saving again finishes it without adding anything twice). This
        CV stays current if it was; other CVs made from the profile before are not any more."""
        name, document = editable_cv(job_id, language)
        key = f"{language}:{block}:{hashlib.sha256(request.expected_text.encode()).hexdigest()}"
        if already_saved(document, block, request.expected_text):
            # The CV write completed but removing the notice (or returning the answer) did not.
            with operation("save_line", job_id, key=key, block=block, line=request.expected_text, language=language):
                pass
            return job_view(job_id)
        target = save_target(document, block)
        profile = load_profile(language)
        plan = save_plan(target, profile, language, fact_source(facts_db))
        before_hash = _profile_hash(profile)
        after_hash = _profile_hash(plan["profile"]) if plan["profile"] is not None else before_hash
        earlier = next((notice for notice in read_notices() if notice.get("kind") == "save_line"
                        and notice.get("job_id") == job_id and notice.get("key") == key), None)
        current = document.get("profile_sha256") == before_hash
        if earlier:
            current = (document.get("profile_sha256") == earlier.get("profile_before")
                       and before_hash in (earlier.get("profile_before"), earlier.get("profile_after")))
        with operation("save_line", job_id, key=key, block=block, line=target["text"], language=language,
                       profile_before=before_hash, profile_after=after_hash):
            fact = plan["fact"]
            if plan["create"]:
                fact = import_facts(facts_db, [{"id": block, "text": target["text"], "type": target["fact_type"],
                                                "tags": plan["tags"]}])[0][0]
            elif plan["revise"]:
                fact = revise_fact(facts_db, block, text=target["text"], tags=plan["tags"],
                                   expected_version=fact["version"])[0]
            profile_hash = after_hash if current else None
            if plan["profile"] is not None:
                save_profile(plan["profile"], language)
            workspace.write(job_id, name, mark_saved(document, block, fact["version"], profile_sha256=profile_hash))
        return job_view(job_id)

    @app.post("/api/jobs/{job_id}/gaps/{requirement_id}/decline")
    def decline_suggestion(job_id: str, requirement_id: str, shown: GapRequest | None = None) -> dict:
        workspace.write(job_id, "gaps", decline_gap(acted_check(job_id, shown), requirement_id))
        return job_view(job_id)

    @app.post("/api/jobs/{job_id}/cv/{language}/prepare")
    def cv_prepare(job_id: str, language: str) -> dict:
        prepare_cv(job_id, language)
        return job_view(job_id)

    @app.post("/api/jobs/{job_id}/cv/{language}/plan")
    def cv_plan(job_id: str, language: str) -> dict:
        """Adjust (again) for this job, starting from the reworded CV, or the draft if none, as
        prepared; the job's own changes go on again after."""
        complete_jd(job_id)
        base = workspace.read(job_id, f"cv-tailored-{check_language(language)}") or require(job_id, f"cv-draft-{language}")
        previous = cv_head(job_id, language)[1]
        started = time.monotonic()
        try:
            planned = plan_draft(without_edits(base), require(job_id, "decided"), chat=chat, private=known_private_terms())
        except CVError as exc:
            earlier = workspace.read(job_id, f"cv-planned-{language}") is not None
            update_stage(job_id, language, log_stage(job_id, language, _stage_failure("layout", exc, earlier), started))
            raise
        workspace.write(job_id, f"cv-planned-{language}", carry_over(previous, planned))
        update_stage(job_id, language, log_stage(job_id, language, _adjusted(planned), started))
        return job_view(job_id)

    @app.post("/api/jobs/{job_id}/cv/{language}/change")
    def cv_change(job_id: str, language: str, request: ChangeRequest) -> dict:
        planned = require(job_id, f"cv-planned-{check_language(language)}")
        workspace.write(job_id, f"cv-planned-{language}", set_change(planned, request.change_id, request.undone))
        return job_view(job_id)

    @app.post("/api/jobs/{job_id}/cv/{language}/fit")
    def cv_fit(job_id: str, language: str) -> dict:
        """Leave out the lines that help this job least until the CV prints on one page
        (cv_fit), as cuts listed with the job's other changes, each undone on its own. The
        printed page count decides. A CV that already fits, or cannot without leaving out what
        must stay, is not changed; the page says which, beside this version of the CV. Like every
        change here, it runs alone (one_change_at_a_time), so no edit lands while it prints."""
        _name, document = editable_cv(job_id, check_language(language))
        require_current_cv(document, language)
        fitted, outcome = fit_outcome(document, require(job_id, "decided"), lambda draft: rendered_pages(draft, printer))
        if fitted is not None:
            workspace.write(job_id, f"cv-planned-{language}", fitted)
        workspace.write_note(job_id, f"cv-fit-{language}",
                             {**outcome, "content_sha256": content_fingerprint(fitted or document)})
        return job_view(job_id)

    @app.post("/api/jobs/{job_id}/cv/{language}/pages")
    def cv_pages(job_id: str, language: str) -> dict:
        """Print the current CV as its final PDF would be and keep how many pages that is, for
        exactly this version (its fingerprint): approving needs it. A failed print keeps
        nothing. It runs alone (one_change_at_a_time), so no edit lands while it prints."""
        _step, head = cv_head(job_id, check_language(language))
        if head is None:
            raise WorkspaceError("请先生成简历草稿")
        require_current_cv(head, language)
        pages = rendered_pages(head, printer, final=True)
        workspace.write_note(job_id, f"cv-pages-{language}", {"content_sha256": content_fingerprint(head), "pages": pages,
                                                              "checked_at": datetime.now(timezone.utc).isoformat()})
        return job_view(job_id)

    @app.post("/api/jobs/{job_id}/cv/{language}/approve", response_model=None)
    def cv_approve(job_id: str, language: str, request: ApproveRequest) -> dict | JSONResponse:
        """Approve exactly the CV the page showed, once its pages were checked for exactly that
        version (cv_pages); one that prints on more than one page only when the user accepts
        that. If another tab changed it since, nothing is approved; a second click on the same CV
        approves nothing new."""
        head_step, head = cv_head(job_id, language)
        if head is None:
            raise WorkspaceError("请先生成简历草稿")
        require_current_cv(head, language)
        if content_fingerprint(head) != request.expected_content_sha256:
            return JSONResponse({"error": CV_CHANGED}, status_code=409)
        pages = workspace.read_note(job_id, f"cv-pages-{language}")
        if head_step != "approved" and (not pages or pages.get("content_sha256") != request.expected_content_sha256):
            return JSONResponse({"error": PAGES_UNCHECKED}, status_code=409)
        if head_step != "approved" and pages["pages"] > 1 and not request.allow_multiple_pages:
            return JSONResponse({"error": PAGES_OVER}, status_code=409)
        if head_step != "approved":
            workspace.write(job_id, f"cv-approved-{language}", approve_draft(head, facts_db))
        return job_view(job_id)

    @app.post("/api/jobs/{job_id}/cv/{language}/export")
    def cv_export(job_id: str, language: str) -> dict:
        approved = require(job_id, f"cv-approved-{check_language(language)}")
        require_current_cv(approved, language)
        with tempfile.TemporaryDirectory(prefix="cv-web-") as directory:
            output = Path(directory) / "cv.pdf"
            export_pdf(approved, facts_db, output, printer=printer)
            data = output.read_bytes()
        workspace.write_bytes(job_id, f"cv-final-{language}", data)
        return job_view(job_id)

    @app.get("/preview/{job_id}/{language}", response_class=HTMLResponse, response_model=None)
    def cv_preview(job_id: str, language: str, v: str = "") -> str | HTMLResponse:
        """The CV as it will print. The page asks for the version it holds (v, its fingerprint),
        so the frame never shows a newer CV than the changes and the Approve button beside it."""
        with workspace.lock:
            head_step, head = cv_head(job_id, language)
        if head is None:
            raise WorkspaceError("请先生成简历草稿")
        if v and v != content_fingerprint(head):
            return HTMLResponse(PREVIEW_CHANGED, status_code=409)
        try:
            require_current_cv(head, language)
            final = head_step == "approved" and is_final_approval(head)
        except (CVError, FactStoreError):
            final = False
        return render_html(head, final=final)

    def batch_finals(job_ids: list[str]) -> list[dict]:
        """What a batch download needs of each job (batch_zip.build), so the PDFs, their names
        and the manifest describe the same moment: its request holds out every change (facts,
        profiles, jobs; see one_change_at_a_time), and the workspace's lock is held as well. A
        job that is not found refuses the whole batch."""
        jobs = []
        with workspace.lock:
            for job_id in job_ids:
                jd = (workspace.read(job_id, "input") or {}).get("jd") or {}
                cvs: dict = {}
                for language in CV_LANGUAGES:
                    head_step, head = cv_head(job_id, language)
                    if head is None:
                        continue
                    try:
                        require_current_cv(head, language)
                        stale = False
                    except (CVError, FactStoreError):
                        stale = True
                    version = content_fingerprint(head)
                    approved = head_step == "approved" and not stale and is_final_approval(head)
                    pages = workspace.read_note(job_id, f"cv-pages-{language}") or {}
                    draft = workspace.read(job_id, f"cv-draft-{language}") or {}
                    status = workspace.read_note(job_id, f"cv-status-{language}") or {}
                    cvs[language] = {
                        "content_sha256": version, "stale": stale, "approved": approved,
                        "pdf": workspace.read_bytes(job_id, f"cv-final-{language}") if approved else None,
                        "pages": pages.get("pages") if pages.get("content_sha256") == version else None,
                        "stages": [{"stage": item.get("stage"), "status": item.get("status")}
                                   for item in status.get("stages") or []]
                        if status.get("draft_created_at") == draft.get("created_at") else [],
                    }
                if not cvs:
                    cvs[job_cv_language(jd.get("text") or "", job_id)] = {"content_sha256": None}
                jobs.append({"job_id": job_id, "company": jd.get("company"), "title": jd.get("title"), "cvs": cvs})
        return jobs

    @app.get("/download/batch.zip", response_model=None)
    def download_batch(request: Request, jobs: str = "") -> Response:
        """Every ready final PDF of a batch of jobs in one ZIP with a manifest (batch_zip), as
        the V2 app does: ``ready`` is what the page saw ready, and any difference now, or a job
        not found, refuses the whole download. Reading starts nothing and calls no model."""
        try:
            ids, expected = batch_zip.parse_request(jobs, request.query_params.getlist("ready"))
            data, manifest = batch_zip.build(batch_finals(ids), expected)
        except WorkspaceError:
            return JSONResponse({"error": batch_zip.NOT_FOUND}, status_code=404)
        except batch_zip.BatchError as exc:
            return JSONResponse({"error": str(exc)}, status_code=exc.status)
        return Response(data, media_type="application/zip",
                        headers={"Content-Disposition": batch_zip.disposition(manifest["included"])})

    @app.get("/download/{job_id}/{language}.pdf")
    def cv_download(job_id: str, language: str) -> Response:
        # The approval, its PDF and the job read together, so the name (download_name) is always
        # that PDF's. Read whole (a CV is small), so a new export moving this one to history cannot
        # cut it off.
        with workspace.lock:
            approved = require(job_id, f"cv-approved-{check_language(language)}")
            data = workspace.read_bytes(job_id, f"cv-final-{check_language(language)}")
            jd = (workspace.read(job_id, "input") or {}).get("jd", {})
        require_current_cv(approved, language)
        if data is None:
            raise WorkspaceError("还没有已批准的最终 PDF")
        return Response(data, media_type="application/pdf", headers={"Content-Disposition": pdf_disposition(
            company=jd.get("company"), title=jd.get("title"), language=language, job_id=job_id,
            version=content_fingerprint(approved))})

    app.state.workspace = workspace
    return app


def main(argv: list[str] | None = None) -> int:
    settings = server_settings(argv, os.environ)
    log = logging.getLogger("workbench")
    if settings.json_logs:
        run_log.configure(sys.stderr)
    if settings.require_data:
        problem = data_problem(settings.data)
        if problem:
            if settings.json_logs:
                run_log.event(log, "refused_to_start", level=logging.ERROR, reason=problem)
            else:
                print(f"不启动：{problem}", file=sys.stderr)
            return 2
    import uvicorn

    if settings.json_logs:
        run_log.event(log, "listening", port=settings.port)
    else:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
        print(f"打开 http://127.0.0.1:{settings.port}/ （只在本机可用；按 Ctrl+C 停止）")
    try:
        app = create_app(settings.facts_db, settings.jobs, profile_path=settings.profile)
    except DataFormatError as exc:  # its words are the app's own: format numbers, never data
        if settings.json_logs:
            run_log.event(log, "refused_to_start", level=logging.ERROR, reason=str(exc))
        else:
            print(f"不启动：{exc}", file=sys.stderr)
        return 2
    except Exception:
        if not settings.json_logs:
            raise  # on a terminal, the whole traceback
        run_log.event(log, "failed_to_start", level=logging.ERROR, error=True)  # its type and place only
        return 1
    # Uvicorn's own access log would print the query string, and with it the page token. With
    # JSON logs, its own lines go through the same JSON formatter (no log_config of its own).
    uvicorn.run(app, host=settings.host, port=settings.port, access_log=False,
                **({"log_config": None} if settings.json_logs else {}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
