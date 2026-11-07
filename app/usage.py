"""What the runs use in tokens: book it and add it up for the page "Usage"."""
import time
import uuid
from datetime import date, datetime, timedelta

from . import db
from .runner import Task

# Order of the columns and of the colours in the chart.
KINDS = ("input", "cache_write", "cache_read", "output")
PURPOSES = ("conversation", "colleague", "assessment", "feedback")

# More columns do not fit side by side.
DAYS_IN_CHART = 60


def book(task: Task, end: dict, purpose: str, conversation: int | None = None, job: int | None = None) -> None:
    """Store the usage of a run. If the end names only costs, at least those count."""
    entries = end.get("usage") or []
    if not entries and end.get("cost"):
        entries = [{"model": "unknown", "cost": end["cost"]}]
    if not entries:
        return
    run, now = uuid.uuid4().hex, time.time()
    with db.connect() as con:
        con.executemany(
            "INSERT INTO usage (run, time, employee, procedure, purpose, conversation_id, job_id, model, input, "
            "cache_write, cache_read, output, web_searches, cost_usd, failed) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(run, now, task.rel.split("/")[1], task.procedure, purpose, conversation, job, e["model"],
              e.get("input", 0), e.get("cache_write", 0), e.get("cache_read", 0), e.get("output", 0),
              e.get("web_searches", 0), e.get("cost"), 1 if end.get("error") else 0) for e in entries])


SUMS = ("COUNT(DISTINCT run) AS runs, " + ", ".join(f"SUM({k}) AS {k}" for k in KINDS)
        + ", SUM(web_searches) AS web_searches, SUM(cost_usd) AS cost")


def _rows(con, group: str, since: float) -> list[dict]:
    rows = [dict(r) for r in con.execute(
        f"SELECT {group}, {SUMS} FROM usage WHERE time >= ? GROUP BY {group}", (since,))]
    for r in rows:
        r["total"] = sum(r[k] or 0 for k in KINDS)
    return sorted(rows, key=lambda r: -r["total"])


def overview(days: int) -> dict:
    """Usage of the last "days" days, with 0 everything. Grouped by model, kind of prompt and employee."""
    since = datetime.combine(date.today() - timedelta(days=days - 1), datetime.min.time()).timestamp() if days else 0
    with db.connect() as con:
        total = dict(con.execute(f"SELECT {SUMS}, MIN(time) AS first FROM usage WHERE time >= ?", (since,)).fetchone())
        total["total"] = sum(total[k] or 0 for k in KINDS)
        total["failed"] = con.execute("SELECT COUNT(DISTINCT run) FROM usage WHERE time >= ? AND failed = 1",
                                      (since,)).fetchone()[0]
        per_day = {r["day"]: dict(r) for r in con.execute(
            "SELECT date(time, 'unixepoch', 'localtime') AS day, " + ", ".join(f"SUM({k}) AS {k}" for k in KINDS)
            + ", SUM(cost_usd) AS cost FROM usage WHERE time >= ? GROUP BY day", (since,))}
        groups = {"model": _rows(con, "model", since), "prompt": _rows(con, "procedure, purpose", since),
                  "employee": _rows(con, "employee", since)}

    # One column per day, also without a run, so that the gaps match the time.
    today = date.today()
    first = date.fromtimestamp(max(since, total["first"] or time.time()))
    first = max(first, today - timedelta(days=DAYS_IN_CHART - 1))
    history = []
    for i in range((today - first).days + 1):
        day = (first + timedelta(days=i)).isoformat()
        values = per_day.get(day, {})
        history.append({"day": day, "cost": values.get("cost") or 0, **{k: values.get(k) or 0 for k in KINDS}})
    for d in history:
        d["total"] = sum(d[k] for k in KINDS)
    return {"sums": total, "history": history, "peak": max((d["total"] for d in history), default=0), **groups}
