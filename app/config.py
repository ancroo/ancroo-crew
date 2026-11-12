"""Settings of the service. Everything can be changed through environment variables."""
import os
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent

# The company: profiles, handbook, people. Its own Git repository, every change is committed there.
COMPANY_DIR = Path(os.environ.get("COMPANY_DIR", PROJECT / "company")).resolve()
# Working files of Claude Code (sessions, prompts, MCP extracts).
RUNTIME = COMPANY_DIR / "runtime"
# Operational data: conversations, messages, runs, approval requests.
DATABASE = Path(os.environ.get("COMPANY_DB", PROJECT / "data" / "company.db")).resolve()
# Daily backups and the backup before a migration.
BACKUPS = Path(os.environ.get("COMPANY_BACKUPS", PROJECT / "backups")).resolve()
# Keep backups for this many days.
BACKUP_DAYS = int(os.environ.get("COMPANY_BACKUP_DAYS", "7"))
# Daily backup at this local time, HH:MM. Empty switches it off.
BACKUP_TIME = os.environ.get("COMPANY_BACKUP_TIME", "03:15")

# The commit the image was built from, baked into it by deploy; "-modified" when it carried changes not committed
# yet. Empty when the code does not run from such an image.
VERSION = os.environ.get("COMPANY_VERSION", "")

IMAGE = os.environ.get("COMPANY_IMAGE", "company-agent")
# Containers of runs are named with this prefix and a random part. Deploy looks for them before restarting the
# service, so every installation on a server needs its own prefix; deploy sets "<NAME>-run-".
RUN_PREFIX = os.environ.get("COMPANY_RUN_PREFIX", "company-run-")
# "docker" starts Claude Code in a container, "dummy" answers without an LLM (for tests).
RUNNER = os.environ.get("COMPANY_RUNNER", "docker")
# Docker network of the runs. Empty: the default bridge. On a server a network of its own, in which only the MCP
# services are attached. Docker's DNS then resolves their names.
NETWORK = os.environ.get("COMPANY_NETWORK", "")
# At most this much memory per run, e.g. "768m". Empty: no limit.
RUN_MEMORY = os.environ.get("COMPANY_RUN_MEMORY", "")
# "on": Claude Code in the runs sends its usual telemetry and error reports to Anthropic, as it does unchanged.
# "off": the runs get CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1. The model calls themselves go to Anthropic either way.
RUN_TELEMETRY = os.environ.get("COMPANY_RUN_TELEMETRY", "on") != "off"
# After this many minutes without a new message a conversation ends by itself.
PAUSE_MINUTES = float(os.environ.get("COMPANY_PAUSE_MINUTES", "10"))

BASE_TOOLS = "Read,Glob,Grep,Write,Edit"

# Review conversations per reviewer in the assessment.
PROBES = int(os.environ.get("COMPANY_PROBES", "3"))
# A reviewer sends at most this many messages in one review conversation. After each answer they decide whether to
# follow up. 1: every probe message stands on its own.
PROBE_TURNS = int(os.environ.get("COMPANY_PROBE_TURNS", "3"))
# Words of the two parts of a profile: the core (task, personality, limits) and the way of working (rules, methods,
# facts). An own change may not make a part longer than that, or than it already is; the employee then makes
# room. If a feedback proposal is longer, HR shortens it in one more run.
PROFILE_WORDS = int(os.environ.get("COMPANY_PROFILE_WORDS", "300"))
WORK_WORDS = int(os.environ.get("COMPANY_WORK_WORDS", "1500"))
# The feedback meeting reads at most this many of the latest transcripts since the last meeting, and as many
# assignments and review reports.
FEEDBACK_CONVERSATIONS = int(os.environ.get("COMPANY_FEEDBACK_CONVERSATIONS", "20"))
# The scheduler checks this often, in seconds.
SCHEDULER_TICK = float(os.environ.get("COMPANY_SCHEDULER_TICK", "30"))
# Access for other channels (messenger bridge, scripts) to /channel. Without a token it is switched off.
CHANNEL_TOKEN = os.environ.get("COMPANY_CHANNEL_TOKEN", "")
# A login stays valid in the browser for this many days.
LOGIN_DAYS = int(os.environ.get("COMPANY_LOGIN_DAYS", "30"))
# With "on" the browser sends the login cookie over HTTPS only. For a TLS proxy in front of the UI.
COOKIE_SECURE = os.environ.get("COMPANY_COOKIE_SECURE", "off") == "on"

# An employee may ask a colleague this many times per message of a human. 0 switches asking off.
ASKS = int(os.environ.get("COMPANY_ASKS", "3"))
# An ask waits this many seconds for a colleague who is busy.
ASK_WAIT = float(os.environ.get("COMPANY_ASK_WAIT", "120"))
# A run that takes longer is cancelled, so that the queue does not hang.
RUN_TIMEOUT = float(os.environ.get("COMPANY_RUN_TIMEOUT", "900"))
