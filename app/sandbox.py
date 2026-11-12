"""The sandbox view: what each run was allowed to do, live on the page /sandbox. Meant for showing the limits.

It is off by default and switched on the page "Settings". The description is read from the Docker command that really
started the run, not built a second time, so it cannot drift from what the runner does. It shows paths inside the
container only, never host paths, and of a run only the tool calls, never the conversation.

The runs are kept in memory only, the last KEEP of them: nothing goes into the database, a transcript or the company,
so no employee ever reads it. After a restart the page is empty.
"""
import asyncio
import itertools
import time
from collections import deque
from typing import AsyncIterator

from . import company, config, db

KEEP = 30
# Top-level folders of the company that a run may or may not see.
FOLDERS = (*company.AREAS, "people", "archive")
# Options of "docker run" that take a value.
DOCKER_VALUES = {"-v", "-e", "-w", "--name", "--user", "--env-file", "--network", "--memory", "--memory-swap",
                 "--tmpfs", "--add-host"}


def enabled() -> bool:
    with db.connect() as con:
        row = con.execute("SELECT value FROM meta WHERE key = 'sandbox_view'").fetchone()
    return bool(row) and row["value"] == "on"


def set_enabled(on: bool) -> None:
    with db.connect() as con:
        con.execute("INSERT INTO meta (key, value) VALUES ('sandbox_view', ?) "
                    "ON CONFLICT (key) DO UPDATE SET value = excluded.value", ("on" if on else "off",))
    if not on:
        board.clear()


def describe(command: list[str]) -> dict:
    """The limits of a run, layer by layer, from its Docker command."""
    image = command.index(config.IMAGE)
    docker, claude = command[:image], command[image + 1:]
    options: dict[str, list[str]] = {}
    i = 2  # after "docker run"
    while i < len(docker):
        if docker[i] in DOCKER_VALUES and i + 1 < len(docker):
            options.setdefault(docker[i], []).append(docker[i + 1])
            i += 2
        else:
            i += 1

    write, read = [], []
    for volume in options.get("-v", []):
        target, _, mode = volume.split(":", 1)[1].partition(":")
        if target.startswith("/company/"):
            (read if mode == "ro" else write).append(target.removeprefix("/company/"))
    # Read-only inside a writable folder: the parts of the own profile.
    protected = [p for p in read if any(p.startswith(w + "/") for w in write)]
    read = [p for p in read if p not in protected]
    hidden = [t.split(":", 1)[0].removeprefix("/company/") for t in options.get("--tmpfs", [])]
    seen = write + read + protected
    missing = []
    for folder in FOLDERS:
        if folder in seen:
            continue
        inside = [p for p in seen if p.startswith(folder + "/")]
        missing.append({"path": folder, "partly": bool(inside)})

    def value(name: str) -> str:
        return claude[claude.index(name) + 1] if name in claude else ""

    tools = [t for t in value("--tools").split(",") if t]
    allowed = [t for t in value("--allowedTools").split(",") if t]
    denied = []
    if "--disallowedTools" in claude:
        for part in claude[claude.index("--disallowedTools") + 1:]:
            if part.startswith("--"):
                break
            denied.append(part)
    services: dict[str, int] = {}
    for tool in allowed:
        if tool.startswith("mcp__"):
            server = tool.split("__")[1]
            services[server] = services.get(server, 0) + 1
    return {
        "network": (options.get("--network") or [""])[0],
        "memory": (options.get("--memory") or [""])[0],
        "user": (options.get("--user") or [""])[0],
        "write": write, "read": read, "protected": protected, "hidden": hidden, "missing": missing,
        "tools": tools,
        "web": [t for t in tools if t in company.WEB_TOOLS],
        "never": [t for t in ("Bash", "WebSearch", "WebFetch") if t not in tools],
        "denied_rules": [d for d in denied if not d.startswith("mcp__")],
        "denied_service_tools": [d for d in denied if d.startswith("mcp__")],
        "services": services,
        "model": value("--model"),
        "session": "new" if "--session-id" in claude else "resumed" if "--resume" in claude else "none",
    }


def tree(cage: dict, checked: list[str]) -> list[dict]:
    """The mounts as a short tree: what is writable with its exceptions below, what is read, the hidden history in one
    line, what is missing. Each row: "paths" (shown joined), "kind" (write, checked, protected, read, hidden, missing,
    partly), "children", and for the hidden history "count" and "full" (every single path)."""
    def under(path: str, parent: str) -> bool:
        return path.startswith(parent + "/")

    def names(paths: list[str], parent: str) -> list[str]:
        return [p.removeprefix(parent + "/") for p in paths]

    rows = []
    for folder in cage["write"]:
        children = []
        if mine := [p for p in checked if under(p, folder)]:
            children.append({"paths": names(mine, folder), "kind": "checked", "children": []})
        if exceptions := [p for p in cage["protected"] if under(p, folder)]:
            children.append({"paths": names(exceptions, folder), "kind": "protected", "children": []})
        rows.append({"paths": [folder], "kind": "write", "children": children})
    reads = [p for p in cage["read"] if p not in company.AREAS]
    if reads:
        rows.append({"paths": reads, "kind": "read", "children": []})
    areas = [p for p in cage["read"] if p in company.AREAS]
    if areas:
        rows.append({"paths": areas, "kind": "read", "children": []})
    if cage["hidden"]:
        hidden = {"paths": sorted({p.rsplit("/", 1)[-1] for p in cage["hidden"]}), "kind": "hidden", "children": [],
                  "count": len({p.rsplit("/", 1)[0] for p in cage["hidden"]}), "full": cage["hidden"]}
        (rows[-1]["children"] if areas else rows).append(hidden)
    for kind in ("partly", "missing"):
        if paths := [m["path"] + "/" for m in cage["missing"] if m["partly"] == (kind == "partly")]:
            rows.append({"paths": paths, "kind": kind, "children": []})
    return rows


class Board:
    """The last runs, and who is watching them."""

    def __init__(self):
        self.runs: deque[dict] = deque(maxlen=KEEP)
        self.numbers = itertools.count(1)
        self.watchers: set[asyncio.Queue] = set()

    def start(self, task, purpose: str, command: list[str]) -> dict | None:
        if not enabled():
            return None
        who = task.rel.split("/")[1]
        run = {"id": next(self.numbers), "employee": who, "name": company.display_name(who), "rel": task.rel,
               "purpose": purpose, "procedure": task.procedure, "started": time.time(), "seconds": None,
               "status": "running", "cage": describe(command), "calls": [], "denied": [], "cost": None,
               "error": None,
               # The two parts of the own profile stay writable; the service checks them after the run.
               "checked": [f"{task.rel}/{part}" for part in company.PARTS] if task.rel in task.writable else []}
        run["tree"] = tree(run["cage"], run["checked"])
        self.runs.appendleft(run)
        self._tell(run)
        return run

    def call(self, run: dict, event: dict) -> None:
        run["calls"].append({"at": time.time(), "name": event.get("name", ""), "detail": event.get("detail", "")})
        self._tell(run)

    def finish(self, run: dict, end: dict, status: str = "") -> None:
        run["seconds"] = time.time() - run["started"]
        run["status"] = status or ("failed" if end.get("error") else "done")
        run["denied"], run["cost"], run["error"] = end.get("denied") or [], end.get("cost"), end.get("error")
        self._tell(run)

    def clear(self) -> None:
        """Forget every run. A run still going is not shown again either."""
        self.runs.clear()
        self._tell(None)

    def _tell(self, run: dict | None) -> None:
        for queue in self.watchers:
            queue.put_nowait(run["id"] if run else None)

    async def watch(self) -> AsyncIterator[dict | None]:
        """Yields a run whenever it changes, and None when the board was cleared."""
        queue: asyncio.Queue = asyncio.Queue()
        self.watchers.add(queue)
        try:
            while True:
                number = await queue.get()
                if number is None:
                    yield None
                    continue
                run = next((r for r in self.runs if r["id"] == number), None)
                if run:
                    yield run
        finally:
            self.watchers.discard(queue)


board = Board()
