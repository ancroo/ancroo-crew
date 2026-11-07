"""Notices: what an employee leaves for a human on the page "Notices".

In a working conversation an employee ends their answer with "NOTICE: text". The service takes it out of the answer and
keeps it for the human of the conversation, who sees it without opening the conversation, e.g. the result of a
recurring task. Notices are operation, not company: they are only in the database.
"""
import re
import time

from . import db

# The notice is at the end of the answer. Everything after the colon is its text.
NOTICE = re.compile(r"^[`*> ]*NOTICE:[`* \t]*(.+)\Z", re.MULTILINE | re.DOTALL)
# This many characters of a notice are kept.
LENGTH = 2000
# This many read notices the page shows; unread ones are all shown.
SHOWN = 100


def take(text: str) -> tuple[str, str]:
    """The answer without the notice, and the notice. Without one the notice is empty."""
    found = NOTICE.search(text or "")
    if not found:
        return text, ""
    notice = found.group(1).strip()
    if len(notice) > LENGTH:
        notice = notice[:LENGTH] + " …"
    return text[:found.start()].rstrip(), notice


def post(employee: str, person: str, cid: int | None, text: str) -> int:
    with db.connect() as con:
        return con.execute("INSERT INTO notices (employee, person, conversation_id, text, created) "
                           "VALUES (?, ?, ?, ?, ?)", (employee, person, cid, text, time.time())).lastrowid


def listing(person: str, read: bool = False):
    """The notices of this human, the newest first: all unread ones, or the latest read ones."""
    with db.connect() as con:
        if read:
            return con.execute("SELECT * FROM notices WHERE person = ? AND read IS NOT NULL ORDER BY id DESC LIMIT ?",
                               (person, SHOWN)).fetchall()
        return con.execute("SELECT * FROM notices WHERE person = ? AND read IS NULL ORDER BY id DESC",
                           (person,)).fetchall()


def unread(person: str) -> int:
    with db.connect() as con:
        return con.execute("SELECT COUNT(*) FROM notices WHERE person = ? AND read IS NULL", (person,)).fetchone()[0]


def mark_read(person: str, nid: int | None = None) -> int:
    """Mark one notice of this human as read, without an ID all of them. Returns how many were unread."""
    with db.connect() as con:
        return con.execute("UPDATE notices SET read = ? WHERE person = ? AND read IS NULL AND (? IS NULL OR id = ?)",
                           (time.time(), person, nid, nid)).rowcount
