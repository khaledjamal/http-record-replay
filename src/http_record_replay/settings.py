"""Where the HTTP_* settings come from: the environment, else the project's `.env`.

- The project root is the nearest folder from the current folder upwards that has `.git`
  or `pyproject.toml`; if there is none, the current folder.
- Its `.env` is read with a small parser (`KEY=VALUE` lines, optional `export ` and
  quotes, `#` comments), so the package needs no dependency. Only the settings this
  package asks for are read; nothing is put into `os.environ`.
- A value set in the environment wins over the file.
- `PROJECT_ROOT` and `ENV_FILE` can be set to a path to use that one instead (tests do).
"""

import os
from pathlib import Path

ROOT_MARKERS = (".git", "pyproject.toml")
TRUE_VALUES = ("1", "true", "yes", "on")

PROJECT_ROOT = None
ENV_FILE = None


def project_root():
    """The nearest folder from the current folder upwards that holds a ROOT_MARKERS
    entry, else the current folder."""
    if PROJECT_ROOT is not None:
        return Path(PROJECT_ROOT)
    start = Path.cwd()
    for folder in [start, *start.parents]:
        if any((folder / marker).exists() for marker in ROOT_MARKERS):
            return folder
    return start


def env_file():
    return Path(ENV_FILE) if ENV_FILE is not None else project_root() / ".env"


def read_env_file(path):
    """The KEY=VALUE pairs of a .env file, as a dict; {} when the file is missing."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError, PermissionError):
        return {}
    values = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.removeprefix("export ").partition("=")
        value = value.strip()
        if value[:1] in ("'", '"') and value.endswith(value[0]) and len(value) > 1:
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        values[name.strip()] = value
    return values


def setting(name, default=None):
    """The environment's value of name, else the one in the project's .env, else
    default."""
    if name in os.environ:
        return os.environ[name]
    return read_env_file(env_file()).get(name, default)


def flag(name):
    """True when the setting is 1, true, yes or on (any case)."""
    return (setting(name) or "").strip().lower() in TRUE_VALUES


def names(name):
    """A comma-separated setting as a list of stripped, non-empty names."""
    return [item.strip() for item in (setting(name) or "").split(",") if item.strip()]
