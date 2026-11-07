"""Texts of the user interface and of files the service writes, in German and English.

Each language has one file in app/i18n with the same keys. A key names one text; parameters in braces are filled in
by t(). Texts for a count come as two keys, "<key>.one" and "<key>.other", and are chosen by tn().

The procedures (the prompts) are English only. They are told in which language to answer.
"""
import json
import string
from functools import cache
from pathlib import Path

HERE = Path(__file__).parent / "i18n"
LANGUAGES = ("de", "en")
DEFAULT = "en"
# How the procedures name the language to answer in.
NAMES = {"de": "German", "en": "English"}


@cache
def texts(lang: str) -> dict[str, str]:
    return json.loads((HERE / f"{lang}.json").read_text(encoding="utf-8"))


def language(lang: str | None) -> str:
    return lang if lang in LANGUAGES else DEFAULT


class _Keep(dict):
    """Leaves a placeholder as it is if no value was given, instead of failing."""

    def __missing__(self, key):
        return "{" + key + "}"


def t(lang: str | None, key: str, **params) -> str:
    text = texts(language(lang)).get(key)
    if text is None:
        text = texts(DEFAULT).get(key, key)
    return string.Formatter().vformat(text, (), _Keep(params)) if params else text


def tn(lang: str | None, key: str, n: int, **params) -> str:
    return t(lang, f"{key}.{'one' if n == 1 else 'other'}", n=n, **params)


def from_header(accept: str | None) -> str:
    """The preferred language of a browser that nobody is logged in with yet."""
    for part in (accept or "").split(","):
        code = part.split(";")[0].strip().lower()[:2]
        if code in LANGUAGES:
            return code
    return DEFAULT
