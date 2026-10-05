"""UI login check (live Stardog) and the chat-history database (offline)."""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from askgraph import history  # noqa: E402
from askgraph.config import load_settings, stardog_password  # noqa: E402
from askgraph.stardog import check_login  # noqa: E402

S = load_settings()


# ---------------------------------------------------------------- live Stardog login check

def test_login_accepts_hr_user_with_right_password():
    assert check_login(S, "hr_user", stardog_password(S)) is True


def test_login_rejects_wrong_password():
    assert check_login(S, "hr_user", "definitely-not-the-password") is False


def test_login_rejects_unlisted_user_and_empty_password():
    assert check_login(S, "admin", stardog_password(S)) is False      # not in settings.toml [users]
    assert check_login(S, "hr_user", "") is False


# ---------------------------------------------------------------- history db (offline)

def test_history_tables_exist_after_init(tmp_path):
    db = history.init_db(tmp_path / "data" / "h.db")
    history.init_db(db)                                               # idempotent
    with sqlite3.connect(db) as con:
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        step_cols = {r[1] for r in con.execute("PRAGMA table_info(steps)")}
    assert set(history.TABLES) <= names
    assert {"threadId", "parentId", "output", "metadata", "createdAt", "showInput"} <= step_cols
    assert history.conninfo(db).startswith("sqlite+aiosqlite:///")


def test_turns_from_steps_rebuilds_conversation():
    steps = [
        {"type": "user_message", "output": "Who are the top spenders in Wisconsin?", "createdAt": "1"},
        {"type": "tool", "name": "SPARQL", "output": "rows", "createdAt": "2"},
        {"type": "assistant_message", "name": "system", "output": "_retrying_",
         "metadata": '{"kind": "notice"}', "createdAt": "3"},
        {"type": "assistant_message", "output": "Carolyn Inworth\n\n---\n`m` · *1 query*",
         "metadata": '{"answer": "Carolyn Inworth"}', "createdAt": "4"},
        {"type": "user_message", "output": "And who is second?", "createdAt": "5"},
        {"type": "assistant_message", "output": "Renaud Brosio\n\n---\n`m` · *0 queries*", "createdAt": "6"},
    ]
    assert history.turns_from_steps(steps) == [
        {"role": "user", "content": "Who are the top spenders in Wisconsin?"},
        {"role": "assistant", "content": "Carolyn Inworth"},
        {"role": "user", "content": "And who is second?"},
        {"role": "assistant", "content": "Renaud Brosio"},
    ]
