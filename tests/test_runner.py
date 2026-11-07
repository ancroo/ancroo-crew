import json

import pytest

from app import company, config
from app.errors import Invalid
from app.runner import DockerRunner, StreamParser, Task
from conftest import MILA, NEW

M, N = f"employees/{MILA}", f"candidates/{NEW}"


def test_docker_command(firm):
    task = Task(rel=M, message="hello", session="s-1", new=True, writable=[f"{M}/journal"], services=True)
    c = DockerRunner().command(task, "run")
    # Mila reads on the web. So she sees only her own folder, not those of colleagues and candidates.
    assert f"{firm / M}:/company/{M}:ro" in c
    assert not any(":/company/employees:" in part or ":/company/candidates" in part for part in c)
    assert f"{firm / M / 'journal'}:/company/{M}/journal" in c
    assert f"{firm / 'handbook.md'}:/company/handbook.md:ro" in c
    assert f"{config.RUNTIME / 'procedures'}:/company/procedures:ro" in c
    assert (config.RUNTIME / "procedures" / "review.md").is_file()
    prompt = (config.RUNTIME / f"prompts/employees-{MILA}-conversation.md").read_text(encoding="utf-8")
    assert "You cannot read other folders" in prompt and "You can read everything there" not in prompt
    # What the task names is added, e.g. the folder of whoever she reviews.
    reviewing = DockerRunner().command(Task(rel=M, message="x", readable=[N]), "run")
    assert f"{firm / N}:/company/{N}:ro" in reviewing
    # Whoever does not read on the web still sees the whole company.
    company.set_field(firm / N / company.PROFILE, "tools", "")
    whole = DockerRunner().command(Task(rel=N, message="x", writable=[N]), "run")
    assert f"{firm / 'employees'}:/company/employees:ro" in whole and f"{firm / 'candidates'}:/company/candidates:ro" in whole
    assert f"{firm / N}:/company/{N}" in whole
    assert "You can read everything there" in (config.RUNTIME / f"prompts/candidates-{NEW}-conversation.md").read_text()
    assert c[c.index("--session-id") + 1] == "s-1" and "--resume" not in c
    assert c[c.index("--tools") + 1] == "Read,Glob,Grep,Write,Edit,WebSearch"
    assert c[c.index("--allowedTools") + 1].endswith(",mcp__todo__list_tasks")
    assert "mcp__todo__delete_all" in c[c.index("--disallowedTools"):]
    # Token in the environment, foreign histories and the settings of Claude Code are always blocked.
    without = DockerRunner().command(Task(rel=M, message="hello"), "run")
    for command in (c, without):
        blocked = command[command.index("--disallowedTools") + 1:]
        assert {"Read(//proc/**)", "Read(//home/agent/**)", "Edit(//home/agent/**)"} <= set(blocked)
    assert "Read(//mcp.json)" not in without and "--mcp-config" not in without
    assert "hello" not in c  # the message goes through standard input
    extract = json.loads((config.RUNTIME / f"mcp/employees-{MILA}.json").read_text())
    assert "blocked" not in extract["mcpServers"]["todo"] and "description" not in extract["mcpServers"]["todo"]

    task.new, task.services = False, False
    c = DockerRunner().command(task, "run")
    assert c[c.index("--resume") + 1] == "s-1" and "--mcp-config" not in c


def test_docker_command_hides_folders(firm):
    for path in (f"{M}/journal", f"{M}/assignments", f"{N}/journal"):
        (firm / path).mkdir(parents=True)
    hidden = [f"{M}/journal", f"{M}/assignments", f"{N}/journal", "employees/9999/journal"]
    company.set_field(firm / N / company.PROFILE, "tools", "")
    c = DockerRunner().command(Task(rel=N, message="x", hidden=hidden, writable=[f"{N}/journal"]), "run")
    empty = [c[i + 1] for i, part in enumerate(c) if part == "--tmpfs"]
    # What does not exist Docker cannot create in a read-only mount. What is writable stays visible.
    assert empty == [f"/company/{M}/journal:ro,size=1k", f"/company/{M}/assignments:ro,size=1k"]
    assert c.index("--tmpfs") > c.index(f"{firm / 'employees'}:/company/employees:ro")
    # Whoever reads on the web does not have the colleagues' folders mounted at all.
    c = DockerRunner().command(Task(rel=M, message="x", hidden=hidden), "run")
    assert [c[i + 1] for i, part in enumerate(c) if part == "--tmpfs"] == [
        f"/company/{M}/journal:ro,size=1k", f"/company/{M}/assignments:ro,size=1k"]
    assert "--tmpfs" not in DockerRunner().command(Task(rel=M, message="x"), "run")


def test_docker_command_for_the_own_folder(firm):
    """In a conversation the own folder is writable, but only the two parts of the profile are meant to change."""
    (firm / M / "agreement.md").write_text("Purpose", encoding="utf-8")
    protected = company.protected_in_own(M)
    hidden = [f"{M}/journal", f"{M}/assignments"]
    c = DockerRunner().command(Task(rel=M, message="x", writable=[M], protected=protected, hidden=hidden), "run")
    assert f"{firm / M}:/company/{M}" in c and f"{firm / M}:/company/{M}:ro" not in c
    for name in ("agreement.md", "feedback", "assessment"):
        assert f"{firm / M / name}:/company/{M}/{name}:ro" in c, name
    # Hidden folders get only their empty tmpfs; a second mount on the same path would stop Docker.
    assert f"{firm / M / 'journal'}:/company/{M}/journal:ro" not in c
    assert f"/company/{M}/journal:ro,size=1k" in c
    assert not any(f"/company/{M}/work.md" in part or f"/company/{M}/profile.md" in part for part in c)


def test_docker_command_in_own_network(firm, monkeypatch):
    task = Task(rel=M, message="x", services=True)
    # Default bridge without memory limit: the service resolves the hosts of the services itself.
    c = DockerRunner().command(task, "run")
    assert "--network" not in c and "--memory" not in c and "--add-host" in c
    monkeypatch.setattr(config, "NETWORK", "agents")
    monkeypatch.setattr(config, "RUN_MEMORY", "768m")
    c = DockerRunner().command(task, "run")
    assert c[c.index("--network") + 1] == "agents"
    assert c[c.index("--memory") + 1] == "768m" and c[c.index("--memory-swap") + 1] == "768m"
    # In an own network Docker resolves the names of the services; a fixed host would be in the way.
    assert "--add-host" not in c and "--mcp-config" in c
    assert c.index("--network") < c.index(config.IMAGE)


def test_telemetry_can_be_switched_off(firm, monkeypatch):
    task = Task(rel=M, message="x")
    # By default Claude Code runs as it comes.
    assert "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1" not in DockerRunner().command(task, "run")
    monkeypatch.setattr(config, "RUN_TELEMETRY", False)
    c = DockerRunner().command(task, "run")
    i = c.index("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1")
    assert c[i - 1] == "-e" and i < c.index(config.IMAGE)


def test_tools_from_the_profile(firm):
    # A space in the field does not matter.
    company.set_field(firm / M / company.PROFILE, "tools", "WebSearch, WebFetch")
    c = DockerRunner().command(Task(rel=M, message="x"), "run")
    assert c[c.index("--tools") + 1] == "Read,Glob,Grep,Write,Edit,WebSearch,WebFetch"
    # What is not allowed as an extra tool starts no run, even if an LLM wrote it into the profile.
    for tools in ("Bash", "WebSearch Bash", "Read(//proc/**)"):
        company.set_field(firm / N / company.PROFILE, "tools", tools)
        with pytest.raises(RuntimeError, match="not allowed"):
            DockerRunner().command(Task(rel=N, message="x"), "run")
    with pytest.raises(Invalid):
        company.set_header(N, {"tools": "WebFetch,Bash"})


def test_missing_token_stops_the_run(firm):
    (firm / ".env").unlink()
    with pytest.raises(RuntimeError, match="token"):
        DockerRunner().command(Task(rel=M, message="x"), "run")


def test_single_run_without_session(firm):
    task = Task(rel=M, message="hello", schema={"type": "object"}, writable=[N])
    c = DockerRunner().command(task, "run")
    assert "--no-session-persistence" in c and "--session-id" not in c and "--resume" not in c
    assert json.loads(c[c.index("--json-schema") + 1]) == {"type": "object"}
    assert f"{firm / M}:/company/{M}:ro" in c
    assert f"{firm / N}:/company/{N}" in c


def test_stream_parser():
    p = StreamParser()
    events = []
    for line in [
        {"type": "system", "subtype": "init"},
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "I "}}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "I am looking."}]}},
        {"type": "stream_event", "event": {"type": "content_block_start", "content_block": {"type": "tool_use", "name": "Read"}}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "WebFetch", "input": {"url": "https://example.org/a", "prompt": "What is there?"}}]}},
        {"type": "assistant", "parent_tool_use_id": "t1", "message": {"content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/x"}}]}},
        {"type": "stream_event", "event": {"type": "content_block_start", "content_block": {"type": "text"}}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Done."}]}},
        {"type": "result", "subtype": "success", "is_error": False, "result": "Done.", "total_cost_usd": 0.01,
         "permission_denials": [{"tool_name": "mcp__todo__delete_task"}, {"tool_name": "mcp__todo__delete_task"}]},
    ]:
        events += p.parse(line)
    assert [e["kind"] for e in events] == ["text", "tool", "call", "text", "end"]
    assert events[2] == {"kind": "call", "name": "WebFetch", "detail": "https://example.org/a"}
    assert events[-1]["text"] == "I am looking.\n\nDone."
    assert events[-1]["denied"] == ["mcp__todo__delete_task"]
    assert events[-1]["error"] is None
    assert StreamParser().parse({"type": "result", "subtype": "error_during_execution", "is_error": True})[0]["error"]


def test_stream_parser_usage():
    p = StreamParser()
    assert p.parse({"type": "system", "subtype": "init", "model": "claude-opus-5-5"}) == []
    usage = {"input_tokens": 3, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 900, "output_tokens": 40,
             "server_tool_use": {"web_search_requests": 2}}
    end = p.parse({"type": "result", "subtype": "success", "result": "x", "total_cost_usd": 0.5, "usage": usage,
                   "modelUsage": {
                       "claude-opus-5-5": {"inputTokens": 3, "outputTokens": 40, "cacheReadInputTokens": 900,
                                           "cacheCreationInputTokens": 100, "webSearchRequests": 2, "costUSD": 0.45},
                       "claude-haiku-5-5": {"inputTokens": 500, "outputTokens": 20, "costUSD": 0.05}}})[0]
    assert end["usage"] == [
        {"model": "claude-opus-5-5", "input": 3, "cache_write": 100, "cache_read": 900, "output": 40,
         "web_searches": 2, "cost": 0.45},
        {"model": "claude-haiku-5-5", "input": 500, "cache_write": 0, "cache_read": 0, "output": 20,
         "web_searches": 0, "cost": 0.05}]
    # Without numbers per model, "usage" counts for the model from the first line.
    end = p.parse({"type": "result", "subtype": "success", "result": "x", "total_cost_usd": 0.5, "usage": usage})[0]
    assert end["usage"] == [{"model": "claude-opus-5-5", "input": 3, "cache_write": 100, "cache_read": 900,
                             "output": 40, "web_searches": 2, "cost": 0.5}]
    assert StreamParser().parse({"type": "result", "subtype": "success", "result": "x"})[0]["usage"] == []
