"""The company on disk: profiles, people, the handbook, prompts, services and Git.

An employee is a folder with text files. Everyone, humans and employees alike, has an ID that never changes and a name
that may change. The ID names the folder, the name is what people and agents see. Roles in the profile header decide
who does which job in the company.
"""
import difflib
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

from . import config, i18n
from .errors import Invalid, Unknown

HERE = Path(__file__).parent
PROCEDURES = HERE / "procedures"
PRESETS = HERE / "presets"

AREAS = ("employees", "candidates")
PROFILE = "profile.md"
ID_PATTERN = re.compile(r"^\d{4}$")
SERVICE_ENTRY = re.compile(r"^[A-Za-z0-9_-]+$")
# What the human agreed with HR in the hiring interview: what the employee is there for and how one notices that it is
# worth it. The feedback meeting measures the work by it. Not in the employee's prompt; their profile says the same as
# a task.
AGREEMENT = "agreement.md"
# The way of working: the second part of the profile, see "Own changes to the profile".
WORK = "work.md"
# What a reviewer has tried before and with which result. The service adds to it after each assessment.
REVIEWS = "reviews.md"
# The history: transcripts of conversations (folder journal) and assignments for colleagues. In conversations only the
# archivist sees it; the others work with today's state. Contents go stale. What proves itself as a way of working
# goes into the profile through the feedback meeting.
# Not to be confused with the folder archive, where dismissed employees are kept.
HISTORY = ("journal", "assignments")
# Whoever has one of these tools reads foreign texts from the web and therefore gets only their own folder mounted.
WEB_TOOLS = ("WebSearch", "WebFetch")
# Only these tools may appear in the field tools of a profile. Profiles are also written by an LLM, e.g. HR in a
# hiring interview. If Bash were there, a candidate in the assessment would get past everything that
# --disallowedTools blocks only for Read and Edit.
EXTRA_TOOLS = WEB_TOOLS
# Log of what a human changed in the files of an employee through the UI.
EDITS = "edits.md"
EDIT_KINDS = ("private", "factual", "other")
MODELS = ("", "haiku", "sonnet", "opus")
# Roles of employees.
ROLES = ("hr", "reviewer", "archivist")
# These roles have at most one holder.
SINGLE_ROLES = ("archivist",)
NAME_LENGTH = 40
SCHEMA_FILE = ".schema"
GITIGNORE = ".env\nmcp.json\nruntime/\n"


# ---------------------------------------------------------------- Header of a profile

def header(file: Path) -> dict[str, str]:
    """Fields from the header of a profile (between the two lines with ---)."""
    return header_of(file.read_text(encoding="utf-8"))


def header_of(content: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    lines = content.splitlines()
    if not lines or lines[0] != "---":
        return fields
    for line in lines[1:]:
        if line == "---":
            break
        key, separator, value = line.partition(":")
        if separator and key not in fields:
            fields[key] = value.strip()
    return fields


def body(file: Path) -> str:
    return body_of(file.read_text(encoding="utf-8"))


def body_of(content: str) -> str:
    lines = content.splitlines()
    if lines and lines[0] == "---":
        try:
            end = lines.index("---", 1)
            lines = lines[end + 1:]
        except ValueError:
            lines = []
    return "\n".join(lines)


def set_field(file: Path, key: str, value: str) -> None:
    """Set a value in the header of a profile. Missing fields are appended."""
    lines = file.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "---":
        raise Invalid("no_header", path=str(file))
    end = lines.index("---", 1)
    for i in range(1, end):
        if lines[i].partition(":")[0] == key:
            lines[i] = f"{key}: {value}"
            break
    else:
        lines.insert(end, f"{key}: {value}")
    file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_profile(file: Path, fields: dict[str, str], text: str) -> None:
    file.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---", *(f"{k}: {' '.join(str(v).split())}" for k, v in fields.items()), "---", ""]
    file.write_text("\n".join(lines) + "\n" + text.strip("\n") + "\n", encoding="utf-8")


def split_list(value: str) -> list[str]:
    return [part for part in re.split(r"[,\s]+", value or "") if part]


# ---------------------------------------------------------------- The company itself

class NotACompany(Exception):
    """The company directory holds something that is not a company of a known schema version."""


def ensure() -> None:
    """Create the company in an empty directory, or check the one that is there. Never write into anything else."""
    root = config.COMPANY_DIR
    if not root.exists() or (root.is_dir() and not any(root.iterdir())):
        root.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "--quiet", str(root)], check=True, capture_output=True)
        (root / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
        (root / SCHEMA_FILE).write_text(f"{schema_version()}\n", encoding="utf-8")
        for area in (*AREAS, "people", "archive"):
            (root / area).mkdir(exist_ok=True)
        commit("Company created")
        return
    marker = root / SCHEMA_FILE
    if not marker.is_file():
        raise NotACompany(f"{root} is not empty and is not a company (no {SCHEMA_FILE}). "
                          "A new company is only created in an empty directory.")
    found = marker.read_text(encoding="utf-8").strip()
    if found != str(schema_version()):
        raise NotACompany(f"The company in {root} has schema version {found}, this code expects {schema_version()}.")


def schema_version() -> int:
    from .db import SCHEMA_VERSION
    return SCHEMA_VERSION


def settings() -> dict[str, str]:
    file = config.COMPANY_DIR / "company.md"
    return header(file) if file.is_file() else {}


def language() -> str:
    return i18n.language(settings().get("language"))


def set_language(lang: str) -> None:
    """Applies to what employees write into the company from now on; files already written stay as they are."""
    set_field(config.COMPANY_DIR / "company.md", "language", i18n.language(lang))


def company_name() -> str:
    return settings().get("name", "")


def rename_company(name: str) -> None:
    name = " ".join((name or "").split())
    if not name:
        raise Invalid("company_name_empty")
    set_field(config.COMPANY_DIR / "company.md", "name", name)


# The claim of the company: an optional short line below its name in the header.
CLAIM_LENGTH = 60


def claim() -> str:
    return settings().get("claim", "")


def set_claim(text: str) -> None:
    """An empty claim removes the line."""
    text = " ".join((text or "").split())
    if len(text) > CLAIM_LENGTH:
        raise Invalid("claim_too_long", size=CLAIM_LENGTH)
    set_field(config.COMPANY_DIR / "company.md", "claim", text)


# The logo of the company, next to its name in the header. Only raster images: an SVG can carry a script that runs
# when someone opens /logo directly.
LOGO_TYPES = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}
LOGO_KB = 512


def logo() -> Path | None:
    for ext in LOGO_TYPES:
        file = config.COMPANY_DIR / f"logo.{ext}"
        if file.is_file():
            return file
    return None


def _image_type(data: bytes) -> str | None:
    """The type by the first bytes of the file, not by what the browser claims."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def set_logo(data: bytes) -> None:
    if len(data) > LOGO_KB * 1024:
        raise Invalid("logo_too_big", size=LOGO_KB)
    ext = _image_type(data)
    if not ext:
        raise Invalid("logo_type")
    remove_logo()
    (config.COMPANY_DIR / f"logo.{ext}").write_bytes(data)


def remove_logo() -> bool:
    file = logo()
    if file:
        file.unlink()
    return file is not None


def is_set_up() -> bool:
    return owner() is not None


def sync_procedures() -> Path:
    """Copy the procedures to the runtime folder. Runs mount them from there, because the Docker daemon reads paths on
    the host, and the code of this service may live in an image."""
    target = config.RUNTIME / "procedures"
    target.mkdir(parents=True, exist_ok=True)
    for source in PROCEDURES.glob("*.md"):
        copy = target / source.name
        if not copy.is_file() or copy.read_bytes() != source.read_bytes():
            shutil.copyfile(source, copy)
    return target


def procedure(name: str) -> str:
    return (PROCEDURES / f"{name}.md").read_text(encoding="utf-8").rstrip("\n")


# ---------------------------------------------------------------- IDs and names

def next_id() -> str:
    """The next free ID. IDs are never reused, also not after a dismissal: the archive keeps them."""
    used = [0]
    for area in (*AREAS, "people", "archive"):
        folder = config.COMPANY_DIR / area
        if folder.is_dir():
            used += [int(p.name[:4]) for p in folder.iterdir() if p.is_dir() and p.name[:4].isdigit()]
    return f"{max(used) + 1:04d}"


def names_in_use(except_id: str | None = None) -> dict[str, str]:
    """Lower-case names of everyone who is in the company, humans with name and display name, mapped to their ID."""
    names = {}
    for area in AREAS:
        for profile in (config.COMPANY_DIR / area).glob(f"*/{PROFILE}"):
            if profile.parent.name != except_id:
                names[(header(profile).get("name") or profile.parent.name).lower()] = profile.parent.name
    for p in people():
        if p["id"] != except_id:
            names[p["name"].lower()] = p["id"]
            names[p["display_name"].lower()] = p["id"]
    return names


def names() -> list[str]:
    """The names in the company as they are written, for whoever needs to pick a new one."""
    found = {name: None for p in people() for name in (p["name"], p["display_name"])}
    for area in AREAS:
        for profile in sorted((config.COMPANY_DIR / area).glob(f"*/{PROFILE}")):
            found[header(profile).get("name") or profile.parent.name] = None
    return sorted(found, key=str.lower)


def check_name(name: str, except_id: str | None = None) -> str:
    """A name everyone can tell apart: agents address each other by name, so it must be unique in the company."""
    name = " ".join((name or "").split())
    if not name:
        raise Invalid("name_empty")
    if len(name) > NAME_LENGTH or ":" in name or "`" in name or "*" in name:
        raise Invalid("name_form", length=NAME_LENGTH)
    if name.lower() in names_in_use(except_id):
        raise Invalid("name_taken", name=name)
    return name


# ---------------------------------------------------------------- People

def _person_file(person_id: str) -> Path:
    return config.COMPANY_DIR / "people" / person_id / PROFILE


def _person(file: Path) -> dict:
    fields = header(file)
    return {"id": file.parent.name, "name": fields.get("name") or file.parent.name,
            "display_name": fields.get("display_name") or fields.get("name") or file.parent.name,
            "roles": split_list(fields.get("roles", "")), "language": i18n.language(fields.get("language")),
            "about": body(file).strip("\n")}


def people() -> list[dict]:
    folder = config.COMPANY_DIR / "people"
    return [_person(f) for f in sorted(folder.glob(f"*/{PROFILE}"))] if folder.is_dir() else []


def person(person_id: str) -> dict:
    file = _person_file(person_id or "x")
    if not ID_PATTERN.match(person_id or "") or not file.is_file():
        raise Unknown("unknown_person", id=person_id)
    return _person(file)


def owner() -> str | None:
    return next((p["id"] for p in people() if "owner" in p["roles"]), None)


def display_name(some_id: str) -> str:
    """The name to show for an ID, human or employee, also for someone in the archive."""
    try:
        return person(some_id)["display_name"]
    except Unknown:
        pass
    for area in (*AREAS, "archive"):
        for profile in sorted((config.COMPANY_DIR / area).glob(f"{some_id}*/{PROFILE}")):
            if profile.parent.name == some_id or profile.parent.name.startswith(f"{some_id}-"):
                return header(profile).get("name") or some_id
    return some_id


def person_language(person_id: str | None) -> str:
    try:
        return person(person_id or "")["language"]
    except Unknown:
        return language()


def create_person(name: str, display_name: str, lang: str, roles: list[str], about: str = "") -> str:
    name = check_name(name)
    display = " ".join((display_name or "").split()) or name
    if display.lower() != name.lower():
        check_name(display)
    person_id = next_id()
    write_profile(_person_file(person_id), {"name": name, "display_name": display, "roles": ",".join(roles),
                                            "language": i18n.language(lang)}, about)
    return person_id


def update_person(person_id: str, name: str, display_name: str, lang: str, about: str) -> list[str]:
    """Change the own profile. Returns what changed."""
    before = person(person_id)
    name = check_name(name, person_id)
    display = " ".join((display_name or "").split()) or name
    if display.lower() not in (name.lower(), before["display_name"].lower()):
        check_name(display, person_id)
    after = {"name": name, "display_name": display, "language": i18n.language(lang),
             "about": about.replace("\r\n", "\n").strip("\n")}
    changed = [k for k, v in after.items() if v != before[k]]
    if changed:
        write_profile(_person_file(person_id), {"name": name, "display_name": display,
                                                "roles": ",".join(before["roles"]), "language": after["language"]},
                      after["about"])
    return changed


# ---------------------------------------------------------------- Setup

def preset_text(lang: str, name: str) -> str:
    file = PRESETS / i18n.language(lang) / f"{name}.md"
    return file.read_text(encoding="utf-8") if file.is_file() else ""


def set_up(company: str, lang: str, owner_name: str, owner_display: str) -> str:
    """The first start: company name and language, the owner, the handbook from the preset. Returns the owner's ID."""
    if is_set_up():
        raise Invalid("already_set_up")
    company = " ".join((company or "").split())
    if not company:
        raise Invalid("company_name_empty")
    lang = i18n.language(lang)
    write_profile(config.COMPANY_DIR / "company.md", {"name": company, "language": lang}, "")
    owner_id = create_person(owner_name, owner_display, lang, ["owner"])
    handbook = config.COMPANY_DIR / "handbook.md"
    if not handbook.is_file():
        text = preset_text(lang, "handbook").replace("{owner}", person(owner_id)["display_name"])
        handbook.write_text(text.replace("{company}", company), encoding="utf-8")
    return owner_id


# ---------------------------------------------------------------- Team

def find(some_id: str) -> str:
    """Folder of an employee or candidate, relative to the company."""
    if ID_PATTERN.match(some_id or ""):
        for area in AREAS:
            if (config.COMPANY_DIR / area / some_id / PROFILE).is_file():
                return f"{area}/{some_id}"
    raise Unknown("unknown_employee", id=some_id)


def name_of(rel: str) -> str:
    return header(config.COMPANY_DIR / rel / PROFILE).get("name") or rel.split("/")[1]


def by_name(name: str) -> str | None:
    """ID of the employee (not candidate) with this name or ID. Agents ask colleagues by name."""
    wanted = " ".join(name.split()).strip(" `*").lower()
    for profile in sorted((config.COMPANY_DIR / "employees").glob(f"*/{PROFILE}")):
        if wanted in (profile.parent.name, (header(profile).get("name") or "").lower()):
            return profile.parent.name
    return None


def _latest(folder: Path, only_dirs: bool = False) -> str:
    if not folder.is_dir():
        return ""
    names = sorted(p.name for p in folder.iterdir() if (p.is_dir() if only_dirs else p.suffix == ".md"))
    return names[-1][:10] if names else ""


def card(rel: str) -> dict:
    """The key facts of an employee for the team page and the card."""
    folder = config.COMPANY_DIR / rel
    fields = header(folder / PROFILE)
    area, some_id = rel.split("/")
    return {
        "id": some_id,
        "rel": rel,
        "candidate": area == "candidates",
        "name": fields.get("name") or some_id,
        "title": fields.get("title", ""),
        "model": fields.get("model", ""),
        "tools": split_list(fields.get("tools", "")),
        "services": service_entries(rel),
        "roles": [r for r in split_list(fields.get("roles", "")) if r in ROLES],
        "hired": fields.get("hired", ""),
        "self_edit": self_edit(rel),
        "last_conversation": _latest(folder / "journal"),
        "last_assignment": _latest(folder / "assignments"),
        "last_feedback": _latest(folder / "feedback", only_dirs=True),
        "feedback": feedback_state(rel),
        "assessed": (folder / "assessment").is_dir() and any((folder / "assessment").glob("*-report.md")),
    }


def team() -> list[dict]:
    cards = []
    for area in AREAS:
        folder = config.COMPANY_DIR / area
        found = [card(f"{area}/{p.parent.name}") for p in folder.glob(f"*/{PROFILE}")] if folder.is_dir() else []
        cards += sorted(found, key=lambda c: (c["name"].lower(), c["id"]))
    return cards


def roles_of(rel: str) -> list[str]:
    return [r for r in split_list(header(config.COMPANY_DIR / rel / PROFILE).get("roles", "")) if r in ROLES]


def holders(role: str) -> list[str]:
    """IDs of the employees (not candidates) who hold this role, in the order they joined."""
    return [p.parent.name for p in sorted((config.COMPANY_DIR / "employees").glob(f"*/{PROFILE}"))
            if role in split_list(header(p).get("roles", ""))]


def reviewers() -> list[str]:
    """Who reviews in the assessment: every reviewer."""
    return sorted(holders("reviewer"))


def hr_for(subject: str) -> str | None:
    """Who runs HR work about someone: an HR employee who is not that person."""
    return next((h for h in holders("hr") if h != subject), None)


def archivist() -> str | None:
    """Folder of the archivist, if there is one."""
    found = holders("archivist")
    return f"employees/{found[0]}" if found else None


def prepare_candidates() -> set[str]:
    """Give each draft in candidates the usual subfolders and a proper ID. Returns the IDs.

    HR is told which ID to use. A draft in a folder that is not a free ID, e.g. one named after the candidate,
    is moved to the next free ID.
    """
    folder = config.COMPANY_DIR / "candidates"
    ids = set()
    for profile in sorted(folder.glob(f"*/{PROFILE}")) if folder.is_dir() else []:
        draft = profile.parent
        if not ID_PATTERN.match(draft.name) or any((config.COMPANY_DIR / a / draft.name).exists()
                                                    for a in ("employees", "people")):
            draft = draft.rename(folder / next_id())
        for subfolder in ("journal",):
            (draft / subfolder).mkdir(exist_ok=True)
        # Roles are given by a human on the card, never by whoever writes the draft.
        if header(draft / PROFILE).get("roles"):
            set_field(draft / PROFILE, "roles", "")
        ids.add(draft.name)
    return ids


def hire(some_id: str) -> None:
    """The candidate becomes an employee."""
    source = config.COMPANY_DIR / "candidates" / (some_id or "x")
    target = config.COMPANY_DIR / "employees" / (some_id or "x")
    if not ID_PATTERN.match(some_id or "") or not (source / PROFILE).is_file():
        raise Unknown("unknown_candidate", id=some_id)
    if target.exists():
        raise Invalid("id_taken", id=some_id)
    check_name(name_of(f"candidates/{some_id}"), some_id)
    # HR can write the draft until the end, also its header. Roles come only from a human, after hiring.
    if header(source / PROFILE).get("roles"):
        set_field(source / PROFILE, "roles", "")
    set_field(source / PROFILE, "hired", date.today().isoformat())
    for subfolder in ("journal", "feedback"):
        (source / subfolder).mkdir(exist_ok=True)
    source.rename(target)


def hire_from_preset(fields: dict[str, str], text: str, work: str = "") -> str:
    """A new employee from a preset or an existing profile, without hiring interview and assessment. "text" is the
    core, "work" the way of working."""
    name = check_name(fields.get("name", ""))
    title = " ".join(fields.get("title", "").split())
    if not title:
        raise Invalid("title_empty")
    roles = [r for r in split_list(fields.get("roles", "")) if r in ROLES]
    for role in roles:
        if role in SINGLE_ROLES and holders(role):
            raise Invalid("role_taken", role=role, name=name_of(f"employees/{holders(role)[0]}"))
    tools = split_list(fields.get("tools", ""))
    if unknown := [t for t in tools if t not in EXTRA_TOOLS]:
        raise Invalid("tools_not_allowed", tools=", ".join(unknown), allowed=", ".join(EXTRA_TOOLS))
    model = fields.get("model", "")
    if model not in MODELS:
        raise Invalid("model_unknown", model=model)
    text, work = text.replace("\r\n", "\n"), work.replace("\r\n", "\n").strip("\n")
    if not work:
        # A profile pasted in one piece is split into its two parts.
        text, work = split_profile(text)
    some_id = next_id()
    profile = config.COMPANY_DIR / "employees" / some_id / PROFILE
    write_profile(profile, {"name": name, "title": title, "roles": ",".join(roles), "model": model,
                            "tools": ",".join(tools), "services": "", "hired": date.today().isoformat()}, text)
    (profile.parent / WORK).write_text(work + "\n" if work else "", encoding="utf-8")
    for subfolder in ("journal", "feedback"):
        (profile.parent / subfolder).mkdir(exist_ok=True)
    return some_id


def presets(lang: str) -> list[dict]:
    """The profiles for typical roles that come with the code, in the given language."""
    found = []
    for file in sorted((PRESETS / i18n.language(lang)).glob("*.md")):
        if file.stem == "handbook":
            continue
        fields = header(file)
        found.append({"key": file.stem, "name": fields.get("name", ""), "title": fields.get("title", ""),
                      "description": fields.get("description", ""), "roles": fields.get("roles", ""),
                      "tools": fields.get("tools", ""), "model": fields.get("model", ""),
                      "text": body(file).strip("\n"), **dict(zip(("core", "work"), split_profile(body(file))))})
    return found


def dismiss(some_id: str) -> str:
    """The folder of an employee or candidate moves to the archive. Returns its name in the archive."""
    rel = find(some_id)
    target = config.COMPANY_DIR / "archive" / f"{some_id}-{date.today().isoformat()}"
    if target.exists():
        raise Invalid("archive_taken", folder=target.name)
    target.parent.mkdir(exist_ok=True)
    (config.COMPANY_DIR / rel).rename(target)
    return target.name


ARCHIVE_PATTERN = re.compile(r"^(\d{4})-\d{4}-\d{2}-\d{2}$")


def archive() -> list[str]:
    folder = config.COMPANY_DIR / "archive"
    return sorted(p.name for p in folder.iterdir() if (p / PROFILE).is_file() and ARCHIVE_PATTERN.match(p.name)) \
        if folder.is_dir() else []


def archived(folder: str) -> str:
    """Folder of someone in the archive, relative to the company."""
    if folder not in archive():
        raise Unknown("unknown_archive", folder=folder)
    return f"archive/{folder}"


def rehire(folder: str) -> str:
    """Bring someone back from the archive, with all files, as employee or draft as back then. Returns the ID."""
    rel = archived(folder)
    some_id = folder[:4]
    # Whoever was never hired has no hiring date and comes back as a draft.
    area = "employees" if header(config.COMPANY_DIR / rel / PROFILE).get("hired") else "candidates"
    if any((config.COMPANY_DIR / a / some_id).exists() for a in AREAS):
        raise Invalid("id_taken", id=some_id)
    check_name(name_of(rel), some_id)
    (config.COMPANY_DIR / rel).rename(config.COMPANY_DIR / area / some_id)
    return some_id


# ---------------------------------------------------------------- Header of an employee

def set_header(rel: str, fields: dict[str, str]) -> list[str]:
    """Change name, title, model, extra tools, services and roles in the profile header. Returns what changed."""
    profile = config.COMPANY_DIR / rel / PROFILE
    before = header(profile)
    some_id = rel.split("/")[1]
    new = {}
    for key, value in fields.items():
        value = " ".join(str(value).split())
        if key == "name":
            value = check_name(value, some_id)
        elif key == "title":
            if not value:
                raise Invalid("title_empty")
        elif key == "model":
            if value not in MODELS and value != before.get("model"):
                raise Invalid("model_unknown", model=value)
        elif key == "tools":
            parts = split_list(value)
            if unknown := [t for t in parts if t not in EXTRA_TOOLS]:
                raise Invalid("tools_not_allowed", tools=", ".join(unknown), allowed=", ".join(EXTRA_TOOLS))
            value = ",".join(parts)
        elif key == "services":
            parts = split_list(value)
            services = mcp_json()
            for part in parts:
                service, _, tool = part.partition("__")
                if not SERVICE_ENTRY.match(part) or service not in services:
                    raise Invalid("service_unknown", service=service)
                if tool in services[service].get("blocked", []):
                    raise Invalid("tool_blocked", tool=tool)
            value = ",".join(parts)
        elif key == "roles":
            parts = [r for r in split_list(value)]
            if unknown := [r for r in parts if r not in ROLES]:
                raise Invalid("role_unknown", role=", ".join(unknown))
            for role in parts:
                others = [h for h in holders(role) if h != some_id]
                if role in SINGLE_ROLES and others:
                    raise Invalid("role_taken", role=role, name=name_of(f"employees/{others[0]}"))
            value = ",".join(r for r in ROLES if r in parts)
        elif key == "self_edit":
            if value not in ("yes", "no"):
                raise Invalid("self_edit_value")
            # A profile without the field counts as yes.
            if value == before.get(key, "yes"):
                continue
        else:
            raise Invalid("field_fixed", field=key)
        if value != before.get(key, ""):
            new[key] = value
    for key, value in new.items():
        set_field(profile, key, value)
    return list(new)


# ---------------------------------------------------------------- Own changes to the profile
# The profile has two parts. The core (profile.md, below the header): task, personality, limits. It stays small and
# changes seldom. The way of working (work.md, no header): rules, methods and what to leave. It may grow, but whoever
# changes it merges instead of adding. Both are in every prompt.
# In a conversation with a human the employee changes both themselves. What a human says ("from now on do it like
# this") applies at once, and what the employee notices goes in as well. The human sees every change in the chat and
# can undo it. The header stays the service's: tools, services and roles come only from a human.

# The parts and how many words each may have. A part that is longer already may get shorter, not longer.
PARTS = (PROFILE, WORK)


def part_limit(part: str) -> int:
    return config.PROFILE_WORDS if part == PROFILE else config.WORK_WORDS


def part_text(rel: str, part: str, content: str | None = None) -> str:
    """The text of a part: of the core what is below the header, of the way of working everything."""
    if content is None:
        file = config.COMPANY_DIR / rel / part
        content = file.read_text(encoding="utf-8") if file.is_file() else ""
    return (body_of(content) if part == PROFILE else content).strip("\n")


def self_edit(rel: str) -> bool:
    """Whether the employee's own changes apply at once. Otherwise the human confirms them. Missing means yes."""
    return header(config.COMPANY_DIR / rel / PROFILE).get("self_edit", "yes") != "no"


def own_profile_note(rel: str, person_name: str) -> str:
    """Lines for the prompt of a conversation: how the employee changes their own profile."""
    folder = f"/company/{rel}"
    sizes = "; ".join(
        f"{folder}/{part} has {words(part_text(rel, part))} words, a change that makes it longer than "
        f"{max(part_limit(part), words(part_text(rel, part)))} is not saved" for part in PARTS)
    rules = (f"\n- Your profile has two parts: {folder}/{PROFILE} is the core below the header (task, personality, "
             f"limits), {folder}/{WORK} your way of working (rules, methods, what you leave). New rules go into the "
             "way of working. The header between the --- lines (name, title, model, tools, services, roles) only a "
             f"human changes; changes there are put back. {sizes}. When and how you change them, the handbook says "
             "under Memory.")
    if self_edit(rel):
        return (f"\n- You can change both parts of your profile. A change applies from your next answer on. "
                f"{person_name} sees every change in the chat with the difference and can undo it." + rules)
    return (f"\n- Changes to your profile need {person_name}'s confirmation. Make them in the files as usual: the "
            f"service then puts them back and shows {person_name} the difference to accept." + rules)


# In a conversation the own folder is writable. Only these entries are meant to change; everything else in it is
# mounted read-only on top, and what is new afterwards is removed.
OWN_WRITABLE = PARTS


def own_snapshot(rel: str) -> dict:
    """The state of the own folder before a run that may write in it."""
    folder = config.COMPANY_DIR / rel
    if not (folder / WORK).is_file():
        # It exists before the run, so that a new way of working is a change like any other.
        (folder / WORK).write_text("", encoding="utf-8")
    return {"parts": {part: (folder / part).read_text(encoding="utf-8") for part in PARTS},
            "entries": {p.name for p in folder.iterdir()}}


# Folders the service writes into while the employee may be running, e.g. an assignment for a colleague. They exist
# before the run, so that they are mounted read-only and are no new entry afterwards.
SERVICE_FOLDERS = ("journal", "assignments", "feedback", "assessment")


def protected_in_own(rel: str) -> list[str]:
    """What is mounted read-only inside the writable own folder, relative to the company."""
    folder = config.COMPANY_DIR / rel
    for name in SERVICE_FOLDERS:
        (folder / name).mkdir(exist_ok=True)
    return [f"{rel}/{p.name}" for p in sorted(folder.iterdir()) if p.name not in OWN_WRITABLE]


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _content(part: str, before: str, text: str) -> str:
    """The content of a part's file with this text. The core keeps the header it had."""
    return _new_text(before, text, part == PROFILE, part)


def take_own_change(rel: str, snapshot: dict) -> list[dict]:
    """After a run in the own folder: put back what may not change and say what became of each part of the profile.

    The header is always put back. A change of a text stays if the employee may change it themselves and it does not
    make the part longer than allowed; otherwise the file is put back as well. Per changed part: "part", "state"
    ("applied", "proposed", "too_long" or "header_only"), "before" and "after" (the texts), "words" and "limit".
    """
    folder = config.COMPANY_DIR / rel
    for path in folder.iterdir():
        if path.name not in snapshot["entries"]:
            _remove(path)
    changes = []
    for part, before in snapshot["parts"].items():
        file = folder / part
        # A link or a folder instead of the file would make the service read or write somewhere else.
        if file.is_symlink() or not file.is_file():
            _remove(file)
            file.write_text(before, encoding="utf-8")
            continue
        now = file.read_text(encoding="utf-8")
        if now == before:
            continue
        old, new = part_text(rel, part, before), part_text(rel, part, now)
        limit = max(part_limit(part), words(old))
        change = {"part": part, "before": old, "after": new, "words": words(new), "limit": limit}
        if new == old:
            state = "header_only"
        elif words(new) > limit:
            state = "too_long"
        elif not self_edit(rel):
            state = "proposed"
        else:
            state = "applied"
        file.write_text(_content(part, before, new) if state == "applied" else before, encoding="utf-8")
        changes.append({**change, "state": state})
    return changes


def replace_text(rel: str, part: str, expected: str, text: str) -> bool:
    """Put another text into a part, if the current one is still "expected". False if it changed meanwhile."""
    file = config.COMPANY_DIR / rel / part
    old = file.read_text(encoding="utf-8") if file.is_file() else ""
    if part_text(rel, part, old) != expected.strip("\n"):
        return False
    file.write_text(_content(part, old, text), encoding="utf-8")
    return True


def text_diff(before: str, after: str) -> str:
    return "\n".join(difflib.unified_diff(before.splitlines(), after.splitlines(), "before", "after", lineterm="", n=1))


def _git_output(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(config.COMPANY_DIR), *args], check=False, capture_output=True, text=True)


def profile_versions(rel: str, part: str = PROFILE, limit: int = 20) -> list[dict]:
    """The versions of a part of the profile in Git, newest first: commit, time, message and the path at that time.

    The folder moves from candidates to employees on hiring; --follow keeps the earlier versions. Commits that change
    only the header of profile.md (approved services, roles, model) are left out: the text of the part is the same.
    """
    result = _git_output("log", "--follow", f"-n{limit * 3 if part == PROFILE else limit}",
                         "--format=%x00%H%x09%ad%x09%s", "--date=format:%Y-%m-%d %H:%M", "--name-only", "--",
                         f"{rel}/{part}")
    versions = []
    for entry in result.stdout.split("\x00")[1:] if result.returncode == 0 else []:
        lines = [line for line in entry.splitlines() if line.strip()]
        commit_id, when, subject = (lines[0].split("\t", 2) + ["", ""])[:3]
        path = lines[1] if len(lines) > 1 else f"{rel}/{part}"
        if part == PROFILE and _header_only(rel, commit_id, path):
            continue
        versions.append({"commit": commit_id, "when": when, "subject": subject, "path": path})
        if len(versions) == limit:
            break
    return versions


def _header_only(rel: str, commit_id: str, path: str) -> bool:
    """Whether the commit changed only the header of profile.md. A first version or a move counts as a change."""
    earlier = _git_output("show", f"{commit_id}^:{path}")
    if earlier.returncode != 0:
        return False
    now = _git_output("show", f"{commit_id}:{path}")
    return now.returncode == 0 and part_text(rel, PROFILE, now.stdout) == part_text(rel, PROFILE, earlier.stdout)


def profile_version(rel: str, commit_id: str, part: str = PROFILE) -> dict:
    """The text of a part in one version and the difference to the version before."""
    if part not in PARTS:
        raise Unknown("unknown_version", version=commit_id[:8])
    found = next((v for v in profile_versions(rel, part, 200) if v["commit"] == commit_id), None)
    if not found:
        raise Unknown("unknown_version", version=commit_id[:8])
    shown = _git_output("show", f"{commit_id}:{found['path']}")
    text = part_text(rel, part, shown.stdout) if shown.returncode == 0 else ""
    earlier = _git_output("show", f"{commit_id}^:{found['path']}")
    before = part_text(rel, part, earlier.stdout) if earlier.returncode == 0 else ""
    return {**found, "part": part, "text": text, "diff": text_diff(before, text)}


# The core of a profile: these sections. Everything else is the way of working. Used to split a profile written in one
# piece, e.g. a preset or a pasted profile, when hiring; the human sees both parts before.
CORE_HEADINGS = {"task", "aufgabe", "personality", "persönlichkeit", "limits", "grenzen"}


def split_profile(text: str) -> tuple[str, str]:
    """Core and way of working of a profile in one piece, by its "## " headings. Text before the first heading belongs
    to the core."""
    core, work, current = [], [], None
    for line in text.replace("\r\n", "\n").splitlines():
        if line.startswith("## "):
            current = core if line[3:].strip().lower() in CORE_HEADINGS else work
        (current if current is not None else core).append(line)
    return "\n".join(core).strip("\n"), "\n".join(work).strip("\n")


# ---------------------------------------------------------------- Tools and history

def extra_tools(rel: str) -> list[str]:
    return split_list(header(config.COMPANY_DIR / rel / PROFILE).get("tools", ""))


def reads_web(rel: str) -> bool:
    return any(t in WEB_TOOLS for t in extra_tools(rel))


def web_readers() -> list[str]:
    """Names of the employees who read on the web."""
    return [header(p).get("name") or p.parent.name
            for p in sorted((config.COMPANY_DIR / "employees").glob(f"*/{PROFILE}"))
            if reads_web(f"employees/{p.parent.name}")]


def history_dirs() -> list[str]:
    """The existing folders journal and assignments of all employees and candidates, relative to the company."""
    return [f"{area}/{folder.name}/{name}"
            for area in AREAS if (config.COMPANY_DIR / area).is_dir()
            for folder in sorted((config.COMPANY_DIR / area).iterdir())
            for name in HISTORY if (folder / name).is_dir()]


def capabilities_of(rel: str) -> str:
    """Line for the prompt of whoever reviews or evaluates someone: what the colleague can do technically.

    What counts is the profile header and the setup of this service, not the text of the profile.
    """
    name = name_of(rel)
    tools = ", ".join(config.BASE_TOOLS.split(",") + extra_tools(rel))
    reading = ("only their own folder, the handbook and the procedures, because they read on the web"
               if reads_web(rel) else "the whole company under /company")
    if rel != archivist():
        reading += (". In conversations transcripts and assignments are hidden, their own included. For earlier "
                    "matters they ask the archivist there; otherwise they work with today's state")
    services = ", ".join(service_names(rel))
    return (f"\n- What {name} can do technically, whatever the text of the profile says: tools {tools}. "
            f"{name} can read {reading}. Writing is only possible where the procedure allows it."
            + (f" Services ({services}) are connected only in conversations with a human, not in the review."
               if services else f" {name} has no services.")
            + f" Whether {name} really looked something up or searched is in the tool logs under “Tools”.")


def team_list() -> str:
    lines = [f"- {p['display_name']}: {_person_title(p)}, a human" for p in people()]
    for profile in sorted((config.COMPANY_DIR / "employees").glob(f"*/{PROFILE}")):
        fields = header(profile)
        lines.append(f"- {fields.get('name', '')}: {fields.get('title', '')}")
    return "\n".join(lines)


def _person_title(p: dict) -> str:
    return "owner of the company" if "owner" in p["roles"] else "member of the company"


def write_assignment(rel: str, name: str, text: str) -> str:
    """Store the log of an assignment for colleagues. Returns the path relative to the company."""
    folder = config.COMPANY_DIR / rel / "assignments"
    folder.mkdir(exist_ok=True)
    (folder / f"{name}.md").write_text(text, encoding="utf-8")
    return f"{rel}/assignments/{name}.md"


def files(rel: str, subfolder: str) -> list[str]:
    """Markdown files of a subfolder, newest first, relative to the employee's folder."""
    folder = config.COMPANY_DIR / rel / subfolder
    if not folder.is_dir():
        return []
    return sorted((str(p.relative_to(config.COMPANY_DIR / rel)) for p in folder.rglob("*.md")), reverse=True)


def read(rel: str, path: str) -> str:
    """Read a file from an employee's folder, nothing outside of it."""
    folder = (config.COMPANY_DIR / rel).resolve()
    target = (folder / path).resolve()
    if not target.is_relative_to(folder) or not target.is_file():
        raise FileNotFoundError(path)
    return target.read_text(encoding="utf-8")


# ---------------------------------------------------------------- Editing
# A human changes a file themselves, e.g. to remove private details. That bypasses the feedback meeting and is no
# finding about the employee, so it goes into a log of its own.

def editable(path: str) -> bool:
    return path.endswith(".md") and path != EDITS


def _version(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _current(file: Path, may_be_missing: bool) -> str:
    if file.is_file():
        return file.read_text(encoding="utf-8")
    if may_be_missing:
        return ""
    raise FileNotFoundError(file.name)


def _new_text(old: str, text: str, keep_header: bool, what: str) -> str:
    """The new content of a file. With keep_header the header of a profile stays as it is."""
    text = text.replace("\r\n", "\n").strip("\n")
    new = text + "\n" if text else ""
    if keep_header:
        lines = old.splitlines()
        if not lines or lines[0] != "---":
            raise Invalid("no_header", path=what)
        new = "\n".join(lines[:lines.index("---", 1) + 1]) + "\n\n" + new
    return new


def _document(path: str) -> Path:
    """A file of the company that a human may edit: the handbook or the text of a person's profile."""
    if path == "handbook.md" or re.fullmatch(r"people/\d{4}/profile\.md", path):
        return config.COMPANY_DIR / path
    raise FileNotFoundError(path)


def _employee_file(rel: str, path: str) -> tuple[Path, bool]:
    if not editable(path):
        raise FileNotFoundError(path)
    folder = (config.COMPANY_DIR / rel).resolve()
    target = (folder / path).resolve()
    if not target.is_relative_to(folder):
        raise FileNotFoundError(path)
    # The agreement and the way of working may be missing yet, e.g. after hiring from a preset; they then start empty.
    if not target.is_file() and path not in (AGREEMENT, WORK):
        raise FileNotFoundError(path)
    return target, path in (AGREEMENT, WORK)


def for_editing(rel: str | None, path: str) -> tuple[str, str]:
    """Text for the editor and the version it is based on. Of a profile only the part below the header."""
    file, may_be_missing = _employee_file(rel, path) if rel else (_document(path), False)
    content = _current(file, may_be_missing)
    text = body(file) if path.endswith(PROFILE) and file.is_file() else content
    return text.strip("\n"), _version(content)


def _change(rel: str | None, path: str, text: str) -> tuple[Path, str, str]:
    file, may_be_missing = _employee_file(rel, path) if rel else (_document(path), False)
    old = _current(file, may_be_missing)
    return file, old, _new_text(old, text, path.endswith(PROFILE), path)


def diff(rel: str | None, path: str, text: str) -> str:
    _, old, new = _change(rel, path, text)
    return "\n".join(difflib.unified_diff(old.splitlines(), new.splitlines(), "before", "after", lineterm=""))


def edit_document(path: str, text: str, version: str) -> None:
    """Change the handbook or the text of a person's profile."""
    file, old, new = _change(None, path, text)
    if _version(old) != version:
        raise Invalid("changed_meanwhile")
    if new == old:
        raise Invalid("nothing_changed")
    file.write_text(new, encoding="utf-8")


def edit(rel: str, path: str, text: str, kind: str, reason: str, person_id: str, version: str) -> None:
    """Change a file in an employee's folder and record that in the log."""
    if kind not in EDIT_KINDS:
        raise Invalid("edit_kind_unknown")
    file, old, new = _change(rel, path, text)
    if _version(old) != version:
        raise Invalid("changed_meanwhile")
    if new == old:
        raise Invalid("nothing_changed")
    file.parent.mkdir(exist_ok=True)
    file.write_text(new, encoding="utf-8")
    log = config.COMPANY_DIR / rel / EDITS
    if not log.is_file():
        lang = language()
        log.write_text(f"# {i18n.t(lang, 'file.edits_title')}\n\n{i18n.t(lang, 'file.edits_intro')}\n\n",
                       encoding="utf-8")
    line = f"- {datetime.now():%Y-%m-%d %H:%M} · {path} · {kind} · {person_id} · "
    with log.open("a", encoding="utf-8") as f:
        f.write((line + " ".join(reason.split())).rstrip(" ") + "\n")


def edits(rel: str) -> list[dict]:
    """The log of edits, newest first. "kind" is one of EDIT_KINDS, "person" an ID."""
    log = config.COMPANY_DIR / rel / EDITS
    if not log.is_file():
        return []
    entries = []
    for line in log.read_text(encoding="utf-8").splitlines():
        parts = line[2:].split(" · ", 4) if line.startswith("- ") else []
        if len(parts) >= 4:
            entries.append(dict(zip(("when", "path", "kind", "person", "reason"), parts + [""])))
    return entries[::-1]


def _edits_note(rel: str) -> str:
    """Line for the prompt: what a human changed since the last conversation."""
    folder = config.COMPANY_DIR / rel / "journal"
    latest = max((p.stem for p in folder.glob("*.md")), default="") if folder.is_dir() else ""
    # Transcripts are named YYYY-MM-DD-HHMM, the log writes "YYYY-MM-DD HH:MM".
    new = [e for e in edits(rel) if e["when"].replace(" ", "-").replace(":", "") > latest]
    if not new:
        return ""
    kinds = {"private": "private details removed", "factual": "factual correction", "other": "other"}
    listed = "; ".join(f"{e['path']} ({kinds.get(e['kind'], e['kind'])}{': ' + e['reason'] if e['reason'] else ''})"
                       for e in new[::-1])
    who = ", ".join(sorted({display_name(e["person"]) for e in new}))
    return (f"\n- {who} changed something in your folder since your last conversation: {listed}. "
            "This is no criticism of you. The current version applies. Do not bring it up yourself and do not repeat "
            "anything that was only in the earlier version.")


# ---------------------------------------------------------------- Feedback
# Per meeting a folder feedback/YYYY-MM-DD-HHMM with the evaluation, one page for the owner, and the proposed profile
# if something should change. As long as decision.md is missing, the meeting waits for a decision.

MEETING_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{4}$")
# Who led the feedback meeting: the ID of the HR employee.
LED_BY = "led-by.txt"


def now() -> str:
    """Time stamp for meetings and files."""
    return datetime.now().strftime("%Y-%m-%d-%H%M")


def words(text: str) -> int:
    return len(text.split())


def feedback_dir(rel: str, meeting: str) -> Path:
    folder = config.COMPANY_DIR / rel / "feedback" / (meeting or "x")
    if not MEETING_PATTERN.match(meeting or "") or not (folder / "evaluation.md").is_file():
        raise FileNotFoundError(meeting)
    return folder


def feedback_meetings(rel: str) -> list[dict]:
    """The feedback meetings with an evaluation, newest first."""
    folder = config.COMPANY_DIR / rel / "feedback"
    meetings = []
    for m in sorted(folder.iterdir(), reverse=True) if folder.is_dir() else []:
        if MEETING_PATTERN.match(m.name) and (m / "evaluation.md").is_file():
            decision = (m / "decision.md").read_text(encoding="utf-8").strip() if (m / "decision.md").is_file() else ""
            meetings.append({"meeting": m.name, "open": not decision,
                             "decision": decision.splitlines()[0] if decision else ""})
    return meetings


def feedback_state(rel: str) -> dict:
    """Whether a meeting waits for a decision and when the last feedback meeting was. Nothing becomes due by itself."""
    meetings = feedback_meetings(rel)
    return {"open": next((m["meeting"] for m in meetings if m["open"]), None),
            "last": next((m["meeting"] for m in meetings if not m["open"]), "")}


def feedback_material(rel: str, since: str) -> dict[str, list[str]]:
    """What the feedback meeting reads, relative to the company: transcripts, assignments and review reports since the
    last meeting, of each only the latest FEEDBACK_CONVERSATIONS. Files are named by date, "since" like a meeting."""
    material = {}
    for name, pattern in (("journal", "*.md"), ("assignments", "*.md"), ("assessment", "*-report.md")):
        folder = config.COMPANY_DIR / rel / name
        found = sorted(p.name for p in folder.glob(pattern)) if folder.is_dir() else []
        # Reports are named by day only (YYYY-MM-DD-reviewer-report.md); a report of the day of the meeting counts.
        found = [f for f in found if not since or (f[:10] >= since[:10] if name == "assessment" else f[:15] > since)]
        material[name] = [f"{rel}/{name}/{f}" for f in found[-config.FEEDBACK_CONVERSATIONS:]]
    return material


def hr_track_record(hr_id: str, since: str) -> str:
    """How the owner decided on the proposals of an HR employee since their last feedback meeting.

    Their work lies in the folders of others. Whoever leads their feedback meeting does not see those folders when
    reading on the web. The list therefore names only name, date and decision, nothing from other profiles.
    """
    lang = language()
    lines = []
    for profile in sorted((config.COMPANY_DIR / "employees").glob(f"*/{PROFILE}")):
        some_id = profile.parent.name
        if some_id == hr_id:
            continue
        name = header(profile).get("name") or some_id
        for m in reversed(feedback_meetings(f"employees/{some_id}")):
            folder = profile.parent / "feedback" / m["meeting"]
            led = (folder / LED_BY).read_text(encoding="utf-8").strip() if (folder / LED_BY).is_file() else ""
            if m["meeting"] > since and led in ("", hr_id):
                lines.append("- " + i18n.t(lang, "file.track_feedback", name=name, meeting=m["meeting"],
                                            decision=m["decision"] or i18n.t(lang, "file.undecided"),
                                            words=words((folder / "evaluation.md").read_text(encoding="utf-8"))))
    if not lines:
        return ""
    since_text = i18n.t(lang, "file.track_since", meeting=since) if since else i18n.t(lang, "file.track_start")
    return (f"# {i18n.t(lang, 'file.track_title')}\n\n{i18n.t(lang, 'file.track_intro', since=since_text)}\n\n"
            + "\n".join(lines) + "\n")


def _parts_version(rel: str) -> str:
    return _version("\x00".join(part_text(rel, part) for part in PARTS))


def feedback_proposal(rel: str, meeting: str) -> dict | None:
    """What the meeting proposes for the parts of the profile, if it differs from the current text. Only the text
    counts; a header in the proposal is never taken over. "version" covers both parts."""
    folder = feedback_dir(rel, meeting)
    parts = []
    for part in PARTS:
        if not (folder / part).is_file():
            continue
        text, current = part_text(rel, part, (folder / part).read_text(encoding="utf-8")), part_text(rel, part)
        if text == current:
            continue
        parts.append({"part": part, "text": text, "diff": text_diff(current, text), "words_before": words(current),
                      "words": words(text), "limit": part_limit(part),
                      "header_changed": part == PROFILE and header(folder / PROFILE) not in (
                          {}, header(config.COMPANY_DIR / rel / PROFILE))})
    return {"version": _parts_version(rel), "parts": parts} if parts else None


def feedback_decide(rel: str, meeting: str, accept: bool, texts: dict[str, str] | None = None,
                    version: str | None = None) -> str:
    """The owner's decision. Accepting writes the proposed parts of the profile, edited if wished ("texts" per part).
    Rejecting leaves them as they are.

    Returns "accepted", "accepted_edited" or "rejected". decision.md says the same in the company's language.
    """
    folder = feedback_dir(rel, meeting)
    if (folder / "decision.md").is_file():
        raise Invalid("already_decided")
    result = "rejected"
    if accept:
        # Check everything first, then write, so that never only a part arrives.
        proposal, edited, writes = feedback_proposal(rel, meeting), False, []
        if proposal:
            if version != proposal["version"]:
                raise Invalid("changed_meanwhile")
            for p in proposal["parts"]:
                text = (texts or {}).get(p["part"])
                text = p["text"] if text is None else text.replace("\r\n", "\n").strip("\n")
                edited = edited or text != p["text"]
                file = config.COMPANY_DIR / rel / p["part"]
                writes.append((p["part"], _content(p["part"], file.read_text(encoding="utf-8") if file.is_file()
                                                   else "", text), text != p["text"]))
        for part, content, changed in writes:
            (config.COMPANY_DIR / rel / part).write_text(content, encoding="utf-8")
            if changed:
                # The meeting's folder then holds what really applies. The original proposal stays in Git.
                (folder / part).write_text(content, encoding="utf-8")
        result = "accepted_edited" if edited else "accepted"
    decision = i18n.t(language(), f"file.decision_{result}", date=date.today().isoformat())
    (folder / "decision.md").write_text(decision + "\n", encoding="utf-8")
    return result


def search(term: str) -> list[dict]:
    """In which files of employees and candidates a text appears. Case does not matter."""
    term = term.strip().lower()
    hits = []
    if not term:
        return hits
    for area in AREAS:
        for profile in sorted((config.COMPANY_DIR / area).glob(f"*/{PROFILE}")):
            for file in sorted(profile.parent.rglob("*.md")):
                try:
                    count = file.read_text(encoding="utf-8").lower().count(term)
                except (OSError, UnicodeDecodeError):
                    continue
                if count:
                    hits.append({"id": profile.parent.name, "count": count,
                                 "path": str(file.relative_to(profile.parent))})
    return hits


# ---------------------------------------------------------------- Services (MCP)
# The field "services" in a profile header names services from mcp.json, comma separated.
# "service" allows all tools of the service, "service__tool" only that one.

@dataclass
class Services:
    file: Path | None = None                        # extract of mcp.json for this employee only
    allowed: list[str] = field(default_factory=list)  # tool names for --allowedTools
    blocked: list[str] = field(default_factory=list)
    hosts: list[tuple[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def service_entries(rel: str) -> list[str]:
    return split_list(header(config.COMPANY_DIR / rel / PROFILE).get("services", ""))


def service_names(rel: str) -> list[str]:
    return sorted({e.split("__")[0] for e in service_entries(rel)})


def mcp_json() -> dict:
    file = config.COMPANY_DIR / "mcp.json"
    if not file.is_file():
        return {}
    return json.loads(file.read_text(encoding="utf-8")).get("mcpServers", {})


def blocked_for_all(service: str) -> list[str]:
    return list(mcp_json().get(service, {}).get("blocked", []))


def build_mcp(rel: str) -> Services:
    """Write an extract of mcp.json with the services of this employee to runtime/mcp."""
    s = Services()
    entries = service_entries(rel)
    for entry in entries:
        if not SERVICE_ENTRY.match(entry):
            raise Invalid("service_entry_invalid", rel=rel, entry=entry)
        s.allowed.append(f"mcp__{entry}")
    if not entries:
        return s
    servers = mcp_json()
    if not servers:
        raise Invalid("services_missing", rel=rel)
    extract = {}
    for service in service_names(rel):
        if service not in servers:
            raise Invalid("service_unknown", service=service)
        extract[service] = {k: v for k, v in servers[service].items() if k not in ("description", "blocked")}
        s.blocked += [f"mcp__{service}__{t}" for t in servers[service].get("blocked", [])]
    folder = config.RUNTIME / "mcp"
    folder.mkdir(parents=True, exist_ok=True)
    s.file = folder / f"{rel.replace('/', '-')}.json"
    # The file holds access tokens, so it is readable for the own user only.
    fd = os.open(s.file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"mcpServers": extract}, f, indent=2)
    # The container only asks the DNS server of its network and does not know names that only this machine resolves
    # (VPN, /etc/hosts). The hosts of the services are resolved here and given to the container. In a network of its
    # own (config.NETWORK) Docker resolves the names of the services itself; here they would be unknown.
    hosts = set() if config.NETWORK else {urlparse(v.get("url", "")).hostname or "" for v in extract.values()} - {""}
    for host in sorted(hosts):
        try:
            s.hosts.append((host, str(socket.getaddrinfo(host, None, socket.AF_INET)[0][4][0])))
        except OSError:
            s.notes.append(f"The host '{host}' from mcp.json cannot be resolved here. The service will be unreachable.")
    return s


def services_note(rel: str, connected: bool, person_name: str = "") -> str:
    """Line for the prompt: are the services connected in this procedure?"""
    services = ", ".join(service_names(rel))
    if not services:
        return ""
    if connected:
        return (f"\n- These services are connected in this conversation: {services}. "
                f"You work on {person_name or 'the owner'}'s real data there.")
    return (f"\n- Your services ({services}) are not connected in this procedure. If you needed them, "
            "say so and describe what you would do with them. Do not make up data.")


def services_catalog() -> str:
    """List of all services in mcp.json with description, for the hiring interview."""
    lines = []
    for name, service in mcp_json().items():
        blocked = service.get("blocked", [])
        lines.append(f"  - {name}: {service.get('description', 'no description')}"
                     + (f" Blocked for everyone: {', '.join(blocked)}." if blocked else ""))
    return "\n".join(lines) or "  - none set up"


def grant(rel: str, tool: str) -> str:
    """Allow a tool of a service in the profile. Returns the new entry."""
    if not tool.startswith("mcp__") or not SERVICE_ENTRY.match(tool[5:]):
        raise Invalid("not_a_service_tool", tool=tool)
    entry = tool[5:]
    service, _, name = entry.partition("__")
    if name in blocked_for_all(service):
        raise Invalid("tool_blocked", tool=name)
    entries = service_entries(rel)
    if entry not in entries and service not in entries:
        set_field(config.COMPANY_DIR / rel / PROFILE, "services", ",".join(entries + [entry]))
    return entry


def _write_secret(file: Path, text: str) -> None:
    fd = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(file, 0o600)


def services_overview() -> list[dict]:
    """The services for the page "Services", without secrets: only which header names have a value."""
    return [{"name": name, "type": s.get("type", "http"), "url": s.get("url", ""),
             "description": s.get("description", ""), "blocked": s.get("blocked", []),
             "headers": sorted((s.get("headers") or {}).keys())}
            for name, s in sorted(mcp_json().items())]


def save_service(name: str, kind: str, url: str, description: str, blocked: str, header_name: str = "",
                 header_value: str = "") -> None:
    """Add or change a service. An empty header value keeps the value that is there."""
    if not SERVICE_ENTRY.match(name or "") or "__" in name:
        raise Invalid("service_name")
    if kind not in ("http", "sse"):
        raise Invalid("service_type")
    if urlparse(url or "").scheme not in ("http", "https"):
        raise Invalid("service_url")
    file = config.COMPANY_DIR / "mcp.json"
    data = json.loads(file.read_text(encoding="utf-8")) if file.is_file() else {}
    servers = data.setdefault("mcpServers", {})
    service = servers.setdefault(name, {})
    service.update({"type": kind, "url": url.strip(), "description": " ".join(description.split()),
                    "blocked": split_list(blocked)})
    header_name = header_name.strip()
    if header_name:
        if not re.fullmatch(r"[A-Za-z0-9-]+", header_name):
            raise Invalid("service_header")
        headers = service.setdefault("headers", {})
        if header_value.strip():
            headers[header_name] = header_value.strip()
        elif header_name not in headers:
            raise Invalid("service_header_value")
    _write_secret(file, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def delete_service(name: str) -> bool:
    file = config.COMPANY_DIR / "mcp.json"
    data = json.loads(file.read_text(encoding="utf-8")) if file.is_file() else {}
    if name not in data.get("mcpServers", {}):
        return False
    del data["mcpServers"][name]
    _write_secret(file, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return True


TOKEN_KINDS = {"subscription": "CLAUDE_CODE_OAUTH_TOKEN", "api": "ANTHROPIC_API_KEY"}
# How a token of each kind begins. `claude setup-token` wraps its long token over two lines in many terminals, and a
# copy of the second line alone would otherwise only fail in the first run, with "Invalid bearer token".
TOKEN_PREFIXES = {"subscription": "sk-ant-oat", "api": "sk-ant-api"}


def token_kind() -> str | None:
    """Which kind of Claude token is set, never its value."""
    file = config.COMPANY_DIR / ".env"
    if not file.is_file():
        return None
    keys = {line.partition("=")[0].strip() for line in file.read_text(encoding="utf-8").splitlines()
            if line.partition("=")[2].strip()}
    return next((kind for kind, key in TOKEN_KINDS.items() if key in keys), None)


def set_token(kind: str, value: str) -> None:
    if kind not in TOKEN_KINDS:
        raise Invalid("token_kind")
    value = value.strip()
    if not value or any(c.isspace() for c in value):
        raise Invalid("token_value")
    if not value.startswith(TOKEN_PREFIXES[kind]):
        raise Invalid("token_prefix", prefix=TOKEN_PREFIXES[kind])
    _write_secret(config.COMPANY_DIR / ".env", f"{TOKEN_KINDS[kind]}={value}\n")


# ---------------------------------------------------------------- Prompt

def build_prompt(rel: str, procedure_name: str, context: str, readable: list[str] | None = None,
                 lang: str | None = None) -> Path:
    """Put the system prompt together and store it under runtime/prompts.

    "readable" names what someone who reads on the web may see besides their own folder.
    """
    folder = config.COMPANY_DIR / rel
    fields = header(folder / PROFILE)
    if reads_web(rel):
        more = "".join(f", plus /company/{path}" for path in readable or [] if path != rel)
        reading = ("- The company is under /company. Because you read on the web, only your own folder, the "
                   f"handbook and the procedures are mounted for you{more}. You cannot read other folders.")
    else:
        reading = "- The company is under /company. You can read everything there."
    company_lang = language()
    lang = i18n.language(lang or company_lang)
    # The language of the message wins over the setting: a human who writes in another language means it.
    writing = (f"- You answer a human in the language of their latest message, in {i18n.NAMES[lang]} if that is "
               f"unclear. What you write into files of the company is in {i18n.NAMES[company_lang]}.")
    about = [f"# About {p['display_name']}\n\n{p['about']}\n" for p in people() if p["about"].strip()]
    handbook = config.COMPANY_DIR / "handbook.md"
    parts = [
        "# Who you are",
        "",
        f"Your name is {fields.get('name', '')} and you work at {company_name() or 'the company'} as: "
        f"{fields.get('title', '')}.",
        "You are an AI employee. Everybody knows that; it is no secret.",
        "",
        body(folder / PROFILE),
        "",
        part_text(rel, WORK),
        "",
        "# Handbook of the company",
        "",
        handbook.read_text(encoding="utf-8").rstrip("\n") if handbook.is_file() else "(none)",
        "",
        "# Your team",
        "",
        team_list(),
        "",
        *about,
        "# Workplace",
        "",
        f"- Today is {date.today().isoformat()}.",
        writing,
        f"- Your folder: /company/{rel}",
        reading,
        f"- Your tools: {', '.join(config.BASE_TOOLS.split(',') + extra_tools(rel))}. With them you read and search "
        "files. What is said here applies, even if your profile says something else.",
        "- You can write only where it says so explicitly below. Everything else is blocked technically.",
        "- You do not remember earlier conversations by yourself. What you learned from working together is in "
        "your profile."
        + (_edits_note(rel) if procedure_name in ("conversation", "hiring") else ""),
        context,
        "",
        "# Current procedure",
        "",
        procedure(procedure_name),
    ]
    target = config.RUNTIME / "prompts" / f"{rel.replace('/', '-')}-{procedure_name}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(parts) + "\n", encoding="utf-8")
    return target


# ---------------------------------------------------------------- Git

class GitError(Exception):
    """The commit did not happen. The files are written anyway."""


# Synchronous routes commit in the thread pool, the services through asyncio.to_thread. Two commits at the same time
# would fail on index.lock, or "add -A" would take the files of the other one under the wrong message.
_git = threading.Lock()


def commit(message: str) -> bool:
    """Git commit in the company, if something changed. True if there was a commit."""
    git = ["git", "-C", str(config.COMPANY_DIR), "-c", "user.name=Company", "-c", "user.email=company@localhost"]
    with _git:
        subprocess.run(git + ["add", "-A"], check=False, capture_output=True)
        if subprocess.run(git + ["diff", "--cached", "--quiet"], check=False).returncode == 0:
            return False
        result = subprocess.run(git + ["commit", "--quiet", "-m", message], check=False, capture_output=True,
                                text=True)
    if result.returncode != 0:
        lines = (result.stderr or result.stdout).strip().splitlines()
        raise GitError(lines[0] if lines else "unknown error")
    return True


def committed(message: str) -> str | None:
    """Git commit. Returns the error if it failed, so that the caller can tell the human."""
    try:
        commit(message)
    except GitError as error:
        return str(error)
    return None
