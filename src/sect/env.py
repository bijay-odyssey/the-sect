"""Reading a ``.env`` file, using only the standard library.

``python-dotenv`` does this too, but the base distribution is what every disciple repo
installs and it is deliberately ``httpx`` + ``pydantic`` and nothing else. Forty lines
here is cheaper than a third dependency in every worker.

Loading happens automatically in the three places that read configuration -- the
server's settings, the CLI, and the client -- so ``python -m sect.core.db``, ``sect``
and a locally-run disciple all behave the same without exporting anything per shell.

**The real environment always wins.** A key already present in ``os.environ`` is never
replaced. That matters on a platform like Render, where configuration comes from the
host and a stray ``.env`` in an image has no business overriding it.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_FILENAME = ".env"

#: Point at a specific file instead of searching for one.
ENV_FILE_VAR = "SECT_ENV_FILE"

#: Set to any non-empty value to disable .env loading entirely. The test suite sets
#: this, so a developer's .env -- which may well point at a production database -- can
#: never leak into a test run.
SKIP_VAR = "SECT_SKIP_DOTENV"

_loaded = False


def find_env_file(start: Path | None = None, filename: str = DEFAULT_FILENAME) -> Path | None:
    """The nearest ``filename`` at or above ``start`` (default: the working directory).

    Walking upwards means running a disciple from a subdirectory still finds the file
    at the repository root.
    """
    directory = (start or Path.cwd()).resolve()
    for candidate in (directory, *directory.parents):
        path = candidate / filename
        if path.is_file():
            return path
    return None


def _unescape(value: str) -> str:
    escapes = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}
    out: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            following = value[index + 1]
            out.append(escapes.get(following, "\\" + following))
            index += 2
        else:
            out.append(char)
            index += 1
    return "".join(out)


def parse_env(text: str) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines.

    Handles blank lines, whole-line ``#`` comments, an optional ``export`` prefix, and
    single- or double-quoted values (with ``\\n``-style escapes inside double quotes).

    Inline comments are deliberately **not** stripped from unquoted values. A Postgres
    password may legitimately contain ``#``, and silently truncating a connection string
    is a far worse failure than not supporting a comment style. Quote the value if it
    needs to end in whitespace.
    """
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()

        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        if not key:
            continue

        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            quote = value[0]
            value = value[1:-1]
            if quote == '"':
                value = _unescape(value)
        values[key] = value
    return values


def load_dotenv(path: str | Path | None = None, *, override: bool = False) -> dict[str, str]:
    """Load a ``.env`` into ``os.environ``. Returns only what it actually applied.

    With no ``path``, uses ``$SECT_ENV_FILE`` if set, otherwise searches upwards from
    the working directory. Missing files are not an error -- in production there is no
    ``.env`` and there should not be.
    """
    if os.environ.get(SKIP_VAR):
        return {}

    if path is None:
        configured = os.environ.get(ENV_FILE_VAR)
        path = Path(configured) if configured else find_env_file()
    if path is None:
        return {}

    path = Path(path)
    if not path.is_file():
        return {}

    applied: dict[str, str] = {}
    for key, value in parse_env(path.read_text(encoding="utf-8")).items():
        if override or key not in os.environ:
            os.environ[key] = value
            applied[key] = value
    return applied


def ensure_loaded() -> None:
    """Load once per process. Cheap and safe to call from anywhere."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    load_dotenv()
