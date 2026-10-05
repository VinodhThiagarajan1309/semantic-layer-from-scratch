#!/usr/bin/env python3
"""Terminal front end: same core as the UI.

  python cli.py [-v] [--user hr|marketing] [--model ID] [--schema NAME] "question"
  (no question = interactive; empty line quits)
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from askgraph.agent import run  # noqa: E402
from askgraph.config import load_settings  # noqa: E402


def render(question, s, history, verbose):
    for ev in run(question, s, history):
        if ev.kind == "notice":
            print(f"[notice] {ev.text}", file=sys.stderr)
        elif ev.kind == "tool_call" and verbose:
            print(f"\n--- {ev.name} (reasoning={ev.reasoning}) ---", file=sys.stderr)
        elif ev.kind == "tool_result" and verbose:
            if ev.name == "run_sparql":
                print(ev.query, file=sys.stderr)
            if ev.error:
                print(f"--- error: {ev.error[:500]}", file=sys.stderr)
            else:
                print(f"--- {ev.row_count} row(s) in {ev.elapsed:.1f}s", file=sys.stderr)
                for r in ev.rows[:10]:
                    print("    " + " | ".join(str(r.get(c, "")) for c in ev.columns), file=sys.stderr)
        elif ev.kind == "answer":
            print(ev.text, flush=True)
        elif ev.kind == "usage":
            cost = "n/a" if ev.cost is None else f"${ev.cost:.4f}"
            print(f"\n[{ev.model} | {ev.queries} quer{'y' if ev.queries == 1 else 'ies'} | "
                  f"{ev.total_tokens} tokens | cost {cost}]", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description="Ask the c360 knowledge graph a question.")
    ap.add_argument("-v", "--verbose", action="store_true", help="show each SPARQL query and result size")
    ap.add_argument("--user", help="Stardog user short name (hr | marketing) or full user name")
    ap.add_argument("--model", help="LLM model id")
    ap.add_argument("--schema", help="reasoning schema")
    ap.add_argument("question", nargs="*")
    a = ap.parse_args()
    s = load_settings().with_overrides(user=a.user, model=a.model, schema=a.schema)
    print(f"askgraph: user={s.stardog_user} model={s.model}", file=sys.stderr)
    history = []
    if a.question:
        render(" ".join(a.question), s, history, a.verbose)
        return
    while True:
        try:
            q = input("\n> ").strip()
        except EOFError:
            break
        if not q:
            break
        render(q, s, history, a.verbose)


if __name__ == "__main__":
    main()
