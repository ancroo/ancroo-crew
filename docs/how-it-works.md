# How it works

The layers of the code, how Claude Code is installed and used for the runs, the files of the company, and what lives where.

| Layer | Job | Code |
|---|---|---|
| Channels | Accept and show messages: the web chat, and an endpoint for other channels under `/channel`. | `app/main.py`, `app/templates`, `app/static` |
| Middle | Conversations, queue, countdown, transcripts, approvals, profiles and their own changes, hiring, feedback, scheduler, notices, backups, login | `app/conversations.py`, `app/personnel.py`, `app/scheduler.py`, `app/notices.py`, `app/backup.py`, `app/auth.py`, `app/company.py`, `app/db.py` |
| Outside | LLM and MCP services | `app/runner.py` |

A new channel only calls `send`, `bring_in`, `stop`, `pause`, `resume`, `end`, `reopen` and `subscribe` in `app/conversations.py`. Another LLM is another runner with the method `run` in `app/runner.py`.

## Claude Code in the runs

Ancroo Crew does not talk to the Claude API itself. Every answer of an employee is one run of Anthropic's [Claude Code](https://code.claude.com) CLI, unmodified, in a Docker container of its own.

**The image.** `deploy` builds the image `<NAME>-agent` on the server from [`agent/Dockerfile`](../agent/Dockerfile): `node:26-slim` with `git`, `ripgrep` and `npm install -g @anthropic-ai/claude-code`. The version is not pinned, and the image is built without cache, so every deploy installs the latest Claude Code and prints its version. That also means a deploy can bring a new version of Claude Code even if this code did not change. The auto-updater inside the image is switched off (`DISABLE_AUTOUPDATER=1`), so the version stays fixed until the next deploy. You do not install Claude Code anywhere yourself; the service starts the containers through the Docker socket of the server.

**A run.** For each answer the service starts `docker run --rm` with the image and runs in it, roughly:

```
claude -p --session-id <id> | --resume <id>      # one Claude Code session per conversation
       --output-format stream-json --verbose --include-partial-messages
       --system-prompt-file /prompt.md            # built from profile, handbook, team, humans and procedure
       --add-dir /company --tools Read,Glob,Grep,Write,Edit[,WebSearch,WebFetch]
       --allowedTools ... --disallowedTools 'Read(//proc/**)' ...
       [--mcp-config /mcp.json --strict-mcp-config] [--model haiku|sonnet|opus] [--json-schema ...]
```

The message goes in on standard input; the service reads the stream of events, shows the answer while it is being written and books the tokens under "Usage". The exact command is assembled in `DockerRunner.command` in `app/runner.py`; `test_docker_command` checks it.

**The credential.** The token or API key from the page "Services" is stored in `company/.env` (mode 600, not in Git) and passed to the container as an environment variable, `CLAUDE_CODE_OAUTH_TOKEN` for a subscription token or `ANTHROPIC_API_KEY` for an API key. To get a subscription token you run `claude setup-token` once, wherever you have Claude Code; with an API key from the [Claude Console](https://console.anthropic.com) you need no Claude Code at all.

**State between runs.** The container is removed after each run. Claude Code's own home folder, with its session files and settings, is `runtime/home` on the server, mounted as `/home/agent` into every run, so that `--resume` continues a conversation. The service marks Claude Code's first-start dialogs as done there. Employees cannot read or edit `/home/agent` with their tools (see [Security](security.md#what-is-blocked-technically)).

**What goes to Anthropic.** Claude Code sends the prompt and everything the employee reads during the run to Anthropic, and by default its usual telemetry and error reports; `COMPANY_RUN_TELEMETRY=off` switches the latter off (see [Security](security.md#what-leaves-your-server)).

## What is where

The company is a folder and a Git repository of its own. It never belongs in this repository.

```
company/                       Git repo of the company; every change is committed
├── company.md                 name and language of the company
├── logo.png                   logo of the company, optional (or .jpg, .webp)
├── handbook.md                applies to everyone, part of every prompt
├── .schema                    version of the data layout
├── people/0001/profile.md     a human: name, display name, roles, language, text about them
├── employees/0002/
│   ├── profile.md             header (name, title, roles, model, tools, services, self_edit, hired) and the core:
│   │                          task, personality, limits
│   ├── work.md                the way of working: rules, methods, facts
│   ├── agreement.md           what the human agreed to in the hiring interview: purpose and success
│   ├── journal/               word-for-word transcripts of their conversations
│   ├── assignments/           what they did in colleagues' conversations
│   ├── assessment/  feedback/
│   └── edits.md               what humans changed in their files
├── candidates/0007/           drafts from the hiring interview
├── archive/0005-2026-10-12/   dismissed employees and discarded drafts
├── .env  mcp.json             Claude token and MCP services, mode 600, not in Git
└── runtime/                   working directory of Claude Code, not in Git
```

- **IDs and names.** Everyone, human or employee, has an ID that never changes and is never reused. It names the folder and appears in URLs. The name is what people and agents see; it can be changed on the card. Names are unique within the company, because the team addresses each other by name.
- **Roles** in the profile header decide who does which job:

  | Role | Holders | Does |
  |---|---|---|
  | `owner` | one human | the person the company works for |
  | `hr` | any number | hiring interviews and feedback meetings. The feedback of an HR employee is led by another HR employee. |
  | `reviewer` | any number | reviews candidates and employees in the assessment |
  | `archivist` | at most one | reads everyone's transcripts and answers when a human refers to something earlier |

  If a role is missing, the feature it drives is unavailable and the team page says so. Nothing else breaks.
- The **database** (`data/company.db`, SQLite) holds the operational data: conversations, messages, runs, jobs, usage, approval requests, schedules, notices, passwords and logins. What defines someone is in files; the database is expendable history.
- The **procedures** (`app/procedures/*.md`) are part of the code and come with every update. Each run copies them to `runtime/procedures` and mounts them. They are in English only; every run is told which language to write in.
- The **presets** (`app/presets/<lang>/`) are the handbook and profiles for typical roles, in German and English. They are copied into the company once and belong to it from then on.
- Git commits use `git add -A`: at the end of a conversation or job, everything pending in the company goes into the commit.
