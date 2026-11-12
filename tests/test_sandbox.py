import time

from app import company, sandbox
from app.runner import DockerRunner, Task
from conftest import MILA, NEW, web

M, N = f"employees/{MILA}", f"candidates/{NEW}"


def test_describe_someone_who_reads_on_the_web(firm):
    task = Task(rel=M, message="x", writable=[f"{M}/journal"], services=True)
    cage = sandbox.describe(DockerRunner().command(task, "run"))
    assert cage["write"] == [f"{M}/journal"] and cage["read"] == ["handbook.md", "procedures", M]
    # Only the own folder of the employees; the other areas are not there at all.
    assert {"path": "employees", "partly": True} in cage["missing"]
    assert {"path": "candidates", "partly": False} in cage["missing"]
    assert cage["web"] == ["WebSearch"] and "Bash" in cage["never"] and "WebFetch" in cage["never"]
    assert {"Read(//proc/**)", "Read(//home/agent/**)", "Read(//mcp.json)"} <= set(cage["denied_rules"])
    assert cage["services"] == {"todo": 1} and "mcp__todo__delete_all" in cage["denied_service_tools"]
    assert cage["session"] == "none"
    # Paths inside the container only, never those of the host.
    assert str(firm) not in repr(cage)


def test_describe_the_own_folder(firm):
    company.set_field(firm / N / company.PROFILE, "tools", "")
    (firm / N / "agreement.md").write_text("Purpose", encoding="utf-8")
    (firm / M / "journal").mkdir()
    task = Task(rel=N, message="x", writable=[N], protected=company.protected_in_own(N), hidden=[f"{M}/journal"])
    cage = sandbox.describe(DockerRunner().command(task, "run"))
    assert cage["write"] == [N] and f"{N}/agreement.md" in cage["protected"]
    assert "employees" in cage["read"] and cage["hidden"] == [f"{M}/journal"]
    assert not any(m["path"] in company.AREAS for m in cage["missing"])
    assert cage["web"] == [] and cage["services"] == {}
    # As a tree: the own folder with its exceptions below, the hidden history in one line below the areas.
    own, reads, areas, missing = sandbox.tree(cage, [f"{N}/{part}" for part in company.PARTS])
    assert own["paths"] == [N] and [c["kind"] for c in own["children"]] == ["checked", "protected"]
    assert own["children"][0]["paths"] == ["profile.md", "work.md"] and "agreement.md" in own["children"][1]["paths"]
    assert reads["paths"] == ["handbook.md", "procedures"] and areas["paths"] == ["employees", "candidates"]
    assert areas["children"] == [{"paths": ["journal"], "kind": "hidden", "children": [], "count": 1,
                                  "full": [f"{M}/journal"]}]
    assert missing == {"paths": ["people/", "archive/"], "kind": "missing", "children": []}


def test_sandbox_view_in_the_web_ui(firm):
    sandbox.board.runs.clear()
    with web() as client:
        assert not sandbox.enabled()
        assert "The sandbox view is off" in client.get("/sandbox").text
        assert client.get("/sandbox/events").status_code == 404
        # Off, a run leaves nothing behind.
        client.post(f"/chat/{MILA}/message", json={"text": "Hello"})
        time.sleep(0.3)
        assert not sandbox.board.runs

        assert client.post("/settings/sandbox", json={"on": True}).json() == {"on": True}
        assert 'href="/sandbox"' in client.get("/").text
        client.post(f"/chat/{MILA}/message", json={"text": "Secret appointment"})
        for _ in range(200):
            if sandbox.board.runs and sandbox.board.runs[0]["status"] != "running":
                break
            time.sleep(0.02)
        run = sandbox.board.runs[0]
        assert run["employee"] == MILA and run["status"] == "done" and run["purpose"] == "conversation"
        assert run["checked"] == [f"{M}/profile.md", f"{M}/work.md"]
        page = client.get("/sandbox").text
        assert f'id="run-{run["id"]}"' in page and "WebSearch" in page and "Secret appointment" not in page

        assert 'id="clear"' in page
        assert client.post("/sandbox/clear").json() == {"runs": 0}
        assert not sandbox.board.runs and f'id="run-{run["id"]}"' not in client.get("/sandbox").text

        assert client.post("/settings/sandbox", json={"on": False}).json() == {"on": False}
        assert not sandbox.board.runs and 'href="/sandbox"' not in client.get("/").text


def test_clearing_tells_whoever_watches():
    import asyncio

    async def flow():
        board = sandbox.Board()
        board.runs.append({"id": 1})
        seen = []

        async def watch():
            async for run in board.watch():
                seen.append(run)
                return

        watcher = asyncio.create_task(watch())
        await asyncio.sleep(0)
        board.clear()
        await watcher
        return board, seen

    board, seen = asyncio.run(flow())
    assert seen == [None] and not board.runs
