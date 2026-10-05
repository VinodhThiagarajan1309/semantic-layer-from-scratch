# askgraph

A small ChatGPT-style agent over the c360 Stardog knowledge graph. You ask in English; an LLM
writes SPARQL, calls a read-only `run_sparql` tool, and answers from the returned rows.
Ported from the book's single-file `agent.py`, split into modules, with a Chainlit UI and a CLI
sharing one core.

```
question -> LLM (OpenRouter/OpenAI, tool calling) -> run_sparql -> Stardog kit-c360
         -> virtual graphs (Aurora safe/PII, Redshift) -> rows -> LLM -> answer
```

## Run

```bash
./run.sh ui                                             # chat UI at http://localhost:8000 (log in first)
./run.sh cli --user hr -v "Who are the top spenders in Wisconsin?"
./run.sh cli --user marketing "What is Jolynn Bithany's SSN?"
./run.sh cli                                            # interactive; empty line quits
./run.sh test                                           # live golden tests
```

CLI flags: `-v` (show each SPARQL, row count, time, first rows), `--user hr|marketing`,
`--model <id>`, `--schema <name>`. The first query of a session can take 20-50 s while
Aurora/Redshift wake up.

## UI login and chat history

- **Login.** The UI asks for a username and password. Valid usernames are the Stardog users in
  `config/settings.toml` `[users]` (`hr_user`, `marketing_user`). The password is checked against
  Stardog itself: the app runs `ASK {}` on `kit-c360` with those Basic-auth credentials, and
  HTTP 200 means you are in. No password is stored anywhere new: it is kept in the server
  process's memory only (not in the database, not in the user/thread metadata, not on disk).
  The logged-in user is the Stardog user for every query in that session (there is no user
  picker in the settings panel any more; model and schema pickers remain). If the server restarts
  while your browser is still logged in, the app asks you to log out and log back in.
- **History.** Past conversations are listed in the left sidebar; click one to reopen it and keep
  asking follow-ups (the agent's conversation is rebuilt from the saved messages, and the
  thread's model/schema choice is restored). Each user sees only their own threads: Chainlit
  filters the list by the logged-in user and refuses to open another user's thread.
- **Where it is stored.** `data/askgraph.db`, a local SQLite file used by Chainlit's SQLAlchemy
  data layer (`sqlite+aiosqlite`). Tables `users, threads, steps, elements, feedbacks` are created
  automatically on start (`src/askgraph/history.py`). Uploaded files are not persisted (no storage
  client is configured; Chainlit logs a warning about that, which is expected). `data/` is git-ignored.
- **Wipe history.** Stop the UI and delete the file: `rm data/askgraph.db` (recreated empty on
  the next start). One thread can also be deleted from its `...` menu in the sidebar.
- **Auth secret.** Chainlit signs the login cookie with `CHAINLIT_AUTH_SECRET`. It lives outside
  this folder in `~/.config/semantic-layer/chainlit.env` (`export CHAINLIT_AUTH_SECRET='...'`,
  mode 600). `./run.sh ui` sources it, and creates it with `chainlit create-secret` if it is
  missing. Changing it logs everyone out.

The CLI is unchanged: no login, it picks the user with `--user` and reads the password from the
secret file below.

## Setup (once)

This is the code for Chapter 15 of *The Semantic Layer, From Scratch*. It expects the Stardog
database `kit-c360` with the virtual graphs, rules and the `hr_user` / `marketing_user` users
from the book's earlier build steps.

```bash
cd askgraph
python3.13 -m venv .venv                 # Python 3.11 or newer works
.venv/bin/pip install -r requirements.txt
```

1. **Your endpoint.** Edit `config/settings.toml` and replace `<your-endpoint>` in
   `[stardog] endpoint = "https://<your-endpoint>.stardog.cloud:5820"`, or export
   `STARDOG_ENDPOINT=https://<your-endpoint>.stardog.cloud:5820` (the variable wins).
2. **Your OpenRouter key**, stored outside the project (zsh):

   ```bash
   mkdir -p ~/.config/semantic-layer
   read -rs "KEY?OpenRouter key: "; echo
   printf 'SEMANTIC_LAYER_OPENROUTER_KEY=%q\n' "$KEY" > ~/.config/semantic-layer/openrouter.env
   chmod 600 ~/.config/semantic-layer/openrouter.env; unset KEY
   ```
3. **The demo users' password** (for the CLI and the tests; the web UI asks at login instead).
   Same recipe, by default in `~/.config/semantic-layer/stardog-users.env`; change
   `[secrets] stardog_users_env` in settings.toml to keep it somewhere else:

   ```bash
   read -rs "PW?Password of hr_user and marketing_user: "; echo
   printf 'STARDOG_DEMO_USERS_PASSWORD=%q\n' "$PW" > ~/.config/semantic-layer/stardog-users.env
   chmod 600 ~/.config/semantic-layer/stardog-users.env; unset PW
   ```
4. `./run.sh test`, then `./run.sh ui`.

## Files

| Path | What it does |
|---|---|
| `run.sh` | Launcher: `ui`, `cli`, `test`. Uses `.venv`, sets `PYTHONPATH=src`. |
| `config/settings.toml` | Provider, default + fallback models, UI model list, Stardog endpoint/db/schema, timeouts, LIMIT/row caps, max tool turns, users map, secret file *paths*. |
| `prompts/system.md` | The system prompt. Editable; `{database}` and `{max_rows}` are filled in by code. |
| `prompts/examples/*.sparql` | Few-shot examples, each starting with `# Q: <question>`. All are appended to the prompt. Add a file to teach a new pattern. |
| `src/askgraph/config.py` | Loads settings.toml and secrets into a `Settings` dataclass. |
| `src/askgraph/llm.py` | OpenAI-SDK client for OpenRouter or OpenAI. |
| `src/askgraph/stardog.py` | Read-only guard (SELECT/ASK only, LIMIT capped), `run_sparql`, `get_ontology`. |
| `src/askgraph/tools.py` | Tool schemas shown to the model and dispatch to stardog.py. |
| `src/askgraph/agent.py` | The loop as a generator of events: `tool_call`, `tool_result`, `notice`, `answer`, `usage`. Retries once on 429/5xx, then falls back to the next model. |
| `app.py` | Chainlit UI: Stardog-verified login, history data layer, chat resume, one collapsible step per query, settings panel (model/schema), starters, footer with model/queries/tokens/user. |
| `src/askgraph/history.py` | SQLite schema for Chainlit's SQLAlchemy data layer (`data/askgraph.db`), created on start; rebuilds agent history from saved steps on resume. |
| `cli.py` | Terminal front end on the same core. |
| `tests/test_golden.py` | Guard + prompt tests (offline); Stardog golden results using the example queries; one end-to-end agent test. |
| `tests/test_history_auth.py` | Login check against Stardog (right / wrong password, unlisted user); history tables created; history rebuilt from steps. |
| `chainlit.md`, `.chainlit/config.toml` | Chainlit's readme panel and UI config. |

Prompt files and settings.toml are re-read for every question, so edits apply without restarting.

## Secrets (paths only, nothing secret lives in this folder)

| Secret | Env var (wins) | File fallback |
|---|---|---|
| OpenRouter key | `OPENROUTER_API_KEY` | `~/.config/semantic-layer/openrouter.env` (`SEMANTIC_LAYER_OPENROUTER_KEY=`) |
| OpenAI key (provider = "openai") | `OPENAI_API_KEY` | none |
| Stardog demo users' password (CLI, tests) | `STARDOG_PASSWORD` | `~/.config/semantic-layer/stardog-users.env` (`STARDOG_DEMO_USERS_PASSWORD=`), path set in settings.toml `[secrets] stardog_users_env` |
| Chainlit auth secret (UI login cookie) | `CHAINLIT_AUTH_SECRET` | `~/.config/semantic-layer/chainlit.env` (created by `./run.sh ui`) |

Other env overrides: `ASKGRAPH_MODEL`, `ASKGRAPH_PROVIDER`, `ASKGRAPH_USER`, `STARDOG_ENDPOINT`,
`STARDOG_DB`, `STARDOG_SCHEMA`, `CHAINLIT_ENV_FILE` (other location for the auth-secret file).
The UI does not use the password file: users type their Stardog password at login.

## Users and security

Named-graph security is enforced by Stardog, not by the agent. `hr` = `hr_user` can read
`virtual://aurora_c360_pii`; `marketing` = `marketing_user` cannot, so the same SSN query
returns zero rows and the agent says it cannot see the value.

## Models

Default `nvidia/nemotron-3-super-120b-a12b:free` (good tool calling, free). Fallback
`openai/gpt-4o-mini` (paid). Avoid `qwen/qwen3.8-27b:free`: it garbles tool arguments.
