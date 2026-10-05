"""Tool schemas the model sees, and dispatch from a tool call to stardog.py."""
from __future__ import annotations

import json
import time

from . import stardog
from .config import Settings

TOOLS = [
    {"type": "function", "function": {
        "name": "run_sparql",
        "description": "Run one read-only SPARQL SELECT or ASK query against the c360 knowledge graph.",
        "parameters": {"type": "object", "required": ["query"], "properties": {
            "query": {"type": "string", "description": "A complete SPARQL query with PREFIX lines."},
            "reasoning": {"type": "boolean", "description": "true only for derived classes such as :Big_Spender."}}}}},
    {"type": "function", "function": {
        "name": "get_ontology",
        "description": "Return every class and property in the c360 ontology with its label, comment, domain and range.",
        "parameters": {"type": "object", "properties": {}}}},
]


def parse_args(raw: str | None) -> dict | None:
    """Tool arguments as a dict, or None when the model sent invalid JSON."""
    try:
        args = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return None
    return args if isinstance(args, dict) else None


def as_bool(v) -> bool:
    return v.strip().lower() == "true" if isinstance(v, str) else bool(v)


def dispatch(s: Settings, name: str, args: dict | None):
    """Run one tool. Returns (text for the model, QueryResult or None)."""
    if args is None:
        return "Error: tool arguments were not valid JSON.", None
    if name == "get_ontology":
        t0 = time.monotonic()
        text = stardog.get_ontology(s)
        return text, stardog.QueryResult(query="(ontology)", elapsed=time.monotonic() - t0,
                                         columns=["ontology"], rows=[{"ontology": f"{text.count(chr(10)) + 1} terms"}])
    if name == "run_sparql":
        res = stardog.run_sparql(s, args.get("query", ""), as_bool(args.get("reasoning", False)))
        return res.to_tool_text(s.max_rows, s.max_chars), res
    return f"Error: unknown tool {name!r}.", None
