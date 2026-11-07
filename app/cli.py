"""Commands for deploy, run inside the container: python -m app.cli <command>

  reset-password [name]   new password for a human (default: the owner), read from standard input
"""
import sys

from . import auth, company, db


def reset_password(args: list[str]) -> int:
    company.ensure()
    db.setup()
    wanted = " ".join(args).strip().lower()
    people = company.people()
    found = next((p for p in people if wanted in (p["name"].lower(), p["display_name"].lower(), p["id"])), None) \
        if wanted else next((p for p in people if "owner" in p["roles"]), None)
    if not found:
        print("There is nobody with that name." if wanted else "The company is not set up yet.", file=sys.stderr)
        return 1
    password = sys.stdin.readline().rstrip("\n")
    try:
        auth.set_password(found["id"], password)
    except Exception as error:
        print(error, file=sys.stderr)
        return 1
    print(f"New password set for {found['display_name']}. All their logins have ended.")
    return 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["reset-password"]:
        return reset_password(argv[1:])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
