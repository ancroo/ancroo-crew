"""Problems that reach a human, as a text key with values, so that each one reads them in their own language."""
from . import i18n


class Problem(Exception):
    def __init__(self, key: str, **params):
        super().__init__(key)
        self.key = key
        self.params = params

    def text(self, lang: str | None) -> str:
        return i18n.t(lang, f"err.{self.key}", **self.params)

    def __str__(self) -> str:
        return self.text(i18n.DEFAULT)


class Rejected(Problem):
    """The request does not fit the current state, e.g. of a conversation or a job."""


class Invalid(Problem, ValueError):
    """A value is not allowed."""


class Unknown(Problem, LookupError):
    """There is nobody or nothing with this ID."""

    def __init__(self, key: str = "unknown", **params):
        super().__init__(key, **params)
