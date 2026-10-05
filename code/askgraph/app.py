"""Chainlit chat UI for askgraph (same core as cli.py).

    ./run.sh ui        -> http://localhost:8000

Login: username/password are checked against Stardog itself (ASK {} with Basic auth); the
logged-in Stardog user runs that session's queries. Chat history (left sidebar) lives in
data/askgraph.db via Chainlit's SQLAlchemy data layer. Passwords stay in process memory only.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

import chainlit as cl  # noqa: E402
from chainlit.data.sql_alchemy import SQLAlchemyDataLayer  # noqa: E402
from chainlit.input_widget import Select  # noqa: E402

from askgraph import history as hist  # noqa: E402
from askgraph.agent import run  # noqa: E402
from askgraph.config import load_settings  # noqa: E402
from askgraph.stardog import check_login  # noqa: E402

BASE = load_settings()
SHOW_ROWS = 10
_END = object()

hist.init_db()

# Stardog user -> password, in process memory only. Deliberately NOT in cl.user_session,
# because Chainlit persists the user session into the thread's metadata column.
_CREDENTIALS: dict[str, str] = {}


@cl.data_layer
def get_data_layer():
    # No storage client: file elements are not persisted (Chainlit logs a warning, no error).
    return SQLAlchemyDataLayer(conninfo=hist.conninfo())


@cl.password_auth_callback
async def auth(username: str, password: str):
    username = (username or "").strip()
    ok = await cl.make_async(check_login)(load_settings(), username, password)
    if not ok:
        return None
    _CREDENTIALS[username] = password
    return cl.User(identifier=username, metadata={"provider": "stardog"})


def md_table(columns, rows, limit=SHOW_ROWS) -> str:
    if not columns:
        return ""
    esc = lambda v: str(v).replace("|", "\\|").replace("\n", " ")
    lines = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    lines += ["| " + " | ".join(esc(r.get(c, "")) for c in columns) + " |" for r in rows[:limit]]
    return "\n".join(lines)


def _pick(values, current):
    return values.index(current) if current in values else 0


@cl.set_starters
async def starters(user=None, language=None):
    return [
        cl.Starter(label="Top spenders in Wisconsin", message="Who are the top spenders in Wisconsin?"),
        cl.Starter(label="Big spenders per state", message="How many big spenders are there per state?"),
        cl.Starter(label="Jolynn Bithany's SSN", message="What is Jolynn Bithany's SSN?"),
    ]


def _options():
    models = list(BASE.ui_models) or [BASE.model]
    if BASE.model not in models:
        models.insert(0, BASE.model)
    schemas = list(BASE.schemas) or [BASE.schema]
    return models, schemas


async def _send_settings(chosen: dict | None = None):
    """Model + schema pickers. The Stardog user comes from the login, not from here."""
    chosen = chosen or {}
    models, schemas = _options()
    settings = await cl.ChatSettings([
        Select(id="model", label="Model", values=models,
               initial_index=_pick(models, chosen.get("model") or BASE.model)),
        Select(id="schema", label="Reasoning schema", values=schemas,
               initial_index=_pick(schemas, chosen.get("schema") or BASE.schema)),
    ]).send()
    cl.user_session.set("settings", settings)


def _stardog_user() -> str | None:
    u = cl.user_session.get("user")
    return getattr(u, "identifier", None)


@cl.on_chat_start
async def start():
    await _send_settings()
    cl.user_session.set("history", [])


@cl.on_chat_resume
async def resume(thread):
    """Reopened from the sidebar: rebuild the agent's text history from the saved steps and
    restore the model/schema the thread was using."""
    meta = thread.get("metadata") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except json.JSONDecodeError:
            meta = {}
    chosen = meta.get("settings") or meta.get("chat_settings") or {}
    await _send_settings(chosen if isinstance(chosen, dict) else {})
    cl.user_session.set("history", hist.turns_from_steps(thread.get("steps") or []))


@cl.on_settings_update
async def settings_update(settings):
    cl.user_session.set("settings", settings)   # picked up on the next message


@cl.on_message
async def on_message(message: cl.Message):
    chosen = cl.user_session.get("settings") or {}
    history = cl.user_session.get("history")
    if history is None:
        history = []
        cl.user_session.set("history", history)
    user = _stardog_user()
    password = _CREDENTIALS.get(user or "")
    if not password:
        # e.g. the server restarted while the browser kept its login cookie
        await cl.Message(content="Your Stardog login is no longer in memory (the server restarted). "
                                 "Please log out and log back in.", author="system").send()
        return
    # Reload settings.toml each message too, so config edits apply without a restart.
    s = load_settings().with_overrides(user=user, password=password, model=chosen.get("model"),
                                       schema=chosen.get("schema"))

    events = run(message.content, s, history)
    next_event = cl.make_async(lambda: next(events, _END))
    step = None
    answer = ""
    while (ev := await next_event()) is not _END:
        if ev.kind == "notice":
            await cl.Message(content=f"_{ev.text}_", author="system", metadata={"kind": "notice"}).send()
        elif ev.kind == "tool_call":
            title = "Ontology lookup" if ev.name == "get_ontology" else \
                ("SPARQL with reasoning" if ev.reasoning else "SPARQL")
            step = cl.Step(name=title, type="tool", show_input=False, default_open=False)
            await step.__aenter__()          # opens the step under the current run (sets parent + start)
        elif ev.kind == "tool_result":
            parts = []
            if ev.name == "run_sparql":
                parts.append(f"```sparql\n{ev.query}\n```")
            if ev.error:
                parts.append(f"**Error:** `{ev.error[:800]}`")
            else:
                more = f" (first {SHOW_ROWS} shown)" if ev.row_count > SHOW_ROWS else ""
                parts.append(f"**{ev.row_count} row(s)** in {ev.elapsed:.1f}s{more}")
                parts.append(md_table(ev.columns, ev.rows))
            if step is None:
                step = cl.Step(name=ev.name, type="tool", show_input=False)
                await step.__aenter__()
            step.output = "\n\n".join(p for p in parts if p)
            await step.__aexit__(None, None, None)   # sets end time and sends the update
            step = None
        elif ev.kind == "answer":
            answer = ev.text
        elif ev.kind == "usage":
            cost = "" if ev.cost is None else f" · ${ev.cost:.4f}"
            footer = (f"\n\n---\n`{ev.model}` · *{ev.queries} quer{'y' if ev.queries == 1 else 'ies'}"
                      f" · {ev.total_tokens:,} tokens{cost} · user {s.stardog_user}*")
            # metadata["answer"] lets a resumed chat rebuild the history without the footer
            await cl.Message(content=answer + footer, metadata={"answer": answer}).send()
