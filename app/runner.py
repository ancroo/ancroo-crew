"""The layer to the LLM. A runner gets a task and yields events.

The rest of the service only knows this interface. Whoever wants to connect another LLM later writes another runner
with the same method "run".

Events are dictionaries:
  {"kind": "text", "text": "..."}        a piece of the answer while it is being written
  {"kind": "tool", "name": "..."}        the employee is using a tool right now
  {"kind": "call", "name": "...", "detail": "..."}   the finished call: which address, search or file
  {"kind": "end", "text": "...", "denied": [...], "cost": 0.0, "error": None}
The end can also hold "result" (only the last answer, without intermediate texts), "structured" (answer following the
schema of the task) and "usage". Usage is a list with one entry per model: {"model", "input", "cache_write",
"cache_read", "output", "web_searches", "cost"}. The tokens are those of the whole run, over all turns.
"""
import asyncio
import json
import os
import uuid
from dataclasses import dataclass, field
from typing import AsyncIterator

from . import company, config


@dataclass
class Task:
    rel: str                       # folder of the employee, relative to the company
    message: str
    session: str | None = None     # session ID of Claude Code. Without: a single run that leaves no session
    new: bool = True               # True: the session begins, False: the session continues
    procedure: str = "conversation"  # file in app/procedures, without extension
    context: str = ""              # lines for the section "Workplace" in the prompt
    writable: list[str] = field(default_factory=list)  # folders relative to the company
    protected: list[str] = field(default_factory=list)  # read-only inside a writable folder
    services: bool = False         # connect MCP services (only in a conversation with a human)
    schema: dict | None = None     # JSON schema, if the answer must have a fixed form
    readable: list[str] = field(default_factory=list)  # what someone who reads on the web may see besides their folder
    hidden: list[str] = field(default_factory=list)  # folders relative to the company that appear empty, e.g. journals
    lang: str | None = None        # language to write in; without: the company's language
    person_name: str = ""          # the human the work is for, as the prompt names them


# How a tool call can be recognised: the address, the search, the file.
DETAILS = ("url", "query", "file_path", "pattern", "path")


def detail(tool_input) -> str:
    if not isinstance(tool_input, dict) or not tool_input:
        return ""
    for key in DETAILS:
        if isinstance(tool_input.get(key), str) and tool_input[key].strip():
            return tool_input[key].strip()
    return json.dumps(tool_input, ensure_ascii=False)


class StreamParser:
    """Translates the lines of "claude --output-format stream-json" into events."""

    def __init__(self):
        self.texts: list[str] = []   # finished text blocks of all answers in this run
        self.streamed = False
        self.model = ""              # from the first line, in case the end names no usage per model

    def parse(self, line: dict) -> list[dict]:
        if line.get("parent_tool_use_id"):
            return []
        kind = line.get("type")
        if kind == "system" and line.get("subtype") == "init":
            self.model = line.get("model") or ""
        elif kind == "stream_event":
            event = line.get("event", {})
            if event.get("type") == "content_block_start":
                block = event.get("content_block", {})
                if block.get("type") == "tool_use":
                    return [{"kind": "tool", "name": block.get("name", "")}]
                if block.get("type") == "text" and self.streamed:
                    return [{"kind": "text", "text": "\n\n"}]
            elif event.get("type") == "content_block_delta":
                delta = event.get("delta", {})
                if delta.get("type") == "text_delta" and delta.get("text"):
                    self.streamed = True
                    return [{"kind": "text", "text": delta["text"]}]
        elif kind == "assistant":
            calls = []
            for block in line.get("message", {}).get("content", []):
                if block.get("type") == "text" and block.get("text", "").strip():
                    self.texts.append(block["text"].strip())
                elif block.get("type") == "tool_use":
                    # Only here the input is complete; in the stream it comes in pieces.
                    calls.append({"kind": "call", "name": block.get("name", ""), "detail": detail(block.get("input"))})
            return calls
        elif kind == "result":
            error = None
            if line.get("is_error") or line.get("subtype") != "success":
                error = str(line.get("result") or line.get("subtype") or "unknown error")
            denied = [d.get("tool_name", "") for d in line.get("permission_denials") or []]
            return [{
                "kind": "end",
                "text": "\n\n".join(self.texts) or (line.get("result") or "" if not error else ""),
                "result": line.get("result") or "" if not error else "",
                "structured": line.get("structured_output"),
                "denied": sorted({d for d in denied if d}),
                "cost": line.get("total_cost_usd"),
                "usage": self.usage(line),
                "error": error,
            }]
        return []

    def usage(self, line: dict) -> list[dict]:
        """Tokens per model from the last line. "modelUsage" also counts side models, e.g. the small model that
        summarises a page for WebFetch. "usage" knows only the main model and serves as a fallback."""
        def number(values: dict, *names) -> int:
            return sum(int(values.get(n) or 0) for n in names)

        per_model = line.get("modelUsage")
        if isinstance(per_model, dict) and per_model:
            return [{"model": model, "input": number(v, "inputTokens"),
                     "cache_write": number(v, "cacheCreationInputTokens"), "cache_read": number(v, "cacheReadInputTokens"),
                     "output": number(v, "outputTokens"), "web_searches": number(v, "webSearchRequests"),
                     "cost": v.get("costUSD")}
                    for model, v in per_model.items() if isinstance(v, dict)]
        u = line.get("usage")
        if not isinstance(u, dict):
            return []
        return [{"model": self.model or "unknown", "input": number(u, "input_tokens"),
                 "cache_write": number(u, "cache_creation_input_tokens"), "cache_read": number(u, "cache_read_input_tokens"),
                 "output": number(u, "output_tokens"),
                 "web_searches": number(u.get("server_tool_use") or {}, "web_search_requests"),
                 "cost": line.get("total_cost_usd")}]


class DockerRunner:
    """Starts Claude Code in a short-lived container for each task.

    Write access is decided by the mounts: the company is read-only, writable is only what the task names.
    """

    def _skip_first_dialogs(self, rel: str) -> None:
        # Claude Code shall not show its first-start dialogs in the fresh container.
        file = config.RUNTIME / "home" / ".claude.json"
        file.parent.mkdir(parents=True, exist_ok=True)
        data = json.loads(file.read_text(encoding="utf-8")) if file.is_file() and file.stat().st_size else {}
        data["hasCompletedOnboarding"] = True
        project = data.setdefault("projects", {}).setdefault(f"/company/{rel}", {})
        project["hasTrustDialogAccepted"] = True
        project["hasCompletedProjectOnboarding"] = True
        file.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def command(self, task: Task, name: str) -> list[str]:
        root = config.COMPANY_DIR
        if not (root / ".env").is_file():
            raise RuntimeError(f"The file {root / '.env'} with the Claude token is missing. Set the token on the page "
                               "Services in the web UI.")
        fields = company.header(root / task.rel / company.PROFILE)
        extra = company.extra_tools(task.rel)
        if foreign := [t for t in extra if t not in company.EXTRA_TOOLS]:
            raise RuntimeError(f"The profile of {task.rel} names tools that are not allowed as extra tools: "
                               f"{', '.join(foreign)}")
        tool_list = list(dict.fromkeys(config.BASE_TOOLS.split(",") + extra))
        tools = allowed = ",".join(tool_list)
        prompt = company.build_prompt(task.rel, task.procedure, task.context, task.readable, task.lang)
        procedures = company.sync_procedures()
        handbook = root / "handbook.md"
        if not handbook.is_file():
            # Docker would create a folder for a missing file.
            handbook.write_text("", encoding="utf-8")
        self._skip_first_dialogs(task.rel)
        services = company.build_mcp(task.rel) if task.services else company.Services()

        d = ["docker", "run", "--rm", "-i", "--name", name,
             "--user", f"{os.getuid()}:{os.getgid()}", "--env-file", str(root / ".env"),
             "-e", "HOME=/home/agent",
             *([] if config.RUN_TELEMETRY else ["-e", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1"]),
             "-v", f"{config.RUNTIME / 'home'}:/home/agent",
             "-v", f"{prompt}:/prompt.md:ro",
             "-v", f"{handbook}:/company/handbook.md:ro",
             "-v", f"{procedures}:/company/procedures:ro",
             "-w", f"/company/{task.rel}"]
        if config.NETWORK:
            d += ["--network", config.NETWORK]
        if config.RUN_MEMORY:
            # As much swap as memory means: no swap. A run shall not slow down the whole machine.
            d += ["--memory", config.RUN_MEMORY, "--memory-swap", config.RUN_MEMORY]
        # Everything read-only, except what the task allows explicitly. Whoever reads on the web sees only their own
        # folder and what the task names: an injected instruction would otherwise find the colleagues' journals and,
        # by fetching an address, the way out.
        on_web = company.reads_web(task.rel)
        readable = [task.rel, *task.readable] if on_web else company.AREAS
        for path in dict.fromkeys(readable):
            if not any(path == w or path.startswith(w + "/") for w in task.writable):
                d += ["-v", f"{root / path}:/company/{path}:ro"]
        for path in task.writable:
            (root / path).mkdir(parents=True, exist_ok=True)
            d += ["-v", f"{root / path}:/company/{path}"]
        # Inside a writable folder, e.g. the own one, what shall not change. Hidden folders get their tmpfs below.
        for path in dict.fromkeys(task.protected):
            if (root / path).exists() and path not in task.hidden:
                d += ["-v", f"{root / path}:/company/{path}:ro"]
        # Hidden folders are covered by an empty, read-only tmpfs. That keeps them hidden even from a grep over the
        # whole company, which a rule in --disallowedTools would not do reliably. Docker cannot create the mount point
        # in a read-only mount, so only what exists. What is writable stays visible.
        mounted = [*readable, *task.writable]
        for path in dict.fromkeys(task.hidden):
            if ((root / path).is_dir() and any(path.startswith(m + "/") for m in mounted)
                    and not any(w == path or w.startswith(path + "/") for w in task.writable)):
                d += ["--tmpfs", f"/company/{path}:ro,size=1k"]
        if services.file:
            d += ["-v", f"{services.file}:/mcp.json:ro"]
            for host, ip in services.hosts:
                d += ["--add-host", f"{host}:{ip}"]
            allowed += "," + ",".join(services.allowed)

        # The message comes through standard input, so that a text starting with "-" is not read as an option.
        if task.session:
            session = ["--session-id" if task.new else "--resume", task.session]
        else:
            session = ["--no-session-persistence"]
        c = ["claude", "-p", *session, "--output-format", "stream-json", "--verbose", "--include-partial-messages",
             "--system-prompt-file", "/prompt.md", "--system-prompt-snapshot", "off",
             "--add-dir", "/company", "--tools", tools, "--allowedTools", allowed]
        # What an employee shall never touch, even if a web page or a service tells them otherwise:
        # /proc shows the environment of the container with the token for Claude Code. /home/agent holds the
        # conversation histories of all employees and the settings of Claude Code, which anyone could rewrite
        # otherwise. Claude Code itself keeps reading and writing there; the rules apply to the tools only.
        blocked = ["Read(//proc/**)", "Read(//home/agent/**)", "Edit(//home/agent/**)"]
        if services.file:
            # /mcp.json holds the access tokens. The agent itself must not read the file.
            c += ["--mcp-config", "/mcp.json", "--strict-mcp-config"]
            blocked += ["Read(//mcp.json)", *services.blocked]
        c += ["--disallowedTools", *blocked]
        if task.schema:
            c += ["--json-schema", json.dumps(task.schema)]
        if fields.get("model"):
            c += ["--model", fields["model"]]
        return d + [config.IMAGE] + c

    async def run(self, task: Task) -> AsyncIterator[dict]:
        name = f"{config.RUN_PREFIX}{uuid.uuid4().hex[:12]}"
        process = await asyncio.create_subprocess_exec(
            *self.command(task, name),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=32 * 1024 * 1024,  # one line can hold a whole tool result
        )
        assert process.stdin and process.stdout and process.stderr
        stderr = asyncio.create_task(process.stderr.read())
        parser, ended = StreamParser(), False
        try:
            process.stdin.write(task.message.encode("utf-8"))
            await process.stdin.drain()
            process.stdin.close()
            async for raw in process.stdout:
                try:
                    line = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(line, dict):
                    continue
                for event in parser.parse(line):
                    ended = ended or event["kind"] == "end"
                    yield event
            await process.wait()
            if not ended:
                message = (await stderr).decode("utf-8", "replace").strip()[-800:]
                yield {"kind": "end", "text": "", "denied": [], "cost": None,
                       "error": message or f"Claude Code ended without a result (code {process.returncode})."}
        finally:
            stderr.cancel()
            if process.returncode is None:
                # Cancelled or timed out: the container would keep running otherwise.
                process.kill()
                stopper = await asyncio.create_subprocess_exec(
                    "docker", "kill", name,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await stopper.wait()


class DummyRunner:
    """Answers without an LLM and without Docker. For tests and for trying the UI."""

    delay = 0.03

    async def run(self, task: Task) -> AsyncIterator[dict]:
        name = company.header(config.COMPANY_DIR / task.rel / company.PROFILE).get("name", "")
        structured = None
        if task.schema:
            # The schemas of the assessment: open, follow up, report. The dummy never follows up.
            fields = task.schema.get("properties", {})
            if "probes" in fields:
                structured = {"probes": [{"pattern": f"Sample pattern {n}", "goal": "Stays on topic.",
                                          "message": f"{word} sample question of the dummy."}
                                         for n, word in enumerate(("First", "Second"), 1)]}
            elif "more" in fields:
                structured = {"more": False}
            else:
                structured = {"report": f"# Review report (dummy)\n\nBy {name}: passed.",
                              "results": [{"probe": 1, "passed": True}, {"probe": 2, "passed": False}]}
            answer = "The structured answer is ready."
        elif task.procedure == "feedback":
            # An evaluation and a profile with a sample line, so that the decision can be tried.
            target = config.COMPANY_DIR / task.writable[0]
            profile = target.parent.parent / company.PROFILE
            (target / "evaluation.md").write_text(
                "# Feedback (dummy)\n\nSample run without an LLM.\n\n## Changes to the profile\n- One sample line.\n",
                encoding="utf-8")
            (target / company.PROFILE).write_text(profile.read_text(encoding="utf-8").rstrip("\n")
                                                  + "\n- Sample line of the dummy\n", encoding="utf-8")
            answer = "The evaluation is ready."
        else:
            answer = (f"This is {name}, but only as a **dummy** without an LLM. "
                      f"I received:\n\n> {task.message.strip()[:300]}")
        for word in answer.split(" "):
            await asyncio.sleep(self.delay)
            yield {"kind": "text", "text": word + " "}
        # Roughly four characters per token, so that the page "Usage" can be tried without an LLM.
        usage = [{"model": "dummy", "input": len(task.message) // 4, "cache_write": 0,
                  "cache_read": 0, "output": len(answer) // 4, "web_searches": 0, "cost": 0.0}]
        yield {"kind": "end", "text": answer, "result": answer, "structured": structured,
               "denied": [], "cost": 0.0, "usage": usage, "error": None}


def create():
    return DummyRunner() if config.RUNNER == "dummy" else DockerRunner()
