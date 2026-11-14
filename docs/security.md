# Security

What leaves your server, the two big risks, and which limits are enforced technically and which only by rule. To report a vulnerability, see [SECURITY.md](../SECURITY.md).


## What leaves your server

Self-hosted means that the UI, the company and the database live on your server. It does not mean that nothing leaves it: every run sends its prompt to Anthropic, and that includes the profile, the handbook, the texts about the humans, the conversation, and whatever the employee reads during the run, from files of the company to results of MCP services. Web searches run on Anthropic's servers; `WebFetch` fetches pages from your server's IP address. By default Claude Code also sends its usual telemetry and error reports to Anthropic, as it does anywhere else; `COMPANY_RUN_TELEMETRY=off` switches them off. The model calls themselves cannot be switched off, they are the work. What Anthropic does with all this is governed by the terms of your credential (see [Billing](billing.md)). MCP services talk to whatever they are connected to, e.g. your CalDAV server. Beyond that, the service sends nothing anywhere; the pages load fonts and scripts only from the UI itself.

## The two big risks

The employees read texts that do not come from the humans of the company: web pages, search results, calendar entries, tasks and notes from services. Such a text can contain an instruction that an employee mistakes for a task (prompt injection). No model is safe against that. The protection therefore lies mainly in limiting what an employee can do at all.

The service itself starts the runs through the server's Docker socket, and access to that socket is as powerful as root: whoever takes over the service takes over the server. That is why the UI listens on `127.0.0.1` only and every page needs a login.

## Where the web is read

- **`WebSearch`** runs on Anthropic's servers and returns titles and addresses, no page contents.
- **`WebFetch`** fetches the page from the container, with the server's IP address. The content then goes into a separate model call that extracts only what was asked for, and the employee gets this extract, not the raw page. That filters some things out, but it is no reliable protection.

`WebFetch` is also a way for data to leave the company, e.g. as part of a fetched address. The other way would be a human's browser: an answer can contain a Markdown image whose address carries data. The pages therefore load nothing from outside addresses.

## What is blocked technically

| Block | Where | Protects |
|---|---|---|
| The company is mounted read-only. Only what the task names is writable. | mounts in `app/runner.py` | profiles, handbook, procedures, colleagues' folders |
| In a conversation the employee's own folder is writable, but everything in it except `profile.md` and `work.md` is mounted read-only on top (`Task.protected`). After the run, the service removes anything new in the folder, restores the profile header, and replaces a link or folder put in place of either part of the profile. | `protected_in_own`, `take_own_change` in `app/company.py`, `_own_change` in `app/conversations.py` | tools, services and roles in the header, the agreement, the edit log, feedback and assessment; the service never reads or writes through a link the employee created |
| Whoever has `WebSearch` or `WebFetch` gets only their own folder mounted, plus handbook and procedures. As a reviewer, the folder of the employee under review is added. | mounts in `app/runner.py` | colleagues' transcripts and profiles, against an instruction from the web |
| When asked, a colleague with web access gets only the message with the question, not the conversation. | `_ask` in `app/conversations.py` | whatever else was in the conversation, e.g. appointments from the calendar |
| In conversations, and for colleagues brought in or asked, an empty read-only `tmpfs` covers every `journal` and `assignments` folder, except for the archivist. | `Task.hidden`, `--tmpfs` in `app/runner.py`, `hidden` in `app/conversations.py` | the history, so that employees work with today's state; a `Grep` over the whole company finds nothing there either |
| Whoever reads the web cannot ask the archivist. | `_ask` in `app/conversations.py` | everyone's history, against an instruction from the web that could otherwise query it through the archivist and send it out by fetching an address |
| Once the archivist has answered in a conversation (asked or brought in), or the conversation is with the archivist, nobody in it can ask a colleague who reads the web, also not in later messages or after "Continue conversation". | `_ask`, `_knows_history` in `app/conversations.py` | the same history against the detour: an instruction in a researcher's answer makes an employee without web access ask the archivist and then hand the result to the researcher inside an address to fetch |
| `Read(//proc/**)` | `--disallowedTools` | the Claude token in the container's environment |
| `Read(//home/agent/**)`, `Edit(//home/agent/**)` | `--disallowedTools` | the conversation histories of all employees and the settings of Claude Code |
| `Read(//mcp.json)` | `--disallowedTools` | the access tokens of the services |
| Tools listed under `blocked` for a service | `--disallowedTools` | actions nobody should carry out |
| The runs are in the Docker network `COMPANY_NETWORK`, which holds only the MCP services. | `--network` in `app/runner.py` | other services on the server and the UI itself, against an instruction that sends an employee there |
| A memory limit per run | `--memory`, `COMPANY_RUN_MEMORY` | the other services on the server, against a run that uses up all memory |
| No `Bash` among the base tools | `BASE_TOOLS` in `app/config.py` | everything that is reachable only through a shell command |
| Only `WebSearch` and `WebFetch` are allowed in the `tools` field of a profile. If anything else is there, the run does not start. | `EXTRA_TOOLS` in `app/company.py`, checked in `set_header` and `DockerRunner.command` | profiles are also written by an LLM, e.g. by HR in the hiring interview. A `Bash` there would get around the blocks on `/proc` and `/home/agent`, which `--disallowedTools` applies only to `Read` and `Edit` |
| Candidates have no roles. Whatever a draft says in `roles` is cleared when it is created and when the candidate is hired; only a human gives roles, on the card. | `prepare_candidates` and `hire` in `app/company.py` | the history and the HR jobs, against a draft in which HR (or an instruction that reached HR) gave the new colleague the role archivist or HR |
| Answers are rendered as Markdown without raw HTML. Every page has a Content Security Policy: images, scripts and connections only from the UI itself, no frames. | `MarkdownIt("js-default")`, `POLICY` in `app/main.py` | the data in an answer, against an image address that the browser would fetch without a click |
| The setup creates a company only in an empty folder, and the service starts only with a company of a known schema version. | `ensure` in `app/company.py` | existing data, against a new version of the code or a wrong path in the settings |

The sandbox view (page "Sandbox", off by default) shows every logged-in human how the runs are limited: paths inside the container, tool lists, blocked rules, service names and the tool calls with their targets (file, search, address). It shows no host paths, no tokens and no conversation texts, and keeps nothing on disk. Its description is read from the command that started the run, so it shows what was enforced, not what was meant. Still, it tells whoever sees it where the limits are; switch it on for a demo or a check, not permanently.

The rules in `--disallowedTools` apply to the employees' tools; Claude Code itself keeps reading and writing in `/home/agent`. You can check the block in the chat by asking an employee to read `/proc/version`: the call appears in the tool log, but the employee does not get the content.

## What applies only by rule

- Only what a human says in the conversation, and what the employee observes there, goes into their own profile, never anything from outside texts. That rule is only in the handbook: technically, an employee can change the text of their profile in a conversation, and it is in their prompt from the next answer on. An injected instruction that lands there stays in effect until a human undoes it. Every change is shown in the chat with its diff and is in Git; for an employee who reads the web, "own changes apply at once" can be switched off on the card.
- An employee gives colleagues no tasks. Technically, an asked colleague has no write access and no services anyway.
- Employees without web access can read the whole company, including their colleagues' profiles. Transcripts and assignments are hidden in conversations and review conversations, but not in evaluations and reviews. The texts about the humans are in everyone's prompt, including those with web access.
- Whoever brings a colleague with web access into a conversation decides that this colleague reads the conversation so far.
- A link in an answer leads wherever it says. Its address can carry data too, so look at it before you click.

## Who gets which model and tools

The principle: whoever reads outside texts and can change something at the same time gets the strongest model and as little web access as possible. An assistant with write access to calendars or task lists gets no web access of their own and asks the researcher instead. The reviewer needs the web to check what an employee claims, but cannot change anything. The presets follow this principle.
