import asyncio
import sqlite3
import time
from datetime import date

import pytest

from app import company, config, db, notices
from app.conversations import Conversations
from app.errors import Rejected
from app.runner import DummyRunner, Task
from conftest import CORA, IDA, MILA, NEW, OWNER, employee, quick, settle, web

M = f"employees/{MILA}"


class Recording(DummyRunner):
    """Dummy that remembers the tasks."""
    delay = 0

    def __init__(self):
        self.tasks: list[Task] = []

    async def run(self, task):
        self.tasks.append(task)
        async for e in super().run(task):
            yield e


def test_conversation_from_start_to_journal(firm):
    async def flow():
        service = Conversations(quick())
        first = service.send(MILA, OWNER, "Hello Mila")
        service.send(MILA, OWNER, "And one more thing")
        cid = first["conversation_id"]
        assert service.state(cid)["state"] == "running"
        await settle(service)
        assert [m["sender_kind"] for m in service.messages(cid)] == ["human", "human", "agent", "agent"]
        state = service.state(cid)
        assert state["state"] == "open" and state["ends_at"]

        service.pause(cid)
        assert service.state(cid) == {"state": "paused", "ends_at": None}
        service.end(cid)
        with pytest.raises(Rejected):
            service.send(MILA, OWNER, "too late")
        await settle(service)
        c = service.get(cid)
        assert c["status"] == "ended" and (firm / c["transcript"]).is_file()
        # Afterwards a new conversation with its own session begins.
        new = service.send(MILA, OWNER, "New topic")
        assert new["conversation_id"] != cid
        await settle(service)
    asyncio.run(flow())


def test_conversation_is_recorded_word_for_word(firm):
    async def flow():
        runner = Recording()
        service = Conversations(runner)
        cid = service.send(MILA, OWNER, "Buy flowers, today")["conversation_id"]
        await settle(service)
        service.send(MILA, OWNER, "And bread")
        await settle(service)
        service.end(cid)
        await settle(service)
        # No run for a summary: the service records the conversation word for word.
        assert [t.procedure for t in runner.tasks] == ["conversation", "conversation"]
        text = (firm / service.get(cid)["transcript"]).read_text(encoding="utf-8")
        assert "Recorded by the service. Conversation no." in text
        assert "**Paul:**\n\n> Buy flowers, today" in text and "**Mila:**\n\n> This is Mila" in text
        assert "> And bread" in text
        assert any(m["text"].startswith(f"Conversation ended. Transcript: `{M}/journal/")
                   for m in service.messages(cid) if m["sender_kind"] == "system")
        # Both conversations end in the same minute. The second transcript does not overwrite the first.
        second = service.send(MILA, OWNER, "One")["conversation_id"]
        await settle(service)
        service.end(second)
        await settle(service)
        assert service.get(second)["transcript"] != service.get(cid)["transcript"]
        assert len(list((firm / M / "journal").glob("*.md"))) == 2
    asyncio.run(flow())


def test_system_messages_and_transcripts_follow_the_languages(firm):
    company.update_person(OWNER, "Paul Weber", "Paul", "de", "About Paul")

    async def flow():
        runner = Recording()
        service = Conversations(runner)
        cid = service.send(MILA, OWNER, "Hallo")["conversation_id"]
        await settle(service)
        service.end(cid)
        await settle(service)
        # The human reads German, the company writes English.
        assert service.messages(cid)[-1]["text"].startswith("Gespräch beendet. Mitschnitt:")
        assert "Recorded by the service." in (firm / service.get(cid)["transcript"]).read_text(encoding="utf-8")
        assert runner.tasks[0].lang == "de"
    asyncio.run(flow())


def test_reopen_an_ended_conversation(firm):
    async def flow():
        runner = Recording()
        service = Conversations(runner)
        journal = firm / M / "journal"
        cid = service.send(MILA, OWNER, "One")["conversation_id"]
        await settle(service)
        with pytest.raises(Rejected):
            service.reopen(cid, OWNER)  # still running
        service.send(MILA, OWNER, "Two")
        await settle(service)
        service.end(cid)
        await settle(service)
        first = service.get(cid)["transcript"]
        content = (firm / first).read_text(encoding="utf-8")
        with pytest.raises(Rejected):
            service.reopen(cid, "0009")  # not their conversation

        # Reopened and ended again without a message: no further transcript, the first stays recorded.
        service.reopen(cid, OWNER)
        assert service.state(cid)["state"] == "open" and service.state(cid)["ends_at"]
        assert service.active(MILA, OWNER)["id"] == cid
        service.end(cid)
        await settle(service)
        assert service.get(cid)["transcript"] == first and len(list(journal.glob("*.md"))) == 1
        assert service.messages(cid)[-1]["text"] == "Conversation ended, without another transcript."

        # A message: the same session, a note to Mila, and the service records only the continuation.
        service.reopen(cid, OWNER)
        assert service.send(MILA, OWNER, "Three")["conversation_id"] == cid
        await settle(service)
        run = runner.tasks[-1]
        assert run.session == runner.tasks[0].session and not run.new
        assert run.message.startswith("[Paul continues this conversation")
        # The transcripts are hidden in the conversation; a path there would not help.
        assert first not in run.message
        service.end(cid)
        await settle(service)
        second = service.get(cid)["transcript"]
        text = (firm / second).read_text(encoding="utf-8")
        assert second != first and "> Three" in text and "> One" not in text
        assert f"Earlier transcript: `{first.rsplit('/', 1)[1]}`" in text
        assert (firm / first).read_text(encoding="utf-8") == content

        # Two messages after the next continuation: the note comes only once.
        service.reopen(cid, OWNER)
        service.send(MILA, OWNER, "Four")
        await settle(service)
        service.send(MILA, OWNER, "Five")
        await settle(service)
        assert not runner.tasks[-1].message.startswith("[Paul continues")
        service.end(cid)
        await settle(service)
        third = service.get(cid)["transcript"]
        assert third != second and "> Four" in (firm / third).read_text(encoding="utf-8")
        assert len(list(journal.glob("*.md"))) == 3 and all(t.procedure == "conversation" for t in runner.tasks)

        # As long as another conversation with Mila is open, the old one cannot be continued.
        service.send(MILA, OWNER, "New topic")
        await settle(service)
        with pytest.raises(Rejected):
            service.reopen(cid, OWNER)
    asyncio.run(flow())


def test_session_continues(firm):
    async def flow():
        runner = Recording()
        service = Conversations(runner)
        cid = service.send(MILA, OWNER, "one")["conversation_id"]
        await settle(service)
        service.send(MILA, OWNER, "two")
        await settle(service)
        service.end(cid)
        await settle(service)
        return runner.tasks
    tasks = asyncio.run(flow())
    assert [t.new for t in tasks] == [True, False]
    assert len({t.session for t in tasks}) == 1
    assert [t.services for t in tasks] == [True, True]


def test_candidate_and_empty_message(firm):
    async def flow():
        service = Conversations(quick())
        with pytest.raises(Rejected):
            service.send(NEW, OWNER, "Hello")
        with pytest.raises(Rejected):
            service.send(MILA, OWNER, "   ")
    asyncio.run(flow())


def test_denied_tool_becomes_approval_request(firm):
    class Denying:
        async def run(self, task):
            yield {"kind": "end", "text": "I may not do that.", "cost": None, "error": None,
                   "denied": ["mcp__todo__delete_task", "Grep"]}

    async def flow():
        service = Conversations(Denying())
        cid = service.send(MILA, OWNER, "Delete that")["conversation_id"]
        await settle(service)
        service.send(MILA, OWNER, "Please delete that")
        await settle(service)
        note = service.messages(cid)[-1]["text"]
        assert "Not approved: `mcp__todo__delete_task`." in note and "Grep" not in note
    asyncio.run(flow())
    with db.connect() as con:
        requests = con.execute("SELECT * FROM approval_requests").fetchall()
    # Grep was denied by a fixed block. It could not be approved, so it does not become a request either.
    assert len(requests) == 1 and requests[0]["tool"] == "mcp__todo__delete_task"
    assert requests[0]["count"] == 2 and requests[0]["status"] == "open"


def test_error_does_not_stop_the_queue(firm):
    class Broken:
        async def run(self, task):
            raise RuntimeError("Docker missing")
            yield

    async def flow():
        service = Conversations(Broken())
        cid = service.send(MILA, OWNER, "Hello")["conversation_id"]
        old = service.get(cid)["claude_session"]
        await settle(service)
        assert "Docker missing" in service.messages(cid)[-1]["text"]
        assert service.get(cid)["claude_session"] != old
        assert service.state(cid)["state"] == "open"
        service.end(cid)
        await settle(service)
        assert service.get(cid)["transcript"] is None
    asyncio.run(flow())


def test_countdown_ends_the_conversation(firm, monkeypatch):
    monkeypatch.setattr(config, "PAUSE_MINUTES", 0)

    async def flow():
        service = Conversations(quick())
        cid = service.send(MILA, OWNER, "Hello")["conversation_id"]
        await settle(service)
        watcher = asyncio.create_task(service.watch(tick=0.01))
        await asyncio.sleep(0.1)
        await settle(service)
        watcher.cancel()
        assert service.get(cid)["status"] == "ended"
    asyncio.run(flow())


def test_watcher_survives_errors(firm, monkeypatch):
    async def flow():
        service = Conversations(quick())
        cid = service.send(MILA, OWNER, "Hello")["conversation_id"]
        await settle(service)
        with db.connect() as con:
            con.execute("UPDATE conversations SET ends_at = 0 WHERE id = ?", (cid,))
        attempts, real = [], service.end

        def end(c):
            attempts.append(c)
            if len(attempts) == 1:
                raise sqlite3.OperationalError("database is locked")
            real(c)
        monkeypatch.setattr(service, "end", end)
        watcher = asyncio.create_task(service.watch(tick=0.01))
        for _ in range(200):
            if service.get(cid)["status"] == "ended":
                break
            await asyncio.sleep(0.01)
        watcher.cancel()
        await settle(service)
        assert len(attempts) == 2 and service.get(cid)["status"] == "ended"
    asyncio.run(flow())


# ---------------------------------------------------------------- Hiring interview

class Office(DummyRunner):
    """Dummy that remembers the tasks and writes the files a real run would leave."""
    delay = 0

    def __init__(self):
        self.tasks: list[Task] = []

    async def run(self, task):
        self.tasks.append(task)
        if task.procedure == "hiring" and "create" in task.message:
            folder = config.COMPANY_DIR / "candidates" / "hedi"   # HR took the name instead of the ID
            folder.mkdir()
            (folder / "profile.md").write_text("---\nname: Hedi\ntitle: Mails\nstatus: candidate\n---\n",
                                               encoding="utf-8")
        if task.procedure == "revision":
            target = config.COMPANY_DIR / task.writable[0] / "assessment"
            (target / "revision.md").write_text("Recommendation: hire", encoding="utf-8")
        async for e in super().run(task):
            yield e


def test_hiring_interview_creates_candidate(firm):
    company.set_field(firm / M / company.PROFILE, "roles", "hr")

    async def flow():
        runner = Office()
        service = Conversations(runner)
        cid = service.send(MILA, OWNER, "I need someone for mails", kind="hiring")["conversation_id"]
        await settle(service)
        assert not list((firm / "candidates").glob("*/profile.md"))[1:]
        # A normal conversation runs next to it as a conversation of its own.
        assert service.send(MILA, OWNER, "Hello")["conversation_id"] != cid
        service.send(MILA, OWNER, "Fine, create it", kind="hiring")
        await settle(service)
        # The draft got the next free ID, whatever folder HR chose.
        assert "/team/0004" in service.messages(cid)[-1]["text"]
        assert (firm / "candidates" / "0004" / "journal").is_dir() and not (firm / "candidates" / "hedi").exists()
        hiring = [t for t in runner.tasks if t.procedure == "hiring"]
        assert len(hiring) == 2 and all("candidates" in t.writable for t in hiring)
        assert f"at most {config.PROFILE_WORDS} words" in hiring[0].context
        assert "/company/candidates/0004" in hiring[0].context
        assert "Names in the company so far: Mila, Neu, Paul, Paul Weber." in hiring[0].context
        assert all("candidates" not in t.writable for t in runner.tasks if t.procedure == "conversation")
        with pytest.raises(Rejected):
            service.send(MILA, OWNER, "Hello", kind="nonsense")
        company.set_field(firm / M / company.PROFILE, "roles", "")
        with pytest.raises(Rejected) as problem:
            service.send(MILA, OWNER, "Hello", kind="hiring")
        assert problem.value.key == "hiring_needs_hr"
    asyncio.run(flow())


# ---------------------------------------------------------------- Colleagues

class Asking(DummyRunner):
    """Mila asks back as long as she is told to."""
    delay = 0

    def __init__(self, answers: list[str]):
        self.answers = answers       # what Mila says in turn
        self.tasks: list[Task] = []

    async def run(self, task):
        self.tasks.append(task)
        async for e in super().run(task):
            if e["kind"] == "end" and task.rel == M and task.session:
                text = self.answers.pop(0) if self.answers else "Done."
                e = e | {"text": text, "result": text}
            yield e


def test_bring_in_colleagues(office):
    async def flow():
        runner = Recording()
        service = Conversations(runner)
        for colleague in (MILA, NEW, "9999"):
            with pytest.raises(Rejected):
                service.bring_in(MILA, OWNER, colleague, "Hello")
        cid = service.send(MILA, OWNER, "Plan my week.")["conversation_id"]
        await settle(service)
        assert service.bring_in(MILA, OWNER, IDA, "Is that right?")["conversation_id"] == cid
        await settle(service)
        history = service.messages(cid)
        assert [(m["sender_kind"], m["sender"]) for m in history] == [
            ("human", OWNER), ("agent", MILA), ("human", OWNER), ("agent", IDA)]
        assert history[2]["text"] == "**→ Ida:** Is that right?"
        question = runner.tasks[1]
        assert question.rel == f"employees/{IDA}" and question.session is None and not question.writable
        assert not question.services
        assert "Paul: Plan my week." in question.message and question.message.endswith("Paul asks you: Is that right?")
        assert "brought you into" in question.context and service.state(cid)["ends_at"]

        # Mila learns with the next message what Ida said, and not again after that.
        service.send(MILA, OWNER, "What do you say?")
        await settle(service)
        service.send(MILA, OWNER, "Good.")
        await settle(service)
        third, fourth = runner.tasks[2], runner.tasks[3]
        assert third.message.startswith("[Since your last answer") and "Ida: This is Ida" in third.message
        assert third.message.endswith("What do you say?") and not third.new and fourth.message == "Good."

        # If the conversation begins with a colleague, Mila's session still begins only with her first answer.
        service.end(cid)
        await settle(service)
        cid = service.bring_in(MILA, OWNER, IDA, "To you first")["conversation_id"]
        await settle(service)
        service.send(MILA, OWNER, "And now you")
        await settle(service)
        assert runner.tasks[-1].new and runner.tasks[-1].session == service.get(cid)["claude_session"]
    asyncio.run(flow())


class Held(DummyRunner):
    """Ida answers only when the test lets her."""
    delay = 0

    def __init__(self):
        self.free = asyncio.Event()
        self.tasks: list[Task] = []

    async def run(self, task):
        self.tasks.append(task)
        if task.rel == f"employees/{IDA}":
            await self.free.wait()
        async for e in super().run(task):
            yield e


def test_brought_in_colleague_answers_before_the_end(office):
    async def flow():
        runner = Held()
        service = Conversations(runner)
        cid = service.bring_in(MILA, OWNER, IDA, "Have a look")["conversation_id"]
        service.send(MILA, OWNER, "First")
        service.send(MILA, OWNER, "Second")
        await asyncio.sleep(0.05)
        # Mila waits until Ida has answered. So their partial texts do not mix.
        assert [t.rel for t in runner.tasks] == [f"employees/{IDA}"]
        service.end(cid)
        await asyncio.sleep(0.05)
        assert service.get(cid)["status"] == "ending"
        runner.free.set()
        await settle(service)
        # Mila's first run knows Ida' answer, but not the second message, and the first only once.
        first, second = runner.tasks[1:]
        assert first.message.startswith("[Since your last answer") and "Ida: This is Ida" in first.message
        assert first.message.endswith("First") and first.message.count("First") == 1 and "Second" not in first.message
        assert second.message == "Second"
        # The conversation ended only after Ida' answer. Her assignment is recorded.
        assert service.get(cid)["status"] == "ended"
        assert [m["sender"] for m in service.messages(cid) if m["sender_kind"] == "agent"] == [IDA, MILA, MILA]
        assert list((office.parent / IDA / "assignments").glob(f"*-{MILA}-{cid}.md"))
    asyncio.run(flow())


def test_employee_asks_colleague(office, monkeypatch):
    async def flow():
        runner = Asking(["Let me ask.\n\nASK Ida: Is the date right?\nIt is about Friday.",
                         "According to Ida it fits. The appointment stands."])
        service = Conversations(runner)
        cid = service.send(MILA, OWNER, "Plan the appointment.")["conversation_id"]
        await settle(service)
        history = [(m["sender_kind"], m["sender"], m["text"]) for m in service.messages(cid)]
        assert [h[:2] for h in history] == [("human", OWNER), ("agent", MILA), ("agent", MILA),
                                            ("agent", IDA), ("agent", MILA)]
        assert history[1][2] == "Let me ask."
        assert history[2][2] == "**→ Ida:** Is the date right?\nIt is about Friday."
        assert history[4][2] == "According to Ida it fits. The appointment stands."
        first, colleague, second = runner.tasks
        assert "ASK Name" in first.context and first.new and not second.new and second.session == first.session
        assert colleague.rel == f"employees/{IDA}" and colleague.session is None and not colleague.writable
        assert not colleague.services and "cannot ask a colleague yourself" in colleague.context
        assert "Paul: Plan the appointment." in colleague.message and colleague.message.endswith("It is about Friday.")
        assert second.message.startswith("Answer of Ida to your question:") and "2 more asks" in second.message
        # The next message of Paul does not bring the colleague's answer again.
        service.send(MILA, OWNER, "Thanks.")
        await settle(service)
        assert runner.tasks[-1].message == "Thanks."

        # If the colleague reads on the web, she gets not the conversation, only what Mila writes in this message.
        company.set_field(office.parent / IDA / "profile.md", "tools", "WebSearch,WebFetch")
        runner.answers = ["I ask Ida.\n\nASK Ida: When is the library open?", "Until 7 pm."]
        before = len(runner.tasks)
        service.send(MILA, OWNER, "Secret appointment at the doctor, before that to the library.")
        await settle(service)
        first, colleague = runner.tasks[before], runner.tasks[before + 1]
        assert "Whoever reads on the web (Ida)" in first.context
        assert "Secret appointment" not in colleague.message and "Plan the appointment" not in colleague.message
        assert colleague.message == ("From the same message:\n\nMila: I ask Ida.\n\n---\n\n"
                                     "Mila asks you: When is the library open?")
        assert "You do not get the conversation" in colleague.context
        company.set_field(office.parent / IDA / "profile.md", "tools", "")

        # After three asks it is over, even if Mila keeps asking. Unknown names count as well.
        runner.answers = ["ASK Ida: one", "ASK nobody: two", "ASK mila: three", "ASK Ida: four", "ASK Ida: five"]
        before = len(runner.tasks)
        service.send(MILA, OWNER, "And now?")
        await settle(service)
        new = runner.tasks[before:]
        assert [t.rel.split("/")[1] for t in new] == [MILA, IDA, MILA, MILA, MILA, MILA]
        assert "no colleague named “nobody”" in new[3].message and "no colleague named “mila”" in new[4].message
        assert "last ask" in new[4].message and "no ask left" in new[5].message
        assert service.messages(cid)[-1]["text"] == "ASK Ida: five" and runner.answers == []
        assert service.state(cid)["state"] == "open"

        # If the colleague is busy, Mila does not wait forever. Switched off, there are no asks.
        monkeypatch.setattr(config, "ASK_WAIT", 0.05)
        runner.answers = ["ASK ida: six"]
        async with service.locks[IDA]:
            service.send(MILA, OWNER, "One more thing.")
            await settle(service)
        assert "busy right now" in runner.tasks[-1].message
        monkeypatch.setattr(config, "ASKS", 0)
        runner.answers = ["ASK ida: seven"]
        before = len(runner.tasks)
        service.send(MILA, OWNER, "Last.")
        await settle(service)
        assert len(runner.tasks) == before + 1 and "ASK " not in runner.tasks[-1].context
    asyncio.run(flow())


def test_employee_leaves_a_notice(office):
    async def flow():
        runner = Asking(["Three new studies.\n\n- A\n- B\n\n**NOTICE:** 3 new studies on LLM psychology: A, B.",
                         "Let me ask.\n\nNOTICE: Found something.\n\nASK Ida: Is it new?", "Ida says it is new.",
                         "Nothing new.", "NOTICE: " + "x" * (notices.LENGTH + 10)])
        service = Conversations(runner)
        cid = service.send(MILA, OWNER, "Check your sources.")["conversation_id"]
        await settle(service)
        history = [(m["sender_kind"], m["text"]) for m in service.messages(cid)]
        # The chat keeps the answer without the notice and says where it went.
        assert history[1] == ("agent", "Three new studies.\n\n- A\n- B")
        assert history[2][0] == "system" and "/notices#n-" in history[2][1]
        assert "NOTICE: text" in runner.tasks[0].context
        [notice] = notices.listing(OWNER)
        assert notice["text"] == "3 new studies on LLM psychology: A, B." and notice["employee"] == MILA
        assert notice["conversation_id"] == cid and notices.unread(OWNER) == 1

        # Before an ask the notice is taken out as well; the asked colleague does not read it.
        before = len(runner.tasks)
        service.send(MILA, OWNER, "And the other source?")
        await settle(service)
        assert "Found something" not in runner.tasks[before + 1].message
        assert [n["text"] for n in notices.listing(OWNER)] == ["Found something.", notice["text"]]

        # Nothing worth a notice, nothing on the page. A notice alone leaves no empty answer, and a long one is cut.
        service.send(MILA, OWNER, "Anything else?")
        await settle(service)
        service.send(MILA, OWNER, "And now?")
        await settle(service)
        assert service.messages(cid)[-1]["sender_kind"] == "system"
        latest = notices.listing(OWNER)
        assert len(latest) == 3 and latest[0]["text"].endswith(" …")
        assert len(latest[0]["text"]) == notices.LENGTH + 2

        # Only the human it is for marks it as read.
        assert notices.mark_read("9999", latest[0]["id"]) == 0
        assert notices.mark_read(OWNER, latest[0]["id"]) == 1 and notices.unread(OWNER) == 2
        assert notices.mark_read(OWNER) == 2 and notices.unread(OWNER) == 0
    asyncio.run(flow())


def test_archive_only_for_the_archivist(office, monkeypatch):
    firm = office.parent.parent
    employee(firm, "0006", "---\nname: Max\ntitle: Archivist\nroles: archivist\n---\n")
    (firm / "employees" / IDA / "assignments").mkdir()
    company.set_field(office / "profile.md", "tools", "")
    assert company.archivist() == "employees/0006"
    assert company.history_dirs() == [f"employees/{MILA}/journal", f"employees/{IDA}/assignments"]
    # Whoever reviews or evaluates learns that the archive is hidden in conversations.
    assert "transcripts and assignments are hidden" in company.capabilities_of(M)
    assert "hidden" not in company.capabilities_of("employees/0006")

    async def flow():
        runner = Asking(["ASK Max: What did we have on 15.10. for the bowling night?", "According to Max 7 pm."])
        service = Conversations(runner)
        cid = service.send(MILA, OWNER, "Enter the bowling night like last time.")["conversation_id"]
        await settle(service)
        first, max, second = runner.tasks
        # In the conversation Mila sees neither her transcripts nor foreign assignments. Of her own folder only the
        # two parts of the profile are writable; the rest is read-only on top.
        assert first.hidden == [f"employees/{MILA}/journal", f"employees/{IDA}/assignments"]
        assert first.writable == [M] and f"{M}/journal" in first.protected and f"{M}/work.md" not in first.protected
        assert "with today's state" in first.context and "you ask Max" in first.context
        # Max sees the archive, also as the one asked.
        assert max.rel == "employees/0006" and max.hidden == [] and "You keep the archive" in max.context
        assert second.message.startswith("Answer of Max")
        service.end(cid)
        await settle(service)
        # The end costs no run; the service records.
        assert len(runner.tasks) == 3

        # Whoever reads on the web gets no information from the archive but shall ask Paul to bring in Max.
        company.set_field(office / "profile.md", "tools", "WebSearch")
        runner.answers = ["ASK Max: What was on 15.10.?", "Then without."]
        before = len(runner.tasks)
        cid = service.send(MILA, OWNER, "Like last time.")["conversation_id"]
        await settle(service)
        new = runner.tasks[before:]
        assert [t.rel for t in new] == [M, M]
        assert "ask them to bring in Max" in new[0].context
        assert "cannot ask Max, because you read on the web" in new[1].message
        assert any("no information from the archive" in m["text"] for m in service.messages(cid))
        service.end(cid)
        await settle(service)

        # Without an archivist the archive is hidden for everyone; Paul is asked.
        company.set_field(firm / "employees" / "0006" / "profile.md", "roles", "")
        service.bring_in(MILA, OWNER, "0006", "What do you remember?")
        await settle(service)
        assert runner.tasks[-1].rel == "employees/0006" and runner.tasks[-1].hidden
        assert "ask them for the details" in service.history_note(M, can_ask=True)
    asyncio.run(flow())


def test_no_web_after_the_archive(office):
    firm = office.parent.parent
    employee(firm, "0006", "---\nname: Max\ntitle: Archivist\nroles: archivist\n---\n")
    company.set_field(office / "profile.md", "tools", "")
    company.set_field(firm / "employees" / IDA / "profile.md", "tools", "WebSearch")

    async def flow():
        # First the web, then the archive: the digest that leaves out what was shown already.
        runner = Asking(["ASK Ida: What is new on the sites?", "ASK Max: What of it was shown already?", "Only new."])
        service = Conversations(runner)
        cid = service.send(MILA, OWNER, "News of today.")["conversation_id"]
        await settle(service)
        assert [t.rel for t in runner.tasks] == [M, f"employees/{IDA}", M, "employees/0006", M]
        service.end(cid)
        await settle(service)

        # The other way round the history could go out through the one who fetches addresses.
        runner.tasks, runner.answers = [], ["ASK Max: What was on 15.10.?", "ASK Ida: Fetch this address.", "No."]
        cid = service.send(MILA, OWNER, "Like last time.")["conversation_id"]
        await settle(service)
        assert [t.rel for t in runner.tasks] == [M, "employees/0006", M, M]
        assert "cannot ask Ida in this conversation" in runner.tasks[-1].message
        assert any("archive has been consulted" in m["text"] for m in service.messages(cid))

        # Also in the next message of the same conversation.
        runner.tasks, runner.answers = [], ["ASK Ida: Now?", "No."]
        service.send(MILA, OWNER, "And now?")
        await settle(service)
        assert [t.rel for t in runner.tasks] == [M, M]
        service.end(cid)
        await settle(service)
    asyncio.run(flow())


def test_assignments_for_colleagues(office):
    async def flow():
        runner = Asking(["Let me ask.\n\nASK Ida: Is the date right?", "According to Ida it fits."])
        service = Conversations(runner)
        cid = service.send(MILA, OWNER, "Plan the appointment.")["conversation_id"]
        await settle(service)
        service.send(MILA, OWNER, "The date was wrong.")
        await settle(service)
        service.bring_in(MILA, OWNER, IDA, "Check that again.")
        await settle(service)
        folder = office.parent / IDA / "assignments"
        assert not folder.exists()
        service.end(cid)
        await settle(service)

        # One log per conversation in the colleague's folder, with both tasks and what came afterwards.
        file, = folder.glob("*.md")
        assert file.stem.endswith(f"-{MILA}-{cid}") and file.stem.startswith(date.today().isoformat())
        text = file.read_text(encoding="utf-8")
        assert text.startswith(f"# Assignment of {date.today().isoformat()} in the conversation of Paul with Mila")
        assert "not by Ida" in text and "2 tasks" in text
        first, second = text.split("## Task ")[1:]
        assert first.startswith("1: asked by Mila") and "> Plan the appointment." in first
        assert "### Question\n\n> Is the date right?" in first and "### Answer\n\n> This is Ida" in first
        assert "Mila:\n\n> According to Ida it fits." in first and first.rstrip().endswith("> The date was wrong.")
        assert second.startswith("2: brought in by Paul") and "### Occasion" not in second
        assert "> Check that again." in second and second.rstrip().endswith("Nothing more.")
        assert not list((office / "assignments").glob("*.md"))

        # The card names the last assignment.
        file.rename(folder / f"2026-01-05-0900-{MILA}-{cid}.md")
        assert company.card(f"employees/{IDA}")["last_assignment"] == "2026-01-05"

        # Also a conversation in which Mila never answered leaves the assignment. Writing again changes nothing.
        cid = service.bring_in(MILA, OWNER, IDA, "Only you.")["conversation_id"]
        await settle(service)
        service.end(cid)
        await settle(service)
        assert service.get(cid)["transcript"] is None and len(list(folder.glob("*.md"))) == 2
        assert service.write_assignments(cid) and len(list(folder.glob("*.md"))) == 2
    asyncio.run(flow())


def test_tool_log(office):
    class Surfing(Asking):
        """Ida looks things up on the web before she answers."""
        async def run(self, task):
            if task.rel == f"employees/{IDA}":
                yield {"kind": "tool", "name": "WebSearch"}
                yield {"kind": "call", "name": "WebSearch", "detail": "Holidays `Bavaria`\n2026"}
                yield {"kind": "call", "name": "WebFetch", "detail": "https://example.org/holidays?year=2026"}
                yield {"kind": "call", "name": "Grep", "detail": "<b>" + "x" * 400}
            async for e in super().run(task):
                yield e

    async def flow():
        runner = Surfing(["ASK Ida: Is Friday a holiday?", "No holiday."])
        service = Conversations(runner)
        cid = service.send(MILA, OWNER, "Plan Friday.")["conversation_id"]
        seen = []

        async def listen():
            async for kind, data in service.subscribe(cid):
                seen.append((kind, data))
        listener = asyncio.create_task(listen())
        await settle(service)
        listener.cancel()
        history = service.messages(cid)
        assert [(m["sender_kind"], m["sender"]) for m in history] == [
            ("human", OWNER), ("agent", MILA), ("tools", IDA), ("agent", IDA), ("agent", MILA)]
        head, _, lines = history[2]["text"].partition("\n\n")
        assert head == "3 tool calls, 1 web search, 1 page fetched"
        assert lines.split("\n")[:2] == ["- WebSearch: `Holidays 'Bavaria' 2026`",
                                         "- WebFetch: <https://example.org/holidays?year=2026>"]
        assert lines.split("\n")[2].startswith("- Grep: `<b>xxx") and lines.endswith("` …")
        # The colleague can be seen while she works, and Mila does not get to read the log.
        assert ("tool", {"name": "WebSearch", "sender": "Ida"}) in seen
        assert {d["sender"] for kind, d in seen if kind == "delta"} == {"Mila", "Ida"}
        assert "tool calls" not in runner.tasks[2].message
        service.bring_in(MILA, OWNER, CORA, "And you?")
        await settle(service)
        assert "tool calls" not in runner.tasks[-1].message and "Ida: This is Ida" in runner.tasks[-1].message
        return cid
    cid = asyncio.run(flow())
    with web() as client:
        html = client.get(f"/conversation/{cid}").text
        assert '<details class="message tools"' in html and "Ida · " in html
        assert '<a href="https://example.org/holidays?year=2026">' in html and "&lt;b&gt;" in html


class Hanging(Recording):
    """Writes a bit and then hangs until it is stopped, as long as "hang" is set."""

    def __init__(self):
        super().__init__()
        self.hang = False

    async def run(self, task):
        if not self.hang:
            async for e in super().run(task):
                yield e
            return
        self.tasks.append(task)
        yield {"kind": "call", "name": "WebSearch", "detail": "flowers"}
        yield {"kind": "text", "text": "Half an "}
        yield {"kind": "text", "text": "answer"}
        await asyncio.Event().wait()


async def writing(service, cid):
    """Wait until a run has begun to write."""
    while not service.partial.get(cid):
        await asyncio.sleep(0.01)


def test_stop_ends_every_run_of_the_conversation(office):
    async def flow():
        runner = Hanging()
        service = Conversations(runner)
        cid = service.send(MILA, OWNER, "one")["conversation_id"]
        await settle(service)
        session = service.get(cid)["claude_session"]
        before = len(service.messages(cid))

        # A running answer, a brought-in colleague and another message waiting behind it: all of them stop.
        runner.hang = True
        service.send(MILA, OWNER, "two")
        service.bring_in(MILA, OWNER, IDA, "Is that right?")
        service.send(MILA, OWNER, "three")
        await writing(service, cid)
        await service.stop(cid)
        assert not service.running[cid] and service.pending[cid] == 0
        state = service.state(cid)
        assert state["state"] == "open" and state["ends_at"]
        history = [(m["sender_kind"], m["sender"], m["text"]) for m in service.messages(cid)[before:]]
        assert [h[:2] for h in history] == [("human", OWNER)] * 3 + [("tools", MILA), ("agent", MILA),
                                                                    ("system", "system")]
        assert history[4][2] == "Half an answer\n\n*(stopped)*" and history[5][2] == "Stopped."
        with db.connect() as con:
            assert [r["status"] for r in con.execute("SELECT status FROM runs ORDER BY id")] == ["done", "stopped"]
        assert len(runner.tasks) == 2
        # The session had begun already; it continues, and the employee learns that they were stopped.
        assert service.get(cid)["claude_session"] == session

        runner.hang = False
        service.send(MILA, OWNER, "four")
        await settle(service)
        last = runner.tasks[-1]
        assert not last.new and last.session == session
        assert last.message == "[Paul stopped your previous answer before it was finished.]\n\nfour"
        service.send(MILA, OWNER, "five")
        await settle(service)
        assert runner.tasks[-1].message == "five"

        # Stopping a quiet conversation does nothing.
        await service.stop(cid)
        assert service.messages(cid)[-1]["sender_kind"] == "agent"
        service.end(cid)
        await settle(service)
        transcript = (config.COMPANY_DIR / service.get(cid)["transcript"]).read_text(encoding="utf-8")
        assert "> Half an answer" in transcript
    asyncio.run(flow())


def test_stop_a_first_answer_and_a_colleague(office):
    async def flow():
        runner = Hanging()
        runner.hang = True
        service = Conversations(runner)
        cid = service.send(MILA, OWNER, "Hello")["conversation_id"]
        session = service.get(cid)["claude_session"]
        await writing(service, cid)
        await service.stop(cid)
        # The first run may have left half a session. The next message starts a new one.
        assert service.get(cid)["claude_session"] != session
        runner.hang = False
        service.send(MILA, OWNER, "Hello again")
        await settle(service)
        assert runner.tasks[-1].new and runner.tasks[-1].message == "Hello again"

        # A brought-in colleague is stopped as well. Her cut-off answer is hers, Mila's session stays.
        session = service.get(cid)["claude_session"]
        runner.hang = True
        service.bring_in(MILA, OWNER, IDA, "And you?")
        await writing(service, cid)
        await service.stop(cid)
        assert [(m["sender_kind"], m["sender"]) for m in service.messages(cid)[-2:]] == [
            ("agent", IDA), ("system", "system")]
        assert service.get(cid)["claude_session"] == session
        assert service.state(cid)["state"] == "open"
    asyncio.run(flow())


def test_stop_through_the_web(firm):
    with web() as client:
        m = client.post(f"/chat/{MILA}/message", json={"text": "Hello"}).json()
        assert 'id="stop"' in client.get(f"/chat/{MILA}").text
        response = client.post(f"/conversation/{m['conversation']}/stop")
        assert response.status_code == 200 and response.json()["state"] in ("open", "running")
        assert client.post(f"/conversation/{m['conversation']}/jump").status_code == 404


class Learning(Recording):
    """Dummy that adds a rule to its own profile when told so, as Claude Code would through Edit."""

    async def run(self, task):
        if "from now on" in task.message and task.rel in task.writable:
            profile = config.COMPANY_DIR / task.rel / company.PROFILE
            text = profile.read_text(encoding="utf-8").replace("model:", "model: opus")
            profile.write_text(text + "- Appointments always with the place.\n", encoding="utf-8")
        async for e in super().run(task):
            yield e


def test_employee_changes_own_profile(firm):
    profile = firm / M / company.PROFILE

    async def flow():
        runner = Learning()
        service = Conversations(runner)
        cid = service.send(MILA, OWNER, "From now on always with the place, please.".lower())["conversation_id"]
        await settle(service)
        # The rule applies, the header came back. The chat shows the difference, Git has the version.
        assert "- Appointments always with the place." in company.body(profile)
        assert company.header(profile)["model"] == ""
        note = next(m for m in service.messages(cid) if m["sender_kind"] == "profile")
        assert "+- Appointments always with the place." in note["text"] and "model" not in note["text"]
        assert service.profile_change(note["id"])["state"] == "applied"
        assert company.profile_versions(M)[0]["subject"] == f"Profile of Mila changed by Mila in conversation {cid}: profile.md"
        assert "You can change both parts of your profile" in runner.tasks[0].context

        # Undo: the earlier text applies again, and Mila learns of it with the next message.
        await service.decide_profile_change(note["id"], "undo", OWNER)
        assert "Appointments" not in company.body(profile)
        with pytest.raises(Rejected):
            await service.decide_profile_change(note["id"], "undo", OWNER)
        service.send(MILA, OWNER, "Thanks")
        await settle(service)
        assert runner.tasks[-1].message.startswith("[Paul undid your change to profile.md.")

        # Without own changes it is a proposal; the human accepts it.
        company.set_header(M, {"self_edit": "no"})
        service.send(MILA, OWNER, "from now on with the place")
        await settle(service)
        proposal = [m for m in service.messages(cid) if m["sender_kind"] == "profile"][-1]
        assert service.profile_change(proposal["id"])["state"] == "proposed"
        assert "Appointments" not in company.body(profile)
        await service.decide_profile_change(proposal["id"], "accept", OWNER)
        assert "- Appointments always with the place." in company.body(profile)

        # While Mila runs, a human's change waits: afterwards it would be taken for hers and put back.
        async with service.locks[MILA]:
            assert service.busy(MILA)
            with pytest.raises(Rejected):
                await service.decide_profile_change(proposal["id"], "undo", OWNER)
        service.end(cid)
        await settle(service)
        transcript = (firm / service.get(cid)["transcript"]).read_text(encoding="utf-8")
        assert "**Own change of Mila to the profile (core, undone by a human):**" in transcript
        assert "```diff" in transcript
    asyncio.run(flow())


def test_profile_change_through_the_web(firm):
    with web(runner=Learning()) as client:
        cid = client.post(f"/chat/{MILA}/message", json={"text": "from now on with the place"}).json()["conversation"]
        # The note comes before the answer. Undo waits until Mila is done.
        for _ in range(200):
            if client.post(f"/conversation/{cid}/pause").json()["state"] == "paused":
                break
            time.sleep(0.02)
        notes = [m for m in Conversations(None).messages(cid) if m["sender_kind"] == "profile"]
        page = client.get(f"/conversation/{cid}").text
        assert "changed the own profile" in page and 'data-action="undo"' in page and 'class="add"' in page
        answer = client.post(f"/profile-change/{notes[0]['id']}/undo")
        assert answer.status_code == 200 and "change to the profile undone" in answer.json()["html"]
        assert client.post(f"/profile-change/{notes[0]['id']}/undo").status_code == 409
        assert client.post("/profile-change/999/undo").status_code == 404
        # The card lists the versions; an earlier one can be restored.
        card = client.get(f"/team/{MILA}").text
        assert "Versions (" in card and "Own changes to the profile" in card
        versions = company.profile_versions(M)
        page = client.get(f"/team/{MILA}/profile/{versions[0]['commit']}")
        assert page.status_code == 200 and "Version of the profile" in page.text
        changed = next(v for v in versions if "changed by Mila" in v["subject"])
        assert client.post(f"/team/{MILA}/profile/{changed['commit']}/restore").status_code == 200
        assert "Appointments always" in company.body(firm / M / company.PROFILE)
        assert company.edits(M)[0]["reason"].startswith("version of")


class Wordy(Recording):
    """Dummy that first adds too much to its profile and, when told so, makes room."""

    async def run(self, task):
        profile = config.COMPANY_DIR / task.rel / company.PROFILE
        if task.rel in task.writable and "note this" in task.message:
            profile.write_text(profile.read_text(encoding="utf-8") + "- A long rule " + "word " * 20 + "\n",
                               encoding="utf-8")
        elif task.rel in task.writable and "was not saved" in task.message:
            profile.write_text(profile.read_text(encoding="utf-8").replace("You are Mila.", "- Short rule."),
                               encoding="utf-8")
        async for e in super().run(task):
            yield e


def test_too_long_change_gets_one_more_run(firm, monkeypatch):
    monkeypatch.setattr(config, "PROFILE_WORDS", 5)

    async def flow():
        runner = Wordy()
        service = Conversations(runner)
        cid = service.send(MILA, OWNER, "Please note this")["conversation_id"]
        await settle(service)
        first, second = runner.tasks
        # Mila learns at once that it was not saved, in the same session, and answers again.
        assert "would have 29 words, at most 5" in second.message and not second.new
        assert second.session == first.session
        states = [service.profile_change(m["id"])["state"] for m in service.messages(cid)
                  if m["sender_kind"] == "profile"]
        assert states == ["too_long", "applied"]
        assert "- Short rule." in company.body(firm / M / company.PROFILE)
        # Only the second answer reaches Paul, and nothing is left for the next message.
        assert [m["sender_kind"] for m in service.messages(cid)].count("agent") == 1
        assert not service.remarks.get(cid)
    asyncio.run(flow())
