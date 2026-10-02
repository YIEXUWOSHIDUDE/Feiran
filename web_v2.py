"""V2's web app: the single-user page and API, for up to 100 signed-in users on one server.

Who a request is comes only from a server-side session created after a verified Cognito sign-in
(identity.py); every private route then works through that user's UserWorkspace, so another
user's IDs find nothing. Writes need the session's CSRF token. Model calls, page reads and PDF
printing are tasks (task_runner.py): a request queues them and returns at once, and the page
follows the task. The single-user app (web.py) is unchanged and is never a fallback for a
failed sign-in: this app refuses to start without Cognito settings.
"""

import argparse
import asyncio
import hashlib
import json
import logging
import os
import secrets
import sys
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.routing import Match

import run_log
import tenant_store
from cv import CVError, rejected_lines, render_html, rewritten_lines
from cv_import import MAX_PDF_BYTES, CVImportError, read_pdf_limited
from cv_status import CANDIDATE_FIELDS, describe_fallbacks, explained
from facts import FactStoreError, parse_fact_refs, tag_finder
from gaps import GAPS_VERSION, coverage, places
from identity import Identity, IdentityError, OIDCSettings
from job_search import SearchError, check_board, fetch_board, parse_board_link, parse_posting_link, prepare_pasted_jd
from job_url import normalize_url
from listings import STALE_AFTER, ListingsError, group_postings, load_starter
from requirement_flow import RequirementError, add_manual_requirements, apply_requirement_decisions
from task_runner import TaskRunner
from tenant_store import Conflict, NotFound, Refused, StoreError, UserWorkspace
from v2_flow import CONSENT, HEAVY, UNITS, auto_decide, cv_language, make_handlers
from workspace import DataFormatError, recorded_data_format, write_atomically


WEB_DIR = Path(__file__).parent / "web"
SESSION_COOKIE = "__Host-feiran"
LOGIN_COOKIE = "__Host-feiran-login"
LOCAL_SESSION_COOKIE = "feiran-session"  # http://localhost testing only: browsers refuse __Host- without https
LOCAL_LOGIN_COOKIE = "feiran-login"
V2_DATA_FORMAT = 2
V2_DATABASE = tenant_store.DATABASE
PUBLIC_PATHS = ("/healthz", "/login", "/auth/callback", "/signed-out")
UNSAFE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
MAX_BODY = 256_000
LANGUAGES = ("en", "zh")
# A board copy younger than this is served from the shared public cache without reading it again.
BOARD_FRESH = timedelta(minutes=30)
BOARD_READS_PER_HOUR = 60
BOARD_READERS = 4
PDF_READERS = 2
CV_CHANGED = "This CV changed after the page showed it (another tab or window changed it). Read it again below, then approve."
PREVIEW_CHANGED = ("<!doctype html><meta charset=\"utf-8\"><p>This CV changed after the page showed it. "
                   "Reload the page to read the current version.</p>")
REFUSED_STATUS = {"queue_full": 429, "quota_exhausted": 429, "too_many_attempts": 429, "busy": 503,
                  "tasks_paused": 503, "quota_unconfigured": 503, "consent_required": 403}
# What each model data flow sends, shown before a user first uses it (tenant_store.CONSENT_STAGES).
PAGE_OUT_OF_DATE = "This page is out of date; reload it"
CONSENT_NOTICES = {
    "upload_parsing": (
        "Reading an uploaded CV: your name, email, phone and links stay on this server. The other lines of the "
        "CV, including school and employer names, are sent to DeepSeek (a third-party model service) so it can "
        "tell sections, entries and bullets apart."),
    "job_processing": (
        "Working on a job (finding its requirements, rewording and adjusting your CV, checking what it shows): "
        "the job description and your CV lines are sent to DeepSeek. Your name, contact details, schools and "
        "employers known from your CV are masked first; a private word that never appears in your CV profile "
        "may still get through, so this is not anonymous."),
}
access_log = logging.getLogger("workbench.access")


class RequestTooLarge(StarletteHTTPException):
    """A request body passed its route's limit while it was being read."""

    def __init__(self, message: str) -> None:
        super().__init__(status_code=413, detail=message)


def _body_limit(path: str) -> tuple[int, str]:
    if path == "/api/cv/upload":
        return MAX_PDF_BYTES, f"The PDF is larger than {MAX_PDF_BYTES // 1_000_000} MB"
    return MAX_BODY, "The request is too large"


class BodyLimit:
    """Counts each request body as it arrives and stops reading it past its route's limit (a PDF
    upload up to its own limit, anything else a few hundred kilobytes), with or without a
    Content-Length: a chunked body is held to the same limit. A declared length over the limit is
    refused before anything is read."""

    def __init__(self, app: Callable) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit, message = _body_limit(scope["path"])
        declared = [value for name, value in scope["headers"] if name == b"content-length"]
        if declared and (len(declared) > 1 or not declared[0].isdigit() or int(declared[0]) > limit):
            await JSONResponse({"error": message}, status_code=413)(scope, receive, send)
            return
        received = 0

        async def counted() -> dict:
            nonlocal received
            event = await receive()
            if event["type"] == "http.request":
                received += len(event.get("body", b""))
                if received > limit:
                    raise RequestTooLarge(message)
            return event

        await self.app(scope, counted, send)


class V2Settings:
    """How the V2 app is reached and how long a session lasts."""

    def __init__(self, *, public_host: str, public_origin: str, session_lifetime: timedelta = timedelta(hours=12),
                 session_idle: timedelta = timedelta(hours=2)) -> None:
        self.public_host = public_host.strip().lower()
        self.public_origin = public_origin.rstrip("/")
        self.secure = self.public_origin.startswith("https://")
        local = self.public_origin.startswith(("http://localhost", "http://127.0.0.1"))
        if not self.secure and not local:
            raise ValueError("V2 must be served over https (http only for localhost testing)")
        self.session_lifetime, self.session_idle = session_lifetime, session_idle
        self.session_cookie = SESSION_COOKIE if self.secure else LOCAL_SESSION_COOKIE
        self.login_cookie = LOGIN_COOKIE if self.secure else LOCAL_LOGIN_COOKIE


class ConfirmRequest(BaseModel):
    refs: list[str]


class EditFactRequest(BaseModel):
    expected_version: int
    text: str
    tags: list[str]


class ContactLink(BaseModel):
    label: str = ""
    url: str


class SaveCVRequest(BaseModel):
    name: str
    location: str = ""
    phone: str = ""
    email: str = ""
    links: list[ContactLink] = []


class DismissRequest(BaseModel):
    id: str


class AddSourceRequest(BaseModel):
    link: str


class StartListingRequest(BaseModel):
    provider: str
    board: str
    job_id: str


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
    expected_version: int  # the requirements version the page showed


class RequirementDecisionRequest(BaseModel):
    confirm: list[str] = []
    exclude: list[str] = []
    expected_version: int


class FindRequirementsRequest(BaseModel):
    expected_version: int


class CVLanguageRequest(BaseModel):
    language: str


class ChangeRequest(BaseModel):
    change_id: str
    undone: bool = True


class ApproveRequest(BaseModel):
    expected_content_sha256: str


class WriteLineRequest(BaseModel):
    place: str
    text: str


class ConsentRequest(BaseModel):
    stage: str
    version: int


class DeleteAccountRequest(BaseModel):
    confirm: str


def _page(title: str, message: str, link: tuple[str, str] | None = None, status: int = 200) -> HTMLResponse:
    """A small page for sign-in outcomes; every value is escaped."""
    action = f'<p><a href="{escape(link[0], quote=True)}">{escape(link[1])}</a></p>' if link else ""
    return HTMLResponse(f'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" '
                        f'content="width=device-width, initial-scale=1"><title>{escape(title)}</title>'
                        f'<link rel="stylesheet" href="/static/style.css"><main class="panel"><h1>{escape(title)}</h1>'
                        f'<p>{escape(message)}</p>{action}</main></html>', status_code=status)


def _language(value: str) -> str:
    if value not in LANGUAGES:
        raise StoreError("CV language must be en or zh")
    return value


def claim_v2_format(folder: Path) -> None:
    """Record that this data folder is in V2's format, so no single-user release opens it again,
    and refuse a folder a newer release has claimed."""
    recorded = recorded_data_format(folder)
    if recorded > V2_DATA_FORMAT:
        raise DataFormatError(f"the data is in format {recorded}, newer than this release's format {V2_DATA_FORMAT}")
    if recorded < V2_DATA_FORMAT:
        write_atomically(Path(folder) / ".workbench-format", f"{V2_DATA_FORMAT}\n".encode("utf-8"))


class BoardReads:
    """Public board reads: at most a few at once for the whole site and a bounded number per user
    per hour; a recent copy in the shared public cache is served without reading again."""

    def __init__(self, fetch: Callable[..., list[dict]], database: Path) -> None:
        self.fetch, self.database = fetch, database
        self.slots = threading.BoundedSemaphore(BOARD_READERS)
        self.recent: dict[str, deque] = {}
        self.lock = threading.Lock()

    def read(self, user_id: str, provider: str, board: str, company: str | None = None) -> dict[str, Any]:
        cached_company, age, count = tenant_store.public_board_age(self.database, provider, board)
        if age is not None and age < BOARD_FRESH:
            return {"company": company or cached_company, "open": count, "new": 0, "closed": 0, "cached": True}
        now = time.monotonic()
        with self.lock:
            reads = self.recent.setdefault(user_id, deque())
            while reads and now - reads[0] > 3600:
                reads.popleft()
            if len(reads) >= BOARD_READS_PER_HOUR:
                raise Refused("Too many company refreshes this hour; try again later", "queue_full")
            reads.append(now)
        if not self.slots.acquire(timeout=30):
            raise Refused("Many companies are being read now; try again in a minute", "busy")
        try:
            postings = self.fetch(board, provider)
        finally:
            self.slots.release()
        name = company or cached_company or next((job["company"] for job in postings if job.get("company")), None) or board
        counts = tenant_store.store_public_board(self.database, provider, board, name, postings)
        return {"company": name, **counts}


def create_v2_app(*, database: Path, identity: Identity, settings: V2Settings,
                  handlers: dict[str, Callable] | None = None, model: Callable[..., dict] | None = None,
                  boards: Callable[..., list[dict]] = fetch_board, starter: Iterable[dict[str, str]] | None = None,
                  ledger: Path | None = None, runner_slots: tuple[int, int] = (2, 1),
                  task_deadline: timedelta = timedelta(minutes=10)) -> FastAPI:
    database = Path(database)
    tenant_store.initialize_store(database)
    starter = list(load_starter() if starter is None else starter)
    ledger = Path(ledger) if ledger else database.parent / tenant_store.DELETION_LEDGER
    tenant_store.ensure_ledger(database, ledger)
    runner_kwargs = {"model": model} if model is not None else {}
    runner = TaskRunner(database, handlers if handlers is not None else make_handlers(),
                        total=runner_slots[0], pdf=runner_slots[1], deadline=task_deadline, **runner_kwargs)
    board_reads = BoardReads(boards, database)
    pdf_readers = threading.BoundedSemaphore(PDF_READERS)
    allowed_hosts = {settings.public_host, "localhost", "127.0.0.1"}

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        runner.start()
        try:
            yield
        finally:
            await run_in_threadpool(runner.stop)

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.runner = runner
    app.state.database = database

    def workspace(request: Request) -> UserWorkspace:
        return UserWorkspace(database, request.state.user_id)

    def route_of(request: Request) -> str:
        route = request.scope.get("route")
        if route is None:
            route = next((item for item in app.router.routes if item.matches(request.scope)[0] == Match.FULL), None)
        return getattr(route, "path", None) or "(no route)"

    # Middlewares: the last declared runs first. Order: log, then guard, then limit the body.
    app.add_middleware(BodyLimit)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        """Host check, session, CSRF and response headers. Only the public paths and static files
        are served without a session; a write needs the session's CSRF token in X-Workbench-Token
        and, when the browser names its origin, this site's origin."""
        host = (request.headers.get("host") or "").rsplit(":", 1)[0].lower()
        if host not in allowed_hosts:
            return JSONResponse({"error": "Unknown host"}, status_code=403)
        path = request.url.path
        request.state.user_id = None
        public = path in PUBLIC_PATHS or path.startswith("/static/")
        if not public:
            token = request.cookies.get(settings.session_cookie)
            session = await run_in_threadpool(tenant_store.resolve_session, database, token)
            if session is None:
                if path == "/":
                    return RedirectResponse("/login?return_to=/", status_code=303)
                response = JSONResponse({"error": "Please sign in again", "code": "signed_out"}, status_code=401)
                return _headers(response)
            if request.method in UNSAFE_METHODS:
                supplied = request.headers.get("x-workbench-token", "")
                origin = request.headers.get("origin")
                if not secrets.compare_digest(supplied, session.csrf_token) or (
                        origin is not None and origin != settings.public_origin):
                    return _headers(JSONResponse({"error": "This page is out of date; reload it"}, status_code=403))
            request.state.user_id = session.user_id
            request.state.csrf = session.csrf_token
        return _headers(await call_next(request))

    def _headers(response: Response) -> Response:
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        if settings.secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        """One line per request: its route (never the path sent), status and time."""
        started, current = time.monotonic(), secrets.token_hex(8)
        run_log.request_id.set(current)
        try:
            response = await call_next(request)
        except Exception:
            run_log.event(access_log, "request", level=logging.ERROR, error=True, route=route_of(request),
                          status=500, duration_ms=run_log.elapsed_ms(started))
            raise
        if not (request.url.path == "/healthz" and response.status_code == 200):
            run_log.event(access_log, "request", level=logging.ERROR if response.status_code >= 500 else logging.INFO,
                          route=route_of(request), status=response.status_code, duration_ms=run_log.elapsed_ms(started))
        response.headers["X-Request-Id"] = current
        return response

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> PlainTextResponse:
        current = run_log.request_id.get()
        return PlainTextResponse("Internal Server Error", status_code=500,
                                 headers={"X-Request-Id": current} if current else None)

    @app.exception_handler(RequestTooLarge)
    async def too_large(_request: Request, exc: RequestTooLarge) -> JSONResponse:
        return JSONResponse({"error": exc.detail}, status_code=413)

    @app.exception_handler(NotFound)
    async def not_found(_request: Request, _exc: NotFound) -> JSONResponse:
        # Missing and other users' objects look the same.
        return JSONResponse({"error": "Not found, or not yours"}, status_code=404)

    @app.exception_handler(Conflict)
    async def conflict(_request: Request, exc: Conflict) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=409)

    @app.exception_handler(Refused)
    async def refused(_request: Request, exc: Refused) -> JSONResponse:
        body: dict[str, Any] = {**exc.detail, "error": str(exc), "code": exc.code}
        headers = {"Retry-After": "60"} if REFUSED_STATUS.get(exc.code) in (429, 503) else None
        return JSONResponse(body, status_code=REFUSED_STATUS.get(exc.code, 403), headers=headers)

    @app.exception_handler(StoreError)
    @app.exception_handler(FactStoreError)
    @app.exception_handler(CVError)
    @app.exception_handler(SearchError)
    @app.exception_handler(RequirementError)
    @app.exception_handler(ListingsError)
    @app.exception_handler(ValueError)
    async def domain_error(_request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=400)

    # -- signing in and out ----------------------------------------------------------------

    def _cookie(response: Response, name: str, value: str, max_age: int | None = None) -> None:
        response.set_cookie(name, value, max_age=max_age, path="/", secure=settings.secure, httponly=True,
                            samesite="lax")

    @app.get("/healthz")
    def health() -> JSONResponse:
        """Whether the database answers; nothing personal is shown."""
        try:
            tenant_store.get_settings(database)
        except StoreError:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return JSONResponse({"status": "ok"})

    @app.get("/login", response_model=None)
    def login(return_to: str = "/") -> Response:
        try:
            url, binding = identity.begin(return_to)
        except Refused:
            return _page("Try again soon", "Too many sign-ins are in progress. Try again in a few minutes.",
                         ("/login", "Sign in"), 503)
        response = RedirectResponse(url, status_code=303)
        _cookie(response, settings.login_cookie, binding, max_age=600)
        return response

    @app.get("/auth/callback", response_model=None)
    def callback(request: Request, code: str = "", state: str = "", error: str = "") -> Response:
        if error:
            return _page("Not signed in", "Sign-in was cancelled or refused.", ("/login", "Sign in again"), 400)
        try:
            issuer, subject, return_to = identity.finish(code=code, state=state,
                                                         binding=request.cookies.get(settings.login_cookie, ""))
            user_id = tenant_store.sign_in(database, issuer=issuer, subject=subject, starter=starter)
        except IdentityError as exc:
            run_log.event(access_log, "sign_in_refused", level=logging.WARNING, reason=exc.reason)
            return _page("Not signed in", str(exc), ("/login", "Sign in again"), 400)
        except Refused as exc:
            run_log.event(access_log, "sign_in_refused", level=logging.WARNING, reason=exc.code)
            messages = {"registration_closed": "This pilot is not taking new accounts right now.",
                        "registration_full": "This pilot is full.",
                        "account_disabled": "This account is disabled. Contact the pilot organizer.",
                        "account_deleted": "This account was deleted."}
            return _page("Not signed in", messages.get(exc.code, str(exc)), None, 403)
        tenant_store.revoke_session(database, request.cookies.get(settings.session_cookie))
        token, _session = tenant_store.create_session(database, user_id, lifetime=settings.session_lifetime,
                                                      idle=settings.session_idle)
        response = RedirectResponse(return_to, status_code=303)
        _cookie(response, settings.session_cookie, token)
        response.delete_cookie(settings.login_cookie, path="/", secure=settings.secure, httponly=True, samesite="lax")
        run_log.event(access_log, "signed_in")
        return response

    @app.get("/signed-out")
    def signed_out() -> HTMLResponse:
        return _page("Signed out", "You are signed out of Feiran.", ("/login", "Sign in"))

    @app.post("/logout")
    def logout(request: Request) -> JSONResponse:
        """End this session here first, then send the browser to Cognito's sign-out."""
        tenant_store.revoke_session(database, request.cookies.get(settings.session_cookie))
        response = JSONResponse({"logout_url": identity.logout_url()})
        response.delete_cookie(settings.session_cookie, path="/", secure=settings.secure, httponly=True, samesite="lax")
        return response

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> str:
        page = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        page = page.replace("__WORKBENCH_TOKEN__", request.state.csrf)
        return page.replace('<meta name="workbench-mode" content="local">', '<meta name="workbench-mode" content="v2">')

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    # -- account ---------------------------------------------------------------------------

    @app.get("/api/me")
    def me(request: Request) -> dict:
        user = workspace(request)
        return {"consents": user.consents(), "usage": user.usage_today(),
                "notices": {stage: {"version": tenant_store.CONSENT_STAGES[stage], "text": text}
                            for stage, text in CONSENT_NOTICES.items()}}

    @app.post("/api/consent")
    def consent(request: Request, body: ConsentRequest) -> dict:
        return {"consents": workspace(request).give_consent(body.stage, body.version)}

    @app.post("/api/account/delete")
    def delete_my_account(request: Request, body: DeleteAccountRequest) -> JSONResponse:
        """Delete this account and all its data now; backups expire on their own schedule."""
        if body.confirm != "DELETE":
            raise StoreError('Type DELETE to confirm')
        tenant_store.delete_account(database, request.state.user_id, ledger)
        response = JSONResponse({"deleted": True, "logout_url": identity.logout_url()})
        response.delete_cookie(settings.session_cookie, path="/", secure=settings.secure, httponly=True, samesite="lax")
        return response

    # -- tasks -----------------------------------------------------------------------------

    def client_key(request: Request) -> str | None:
        """The request key the page sent for this action of the user's (Idempotency-Key), if any."""
        supplied = request.headers.get("idempotency-key")
        if supplied is None:
            return None
        if not (0 < len(supplied) <= 128 and supplied.isprintable()):
            raise StoreError("Invalid Idempotency-Key")
        return f"client:{supplied}"

    def submit(request: Request, operation: str, intent: dict, *, key: str, context: dict | None = None,
               job_id: str | None = None, follow_up: bool = False) -> dict:
        """Queue one operation for this user. ``intent`` is what the page asked for; ``context`` what
        the server worked out from the state of now (the CV to build on, say). The page sends an
        Idempotency-Key for each of the user's actions, and the same key with the same intent finds
        the same task again, even after it finished; without one, ``key`` names the request from
        the state it was made in. A ``follow_up`` (the CV prepared again after a change was saved)
        is not what the page's key names, so it is always named by that state. The page's key is
        bound to the intent in the same transaction (UserWorkspace.submit_task). The runner is
        woken at once."""
        named = None if follow_up else client_key(request)
        task = workspace(request).submit_task(operation, key=named or key, bind=named is not None,
                                              request=intent, context=context, units=UNITS[operation],
                                              heavy=HEAVY.get(operation, "model"), job_id=job_id,
                                              consent=CONSENT.get(operation))
        runner.wake()
        return task

    def queued(task: dict, **extra: Any) -> JSONResponse:
        return JSONResponse({"task": task, **extra}, status_code=202)

    def then_prepare(request: Request, job_id: str, language: str, view: dict) -> JSONResponse:
        """After a change the CV depends on, prepare the CV again; the change stays saved even
        when that cannot be queued now, and the page says why."""
        try:
            task = prepare_task(request, job_id, language, follow_up=True)
        except Refused as exc:
            return JSONResponse({**view, "task_error": {"code": exc.code, "error": str(exc)}})
        return JSONResponse({**job_view(workspace(request), job_id), "task": task}, status_code=202)

    def prepare_task(request: Request, job_id: str, language: str, *, follow_up: bool = False) -> dict:
        inputs = workspace(request).job_snapshot(job_id)
        head = inputs["cv"][language]
        base = head["material"]["material_id"] if head else None
        facts = hashlib.sha256(json.dumps(inputs["confirmed"]).encode()).hexdigest()[:16]
        requirements = (inputs["requirements"] or {}).get("version")
        return submit(request, "prepare_cv", {"job_id": job_id, "language": language}, context={"base_head": base},
                      key=f"{job_id}:{language}:{base}:{requirements}:{facts}", job_id=job_id, follow_up=follow_up)

    @app.get("/api/tasks/{task_id}")
    def task(request: Request, task_id: str) -> dict:
        return {"task": workspace(request).get_task(task_id)}

    @app.post("/api/tasks/{task_id}/cancel")
    def cancel(request: Request, task_id: str) -> dict:
        return {"task": workspace(request).cancel_task(task_id)}

    @app.post("/api/notices/dismiss")
    def dismiss(request: Request, body: DismissRequest) -> dict:
        workspace(request).dismiss_task(body.id)
        return {"dismissed": True}

    # -- facts and CV uploads --------------------------------------------------------------

    @app.get("/api/facts")
    def facts(request: Request) -> dict:
        return {"facts": workspace(request).list_facts()}

    @app.get("/api/cv/languages")
    def languages(request: Request) -> dict:
        return {"languages": workspace(request).cv_languages()}

    @app.post("/api/facts/{fact_id}/edit")
    def edit_fact(request: Request, fact_id: str, body: EditFactRequest) -> dict:
        updated = workspace(request).revise_fact(fact_id, expected_version=body.expected_version, text=body.text,
                                                 tags=body.tags)
        return {"fact": updated, "changed": updated["version"] != body.expected_version}

    @app.post("/api/facts/confirm")
    def confirm(request: Request, body: ConfirmRequest) -> dict:
        user = workspace(request)
        refs = parse_fact_refs(body.refs)
        pending = {fact["id"] for fact in user.list_facts() if fact["status"] == "pending"}
        confirmed = user.confirm_facts(refs)
        return {"changed_count": sum(fact["id"] in pending for fact in confirmed)}

    @app.post("/api/cv/upload")
    async def upload_cv(request: Request, language: str = "") -> JSONResponse:
        """Read the PDF here (in a limited child process), then queue its structuring by the model."""
        language = _language(language)
        data = bytearray()
        async for chunk in request.stream():
            data += chunk
            if len(data) > MAX_PDF_BYTES:
                raise CVImportError(f"PDF 不能超过 {MAX_PDF_BYTES // 1_000_000} MB")
        user = workspace(request)
        if "upload_parsing" not in await run_in_threadpool(user.consents):
            raise Refused("Agree to how your data is used first", "consent_required", {"stage": "upload_parsing"})

        def parse() -> dict:
            if not pdf_readers.acquire(timeout=30):
                raise Refused("Many PDFs are being read now; try again in a minute", "busy")
            try:
                return read_pdf_limited(bytes(data))
            finally:
                pdf_readers.release()
        pdf = await run_in_threadpool(parse)
        digest = hashlib.sha256(bytes(data)).hexdigest()
        task = await run_in_threadpool(lambda: submit(request, "upload_cv", {"language": language, "source_sha256": digest},
                                                      context={"lines": pdf["lines"], "links": pdf["links"]},
                                                      key=f"upload:{secrets.token_hex(8)}"))
        return queued(task)

    @app.get("/api/cv/uploads/{upload_id}")
    def upload(request: Request, upload_id: str) -> dict:
        user = workspace(request)
        proposal = user.get_upload(upload_id)
        known = {fact["text"] for fact in user.list_facts()}
        for section in proposal["sections"]:
            for entry in section["entries"]:
                for fact in entry["facts"]:
                    fact["known"] = fact["text"] in known
        return {**{key: value for key, value in proposal.items() if key != "structuring"},
                "has_profile": proposal["language"] in user.cv_languages()}

    @app.post("/api/cv/uploads/{upload_id}/save")
    def save_upload(request: Request, upload_id: str, body: SaveCVRequest) -> dict:
        return workspace(request).commit_upload(upload_id, body.model_dump())

    @app.delete("/api/cv/uploads/{upload_id}")
    def cancel_upload(request: Request, upload_id: str) -> dict:
        workspace(request).delete_upload(upload_id)
        return {"cancelled": True}

    # -- followed companies and ranked postings --------------------------------------------

    def sources_of(user: UserWorkspace) -> list[dict]:
        stale_before = time.time() - STALE_AFTER.total_seconds()
        result = []
        for board in user.followed_boards():
            fetched = board["fetched_at"]
            stale = fetched is None or _epoch(fetched) < stale_before
            result.append({**board, "stale": stale})
        return result

    @app.get("/api/sources")
    def sources(request: Request) -> dict:
        return {"sources": sources_of(workspace(request))}

    @app.post("/api/sources")
    def follow(request: Request, body: AddSourceRequest) -> dict:
        user = workspace(request)
        provider, board = parse_board_link(body.link)
        if user.follows(provider, board):
            raise Conflict(f"Already following {user.follows(provider, board)['company']}")
        read = board_reads.read(request.state.user_id, provider, board)
        user.follow_board(provider, board, read["company"])
        return {"added": {"company": read["company"], "open": read.get("open"), "new": read.get("new"),
                          "closed": read.get("closed")}, "sources": sources_of(user)}

    @app.delete("/api/sources/{provider}/{board}")
    def unfollow(request: Request, provider: str, board: str) -> dict:
        user = workspace(request)
        user.unfollow_board(*check_board(provider, board))
        return {"sources": sources_of(user)}

    @app.post("/api/sources/{provider}/{board}/refresh")
    def refresh(request: Request, provider: str, board: str) -> dict:
        user = workspace(request)
        provider, board = check_board(provider, board)
        followed = user.follows(provider, board)
        if followed is None:
            raise NotFound("Not following this company")
        try:
            read = board_reads.read(request.state.user_id, provider, board, followed["company"])
        except SearchError as exc:
            user.record_board_read(provider, board, str(exc))
            raise ListingsError(f"{followed['company']}：{exc}") from exc
        user.record_board_read(provider, board, None)
        return read

    @app.get("/api/listings")
    def listings(request: Request, title: str = "", location: str = "", limit: int = 50, offset: int = 0,
                 hide_senior: bool = False, region: str = "all") -> dict:
        user = workspace(request)
        tags = list(dict.fromkeys(tag for fact in user.list_facts() if fact["status"] == "confirmed"
                                  for tag in fact["tags"]))
        rows = user.ranking_rows(tags, tag_finder(tags))
        ranked = group_postings(rows, len({tag.casefold() for tag in tags}), title, location, limit, offset,
                                hide_senior, region)
        started = {summary["source"]: summary["job_id"] for summary in reversed(user.job_summaries())
                   if summary["source"]}
        for group in ranked["listings"]:
            group["started_job"] = next((started[item["source"]] for item in group["postings"]
                                         if item["source"] in started), None)
        return ranked

    @app.post("/api/listings/start", response_model=None)
    def start_listing(request: Request, body: StartListingRequest) -> dict | JSONResponse:
        user = workspace(request)
        posting = user.followed_posting(body.provider, body.board, body.job_id)
        intent = {"provider": posting["provider"], "board": posting["board"], "posting_id": posting["job_id"]}
        existing = user.find_job(source=posting["source"])
        if existing:  # answered by the job already made from it; the request key is bound all the same
            user.bind_request("start_listing", client_key(request), tenant_store.input_digest(intent))
            return {"job_id": existing, "existing": True}
        task = submit(request, "start_listing", intent, context={"company": posting["company"]},
                      key=f"listing:{posting['provider']}:{posting['board']}:{posting['job_id']}")
        return queued(task)

    # -- jobs ------------------------------------------------------------------------------

    @app.get("/api/jobs")
    def jobs(request: Request) -> dict:
        return {"jobs": workspace(request).job_summaries()}

    @app.post("/api/jobs")
    def new_job(request: Request, body: NewJobRequest) -> JSONResponse:
        """Save a pasted job and find its requirements. The same words sent again (a resend whose
        answer was lost, or the same paste later) give the same job as first captured, and the
        work already under way or done for it; the same request key with other words is refused
        and saves nothing (UserWorkspace.paste_job)."""
        user = workspace(request)
        selected = prepare_pasted_jd(body.text, body.title, body.company, body.url, body.location)
        digest = hashlib.sha256(json.dumps(body.model_dump(), sort_keys=True).encode()).hexdigest()
        outcome = user.paste_job(selected["jd"], request_id=f"paste:{digest}", key=client_key(request),
                                 input_sha256=digest, units=UNITS["prepare_job"], consent=CONSENT["prepare_job"])
        job_id = outcome["job"]["job_id"]
        if outcome["refused"] is not None:
            refused = outcome["refused"]
            return JSONResponse({"job_id": job_id, "task_error": {"code": refused.code, "error": str(refused)}})
        if outcome["task"] is None:  # its requirements are found already: nothing new to do
            return JSONResponse({"job_id": job_id, "existing": True})
        runner.wake()
        return queued(outcome["task"], job_id=job_id)

    @app.post("/api/jobs/from-url", response_model=None)
    def new_job_from_url(request: Request, body: JobURLRequest) -> dict | JSONResponse:
        user = workspace(request)
        url = normalize_url(body.url)
        try:
            identity_key = parse_posting_link(url)
        except SearchError:
            identity_key = None
        existing = user.find_job(identity=identity_key, url=url)
        if existing:  # answered by the job already made from it; the request key is bound all the same
            user.bind_request("job_from_url", client_key(request), tenant_store.input_digest({"url": url}))
            return {"job_id": existing, "existing": True}
        task = submit(request, "job_from_url", {"url": url},
                      context={"identity": list(identity_key) if identity_key else None}, key=f"url:{url}")
        return queued(task)

    def job_view(user: UserWorkspace, job_id: str) -> dict:
        return build_job_view(user.job_snapshot(job_id))

    @app.get("/api/jobs/{job_id}")
    def job(request: Request, job_id: str) -> dict:
        return job_view(workspace(request), job_id)

    def current_requirements(user: UserWorkspace, job_id: str, shown: int) -> dict:
        """The job's requirements, as long as they are still the version the page showed: a page
        left open elsewhere cannot overwrite a newer decision (the save checks it once more)."""
        requirements = user.requirements(job_id)
        if requirements is None:
            raise StoreError("请先完成上一步（找到这个岗位的要求）")
        if requirements["version"] != shown:
            raise Conflict(PAGE_OUT_OF_DATE)
        return requirements

    @app.post("/api/jobs/{job_id}/requirements/add")
    def add_requirement(request: Request, job_id: str, body: AddRequirementRequest) -> JSONResponse:
        user = workspace(request)
        current = current_requirements(user, job_id, body.expected_version)
        decided = current.get("decided") or {}
        # What the user decided stays; only they ever exclude a line.
        kept = {item["id"]: item["status"] for item in decided.get("requirement_candidates", [])
                if item.get("decided_by") == "user" or item.get("status") == "excluded"}
        candidates = current["candidates"]
        added = add_manual_requirements(candidates, [body.text])
        for item in added["requirement_candidates"][len(candidates["requirement_candidates"]):]:
            kept[item["id"]] = "confirmed"  # the user added it as a requirement
        user.save_requirements(job_id, added, auto_decide(added, kept), expected_version=body.expected_version)
        view = job_view(user, job_id)
        return then_prepare(request, job_id, view["language"], view)

    @app.post("/api/jobs/{job_id}/requirements/find")
    def find_requirements(request: Request, job_id: str, body: FindRequirementsRequest) -> JSONResponse:
        """Find the requirements again (decisions start over, as in the single-user app); only from
        a page that shows the current version, and the task saves nothing if they change meanwhile."""
        user = workspace(request)
        current = user.requirements(job_id)
        if (current["version"] if current else 0) != body.expected_version:
            raise Conflict(PAGE_OUT_OF_DATE)
        version = body.expected_version
        task = submit(request, "prepare_job", {"job_id": job_id, "base_requirements_version": version},
                      key=f"prepare-job:{job_id}:{version}", job_id=job_id)
        return queued(task, **job_view(user, job_id))

    @app.post("/api/jobs/{job_id}/requirements/decide")
    def decide_requirements(request: Request, job_id: str, body: RequirementDecisionRequest) -> JSONResponse:
        user = workspace(request)
        current = current_requirements(user, job_id, body.expected_version)
        decided = apply_requirement_decisions(current["candidates"], set(body.confirm), set(body.exclude))
        user.save_requirements(job_id, current["candidates"], decided, expected_version=body.expected_version)
        view = job_view(user, job_id)
        return then_prepare(request, job_id, view["language"], view)

    @app.post("/api/jobs/{job_id}/language")
    def choose_language(request: Request, job_id: str, body: CVLanguageRequest) -> dict:
        user = workspace(request)
        language = _language(body.language)
        if language not in user.cv_languages():
            raise CVError("请先上传这个语言的简历", reason="no_profile")
        user.set_note(job_id, "cv-language", {"language": language})
        return job_view(user, job_id)

    def redo(request: Request, job_id: str, language: str, operation: str) -> JSONResponse:
        user = workspace(request)
        snapshot = user.job_snapshot(job_id)
        head = snapshot["cv"][_language(language)]
        if head is None:
            raise StoreError("请先准备这个岗位的简历")
        base = head["material"]["material_id"]
        task = submit(request, operation, {"job_id": job_id, "language": language}, context={"base_head": base},
                      key=f"{job_id}:{language}:{base}", job_id=job_id)
        return queued(task, **build_job_view(snapshot))

    @app.post("/api/jobs/{job_id}/cv/{language}/prepare")
    def cv_prepare(request: Request, job_id: str, language: str) -> JSONResponse:
        task = prepare_task(request, job_id, _language(language))
        return queued(task, **job_view(workspace(request), job_id))

    @app.post("/api/jobs/{job_id}/cv/{language}/tailor")
    def cv_tailor(request: Request, job_id: str, language: str) -> JSONResponse:
        return redo(request, job_id, language, "tailor_cv")

    @app.post("/api/jobs/{job_id}/cv/{language}/plan")
    def cv_plan(request: Request, job_id: str, language: str) -> JSONResponse:
        return redo(request, job_id, language, "plan_cv")

    @app.post("/api/jobs/{job_id}/cv/{language}/change")
    def cv_change(request: Request, job_id: str, language: str, body: ChangeRequest) -> dict:
        user = workspace(request)
        user.change_cv(job_id, _language(language), body.change_id, body.undone)
        return job_view(user, job_id)

    @app.post("/api/jobs/{job_id}/cv/{language}/approve", response_model=None)
    def cv_approve(request: Request, job_id: str, language: str, body: ApproveRequest) -> dict | JSONResponse:
        user = workspace(request)
        try:
            user.approve(job_id, _language(language), body.expected_content_sha256)
        except Conflict as exc:
            message = CV_CHANGED if "page showed" in str(exc) else str(exc)
            return JSONResponse({"error": message}, status_code=409)
        return job_view(user, job_id)

    @app.post("/api/jobs/{job_id}/cv/{language}/export")
    def cv_export(request: Request, job_id: str, language: str) -> JSONResponse:
        user = workspace(request)
        inputs = user.export_inputs(job_id, _language(language))
        task = submit(request, "export_pdf", {"job_id": job_id, "language": language},
                      context={"approval_id": inputs["approval_id"]}, key=f"export:{inputs['approval_id']}",
                      job_id=job_id)
        return queued(task, **job_view(user, job_id))

    @app.post("/api/jobs/{job_id}/gaps")
    def check_gaps(request: Request, job_id: str) -> JSONResponse:
        user = workspace(request)
        snapshot = user.job_snapshot(job_id)
        view = build_job_view(snapshot)
        head = snapshot["cv"][view["language"]]
        if head is None:
            raise StoreError("请先准备这个岗位的简历，再检查缺口")
        version = snapshot["gaps"]["version"] if snapshot["gaps"] else 0
        facts = hashlib.sha256(json.dumps(snapshot["confirmed"]).encode()).hexdigest()[:16]
        task = submit(request, "check_gaps", {"job_id": job_id},
                      context={"language": view["language"], "base_gaps_version": version},
                      key=f"gaps:{job_id}:{head['material']['material_id']}:{version}:{facts}", job_id=job_id)
        return queued(task, **view)

    def gap_change(request: Request, job_id: str, change: Callable[[UserWorkspace], dict]) -> JSONResponse:
        user = workspace(request)
        changed = change(user)
        view = job_view(user, job_id)
        return then_prepare(request, job_id, changed["language"], view)

    @app.post("/api/jobs/{job_id}/gaps/{requirement_id}/accept")
    def accept_suggestion(request: Request, job_id: str, requirement_id: str) -> JSONResponse:
        return gap_change(request, job_id, lambda user: user.accept_gap(job_id, requirement_id))

    @app.post("/api/jobs/{job_id}/gaps/{requirement_id}/write")
    def write_own_line(request: Request, job_id: str, requirement_id: str, body: WriteLineRequest) -> JSONResponse:
        return gap_change(request, job_id, lambda user: user.write_gap_line(job_id, requirement_id, body.place, body.text))

    @app.post("/api/jobs/{job_id}/gaps/{requirement_id}/decline")
    def decline_suggestion(request: Request, job_id: str, requirement_id: str) -> dict:
        user = workspace(request)
        user.decline_gap(job_id, requirement_id)
        return job_view(user, job_id)

    # -- preview and download --------------------------------------------------------------

    @app.get("/preview/{job_id}/{language}", response_class=HTMLResponse, response_model=None)
    def preview(request: Request, job_id: str, language: str, v: str = "") -> str | HTMLResponse:
        try:
            document, final = workspace(request).preview(job_id, _language(language), v or None)
        except Conflict:
            return HTMLResponse(PREVIEW_CHANGED, status_code=409)
        return render_html(document, final=final)

    @app.get("/download/{job_id}/{language}.pdf")
    def download(request: Request, job_id: str, language: str) -> Response:
        data = workspace(request).final_pdf(job_id, _language(language))
        return Response(data, media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="CV-{language.upper()}.pdf"'})

    return app


def _epoch(stamp: str) -> float:
    return datetime.fromisoformat(stamp).timestamp()


def build_job_view(snapshot: dict) -> dict:
    """What the job page needs, in the single-user page's shape, from one consistent snapshot."""
    job, requirements = snapshot["job"], snapshot["requirements"]
    jd = {key: value for key, value in job["jd"].items() if key != "raw_content"}
    language = cv_language(snapshot["notes"].get("cv-language"), jd["text"], snapshot["cv_languages"])
    steps = ["input"]
    view: dict[str, Any] = {"job_id": job["job_id"], "jd": jd, "language": language,
                            "cv_languages": snapshot["cv_languages"],
                            "requirements_version": requirements["version"] if requirements else 0}
    decided = requirements.get("decided") if requirements else None
    shown = decided or (requirements or {}).get("candidates")
    if shown:
        steps.append("candidates")
        view["extraction"] = explained(shown.get("requirement_extraction"))
        view["candidates"] = [{field: item.get(field) for field in CANDIDATE_FIELDS}
                              for item in shown["requirement_candidates"]]
    if decided:
        steps.append("decided")
        view["selected_requirements"] = decided["selected_requirements"]
    view["cv"] = {}
    for lang in LANGUAGES:
        cv = _cv_view(snapshot, lang)
        view["cv"][lang] = cv
        if cv["head"]:
            steps.append(f"cv-{cv['head']}-{lang}")
        if cv["final_pdf"]:
            steps.append(f"cv-final-{lang}")
    gaps = snapshot["gaps"]
    if gaps:
        steps.append("gaps")
        view["gaps"] = _gaps_view(snapshot, gaps["gaps"]) if gaps["gaps"].get("language") == language else {"outdated": True}
    view["steps"] = steps
    view["tasks"] = snapshot["tasks"]
    return view


def _cv_view(snapshot: dict, language: str) -> dict:
    cv = snapshot["cv"][language]
    status = snapshot["notes"].get(f"cv-status-{language}")
    if cv is None:
        view: dict[str, Any] = {"head": None, "content_sha256": None, "final_pdf": False, "stale": False}
        if status and status.get("draft_material_id") is None:
            view["stages"] = status["stages"]
        return view
    material, approval = cv["material"], cv["approval"]
    head = material["draft"]
    view = {"head": "approved" if approval else material["stage"], "content_sha256": material["content_sha256"],
            "final_pdf": cv["final_pdf"], "stale": not material["inputs_current"],
            "language_fallbacks": describe_fallbacks(cv["draft"])}
    if status and status.get("draft_material_id") == cv["draft_id"]:
        view["stages"] = status["stages"]
    if "tailoring" in head:
        tailoring = head["tailoring"]
        view["tailoring"] = {key: tailoring.get(key) for key in ("model", "accepted", "rejected", "usage")}
        view["rewrites"] = rewritten_lines(head)
        view["rejected"] = rejected_lines(head)
    if "plan" in head:
        undone = set(head["plan"]["undone"])
        view["changes"] = [{**item, "undone": item["id"] in undone} for item in head["plan"]["changes"]]
    if approval:
        view["approved_at"] = approval["approved_at"]
    return view


def _gaps_view(snapshot: dict, gaps: dict) -> dict:
    cv = snapshot["cv"].get(gaps.get("language", "en"))
    if gaps.get("gaps_version") != GAPS_VERSION or cv is None:
        return {"outdated": True}
    head = cv["material"]["draft"]
    return {**coverage(gaps, head, snapshot["confirmed"]), "language": gaps["language"],
            "created_at": gaps["created_at"],
            "places": [{key: place[key] for key in ("id", "kind", "where")} for place in places(head)],
            "evidence_check": explained(gaps.get("evidence_check")), "suggesting": explained(gaps.get("suggesting"))}


def main(argv: list[str] | None = None, environ: Mapping[str, str] = os.environ) -> int:
    """Start V2. It refuses to start without Cognito settings, on a folder that is not the data
    volume, or on a single-user data folder that has not been migrated (v2_migrate.py)."""
    parser = argparse.ArgumentParser(description="Feiran V2: signed-in users, one server")
    parser.add_argument("--host", default=environ.get("WORKBENCH_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(environ.get("WORKBENCH_PORT", "8765")))
    parser.add_argument("--data", type=Path, default=Path(environ.get("WORKBENCH_DATA", ".local")))
    parser.add_argument("--json-logs", action="store_true", default=environ.get("WORKBENCH_LOG_FORMAT") == "json")
    args = parser.parse_args(argv)
    log = logging.getLogger("workbench")
    if args.json_logs:
        run_log.configure(sys.stderr)
    else:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    def refuse(reason: str) -> int:
        if args.json_logs:
            run_log.event(log, "refused_to_start", level=logging.ERROR, reason=reason)
        else:
            print(f"不启动：{reason}", file=sys.stderr)
        return 2

    try:
        oidc = OIDCSettings.from_environment(environ)
    except ValueError as exc:
        return refuse(str(exc))
    if oidc is None:
        return refuse("V2 需要 Cognito 设置（WORKBENCH_OIDC_*）；不会退回单用户登录")
    data = args.data
    if not (data / ".workbench-data").is_file() and environ.get("WORKBENCH_REQUIRE_DATA") == "1":
        return refuse("数据目录中没有 .workbench-data：数据卷可能没有挂载")
    database = data / V2_DATABASE
    if not database.exists() and ((data / "workbench.db").exists() or (data / "jobs").exists()):
        return refuse("这个数据目录还是单用户格式；先用 v2_migrate.py 迁移，再启动 V2")
    try:
        data.mkdir(parents=True, exist_ok=True)
        claim_v2_format(data)
        settings = V2Settings(public_host=environ.get("WORKBENCH_PUBLIC_HOST", "localhost"),
                              public_origin=oidc.public_origin)
        app = create_v2_app(database=database, identity=Identity(oidc, database), settings=settings)
    except (DataFormatError, ValueError, StoreError) as exc:
        return refuse(str(exc))
    import uvicorn

    run_log.event(log, "listening", port=args.port)
    uvicorn.run(app, host=args.host, port=args.port, access_log=False, workers=1,
                **({"log_config": None} if args.json_logs else {}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
