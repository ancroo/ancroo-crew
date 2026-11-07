"""Login with name and password. Every human has a password from the setup on; without login only the setup and the
login page are reachable.

The password is stored as a scrypt hash with the person. The browser gets a cookie with a random value; the database
holds only its SHA-256.
"""
import hashlib
import hmac
import secrets
import time

from . import company, config, db
from .errors import Invalid

COOKIE = "company_session"
MIN_LENGTH = 10


def _hash(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1).hex()


def _short(token: str) -> str:
    return hashlib.sha256(token.encode("ascii", "replace")).hexdigest()


def set_password(person_id: str, password: str) -> None:
    """Set a password. All logins of this human end."""
    if len(password or "") < MIN_LENGTH:
        raise Invalid("password_short", length=MIN_LENGTH)
    salt = secrets.token_bytes(16)
    db.add_person(person_id)
    with db.connect() as con:
        con.execute("UPDATE people SET password = ? WHERE id = ?", (f"{salt.hex()}${_hash(password, salt)}", person_id))
        con.execute("DELETE FROM sessions WHERE person = ?", (person_id,))


def _who(name: str) -> str | None:
    """The human with this name or display name, case does not matter."""
    wanted = " ".join((name or "").split()).lower()
    return next((p["id"] for p in company.people() if wanted in (p["name"].lower(), p["display_name"].lower())), None)


def log_in(name: str, password: str) -> str | None:
    """Check the password and return the value for the cookie."""
    person_id = _who(name)
    row = None
    if person_id:
        with db.connect() as con:
            row = con.execute("SELECT password FROM people WHERE id = ?", (person_id,)).fetchone()
    # Compute also without a match, so that the response time does not tell whether the name exists.
    salt, _, expected = (row["password"] if row and row["password"] else "00$").partition("$")
    if not hmac.compare_digest(_hash(password, bytes.fromhex(salt)), expected or "x") or not person_id:
        return None
    token = secrets.token_urlsafe(32)
    with db.connect() as con:
        con.execute("DELETE FROM sessions WHERE until < ?", (time.time(),))
        con.execute("INSERT INTO sessions (token, person, until) VALUES (?, ?, ?)",
                    (_short(token), person_id, time.time() + config.LOGIN_DAYS * 86400))
    return token


def person(token: str | None) -> str | None:
    if not token:
        return None
    with db.connect() as con:
        row = con.execute("SELECT person FROM sessions WHERE token = ? AND until > ?",
                          (_short(token), time.time())).fetchone()
    return row["person"] if row else None


def log_out(token: str | None) -> None:
    if token:
        with db.connect() as con:
            con.execute("DELETE FROM sessions WHERE token = ?", (_short(token),))
