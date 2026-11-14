import asyncio
import base64
import json
import re
import shutil
import subprocess
import time
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import auth, company, config, db, i18n, main, notices, sandbox, usage
from app.conversations import Conversations
from app.personnel import KINDS, VERDICTS, Personnel
from app.runner import DummyRunner
from conftest import CORA, IDA, MILA, NEW, OWNER, PASSWORD, quick, settle, wait_for_job, web
from test_personnel import HRDesk

HTML = {"accept": "text/html"}


def wait_until_ended(cid):
    for _ in range(200):
        if main.conversations.get(cid)["status"] == "ended":
            return
        time.sleep(0.02)


# ---------------------------------------------------------------- Setup and login

def test_first_start_is_the_setup(tmp_path, monkeypatch):
    root = tmp_path / "company"
    monkeypatch.setattr(config, "COMPANY_DIR", root)
    monkeypatch.setattr(config, "RUNTIME", root / "runtime")
    monkeypatch.setattr(config, "DATABASE", tmp_path / "db.sqlite")
    with TestClient(main.app, follow_redirects=False) as client:
        # Before the setup everything leads there.
        assert client.get("/", headers=HTML).headers["location"] == "/setup"
        assert client.get("/team/0002", headers=HTML).headers["location"] == "/setup"
        assert client.post("/team/0002/dismiss").status_code == 409
        page = client.get("/setup", headers={"accept-language": "de-DE,de;q=0.9"}).text
        assert "Firma einrichten" in page and 'lang="de"' in page
        assert "Set up the company" in client.get("/setup?lang=en").text
        data = {"company_name": "Testfirma", "language": "de", "name": "Paul Weber", "display_name": "Paul",
                "password": "short"}
        assert client.post("/setup", json=data).status_code == 409
        data["password"] = PASSWORD
        answer = client.post("/setup", json=data)
        assert answer.status_code == 200 and auth.COOKIE in answer.cookies
        assert client.post("/setup", json=data).status_code == 409
        # The owner is logged in at once and sees the UI in German.
        team = client.get("/", headers=HTML).text
        assert "KI-Mitarbeiter" in team and "Das Token für Claude fehlt" in team and "Aus Vorlage einstellen" in team
        assert client.get("/setup", headers=HTML).headers["location"] == "/"
    assert company.owner() == "0001" and (root / "handbook.md").is_file()
    log = subprocess.run(["git", "-C", str(root), "log", "--format=%s"], capture_output=True, text=True).stdout
    assert "Setup: company and owner" in log


def test_login(firm):
    auth.set_password(OWNER, PASSWORD)
    with TestClient(main.app, follow_redirects=False) as client:
        away = client.get(f"/team/{MILA}", headers=HTML)
        assert away.status_code == 303 and away.headers["location"] == f"/login?next=/team/{MILA}"
        assert client.post(f"/team/{MILA}/dismiss").status_code == 401
        assert client.get("/conversation/1/events").status_code == 401
        assert client.get("/static/style.css").status_code == 200 and "Password" in client.get("/login").text
        assert 'location.href = "/"' in client.get("/login?next=//evil.example").text
        assert client.post("/login", json={"name": "Paul", "password": "wrong"}).status_code == 401
        assert client.post("/login", json={"name": "Nobody", "password": PASSWORD}).status_code == 401
        assert client.post("/login", json={"name": "Mila", "password": PASSWORD}).status_code == 401
        # Name or display name, case and spaces do not matter.
        for name in ("paul ", "Paul Weber"):
            logged_in = client.post("/login", json={"name": name, "password": PASSWORD})
            assert logged_in.status_code == 200 and "secure" not in logged_in.headers["set-cookie"].lower()
        assert "Log out" in client.get("/", headers=HTML).text
        assert auth.person(client.cookies[auth.COOKIE]) == OWNER
        with db.connect() as con:
            stored = con.execute("SELECT token FROM sessions").fetchone()["token"]
            assert stored != client.cookies[auth.COOKIE] and PASSWORD not in con.execute(
                "SELECT password FROM people WHERE id = ?", (OWNER,)).fetchone()["password"]
        assert client.post("/logout").status_code == 200
        assert client.get("/", headers=HTML).status_code == 303


def test_login_cookie_only_over_https(firm, monkeypatch):
    monkeypatch.setattr(config, "COOKIE_SECURE", True)
    auth.set_password(OWNER, PASSWORD)
    with TestClient(main.app, follow_redirects=False) as client:
        answer = client.post("/login", json={"name": "Paul", "password": PASSWORD})
        assert answer.status_code == 200 and "secure" in answer.headers["set-cookie"].lower()


def test_foreign_sites_and_redirects(office):
    with web(follow_redirects=False) as client:
        # A form on another site, also another service on 127.0.0.1, changes nothing.
        for header in ({"sec-fetch-site": "cross-site"}, {"sec-fetch-site": "same-site"},
                       {"origin": "http://127.0.0.1:3000"}, {"origin": "null"}):
            assert client.post(f"/team/{MILA}/dismiss", headers=header).status_code == 403
        assert company.find(MILA) == f"employees/{MILA}"
        for header in ({"sec-fetch-site": "same-origin"}, {"origin": "http://testserver"}, {}):
            assert client.post(f"/team/{MILA}/favorite", json={"favorite": True}, headers=header).status_code == 200
        assert client.get(f"/team/{MILA}", headers={"sec-fetch-site": "cross-site"}).status_code == 200
        # The channel checks its token, not the origin.
        assert client.post(f"/channel/{MILA}/message", json={"text": "x"},
                           headers={"sec-fetch-site": "cross-site"}).status_code == 404
    for address in ("//evil.example", "/\\evil.example", "/\t/evil.example", "https://evil.example", "evil"):
        assert not main._own_page(address), address
    assert main._own_page(f"/team/{MILA}?x=1")


def test_about_offers_license_and_source(firm, monkeypatch):
    """The AGPL asks that everyone who uses the software over the network is offered its source."""
    source = f'<a href="{main.SOURCE}">'
    with TestClient(main.app) as client:
        login = client.get("/login").text
        assert source in login and "AGPLv3" in login and 'href="/about"' not in login
        assert client.get("/about").status_code == 401  # the page itself needs a login: it names the version
    with web() as client:
        monkeypatch.setattr(config, "VERSION", "")
        assert "not built with deploy" in client.get("/about").text
        monkeypatch.setattr(config, "VERSION", "b9a95ff-modified")
        page = client.get("/about").text
        assert f'{main.SOURCE}/commit/b9a95ff"' in page and "not committed yet" in page and main.SUPPORT in page and main.AUTHOR[1] in page
        assert f"{main.SOURCE}/security/advisories/new" in page and source in client.get(f"/team/{MILA}").text
        assert 'href="/about"' in client.get("/").text
        assert 'class="footer"' in client.get("/").text
        cid = client.post(f"/chat/{MILA}/message", json={"text": "Hello"}).json()["conversation"]
        assert 'class="footer"' not in client.get(f"/conversation/{cid}").text  # the chat fills the window


def test_pages_load_nothing_from_foreign_addresses(firm):
    """A Markdown image in an answer would otherwise carry data out of the company without a click."""
    with web(follow_redirects=False) as client:
        for response in (client.get(f"/team/{MILA}"), client.post(f"/team/{MILA}/dismiss",
                                                                  headers={"sec-fetch-site": "cross-site"})):
            policy = response.headers["content-security-policy"]
            assert "img-src 'self';" in policy and "connect-src 'self';" in policy and "frame-ancestors 'none'" in policy
            assert response.headers["referrer-policy"] == "no-referrer"
    assert '<img src="https://evil.example/' in str(main.templates.env.filters["markdown"]("![x](https://evil.example/)"))


# ---------------------------------------------------------------- Pages

def test_card_shows_the_latest_conversations(firm):
    with db.connect() as con:
        for i in range(12):
            con.execute("INSERT INTO conversations (employee, person, claude_session, status, started, last_activity) "
                        "VALUES (?, ?, ?, 'ended', ?, ?)", (MILA, OWNER, f"s{i}", 1_790_000_000 + i * 86400, 0))
    with web() as client:
        card = client.get(f"/team/{MILA}").text
        assert card.count('href="/conversation/') == main.CONVERSATIONS_ON_CARD
        assert 'href="/conversation/12"' in card and 'href="/conversation/2"' not in card
        assert "All conversations (12)" in card
        everything = client.get(f"/team/{MILA}/conversations").text
        assert everything.count('href="/conversation/') == 12 and "09/2026" in everything and "10/2026" in everything
        assert client.get("/team/9999/conversations").status_code == 404
        assert "All conversations" not in client.get(f"/team/{NEW}").text


def test_reopen_in_the_web_chat(firm):
    with web() as client:
        cid = client.post(f"/chat/{MILA}/message", json={"text": "Hello"}).json()["conversation"]
        assert client.post(f"/conversation/{cid}/reopen").status_code == 409  # still open
        assert client.post("/conversation/999/reopen").status_code == 404
        client.post(f"/conversation/{cid}/end")
        wait_until_ended(cid)
        assert 'data-state="ended"' in client.get(f"/conversation/{cid}").text
        assert 'data-conversation=""' in client.get(f"/chat/{MILA}").text
        assert client.post(f"/conversation/{cid}/reopen").json()["state"] == "open"
        chat = client.get(f"/chat/{MILA}").text
        assert f'data-conversation="{cid}"' in chat and "Conversation continued." in chat and 'id="reopen"' in chat


def test_own_messages_are_shown_as_typed(firm):
    # A pasted table keeps its lines and tabs, "1." is no list and HTML stays text.
    text = "Holiday\tDate\n1. Christmas Day\t25.12.2027\n\n**bold** <b>x</b>"
    with web() as client:
        html = client.post(f"/chat/{MILA}/message", json={"text": text}).json()["html"]
        assert '<div class="text raw">Holiday\tDate\n1. Christmas Day\t25.12.2027\n\n**bold** &lt;b&gt;x&lt;/b&gt;</div>' in html
        assert "<ol" not in html


def test_personnel_pages(office):
    with web() as client:
        card = client.get(f"/team/{MILA}").text
        for part in ("Start assessment", "Dismiss", "Agreement", "No agreement yet.", "Own changes to the profile",
                     "apply at once", "Mila has web access or services and has never been assessed."):
            assert part in card, part
        draft = client.get(f"/team/{NEW}").text
        assert "Discard draft" in draft and 'id="hire"' in draft and "Start assessment" not in draft
        assert client.post(f"/team/{NEW}/assessment").status_code == 409
        assert client.post(f"/team/{MILA}/probation/confirm").status_code == 404
        # Own changes can be switched off on the card; they then need a confirmation.
        assert client.post(f"/team/{MILA}/header", json={"fields": {"self_edit": "no"}}).json()["changed"] == ["self_edit"]
        assert "need your confirmation" in client.get(f"/team/{MILA}").text
        assert client.post(f"/team/{MILA}/dismiss").json()["archive"].startswith(f"{MILA}-")
        assert client.get(f"/team/{MILA}").status_code == 404
        assert "Archive (1)" in client.get("/").text


def test_feedback_pages(office):
    with web() as client:
        card = client.get(f"/team/{MILA}").text
        assert "Start feedback meeting" in card and "Last feedback meeting: none yet" in card and "5 words)" in card
        assert "In the assessment the ticked reviewers play through up to 5 situations with Mila" in card
        # Without an open approval request no section for it. Transcripts are only at their conversation.
        assert 'id="approvals"' not in card and "journal/2026-01-05-0900.md" not in card
        assert "Last conversation" not in card
        assert "so Ida does it" in client.get(f"/team/{CORA}").text
        assert 'id="feedback"' not in client.get(f"/team/{NEW}").text
        jid = client.post(f"/team/{MILA}/feedback", json={"brief": "Please shorter."}).json()["job"]
        assert client.get(f"/team/{MILA}").status_code == 200  # card with a running feedback meeting
        wait_for_job(client, jid)
        meeting = company.feedback_state(f"employees/{MILA}")["open"]
        assert "Feedback: decision open" in client.get("/").text
        assert "View and decide" in client.get(f"/team/{MILA}").text
        path = f"/team/{MILA}/feedback/{meeting}"
        text = client.get(path).text
        for part in ("Please shorter.", "Evaluation by Cora", "Sample run without an LLM", "Profile: 5 → <strong",
                     ">11</strong>", "Sample line of the dummy", "Accept", "Reject", "writes the new profile"):
            assert part in text, part
        assert client.get(f"/team/{MILA}/feedback/2026-01-01-0000").status_code == 404
        assert client.get(f"/team/{MILA}/feedback/latest").status_code == 404
        other = client.post(path + "/diff", json={"text": "- Completely different"}).json()
        assert "+- Completely different" in other["diff"] and other["words"] == 3
        version = company.feedback_proposal(f"employees/{MILA}", meeting)["version"]
        assert client.post(path + "/decision", json={"accept": True, "version": "old"}).status_code == 409
        result = client.post(path + "/decision", json={"accept": True, "version": version}).json()
        assert result["result"] == "accepted"
        assert client.post(path + "/decision", json={"accept": False}).status_code == 409
        assert "Sample line of the dummy" in (office / "profile.md").read_text(encoding="utf-8")
        text = client.get(path).text
        assert "accepted on" in text and 'id="accept"' not in text and "evaluation.md" in text
        assert "accepted on" in client.get(f"/team/{MILA}").text


def test_pages(firm):
    company.set_field(firm / "employees" / MILA / "profile.md", "roles", "hr,reviewer")
    with web() as client:
        assert "Hiring interview" in client.get("/").text
        assert "Hiring interview" in client.get("/hiring").text
        assert 'id="hiring"' not in client.get(f"/team/{MILA}").text
        assert client.post(f"/team/{NEW}/hire").json()["hint"] is None
        assert "Chat" in client.get(f"/team/{NEW}").text
        card = client.get(f"/team/{NEW}").text
        assert "Start assessment" in card and f'<input type="checkbox" value="{MILA}" checked> Mila' in card
        assert client.post(f"/team/{NEW}/assessment", json={"reviewers": []}).status_code == 409
        jid = client.post(f"/team/{NEW}/assessment", json={"reviewers": [MILA]}).json()["job"]
        assert client.post(f"/team/{NEW}/assessment").status_code == 409
        assert client.get(f"/team/{NEW}").status_code == 200  # card with a running assessment
        wait_for_job(client, jid)
        assert client.get(f"/job/{jid}").json()["progress"] == [["Mila", "done"]]
        assert "Review again" in client.get(f"/team/{NEW}").text
        # Editing: editor, diff, saving, log on the card, search.
        editor = client.get(f"/team/{MILA}/edit?path=profile.md").text
        assert "You are Mila." in editor and "tools:" not in editor
        version = company.for_editing(f"employees/{MILA}", "profile.md")[1]
        data = {"path": "profile.md", "text": "## Task\n\nYou are Mila, newly written.", "kind": "factual",
                "reason": "Sample", "version": version}
        assert "+You are Mila, newly written." in client.post(f"/team/{MILA}/diff", json=data).json()["diff"]
        assert client.post(f"/team/{MILA}/edit", json=data).status_code == 200
        assert client.post(f"/team/{MILA}/edit", json=data).status_code == 409  # stale version
        assert client.get(f"/team/{MILA}/edit?path=edits.md").status_code == 404
        card = client.get(f"/team/{MILA}").text
        assert "newly written" in card and "Factual correction" in card and "Sample" in card and ">Paul<" in card
        assert 'data-copy="## Task\n\nYou are Mila, newly written."' in card  # the source, not the rendered text
        assert "Edit" in client.get(f"/team/{MILA}/file?path=profile.md").text
        assert "profile.md" in client.get("/search?q=newly written").text
        assert "at least three characters" in client.get("/search?q=ab").text


def test_without_hr_hiring_starts_from_a_preset(firm):
    with web(follow_redirects=False) as client:
        assert client.get("/hiring").headers["location"] == "/hire"
        team = client.get("/").text
        assert "/hiring" not in team and "nobody – without HR" in team
        hire = client.get("/hire").text
        assert "HR (hiring, onboarding, feedback)" in hire and 'id="form"' not in hire
        form = client.get("/hire?preset=1-hr").text
        assert 'id="form"' in form and "Paula" in form and "You are responsible for the humans" in form
        assert 'id="form"' in client.get("/hire?preset=own").text
        answer = client.post("/hire", json={"fields": {"name": "Paula", "title": "HR", "roles": "hr"},
                                             "text": "## Task\n\nYou do HR."}).json()
        assert answer["id"] == "0004" and company.holders("hr") == ["0004"]
        assert client.post("/hire", json={"fields": {"name": "Paula", "title": "HR"}, "text": "x"}).status_code == 409
        assert client.get("/hiring").status_code == 200


def test_favorites(office):
    def order(client):
        text = client.get("/").text
        return sorted((CORA, IDA, MILA), key=lambda some_id: text.index(f'data-favorite="{some_id}"'))

    with web() as client:
        assert order(client) == [CORA, IDA, MILA]  # by name
        assert f'data-favorite="{NEW}"' not in client.get("/").text  # candidates have no star
        for some_id in (MILA, IDA, MILA):
            assert client.post(f"/team/{some_id}/favorite", json={"favorite": True}).json() == {"favorite": True}
        assert order(client) == [IDA, MILA, CORA]
        assert client.get("/").text.count('aria-pressed="true"') == 2
        client.post(f"/team/{IDA}/favorite", json={"favorite": False})
        assert order(client) == [MILA, CORA, IDA]
        assert client.post("/team/9999/favorite", json={"favorite": True}).status_code == 404
        client.post(f"/team/{MILA}/dismiss")
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM favorites").fetchone()[0] == 0


def test_header_schedules_archive_channel(office, monkeypatch):
    company.set_field(office / "profile.md", "hired", "2026-01-01")
    with web(follow_redirects=False) as client:
        card = client.get(f"/team/{MILA}").text
        assert "Change key facts" in card and "Recurring tasks" in card
        assert client.post(f"/team/{MILA}/header", json={"fields": {"model": "opus"}}).json()["changed"] == ["model"]
        assert client.post(f"/team/{MILA}/header", json={"fields": {"services": "missing"}}).status_code == 409
        assert "There is no service" in client.post(f"/team/{MILA}/header",
                                                    json={"fields": {"services": "missing"}}).json()["detail"]
        assert client.post(f"/team/{MILA}/header", json={"fields": {"name": "Lisa"}}).json()["changed"] == ["name"]
        assert "Lisa" in client.get(f"/team/{MILA}").text and company.by_name("Lisa") == MILA
        sid = client.post(f"/team/{MILA}/schedule", json={"time": "07:00", "days": [0, 6], "text": "Day plan"}).json()["schedule"]
        assert client.post(f"/team/{MILA}/schedule", json={"time": "7", "days": [0], "text": "x"}).status_code == 409
        assert "Day plan" in client.get(f"/team/{MILA}").text and "Mon, Sun" in client.get(f"/team/{MILA}").text
        assert client.post(f"/schedule/{sid}/pause", json={"paused": True}).json() == {"paused": True}
        assert ">paused</span>" in client.get(f"/team/{MILA}").text and "Resume" in client.get(f"/team/{MILA}").text
        assert client.post(f"/schedule/{sid}/pause", json={"paused": False}).json() == {"paused": False}
        assert client.post("/schedule/999/pause", json={"paused": True}).status_code == 404
        cid = client.post(f"/schedule/{sid}/run").json()["conversation"]
        assert main.conversations.get(cid)["channel"] == "schedule"
        assert main.conversations.messages(cid)[0]["text"] == "Day plan"
        assert client.post("/schedule/999/run").status_code == 404
        assert client.post(f"/schedule/{sid}/delete").status_code == 200
        assert client.post(f"/schedule/{sid}/delete").status_code == 404
        assert "bring in Ida" in client.get(f"/chat/{MILA}").text
        m = client.post(f"/chat/{MILA}/message", json={"text": "Have a look", "to": IDA}).json()
        assert "<strong>→ Ida:</strong> Have a look" in m["html"]
        assert client.post(f"/chat/{MILA}/message", json={"text": "x", "to": NEW}).status_code == 409
        for _ in range(200):
            if main.conversations.state(m["conversation"])["state"] != "running":
                break
            time.sleep(0.02)
        client.post(f"/conversation/{m['conversation']}/end")
        wait_until_ended(m["conversation"])
        folder = client.post(f"/team/{MILA}/dismiss").json()["archive"]
        archive = client.get(f"/archive/{folder}").text
        assert "Rehire" in archive and "You are Mila." in archive and "2026-01-05-0900.md" in archive
        assert "# Conversation" not in archive
        assert "Conversation" in client.get(f"/archive/{folder}/file?path=journal/2026-01-05-0900.md").text
        assert client.get("/archive/nonsense").status_code == 404
        assert client.get(f"/archive/{folder}/file?path=../../.env").status_code == 404
        assert client.post(f"/archive/{folder}/rehire").json()["id"] == MILA
        assert client.get(f"/team/{MILA}").status_code == 200

        # Channel: switched off without a token, with a token a message in and the answer out.
        assert client.post(f"/channel/{MILA}/message", json={"text": "Hello"}).status_code == 404
        monkeypatch.setattr(config, "CHANNEL_TOKEN", "secret")
        assert client.post(f"/channel/{MILA}/message", json={"text": "Hello"}).status_code == 401
        token = {"authorization": "Bearer secret"}
        assert client.post(f"/channel/{MILA}/message", json={"text": "Hello", "person": "9999"},
                           headers=token).status_code == 409
        answer = client.post(f"/channel/{MILA}/message", json={"text": "Hello from outside", "channel": "telegram"},
                             headers=token).json()
        assert answer["answers"][0]["sender"] == MILA and "Hello from outside" in answer["answers"][0]["text"]
        assert main.conversations.get(answer["conversation"])["channel"] == "telegram"
        assert main.conversations.get(answer["conversation"])["person"] == OWNER
        # The channel needs no login, only its token.
        client.post("/logout")
        assert client.post(f"/channel/{MILA}/message", json={"text": "Hello", "wait": False},
                           headers=token).status_code == 200


def test_person_page_and_documents(firm):
    with web() as client:
        page = client.get(f"/people/{OWNER}").text
        assert "About Paul" in page and "Set password" in page and "Your profile" in page
        assert client.get("/people/9999").status_code == 404
        assert client.post(f"/people/{OWNER}/password", json={"password": "short"}).status_code == 409
        answer = client.post(f"/people/{OWNER}/profile", json={"name": "Paul Weber", "display_name": "Pauli",
                                                               "language": "de", "about": "About Paul"}).json()
        assert answer["changed"] == ["display_name", "language"]
        # The UI is German now, the company stays English.
        assert "Dein Profil" in client.get(f"/people/{OWNER}").text and company.language() == "en"
        assert client.post(f"/people/{OWNER}/profile", json={"name": "Mila", "language": "de"}).status_code == 409
        # Handbook and the own text are edited like files of employees.
        assert "Handbook text" in client.get("/handbook").text
        editor = client.get("/document/edit?path=handbook.md").text
        version = re.search(r'data-version="(\w+)"', editor).group(1)
        assert client.post("/document/edit", json={"path": "handbook.md", "text": "New rules", "version": version}).status_code == 200
        assert (firm / "handbook.md").read_text() == "New rules\n"
        assert client.get("/document/edit?path=mcp.json").status_code == 404
        assert client.get("/document/edit?path=people/0009/profile.md").status_code == 403
        version = company.for_editing(None, f"people/{OWNER}/profile.md")[1]
        assert client.post("/document/edit", json={"path": f"people/{OWNER}/profile.md", "text": "Short answers.",
                                                   "version": version}).status_code == 200
        assert company.person(OWNER)["about"] == "Short answers."
        # Setting the password ends the login.
        assert client.post(f"/people/{OWNER}/password", json={"password": "another long one"}).status_code == 200
        assert client.get("/", headers=HTML, follow_redirects=False).status_code == 303


def test_services_page(firm):
    with web() as client:
        page = client.get("/services").text
        assert "todo" in page and "Authorization" in page and "Bearer secret" not in page
        assert "A token of the subscription is set" in page
        assert client.post("/services", json={"name": "calendar", "type": "http", "url": "http://cal:8000/mcp",
                                              "header_name": "Authorization", "header_value": "Bearer cal"}).status_code == 200
        assert "calendar" in client.get("/services").text and "Bearer cal" not in client.get("/services").text
        assert client.post("/services", json={"name": "x y", "url": "http://a"}).status_code == 409
        assert client.post("/services/calendar/delete").status_code == 200
        assert client.post("/services/calendar/delete").status_code == 404
        assert client.post("/token", json={"kind": "api", "value": "sk-ant-api03-1"}).json() == {"token": "api"}
        assert client.post("/token", json={"kind": "api", "value": ""}).status_code == 409


PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32


def test_company_name_and_logo(firm):
    with web() as client:
        # The software keeps its name and anchor; the company stands beside it.
        page = client.get("/settings").text
        assert "/static/icons/icon-128.png" in page and "/logo?v=" not in page
        assert ">Ancroo</span>" in page and ">Crew</span>" in page and "firm-claim" not in page

        assert client.post("/settings/name", json={"name": "  Muster   GmbH "}).json() == {"hint": None}
        page = client.get("/").text
        assert company.company_name() == "Muster GmbH" and "Muster GmbH" in page and "Muster GmbH · Ancroo Crew</title>" in page
        assert client.post("/settings/name", json={"name": " "}).status_code == 409

        # The claim is optional and short; empty removes it.
        assert client.post("/settings/claim", json={"claim": " Wir  bauen Brücken "}).json() == {"hint": None}
        assert company.claim() == "Wir bauen Brücken" and '<span class="firm-claim">Wir bauen Brücken</span>' in client.get("/").text
        assert client.post("/settings/claim", json={"claim": "x" * (company.CLAIM_LENGTH + 1)}).status_code == 409
        assert company.claim() == "Wir bauen Brücken"
        assert client.post("/settings/claim", json={"claim": ""}).status_code == 200
        assert company.claim() == "" and "firm-claim" not in client.get("/").text

        # The language of the company can change; an unknown one falls back to the default like everywhere.
        before = company.language()
        other = next(lang for lang in i18n.LANGUAGES if lang != before)
        assert client.post("/settings/language", json={"language": other}).json() == {"hint": None}
        assert company.language() == other and f'<option value="{other}" selected>' in client.get("/settings").text
        assert client.post("/settings/language", json={"language": "xx"}).status_code == 200
        assert company.language() == i18n.DEFAULT
        company.set_language(before)

        assert client.post("/settings/logo", json={"data": base64.b64encode(PNG).decode()}).status_code == 200
        assert (firm / "logo.png").read_bytes() == PNG
        page = client.get("/").text
        assert "/logo?v=" in page and "/static/icons/icon-128.png" in page and 'rel="icon" href="/logo' not in page
        answer = client.get("/logo")
        assert answer.content == PNG and answer.headers["content-type"] == "image/png"
        assert answer.headers["x-content-type-options"] == "nosniff"

        # Another type replaces the logo. The type comes from the bytes, not from a name.
        jpeg = b"\xff\xd8\xff\xe0" + b"\0" * 32
        assert client.post("/settings/logo", json={"data": base64.b64encode(jpeg).decode()}).status_code == 200
        assert company.logo() == firm / "logo.jpg" and not (firm / "logo.png").exists()
        assert client.get("/logo").headers["content-type"] == "image/jpeg"

        # No SVG, no HTML, nothing that is not Base64, nothing too big.
        for data in (b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', b"<html>", b""):
            assert client.post("/settings/logo", json={"data": base64.b64encode(data).decode()}).status_code == 409
        assert client.post("/settings/logo", json={"data": "not base64!"}).status_code == 409
        big = PNG + b"\0" * company.LOGO_KB * 1024
        assert client.post("/settings/logo", json={"data": base64.b64encode(big).decode()}).status_code == 409
        assert company.logo() == firm / "logo.jpg"

        # The login page shows the logo, so /logo needs no login.
        client.post("/logout")
        assert client.get("/logo").status_code == 200
        assert "/logo?v=" in client.get("/login").text

    with web() as client:
        assert client.post("/settings/logo/delete").status_code == 200
        assert client.post("/settings/logo/delete").status_code == 404
        assert client.get("/logo").status_code == 404 and company.logo() is None
    log = subprocess.run(["git", "-C", str(firm), "log", "--format=%s"], capture_output=True, text=True).stdout
    assert "Company renamed by 0001" in log and "Logo changed by 0001" in log and "Logo removed by 0001" in log
    assert "Claim changed by 0001" in log


def test_usage_is_booked_and_shown(office):
    class Expensive(DummyRunner):
        """Fails with Ida but names usage, and names only costs with Cora in a conversation."""
        delay = 0

        async def run(self, task):
            async for e in super().run(task):
                if e["kind"] == "end" and task.rel == f"employees/{IDA}":
                    e = e | {"error": "broken"}
                elif e["kind"] == "end" and task.rel == f"employees/{CORA}" and task.procedure == "conversation":
                    e = e | {"usage": [], "cost": 0.25}
                yield e

    async def flow():
        service = Conversations(quick())
        cid = service.send(MILA, OWNER, "Hello Mila")["conversation_id"]
        await settle(service)
        service.end(cid)
        await settle(service)
        personnel = Personnel(Conversations(HRDesk()))
        jid = personnel.start_feedback(MILA, OWNER)
        await settle(personnel.conversations)
        expensive = Conversations(Expensive())
        for who in (IDA, CORA):
            expensive.send(who, OWNER, "Hello")
            await settle(expensive)
        return cid, jid

    cid, jid = asyncio.run(flow())
    with db.connect() as con:
        rows = [dict(r) for r in con.execute("SELECT * FROM usage ORDER BY id")]
    # The conversation costs one run, the end none.
    assert [(r["employee"], r["procedure"], r["purpose"], r["conversation_id"]) for r in rows
            if r["conversation_id"] == cid] == [(MILA, "conversation", "conversation", cid)]
    assert rows[0]["model"] == "dummy" and rows[0]["input"] == len("Hello Mila") // 4 and rows[0]["output"]
    feedback = [r for r in rows if r["job_id"] == jid]
    assert {r["purpose"] for r in feedback} == {"feedback"} and [r["procedure"] for r in feedback] == ["feedback"]
    assert [(r["employee"], r["failed"]) for r in rows if r["employee"] == IDA and r["conversation_id"]] == [(IDA, 1)]
    cora = [r for r in rows if r["employee"] == CORA and r["conversation_id"]]
    assert [(r["model"], r["cost_usd"], r["input"]) for r in cora] == [("unknown", 0.25, 0)]

    overview = usage.overview(30)
    assert overview["sums"]["runs"] == len({r["run"] for r in rows}) and overview["sums"]["failed"] == 1
    assert overview["sums"]["total"] == sum(r["input"] + r["output"] for r in rows)
    assert overview["history"][-1]["day"] == date.today().isoformat() and overview["peak"] == overview["sums"]["total"]
    assert {r["model"] for r in overview["model"]} == {"dummy", "unknown"}
    assert ("conversation", "conversation") in {(r["procedure"], r["purpose"]) for r in overview["prompt"]}

    with web() as client:
        assert 'href="/usage"' in client.get("/").text
        text = client.get("/usage").text
        assert "By model" in text and "Feedback meeting" in text and "<code>feedback</code>" in text
        assert "1 with error" in text and "0.25 $" in text
        # A period without runs: everything is in the past.
        with db.connect() as con:
            con.execute("UPDATE usage SET time = time - 86400 * 3")
        assert "no booked runs" in client.get("/usage?days=1").text
        assert "By model" in client.get("/usage?days=7").text
        assert client.get("/usage?days=12").status_code == 200  # unknown period means everything
        company.update_person(OWNER, "Paul Weber", "Paul", "de", "")
        assert "0,25 $" in client.get("/usage").text and "Nach Modell" in client.get("/usage").text


def test_notices_page(firm):
    with web() as client:
        assert "unread notice" not in client.get("/").text and "No unread notices." in client.get("/notices").text
        notices.post(MILA, OWNER, None, "Two new studies: [A](https://example.org/a)")
        notices.post(MILA, "9999", None, "For someone else")
        assert "1 unread notice" in client.get("/").text
        text = client.get("/notices").text
        assert '<a href="https://example.org/a">A</a>' in text and "For someone else" not in text
        assert "Two new studies" not in client.get("/notices?read=1").text
        assert client.post("/notices/read", json={}).json() == {"marked": 1}
        # A read notice moves from the default view to "Read".
        text = client.get("/notices").text
        assert "1 unread notice" not in text and "Two new studies" not in text and "No unread notices." in text
        assert "Two new studies" in client.get("/notices?read=1").text
        # A notice from a conversation leads there; while unread, the link marks it as read on the way.
        cid = client.post(f"/chat/{MILA}/message", json={"text": "Check your sources."}).json()["conversation"]
        nid = notices.post(MILA, OWNER, cid, "One new study")
        assert f'href="/conversation/{cid}" data-open="{nid}">Open conversation</a>' in client.get("/notices").text


def test_page_scripts_are_valid(office, tmp_path):
    """A syntax error disables all buttons of a page without the server noticing."""
    if not shutil.which("node"):
        pytest.skip("node missing")
    company.set_field(office / "profile.md", "hired", "2026-01-01")

    def check(name: str, text: str):
        scripts = re.findall(r"<script>(.*?)</script>", text, re.S)
        assert scripts, f"{name}: no script found"
        for n, script in enumerate(scripts):
            file = tmp_path / f"{name}-{n}.js"
            file.write_text(script, encoding="utf-8")
            result = subprocess.run(["node", "--check", str(file)], capture_output=True, text=True)
            assert result.returncode == 0, f"{name}: {result.stderr[:400]}"

    with web() as client:
        jid = client.post(f"/team/{MILA}/feedback", json={}).json()["job"]
        wait_for_job(client, jid)
        meeting = company.feedback_state(f"employees/{MILA}")["open"]
        folder = client.post(f"/team/{IDA}/dismiss").json()["archive"]
        company.commit("Start")
        version = company.profile_versions(f"employees/{MILA}")[0]["commit"]
    pages = {"card-employee": f"/team/{MILA}", "card-candidate": f"/team/{NEW}", "card-hr": f"/team/{CORA}",
             "feedback": f"/team/{MILA}/feedback/{meeting}", "edit": f"/team/{MILA}/edit?path=profile.md",
             "document": "/document/edit?path=handbook.md", "archive": f"/archive/{folder}",
             "person": f"/people/{OWNER}", "services": "/services", "settings": "/settings", "hire": "/hire?preset=1-hr",
             "team": "/", "version": f"/team/{MILA}/profile/{version}", "notices": "/notices",
             "sandbox": "/sandbox"}
    sandbox.set_enabled(True)  # the page has its script only then
    for lang in ("en", "de"):
        company.update_person(OWNER, "Paul Weber", "Paul", lang, "")
        with web() as client:
            for name, path in pages.items():
                answer = client.get(path)
                assert answer.status_code == 200, path
                check(f"{lang}-{name}", answer.text)
            client.post("/logout")
            check(f"{lang}-login", client.get("/login").text)
    for file in (main.HERE / "static").glob("*.js"):
        assert subprocess.run(["node", "--check", str(file)], capture_output=True).returncode == 0, file.name


# ---------------------------------------------------------------- Languages

def _used_keys() -> set[str]:
    """Every text key the code and the templates name literally."""
    root = Path(main.HERE)
    keys = set()
    for file in (root / "templates").glob("*.html"):
        text = file.read_text(encoding="utf-8")
        keys |= set(re.findall(r'\btn?\(\s*"([a-z_]+\.[a-z_.]+[a-z_])"', text))
    for file in root.glob("*.py"):
        text = file.read_text(encoding="utf-8")
        keys |= set(re.findall(r'i18n\.tn?\([^,()]+(?:\([^()]*\))?,\s*"([a-z_]+\.[a-z_.]+)"', text))
        keys |= {"err." + k for k in re.findall(r'(?:Rejected|Invalid|Unknown)\(\s*"([a-z_]+)"', text)}
        keys |= {"sys." + k for k in re.findall(r'_say\(\s*cid,\s*"([a-z_]+)"', text)}
        keys |= {"job." + k for k in re.findall(r'_report\(\s*jid,\s*\w+,\s*"([a-z_]+)"', text)}
        keys |= {"hint." + k for k in re.findall(r'hint\(\s*request,\s*"([a-z_]+)"', text)}
    return keys


def test_both_languages_have_the_same_texts():
    de, en = i18n.texts("de"), i18n.texts("en")
    assert set(de) == set(en), set(de) ^ set(en)
    for key in de:
        placeholders = lambda text: set(re.findall(r"\{(\w+)\}", text))
        assert placeholders(de[key]) == placeholders(en[key]), key


def test_every_text_the_code_names_exists():
    texts = i18n.texts("en")
    missing = sorted(k for k in _used_keys() if k not in texts and f"{k}.one" not in texts)
    assert not missing, missing
    # Keys put together from values.
    for prefix, values in (("role.", company.ROLES), ("status.", ("candidate", "employee")),
                           ("profile.note.", ("applied", "proposed", "too_long", "undone", "accepted", "discarded")),
                           ("profile.state.", ("applied", "proposed", "too_long", "undone", "accepted", "discarded")),
                           ("edit.kind.", company.EDIT_KINDS), ("usage.kind.", usage.KINDS),
                           ("usage.purpose.", usage.PURPOSES), ("lang.", i18n.LANGUAGES),
                           ("team.role_missing.", company.ROLES[0:]), ("folder.", ("assignments", "assessment",
                                                                                    "journal", "feedback")),
                           ("svc.token_set.", tuple(company.TOKEN_KINDS)),
                           ("conv.status.", ("open", "paused", "ending")),
                           ("file.decision_", ("accepted", "accepted_edited", "rejected", "no_changes")),
                           ("err.works_in_", ("assessment", "feedback")),
                           ("file.kind.", KINDS), ("file.verdict.", VERDICTS)):
        for value in values:
            assert prefix + value in texts, prefix + value


def test_translation_helpers():
    assert i18n.t("de", "err.name_taken", name="Mila").startswith("In der Firma heißt schon jemand „Mila“")
    assert i18n.t("xx", "nav.team") == "Team" and i18n.t("en", "no.such.key") == "no.such.key"
    assert i18n.t("en", "err.name_taken") .count("{name}") == 1  # a missing value stays visible
    assert i18n.tn("de", "log.calls", 1) == "1 Werkzeugaufruf" and i18n.tn("en", "log.calls", 3) == "3 tool calls"
    assert i18n.from_header("fr-FR,de;q=0.8") == "de" and i18n.from_header(None) == "en"
