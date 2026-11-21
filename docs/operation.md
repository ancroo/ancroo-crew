# Operation

Updating, backups, services, a second installation and uninstalling, plus all settings, the login and the endpoint for other channels. Installing is described in the [README](../README.md#install).

## Update

`git pull`, then `./deploy`. If you changed the code yourself, commit and run the tests first; `deploy` also sends uncommitted changes to tracked files. Open conversations survive the restart; runs that were cut off are marked as failed. Procedures and presets come with the code. The company itself is only ever changed by the UI, the employees and, once the data layout changes, by migrations that run on start after a backup.

- `./deploy --rollback` goes back to the image of the previous deploy (`deploys.log` in the base folder lists them). It goes back one step only: a second `--rollback` right after the first undoes it. The data stays as it is, and so does the image for the runs: Claude Code keeps the version of the latest deploy.
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

### How it fits together

Every run is a container in the Docker network of the runs, `<NAME>-agents` (`COMPANY_NETWORK`), which `deploy` creates. An MCP server runs as a container of its own on the same server and joins that network; the runs then reach it by its container name over plain HTTP, e.g. `http://caldav-mcp:8000/caldav-mcp`. It needs no published port, no reverse proxy and no HTTPS for this, because the traffic never leaves the server. Only the MCP services and the runs are in this network; the UI and other containers on the server are not.

On the page "Services" the MCP server gets a name, e.g. `calendar`. Its tools are then called `calendar__list_events` and so on, and that name is what goes into an employee's profile.

### Example: a calendar with caldav-mcp

The example assumes an installation with `NAME=company`, so the network is `company-agents`, and a Nextcloud at `cloud.example.net`. Replace both with your own.

**1. Install the MCP server.** Follow [its README](https://github.com/Stefan-Schmidbauer/caldav-mcp#deployment) up to the point where the image is on the server and the env file `.env.caldav-mcp` is filled in: CalDAV address, user, an app password, and `MCP_AUTH_TOKEN` (`openssl rand -hex 32`). `CALDAV_CALENDARS` limits which calendars it offers at all, e.g. only the work calendar for an assistant. Skip its steps about TLS and the reverse proxy; the employees do not need them.

**2. Put it into the network of the runs, in its compose file.** Change its service definition like this and add the network at the end of the file:

```yaml
  caldav-mcp:
    # ... everything else as in the snippet of its README ...
    environment:
      - MCP_TRANSPORT=streamable-http
      - MCP_HOST=0.0.0.0
      - MCP_PORT=8000
      - MCP_PATH=/caldav-mcp
      - TZ=Europe/Berlin
      # The runs call it by its container name; without this line every call is refused with 421.
      - MCP_ALLOWED_HOSTS=caldav-mcp:8000
    networks:
      - company-agents

networks:
  company-agents:
    external: true
```

Then start it with `docker compose up -d caldav-mcp`. The network exists after the first `./deploy`; before that, `up` fails with "network company-agents declared as external, but could not be found". Write the network into the compose file rather than using `docker network connect`: a connected network is lost as soon as the container is recreated, e.g. by an update of the MCP server, and the employees silently lose the service. If the published port `127.0.0.1:9153` from the snippet is not used by anything else, you can remove it.

**3. Check that the runs can reach it.** On the server, from a throwaway container in the same network:

```
docker run --rm --network company-agents curlimages/curl -s \
  http://caldav-mcp:8000/caldav-mcp -H 'Authorization: Bearer <MCP_AUTH_TOKEN>'
```

`Bad Request: Missing session ID` is the right answer: network, path, token and host check are in order. `Invalid Host header` means `MCP_ALLOWED_HOSTS` is wrong, `unauthorized` the token, `Could not resolve host` the network.

**4. Add it on the page "Services".**

| Field | Value |
|---|---|
| Name | `calendar` (letters, digits, `-` and `_`; no `__`) |
| Type | `http` (streamable HTTP; `sse` only for older servers that speak nothing else) |
| Address | `http://caldav-mcp:8000/caldav-mcp` |
| Description | e.g. "Calendar of the company in Nextcloud: read, enter, move and delete appointments". HR reads it in the hiring interview to decide who needs the service. |
| Tools blocked for everyone | e.g. `delete_event`, if no employee should ever delete an appointment |
| Header for the access | `Authorization` |
| Value of the header | `Bearer <MCP_AUTH_TOKEN>`, the word `Bearer`, a space and the token |

The header value is stored with mode 600 and never shown again; to change the service later, leave it empty and the stored one stays.

**5. Give it to an employee.** On their card, "Change key facts", field "Services": `calendar` allows all its tools. To allow only some, list them instead, e.g. `calendar__list_events, calendar__search_events` for someone who may only read. If the employee later needs a tool they do not have, an approval request appears in the chat and on their card; "Approve" adds it.

**6. Try it.** Ask the employee in the chat "What is on my calendar tomorrow?". The tool log above the answer should list `mcp__calendar__list_events`. The sandbox view (switch it on in "Settings") shows which services a run has and which tools are blocked.

### When it does not work

| What you see | Cause and fix |
|---|---|
| The employee says they have no calendar, and the tool log shows no call to it | The service is not in the field "Services" on their card, or the run cannot connect to it (network, host check, token or path): check with step 3; `docker logs caldav-mcp` has the details. |
| A conversation does not start: "There is no service “calendar”." | The name on the card differs from the name on the page "Services". |
| It worked until the MCP server was updated | The container was recreated and lost a network that had been connected by hand; put the network into its compose file (step 2). |
| `Cannot connect to the CalDAV server` | The MCP server reaches the service but not the CalDAV server: address, user or app password in its env file. |

Other MCP servers are set up the same way: container in the network of the runs, the container name as address, the access header if the server has one. Whatever a service returns, the employee reads as text, including appointments or notes written by others; see [Security](security.md) for what that means and why blocking dangerous tools is worth it.

## Backups

Every day the service writes a backup into `<BASE>/backups` and keeps it for `COMPANY_BACKUP_DAYS` days: `company.db` (SQLite online backup), `company.bundle` (the company's Git history; restore it with `git clone company.bundle company`) and the secrets. Copying it off the server is up to you, e.g. with a daily `rsync` of that folder. The backups are not encrypted and contain the Claude token and the access headers of the services in plain text (readable only by the service's user on the server, mode 600), so store the copy encrypted or somewhere only you can read. Neither the company folder nor the database is encrypted on the server either; disk encryption is up to the server.

## A second installation

`NAME`, `BASE` and `PORT` tell installations apart. A second one on the same server, e.g. for trying things out, needs different values for all three and its own settings file: `COMPANY_ENV=other.env ./deploy`. The image, the containers and the network of the runs are named after `NAME` (`<NAME>-agent`, `<NAME>-run-*`, `<NAME>-agents`). So a run of one installation does not block the deploy of the other, and its employees cannot reach the other's services. If you set `COMPANY_IMAGE` or `COMPANY_NETWORK` in a settings file, give them different values in each.

## Uninstall

There is no `deploy` option for this; it is a few commands on the server, run as the SSH user. The examples use the defaults `NAME=company` and `BASE=/opt/company`; replace them with your values.

1. **Keep what you want to keep.** Copy `/opt/company/backups` off the server, or the whole folder `company/`, which is a Git repository with the full history of your crew. Everything below deletes it for good.
2. **Stop and remove the service:** `docker compose -f /opt/company/compose.yml down`. Run containers remove themselves when they end.
3. **Remove the network of the runs:** first detach your MCP services (`docker network disconnect company-agents my-mcp`), then `docker network rm company-agents`. `docker network inspect company-agents --format '{{range .Containers}}{{.Name}} {{end}}'` lists what is attached.
4. **Remove the images:** `docker images --format '{{.Repository}}:{{.Tag}}' | grep -E '^company(-agent)?:' | xargs -r docker image rm`. The pattern matches only this installation, not e.g. `company2`.
5. **Remove the data:** `rm -rf /opt/company`. Under `/opt` removing the folder itself needs root; its contents belong to the SSH user.

Finally, delete `company.env` in your checkout; it holds the server address of this installation.

**Starting over** with an empty company is uninstalling plus [installing](../README.md#install) again, keeping `company.env`. The MCP services can stay: after the new `./deploy`, attach them to the network of the runs again and add them on the page "Services" again (see [Services for the employees](#services-for-the-employees), steps 2 and 3), because the old entries were deleted with the data.

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
| `COMPANY_PROBES` | `5` | At most this many review conversations per reviewer in an assessment; the reviewer may open fewer |
| `COMPANY_PROBE_TURNS` | `3` | Messages a reviewer can send per review conversation |
| `COMPANY_PROFILE_WORDS` | `300` | Maximum words in the core of a profile; an own change may not exceed it |
| `COMPANY_WORK_WORDS` | `1500` | Maximum words in the way of working; beyond it, the employee merges before adding |
| `COMPANY_PLAYBOOK_WORDS` | `1500` | Maximum words in one special instruction (`playbooks/<name>.md`) |
| `COMPANY_FEEDBACK_CONVERSATIONS` | `20` | Latest transcripts, assignments and review reports the feedback meeting reads |
| `COMPANY_ASKS` | `3` | Asks between colleagues per human message; `0` switches them off |
| `COMPANY_ASK_WAIT` | `120` | Seconds an ask waits for a busy colleague |
| `COMPANY_SCHEDULER_TICK` | `30` | Seconds between two checks of the scheduler |
| `COMPANY_CHANNEL_TOKEN` | empty | Token for `/channel`; without it the endpoint is off |
| `COMPANY_LOGIN_DAYS` | `30` | Days a login stays valid |

`deploy` sets `COMPANY_DIR`, `COMPANY_DB`, `COMPANY_BACKUPS` and `COMPANY_RUN_PREFIX` itself from `BASE` and `NAME`, and also `COMPANY_NETWORK` and `COMPANY_IMAGE` unless the settings file sets them. The address and port of the UI inside its container are fixed by the image, and so is `COMPANY_VERSION`, the commit `deploy` built it from, shown on the page "About".

## On the phone

The chat with an employee can sit on the phone's home screen as an app of its own, with the employee's name under the icon, and messages can be dictated instead of typed (see [Features](features.md#working-with-employees)).

**What it needs:** the phone reaches the UI over HTTPS. An SSH tunnel does not work here, and without HTTPS the browser offers neither the app nor the microphone. The simplest way is Tailscale: `tailscale serve` on the server (see [Reaching the UI](../README.md#reaching-the-ui)), and the Tailscale app on the phone, signed in to the same tailnet and switched on.

**Android (Chrome):**

1. Open the HTTPS address of the UI in Chrome, e.g. `https://server.your-tailnet.ts.net:8443`, and log in.
2. Open the card of the employee and from there the chat.
3. Tap the menu ⋮ at the top right, then the entry for installing the page (in a German Chrome "Installieren und Verknüpfung erstellen"; older versions say "Add to home screen"). In the dialog that follows, tap "Install" ("Installieren"): only then does the chat open without the browser bar.
4. The icon is now on the home screen; it opens this chat directly. Repeat steps 2 and 3 for every employee you want there.
5. Tap the microphone next to "Send" the first time and allow Chrome to use the microphone. Speak, pause, and the text is in the field; correct it if needed and send. If you denied access by mistake: Chrome menu ⋮ → Settings → Site settings → Microphone, and allow the address there.

**iPhone (Safari):**

1. Open the HTTPS address of the UI in Safari and log in.
2. Open the card of the employee and from there the chat.
3. Tap the share button (square with an arrow), then "Add to Home Screen", keep or change the name, and tap "Add".
4. Open the new icon and log in once more: on the iPhone, an app from the home screen does not share the login with Safari.
5. Tap the microphone next to "Send" and allow access. If the button does not appear or does not react, use the microphone key of the iPhone keyboard; it writes into the same field.

The app is the same page as in the browser: it needs the connection (on with Tailscale), and it shows the login page again once the login has ended. The audio of the microphone button goes to the maker of the browser, Google or Apple (see [Security](security.md#what-leaves-your-server)).

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
