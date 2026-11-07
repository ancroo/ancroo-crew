"""deploy against a pretend server: ssh runs the command locally under sh, docker only records what it is asked."""
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

FAKE_SSH = """#!/bin/sh
# Skip the options and the host, then run the command like a remote login shell would.
while [ "$#" -gt 1 ]; do case "$1" in -o) shift 2 ;; *) shift; break ;; esac; done
exec sh -c "$1"
"""

FAKE_DOCKER = """#!/bin/sh
log="$FAKE_STATE/docker.log"
printf '%s\\n' "$*" >> "$log"
case "$1" in
  version) echo 29.0 ;;
  ps) case "$*" in *-run-*) f=running ;; *) f=ports ;; esac; [ -f "$FAKE_STATE/$f" ] && cat "$FAKE_STATE/$f"; true ;;
  inspect) [ -f "$FAKE_STATE/container" ] ;;
  build) cat > /dev/null; echo sha256:1 ;;
  run) echo "2.0.0 (Claude Code)" ;;
  network) [ "$2" = create ] || [ -f "$FAKE_STATE/network" ] ;;
  compose) touch "$FAKE_STATE/container" ;;
  exec) case "$*" in *reset-password*) cat > "$FAKE_STATE/password" ;; esac; true ;;
  image) [ "$2" = inspect ] || exit 0
         for a; do case "$a" in company-agent:latest|company-agent:t4) echo sha256:old ;; esac; done ;;
  tag|logs) true ;;
esac
"""

FAKE_STAT = """#!/bin/sh
case "$*" in *docker.sock*) echo 989 ;; *) exec /usr/bin/stat "$@" ;; esac
"""


@pytest.fixture
def pretend(tmp_path):
    """A copy of the repository, company.env and the fake tools."""
    repo = tmp_path / "repo"
    # The files of the working tree that Git knows or would take, in a repository of their own.
    listed = subprocess.run(["git", "-C", str(REPO), "ls-files", "-co", "--exclude-standard"], capture_output=True,
                            text=True, check=True).stdout.splitlines()
    for name in listed:
        if (REPO / name).is_file() and not (REPO / name).is_symlink():
            (repo / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / name, repo / name)
    git = ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@localhost"]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(git + ["add", "-A"], check=True)
    subprocess.run(git + ["commit", "-q", "-m", "state"], check=True)
    bin_dir, state = tmp_path / "bin", tmp_path / "state"
    bin_dir.mkdir()
    state.mkdir()
    for name, text in (("ssh", FAKE_SSH), ("docker", FAKE_DOCKER), ("stat", FAKE_STAT)):
        (bin_dir / name).write_text(text)
        (bin_dir / name).chmod(0o755)
    base = tmp_path / "server" / "opt company"
    (repo / "company.env").write_text(f"SERVER=admin@server.example\nBASE={base}\nNAME=company2\nPORT=8701\n"
                                      "TZ=Europe/Berlin\nCOMPANY_NETWORK=agents2\nCOMPANY_RUN_MEMORY=768m\n"
                                      "COMPANY_CHANNEL_TOKEN=it's secret\n")
    env = os.environ | {"PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_STATE": str(state)}

    def deploy(*args, stdin=None):
        return subprocess.run([str(repo / "deploy"), *args], cwd=repo, env=env, input=stdin, capture_output=True,
                              text=True)
    return deploy, base, state, repo


def test_first_deploy_installs(pretend):
    deploy, base, state, _ = pretend
    result = deploy()
    assert result.returncode == 0, result.stderr
    assert "Running: company2" in result.stdout and "ssh -L 8701:127.0.0.1:8701" in result.stdout
    assert sorted(p.name for p in base.iterdir()) == ["backups", "company", "compose.yml", "data", "deploys.log"]
    assert not any((base / "company").iterdir())  # the service creates the company itself
    compose = (base / "compose.yml").read_text()
    for part in ("name: company2", "container_name: company2", f"user: '{os.getuid()}:{os.getgid()}'", "- '989'",
                 "- '127.0.0.1:8701:8700'", f"- '{base}:{base}'", f"COMPANY_DIR: '{base}/company'",
                 f"COMPANY_DB: '{base}/data/company.db'", "COMPANY_NETWORK: 'agents2'", "TZ: 'Europe/Berlin'",
                 "COMPANY_RUN_MEMORY: '768m'", "COMPANY_CHANNEL_TOKEN: 'it''s secret'", "read_only: true",
                 "COMPANY_IMAGE: 'company2-agent'", "COMPANY_RUN_PREFIX: 'company2-run-'"):
        assert part in compose, part
    assert "SERVER" not in compose and "admin@" not in compose
    calls = (state / "docker.log").read_text()
    assert "build -q -t company2:" in calls and "-t company2:local -" in calls
    assert "--pull --no-cache -f agent/Dockerfile -t company2-agent:" in calls and "-t company2-agent:latest -" in calls
    assert "network create agents2" in calls and "compose -f" in calls and "up -d --remove-orphans" in calls
    assert "ps --filter name=company2-run- --format {{.Names}} ({{.RunningFor}})" in calls
    assert "image rm" not in calls and "Removing images" not in result.stdout  # nothing earlier to remove
    log = (base / "deploys.log").read_text().splitlines()
    assert len(log) == 1 and log[0].split("\t")[2] == "deploy"


def test_deploy_refuses_a_foreign_folder(pretend):
    deploy, base, state, _ = pretend
    # Like the old installation: something that is not this code.
    (base / "firma").mkdir(parents=True)
    (base / "firma" / "akte.md").write_text("old data")
    result = deploy()
    assert result.returncode != 0 and "not an installation of this code" in result.stderr
    assert sorted(p.name for p in base.iterdir()) == ["firma"]
    assert "build" not in (state / "docker.log").read_text()
    shutil.rmtree(base / "firma")
    # A company folder without schema is foreign as well.
    (base / "company").mkdir()
    (base / "company" / "notes.md").write_text("x")
    result = deploy()
    assert result.returncode != 0 and f"{base}/company" in result.stderr
    (base / "company" / ".schema").write_text("1\n")
    assert deploy().returncode == 0


def test_deploy_waits_for_running_employees_and_free_ports(pretend):
    deploy, base, state, _ = pretend
    # A run of another installation on the same server, whose name the filter of Docker matches as well.
    (state / "running").write_text("my-company2-run-xyz (1 minute)\n")
    assert deploy().returncode == 0
    (state / "running").write_text("company2-run-abc (2 minutes)\n")
    result = deploy()
    assert result.returncode != 0 and "An employee is working right now" in result.stderr
    assert deploy("--force").returncode == 0
    (state / "running").unlink()
    (state / "container").unlink()
    (state / "ports").write_text("ancroo 127.0.0.1:8701->8700/tcp\n")
    result = deploy()
    assert result.returncode != 0 and "Port 8701" in result.stderr


def test_deploy_removes_images_of_earlier_deploys(pretend):
    deploy, base, state, _ = pretend
    base.mkdir(parents=True)
    # Four earlier deploys, the last one rolled back to t3 and forward again; t4 still built company-agent.
    (base / "deploys.log").write_text("".join(f"2026-10-0{i}T10:00:00+01:00\t{t}\t{how}\n" for i, (t, how) in
                                              enumerate([("t1", "deploy"), ("t2", "deploy"), ("t3", "deploy"),
                                                         ("t4", "deploy"), ("t3", "rollback")], 1)))
    result = deploy()
    assert result.returncode == 0, result.stderr
    removal = next(line for line in (state / "docker.log").read_text().splitlines() if line.startswith("image rm"))
    removed = set(removal.split()[2:])
    current = (base / "deploys.log").read_text().splitlines()[-1].split("\t")[1]
    # The service keeps the current deploy and the two before it, t3 and t4.
    assert {"company2:t1", "company2:t2"} <= removed
    assert not {f"company2:{current}", "company2:t3", "company2:t4"} & removed
    # Of the runs only the latest image stays, and company-agent:latest goes because it was built by t4.
    assert {f"company2-agent:{t}" for t in ("t1", "t2", "t3", "t4")} <= removed
    assert {"company-agent:t1", "company-agent:t4", "company-agent:latest"} <= removed
    assert f"company2-agent:{current}" not in removed and "company2-agent:latest" not in removed


def test_rollback_and_password(pretend):
    deploy, base, state, repo = pretend
    assert deploy("--rollback").returncode != 0  # nothing deployed yet
    assert deploy().returncode == 0
    assert "no earlier deploy" in deploy("--rollback").stderr
    time.sleep(1.1)  # the tag holds the second of the deploy
    assert deploy().returncode == 0
    first, _ = [line.split("\t")[1] for line in (base / "deploys.log").read_text().splitlines()]
    result = deploy("--rollback")
    assert result.returncode == 0, result.stderr
    assert f"tag company2:{first} company2:local" in (state / "docker.log").read_text()
    assert (base / "deploys.log").read_text().splitlines()[-1].split("\t")[1:] == [first, "rollback"]
    result = deploy("--reset-password", stdin="a new long password\na new long password\n")
    assert result.returncode == 0, result.stderr
    assert (state / "password").read_text() == "a new long password\n"
    assert "differ" in deploy("--reset-password", stdin="one long password\nother long password\n").stderr


def test_settings_are_checked(pretend):
    deploy, base, state, repo = pretend
    env = repo / "company.env"
    good = env.read_text()
    for wrong, message in (("SERVER=\n", "SERVER is missing"), ("BASE=relative\n", "absolute"),
                           ("NAME=Big Name\n", "NAME may contain"), ("PORT=eighty\n", "PORT must be"),
                           ("this is no setting\n", "cannot read")):
        env.write_text(good + wrong)
        result = deploy()
        assert result.returncode != 0 and message in result.stderr, wrong
    env.unlink()
    assert "company.env is missing" in deploy().stderr
