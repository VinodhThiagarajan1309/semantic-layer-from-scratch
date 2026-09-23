#!/usr/bin/env python3
"""agent.py - a plain-English question in, a grounded answer out.

question -> LLM (OpenAI or OpenRouter, tool calling) -> run_sparql -> Stardog kit-c360
         -> Aurora + Redshift (virtual graphs) -> rows -> LLM -> answer

Environment variables (never hardcode secrets):
  STARDOG_ENDPOINT   e.g. https://<your-endpoint>.stardog.cloud:5820
  STARDOG_USER       a native Stardog user (stardog_admin, or better a read-only user)
  STARDOG_PASSWORD   that user's password
  STARDOG_DB         optional, default kit-c360
  STARDOG_SCHEMA     optional reasoning schema, default c360_extended_rules (Chapter 10)
  LLM_PROVIDER       optional: "openai" (default) or "openrouter"
  OPENAI_API_KEY     when LLM_PROVIDER=openai
  OPENROUTER_API_KEY when LLM_PROVIDER=openrouter
  AGENT_MODEL        optional model id override

Usage:  python agent.py -v "Who are our top spenders in Wisconsin?"   (-v prints each SPARQL)
"""
import json, os, re, sys
import requests
from openai import OpenAI

ENDPOINT = os.environ["STARDOG_ENDPOINT"].rstrip("/")
DB = os.getenv("STARDOG_DB", "kit-c360")
SCHEMA = os.getenv("STARDOG_SCHEMA", "c360_extended_rules")
AUTH = (os.environ["STARDOG_USER"], os.environ["STARDOG_PASSWORD"])
MAX_ROWS, MAX_CHARS, MAX_STEPS, TIMEOUT_S = 100, 8000, 8, 90
VERBOSE = "-v" in sys.argv

if os.getenv("LLM_PROVIDER", "openai").lower() == "openrouter":
    client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=os.environ["OPENROUTER_API_KEY"])
    MODEL = os.getenv("AGENT_MODEL", "anthropic/claude-sonnet-4.6")
else:
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    MODEL = os.getenv("AGENT_MODEL", "gpt-4.1")

SYSTEM_PROMPT = """You answer business questions about customers and purchases by writing
SPARQL for a Stardog knowledge graph (database kit-c360) and calling the run_sparql tool.
Never guess numbers: every figure in your answer must come from a tool result.

PREFIX : <tag:stardog:api:ecomm:>      (also declare xsd: and rdfs: when you use them)

Data lives in three virtual graphs (live SQL underneath). Always wrap patterns in GRAPH:
  GRAPH <virtual://aurora_c360_safe>
    :Customer  :name :firstName :lastName :email :phone, :address -> :Address, :hasRewards -> :RewardsAccount
    :Address   :streetAddress :city :state :zipCode   (state is the full name, e.g. "Wisconsin")
    :CreditCard :cardType, :cardHolder -> :Customer   (no card numbers here)
    :RewardsAccount :accountId :openDate (xsd:date)
  GRAPH <virtual://redshift_c360>
    :Order  :purchasedBy -> :Customer, :itemPurchased -> :Product, :cardUsed -> :CreditCard,
            :rewardsAccountUsed -> :RewardsAccount, :purchasePrice (xsd:decimal, unit price),
            :quantity (xsd:integer), :purchaseDate (xsd:date), :purchaseTime (string)
    :Product :name :description, :inCategory -> :Category, :vendor -> :Vendor, :msrp -> :UnitPrice
    :UnitPrice :hasPrice (xsd:float) :hasCurrency     :Category :name     :Vendor :name, :industry -> :Industry
  GRAPH <virtual://aurora_c360_pii>  :ssn, :cardNumber. Sensitive: do NOT query unless explicitly asked.
Call get_ontology if you need labels, comments or a term not listed here.

Customer IRIs are identical in both warehouses, so a variable shared by two GRAPH blocks IS
the join. Derived classes (need reasoning=true): :Big_Spender, :Large_Order, :2022_Order,
:2022_Orderer, :Sports_Category_Shopper. Leave reasoning false otherwise (it is slower).
A reasoning query must use FROM <virtual://aurora_c360_safe> FROM <virtual://redshift_c360>
instead of GRAPH blocks, so the rules can join across both warehouses.

Example. "Top 3 customers by spend, with state":
SELECT ?name ?state (SUM(?p * ?q) AS ?spend) WHERE {
  GRAPH <virtual://aurora_c360_safe> { ?c :name ?name ; :address ?a . ?a :state ?state }
  GRAPH <virtual://redshift_c360> { ?o :purchasedBy ?c ; :purchasePrice ?p ; :quantity ?q }
} GROUP BY ?name ?state ORDER BY DESC(?spend) LIMIT 3

Rules: read-only SELECT/ASK only; always add LIMIT (<= 100); spend = SUM(price * quantity);
compare dates with "2022-01-01"^^xsd:date; cast with xsd:decimal()/xsd:integer() if a comparison
misbehaves; return names, not IRIs. If a query errors or returns nothing, read the result, fix
the query and retry. Answer concisely, show the key numbers, and say which data you used."""

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


def guard(query: str) -> str:
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
            body += f"\nLIMIT {MAX_ROWS}"
        elif int(m.group(1)) > MAX_ROWS:          # cap the last (outermost) LIMIT
            body = re.sub(r"(?is)(.*\bLIMIT\s+)\d+", rf"\g<1>{MAX_ROWS}", body, count=1)
    return body


def stardog(query: str, reasoning: bool = False) -> dict:
    """POST one query to the read-only query endpoint and return the SPARQL JSON results."""
    data = {"query": query, "reasoning": str(reasoning).lower(), "timeout": TIMEOUT_S * 1000}
    if reasoning:
        data["schema"] = SCHEMA  # the Chapter 10 rules; the default schema is empty
    r = requests.post(
        f"{ENDPOINT}/{DB}/query", data=data,
        headers={"Accept": "application/sparql-results+json"},
        auth=AUTH, timeout=(10, TIMEOUT_S + 10))
    if r.status_code != 200:
        raise RuntimeError(f"Stardog error {r.status_code}: {r.text[:1500]}")
    return r.json()


def run_sparql(query: str, reasoning: bool = False) -> str:
    """Tool 1. Guard, run, flatten, truncate. Errors come back as text the model can react to."""
    try:
        q = guard(query)
        if VERBOSE:
            print(f"\n--- SPARQL (reasoning={reasoning}) ---\n{q}", file=sys.stderr)
        res = stardog(q, reasoning)
    except (ValueError, RuntimeError, requests.RequestException) as e:
        return str(e)
    if "boolean" in res:
        return json.dumps({"ask": res["boolean"]})
    rows = [{k: v["value"] for k, v in b.items()} for b in res["results"]["bindings"]]
    if VERBOSE:
        print(f"--- {len(rows)} row(s) ---", file=sys.stderr)
    text = json.dumps({"columns": res["head"]["vars"], "rows": rows[:MAX_ROWS], "row_count": len(rows)})
    return text if len(text) <= MAX_CHARS else text[:MAX_CHARS] + " ... [truncated]"


def get_ontology() -> str:
    """Tool 2. Read the T-Box from the model graphs (no warehouse involved) as compact lines."""
    try:
        res = stardog(ONTOLOGY_QUERY)
    except (RuntimeError, requests.RequestException) as e:
        return str(e)
    short = lambda s: (s.replace("tag:stardog:api:ecomm:", ":").replace("http://www.w3.org/2001/XMLSchema#", "xsd:")
                        .replace("http://www.w3.org/2002/07/owl#", ""))
    lines = []
    for b in res["results"]["bindings"]:
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


def ask(question: str) -> str:
    """The agent loop: call the model, run the tools it asks for, repeat until it answers."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": question}]
    for _ in range(MAX_STEPS):
        msg = client.chat.completions.create(model=MODEL, messages=messages, tools=TOOLS).choices[0].message
        if not msg.tool_calls:
            return msg.content or "(no answer)"
        messages.append(msg.model_dump(exclude_none=True))
        for call in msg.tool_calls:
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = None
            if args is None:
                result = "Error: tool arguments were not valid JSON."
            elif call.function.name == "get_ontology":
                result = get_ontology()
            else:
                result = run_sparql(args.get("query", ""), bool(args.get("reasoning", False)))
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
    return "Stopped: too many tool calls without a final answer. Try rephrasing the question."


if __name__ == "__main__":
    words = [a for a in sys.argv[1:] if a != "-v"]
    if words:
        print(ask(" ".join(words)))
    else:
        print(f"c360 agent ({MODEL}). Ask a question; press Enter on an empty line to quit.")
        while q := input("\n> ").strip():
            print(ask(q))
