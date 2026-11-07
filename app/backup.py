"""Backups of the database and the company, made by the service itself.

The database is copied with SQLite's online backup, which is consistent even while the service writes. The company is
a Git repository and is saved as a bundle with its whole history. Secrets (.env, mcp.json) are not in Git and are
copied next to it. Backups older than BACKUP_DAYS are removed.
"""
import shutil
import sqlite3
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path

from . import company, config


def make(label: str = "") -> Path:
    """One backup in BACKUPS/<date-time>[-label]. Returns its folder."""
    name = datetime.now().strftime("%Y-%m-%d-%H%M%S") + (f"-{label}" if label else "")
    target = config.BACKUPS / name
    part = target.with_name(name + ".part")
    shutil.rmtree(part, ignore_errors=True)
    part.mkdir(parents=True)
    part.chmod(0o700)
    if config.DATABASE.is_file():
        with sqlite3.connect(f"file:{config.DATABASE}?mode=ro", uri=True) as source, \
                sqlite3.connect(part / "company.db") as copy:
            source.backup(copy)
    company.commit("Backup")
    subprocess.run(["git", "-C", str(config.COMPANY_DIR), "bundle", "create", str(part / "company.bundle"), "--all"],
                   check=True, capture_output=True)
    for secret in (".env", "mcp.json"):
        if (config.COMPANY_DIR / secret).is_file():
            shutil.copy2(config.COMPANY_DIR / secret, part / secret)
            (part / secret).chmod(0o600)
    part.rename(target)
    prune()
    return target


def prune() -> list[str]:
    """Remove backups older than BACKUP_DAYS. Returns what was removed."""
    if not config.BACKUPS.is_dir():
        return []
    limit = (date.today() - timedelta(days=config.BACKUP_DAYS)).isoformat()
    removed = []
    for folder in config.BACKUPS.iterdir():
        if folder.is_dir() and folder.name[:10] < limit and folder.name[:4].isdigit():
            shutil.rmtree(folder)
            removed.append(folder.name)
    return removed


def latest() -> str | None:
    if not config.BACKUPS.is_dir():
        return None
    found = sorted(f.name for f in config.BACKUPS.iterdir() if f.is_dir() and not f.name.endswith(".part"))
    return found[-1] if found else None
