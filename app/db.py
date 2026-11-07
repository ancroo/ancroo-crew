"""SQLite for the operational data. What defines someone stays in files in the company's Git repository."""
import sqlite3
import time
from contextlib import contextmanager

from . import config

# The version of the data layout, stored here and in the company. A migration raises it.
# 2: no probation and no memo; the employees change their profile themselves.
SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS people (               -- the humans; name and roles are in people/<id>/profile.md
    id        TEXT PRIMARY KEY,
    password  TEXT,                               -- salt$scrypt-hash
    created   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id             INTEGER PRIMARY KEY,
    employee       TEXT NOT NULL,                 -- ID of the AI employee
    person         TEXT NOT NULL,                 -- ID of the human
    channel        TEXT NOT NULL DEFAULT 'web',
    kind           TEXT NOT NULL DEFAULT 'conversation',  -- conversation, hiring
    claude_session TEXT NOT NULL,                 -- session ID of Claude Code
    status         TEXT NOT NULL DEFAULT 'open',  -- open, paused, ending, ended
    started        REAL NOT NULL,
    last_activity  REAL NOT NULL,
    ends_at        REAL,                          -- the conversation ends by itself then
    ended          REAL,
    transcript     TEXT,                          -- path of the latest transcript in journal, relative to the company
    resumed        REAL                           -- when it was last resumed after it had ended
);
CREATE INDEX IF NOT EXISTS conversations_employee ON conversations(employee, status);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id),
    sender_kind     TEXT NOT NULL,                -- human, agent, system, tools, profile
    sender          TEXT NOT NULL,
    text            TEXT NOT NULL,
    created         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_conversation ON messages(conversation_id, id);

CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id),
    kind            TEXT NOT NULL,                -- conversation, colleague
    status          TEXT NOT NULL DEFAULT 'running',  -- running, done, failed, stopped
    started         REAL NOT NULL,
    ended           REAL,
    cost_usd        REAL,
    error           TEXT
);

CREATE TABLE IF NOT EXISTS usage (                -- one row per run and model the run used
    id              INTEGER PRIMARY KEY,
    run             TEXT NOT NULL,                -- connects the rows of one run
    time            REAL NOT NULL,
    employee        TEXT NOT NULL,                -- whose prompt ran
    procedure       TEXT NOT NULL,                -- the procedure, i.e. the kind of prompt
    purpose         TEXT NOT NULL,                -- conversation, colleague or the kind of job
    conversation_id INTEGER,
    job_id          INTEGER,
    model           TEXT NOT NULL,
    input           INTEGER NOT NULL DEFAULT 0,   -- input without cache
    cache_write     INTEGER NOT NULL DEFAULT 0,
    cache_read      INTEGER NOT NULL DEFAULT 0,
    output          INTEGER NOT NULL DEFAULT 0,   -- including thinking
    web_searches    INTEGER NOT NULL DEFAULT 0,
    cost_usd        REAL,                         -- what Claude Code charges for it at API prices
    failed          INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS usage_time ON usage(time);

CREATE TABLE IF NOT EXISTS approval_requests (
    id              INTEGER PRIMARY KEY,
    employee        TEXT NOT NULL,
    tool            TEXT NOT NULL,
    conversation_id INTEGER REFERENCES conversations(id),
    first           REAL NOT NULL,
    last            REAL NOT NULL,
    count           INTEGER NOT NULL DEFAULT 1,
    status          TEXT NOT NULL DEFAULT 'open',  -- open, granted, denied
    decided_by      TEXT,
    decided_at      REAL,
    UNIQUE (employee, tool)
);

CREATE TABLE IF NOT EXISTS jobs (
    id        INTEGER PRIMARY KEY,
    kind      TEXT NOT NULL,                      -- assessment, feedback
    employee  TEXT NOT NULL,                      -- who it is about
    person    TEXT NOT NULL,                      -- who started it
    status    TEXT NOT NULL DEFAULT 'running',    -- running, done, failed
    progress  TEXT NOT NULL DEFAULT '{}',         -- JSON: participant -> what they are doing, as text key and values
    started   REAL NOT NULL,
    ended     REAL,
    cost_usd  REAL,
    error     TEXT                                -- with "done": a note, e.g. a failed Git commit
);
CREATE INDEX IF NOT EXISTS jobs_employee ON jobs(employee, id);

CREATE TABLE IF NOT EXISTS schedules (
    id        INTEGER PRIMARY KEY,
    employee  TEXT NOT NULL,
    person    TEXT NOT NULL,                      -- on whose behalf the message comes
    time      TEXT NOT NULL,                      -- HH:MM
    days      TEXT NOT NULL,                      -- weekdays, 0 = Monday, comma separated
    text      TEXT NOT NULL,
    last_run  TEXT,                               -- day of the last run, YYYY-MM-DD
    error     TEXT,                               -- why the last run did not happen
    created   REAL NOT NULL,
    paused    INTEGER NOT NULL DEFAULT 0          -- 1: stays, but does not come due
);

CREATE TABLE IF NOT EXISTS sessions (             -- logins in the browser
    token   TEXT PRIMARY KEY,                     -- SHA-256 of the cookie, never the cookie itself
    person  TEXT NOT NULL,
    until   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS profile_changes (      -- what an employee changed in their own profile, as in the chat
    id              INTEGER PRIMARY KEY,
    message_id      INTEGER NOT NULL UNIQUE REFERENCES messages(id),  -- the note with the difference
    conversation_id INTEGER NOT NULL REFERENCES conversations(id),
    employee        TEXT NOT NULL,
    part            TEXT NOT NULL DEFAULT 'profile.md',  -- profile.md (the core) or work.md (the way of working)
    state           TEXT NOT NULL,  -- applied, proposed, too_long; after a human's decision undone, accepted, discarded
    before          TEXT NOT NULL,  -- the text below the header
    after           TEXT NOT NULL,
    created         REAL NOT NULL,
    decided_by      TEXT,
    decided_at      REAL
);

CREATE TABLE IF NOT EXISTS notices (              -- what an employee left for a human on the page "Notices"
    id              INTEGER PRIMARY KEY,
    employee        TEXT NOT NULL,
    person          TEXT NOT NULL,                -- for whom: the human of the conversation
    conversation_id INTEGER REFERENCES conversations(id),
    text            TEXT NOT NULL,
    created         REAL NOT NULL,
    read            REAL                          -- when the human marked it as read
);
CREATE INDEX IF NOT EXISTS notices_person ON notices(person, read);

CREATE TABLE IF NOT EXISTS favorites (
    person    TEXT NOT NULL,
    employee  TEXT NOT NULL,
    PRIMARY KEY (person, employee)
);
"""


class SchemaMismatch(Exception):
    """The database belongs to another version of the data layout than the code or the company."""


@contextmanager
def connect():
    con = sqlite3.connect(config.DATABASE, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    try:
        with con:
            yield con
    finally:
        con.close()


def setup(version: int = SCHEMA_VERSION) -> None:
    """Create the tables. A new database records the version; an existing one must already have it."""
    config.DATABASE.parent.mkdir(parents=True, exist_ok=True)
    with connect() as con:
        con.execute("PRAGMA journal_mode = WAL")
        con.executescript(SCHEMA)
        # Columns added later. CREATE TABLE IF NOT EXISTS leaves an existing table as it is.
        for table, column, definition in (("profile_changes", "part", "TEXT NOT NULL DEFAULT 'profile.md'"),
                                          ("schedules", "paused", "INTEGER NOT NULL DEFAULT 0")):
            if column not in {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        row = con.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        if row is None:
            con.execute("INSERT INTO meta (key, value) VALUES ('schema_version', ?)", (str(version),))
        elif row["value"] != str(version):
            raise SchemaMismatch(f"The database {config.DATABASE} has schema version {row['value']}, "
                                 f"expected {version}.")


def add_person(person_id: str) -> None:
    with connect() as con:
        con.execute("INSERT OR IGNORE INTO people (id, created) VALUES (?, ?)", (person_id, time.time()))
