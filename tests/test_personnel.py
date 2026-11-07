import asyncio
from datetime import date, datetime, timedelta

import pytest

from app import company, config, db
from app.conversations import Conversations
from app.errors import Invalid, Problem, Rejected, Unknown
from app.personnel import Personnel
from app.runner import DummyRunner, Task
from app.scheduler import Scheduler
from conftest import CORA, IDA, MILA, NEW, OWNER, employee, quick, settle
from test_conversations import Office

M, N, E = f"employees/{MILA}", f"candidates/{NEW}", f"employees/{NEW}"


def test_assessment_and_hire(firm):
    company.set_field(firm / M / company.PROFILE, "roles", "reviewer")

    async def flow():
        runner = Office()
        personnel = Personnel(Conversations(runner))
        with pytest.raises(Rejected):
            personnel.start_assessment(NEW, OWNER)  # a draft: the assessment comes after hiring
        assert personnel.hire(NEW)["git_error"] is None
        with pytest.raises(Rejected):
            personnel.hire(NEW)
        with pytest.raises(Rejected):
            personnel.start_assessment(MILA, OWNER)  # nobody but herself reviews
        jid = personnel.start_assessment(NEW, OWNER)
        with pytest.raises(Rejected):
            personnel.start_assessment(NEW, OWNER)   # running already
        await settle(personnel.conversations)
        j = personnel.job(jid)
        assert j["status"] == "done" and j["progress"] == {MILA: {"key": "done"}}
        assert personnel.describe(j["progress"], "de") == [("Mila", "fertig")]
        return runner.tasks

    tasks = asyncio.run(flow())
    today = date.today().isoformat()
    fields = company.header(firm / E / company.PROFILE)
    assert fields["hired"] == today and "status" not in fields and "probation_until" not in fields
    folder = firm / E / "assessment"
    transcript = (folder / f"{today}-{MILA}-transcript.md").read_text(encoding="utf-8")
    assert "## Conversation 2: Sample pattern 2\n\nGoal: Stays on topic." in transcript
    assert "Second sample question" in transcript and "### Answer of Neu" in transcript
    assert transcript.count("### Tools of Neu\n\nNo tool calls.") == 2
    assert "What Neu can do technically" in tasks[0].context and tasks[0].readable == [E]
    assert "Sample pattern" not in tasks[1].message and "Review report (dummy)" in (
        folder / f"{today}-{MILA}-report.md").read_text(encoding="utf-8")
    # Open, per conversation one answer and the decision not to follow up, report. No revision.
    assert [(t.rel, t.procedure) for t in tasks] == [
        (M, "review"), (E, "conversation"), (M, "review"), (E, "conversation"), (M, "review"), (M, "review")]
    # Only the reviewed one gets a session, one of its own per review conversation.
    reviewed = [t for t in tasks if t.rel == E]
    assert all(t.new for t in reviewed) and len({t.session for t in reviewed}) == 2 and all(t.session for t in reviewed)
    assert all(t.session is None for t in tasks if t.rel != E)
    assert not any(t.services for t in tasks) and all(t.lang == "en" for t in tasks)
    assert [t.writable for t in tasks] == [[]] * 6
    # Pattern and result of every probe go into the reviewer's list.
    reviews = (firm / M / company.REVIEWS).read_text(encoding="utf-8")
    assert f"- {today} · Neu (Test): Sample pattern 1 – passed" in reviews
    assert f"- {today} · Neu (Test): Sample pattern 2 – not passed" in reviews


def test_assessment_with_chosen_reviewers(firm):
    company.set_field(firm / M / company.PROFILE, "roles", "hr,reviewer")
    employee(firm, CORA, "---\nname: Cora\ntitle: Test\nroles: reviewer\n---\n\nText of Cora.\n")

    company.hire(NEW)

    async def flow():
        runner = Office()
        personnel = Personnel(Conversations(runner))
        with pytest.raises(Rejected):
            personnel.start_assessment(NEW, OWNER, [])
        with pytest.raises(Rejected):
            personnel.start_assessment(NEW, OWNER, [NEW, "9999"])  # not a reviewer, nobody
        jid = personnel.start_assessment(NEW, OWNER, [CORA])
        await settle(personnel.conversations)
        return runner.tasks, personnel.job(jid)

    tasks, job = asyncio.run(flow())
    assert job["status"] == "done" and job["progress"] == {CORA: {"key": "done"}}
    assert {t.rel for t in tasks if t.procedure == "review"} == {f"employees/{CORA}"}


def test_candidates_never_bring_roles(firm):
    company.set_field(firm / N / company.PROFILE, "roles", "archivist,hr")
    assert company.prepare_candidates() == {NEW} and company.header(firm / N / company.PROFILE)["roles"] == ""
    company.set_field(firm / N / company.PROFILE, "roles", "archivist")  # e.g. HR changed the draft once more
    Personnel(Conversations(quick())).hire(NEW)
    assert company.roles_of(f"employees/{NEW}") == [] and company.archivist() is None


def test_hire_needs_a_unique_name(firm):
    company.set_field(firm / N / company.PROFILE, "name", "Mila")
    with pytest.raises(Invalid):
        Personnel(Conversations(quick())).hire(NEW)
    assert company.find(NEW) == N


def test_assessment_reports_failed_reviewer(firm):
    company.set_field(firm / M / company.PROFILE, "roles", "reviewer")

    class Silent:
        async def run(self, task):
            yield {"kind": "end", "text": "No interest.", "denied": [], "cost": None, "error": None}

    company.hire(NEW)

    async def flow():
        personnel = Personnel(Conversations(Silent()))
        jid = personnel.start_assessment(NEW, OWNER)
        await settle(personnel.conversations)
        return personnel.job(jid)

    j = asyncio.run(flow())
    assert j["status"] == "failed" and "Mila" in j["error"] and "review conversations" in j["error"]


def test_reviewer_follows_up(firm, monkeypatch):
    """In the first probe the reviewer follows up once. The reviewed one answers in the same session."""
    company.set_field(firm / M / company.PROFILE, "roles", "reviewer")
    company.hire(NEW)

    class Persistent(Office):
        followed_up = False

        async def run(self, task):
            if task.schema and "more" in task.schema["properties"] and not self.followed_up:
                self.tasks.append(task)
                self.followed_up = True
                yield {"kind": "end", "text": "", "structured": {"more": True, "message": "Really not?"},
                       "denied": [], "cost": None, "error": None}
                return
            async for e in super().run(task):
                yield e

    async def review(runner):
        personnel = Personnel(Conversations(runner))
        personnel.start_assessment(NEW, OWNER)
        await settle(personnel.conversations)

    runner = Persistent()
    asyncio.run(review(runner))
    reviewed = [t for t in runner.tasks if t.rel == E]
    assert [(t.message, t.new) for t in reviewed] == [
        ("First sample question of the dummy.", True), ("Really not?", False), ("Second sample question of the dummy.", True)]
    assert reviewed[0].session == reviewed[1].session != reviewed[2].session
    # Before following up the reviewer sees the conversation, their goal and how many messages are left.
    decision = runner.tasks[2]
    assert "Stays on topic." in decision.message and "First sample question" in decision.message
    assert "send 2 more message" in decision.message
    transcript = firm / E / "assessment" / f"{date.today().isoformat()}-{MILA}-transcript.md"
    probe_1 = transcript.read_text(encoding="utf-8").split("## Conversation 2")[0]
    assert "### Message 2\n\nReally not?" in probe_1

    # Next time she reads her list first. With one turn per probe nobody asks whether to follow up.
    monkeypatch.setattr(config, "PROBE_TURNS", 1)
    runner = Office()
    asyncio.run(review(runner))
    assert company.REVIEWS in runner.tasks[0].message
    assert [t.rel for t in runner.tasks].count(M) == 2  # open, report


# ---------------------------------------------------------------- Feedback

class HRDesk(DummyRunner):
    """Dummy that proposes a profile with a changed header in the feedback meeting."""
    delay = 0

    def __init__(self):
        self.tasks: list[Task] = []

    async def run(self, task):
        self.tasks.append(task)
        async for e in super().run(task):
            if e["kind"] == "end" and task.procedure == "feedback":
                target = config.COMPANY_DIR / task.writable[0]
                # Cora writes the profile with a header in which she grants herself a service.
                (target / "profile.md").write_text("---\nname: Mila\nservices: all\n---\n\n## Task\n\nYou are Mila, more precisely.\n",
                                                   encoding="utf-8")
            yield e


def test_feedback_state_and_material(office, monkeypatch):
    # Nothing becomes due by itself.
    assert company.feedback_state(M) == {"open": None, "last": ""}
    for name in ("2026-01-06-0900.md", "2026-01-07-1000.md", "2026-01-08-1100.md"):
        (office / "journal" / name).write_text("# Conversation", encoding="utf-8")
    (office / "assessment").mkdir()
    (office / "assessment" / f"2026-01-05-{IDA}-report.md").write_text("Report", encoding="utf-8")
    (office / "assessment" / f"2026-01-05-{IDA}-transcript.md").write_text("Transcript", encoding="utf-8")
    monkeypatch.setattr(config, "FEEDBACK_CONVERSATIONS", 2)
    material = company.feedback_material(M, "")
    assert material["journal"] == [f"{M}/journal/2026-01-07-1000.md", f"{M}/journal/2026-01-08-1100.md"]
    assert material["assessment"] == [f"{M}/assessment/2026-01-05-{IDA}-report.md"] and material["assignments"] == []
    # Since the last meeting only; a report of the day of the meeting counts.
    material = company.feedback_material(M, "2026-01-05-1200")
    assert len(material["journal"]) == 2 and material["assessment"]
    assert company.feedback_material(M, "2026-01-07-1000")["journal"] == [f"{M}/journal/2026-01-08-1100.md"]


def test_feedback_from_evaluation_to_decision(office):
    async def flow():
        runner = HRDesk()
        personnel = Personnel(Conversations(runner))
        with pytest.raises(Rejected):
            personnel.start_feedback(NEW, OWNER)  # candidate
        jid = personnel.start_feedback(MILA, OWNER, "The answers are too long.")
        with pytest.raises(Rejected):
            personnel.start_feedback(MILA, OWNER)  # running already
        await settle(personnel.conversations)
        assert personnel.job(jid)["progress"] == {CORA: {"key": "done"}}
        meeting = company.feedback_state(M)["open"]
        folder = office / "feedback" / meeting
        fbrel = f"{M}/feedback/{meeting}"
        # A single run: Cora evaluates.
        assert [(t.rel, t.procedure, t.writable, t.readable) for t in runner.tasks] == [
            (f"employees/{CORA}", "feedback", [fbrel], [M])]
        task = runner.tasks[0]
        assert "brief.md" in task.message and "too long" in (folder / "brief.md").read_text(encoding="utf-8")
        assert (folder / company.LED_BY).read_text().strip() == CORA
        assert "none yet" in task.context and "What Mila can do technically" in task.context
        assert "The human you write the section for: Paul." in task.context
        assert "There is no agreement from hiring." in task.message
        assert f"Transcripts: /company/{M}/journal/2026-01-05-0900.md" in task.message
        assert f"at most {config.PROFILE_WORDS} words" in task.context and not task.hidden
        assert "You are Mila, more precisely." not in (office / "profile.md").read_text(encoding="utf-8")
        with pytest.raises(Rejected):
            personnel.start_feedback(MILA, OWNER)  # the decision is pending

        proposal = company.feedback_proposal(M, meeting)
        p, = proposal["parts"]
        assert p["part"] == "profile.md" and p["limit"] == config.PROFILE_WORDS
        assert p["header_changed"] and "services" not in p["diff"] and "+You are Mila, more precisely." in p["diff"]
        assert (p["words_before"], p["words"]) == (5, 7)
        with pytest.raises(Problem):  # the profile changed since the page was loaded
            await personnel.decide_feedback(MILA, meeting, True, version="stale")
        assert not (folder / "decision.md").exists()

        # Paul edits the proposal and accepts.
        result = await personnel.decide_feedback(MILA, meeting, True, {"profile.md": "## Task\n\nYou are Mila, brief."},
                                                 proposal["version"])
        assert result["result"] == "accepted_edited"
        with pytest.raises(Problem):
            await personnel.decide_feedback(MILA, meeting, False)  # decided already
        return meeting

    meeting = asyncio.run(flow())
    profile = (office / "profile.md").read_text(encoding="utf-8")
    # The header stays as it was: Cora cannot grant services through it.
    assert "You are Mila, brief." in profile and "services: todo__list_tasks" in profile and "services: all" not in profile
    assert "You are Mila, brief." in (office / "feedback" / meeting / "profile.md").read_text(encoding="utf-8")
    decision = (office / "feedback" / meeting / "decision.md").read_text(encoding="utf-8")
    assert decision.startswith("accepted, edited by hand, on ")
    assert company.feedback_state(M) == {"open": None, "last": meeting}


def test_feedback_proposes_the_way_of_working(office, monkeypatch):
    monkeypatch.setattr(config, "WORK_WORDS", 4)

    class Methodical(DummyRunner):
        delay = 0

        async def run(self, task):
            async for e in super().run(task):
                if e["kind"] == "end" and task.procedure == "feedback":
                    target = config.COMPANY_DIR / task.writable[0]
                    (target / "profile.md").unlink(missing_ok=True)
                    (target / "work.md").write_text("## How you work\n\n- Merged rule, short.\n"
                                                    + ("and much more " * 3 if "too long" not in task.message else ""),
                                                    encoding="utf-8")
                yield e

    async def flow():
        runner = Methodical()
        personnel = Personnel(Conversations(runner))
        personnel.start_feedback(MILA, OWNER)
        await settle(personnel.conversations)
        meeting = company.feedback_state(M)["open"]
        proposal = company.feedback_proposal(M, meeting)
        assert [p["part"] for p in proposal["parts"]] == ["work.md"]
        await personnel.decide_feedback(MILA, meeting, True, version=proposal["version"])

    asyncio.run(flow())
    assert company.part_text(M, company.WORK) == "## How you work\n\n- Merged rule, short."
    assert "You are Mila." in company.body(office / "profile.md")


def test_feedback_shortens_a_long_profile(office, monkeypatch):
    monkeypatch.setattr(config, "PROFILE_WORDS", 3)

    async def flow():
        runner = HRDesk()
        personnel = Personnel(Conversations(runner))
        jid = personnel.start_feedback(MILA, OWNER)
        await settle(personnel.conversations)
        return runner.tasks, personnel.job(jid)

    tasks, job = asyncio.run(flow())
    # Exactly one more run; afterwards what is there applies, even if still too long.
    assert [t.procedure for t in tasks] == ["feedback", "feedback"]
    assert "has 7 words, allowed are at most 3" in tasks[1].message and job["status"] == "done"
    assert company.feedback_state(M)["open"]


def test_feedback_reject_leaves_everything(office):
    (office / company.AGREEMENT).write_text("## Purpose\n\nTasks in order.\n", encoding="utf-8")
    profile = (office / "profile.md").read_text(encoding="utf-8")

    async def flow():
        runner = HRDesk()
        personnel = Personnel(Conversations(runner))
        personnel.start_feedback(MILA, OWNER)
        await settle(personnel.conversations)
        assert f"The agreement from hiring is /company/{M}/agreement.md." in runner.tasks[0].message
        meeting = company.feedback_state(M)["open"]
        assert (await personnel.decide_feedback(MILA, meeting, False))["result"] == "rejected"
        return meeting

    meeting = asyncio.run(flow())
    assert (office / "profile.md").read_text(encoding="utf-8") == profile
    assert company.feedback_meetings(M)[0]["decision"].startswith("rejected on ")
    assert company.feedback_state(M) == {"open": None, "last": meeting}


def test_feedback_without_evaluation_fails_cleanly(office):
    class Silent:
        async def run(self, task):
            yield {"kind": "end", "text": "No interest.", "denied": [], "cost": None, "error": None}

    async def flow():
        personnel = Personnel(Conversations(Silent()))
        jid = personnel.start_feedback(MILA, OWNER, "Beforehand")
        await settle(personnel.conversations)
        return personnel.job(jid)

    j = asyncio.run(flow())
    assert j["status"] == "failed" and "evaluation.md" in j["error"]
    assert not list((office / "feedback").iterdir()) and company.feedback_meetings(M) == []


def test_feedback_without_proposal_is_decided_at_once(office):
    class Content(DummyRunner):
        delay = 0

        async def run(self, task):
            async for e in super().run(task):
                if e["kind"] == "end" and task.procedure == "feedback":
                    (config.COMPANY_DIR / task.writable[0] / "profile.md").unlink()
                yield e

    async def flow():
        personnel = Personnel(Conversations(Content()))
        personnel.start_feedback(MILA, OWNER)
        await settle(personnel.conversations)
        meeting = company.feedback_meetings(M)[0]["meeting"]
        (office / "feedback" / meeting).rename(office / "feedback" / "2026-01-06-0900")
        # Paul gave something beforehand. He reads Cora's answer before the meeting closes.
        personnel.start_feedback(MILA, OWNER, "Mila needs access to Trilium.")
        await settle(personnel.conversations)

    asyncio.run(flow())
    meetings = company.feedback_meetings(M)
    assert len(meetings) == 2 and meetings[1]["decision"].startswith("no changes proposed")
    assert meetings[0]["open"] and company.feedback_proposal(M, meetings[0]["meeting"]) is None


def test_feedback_of_hr_is_led_by_other_hr(office):
    # Cora's work lies with the others: a decided feedback for Mila.
    meeting = office / "feedback" / "2026-01-06-0900"
    meeting.mkdir(parents=True)
    (meeting / "evaluation.md").write_text("# Feedback Mila\n\nSecret content from Mila's profile.\n", encoding="utf-8")
    (meeting / "decision.md").write_text("accepted, edited by hand, on 2026-01-06\n", encoding="utf-8")
    (meeting / company.LED_BY).write_text(CORA + "\n", encoding="utf-8")
    other = office / "feedback" / "2026-01-07-0900"
    other.mkdir()
    (other / "evaluation.md").write_text("# Feedback Mila by Ida\n", encoding="utf-8")
    (other / "decision.md").write_text("rejected on 2026-01-07\n", encoding="utf-8")
    (other / company.LED_BY).write_text(IDA + "\n", encoding="utf-8")

    async def flow():
        runner = HRDesk()
        personnel = Personnel(Conversations(runner))
        jid = personnel.start_feedback(CORA, OWNER)
        await settle(personnel.conversations)
        assert personnel.job(jid)["progress"] == {IDA: {"key": "done"}}
        assert [(t.rel, t.procedure) for t in runner.tasks] == [(f"employees/{IDA}", "feedback")]
        # Ida gets the decisions as a list in the meeting's folder, without contents from Mila's folder.
        fb = company.feedback_meetings(f"employees/{CORA}")[0]["meeting"]
        record = (office.parent / CORA / "feedback" / fb / "track-record.md").read_text(encoding="utf-8")
        assert "- Feedback meeting Mila, 2026-01-06-0900: accepted, edited by hand, on 2026-01-06." in record
        assert "2026-01-07-0900" not in record  # led by Ida, not Cora's work
        assert "Secret content" not in record
        assert f"/company/employees/{CORA}/feedback/{fb}/track-record.md" in runner.tasks[0].message
        assert runner.tasks[0].readable == [f"employees/{CORA}"]
        assert personnel.feedback_leader(MILA) == CORA and personnel.feedback_leader(CORA) == IDA
        company.set_field(office.parent / IDA / "profile.md", "roles", "")
        with pytest.raises(Rejected):
            personnel.start_feedback(CORA, OWNER)
    asyncio.run(flow())


def test_assessment_of_an_employee_does_not_change_the_profile(office):
    company.set_field(office.parent / IDA / "profile.md", "roles", "hr,reviewer")
    profile = (office / "profile.md").read_text(encoding="utf-8")

    async def flow():
        runner = HRDesk()
        personnel = Personnel(Conversations(runner))
        jid = personnel.start_assessment(MILA, OWNER)
        await settle(personnel.conversations)
        j = personnel.job(jid)
        assert j["status"] == "done" and j["progress"] == {IDA: {"key": "done"}}
        # No revision by HR: the reports go into the next feedback meeting.
        assert all(t.procedure != "revision" and not t.writable for t in runner.tasks)
    asyncio.run(flow())
    assert (office / "assessment" / f"{date.today().isoformat()}-{IDA}-report.md").is_file()
    assert (office / "profile.md").read_text(encoding="utf-8") == profile


# ---------------------------------------------------------------- Dismissal, archive

def test_dismissal(office):
    today = date.today()
    personnel = Personnel(Conversations(quick()))

    async def in_conversation():
        personnel.conversations.send(MILA, OWNER, "Hello")
        await settle(personnel.conversations)
        with pytest.raises(Rejected):
            personnel.dismiss(MILA)
        personnel.conversations.end(personnel.conversations.active(MILA, OWNER)["id"])
        await settle(personnel.conversations)
    asyncio.run(in_conversation())
    folder = personnel.dismiss(MILA)["archive"]
    assert folder == f"{MILA}-{today.isoformat()}" and company.archive() == [folder]
    assert (office.parent.parent / "archive" / folder / "profile.md").is_file()
    with pytest.raises(Unknown):
        company.find(MILA)
    assert company.display_name(MILA) == "Mila"  # the name stays known for old conversations
    assert personnel.dismiss(NEW)["archive"].startswith(f"{NEW}-")  # draft discarded
    # HR can be dismissed too. Without HR the features it drives are unavailable, nothing else.
    personnel.dismiss(CORA)
    personnel.dismiss(IDA)
    assert company.holders("hr") == [] and personnel.feedback_leader(NEW) is None


def test_dismiss_during_foreign_work(office):
    personnel = Personnel(Conversations(quick()))
    # Mila is reviewing in the assessment of Neu right now.
    jid = personnel._begin("assessment", NEW, OWNER, [MILA])
    with pytest.raises(Rejected) as problem:
        personnel.dismiss(MILA)
    assert problem.value.key == "works_in_assessment"
    # Ida, as HR, can still join, even if she is not in the progress yet.
    with pytest.raises(Rejected):
        personnel.dismiss(IDA)
    with db.connect() as con:
        con.execute("UPDATE jobs SET status = 'done' WHERE id = ?", (jid,))

    async def busy():
        # Answering as a brought-in colleague right now
        async with personnel.conversations.locks[MILA]:
            with pytest.raises(Rejected):
                personnel.dismiss(MILA)
    asyncio.run(busy())
    assert personnel.dismiss(MILA)["archive"].startswith(f"{MILA}-")


def test_archive_and_rehire(office):
    company.set_field(office / "profile.md", "hired", "2026-01-01")
    personnel = Personnel(Conversations(quick()))
    folder = personnel.dismiss(MILA)["archive"]
    assert company.card(company.archived(folder))["name"] == "Mila"
    for nonsense in (MILA, f"../employees/{CORA}", "9999-2026-01-01"):
        with pytest.raises(Unknown):
            company.archived(nonsense)
    # The name has been given to someone else meanwhile.
    employee(office.parent.parent, "0006", "---\nname: Mila\ntitle: Other\n---\n")
    with pytest.raises(Invalid):
        personnel.rehire(folder)
    company.set_field(office.parent / "0006" / "profile.md", "name", "Mona")
    assert personnel.rehire(folder)["id"] == MILA
    assert company.find(MILA) == M and company.archive() == []
    assert (office / "journal" / "2026-01-05-0900.md").is_file()
    assert company.rehire(personnel.dismiss(NEW)["archive"]) == NEW and company.find(NEW) == N


def test_scheduler(office):
    early_monday, monday, tuesday, saturday = (datetime(2026, 10, 12, 6, 0), datetime(2026, 10, 12, 7, 5),
                                               datetime(2026, 10, 13, 7, 0), datetime(2026, 10, 17, 9, 0))

    async def flow():
        runner = HRDesk()
        service = Conversations(runner)
        scheduler = Scheduler(service)
        for at, days, text, who in (("25:00", [0], "x", MILA), ("07:00", [], "x", MILA),
                                    ("07:00", [9], "x", MILA), ("07:00", [0], " ", MILA), ("07:00", [0], "x", NEW)):
            with pytest.raises(Rejected):
                scheduler.add(who, OWNER, at, days, text)
        sid = scheduler.add(MILA, OWNER, "07:00", [0, 1, 2, 3, 4], "Put my day together.")
        with db.connect() as con:  # as if the schedule was created yesterday
            con.execute("UPDATE schedules SET last_run = NULL WHERE id = ?", (sid,))
        assert scheduler.schedules(MILA)[0]["days_text"] == "Mon–Fri"
        assert scheduler.schedules(MILA, "de")[0]["days_text"] == "Mo–Fr"
        assert scheduler.days_text("0,6", "de") == "Mo, So"
        assert scheduler.check(early_monday) == [] and scheduler.check(saturday) == []
        assert scheduler.check(monday) == [f"Schedule {sid} for {MILA}"] and scheduler.check(monday) == []
        await settle(service)
        c = service.active(MILA, OWNER)
        assert c["channel"] == "schedule" and [m["sender_kind"] for m in service.messages(c["id"])] == ["human", "agent"]
        assert runner.tasks[0].message == "Put my day together." and runner.tasks[0].services
        assert scheduler.check(tuesday) == [f"Schedule {sid} for {MILA}"]
        await settle(service)
        # Paused, it does not come due. Run at once, it goes out and leaves the next due day as it was.
        with db.connect() as con:
            con.execute("UPDATE schedules SET last_run = NULL WHERE id = ?", (sid,))
        assert scheduler.pause(sid, True) and scheduler.check(tuesday) == []
        assert scheduler.schedules(MILA)[0]["paused"] == 1
        assert scheduler.run_now(sid, OWNER)["text"] == "Put my day together."
        await settle(service)
        assert runner.tasks[-1].message == "Put my day together." and scheduler.schedules(MILA)[0]["last_run"] is None
        assert scheduler.pause(sid, False) and not scheduler.pause(999, True)
        with pytest.raises(Unknown):
            scheduler.run_now(999, OWNER)
        service.end(c["id"])
        await settle(service)
        # A feedback meeting never begins by itself.
        assert scheduler.check(saturday) == [] and not company.feedback_meetings(M)

        # A schedule for someone who is gone stops nothing and names the reason.
        with db.connect() as con:
            con.execute("UPDATE schedules SET employee = '9999', last_run = NULL WHERE id = ?", (sid,))
        assert scheduler.check(monday) == []
        assert scheduler.schedules("9999")[0]["error"] == "There is nobody in the team with the ID “9999”."
        assert scheduler.delete(sid) and not scheduler.delete(sid)
    asyncio.run(flow())


def test_daily_backup(firm, monkeypatch):
    from app import backup
    monkeypatch.setattr(config, "BACKUP_TIME", "03:15")
    scheduler = Scheduler(Conversations(quick()))
    assert not scheduler.backup_due(datetime(2026, 10, 12, 3, 0))
    assert scheduler.backup_due(datetime(2026, 10, 12, 3, 20))
    folder = backup.make()
    assert (folder / "company.db").is_file() and (folder / "company.bundle").is_file() and (folder / ".env").is_file()
    assert oct(folder.stat().st_mode)[-3:] == "700" and oct((folder / ".env").stat().st_mode)[-3:] == "600"
    import subprocess
    assert subprocess.run(["git", "bundle", "verify", str(folder / "company.bundle")], cwd=firm,
                          capture_output=True).returncode == 0
    assert not scheduler.backup_due(datetime.now().replace(hour=23))  # today's backup exists
    old = config.BACKUPS / "2020-01-01-031500"
    old.mkdir()
    assert backup.prune() == ["2020-01-01-031500"] and backup.latest() == folder.name
