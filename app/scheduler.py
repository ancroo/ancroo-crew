"""Scheduler: recurring tasks for employees and the daily backup.

A schedule is a message that goes to an employee on certain weekdays at a time, on behalf of a human. It lands in the
conversation like any other; the answer is in the chat and later in the transcript. A human can also send it at once
("run now") or pause it.
"""
import asyncio
import re
import time
from datetime import datetime

from . import backup, company, config, db, i18n
from .conversations import Conversations
from .errors import Rejected, Unknown

TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class Scheduler:
    def __init__(self, conversations: Conversations):
        self.conversations = conversations
        self.last_backup = ""   # day of the last daily backup

    # ------------------------------------------------------------ Schedules

    def schedules(self, employee: str, lang: str | None = None) -> list[dict]:
        with db.connect() as con:
            rows = con.execute("SELECT * FROM schedules WHERE employee = ? ORDER BY time, id", (employee,)).fetchall()
        return [dict(r) | {"days_text": self.days_text(r["days"], lang)} for r in rows]

    @staticmethod
    def days_text(days: str, lang: str | None = None) -> str:
        numbers = [int(d) for d in days.split(",")]
        if numbers == list(range(7)):
            return i18n.t(lang, "days.daily")
        if numbers == list(range(5)):
            return i18n.t(lang, "days.weekdays")
        names = i18n.t(lang, "days.short").split(",")
        return ", ".join(names[n] for n in numbers)

    def add(self, employee: str, person: str, at: str, days: list[int], text: str) -> int:
        text = text.strip()
        if not company.find(employee).startswith("employees/"):
            raise Rejected("schedule_candidate", name=company.display_name(employee))
        if not TIME_PATTERN.match(at or ""):
            raise Rejected("time_form")
        days = sorted({d for d in days if isinstance(d, int) and 0 <= d <= 6})
        if not days:
            raise Rejected("no_weekday")
        if not text:
            raise Rejected("schedule_empty")
        now = datetime.now()
        # If the time has passed today, the schedule begins on the next matching day.
        last_run = now.date().isoformat() if at <= now.strftime("%H:%M") else None
        with db.connect() as con:
            return con.execute(
                "INSERT INTO schedules (employee, person, time, days, text, last_run, created) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (employee, person, at, ",".join(map(str, days)), text, last_run, time.time())).lastrowid

    def delete(self, sid: int) -> bool:
        with db.connect() as con:
            return con.execute("DELETE FROM schedules WHERE id = ?", (sid,)).rowcount > 0

    def pause(self, sid: int, paused: bool) -> bool:
        """A paused schedule stays on the card but does not come due."""
        with db.connect() as con:
            return con.execute("UPDATE schedules SET paused = ? WHERE id = ?", (int(paused), sid)).rowcount > 0

    def run_now(self, sid: int, person: str) -> dict:
        """Send the message of the schedule now, on behalf of whoever asks. The next due run stays as it was."""
        with db.connect() as con:
            s = con.execute("SELECT * FROM schedules WHERE id = ?", (sid,)).fetchone()
        if not s:
            raise Unknown("no_such_schedule")
        return self.conversations.send(s["employee"], person, s["text"], channel="schedule")

    # ------------------------------------------------------------ Operation

    def check(self, now: datetime | None = None) -> list[str]:
        """Send off what is due. Returns what was started."""
        now = now or datetime.now()
        today, clock = now.date().isoformat(), now.strftime("%H:%M")
        started = []
        with db.connect() as con:
            due = con.execute("SELECT * FROM schedules WHERE time <= ? AND (last_run IS NULL OR last_run < ?) "
                              "AND paused = 0", (clock, today)).fetchall()
        for s in due:
            if str(now.weekday()) not in s["days"].split(","):
                continue
            error = None
            try:
                self.conversations.send(s["employee"], s["person"], s["text"], channel="schedule")
                started.append(f"Schedule {s['id']} for {s['employee']}")
            except Rejected as reason:
                error = reason.text(company.person_language(s["person"]))
            except Unknown:
                error = i18n.t(company.person_language(s["person"]), "err.nobody_in_team", name=s["employee"])
            # Also after an error the day counts as done, otherwise the attempt would come back every few seconds.
            with db.connect() as con:
                con.execute("UPDATE schedules SET last_run = ?, error = ? WHERE id = ?", (today, error, s["id"]))
        return started

    def backup_due(self, now: datetime | None = None) -> bool:
        now = now or datetime.now()
        if not config.BACKUP_TIME or now.strftime("%H:%M") < config.BACKUP_TIME:
            return False
        today = now.date().isoformat()
        if self.last_backup == today or (backup.latest() or "")[:10] == today:
            self.last_backup = today
            return False
        return True

    async def watch(self) -> None:
        while True:
            await asyncio.sleep(config.SCHEDULER_TICK)
            try:
                self.check()
            except Exception:  # a broken schedule must not stop the scheduler
                pass
            try:
                if self.backup_due():
                    self.last_backup = datetime.now().date().isoformat()
                    await asyncio.to_thread(backup.make)
            except Exception:  # a failed backup is retried the next day; the service keeps running
                pass
