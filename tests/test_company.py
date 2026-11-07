import subprocess

import pytest

from app import company, config, db
from app.errors import Invalid, Unknown
from conftest import MILA, NEW, OWNER, PROFILE, employee


# ---------------------------------------------------------------- The company itself

def test_a_company_is_created_only_in_an_empty_directory(tmp_path, monkeypatch):
    root = tmp_path / "company"
    monkeypatch.setattr(config, "COMPANY_DIR", root)
    company.ensure()
    assert (root / company.SCHEMA_FILE).read_text().strip() == str(db.SCHEMA_VERSION)
    assert (root / ".git").is_dir() and ".env" in (root / ".gitignore").read_text()
    assert subprocess.run(["git", "-C", str(root), "log", "--oneline"], capture_output=True, text=True).stdout
    company.ensure()  # a company of a known version is fine
    # Something else in the directory: no company is created on top of it, whatever it is.
    other = tmp_path / "other"
    (other / "firma").mkdir(parents=True)
    (other / "firma" / "akte.md").write_text("old data")
    monkeypatch.setattr(config, "COMPANY_DIR", other)
    with pytest.raises(company.NotACompany):
        company.ensure()
    assert sorted(p.name for p in other.iterdir()) == ["firma"]
    # Another schema version is refused as well.
    (root / company.SCHEMA_FILE).write_text("99\n")
    monkeypatch.setattr(config, "COMPANY_DIR", root)
    with pytest.raises(company.NotACompany):
        company.ensure()


def test_the_database_keeps_its_schema_version(firm, monkeypatch):
    with db.connect() as con:
        assert con.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0] == str(db.SCHEMA_VERSION)
    db.setup()
    with pytest.raises(db.SchemaMismatch):
        db.setup(db.SCHEMA_VERSION + 1)


def test_setup_creates_owner_and_handbook(tmp_path, monkeypatch):
    root = tmp_path / "company"
    monkeypatch.setattr(config, "COMPANY_DIR", root)
    monkeypatch.setattr(config, "DATABASE", tmp_path / "db.sqlite")
    company.ensure()
    db.setup()
    assert not company.is_set_up()
    with pytest.raises(Invalid):
        company.set_up(" ", "de", "Paul Weber", "Paul")
    owner = company.set_up("Testfirma", "de", "Paul Weber", "Paul")
    assert owner == "0001" and company.is_set_up() and company.owner() == owner
    assert company.settings() == {"name": "Testfirma", "language": "de"} and company.language() == "de"
    handbook = (root / "handbook.md").read_text(encoding="utf-8")
    assert "Paul ist Inhaber" in handbook and "Testfirma" in handbook and "{owner}" not in handbook
    assert company.person(owner)["roles"] == ["owner"] and company.person(owner)["language"] == "de"
    with pytest.raises(Invalid):
        company.set_up("Noch eine", "en", "Jemand", "")


# ---------------------------------------------------------------- IDs and names

def test_ids_are_never_reused(firm):
    assert company.next_id() == "0004"
    (firm / "archive" / "0007-2026-01-01").mkdir(parents=True)
    assert company.next_id() == "0008"


def test_names_are_unique_across_humans_and_employees(firm):
    for taken in ("Mila", "mila", "Neu", "Paul", "paul weber"):
        with pytest.raises(Invalid) as problem:
            company.check_name(taken)
        assert problem.value.key == "name_taken"
    assert company.check_name("Mila", MILA) == "Mila"  # the own name
    for wrong in ("", "   ", "A: B", "x" * 41, "`x`"):
        with pytest.raises(Invalid):
            company.check_name(wrong)
    assert company.check_name("  Lisa   Maria ") == "Lisa Maria"


def test_rename_keeps_the_id(firm):
    rel = f"employees/{MILA}"
    assert company.set_header(rel, {"name": "Lisa"}) == ["name"]
    assert company.find(MILA) == rel and company.card(rel)["name"] == "Lisa"
    assert company.by_name("lisa") == MILA and company.by_name("Mila") is None and company.by_name(MILA) == MILA
    assert company.display_name(MILA) == "Lisa" and company.display_name(OWNER) == "Paul"
    with pytest.raises(Invalid):
        company.set_header(rel, {"name": "Paul"})


def test_people_profiles(firm):
    p = company.person(OWNER)
    assert (p["name"], p["display_name"], p["roles"], p["language"], p["about"]) == (
        "Paul Weber", "Paul", ["owner"], "en", "About Paul")
    assert company.update_person(OWNER, "Paul Weber", "Pauli", "de", "New text") == [
        "display_name", "language", "about"]
    p = company.person(OWNER)
    assert p["display_name"] == "Pauli" and p["language"] == "de" and p["roles"] == ["owner"]
    with pytest.raises(Invalid):
        company.update_person(OWNER, "Mila", "", "de", "")
    with pytest.raises(Unknown):
        company.person("../x")


# ---------------------------------------------------------------- Profiles

def test_read_and_set_header(firm):
    profile = firm / "employees" / MILA / company.PROFILE
    assert company.header(profile)["title"] == "Assistant"
    assert company.header(profile)["model"] == ""
    company.set_field(profile, "status", "permanent")
    company.set_field(profile, "roles", "reviewer")
    fields = company.header(profile)
    assert fields["status"] == "permanent" and fields["roles"] == "reviewer"
    assert company.body(profile).strip().startswith("## Task")


def test_team_and_find(firm):
    assert [c["id"] for c in company.team()] == [MILA, NEW]
    assert company.find(NEW) == f"candidates/{NEW}"
    for wrong in ("9999", "../0002", "", "mila"):
        with pytest.raises(Unknown):
            company.find(wrong)


def test_read_stays_in_the_folder(firm):
    assert "Mila" in company.read(f"employees/{MILA}", "profile.md")
    with pytest.raises(FileNotFoundError):
        company.read(f"employees/{MILA}", "../../.env")


def test_roles(firm):
    rel = f"employees/{MILA}"
    assert company.holders("hr") == [] and company.hr_for(MILA) is None
    assert company.set_header(rel, {"roles": "reviewer, hr"}) == ["roles"]
    assert company.roles_of(rel) == ["hr", "reviewer"] and company.holders("hr") == [MILA]
    assert company.hr_for(MILA) is None and company.hr_for(NEW) == MILA and company.reviewers() == [MILA]
    company.set_header(rel, {"roles": "archivist"})
    assert company.archivist() == rel
    employee(firm, "0004", "---\nname: Otto\ntitle: Archive\n---\n")
    with pytest.raises(Invalid) as problem:
        company.set_header("employees/0004", {"roles": "archivist"})
    assert problem.value.key == "role_taken" and problem.value.params["name"] == "Mila"
    with pytest.raises(Invalid):
        company.set_header(rel, {"roles": "boss"})
    # Candidates hold no roles.
    company.set_field(firm / "candidates" / NEW / company.PROFILE, "roles", "hr")
    assert company.holders("hr") == []


def test_prompt_has_all_parts(firm):
    text = company.build_prompt(f"employees/{MILA}", "conversation", "- Context line").read_text(encoding="utf-8")
    for part in ("Your name is Mila and you work at Test Company as: Assistant", "You are Mila.", "Handbook text",
                 "- Paul: owner of the company, a human", "- Mila: Assistant", "# About Paul", "About Paul",
                 "What you learned from working together is in your profile.", "- Context line",
                 "You are having a normal working conversation",
                 "Your tools: Read, Glob, Grep, Write, Edit, WebSearch.",
                 "You answer a human in the language of their latest message, in English if that is unclear."):
        assert part in text, part
    # The memory is the profile, nothing else. The header is not in the prompt.
    assert "\ntools:" not in text and "\nservices:" not in text and "# Your memo" not in text
    german = company.build_prompt(f"employees/{MILA}", "conversation", "", lang="de").read_text(encoding="utf-8")
    assert ("in German if that is unclear. What you write into files of the company is in English."
            in german)
    # Whoever reviews someone learns what they can do technically, whatever their profile claims.
    facts = company.capabilities_of(f"employees/{MILA}")
    assert "tools Read, Glob, Grep, Write, Edit, WebSearch" in facts and "Services (todo)" in facts
    assert "only their own folder" in facts and "the whole company" in company.capabilities_of(f"candidates/{NEW}")


def test_grant(firm):
    rel = f"employees/{MILA}"
    assert company.grant(rel, "mcp__todo__create_task") == "todo__create_task"
    assert company.service_entries(rel) == ["todo__list_tasks", "todo__create_task"]
    with pytest.raises(Invalid):
        company.grant(rel, "mcp__todo__delete_all")
    with pytest.raises(Invalid):
        company.grant(rel, "Bash")


def test_edit_with_log_and_note(firm):
    rel, profile = f"employees/{MILA}", firm / "employees" / MILA / company.PROFILE
    profile.write_text(PROFILE + "Paul lives at Sample Street 1.\n", encoding="utf-8")
    text, version = company.for_editing(rel, "profile.md")
    assert text.startswith("## Task") and "services:" not in text
    new = text.replace("Paul lives at Sample Street 1.", "")
    assert "-Paul lives" in company.diff(rel, "profile.md", new)
    company.edit(rel, "profile.md", new, "private", "Address  does not\nbelong here", OWNER, version)
    assert "Sample Street" not in profile.read_text(encoding="utf-8")
    assert company.header(profile)["services"] == "todo__list_tasks" and "You are Mila." in company.body(profile)
    entry = company.edits(rel)[0]
    assert (entry["path"], entry["kind"], entry["person"]) == ("profile.md", "private", OWNER)
    assert entry["reason"] == "Address does not belong here"
    for stale, kind, path in ((version, "private", "profile.md"), ("x", "unknown", "profile.md"),
                              ("x", "private", company.EDITS)):
        with pytest.raises((Invalid, FileNotFoundError)):
            company.edit(rel, path, "other", kind, "", OWNER, stale)
    with pytest.raises(FileNotFoundError):
        company.for_editing(rel, "../../handbook.md")
    # The agreement can be started even if it does not exist yet, e.g. after hiring from a preset.
    text, version = company.for_editing(rel, company.AGREEMENT)
    assert text == ""
    company.edit(rel, company.AGREEMENT, "## Purpose\n\nTasks in order", "other", "", OWNER, version)
    assert company.read(rel, company.AGREEMENT) == "## Purpose\n\nTasks in order\n"
    with pytest.raises(FileNotFoundError):
        company.for_editing(rel, "assignments/missing.md")
    # The employee learns about it until their next conversation is recorded.
    prompt = company.build_prompt(rel, "conversation", "").read_text(encoding="utf-8")
    assert "Paul changed something in your folder" in prompt and "profile.md (private details removed: Address" in prompt
    (firm / rel / "journal").mkdir()
    (firm / rel / "journal" / "2999-01-01-0000.md").write_text("# Conversation", encoding="utf-8")
    assert "changed something" not in company.build_prompt(rel, "conversation", "").read_text(encoding="utf-8")


def test_edit_documents(firm):
    text, version = company.for_editing(None, "handbook.md")
    assert text == "Handbook text"
    company.edit_document("handbook.md", "New handbook", version)
    assert (firm / "handbook.md").read_text() == "New handbook\n"
    with pytest.raises(Invalid):
        company.edit_document("handbook.md", "Other", version)
    text, version = company.for_editing(None, f"people/{OWNER}/profile.md")
    assert text == "About Paul"
    company.edit_document(f"people/{OWNER}/profile.md", "I like short answers.", version)
    assert company.person(OWNER)["about"] == "I like short answers." and company.person(OWNER)["roles"] == ["owner"]
    for path in ("company.md", "mcp.json", ".env", f"employees/{MILA}/profile.md", "people/../.env"):
        with pytest.raises(FileNotFoundError):
            company.for_editing(None, path)


def test_search(firm):
    (firm / "candidates" / NEW / "note.md").write_text("Sample Street and sample street", encoding="utf-8")
    assert company.search("SAMPLE STR") == [{"id": NEW, "count": 2, "path": "note.md"}]
    assert company.search("  ") == []


def test_header_changes(office):
    rel = f"employees/{MILA}"
    changed = company.set_header(rel, {"title": "  Planner ", "model": "opus", "tools": "WebSearch, WebFetch",
                                       "services": "todo", "roles": "reviewer"})
    assert changed == ["title", "model", "tools", "services", "roles"]
    c = company.card(rel)
    assert (c["title"], c["model"], c["tools"], c["services"], c["roles"]) == (
        "Planner", "opus", ["WebSearch", "WebFetch"], ["todo"], ["reviewer"])
    assert company.set_header(rel, {"title": "Planner", "roles": ""}) == ["roles"]
    assert "You are Mila." in (office / "profile.md").read_text(encoding="utf-8")
    for fields in ({"title": ""}, {"model": "gpt"}, {"tools": "Web Search;rm"}, {"services": "missing"},
                   {"services": "todo__delete_all"}, {"status": "permanent"}, {"hired": "2020-01-01"}):
        with pytest.raises(Invalid):
            company.set_header(rel, fields)
    assert company.card(rel)["title"] == "Planner"


# ---------------------------------------------------------------- Presets and services

def test_presets_exist_in_both_languages(firm):
    de, en = company.presets("de"), company.presets("en")
    assert [p["key"] for p in de] == [p["key"] for p in en] and len(de) >= 5
    for preset in de + en:
        assert preset["name"] and preset["title"] and preset["description"] and preset["text"].startswith("## ")
        assert all(r in company.ROLES for r in company.split_list(preset["roles"]))
        assert all(t in company.EXTRA_TOOLS for t in company.split_list(preset["tools"]))
    assert {r for p in en for r in company.split_list(p["roles"])} == set(company.ROLES)
    for lang in ("de", "en"):
        assert "{owner}" in company.preset_text(lang, "handbook")


def test_hire_from_preset(firm):
    hr = next(p for p in company.presets("en") if "hr" in p["roles"])
    some_id = company.hire_from_preset({"name": hr["name"], "title": hr["title"], "roles": hr["roles"]}, hr["text"])
    rel = f"employees/{some_id}"
    c = company.card(rel)
    assert some_id == "0004" and c["roles"] == ["hr"] and c["hired"] and c["self_edit"]
    assert (firm / rel / "journal").is_dir() and company.holders("hr") == [some_id]
    assert "probation" not in (firm / rel / company.PROFILE).read_text(encoding="utf-8")
    # A profile in one piece is split: the way of working goes into its own file.
    assert "## How you work" not in company.body(firm / rel / company.PROFILE)
    assert company.part_text(rel, company.WORK).startswith("## How you work")
    assert hr["core"] and hr["work"].startswith("## How you work")
    for fields in ({"name": "Mila", "title": "x"}, {"name": "Neu2", "title": ""},
                   {"name": "Neu2", "title": "x", "tools": "Bash"}, {"name": "Neu2", "title": "x", "model": "gpt"},
                   {"name": "Neu2", "title": "x", "roles": "archivist,archivist"}):
        if "archivist" in fields.get("roles", ""):
            company.hire_from_preset({"name": "Otto", "title": "Archive", "roles": "archivist"}, "x")
        with pytest.raises(Invalid):
            company.hire_from_preset(fields, "x")


def test_own_change_to_the_profile(firm):
    rel = f"employees/{MILA}"
    profile, work = firm / rel / company.PROFILE, firm / rel / company.WORK
    company.protected_in_own(rel)
    snapshot = company.own_snapshot(rel)
    assert work.read_text(encoding="utf-8") == "" and company.take_own_change(rel, snapshot) == []
    # A rule in the way of working stays. The header the employee touched comes back as it was.
    profile.write_text(PROFILE.replace("tools: WebSearch", "tools: Bash"), encoding="utf-8")
    work.write_text("## How you work\n\n- Tuesdays in the office.\n", encoding="utf-8")
    (firm / rel / "agreement.md").write_text("made up", encoding="utf-8")
    header_only, rule = company.take_own_change(rel, snapshot)
    assert (header_only["part"], header_only["state"]) == ("profile.md", "header_only")
    assert (rule["part"], rule["state"], rule["limit"]) == ("work.md", "applied", config.WORK_WORDS)
    assert company.header(profile)["tools"] == "WebSearch" and "Tuesdays" in work.read_text(encoding="utf-8")
    assert not (firm / rel / "agreement.md").exists()
    assert "Tuesdays in the office" in company.build_prompt(rel, "conversation", "").read_text(encoding="utf-8")
    # The core is small: longer than allowed is not saved. A part that is too long already may get shorter.
    snapshot = company.own_snapshot(rel)
    profile.write_text(snapshot["parts"]["profile.md"] + "word " * config.PROFILE_WORDS, encoding="utf-8")
    change, = company.take_own_change(rel, snapshot)
    assert change["state"] == "too_long" and change["limit"] == config.PROFILE_WORDS
    assert profile.read_text(encoding="utf-8") == snapshot["parts"]["profile.md"]
    # A link instead of a part would let the service read and write elsewhere.
    work.unlink()
    work.symlink_to(firm / "handbook.md")
    assert company.take_own_change(rel, snapshot) == []
    assert not work.is_symlink() and "Tuesdays" in work.read_text(encoding="utf-8")
    assert (firm / "handbook.md").read_text(encoding="utf-8") == "Handbook text"
    # Without own changes the file comes back, and the change is a proposal for the human.
    company.set_header(rel, {"self_edit": "no"})
    assert not company.self_edit(rel) and "need Paul's confirmation" in company.own_profile_note(rel, "Paul")
    snapshot = company.own_snapshot(rel)
    work.write_text(snapshot["parts"]["work.md"] + "- New rule.\n", encoding="utf-8")
    change, = company.take_own_change(rel, snapshot)
    assert change["state"] == "proposed" and work.read_text(encoding="utf-8") == snapshot["parts"]["work.md"]
    assert company.replace_text(rel, "work.md", change["before"], change["after"])
    assert "New rule" in work.read_text(encoding="utf-8")
    assert not company.replace_text(rel, "work.md", change["before"], "stale")
    with pytest.raises(Invalid):
        company.set_header(rel, {"self_edit": "maybe"})
    # Read-only inside the own folder: everything but the two parts.
    assert company.protected_in_own(rel) == [f"{rel}/{name}" for name in
                                             ("assessment", "assignments", "feedback", "journal")]


def test_split_profile():
    core, work = company.split_profile("Intro\n\n## Task\n\nDo it.\n\n## How you work\n\n- Rule.\n\n"
                                       "## Persönlichkeit\n\nCalm.\n\n## Style and frame\n\nShort.\n")
    assert core == "Intro\n\n## Task\n\nDo it.\n\n## Persönlichkeit\n\nCalm."
    assert work == "## How you work\n\n- Rule.\n\n## Style and frame\n\nShort."


def test_profile_versions(firm):
    rel = f"employees/{MILA}"
    company.commit("Start")
    company.replace_text(rel, company.PROFILE, company.body(firm / rel / company.PROFILE), "## Task\n\nYou are Mila, briefly.")
    company.commit("Profile of Mila changed by Mila in conversation 1")
    versions = company.profile_versions(rel)
    assert [v["subject"] for v in versions] == ["Profile of Mila changed by Mila in conversation 1", "Start"]
    old = company.profile_version(rel, versions[1]["commit"])
    assert old["text"] == "## Task\n\nYou are Mila." and "+You are Mila." in old["diff"]
    assert "+You are Mila, briefly." in company.profile_version(rel, versions[0]["commit"])["diff"]
    with pytest.raises(Unknown):
        company.profile_version(rel, "0" * 40)


def test_services_and_token(firm):
    assert company.services_overview() == [{"name": "todo", "type": "http", "url": "http://localhost:9/mcp",
                                            "description": "Tasks", "blocked": ["delete_all"],
                                            "headers": ["Authorization"]}]
    # A new value replaces the secret, an empty one keeps it.
    company.save_service("todo", "http", "http://todo:8000/mcp", "Task list", "delete_all, purge",
                         "Authorization", "")
    stored = company.mcp_json()["todo"]
    assert stored["headers"]["Authorization"] == "Bearer secret" and stored["blocked"] == ["delete_all", "purge"]
    company.save_service("calendar", "sse", "https://cal.example/sse", "Calendar", "", "X-Key", "k1")
    assert company.mcp_json()["calendar"]["headers"] == {"X-Key": "k1"}
    assert oct((firm / "mcp.json").stat().st_mode)[-3:] == "600"
    for args in (("bad name", "http", "http://x", "", ""), ("x", "ftp", "http://x", "", ""),
                 ("x", "http", "file:///etc/passwd", "", ""), ("a__b", "http", "http://x", "", ""),
                 ("y", "http", "http://x", "", "", "X-New", "")):
        with pytest.raises(Invalid):
            company.save_service(*args)
    assert company.delete_service("calendar") and not company.delete_service("calendar")
    assert company.token_kind() == "subscription"
    company.set_token("api", "sk-ant-api03-test")
    assert company.token_kind() == "api" and oct((firm / ".env").stat().st_mode)[-3:] == "600"
    # The second line of a wrapped token alone, or a key of the other kind.
    for kind, value in (("api", ""), ("api", "a b"), ("other", "x"), ("subscription", "Xq7vR2mK9wLp"),
                        ("subscription", "sk-ant-api03-test")):
        with pytest.raises(Invalid):
            company.set_token(kind, value)
