"""Load config/settings.toml plus secrets (env vars first, then private files).

Secrets are read at runtime only and are never written anywhere.
"""
from __future__ import annotations

import os
import re
import shlex
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[2]          # .../askgraph
SETTINGS_FILE = ROOT / "config" / "settings.toml"
PROMPTS_DIR = ROOT / "prompts"


@dataclass(frozen=True)
class Settings:
    # LLM
    provider: str
    model: str
    fallback_models: tuple[str, ...]
    ui_models: tuple[str, ...]
    retry_backoff_s: float
    max_tool_turns: int
    prices: dict = field(default_factory=dict)
    # Stardog
    endpoint: str = ""
    database: str = ""
    schema: str = ""
    schemas: tuple[str, ...] = ()
    timeout_s: int = 120
    max_rows: int = 100
    max_chars: int = 8000
    # Users
    user: str = "hr"                                   # short name, e.g. "hr"
    users: dict = field(default_factory=dict)          # short name -> Stardog user
    # Secret file locations (paths only)
    openrouter_env: str = ""
    stardog_users_env: str = ""
    # Password for this run, set in memory by the UI after login (never loaded from or
    # written to disk). None = use STARDOG_PASSWORD / the private file (CLI, tests).
    password: str | None = field(default=None, repr=False, compare=False)

    @property
    def stardog_user(self) -> str:
        """Native Stardog user for the selected short name (a full user name also works)."""
        return self.users.get(self.user, self.user)

    def with_overrides(self, **kw) -> "Settings":
        """Copy with some fields changed, ignoring None values (e.g. user=None)."""
        return replace(self, **{k: v for k, v in kw.items() if v is not None})


def load_settings(path: Path | str | None = None) -> Settings:
    data = tomllib.loads(Path(path or SETTINGS_FILE).read_text())
    llm, sd, users, sec = data["llm"], data["stardog"], dict(data["users"]), data.get("secrets", {})
    default_user = users.pop("default", "hr")
    return Settings(
        provider=os.getenv("ASKGRAPH_PROVIDER", llm.get("provider", "openrouter")).lower(),
        model=os.getenv("ASKGRAPH_MODEL", llm["model"]),
        fallback_models=tuple(llm.get("fallback_models", [])),
        ui_models=tuple(llm.get("ui_models", [llm["model"]])),
        retry_backoff_s=float(llm.get("retry_backoff_s", 8)),
        max_tool_turns=int(llm.get("max_tool_turns", 8)),
        prices=dict(llm.get("prices", {})),
        endpoint=os.getenv("STARDOG_ENDPOINT", sd["endpoint"]).rstrip("/"),
        database=os.getenv("STARDOG_DB", sd["database"]),
        schema=os.getenv("STARDOG_SCHEMA", sd["schema"]),
        schemas=tuple(sd.get("schemas", [sd["schema"]])),
        timeout_s=int(sd.get("timeout_s", 120)),
        max_rows=int(sd.get("max_rows", 100)),
        max_chars=int(sd.get("max_chars", 8000)),
        user=os.getenv("ASKGRAPH_USER", default_user),
        users=users,
        openrouter_env=sec.get("openrouter_env", ""),
        stardog_users_env=sec.get("stardog_users_env", ""),
    )


# ---------------------------------------------------------------- secrets

def _resolve(p: str) -> Path:
    path = Path(os.path.expanduser(p))
    return path if path.is_absolute() else (ROOT / path).resolve()


def _read_var(file: str, name: str) -> str | None:
    """Return NAME's value from a shell-style env file (`export NAME=...` or `NAME=...`)."""
    if not file:
        return None
    path = _resolve(file)
    if not path.is_file():
        return None
    pat = re.compile(rf"^\s*(?:export\s+)?{re.escape(name)}\s*=\s*(.*?)\s*$")
    for line in path.read_text().splitlines():
        m = pat.match(line)
        if m:
            raw = m.group(1).strip()
            try:      # undo shell quoting, e.g. the backslashes `printf %q` adds
                parts = shlex.split(raw)
                return parts[0] if len(parts) == 1 else raw
            except ValueError:
                return raw.strip("'\"")
    return None


def llm_api_key(s: Settings) -> str:
    if s.provider == "openai":
        key = os.getenv("OPENAI_API_KEY")
        src = "OPENAI_API_KEY"
    else:
        key = (os.getenv("OPENROUTER_API_KEY") or os.getenv("SEMANTIC_LAYER_OPENROUTER_KEY")
               or _read_var(s.openrouter_env, "SEMANTIC_LAYER_OPENROUTER_KEY"))
        src = f"OPENROUTER_API_KEY or {s.openrouter_env}"
    if not key:
        raise RuntimeError(f"No API key found for provider {s.provider!r} (looked in {src}).")
    return key


def stardog_password(s: Settings) -> str:
    if s.password:
        return s.password
    pw = os.getenv("STARDOG_PASSWORD") or _read_var(s.stardog_users_env, "STARDOG_DEMO_USERS_PASSWORD")
    if not pw:
        raise RuntimeError(f"No Stardog password (set STARDOG_PASSWORD or fill {s.stardog_users_env}).")
    return pw
