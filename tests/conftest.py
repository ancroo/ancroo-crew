import asyncio
import json
from contextlib import contextmanager

import pytest

from app import auth, company, config, db
from app.runner import DummyRunner

OWNER = "0001"
MILA = "0002"
NEW = "0003"
CORA = "0004"
IDA = "0005"
PASSWORD = "long enough here"

PROFILE = """---
name: Mila
title: Assistant
model:
tools: WebSearch
services: todo__list_tasks
---

## Task

You are Mila.
"""


def employee(root, some_id: str, text: str, area: str = "employees"):
    folder = root / area / some_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / company.PROFILE).write_text(text, encoding="utf-8")
    return folder


@pytest.fixture
def firm(tmp_path, monkeypatch):
    """A small company: the owner Paul, Mila as employee and Neu as candidate."""
    root = tmp_path / "company"
    monkeypatch.setattr(config, "COMPANY_DIR", root)
    monkeypatch.setattr(config, "RUNTIME", root / "runtime")
    monkeypatch.setattr(config, "DATABASE", tmp_path / "test.db")
    monkeypatch.setattr(config, "BACKUPS", tmp_path / "backups")
    company.ensure()
    db.setup()
    company.write_profile(root / "company.md", {"name": "Test Company", "language": "en"}, "")
    assert company.create_person("Paul Weber", "Paul", "en", ["owner"], "About Paul") == OWNER
    db.add_person(OWNER)
    employee(root, MILA, PROFILE)
    employee(root, NEW, "---\nname: Neu\ntitle: Test\n---\n", "candidates")
    (root / "handbook.md").write_text("Handbook text", encoding="utf-8")
    (root / ".env").write_text("CLAUDE_CODE_OAUTH_TOKEN=x\n", encoding="utf-8")
    (root / "mcp.json").write_text(json.dumps({"mcpServers": {"todo": {
        "description": "Tasks", "blocked": ["delete_all"], "type": "http",
        "url": "http://localhost:9/mcp", "headers": {"Authorization": "Bearer secret"}}}}), encoding="utf-8")
    return root


@pytest.fixture
def office(firm):
    """Cora and Ida do HR, Mila has worked already."""
    for some_id, name in ((CORA, "Cora"), (IDA, "Ida")):
        employee(firm, some_id, f"---\nname: {name}\ntitle: Test\nroles: hr\nservices: foreign\n---\n\nText of {name}.\n")
    mila = firm / "employees" / MILA
    (mila / "journal").mkdir()
    (mila / "journal" / "2026-01-05-0900.md").write_text("# Conversation", encoding="utf-8")
    return mila


def quick():
    runner = DummyRunner()
    runner.delay = 0
    return runner


async def settle(service):
    while service.tasks:
        await asyncio.gather(*service.tasks)
        # A gather over tasks that are all done returns without yielding to the loop. A task that finished just now
        # leaves the set only in its done callback, which then never runs, and this loop would spin forever.
        await asyncio.sleep(0)


@contextmanager
def web(follow_redirects: bool = True, runner=None):
    """The web UI with the owner logged in."""
    from fastapi.testclient import TestClient
    from app import main
    before = main.conversations.runner
    main.conversations.runner = runner or quick()
    auth.set_password(OWNER, PASSWORD)
    try:
        with TestClient(main.app, follow_redirects=follow_redirects) as client:
            assert client.post("/login", json={"name": "Paul", "password": PASSWORD}).status_code == 200
            yield client
    finally:
        main.conversations.runner = before


def wait_for_job(client, jid):
    import time
    for _ in range(200):
        if client.get(f"/job/{jid}").json()["status"] != "running":
            return
        time.sleep(0.02)
