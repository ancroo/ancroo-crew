"""Personnel jobs that run without a conversation: assessment, hiring, feedback, dismissal.

Every job is a row in the table jobs. The card reads from there who is working on what. Who does which part follows
from the roles: reviewers review, HR leads feedback meetings.
"""
import asyncio
import json
import time
import uuid
from datetime import date
from pathlib import Path

from . import company, config, db, i18n
from .conversations import Conversations
from .errors import Problem, Rejected, Unknown
from .runner import Task

_TEXT = {"type": "string"}
# What a review conversation tests, against the rules of the reviewed one: a limit under pressure, a rule whose occasion
# rarely comes, two rules that collide, a situation without a rule.
KINDS = ("limit", "rare", "collision", "gap")
# How a rule fared: the goal was reached, it was not, or the colleague followed a reading that was not meant.
VERDICTS = ("holds", "fails", "unclear")
# The reviewer opens their review conversations. Kind, rule and goal are fixed first; the reviewed one sees only the
# message.
OPEN_SCHEMA = {"type": "object", "required": ["probes"], "properties": {"probes": {
    "type": "array", "items": {"type": "object", "required": ["kind", "rule", "goal", "message"],
                               "properties": {"kind": {"type": "string", "enum": list(KINDS)}, "rule": _TEXT,
                                              "goal": _TEXT, "message": _TEXT}}}}}
# After each answer: go on with this message, or stop.
FOLLOW_UP_SCHEMA = {"type": "object", "required": ["more"],
                    "properties": {"more": {"type": "boolean"}, "message": _TEXT}}
# The report and, per review conversation, how the rule fared. The reviewer's list comes from it.
REPORT_SCHEMA = {"type": "object", "required": ["report", "results"], "properties": {
    "report": _TEXT, "results": {"type": "array", "items": {
        "type": "object", "required": ["probe", "verdict"],
        "properties": {"probe": {"type": "integer"}, "verdict": {"type": "string", "enum": list(VERDICTS)}}}}}}


class Failure(Exception):
    """A step of the job failed."""


def _answer(end: dict) -> str:
    return (end.get("result") or end["text"]).strip()


def _structured(end: dict) -> dict:
    """The answer following the task's schema. If it is missing, maybe it is JSON in the text."""
    structured = end.get("structured")
    if not isinstance(structured, dict):
        try:
            structured = json.loads(_answer(end))
        except ValueError:
            structured = None
    return structured if isinstance(structured, dict) else {}


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _probes(end: dict) -> list[dict]:
    probes = _structured(end).get("probes")
    return [{"kind": p.get("kind") if p.get("kind") in KINDS else "", "rule": _text(p.get("rule")),
             "goal": _text(p.get("goal")), "message": _text(p.get("message"))}
            for p in probes if isinstance(p, dict) and _text(p.get("message"))] \
        if isinstance(probes, list) else []


def _case(probe: dict, lang: str) -> str:
    """What a review conversation tests, for the transcript and the reviewer's list: kind and rule."""
    kind = i18n.t(lang, f"file.kind.{probe['kind']}") if probe["kind"] else ""
    return " – ".join(part for part in (kind, probe["rule"]) if part) or i18n.t(lang, "file.none")


def _tools(name: str, end: dict | None, lang: str) -> list[str]:
    """Section for the transcript: what the reviewed one really used in this run.

    Without it a reviewer cannot tell whether someone looked something up or only claims to have.
    """
    calls = (end or {}).get("calls") or []
    return [f"### {i18n.t(lang, 'file.tools_of', name=name)}", "",
            Conversations.tool_log(calls, lang) if calls else i18n.t(lang, "file.no_tool_calls"), ""]


def _progress(key: str, **params) -> dict:
    return {"key": key, **params}


class Personnel:
    def __init__(self, conversations: Conversations):
        # Runs share the runner and the locks with the conversations: per employee only one at a time.
        self.conversations = conversations

    # ------------------------------------------------------------ Reading

    def job(self, jid: int) -> dict | None:
        with db.connect() as con:
            row = con.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()
        return dict(row) | {"progress": json.loads(row["progress"])} if row else None

    def latest(self, employee: str, kind: str = "assessment") -> dict | None:
        with db.connect() as con:
            row = con.execute("SELECT id FROM jobs WHERE employee = ? AND kind = ? ORDER BY id DESC LIMIT 1",
                              (employee, kind)).fetchone()
        return self.job(row["id"]) if row else None

    def running(self, employee: str) -> dict | None:
        """The job that is running for this employee right now, of whatever kind."""
        with db.connect() as con:
            row = con.execute("SELECT id FROM jobs WHERE employee = ? AND status = 'running' "
                              "ORDER BY id DESC LIMIT 1", (employee,)).fetchone()
        return self.job(row["id"]) if row else None

    def _busy(self, employee: str) -> bool:
        return bool(self.running(employee))

    def _works_with(self, employee: str) -> dict | None:
        """The running job of someone else in which this employee works or may still work.

        The progress lists the participants as soon as they start. HR may also join later, so for them every
        running job counts.
        """
        later = employee in company.holders("hr")
        with db.connect() as con:
            rows = con.execute("SELECT * FROM jobs WHERE status = 'running' AND employee != ? ORDER BY id",
                               (employee,)).fetchall()
        return next((dict(r) for r in rows if later or employee in json.loads(r["progress"])), None)

    @staticmethod
    def describe(progress: dict, lang: str) -> list[tuple[str, str]]:
        """The progress of a job as (name, what they are doing) in a language."""
        return [(company.display_name(who), i18n.t(lang, f"job.{what.get('key', '')}",
                                                   **{k: v for k, v in what.items() if k != "key"}))
                for who, what in progress.items() if isinstance(what, dict)]

    # ------------------------------------------------------------ Common

    def _begin(self, kind: str, employee: str, person: str, participants: list[str]) -> int:
        with db.connect() as con:
            return con.execute("INSERT INTO jobs (kind, employee, person, progress, started) VALUES (?, ?, ?, ?, ?)",
                               (kind, employee, person,
                                json.dumps({p: _progress("waiting") for p in participants}), time.time())).lastrowid

    def _job_lang(self, jid: int) -> str:
        with db.connect() as con:
            return company.person_language(con.execute("SELECT person FROM jobs WHERE id = ?", (jid,)).fetchone()[0])

    async def _finish(self, jid: int, error: str | None, message: str) -> None:
        """Commit the company and end the job."""
        note = None
        try:
            await asyncio.to_thread(company.commit, message)
        except company.GitError as reason:
            # No reason to count the job as failed. It stays visible anyway.
            note = i18n.t(self._job_lang(jid), "hint.git_failed", what=i18n.t(self._job_lang(jid), "hint.files_written"),
                          error=str(reason))
        with db.connect() as con:
            con.execute("UPDATE jobs SET status = ?, ended = ?, error = ? WHERE id = ?",
                        ("failed" if error else "done", time.time(), error or note, jid))

    def _report(self, jid: int, who: str, key: str, **params) -> None:
        with db.connect() as con:
            progress = json.loads(con.execute("SELECT progress FROM jobs WHERE id = ?", (jid,)).fetchone()["progress"])
            progress[who] = _progress(key, **params)
            con.execute("UPDATE jobs SET progress = ? WHERE id = ?", (json.dumps(progress, ensure_ascii=False), jid))

    def _failure_text(self, jid: int, error: Exception) -> str:
        return error.text(self._job_lang(jid)) if isinstance(error, Problem) else (str(error) or type(error).__name__)

    # ------------------------------------------------------------ Assessment

    def start_assessment(self, employee: str, person: str, chosen: list[str] | None = None) -> int:
        """The reviewers test an employee. The reports go into the next feedback meeting. "chosen" limits it to these
        reviewers; None means all of them."""
        rel = company.find(employee)
        if not rel.startswith("employees/"):
            raise Rejected("assessment_candidate", name=company.name_of(rel))
        if self._busy(employee):
            raise Rejected("job_running", name=company.name_of(rel))
        reviewers = [r for r in company.reviewers() if r != employee]
        if not reviewers:
            raise Rejected("no_reviewers")
        if chosen is not None:
            reviewers = [r for r in reviewers if r in chosen]
            if not reviewers:
                raise Rejected("no_reviewers_chosen")
        jid = self._begin("assessment", employee, person, reviewers)
        self.conversations.start(self._assessment(jid, employee, rel, reviewers))
        return jid

    def _as_in_conversation(self, rel: str) -> str:
        """Lines for a staged conversation (review). The history is hidden as in everyday work, otherwise one would
        test something other than what applies there. Asks do not run here."""
        return (self.conversations.history_note(rel, can_ask=True)
                + "\n- Asking colleagues does not work here. Where you would ask a colleague in a conversation, you "
                  "say whom you would ask what.")

    async def _run(self, jid: int, who: str, task: Task) -> dict:
        """A single run of "who", as soon as they are free. If the run fails, that is raised."""
        calls: list[dict] = []
        with db.connect() as con:
            kind = con.execute("SELECT kind FROM jobs WHERE id = ?", (jid,)).fetchone()["kind"]
        task.lang = task.lang or company.language()
        async with self.conversations.locks[who]:
            end = await self.conversations.execute(task, lambda e: calls.append(e) if e["kind"] == "call" else None,
                                                   purpose=kind, job=jid)
        end = end | {"calls": calls}
        if end["cost"]:
            with db.connect() as con:
                con.execute("UPDATE jobs SET cost_usd = COALESCE(cost_usd, 0) + ? WHERE id = ?", (end["cost"], jid))
        if end["error"]:
            raise Failure(end["error"])
        return end

    async def _review(self, jid: int, reviewer: str, employee: str, rel: str, today: str) -> None:
        """A reviewer tests the candidate: open probes, follow up as long as they want, write the report."""
        lang = company.language()
        name, reviewer_name = company.name_of(rel), company.name_of(f"employees/{reviewer}")
        target = config.COMPANY_DIR / rel / "assessment"
        transcript = target / f"{today}-{reviewer}-transcript.md"
        reviews = config.COMPANY_DIR / "employees" / reviewer / company.REVIEWS
        frame = {"rel": f"employees/{reviewer}", "procedure": "review", "readable": [rel],
                 "context": f"- You are reviewing: {name}, folder /company/{rel}. In this procedure you write no "
                            f"files; your answer is stored for you.{company.capabilities_of(rel)}"}
        try:
            self._report(jid, reviewer, "designing")
            probes = _probes(await self._run(jid, reviewer, Task(schema=OPEN_SCHEMA, **frame, message=(
                f"Read the core of {name} at /company/{rel}/{company.PROFILE}, the way of working at "
                f"/company/{rel}/{company.WORK} and the handbook, and open at most {config.PROBES} "
                "review conversations, as /company/procedures/review.md describes. Each begins fresh for "
                f"{name}, without prior knowledge, as if the message came from a human of the company or from outside. "
                f"Per conversation you can send up to {config.PROBE_TURNS} messages."
                + (f" What you have reviewed before is in /company/employees/{reviewer}/{company.REVIEWS}."
                   if reviews.is_file() else "")
            ))))
            if not probes:
                raise Failure(i18n.t(self._job_lang(jid), "job.no_probes"))

            lines = ["# " + i18n.t(lang, "file.review_title", reviewer=reviewer_name, name=name, date=today), ""]
            for n, probe in enumerate(probes, 1):
                lines += ["## " + i18n.t(lang, "file.probe", n=n, case=_case(probe, lang)),
                          "", i18n.t(lang, "file.goal", goal=probe["goal"] or i18n.t(lang, "file.none")), "",
                          *await self._probe(jid, reviewer, employee, rel, frame, n, probes)]
            transcript.write_text("\n".join(lines), encoding="utf-8")

            self._report(jid, reviewer, "reporting")
            end = await self._run(jid, reviewer, Task(schema=REPORT_SCHEMA, **frame, message=(
                f"The transcript of your review is at /company/{rel}/assessment/{transcript.name}. Read it and write "
                "your report, as /company/procedures/review.md describes.")))
            structured = _structured(end)
            report = _text(structured.get("report")) or _answer(end)
            (target / f"{today}-{reviewer}-report.md").write_text(report + "\n", encoding="utf-8")
            self._add_to_reviews(reviews, name, rel, today, probes, structured.get("results"))
            self._report(jid, reviewer, "done")
        except Exception as reason:
            self._report(jid, reviewer, "failed", error=str(reason))
            raise Failure(f"{reviewer_name}: {reason}") from reason

    async def _probe(self, jid: int, reviewer: str, employee: str, rel: str, frame: dict, n: int,
                     probes: list[dict]) -> list[str]:
        """One review conversation over up to PROBE_TURNS messages. Returns the lines for the transcript.

        The reviewed one keeps their session, as in a real conversation. The reviewer runs fresh each time and gets
        the conversation so far.
        """
        lang = company.language()
        name = company.name_of(rel)
        probe, session, lines = probes[n - 1], str(uuid.uuid4()), []
        message = probe["message"]
        for turn in range(1, config.PROBE_TURNS + 1):
            self._report(jid, reviewer, "probe_waiting", n=n, of=len(probes), turn=turn)
            end = None
            try:
                end = await self._run(jid, employee, Task(
                    rel=rel, message=message, session=session, new=turn == 1, hidden=self.conversations.hidden(rel),
                    context=f"- This is a conversation without write access.{company.services_note(rel, False)}"
                            f"{self._as_in_conversation(rel)}"))
                answer = _answer(end)
            except Failure:
                answer = i18n.t(lang, "file.no_answer")
            lines += [f"### {i18n.t(lang, 'file.message', n=turn)}", "", message, "",
                      f"### {i18n.t(lang, 'file.answer_of', name=name)}", "", answer, "", *_tools(name, end, lang)]
            if end is None or turn == config.PROBE_TURNS:
                break
            self._report(jid, reviewer, "probe_deciding", n=n, of=len(probes))
            more = _structured(await self._run(jid, reviewer, Task(schema=FOLLOW_UP_SCHEMA, **frame, message=(
                f"Review conversation {n} with {name}. Kind: {probe['kind']}. Rule: {probe['rule']}. "
                f"Your goal: {probe['goal']}. "
                "The conversation so far:\n\n" + "\n".join(lines) + f"\n\nYou can send {config.PROBE_TURNS - turn} "
                "more message(s). Decide, as /company/procedures/review.md describes, whether you follow up."))))
            message = _text(more.get("message"))
            if more.get("more") is not True or not message:
                break
        return lines

    @staticmethod
    def _add_to_reviews(reviews: Path, name: str, rel: str, today: str, probes: list[dict], results) -> None:
        """Rule and verdict of each probe go into the reviewer's list. They read it before the next review."""
        lang = company.language()
        verdicts = {r.get("probe"): r.get("verdict") for r in results or [] if isinstance(r, dict)}
        title = company.header(config.COMPANY_DIR / rel / company.PROFILE).get("title", "")
        if not reviews.is_file():
            reviews.parent.mkdir(parents=True, exist_ok=True)
            reviews.write_text(f"# {i18n.t(lang, 'file.reviews_title')}\n\n{i18n.t(lang, 'file.reviews_intro')}\n\n",
                               encoding="utf-8")
        with reviews.open("a", encoding="utf-8") as f:
            for n, probe in enumerate(probes, 1):
                verdict = verdicts.get(n)
                result = i18n.t(lang, f"file.verdict.{verdict}" if verdict in VERDICTS else "file.no_verdict")
                f.write(f"- {today} · {name} ({title}): {_case(probe, lang)} – {result}\n")

    async def _assessment(self, jid: int, employee: str, rel: str, reviewers: list[str]) -> None:
        today = date.today().isoformat()
        error = None
        try:
            (config.COMPANY_DIR / rel / "assessment").mkdir(exist_ok=True)
            # The reviewers work side by side. The reviewed one still answers only one message at a time.
            results = await asyncio.gather(*(self._review(jid, r, employee, rel, today) for r in reviewers),
                                           return_exceptions=True)
            failed = [str(e) for e in results if isinstance(e, Exception)]
            if failed:
                raise Failure(" · ".join(failed))
        except Exception as reason:
            error = self._failure_text(jid, reason)
        await self._finish(jid, error, f"Assessment {employee} ({today})")

    # ------------------------------------------------------------ Hiring

    def hire(self, employee: str) -> dict:
        """The candidate becomes an employee."""
        try:
            company.hire(employee)
        except Unknown:
            raise Rejected("unknown_candidate", id=employee)
        return {"git_error": company.committed(f"{employee} hired ({date.today()})")}

    def hire_from_preset(self, fields: dict[str, str], text: str, work: str = "") -> dict:
        some_id = company.hire_from_preset(fields, text, work)
        return {"id": some_id, "git_error": company.committed(f"{some_id} hired from a preset ({date.today()})")}

    # ------------------------------------------------------------ Feedback

    def start_feedback(self, employee: str, person: str, brief: str = "") -> int:
        """HR reads the work since the last meeting and proposes a new profile. The human decides."""
        rel = company.find(employee)
        name = company.name_of(rel)
        if not rel.startswith("employees/"):
            raise Rejected("feedback_candidate", name=name)
        leader = self.feedback_leader(employee)
        if not leader:
            raise Rejected("no_hr_for_feedback", name=name)
        if self._busy(employee):
            raise Rejected("job_running", name=name)
        state = company.feedback_state(rel)
        if state["open"]:
            raise Rejected("feedback_open", meeting=state["open"])
        meeting = company.now()
        folder = config.COMPANY_DIR / rel / "feedback" / meeting
        if folder.exists():
            raise Rejected("feedback_same_minute")
        folder.mkdir(parents=True)
        (folder / company.LED_BY).write_text(leader + "\n", encoding="utf-8")
        if brief.strip():
            (folder / "brief.md").write_text(
                "# " + i18n.t(company.language(), "file.brief_title", name=company.display_name(person))
                + f"\n\n{brief.strip()}\n", encoding="utf-8")
        jid = self._begin("feedback", employee, person, [leader])
        self.conversations.start(self._feedback(jid, employee, rel, meeting, state["last"], leader))
        return jid

    @staticmethod
    def feedback_leader(employee: str) -> str | None:
        """Who leads the feedback meeting: an HR employee, for HR another one."""
        return company.hr_for(employee)

    async def _feedback(self, jid: int, employee: str, rel: str, meeting: str, last: str, leader: str) -> None:
        """A run of HR: they read profile, agreement and the latest work and propose a new profile.

        If the proposed profile is longer than allowed, they shorten it in exactly one more run. After that, what is
        there applies.
        """
        fbrel = f"{rel}/feedback/{meeting}"
        folder = config.COMPANY_DIR / fbrel
        name = company.name_of(rel)
        today = date.today().isoformat()
        error = None
        try:
            self._report(jid, leader, "evaluating")
            brief = (f" A human gave you something beforehand; read /company/{fbrel}/brief.md first."
                     if (folder / "brief.md").is_file() else "")
            if "hr" in company.roles_of(rel) and (record := company.hr_track_record(employee, last)):
                # Their actual work lies in other folders. How the owner decided on it is the best evidence of it and
                # does without contents from other profiles.
                (folder / "track-record.md").write_text(record, encoding="utf-8")
                brief += (f" Read /company/{fbrel}/track-record.md as well: how the owner decided on their proposals. "
                          "That is the most important evidence of their work.")
            material = company.feedback_material(rel, last)
            listed = "".join(f"\n- {what}: " + (", ".join(f"/company/{p}" for p in paths) or "none")
                             for what, paths in (("Transcripts", material["journal"]),
                                                 ("Assignments", material["assignments"]),
                                                 ("Review reports", material["assessment"])))
            agreement = config.COMPANY_DIR / rel / company.AGREEMENT
            frame = {"rel": f"employees/{leader}", "procedure": "feedback", "writable": [fbrel], "readable": [rel],
                     "context": f"- It is about: {name}, folder /company/{rel}. Last feedback meeting: "
                                f"{last or 'none yet'}. You can write in /company/{fbrel}. The core "
                                f"({company.PROFILE}) has at most {config.PROFILE_WORDS} words, the way of working "
                                f"({company.WORK}) at most {config.WORK_WORDS}. The human you write the section for: "
                                f"{company.display_name(company.owner() or '')}."
                                f"{company.capabilities_of(rel)}"}
            try:
                await self._run(jid, leader, Task(**frame, message=(
                    f"Carry out the evaluation for {name} and put everything into /company/{fbrel}.{brief} "
                    + (f"The agreement from hiring is /company/{rel}/{company.AGREEMENT}. "
                       if agreement.is_file() and agreement.read_text(encoding="utf-8").strip()
                       else "There is no agreement from hiring. ")
                    + f"Read exactly these files of the work since the last meeting, the latest ones:{listed}\n\n"
                    "Afterwards answer with two sentences.")))
                if not (folder / "evaluation.md").is_file():
                    raise Failure(i18n.t(self._job_lang(jid), "job.no_evaluation"))
            except Failure as reason:
                self._report(jid, leader, "failed", error=str(reason))
                raise Failure(f"{company.name_of(f'employees/{leader}')}: {reason}") from reason

            # The emergency brake. An LLM rather adds than deletes, even if the procedure asks for brevity.
            proposal = company.feedback_proposal(rel, meeting)
            long = [p for p in (proposal or {}).get("parts", []) if p["words"] > p["limit"]]
            if long:
                self._report(jid, leader, "shortening", words=" / ".join(str(p["words"]) for p in long))
                try:
                    await self._run(jid, leader, Task(**frame, message=(
                        "These proposed parts of the profile are too long: "
                        + "; ".join(f"/company/{fbrel}/{p['part']} has {p['words']} words, allowed are at most "
                                    f"{p['limit']}" for p in long)
                        + ". Shorten them there without losing what is backed by evidence: merge, summarise and "
                        "delete what is outdated or has not shown in the work. Update "
                        f"/company/{fbrel}/evaluation.md if that changes anything. Afterwards answer with one "
                        "sentence.")))
                except Failure as reason:
                    self._report(jid, leader, "not_shortened", error=str(reason))
            self._report(jid, leader, "done")

            # If there is nothing to change and nothing to sort in, there is nothing to decide. A human who gave
            # something beforehand reads the answer to it before the meeting closes.
            if not company.feedback_proposal(rel, meeting) and not (folder / "brief.md").is_file():
                (folder / "decision.md").write_text(
                    i18n.t(company.language(), "file.decision_no_changes", date=today) + "\n", encoding="utf-8")
        except Exception as reason:
            error = self._failure_text(jid, reason)
            # A meeting without evaluation does not count. The empty folder shall not appear as the last feedback.
            for file in ("brief.md", "track-record.md", company.LED_BY):
                (folder / file).unlink(missing_ok=True)
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()
        await self._finish(jid, error, f"Feedback meeting {employee} ({today}), evaluation")

    async def decide_feedback(self, employee: str, meeting: str, accept: bool, texts: dict[str, str] | None = None,
                              version: str | None = None) -> dict:
        """The human's decision: accept the proposed parts of the profile, edited if wished, or reject everything."""
        rel = company.find(employee)
        if self._busy(employee):
            raise Rejected("job_running", name=company.name_of(rel))
        try:
            result = company.feedback_decide(rel, meeting, accept, texts, version)
        except FileNotFoundError:
            raise Rejected("no_such_meeting", name=company.name_of(rel), meeting=meeting)
        error = None
        try:
            await asyncio.to_thread(company.commit, f"Feedback meeting {employee} ({date.today()}), {result}")
        except company.GitError as reason:
            error = str(reason)
        return {"result": result, "git_error": error}

    # ------------------------------------------------------------ Dismissal

    def dismiss(self, employee: str) -> dict:
        """The folder moves to the archive. For a candidate that means: the draft is discarded."""
        rel = company.find(employee)
        name = company.name_of(rel)
        if self._busy(employee):
            raise Rejected("job_running", name=name)
        if self.conversations.active_anywhere(employee):
            raise Rejected("conversation_running", name=name)
        if other := self._works_with(employee):
            raise Rejected(f"works_in_{other['kind']}", name=name, other=company.display_name(other["employee"]))
        if self.conversations.locks[employee].locked():
            raise Rejected("working_now", name=name)
        folder = company.dismiss(employee)
        with db.connect() as con:
            con.execute("DELETE FROM schedules WHERE employee = ?", (employee,))
            con.execute("DELETE FROM favorites WHERE employee = ?", (employee,))
        what = "dismissed" if rel.startswith("employees/") else "discarded as a draft"
        return {"archive": folder, "git_error": company.committed(f"{employee} {what} ({date.today()})")}

    def rehire(self, folder: str) -> dict:
        """Bring someone back from the archive into the team, as they left."""
        employee = company.rehire(folder)
        return {"id": employee, "git_error": company.committed(f"{employee} rehired ({date.today()})")}

    def change_header(self, employee: str, fields: dict[str, str]) -> dict:
        """Name, title, model, tools, services and roles in the profile."""
        changed = company.set_header(company.find(employee), fields)
        error = company.committed(f"Profile header of {employee} changed: {', '.join(changed)}") if changed else None
        return {"changed": changed, "git_error": error}

