"""Talk to Stardog: a read-only guard, run_sparql, and get_ontology (ported from the book's agent.py)."""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

import requests

from .config import Settings, stardog_password

ONTOLOGY_QUERY = """PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX so: <https://schema.org/>
SELECT ?term ?kind (SAMPLE(?l) AS ?label) (SAMPLE(?cm) AS ?comment)
       (GROUP_CONCAT(DISTINCT COALESCE(STR(?sup), "")) AS ?parent)
       (GROUP_CONCAT(DISTINCT COALESCE(STR(?d), "")) AS ?domain)
       (GROUP_CONCAT(DISTINCT COALESCE(STR(?r), "")) AS ?range)
WHERE {
  VALUES ?g { <urn:stardog:training-c360:1.0:model> <urn:stardog:c360_extended_rules:1.0:model> }
  VALUES ?kind { owl:Class owl:ObjectProperty owl:DatatypeProperty }
  GRAPH ?g { ?term a ?kind .
    OPTIONAL { ?term rdfs:label ?l }  OPTIONAL { ?term rdfs:comment ?cm }  OPTIONAL { ?term rdfs:subClassOf ?sup }
    OPTIONAL { ?term so:domainIncludes ?d }  OPTIONAL { ?term so:rangeIncludes ?r } }
} GROUP BY ?term ?kind ORDER BY ?kind ?term"""

FORBIDDEN = re.compile(r"\b(INSERT|DELETE|DROP|CLEAR|LOAD|CREATE|ADD|MOVE|COPY)\b", re.I)
READ_FORM = re.compile(r"^\s*((PREFIX|BASE)\s[^\n]*\n\s*)*(SELECT|ASK)\b", re.I)
TRAILING_LIMIT = re.compile(r"\bLIMIT\s+(\d+)\s*(OFFSET\s+\d+\s*)?$", re.I)


def guard(query: str, max_rows: int = 100) -> str:
    """Reject anything that is not a plain read, and make sure a SELECT has a sane LIMIT."""
    body = re.sub(r"(?m)^\s*#.*$", "", query).strip()     # drop whole-line comments
    # Check a copy with IRIs, string literals and inline comments blanked out, so that
    # <...#> or "Create date" can't confuse the keyword test.
    check = re.sub(r"#[^\n]*", " ", re.sub(r"<[^<>\s]*>|\"[^\"]*\"|'[^']*'", " ", body))
    if FORBIDDEN.search(check):
        raise ValueError("Rejected: only read-only SELECT/ASK queries are allowed.")
    if not READ_FORM.match(check):
        raise ValueError("Rejected: the query must be a SELECT or ASK (after PREFIX lines).")
    if re.search(r"\bSELECT\b", check, re.I):
        m = TRAILING_LIMIT.search(check.rstrip())
        if not m:
            body += f"\nLIMIT {max_rows}"
        elif int(m.group(1)) > max_rows:          # cap the last (outermost) LIMIT
            body = re.sub(r"(?is)(.*\bLIMIT\s+)\d+", rf"\g<1>{max_rows}", body, count=1)
    return body


@dataclass
class QueryResult:
    query: str                       # the query actually sent (after the guard)
    reasoning: bool = False
    columns: list[str] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)
    ask: bool | None = None
    elapsed: float = 0.0
    error: str | None = None

    @property
    def row_count(self) -> int:
        return len(self.rows)

    def to_tool_text(self, max_rows: int = 100, max_chars: int = 8000) -> str:
        """What the model sees. Errors come back as text it can react to."""
        if self.error:
            return self.error
        if self.ask is not None:
            return json.dumps({"ask": self.ask})
        text = json.dumps({"columns": self.columns, "rows": self.rows[:max_rows], "row_count": self.row_count})
        return text if len(text) <= max_chars else text[:max_chars] + " ... [truncated]"


def require_endpoint(s: Settings) -> None:
    """Fail with a clear message while settings.toml still has the placeholder endpoint."""
    if not s.endpoint or "<your-endpoint>" in s.endpoint:
        raise RuntimeError("Set your Stardog endpoint: [stardog] endpoint in config/settings.toml, "
                           "or export STARDOG_ENDPOINT=https://<name>.stardog.cloud:5820")


def post_query(s: Settings, query: str, reasoning: bool = False) -> dict:
    """POST one query to {endpoint}/{db}/query and return the SPARQL JSON results."""
    require_endpoint(s)
    data = {"query": query, "reasoning": str(reasoning).lower(), "timeout": s.timeout_s * 1000}
    if reasoning and s.schema and s.schema != "default":
        data["schema"] = s.schema    # the extended rules; the default schema is empty
    r = requests.post(
        f"{s.endpoint}/{s.database}/query", data=data,
        headers={"Accept": "application/sparql-results+json"},
        auth=(s.stardog_user, stardog_password(s)), timeout=(10, s.timeout_s + 10))
    if r.status_code != 200:
        raise RuntimeError(f"Stardog error {r.status_code}: {r.text[:1500]}")
    return r.json()


def check_login(s: Settings, username: str, password: str) -> bool:
    """True when Stardog accepts these Basic-auth credentials for a cheap read (ASK {}) on the
    configured database. Only users listed in settings.toml [users] may log in. The password
    is used for this one request and is not stored here."""
    if not username or not password or username not in set(s.users.values()):
        return False
    require_endpoint(s)
    try:
        r = requests.post(
            f"{s.endpoint}/{s.database}/query", data={"query": "ASK {}"},
            headers={"Accept": "application/sparql-results+json"},
            auth=(username, password), timeout=(10, 30))
    except requests.RequestException:
        return False
    return r.status_code == 200


def run_sparql(s: Settings, query: str, reasoning: bool = False) -> QueryResult:
    """Tool 1. Guard, run, flatten. Never raises: failures land in QueryResult.error."""
    res = QueryResult(query=query, reasoning=reasoning)
    t0 = time.monotonic()
    try:
        res.query = guard(query, s.max_rows)
        js = post_query(s, res.query, reasoning)
    except (ValueError, RuntimeError, requests.RequestException) as e:
        res.error = str(e)
        res.elapsed = time.monotonic() - t0
        return res
    res.elapsed = time.monotonic() - t0
    if "boolean" in js:
        res.ask = bool(js["boolean"])
        return res
    res.columns = js["head"]["vars"]
    res.rows = [{k: v["value"] for k, v in b.items()} for b in js["results"]["bindings"]]
    return res


def get_ontology(s: Settings) -> str:
    """Tool 2. Read the T-Box from the model graphs (no warehouse involved) as compact lines."""
    try:
        js = post_query(s, ONTOLOGY_QUERY)
    except (RuntimeError, requests.RequestException) as e:
        return str(e)

    def short(x: str) -> str:
        return (x.replace("tag:stardog:api:ecomm:", ":").replace("http://www.w3.org/2001/XMLSchema#", "xsd:")
                 .replace("http://www.w3.org/2002/07/owl#", ""))

    lines = []
    for b in js["results"]["bindings"]:
        v = {k: short(x["value"]).strip() for k, x in b.items()}
        line = f'{v["term"]} ({v["kind"]}) "{v.get("label", "")}"'
        if v.get("parent"):
            line += f' subClassOf {v["parent"]}'
        if v.get("domain"):
            line += f' {v["domain"]} -> {v.get("range", "")}'
        if v.get("comment"):
            line += f' -- {v["comment"]}'
        lines.append(line)
    return "\n".join(lines)
