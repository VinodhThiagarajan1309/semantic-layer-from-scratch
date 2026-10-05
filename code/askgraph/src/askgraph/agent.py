"""The agent loop, as a generator of events that any front end (CLI, Chainlit) can render.

    for ev in run("Who are the top spenders in Wisconsin?", settings):
        ev.kind in {"tool_call", "tool_result", "notice", "answer", "usage"}

Events
  tool_call   name, query, reasoning
  tool_result name, query, columns, rows, row_count, elapsed, error
  notice      text            (retry / model fallback messages)
  answer      text
  usage       model, prompt_tokens, completion_tokens, total_tokens, cost (USD or None), queries
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Iterator

import openai

from . import tools
from .config import PROMPTS_DIR, Settings
from .llm import extra_body, make_client


@dataclass
class Event:
    kind: str
    data: dict = field(default_factory=dict)

    def __getattr__(self, k):
        try:
            return self.data[k]
        except KeyError:
            raise AttributeError(k) from None


# ---------------------------------------------------------------- prompt

def build_system_prompt(s: Settings) -> str:
    """prompts/system.md with {placeholders} filled, then every example appended.
    Read from disk on every call, so prompt edits apply without a restart."""
    text = (PROMPTS_DIR / "system.md").read_text()
    values = {"database": s.database, "max_rows": s.max_rows, "schema": s.schema, "endpoint": s.endpoint}
    # Only replace known {names}; SPARQL braces elsewhere are left alone.
    text = re.sub(r"\{(\w+)\}", lambda m: str(values.get(m.group(1), m.group(0))), text)
    examples = sorted((PROMPTS_DIR / "examples").glob("*.sparql"))
    if examples:
        text += "\n\nWorked examples (the # Q: line is the question; the rest is a query that answers it):\n"
        for p in examples:
            text += f"\n--- {p.stem} ---\n{p.read_text().strip()}\n"
    return text


# ---------------------------------------------------------------- model call with retry + fallback

class EmptyResponse(Exception):
    """The provider answered 200 but with no choices (seen on free OpenRouter models)."""


RETRYABLE = (EmptyResponse, openai.RateLimitError, openai.InternalServerError, openai.APIConnectionError, openai.APITimeoutError)


def _models(s: Settings) -> list[str]:
    out = [s.model]
    out += [m for m in s.fallback_models if m not in out]
    return out


def _complete(client, s: Settings, models: list[str], messages: list, notices: list):
    """Call the first model that works. Each model gets one retry (with backoff) on a
    transient error, then we fall through to the next model. Mutates `models` so the rest
    of the question sticks with the model that worked."""
    last = None
    while models:
        model = models[0]
        for attempt in (1, 2):
            try:
                resp = client.chat.completions.create(
                    model=model, messages=messages, tools=tools.TOOLS, extra_body=extra_body(s))
                if not resp.choices:
                    raise EmptyResponse(f"empty response from {model}")
                return model, resp
            except (openai.APIStatusError, *RETRYABLE) as e:
                last = e
                status = getattr(e, "status_code", None)
                transient = isinstance(e, RETRYABLE) or (status or 0) >= 500
                if attempt == 1 and transient:
                    wait = s.retry_backoff_s
                    notices.append(f"{model}: {type(e).__name__} ({status}); retrying in {wait:.0f}s")
                    time.sleep(wait)
                    continue
                break
        models.pop(0)
        if models:
            notices.append(f"{model} failed ({type(last).__name__}); falling back to {models[0]}")
    raise RuntimeError(f"All models failed. Last error: {last}")


def _cost(s: Settings, model: str, usage) -> float | None:
    reported = getattr(usage, "cost", None)
    if reported is None and getattr(usage, "model_extra", None):
        reported = usage.model_extra.get("cost")
    if reported is not None:
        return float(reported)
    if model.endswith(":free"):
        return 0.0
    price = s.prices.get(model)
    if price:
        return (usage.prompt_tokens * price[0] + usage.completion_tokens * price[1]) / 1e6
    return None


# ---------------------------------------------------------------- the loop

def run(question: str, s: Settings, history: list[dict] | None = None, client=None) -> Iterator[Event]:
    """Answer one question. `history` is prior user/assistant turns (plain text); the new
    question and final answer are appended to it, so a chat front end can pass the same list."""
    client = client or make_client(s)
    history = history if history is not None else []
    messages = [{"role": "system", "content": build_system_prompt(s)}, *history,
                {"role": "user", "content": question}]
    models = _models(s)
    used_model = models[0]
    tok_in = tok_out = 0
    cost: float | None = 0.0
    queries = 0
    answer = None

    try:
        for _ in range(s.max_tool_turns):
            notices: list[str] = []
            try:
                used_model, resp = _complete(client, s, models, messages, notices)
            finally:
                for n in notices:
                    yield Event("notice", {"text": n})
            if resp.usage:
                tok_in += resp.usage.prompt_tokens or 0
                tok_out += resp.usage.completion_tokens or 0
                c = _cost(s, used_model, resp.usage)
                cost = None if (c is None or cost is None) else cost + c
            msg = resp.choices[0].message
            if not msg.tool_calls:
                answer = (msg.content or "").strip() or "(the model returned an empty answer)"
                break
            messages.append(msg.model_dump(exclude_none=True))
            for call in msg.tool_calls:
                args = tools.parse_args(call.function.arguments)
                name = call.function.name
                if name == "run_sparql":
                    queries += 1
                yield Event("tool_call", {"name": name, "query": (args or {}).get("query", ""),
                                          "reasoning": tools.as_bool((args or {}).get("reasoning", False))})
                text, res = tools.dispatch(s, name, args)
                yield Event("tool_result", {
                    "name": name,
                    "query": res.query if res else "",
                    "columns": res.columns if res else [],
                    "rows": res.rows if res else [],
                    "row_count": res.row_count if res else 0,
                    "elapsed": res.elapsed if res else 0.0,
                    "error": res.error if res else text,
                })
                messages.append({"role": "tool", "tool_call_id": call.id, "content": text})
        else:
            answer = "Stopped: too many tool calls without a final answer. Try rephrasing the question."
    except RuntimeError as e:
        answer = f"Error: {e}"

    history += [{"role": "user", "content": question}, {"role": "assistant", "content": answer}]
    yield Event("answer", {"text": answer})
    yield Event("usage", {"model": used_model, "prompt_tokens": tok_in, "completion_tokens": tok_out,
                          "total_tokens": tok_in + tok_out, "cost": cost, "queries": queries})


def ask(question: str, s: Settings) -> str:
    """Convenience: just the final answer text."""
    return next(ev.text for ev in run(question, s) if ev.kind == "answer")
