"""Web UI and web chat. Start: uvicorn app.main:app"""
import asyncio
import base64
import binascii
import hmac
import json
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from markdown_it import MarkdownIt
from markupsafe import Markup

from . import auth, company, config, db, i18n, notices, runner, sandbox, usage
from .conversations import Conversations
from .errors import Invalid, Problem, Rejected, Unknown
from .personnel import Personnel
from .scheduler import Scheduler

HERE = Path(__file__).parent
# How many conversations the card shows. All of them are under /team/{id}/conversations.
CONVERSATIONS_ON_CARD = 10
conversations = Conversations(runner.create())
personnel = Personnel(conversations)
scheduler = Scheduler(conversations)


@asynccontextmanager
async def lifespan(app: FastAPI):
    company.ensure()
    db.setup()
    company.sync_procedures()
    conversations.clean_up()
    watchers = [asyncio.create_task(conversations.watch()), asyncio.create_task(scheduler.watch())]
    yield
    for watcher in watchers:
        watcher.cancel()


app = FastAPI(title="Ancroo Crew", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")

# "js-default" lets no raw HTML through. Answers of an LLM never land unfiltered in the page.
_markdown = MarkdownIt("js-default")
templates.env.filters["markdown"] = lambda text: Markup(_markdown.render(text or ""))


def _plain(text: str) -> Markup:
    """A human's message as typed: line breaks, tabs and "1." stay. Only the marker of an addressed colleague is bold."""
    text = text or ""
    if text.startswith("**→ ") and ":** " in text:
        marker, rest = text[2:].split(":** ", 1)
        return Markup("<strong>{}:</strong> {}").format(marker, rest)
    return Markup.escape(text)


templates.env.filters["plain"] = _plain
# The browser shall not take a changed style.css or chat.js from its cache.
templates.env.globals["style_version"] = int((HERE / "static" / "style.css").stat().st_mtime)
templates.env.globals["script_version"] = int((HERE / "static" / "chat.js").stat().st_mtime)


def _number(n, lang: str | None, digits: int = 0) -> str:
    """A number with the separators of the language: 1.234.567,89 or 1,234,567.89"""
    text = f"{n or 0:,.{digits}f}"
    return text.replace(",", " ").replace(".", ",").replace(" ", ".") if lang == "de" else text


def _short(n, lang: str | None) -> str:
    """Large token counts in short: 950, 12.3k, 4.1M"""
    n = n or 0
    if n >= 1e6:
        return i18n.t(lang, "fmt.million", n=_number(n / 1e6, lang, 1))
    if n >= 1e3:
        return i18n.t(lang, "fmt.thousand", n=_number(n / 1e3, lang, 1))
    return _number(n, lang)


def _when(t, lang: str | None, key: str) -> str:
    return datetime.fromtimestamp(t).strftime(i18n.t(lang, key)) if t else ""


def _meeting(stamp: str, lang: str | None) -> str:
    """Meetings and transcripts are named YYYY-MM-DD-HHMM, days YYYY-MM-DD."""
    if not stamp:
        return ""
    day = datetime.strptime(stamp[:10], "%Y-%m-%d").strftime(i18n.t(lang, "fmt.date"))
    return day + (f" {stamp[11:13]}:{stamp[13:15]}" if len(stamp) > 10 else "")


@pass_context
def _t(context, key: str, **params) -> str:
    return i18n.t(context.get("lang"), key, **params)


@pass_context
def _tn(context, key: str, n: int, **params) -> str:
    return i18n.tn(context.get("lang"), key, n, **params)


templates.env.globals["t"] = _t
templates.env.globals["tn"] = _tn
templates.env.filters["number"] = pass_context(lambda c, n, digits=0: _number(n, c.get("lang"), digits))
templates.env.filters["usd"] = pass_context(lambda c, n: _number(n, c.get("lang"), 2) + " $")
templates.env.filters["short"] = pass_context(lambda c, n: _short(n, c.get("lang")))
templates.env.filters["datetime"] = pass_context(lambda c, t: _when(t, c.get("lang"), "fmt.datetime"))
templates.env.filters["clock"] = pass_context(lambda c, t: _when(t, c.get("lang"), "fmt.time"))
templates.env.filters["meeting"] = pass_context(lambda c, s: _meeting(s, c.get("lang")))
def _diff_html(text: str) -> Markup:
    """A unified diff with a class per line, as the page colours it: added, removed, location."""
    lines = []
    for line in (text or "").splitlines():
        kind = "loc" if line.startswith(("+++", "---", "@@")) else "add" if line.startswith("+") else \
            "del" if line.startswith("-") else ""
        lines.append(Markup('<span class="{}">{}\n</span>').format(kind, line) if kind else Markup("{}\n").format(line))
    return Markup("").join(lines)


templates.env.filters["diff"] = _diff_html
# The note of an own change to the profile shows buttons by the state of the change.
templates.env.globals["profile_change"] = conversations.profile_change


def lang_of(request: Request) -> str:
    """The language of whoever is asking: their profile, before the login their browser."""
    person_id = request.state.person
    if person_id:
        return company.person_language(person_id)
    if request.query_params.get("lang") in i18n.LANGUAGES:
        return request.query_params["lang"]
    return i18n.from_header(request.headers.get("accept-language"))


def page(request: Request, name: str, data: dict) -> HTMLResponse:
    me = None
    if request.state.person:
        try:
            me = company.person(request.state.person)
        except Unknown:
            pass
    logo = company.logo()
    return templates.TemplateResponse(request, name, {"lang": lang_of(request), "me": me,
                                                      "unread": notices.unread(request.state.person) if me else 0,
                                                      "company_name": company.company_name(),
                                                      "claim": company.claim(),
                                                      "logo_version": logo.stat().st_mtime_ns if logo else None,
                                                      "sandbox_on": sandbox.enabled(),
                                                      **data})


def problem(request: Request, error: Problem) -> HTTPException:
    return HTTPException(404 if isinstance(error, Unknown) else 409, error.text(lang_of(request)))


def hint(request: Request, what: str, error: str | None) -> str | None:
    """A note for the human if the Git commit failed. What they did is done anyway."""
    lang = lang_of(request)
    return i18n.t(lang, "hint.git_failed", what=i18n.t(lang, f"hint.{what}"), error=error) if error else None


def _foreign_site(request: Request) -> bool:
    """Whether a request comes from a site other than this UI.

    A form on any web page could otherwise approve tools or dismiss employees with the cookie of a logged-in human.
    Modern browsers say so in Sec-Fetch-Site, older ones only in Origin. Whoever sends neither is no browser, e.g. a
    script on the own machine.
    """
    site = request.headers.get("sec-fetch-site")
    if site:
        # "same-site" would also be another service on 127.0.0.1 with another port.
        return site not in ("same-origin", "none")
    origin = request.headers.get("origin")
    return bool(origin) and urlparse(origin).netloc != request.headers.get("host")


@app.middleware("http")
async def check_login(request: Request, call_next):
    """Before the setup only the setup is reachable, afterwards only after a login. /channel checks its own token.

    What changes something, the UI accepts only from its own pages.
    """
    path = request.url.path
    if request.method not in ("GET", "HEAD", "OPTIONS") and not path.startswith("/channel/") and _foreign_site(request):
        return JSONResponse({"detail": i18n.t(i18n.from_header(request.headers.get("accept-language")),
                                              "err.foreign_site")}, 403)
    request.state.person = auth.person(request.cookies.get(auth.COOKIE))
    if path.startswith("/static/"):
        return await call_next(request)
    wants_html = request.method == "GET" and "text/html" in request.headers.get("accept", "")
    if not company.is_set_up():
        if path == "/setup":
            return await call_next(request)
        if wants_html:
            return RedirectResponse("/setup", 303)
        return JSONResponse({"detail": i18n.t(lang_of(request), "err.not_set_up")}, 409)
    free = path.startswith("/channel/") or path in ("/login", "/logout", "/logo")
    if not free and not request.state.person:
        if wants_html:
            return RedirectResponse(f"/login?next={quote(path)}", 303)
        return JSONResponse({"detail": i18n.t(lang_of(request), "err.log_in_first")}, 401)
    return await call_next(request)


# The browser loads nothing from foreign addresses. An answer can hold a Markdown image whose address carries data out
# of the company, e.g. after an instruction in a calendar entry; the browser would fetch it without a click, also for
# an employee without web access. The pages have inline scripts and styles, so those stay allowed.
POLICY = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self'; "
          "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'")


@app.middleware("http")
async def protect_pages(request: Request, call_next):
    """Added last, so it runs first and also covers the answers of check_login."""
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = POLICY
    # A link clicked in an answer does not tell the foreign site which conversation it came from.
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def actor(request: Request) -> str:
    """Who is acting: the logged-in human."""
    return request.state.person


def _card(request: Request, some_id: str) -> dict:
    try:
        return company.card(company.find(some_id))
    except Unknown as error:
        raise problem(request, error)


def _not_working(request: Request, some_id: str) -> None:
    """A human's change to an employee's folder waits until the employee is done with their run."""
    if conversations.busy(some_id):
        raise problem(request, Rejected("working_now", name=company.display_name(some_id)))


def _names() -> dict[str, str]:
    """Names to show for IDs, humans and employees."""
    names = {c["id"]: c["name"] for c in company.team()}
    names.update({p["id"]: p["display_name"] for p in company.people()})
    return names


def _message_html(request: Request, message, names: dict[str, str]) -> str:
    return templates.get_template("_message.html").render(m=message, names=names, lang=lang_of(request))


# ---------------------------------------------------------------- Setup

@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request):
    if company.is_set_up():
        return RedirectResponse("/", 303)
    return page(request, "setup.html", {"languages": i18n.LANGUAGES, "min_length": auth.MIN_LENGTH})


@app.post("/setup")
def set_up(request: Request, company_name: str = Body(embed=True), language: str = Body(embed=True),
           name: str = Body(embed=True), display_name: str = Body("", embed=True), password: str = Body(embed=True)):
    """The first start: company, language and the owner with a password. The owner is logged in afterwards."""
    if company.is_set_up():
        raise HTTPException(409, i18n.t(lang_of(request), "err.already_set_up"))
    if len(password or "") < auth.MIN_LENGTH:
        raise problem(request, Invalid("password_short", length=auth.MIN_LENGTH))
    try:
        owner = company.set_up(company_name, language, name, display_name)
    except Problem as error:
        raise problem(request, error)
    auth.set_password(owner, password)
    company.commit("Setup: company and owner")
    token = auth.log_in(name, password)
    response = JSONResponse({"ok": True})
    response.set_cookie(auth.COOKIE, token, max_age=config.LOGIN_DAYS * 86400, httponly=True, samesite="lax",
                        secure=config.COOKIE_SECURE)
    return response


# ---------------------------------------------------------------- Team and cards

@app.get("/", response_class=HTMLResponse)
def team(request: Request, person: str = Depends(actor)):
    cards = company.team()
    with db.connect() as con:
        open_requests = {r["employee"]: r["n"] for r in con.execute(
            "SELECT employee, COUNT(*) AS n FROM approval_requests WHERE status = 'open' GROUP BY employee")}
        busy = {r["employee"] for r in con.execute("SELECT employee FROM conversations WHERE status != 'ended'")}
        favorites = {r["employee"] for r in con.execute("SELECT employee FROM favorites WHERE person = ?", (person,))}
    roles = {role: [company.name_of(f"employees/{h}") for h in company.holders(role)] for role in company.ROLES}
    return page(request, "team.html", {
        # Favorites first; within both groups the order stays by name.
        "employees": sorted((c for c in cards if not c["candidate"]), key=lambda c: c["id"] not in favorites),
        "favorites": favorites,
        "candidates": [c for c in cards if c["candidate"]],
        "people": company.people(), "open_requests": open_requests, "busy": busy, "archive": company.archive(),
        "roles": roles, "token": company.token_kind(),
    })


@app.get("/team/{some_id}", response_class=HTMLResponse)
def employee_card(request: Request, some_id: str):
    card = _card(request, some_id)
    rel = card["rel"]
    lang = lang_of(request)
    with db.connect() as con:
        requests = con.execute("SELECT * FROM approval_requests WHERE employee = ? AND status = 'open' "
                               "ORDER BY last DESC", (some_id,)).fetchall()
        all_conversations = con.execute("SELECT COUNT(*) FROM conversations WHERE employee = ?",
                                        (some_id,)).fetchone()[0]
    # Reports of the latest assessment.
    assessment = company.files(rel, "assessment")
    day = next((p.split("/", 1)[1][:10] for p in assessment if p.endswith("-report.md")), "-")
    job = None if card["candidate"] else personnel.latest(some_id)
    running = None if card["candidate"] else personnel.running(some_id)
    feedback_job = personnel.latest(some_id, "feedback")
    leader = personnel.feedback_leader(some_id)
    return page(request, "card.html", {
        "c": card,
        "job": job, "job_progress": personnel.describe(job["progress"], lang) if job else [],
        "leader": company.name_of(f"employees/{leader}") if leader else "",
        "running": running, "running_progress": personnel.describe(running["progress"], lang) if running else [],
        "feedback_job": feedback_job,
        "meetings": company.feedback_meetings(rel),
        "reports": [p for p in assessment if p.split("/", 1)[1].startswith(day) and p.endswith("-report.md")],
        "reviewers": [{"id": r, "name": company.name_of(f"employees/{r}"),
                       "title": company.header(config.COMPANY_DIR / "employees" / r / company.PROFILE).get("title", "")}
                      for r in company.reviewers() if r != some_id],
        "hr": [company.name_of(f"employees/{h}") for h in company.holders("hr") if h != some_id],
        "names": _names(), "config": config,
        "parts": [{"part": part, "text": company.part_text(rel, part), "words": company.words(company.part_text(rel, part)),
                   "limit": company.part_limit(part), "versions": company.profile_versions(rel, part)}
                  for part in company.PARTS],
        "agreement": company.read(rel, company.AGREEMENT) if (config.COMPANY_DIR / rel / company.AGREEMENT).is_file()
        else "", "agreement_path": company.AGREEMENT,
        "folders": {name: company.files(rel, name) for name in ("assignments", "assessment")},
        "conversations": conversations.listing(some_id, CONVERSATIONS_ON_CARD), "all_conversations": all_conversations,
        "requests": requests,
        "edits": company.edits(rel),
        "schedules": scheduler.schedules(some_id, lang), "weekdays": i18n.t(lang, "days.short").split(","),
        "models": company.MODELS, "all_roles": company.ROLES,
    })


@app.get("/team/{some_id}/conversations", response_class=HTMLResponse)
def all_conversations(request: Request, some_id: str):
    """All conversations of an employee by month. The card shows only the latest."""
    card = _card(request, some_id)
    months: dict[str, list] = {}
    for c in conversations.listing(some_id):
        months.setdefault(datetime.fromtimestamp(c["started"]).strftime("%m/%Y"), []).append(c)
    archivist = company.archivist()
    return page(request, "conversations.html", {
        "c": card, "months": months, "archivist": archivist.split("/")[1] if archivist else "", "names": _names()})


@app.get("/team/{some_id}/file", response_class=HTMLResponse)
def file_page(request: Request, some_id: str, path: str):
    card = _card(request, some_id)
    try:
        content = company.read(card["rel"], path)
    except (FileNotFoundError, UnicodeDecodeError):
        raise HTTPException(404, i18n.t(lang_of(request), "err.no_such_file"))
    return page(request, "file.html", {
        "title": path, "back": f"/team/{some_id}", "back_name": card["name"], "content": content,
        "edit": f"/team/{some_id}/edit?path={quote(path)}" if company.editable(path) else None})


@app.get("/team/{some_id}/edit", response_class=HTMLResponse)
def edit_page(request: Request, some_id: str, path: str):
    """Editor for a file in an employee's folder. Of the profile only the text; the header stays."""
    card = _card(request, some_id)
    try:
        text, version = company.for_editing(card["rel"], path)
    except (FileNotFoundError, UnicodeDecodeError):
        raise HTTPException(404, i18n.t(lang_of(request), "err.not_editable"))
    return page(request, "edit.html", {
        "c": card, "path": path, "text": text, "version": version, "kinds": company.EDIT_KINDS,
        "back": f"/team/{some_id}", "back_name": card["name"], "base": f"/team/{some_id}"})


@app.post("/team/{some_id}/diff")
def edit_diff(request: Request, some_id: str, path: str = Body(embed=True), text: str = Body(embed=True)):
    card = _card(request, some_id)
    try:
        return {"diff": company.diff(card["rel"], path, text)}
    except (FileNotFoundError, UnicodeDecodeError):
        raise HTTPException(404, i18n.t(lang_of(request), "err.no_such_file"))
    except Problem as error:
        raise problem(request, error)


@app.post("/team/{some_id}/edit")
def edit(request: Request, some_id: str, path: str = Body(embed=True), text: str = Body(embed=True),
         kind: str = Body(embed=True), reason: str = Body("", embed=True), version: str = Body(embed=True),
         person: str = Depends(actor)):
    """A human changes a file themselves. That is in the employee's log afterwards and in Git."""
    card = _card(request, some_id)
    _not_working(request, some_id)
    try:
        company.edit(card["rel"], path, text, kind, reason, person, version)
    except (FileNotFoundError, UnicodeDecodeError):
        raise HTTPException(404, i18n.t(lang_of(request), "err.no_such_file"))
    except Problem as error:
        raise problem(request, error)
    # The reason is only in the log, not in the commit message.
    return {"hint": hint(request, "file_changed", company.committed(f"Edit by {person} of {some_id}/{path}: {kind}"))}


@app.post("/team/{some_id}/favorite")
def favorite(request: Request, some_id: str, favorite: bool = Body(embed=True), person: str = Depends(actor)):
    """Favorites are at the top of the team page. Every human has their own."""
    _card(request, some_id)
    with db.connect() as con:
        if favorite:
            con.execute("INSERT OR IGNORE INTO favorites (person, employee) VALUES (?, ?)", (person, some_id))
        else:
            con.execute("DELETE FROM favorites WHERE person = ? AND employee = ?", (person, some_id))
    return {"favorite": favorite}


@app.get("/search", response_class=HTMLResponse)
def search(request: Request, q: str = ""):
    """Where a text still appears in the files, to remove it everywhere."""
    q = q.strip()
    return page(request, "search.html", {
        "q": q, "names": _names(), "editable": company.editable, "hits": company.search(q) if len(q) >= 3 else []})


@app.get("/notices", response_class=HTMLResponse)
def notices_page(request: Request, read: bool = False, person: str = Depends(actor)):
    """What the employees left for the logged-in human, with NOTICE at the end of an answer: unread or read ones."""
    return page(request, "notices.html", {"notices": notices.listing(person, read), "read": read, "names": _names()})


@app.post("/notices/read")
def notices_read(nid: int | None = Body(None, embed=True), person: str = Depends(actor)):
    """Mark one notice as read, without an ID all of them."""
    return {"marked": notices.mark_read(person, nid)}


@app.get("/usage", response_class=HTMLResponse)
def usage_page(request: Request, days: int = 30):
    days = days if days in (1, 7, 30) else 0
    return page(request, "usage.html", {"days": days, "names": _names(), "kinds": usage.KINDS, **usage.overview(days)})


# ---------------------------------------------------------------- People, handbook, services

@app.get("/people/{person_id}", response_class=HTMLResponse)
def person_page(request: Request, person_id: str):
    try:
        p = company.person(person_id)
    except Unknown as error:
        raise problem(request, error)
    return page(request, "person.html", {"p": p, "own": person_id == actor(request), "min_length": auth.MIN_LENGTH,
                                         "languages": i18n.LANGUAGES})


@app.post("/people/{person_id}/profile")
def person_profile(request: Request, person_id: str, name: str = Body(embed=True),
                   display_name: str = Body("", embed=True), language: str = Body(embed=True),
                   about: str = Body("", embed=True), person: str = Depends(actor)):
    """The own profile: names, language and the text the employees get about you."""
    if person_id != person:
        raise HTTPException(403, i18n.t(lang_of(request), "err.own_profile_only"))
    try:
        changed = company.update_person(person_id, name, display_name, language, about)
    except Problem as error:
        raise problem(request, error)
    return {"changed": changed, "hint": hint(request, "profile_changed",
                                             company.committed(f"Profile of {person_id} changed")) if changed else None}


@app.post("/people/{person_id}/password")
def password(request: Request, person_id: str, password: str = Body(embed=True), person: str = Depends(actor)):
    """Set the own password. A new login is needed afterwards."""
    if person_id != person:
        raise HTTPException(403, i18n.t(lang_of(request), "err.own_password_only"))
    try:
        auth.set_password(person_id, password)
    except Problem as error:
        raise problem(request, error)
    return {"ok": True}


@app.get("/handbook", response_class=HTMLResponse)
def handbook(request: Request):
    file = config.COMPANY_DIR / "handbook.md"
    return page(request, "file.html", {
        "title": i18n.t(lang_of(request), "handbook.title"), "back": "/", "back_name": i18n.t(lang_of(request), "nav.team"),
        "content": file.read_text(encoding="utf-8") if file.is_file() else "", "edit": "/document/edit?path=handbook.md"})


@app.get("/document/edit", response_class=HTMLResponse)
def document_edit_page(request: Request, path: str):
    """Editor for the handbook or the text of the own profile."""
    if path.startswith("people/") and path != f"people/{actor(request)}/profile.md":
        raise HTTPException(403, i18n.t(lang_of(request), "err.own_profile_only"))
    try:
        text, version = company.for_editing(None, path)
    except (FileNotFoundError, UnicodeDecodeError):
        raise HTTPException(404, i18n.t(lang_of(request), "err.not_editable"))
    back = "/handbook" if path == "handbook.md" else f"/people/{actor(request)}"
    return page(request, "edit.html", {"c": None, "path": path, "text": text, "version": version, "kinds": (),
                                       "back": back, "back_name": i18n.t(lang_of(request), "edit.back"),
                                       "base": "/document"})


@app.post("/document/diff")
def document_diff(request: Request, path: str = Body(embed=True), text: str = Body(embed=True)):
    try:
        return {"diff": company.diff(None, path, text)}
    except (FileNotFoundError, UnicodeDecodeError):
        raise HTTPException(404, i18n.t(lang_of(request), "err.no_such_file"))
    except Problem as error:
        raise problem(request, error)


@app.post("/document/edit")
def document_edit(request: Request, path: str = Body(embed=True), text: str = Body(embed=True),
                  version: str = Body(embed=True), person: str = Depends(actor)):
    if path.startswith("people/") and path != f"people/{person}/profile.md":
        raise HTTPException(403, i18n.t(lang_of(request), "err.own_profile_only"))
    try:
        company.edit_document(path, text, version)
    except (FileNotFoundError, UnicodeDecodeError):
        raise HTTPException(404, i18n.t(lang_of(request), "err.no_such_file"))
    except Problem as error:
        raise problem(request, error)
    return {"hint": hint(request, "file_changed", company.committed(f"{path} changed by {person}"))}


@app.get("/services", response_class=HTMLResponse)
def services_page(request: Request):
    return page(request, "services.html", {"services": company.services_overview(), "token": company.token_kind(),
                                           "network": config.NETWORK})


@app.post("/services")
def save_service(request: Request, name: str = Body(embed=True), type: str = Body("http", embed=True),
                 url: str = Body(embed=True), description: str = Body("", embed=True),
                 blocked: str = Body("", embed=True), header_name: str = Body("", embed=True),
                 header_value: str = Body("", embed=True)):
    """Add or change a service. A secret is set, but never shown again."""
    try:
        company.save_service(name, type, url, description, blocked, header_name, header_value)
    except Problem as error:
        raise problem(request, error)
    return {"ok": True}


@app.post("/services/{name}/delete")
def delete_service(request: Request, name: str):
    if not company.delete_service(name):
        raise HTTPException(404, i18n.t(lang_of(request), "err.no_such_service"))
    return {"ok": True}


@app.post("/token")
def token(request: Request, kind: str = Body(embed=True), value: str = Body(embed=True)):
    """The Claude token for all runs. It is set, but never shown again."""
    try:
        company.set_token(kind, value)
    except Problem as error:
        raise problem(request, error)
    return {"token": company.token_kind()}


# ---------------------------------------------------------------- Settings of the company

@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    return page(request, "settings.html", {"logo_kb": company.LOGO_KB, "claim_length": company.CLAIM_LENGTH})


@app.post("/settings/name")
def rename_company(request: Request, name: str = Body(embed=True), person: str = Depends(actor)):
    try:
        company.rename_company(name)
    except Problem as error:
        raise problem(request, error)
    return {"hint": hint(request, "company_renamed", company.committed(f"Company renamed by {person}"))}


@app.post("/settings/claim")
def set_claim(request: Request, claim: str = Body(embed=True), person: str = Depends(actor)):
    try:
        company.set_claim(claim)
    except Problem as error:
        raise problem(request, error)
    return {"hint": hint(request, "claim_changed", company.committed(f"Claim changed by {person}"))}


@app.post("/settings/logo")
def set_logo(request: Request, data: str = Body(embed=True), person: str = Depends(actor)):
    """The logo as Base64 in JSON, like every other form of the UI."""
    try:
        company.set_logo(base64.b64decode(data, validate=True))
    except binascii.Error:
        raise problem(request, Invalid("logo_type"))
    except Problem as error:
        raise problem(request, error)
    return {"hint": hint(request, "logo_changed", company.committed(f"Logo changed by {person}"))}


@app.post("/settings/logo/delete")
def remove_logo(request: Request, person: str = Depends(actor)):
    if not company.remove_logo():
        raise HTTPException(404, i18n.t(lang_of(request), "err.no_logo"))
    return {"hint": hint(request, "logo_changed", company.committed(f"Logo removed by {person}"))}


@app.post("/settings/sandbox")
def set_sandbox_view(on: bool = Body(embed=True)):
    sandbox.set_enabled(on)
    return {"on": sandbox.enabled()}


# ---------------------------------------------------------------- Sandbox view

def _run_html(request: Request, run: dict) -> str:
    return templates.get_template("_sandbox_run.html").render(
        run=run, lang=lang_of(request), purposes=usage.PURPOSES)


@app.get("/sandbox", response_class=HTMLResponse)
def sandbox_page(request: Request):
    return page(request, "sandbox.html", {"runs": list(sandbox.board.runs), "purposes": usage.PURPOSES})


@app.post("/sandbox/clear")
def clear_sandbox():
    sandbox.board.clear()
    return {"runs": 0}


@app.get("/sandbox/events")
async def sandbox_events(request: Request):
    if not sandbox.enabled():
        raise HTTPException(404, i18n.t(lang_of(request), "err.sandbox_off"))

    async def stream():
        async for run in sandbox.board.watch():
            if run is None:
                yield "event: clear\ndata: {}\n\n"
            else:
                yield f"event: run\ndata: {json.dumps({'id': run['id'], 'html': _run_html(request, run)})}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/logo")
def logo(request: Request):
    """Reachable without a login, for the login page."""
    file = company.logo()
    if not file:
        raise HTTPException(404, i18n.t(lang_of(request), "err.no_logo"))
    return FileResponse(file, media_type=company.LOGO_TYPES[file.suffix[1:]],
                        headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})


# ---------------------------------------------------------------- Web chat

def _chat_page(request: Request, card: dict, conversation, fixed: bool, kind: str = "conversation"):
    return page(request, "chat.html", {
        "c": card, "conv": conversation, "fixed": fixed, "names": _names(),
        "kind": conversation["kind"] if conversation else kind,
        "colleagues": [c for c in company.team() if not c["candidate"] and c["id"] != card["id"]],
        "messages": conversations.messages(conversation["id"]) if conversation else [],
        "state": conversations.state(conversation["id"]) if conversation else {"state": "new", "ends_at": None},
    })


@app.get("/chat/{some_id}", response_class=HTMLResponse)
def chat(request: Request, some_id: str, person: str = Depends(actor)):
    """The ongoing conversation with an employee, or an empty one if none is open."""
    return _chat_page(request, _card(request, some_id), conversations.active(some_id, person), fixed=False)


@app.get("/hiring", response_class=HTMLResponse)
def hiring(request: Request, person: str = Depends(actor), hr: str = ""):
    """The hiring interview with HR. HR creates the draft as a candidate in it."""
    found = company.holders("hr")
    if not found:
        return RedirectResponse("/hire", 303)
    hr = hr if hr in found else found[0]
    return _chat_page(request, _card(request, hr), conversations.active(hr, person, "hiring"), fixed=False,
                      kind="hiring")


@app.get("/conversation/{cid}", response_class=HTMLResponse)
def conversation(request: Request, cid: int):
    c = conversations.get(cid)
    if not c:
        raise HTTPException(404, i18n.t(lang_of(request), "err.no_such_conversation"))
    return _chat_page(request, _card(request, c["employee"]), c, fixed=True)


@app.post("/chat/{some_id}/message")
async def message(request: Request, some_id: str, text: str = Body(embed=True), kind: str = Body("conversation", embed=True),
                  to: str = Body("", embed=True), person: str = Depends(actor)):
    """Message to the employee. With "to" it goes to a colleague whom the human brings into the conversation."""
    _card(request, some_id)
    try:
        if to and to != some_id:
            if kind != "conversation":
                raise Rejected("bring_in_only_conversation")
            m = conversations.bring_in(some_id, person, to, text)
        else:
            m = conversations.send(some_id, person, text, kind=kind)
    except Problem as error:
        raise problem(request, error)
    return {"conversation": m["conversation_id"], "id": m["id"], "html": _message_html(request, m, _names())}


@app.post("/conversation/{cid}/reopen")
async def reopen(request: Request, cid: int, person: str = Depends(actor)):
    """Continue an ended conversation. It is the ongoing conversation in the chat again."""
    if not conversations.get(cid):
        raise HTTPException(404, i18n.t(lang_of(request), "err.no_such_conversation"))
    try:
        conversations.reopen(cid, person)
    except Problem as error:
        raise problem(request, error)
    return conversations.state(cid)


@app.post("/conversation/{cid}/{action}")
async def control(request: Request, cid: int, action: str):
    if not conversations.get(cid):
        raise HTTPException(404, i18n.t(lang_of(request), "err.no_such_conversation"))
    actions = {"pause": conversations.pause, "resume": conversations.resume, "end": conversations.end}
    if action == "stop":
        await conversations.stop(cid)
    elif action in actions:
        actions[action](cid)
    else:
        raise HTTPException(404, i18n.t(lang_of(request), "err.unknown_action"))
    return conversations.state(cid)


@app.get("/conversation/{cid}/events")
async def events(request: Request, cid: int, after: int = 0):
    if not conversations.get(cid):
        raise HTTPException(404, i18n.t(lang_of(request), "err.no_such_conversation"))
    names = _names()

    async def stream():
        async for kind, data in conversations.subscribe(cid, after):
            if kind == "message":
                data = {"id": data["id"], "sender_kind": data["sender_kind"], "html": _message_html(request, data, names)}
            yield f"event: {kind}\ndata: {json.dumps(data)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------- Other channel

@app.post("/channel/{some_id}/message")
async def channel_message(request: Request, some_id: str, text: str = Body(embed=True), person: str = Body("", embed=True),
                          channel: str = Body("external", embed=True), wait: bool = Body(True, embed=True)):
    """Access for a messenger bridge or a script: a message in, the answer out. Secured by a token."""
    lang = i18n.from_header(request.headers.get("accept-language"))
    if not config.CHANNEL_TOKEN:
        raise HTTPException(404, i18n.t(lang, "err.channel_off"))
    if not hmac.compare_digest(request.headers.get("authorization", "").encode(),
                               f"Bearer {config.CHANNEL_TOKEN}".encode()):
        raise HTTPException(401, i18n.t(lang, "err.channel_token"))
    _card(request, some_id)
    person = person or company.owner()
    if person not in {p["id"] for p in company.people()}:
        raise HTTPException(409, i18n.t(lang, "err.unknown_person", id=person))
    try:
        m = conversations.send(some_id, person, text, channel=channel[:20] or "external")
    except Problem as error:
        raise HTTPException(409, error.text(lang))
    if not wait:
        return {"conversation": m["conversation_id"], "id": m["id"]}
    try:
        new = await conversations.answer_to(m)
    except TimeoutError:
        raise HTTPException(504, i18n.t(lang, "err.answer_late"))
    return {"conversation": m["conversation_id"], "id": m["id"],
            "answers": [{"sender": n["sender"], "text": n["text"]} for n in new if n["sender_kind"] == "agent"],
            "notes": [n["text"] for n in new if n["sender_kind"] == "system"]}


# ---------------------------------------------------------------- Login

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    if request.state.person:
        return RedirectResponse("/", 303)
    return page(request, "login.html", {"next": next if _own_page(next) else "/"})


def _own_page(address: str) -> bool:
    """Redirect only to pages of this UI, never to foreign addresses.

    Browsers read "/\\evil.example" like "//evil.example" and drop tabs and line breaks.
    """
    return (address.startswith("/") and not address.startswith("//") and "\\" not in address
            and not any(ord(c) < 0x20 or c == "\x7f" for c in address))


@app.post("/login")
async def log_in(request: Request, name: str = Body(embed=True), password: str = Body(embed=True)):
    token = await asyncio.to_thread(auth.log_in, name, password)
    if not token:
        await asyncio.sleep(1)  # slows down guessing
        raise HTTPException(401, i18n.t(lang_of(request), "err.login_failed"))
    response = JSONResponse({"ok": True})
    response.set_cookie(auth.COOKIE, token, max_age=config.LOGIN_DAYS * 86400, httponly=True, samesite="lax",
                        secure=config.COOKIE_SECURE)
    return response


@app.post("/logout")
def log_out(request: Request):
    auth.log_out(request.cookies.get(auth.COOKIE))
    response = JSONResponse({"ok": True})
    response.delete_cookie(auth.COOKIE)
    return response


# ---------------------------------------------------------------- Archive

def _archived(request: Request, folder: str) -> str:
    try:
        return company.archived(folder)
    except Unknown as error:
        raise problem(request, error)


@app.get("/archive/{folder}", response_class=HTMLResponse)
def archive(request: Request, folder: str):
    rel = _archived(request, folder)
    return page(request, "archive.html", {
        "folder": folder, "c": company.card(rel), "profile": company.body(config.COMPANY_DIR / rel / company.PROFILE),
        "work": company.part_text(rel, company.WORK),
        "files": {name: company.files(rel, name) for name in ("journal", "assignments", "feedback", "assessment")}})


@app.get("/archive/{folder}/file", response_class=HTMLResponse)
def archive_file(request: Request, folder: str, path: str):
    rel = _archived(request, folder)
    try:
        content = company.read(rel, path)
    except (FileNotFoundError, UnicodeDecodeError):
        raise HTTPException(404, i18n.t(lang_of(request), "err.no_such_file"))
    return page(request, "file.html", {
        "title": path, "back": f"/archive/{folder}",
        "back_name": i18n.t(lang_of(request), "archive.crumb", folder=folder), "content": content, "edit": None})


@app.post("/archive/{folder}/rehire")
def rehire(request: Request, folder: str):
    try:
        result = personnel.rehire(folder)
    except Problem as error:
        raise problem(request, error)
    return {"id": result["id"], "hint": hint(request, "rehired", result["git_error"])}


# ---------------------------------------------------------------- Header and schedules

@app.post("/team/{some_id}/header")
def change_header(request: Request, some_id: str, fields: dict[str, str] = Body(embed=True)):
    """Name, title, model, extra tools, services, roles and whether own changes apply at once."""
    _card(request, some_id)
    _not_working(request, some_id)
    try:
        result = personnel.change_header(some_id, fields)
    except Problem as error:
        raise problem(request, error)
    return {"changed": result["changed"], "hint": hint(request, "profile_changed", result["git_error"])}


@app.post("/team/{some_id}/schedule")
def schedule(request: Request, some_id: str, time: str = Body(embed=True), days: list[int] = Body(embed=True),
             text: str = Body(embed=True), person: str = Depends(actor)):
    """A message that goes to the employee regularly by itself."""
    _card(request, some_id)
    try:
        return {"schedule": scheduler.add(some_id, person, time, days, text)}
    except Problem as error:
        raise problem(request, error)


@app.post("/schedule/{sid}/run")
async def run_schedule(request: Request, sid: int, person: str = Depends(actor)):
    """Send the message of a schedule now, e.g. to try it or not to wait for the next day."""
    try:
        return {"conversation": scheduler.run_now(sid, person)["conversation_id"]}
    except Problem as error:
        raise problem(request, error)


@app.post("/schedule/{sid}/pause")
def pause_schedule(request: Request, sid: int, paused: bool = Body(embed=True)):
    if not scheduler.pause(sid, paused):
        raise HTTPException(404, i18n.t(lang_of(request), "err.no_such_schedule"))
    return {"paused": paused}


@app.post("/schedule/{sid}/delete")
def delete_schedule(request: Request, sid: int):
    if not scheduler.delete(sid):
        raise HTTPException(404, i18n.t(lang_of(request), "err.no_such_schedule"))
    return {"ok": True}


# ---------------------------------------------------------------- Hiring

@app.get("/hire", response_class=HTMLResponse)
def hire_page(request: Request, preset: str = ""):
    """Hire from a preset: a profile for a typical role, edited before hiring. An empty company starts here."""
    presets = company.presets(company.language())
    chosen = next((p for p in presets if p["key"] == preset), None)
    return page(request, "hire.html", {"presets": presets, "chosen": chosen, "hr": company.holders("hr"),
                                       "all_roles": company.ROLES, "models": company.MODELS})


@app.post("/hire")
def hire_from_preset(request: Request, fields: dict[str, str] = Body(embed=True), text: str = Body(embed=True),
                     work: str = Body("", embed=True)):
    try:
        result = personnel.hire_from_preset(fields, text, work)
    except Problem as error:
        raise problem(request, error)
    return {"id": result["id"], "hint": hint(request, "hired", result["git_error"])}


@app.post("/team/{some_id}/assessment")
async def assessment(request: Request, some_id: str, person: str = Depends(actor),
                     reviewers: list[str] | None = Body(None, embed=True)):
    """The chosen reviewers (all without a choice) test the employee in the background. The card shows the
    progress."""
    _card(request, some_id)
    try:
        return {"job": personnel.start_assessment(some_id, person, reviewers)}
    except Problem as error:
        raise problem(request, error)


@app.get("/job/{jid}")
def job(request: Request, jid: int):
    j = personnel.job(jid)
    if not j:
        raise HTTPException(404, i18n.t(lang_of(request), "err.no_such_job"))
    return {"status": j["status"], "error": j["error"],
            "progress": [list(p) for p in personnel.describe(j["progress"], lang_of(request))]}


@app.post("/team/{some_id}/hire")
def hire(request: Request, some_id: str):
    _card(request, some_id)
    try:
        result = personnel.hire(some_id)
    except Problem as error:
        raise problem(request, error)
    return {"hint": hint(request, "hired", result["git_error"])}


@app.post("/team/{some_id}/dismiss")
def dismiss(request: Request, some_id: str):
    """The folder moves to the archive. Conversations and jobs stay in the database."""
    _card(request, some_id)
    try:
        result = personnel.dismiss(some_id)
    except Problem as error:
        raise problem(request, error)
    return {"archive": result["archive"], "hint": hint(request, "archived", result["git_error"])}


# ---------------------------------------------------------------- Feedback

@app.post("/team/{some_id}/feedback")
async def feedback(request: Request, some_id: str, brief: str = Body("", embed=True), person: str = Depends(actor)):
    """The evaluation is written in the background. The card shows the progress."""
    _card(request, some_id)
    try:
        return {"job": personnel.start_feedback(some_id, person, brief)}
    except Problem as error:
        raise problem(request, error)


@app.get("/team/{some_id}/feedback/{meeting}", response_class=HTMLResponse)
def feedback_meeting(request: Request, some_id: str, meeting: str):
    """The evaluation on one page and the proposed profile. As long as the decision is missing, with the buttons."""
    card = _card(request, some_id)
    rel = card["rel"]
    try:
        folder = company.feedback_dir(rel, meeting)
    except FileNotFoundError:
        raise HTTPException(404, i18n.t(lang_of(request), "err.no_such_meeting_page"))
    parts = {n: (folder / f"{n}.md").read_text(encoding="utf-8") if (folder / f"{n}.md").is_file() else ""
             for n in ("brief", "evaluation", "decision")}
    led = (folder / company.LED_BY).read_text(encoding="utf-8").strip() if (folder / company.LED_BY).is_file() else ""
    leader = led or personnel.feedback_leader(some_id) or ""
    return page(request, "feedback.html", {
        "c": card, "meeting": meeting, "names": _names(), "config": config, **parts,
        "leader": company.display_name(leader) if leader else "",
        "busy": bool(personnel.running(some_id)),
        "proposal": None if parts["decision"] else company.feedback_proposal(rel, meeting),
        "files": [p for p in company.files(rel, "feedback") if p.startswith(f"feedback/{meeting}/")]})


@app.post("/team/{some_id}/feedback/{meeting}/diff")
def feedback_diff(request: Request, some_id: str, meeting: str, text: str = Body(embed=True),
                  part: str = Body(company.PROFILE, embed=True)):
    """Difference between a current part of the profile and the proposal as the human edited it."""
    card = _card(request, some_id)
    if part not in company.PARTS:
        raise HTTPException(404, i18n.t(lang_of(request), "err.no_such_file"))
    return {"diff": company.text_diff(company.part_text(card["rel"], part), text.replace("\r\n", "\n").strip("\n")),
            "words": company.words(text)}


@app.post("/team/{some_id}/feedback/{meeting}/decision")
async def feedback_decision(request: Request, some_id: str, meeting: str, accept: bool = Body(embed=True),
                            texts: dict[str, str] | None = Body(None, embed=True),
                            version: str | None = Body(None, embed=True)):
    """Accept the proposed parts of the profile, edited if wished ("texts" per part), or reject them."""
    _card(request, some_id)
    _not_working(request, some_id)
    try:
        result = await personnel.decide_feedback(some_id, meeting, accept, texts, version)
    except Problem as error:
        raise problem(request, error)
    return {"result": result["result"], "hint": hint(request, "decided", result["git_error"])}


# ---------------------------------------------------------------- Own changes to the profile

@app.post("/profile-change/{mid}/{action}")
async def profile_change(request: Request, mid: int, action: str, person: str = Depends(actor)):
    """Undo an own change of an employee to their profile, or accept or discard a proposed one. Returns the note."""
    try:
        await conversations.decide_profile_change(mid, action, person)
    except Problem as error:
        raise problem(request, error)
    except company.GitError as error:
        return {"html": "", "hint": hint(request, "profile_changed", str(error))}
    with db.connect() as con:
        message = con.execute("SELECT * FROM messages WHERE id = ?", (mid,)).fetchone()
    return {"html": _message_html(request, message, _names())}


@app.get("/team/{some_id}/profile/{commit}", response_class=HTMLResponse)
def profile_version(request: Request, some_id: str, commit: str, part: str = company.PROFILE):
    """One version of a part of the profile from Git, with the difference to the version before."""
    card = _card(request, some_id)
    try:
        version = company.profile_version(card["rel"], commit, part)
    except Unknown as error:
        raise problem(request, error)
    return page(request, "version.html", {"c": card, "v": version,
                                          "current": version["text"] == company.part_text(card["rel"], part)})


@app.post("/team/{some_id}/profile/{commit}/restore")
def restore_profile(request: Request, some_id: str, commit: str, part: str = company.PROFILE,
                    person: str = Depends(actor)):
    """Put the text of an earlier version of a part back. The header stays as it is. It goes into the log of edits,
    so that the employee learns of it."""
    card = _card(request, some_id)
    _not_working(request, some_id)
    try:
        version = company.profile_version(card["rel"], commit, part)
        _, current = company.for_editing(card["rel"], part)
        company.edit(card["rel"], part, version["text"], "other",
                     i18n.t(company.language(), "file.restored_reason", when=version["when"]), person, current)
    except Problem as error:
        raise problem(request, error)
    name = card["name"]
    return {"hint": hint(request, "profile_changed",
                         company.committed(f"Profile of {name}: {part} restored to the version of {version['when']} "
                                           f"by {person}"))}


# ---------------------------------------------------------------- Approvals

@app.post("/approval/{aid}/{decision}")
def approve(request: Request, aid: int, decision: str, person: str = Depends(actor)):
    """Approve a tool an employee would have needed in a conversation, or deny the request."""
    if decision not in ("grant", "deny"):
        raise HTTPException(404, i18n.t(lang_of(request), "err.decision_unknown"))
    with db.connect() as con:
        req = con.execute("SELECT * FROM approval_requests WHERE id = ?", (aid,)).fetchone()
    if not req:
        raise HTTPException(404, i18n.t(lang_of(request), "err.no_such_request"))
    entry = None
    if decision == "grant":
        _not_working(request, req["employee"])
        try:
            entry = company.grant(company.find(req["employee"]), req["tool"])
        except Problem as error:
            raise problem(request, error)
    status = "granted" if entry else "denied"
    with db.connect() as con:
        con.execute("UPDATE approval_requests SET status = ?, decided_by = ?, decided_at = ? WHERE id = ?",
                    (status, person, time.time(), aid))
    error = company.committed(f"Approval for {req['employee']}: {entry}") if entry else None
    return {"status": status, "hint": hint(request, "profile_changed", error)}
