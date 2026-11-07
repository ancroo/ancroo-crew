# Operation

Updating, backups, services, a second installation and uninstalling, plus all settings, the login and the endpoint for other channels. Installing is described in the [README](../README.md#install).

## Update

`git pull`, then `./deploy`. If you changed the code yourself, commit and run the tests first; `deploy` also sends uncommitted changes to tracked files. Open conversations survive the restart; runs that were cut off are marked as failed. Procedures and presets come with the code. The company itself is only ever changed by the UI, the employees and, once the data layout changes, by migrations that run on start after a backup.

- `./deploy --rollback` goes back to the image of the previous deploy (`deploys.log` in the base folder lists them). It goes back one step only: a second `--rollback` right after the first undoes it. The data stays as it is.
- `./deploy --reset-password` asks for a new owner password and ends all their logins. The UI cannot do this, because you are locked out.
- `./deploy --force` deploys even while an employee is running.

### What deploy does

1. It reads `company.env` (or the file named in `COMPANY_ENV`) and checks SSH and Docker on the server.
2. It refuses while an employee is running (containers `<NAME>-run-*`), unless `--force` is given. Restarting the service would leave that run orphaned, possibly with write access to the company.
3. It refuses if the base folder holds anything other than an installation of this code (`company/` with `.schema`, `data/`, `backups/`, `compose.yml`, `deploys.log`), or if the port is taken by another container. Nothing is changed in that case.
4. It sends the code over SSH with `git archive` (including uncommitted changes to tracked files) and builds the image `<NAME>:<commit>-<time>` on the server, also tagged `<NAME>:local`.
5. It builds the image for the runs, `<NAME>-agent`, from `agent/Dockerfile` without cache, so that every deploy brings the latest Claude Code, and prints its version.
6. It writes `compose.yml` into the base folder, creates the Docker network for the runs if needed, starts the container and waits until the UI answers. If it does not, it shows the last lines of the log.
7. Once the UI answers, it removes the images of earlier deploys listed in `deploys.log`. It keeps the last three service images for `--rollback`, and only the current image for the runs.

On the first start the service finds the empty folder `company/` and creates the company (`git init`, `.schema`). On every later start it checks that the company and the database have the schema version of the code; if not, it does not start. A company is never created on top of something else.

## Services for the employees

Without services, employees work only with the files of the company and, if allowed, the web. With services they work on real data: any MCP server reachable over HTTP (streamable HTTP or SSE) will do, for example:

- [caldav-mcp](https://github.com/Stefan-Schmidbauer/caldav-mcp): calendars on CalDAV servers such as Nextcloud
- [minitask-mcp](https://github.com/Stefan-Schmidbauer/minitask-mcp): task lists on CalDAV servers
- [trilium-notecast-mcp](https://github.com/Stefan-Schmidbauer/trilium-notecast-mcp): notes and slides in Trilium

The employees' memory is their profile; everything else they need again, such as notes or tasks, belongs in a service.

1. Run the MCP server as a container of its own on the same server, set up as its own README says.
2. Attach it to the network of the runs, `COMPANY_NETWORK` (default `<NAME>-agents`), e.g. with `docker network connect company-agents my-mcp`. Only the MCP services and the runs are in this network.
3. On the page "Services", add it with its container name as the address, e.g. `http://my-mcp:8000/mcp`, plus the access header if it needs one. Tools nobody should use can be blocked there for everyone.
4. Give an employee the service on their card ("Change key facts"). A service allows all its tools; `service__tool` allows only that one.

## Backups

Every day the service writes a backup into `<BASE>/backups` and keeps it for `COMPANY_BACKUP_DAYS` days: `company.db` (SQLite online backup), `company.bundle` (the company's Git history; restore it with `git clone company.bundle company`) and the secrets. Copying it off the server is up to you, e.g. with a daily `rsync` of that folder. The backups are not encrypted and contain the Claude token and the access headers of the services in plain text (readable only by the service's user on the server, mode 600), so store the copy encrypted or somewhere only you can read. Neither the company folder nor the database is encrypted on the server either; disk encryption is up to the server.

## A second installation

`NAME`, `BASE` and `PORT` tell installations apart. A second one on the same server, e.g. for trying things out, needs different values for all three and its own settings file: `COMPANY_ENV=other.env ./deploy`. The image, the containers and the network of the runs are named after `NAME` (`<NAME>-agent`, `<NAME>-run-*`, `<NAME>-agents`). So a run of one installation does not block the deploy of the other, and its employees cannot reach the other's services. If you set `COMPANY_IMAGE` or `COMPANY_NETWORK` in a settings file, give them different values in each.

## Uninstall

There is no `deploy` option for this; it is a few commands on the server, run as the SSH user. The examples use the defaults `NAME=company` and `BASE=/opt/company`; replace them with your values.

1. **Keep what you want to keep.** Copy `/opt/company/backups` off the server, or the whole folder `company/`, which is a Git repository with the full history of your crew. Everything below deletes it for good.
2. **Stop and remove the service:** `docker compose -f /opt/company/compose.yml down`. Run containers remove themselves when they end.
3. **Remove the network of the runs:** first detach your MCP services (`docker network disconnect company-agents my-mcp`), then `docker network rm company-agents`.
4. **Remove the images:** `docker images --format '{{.Repository}}:{{.Tag}}' | grep -E '^company(-agent)?:' | xargs -r docker image rm`. The pattern matches only this installation, not e.g. `company2`.
5. **Remove the data:** `rm -rf /opt/company`. Under `/opt` removing the folder itself needs root; its contents belong to the SSH user.

Finally, delete `company.env` in your checkout; it holds the server address of this installation.

## Settings

Environment variables, all optional. On a server they come from `company.env`.

| Variable | Default | Meaning |
|---|---|---|
| `COMPANY_DIR` | `./company` | Folder of the company |
| `COMPANY_DB` | `./data/company.db` | SQLite file |
| `COMPANY_BACKUPS` | `./backups` | Folder for backups |
| `COMPANY_BACKUP_TIME` | `03:15` | Daily backup at this local time; empty switches it off |
| `COMPANY_BACKUP_DAYS` | `7` | Days a backup is kept |
| `COMPANY_RUNNER` | `docker` | `docker`, or `dummy` for tests |
| `COMPANY_IMAGE` | `company-agent` | Docker image with Claude Code for the runs |
| `COMPANY_RUN_PREFIX` | `company-run-` | Name prefix of the run containers, followed by a random part |
| `COMPANY_NETWORK` | empty | Docker network of the runs, which holds only the MCP services |
| `COMPANY_RUN_MEMORY` | empty | Memory limit per run, e.g. `768m`, without swap |
| `COMPANY_RUN_TELEMETRY` | `on` | With `off`, Claude Code in the runs sends no telemetry and error reports (`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`) |
| `COMPANY_COOKIE_SECURE` | `off` | With `on`, the login cookie is sent over HTTPS only |
| `COMPANY_PAUSE_MINUTES` | `10` | Countdown until a conversation ends |
| `COMPANY_RUN_TIMEOUT` | `900` | Seconds after which a run is cancelled |
| `COMPANY_PROBES` | `3` | Review conversations per reviewer in an assessment |
| `COMPANY_PROBE_TURNS` | `3` | Messages a reviewer can send per review conversation |
| `COMPANY_PROFILE_WORDS` | `300` | Maximum words in the core of a profile; an own change may not exceed it |
| `COMPANY_WORK_WORDS` | `1500` | Maximum words in the way of working; beyond it, the employee merges before adding |
| `COMPANY_FEEDBACK_CONVERSATIONS` | `20` | Latest transcripts, assignments and review reports the feedback meeting reads |
| `COMPANY_ASKS` | `3` | Asks between colleagues per human message; `0` switches them off |
| `COMPANY_ASK_WAIT` | `120` | Seconds an ask waits for a busy colleague |
| `COMPANY_SCHEDULER_TICK` | `30` | Seconds between two checks of the scheduler |
| `COMPANY_CHANNEL_TOKEN` | empty | Token for `/channel`; without it the endpoint is off |
| `COMPANY_LOGIN_DAYS` | `30` | Days a login stays valid |

`deploy` sets `COMPANY_DIR`, `COMPANY_DB`, `COMPANY_BACKUPS` and `COMPANY_RUN_PREFIX` itself from `BASE` and `NAME`, and also `COMPANY_NETWORK` and `COMPANY_IMAGE` unless the settings file sets them. The address and port of the UI inside its container are fixed by the image.

## Login

- Every human logs in with their name or display name and a password. Before the setup, only the setup page is reachable; afterwards, only the login page.
- Passwords are stored as scrypt hashes; of the login cookie, only a SHA-256 hash is stored.
- A forgotten password is reset with `./deploy --reset-password`.
- The UI accepts requests that change something (everything except `GET`) only from its own pages. Otherwise any web page open in the same browser could approve tools or dismiss employees with your cookie. The check uses the header `Sec-Fetch-Site`, and on older browsers compares `Origin` with `Host`. A proxy in front must pass `Host` through unchanged. Scripts that send neither header, such as `curl`, are not affected.
- After login, the UI redirects only to its own pages, never to an outside address from `?next=`.
- Further humans with rights of their own are not built yet; the data model leaves room for them.

## Access for other channels

With `COMPANY_CHANNEL_TOKEN` set, there is an endpoint for a messenger bridge or a script. It checks only the token, no login:

```
curl -X POST http://127.0.0.1:8700/channel/0002/message \
  -H "Authorization: Bearer $COMPANY_CHANNEL_TOKEN" -H "Content-Type: application/json" \
  -d '{"text": "What is on today?", "channel": "telegram"}'
```

The response comes as soon as the employee is done: `{"conversation": 12, "id": 345, "answers": [{"sender": "0002", "text": "…"}], "notes": []}`, where `id` is the message sent. With `"wait": false` the call returns at once with only `conversation` and `id`. `"person"` is the ID of the human on whose behalf the message is sent, by default the owner. The message lands in that human's ongoing conversation with the employee and can be followed in the web chat. Anyone who has the token can write as any human of the company.
