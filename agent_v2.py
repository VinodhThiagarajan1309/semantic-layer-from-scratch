#!/usr/bin/env python3
"""agent_v2.py - the Chapter 14 agent plus two Chapter 15 ideas:

  1. Schema retrieval: search_ontology(term) and describe_class(iri) return a small
     slice of the ontology (matching terms plus their 1-hop neighbors) instead of all of it.
  2. Answers you can trust: get_total_spend(...) checks completeness in code. It reads the
     source registry <urn:c360:registry>, probes every expected source, measures key
     alignment, sums per source, and returns a fixed answer contract (JSON) that the
     model must present verbatim.

Same environment variables as agent.py (it imports agent.py), plus optional:
  C360_REGISTRY      default urn:c360:registry
  C360_METRIC        default tag:c360:ops:total_spend
  OVERLAP_MIN        default 0.9  (share of a source's customer keys that must be known customers)
  ONTOLOGY_SEARCH    "contains" (default, works everywhere), "lexical" (Stardog full-text
                     textMatch) or "semantic" (Stardog semantic search; check your server's options)

Usage:
  python agent_v2.py -v "What is Carolyn Inworth's total spend?"     (LLM + tools)
  python agent_v2.py --spend "Carolyn Inworth"                       (no LLM: prints the contract)
  python agent_v2.py --spend-cid 225
  python agent_v2.py --spend-state Wisconsin
  python agent_v2.py --search spend
"""
import json, os, re, sys
from decimal import Decimal, InvalidOperation

import requests
import agent as v1          # Chapter 14: guard, stardog, run_sparql, get_ontology, client, MODEL
from agent import run_sparql, get_ontology

REGISTRY = os.getenv("C360_REGISTRY", "urn:c360:registry")
METRIC = os.getenv("C360_METRIC", "tag:c360:ops:total_spend")
OVERLAP_MIN = float(os.getenv("OVERLAP_MIN", "0.9"))
SEARCH_MODE = os.getenv("ONTOLOGY_SEARCH", "contains").lower()
KEY_SAMPLE = 100                # agent.guard() caps every LIMIT at 100 anyway
MODEL_GRAPHS = ("urn:stardog:training-c360:1.0:model", "urn:stardog:c360_extended_rules:1.0:model")

PREFIXES = """PREFIX : <tag:stardog:api:ecomm:>
PREFIX ops: <tag:c360:ops:>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX so: <https://schema.org/>
PREFIX fts: <tag:stardog:api:search:>
"""
SAFE_IRI = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:[^\s<>\"{}|\\^`]*$")


# ---------------------------------------------------------------- plumbing
def select(query: str) -> list:
    """Guarded, read-only SELECT. Returns flat rows; raises on any error."""
    q = v1.guard(PREFIXES + query)
    if v1.VERBOSE:
        print(f"\n--- SPARQL (check) ---\n{q}", file=sys.stderr)
    res = v1.stardog(q)
    return [{k: v["value"] for k, v in b.items()} for b in res["results"]["bindings"]]


def iri(value: str) -> str:
    """Only well-formed IRIs may be pasted into a query as <...>."""
    if not SAFE_IRI.match(value or ""):
        raise ValueError(f"not a safe IRI: {value!r}")
    return f"<{value}>"


def literal(text: str) -> str:
    """A user-supplied word as a SPARQL string literal (letters, digits, spaces, - ' . only)."""
    return '"' + re.sub(r"[^\w \-'.]", "", text or "")[:80].replace("'", "\\'") + '"'


def short(s: str) -> str:
    return (s.replace("tag:stardog:api:ecomm:", ":").replace("tag:c360:ops:", "ops:")
             .replace("http://www.w3.org/2002/07/owl#", "").replace("http://www.w3.org/2001/XMLSchema#", "xsd:"))


def name_of(graph: str) -> str:
    return graph.replace("virtual://", "")


def money(d: Decimal) -> str:
    return f"${d:,.2f}"


# ---------------------------------------------------------------- 1. schema retrieval
def _hits(term: str) -> list:
    graphs = " ".join(f"<{g}>" for g in MODEL_GRAPHS + (REGISTRY,))
    if SEARCH_MODE == "lexical":     # Stardog full-text index (search.enabled=true)
        match = f"(?text ?score) <tag:stardog:api:property:textMatch> {literal(term)} ."
    elif SEARCH_MODE == "semantic":  # Stardog semantic search (search.semantic.enabled=true)
        match = f"SERVICE fts:textMatch {{ [] fts:query {literal(term)} ; fts:semantic true ; fts:result ?text }}"
    else:                            # plain scan: fine for thousands of labels, not millions
        match = f"FILTER(CONTAINS(LCASE(STR(?text)), LCASE({literal(term)})))"
    q = f"""SELECT DISTINCT ?term WHERE {{
  {match}
  VALUES ?g {{ {graphs} }}
  GRAPH ?g {{ ?term rdfs:label|rdfs:comment|ops:definition ?text }}
}} LIMIT 8"""
    return [r["term"] for r in select(q)]


def _slice(terms: list) -> str:
    """The terms plus their 1-hop neighbors (domain, range, parent, child, metric inputs)."""
    if not terms:
        return ""
    graphs = " ".join(f"<{g}>" for g in MODEL_GRAPHS + (REGISTRY,))
    hits = " ".join(iri(t) for t in terms)
    q = f"""SELECT ?term (SAMPLE(?k) AS ?kind) (SAMPLE(?l) AS ?label) (SAMPLE(?cm) AS ?comment)
       (GROUP_CONCAT(DISTINCT STR(?d)) AS ?domain) (GROUP_CONCAT(DISTINCT STR(?r)) AS ?range)
       (GROUP_CONCAT(DISTINCT STR(?sup)) AS ?parent) (GROUP_CONCAT(DISTINCT STR(?req)) AS ?requires)
WHERE {{
  VALUES ?hit {{ {hits} }}
  VALUES ?g {{ {graphs} }}
  GRAPH ?g {{
    {{ BIND(?hit AS ?term) }}
    UNION {{ ?term so:domainIncludes|so:rangeIncludes ?hit }}
    UNION {{ ?hit so:domainIncludes|so:rangeIncludes|rdfs:subClassOf|ops:requires ?term }}
    UNION {{ ?term rdfs:subClassOf ?hit }}
  }}
  GRAPH ?g2 {{ ?term a ?k .
    OPTIONAL {{ ?term rdfs:label ?l }} OPTIONAL {{ ?term rdfs:comment|ops:definition ?cm }}
    OPTIONAL {{ ?term so:domainIncludes ?d }} OPTIONAL {{ ?term so:rangeIncludes ?r }}
    OPTIONAL {{ ?term rdfs:subClassOf ?sup }} OPTIONAL {{ ?term ops:requires ?req }} }}
}} GROUP BY ?term ORDER BY ?term LIMIT 60"""
    lines = []
    for v in select(q):
        v = {k: short(x).strip() for k, x in v.items()}
        line = f'{v["term"]} ({v.get("kind", "")}) "{v.get("label", "")}"'
        if v.get("parent"):
            line += f' subClassOf {v["parent"]}'
        if v.get("domain"):
            line += f' {v["domain"]} -> {v.get("range", "")}'
        if v.get("requires"):
            line += f' requires {v["requires"]}'
        if v.get("comment"):
            line += f' -- {v["comment"]}'
        lines.append(line)
    return "\n".join(lines)


def search_ontology(term: str) -> str:
    """Tool 3. Find ontology terms whose label/comment mentions `term`, plus 1-hop neighbors."""
    try:
        hits = _hits(term)
        if not hits:
            return f'No ontology term mentions "{term}". Try a synonym (spend -> price, buyer -> customer).'
        return _slice(hits)
    except (ValueError, RuntimeError, requests.RequestException) as e:
        return str(e)


def describe_class(class_iri: str) -> str:
    """Tool 4. One class or property and its 1-hop neighborhood, like DESCRIBE TABLE."""
    full = class_iri.replace(":", "tag:stardog:api:ecomm:", 1) if class_iri.startswith(":") else class_iri
    try:
        return _slice([full]) or f"{class_iri} is not in the model graphs."
    except (ValueError, RuntimeError, requests.RequestException) as e:
        return str(e)


# ---------------------------------------------------------------- 2. completeness
def read_registry(metric: str = METRIC):
    """Which sources must feed this metric, and which graph is the customer master."""
    sources = select(f"""SELECT ?graph ?cls ?keyProp ?template WHERE {{ GRAPH {iri(REGISTRY)} {{
  {iri(metric)} ops:requires ?cls .
  ?graph ops:provides ?cls ; ops:keyProperty ?keyProp ; ops:joinsOn ?template .
}} }} ORDER BY ?graph""")
    master = select(f"""SELECT ?graph ?template WHERE {{ GRAPH {iri(REGISTRY)} {{
  ?graph ops:provides :Customer ; ops:joinsOn ?template . }} }} LIMIT 1""")
    return sources, (master[0] if master else None)


def find_customers(master: dict, name=None, cid=None, state=None) -> list:
    """Customer IRIs from the master graph, by name, cid and/or state."""
    if cid is not None:
        pick = f"VALUES ?c {{ {iri(master['template'].replace('{cid}', str(int(cid))))} }}"
    else:
        pick = ""
    flt = f"FILTER(LCASE(STR(?name)) = LCASE({literal(name)}))" if name else ""
    st = f"FILTER(STR(?state) = {literal(state)})" if state else ""
    return select(f"""SELECT ?c ?name ?state WHERE {{ {pick}
  GRAPH {iri(master['graph'])} {{ ?c a :Customer ; :name ?name ; :address ?a . ?a :state ?state }}
  {flt} {st} }} LIMIT 100""")


def classify_error(msg: str) -> str:
    m = msg.lower()
    if any(w in m for w in ("permission", "denied", "not authorized", "unauthorized", "403")):
        return "no_permission"
    if any(w in m for w in ("not found", "does not exist", "unknown virtual graph", "no such")):
        return "not_mapped"
    return "unreachable"


def probe(src: dict) -> tuple:
    """Health probe. Returns (status, detail). status 'ok' means rows of the class exist."""
    g, cls = iri(src["graph"]), iri(src["cls"])
    try:
        if select(f"SELECT ?s WHERE {{ GRAPH {g} {{ ?s a {cls} }} }} LIMIT 1"):
            return "ok", ""
        if select(f"SELECT ?s WHERE {{ GRAPH {g} {{ ?s ?p ?o }} }} LIMIT 1"):
            return "mapping_mismatch", f"the graph has triples but no {short(src['cls'])} (wrong class in the mapping?)"
    except (RuntimeError, requests.RequestException) as e:
        return classify_error(str(e)), str(e)[:200]
    # Empty through GRAPH: truly empty, unreadable (silently dropped, Chapter 12) or not defined.
    # SERVICE fails loudly where GRAPH stays silent, so ask again that way.
    try:
        rows = select(f"SELECT ?s WHERE {{ SERVICE {g} {{ ?s ?p ?o }} }} LIMIT 1")
    except (RuntimeError, requests.RequestException) as e:
        return classify_error(str(e)), str(e)[:200]
    if rows:
        return "no_permission", "visible through SERVICE but not through GRAPH"
    return "no_data", "reachable, but the source returned no rows"


def key_overlap(src: dict, master: dict) -> tuple:
    """Share of the source's customer keys that are known customers. Returns (share, n, example)."""
    keys = [r["k"] for r in select(f"""SELECT DISTINCT ?k WHERE {{ GRAPH {iri(src['graph'])} {{
  ?o a {iri(src['cls'])} ; {iri(src['keyProp'])} ?k }} }} LIMIT {KEY_SAMPLE}""")]
    keys = [k for k in keys if SAFE_IRI.match(k)]
    if not keys:
        return 0.0, 0, ""
    values = " ".join(f"<{k}>" for k in keys)
    known = select(f"""SELECT (COUNT(DISTINCT ?k) AS ?n) WHERE {{ VALUES ?k {{ {values} }}
  GRAPH {iri(master['graph'])} {{ ?k a :Customer }} }}""")
    n = int(known[0]["n"]) if known else 0
    return n / len(keys), len(keys), keys[0]


def subtotal(src: dict, customers: list) -> tuple:
    """SUM(price * quantity) and COUNT for these customers in one source."""
    values = " ".join(iri(c) for c in customers)
    rows = select(f"""SELECT (SUM(?p * ?q) AS ?spend) (COUNT(?o) AS ?orders) WHERE {{
  VALUES ?c {{ {values} }}
  GRAPH {iri(src['graph'])} {{ ?o a {iri(src['cls'])} ; {iri(src['keyProp'])} ?c ;
                        :purchasePrice ?p ; :quantity ?q }} }}""")
    r = rows[0] if rows else {}
    try:
        return Decimal(r.get("spend") or "0"), int(r.get("orders") or 0)
    except InvalidOperation:
        return Decimal("0"), 0


def template_regex(template: str) -> re.Pattern:
    return re.compile("^" + re.escape(template).replace(re.escape("{cid}"), r"\d+") + "$")


def contract(value, expected, included, missing, subtotals, customers, warnings, note=""):
    complete = value is not None and not missing and bool(expected)
    reasons = "; ".join(f'{m["source"]} missing because {m["reason"]}' for m in missing)
    shown = money(value) if value is not None else "no value"
    if complete:
        summary = f"Complete: {shown}. All expected sources contributed: {', '.join(expected)}."
    else:
        summary = (f"Partial: {shown}. Expected sources {', '.join(expected) or '(none registered)'}; "
                   f"{reasons or note}.")
    return {"metric": short(METRIC), "value": None if value is None else f"{value:.2f}",
            "complete": complete, "expected": expected, "included": included, "missing": missing,
            "subtotals": subtotals, "customers": customers, "warnings": warnings, "summary": summary}


def get_total_spend(customer_name=None, cid=None, state=None) -> dict:
    """Tool 5. Total spend with a deterministic completeness check. Returns the answer contract."""
    sources, master = read_registry()
    expected = [name_of(s["graph"]) for s in sources]
    if not sources or not master:
        return contract(None, expected, [], [{"source": "registry", "reason":
                        f"no sources or customer master declared in <{REGISTRY}>"}], {}, [], [])
    people = find_customers(master, customer_name, cid, state)
    if not people:
        return contract(None, expected, [], [], {}, [], [],
                        note=f"no matching customer in {name_of(master['graph'])}")
    cust = [p["c"] for p in people]
    total, included, missing, subtotals, warnings = Decimal("0"), [], [], {}, []
    for src in sources:
        name = name_of(src["graph"])
        status, detail = probe(src)
        if status != "ok":
            missing.append({"source": name, "reason": status.replace("_", " ") + (f" ({detail})" if detail else "")})
            continue
        try:
            share, n, example = key_overlap(src, master)
            if share == 0:
                hint = "" if template_regex(src["template"]).match(example) else \
                       f"; keys look like {example}, registry expects {src['template']}"
                missing.append({"source": name, "reason":
                                f"mapping mismatch (0% customer-key overlap in {n} sampled keys{hint})"})
                continue
            if share < OVERLAP_MIN:
                warnings.append(f"{name}: only {share:.0%} of {n} sampled customer keys are known customers")
            amount, orders = subtotal(src, cust)
        except (ValueError, RuntimeError, requests.RequestException) as e:
            missing.append({"source": name, "reason": f"{classify_error(str(e))} ({str(e)[:200]})"})
            continue
        total += amount
        included.append(name)
        subtotals[name] = {"spend": f"{amount:.2f}", "orders": orders, "key_overlap": round(share, 3)}
    return contract(total, expected, included, missing, subtotals,
                    [p["name"] for p in people][:10], warnings)


# ---------------------------------------------------------------- the agent
SYSTEM_PROMPT = v1.SYSTEM_PROMPT + """

Chapter 15 additions:
- Total spend (for a customer by name or cid, or for a state) MUST come from get_total_spend,
  never from your own run_sparql query: only get_total_spend checks that every source that
  should feed the number actually did.
- get_total_spend returns an answer contract (JSON). Begin your answer with its "summary"
  field, copied verbatim. If "complete" is false, never call the value the total: say it is
  partial, and list every entry in "missing" with its reason. Do not guess what is missing.
- If the question uses a word you can't map to the ontology above, call search_ontology with
  that word (or describe_class with a term) before writing SPARQL."""

TOOLS = v1.TOOLS + [
    {"type": "function", "function": {
        "name": "search_ontology",
        "description": "Find ontology classes/properties/metrics whose label or comment mentions a word; returns them with their 1-hop neighbors.",
        "parameters": {"type": "object", "required": ["term"], "properties": {
            "term": {"type": "string", "description": "One word or short phrase, e.g. 'price'."}}}}},
    {"type": "function", "function": {
        "name": "describe_class",
        "description": "Describe one class or property (e.g. ':Order') and its 1-hop neighbors.",
        "parameters": {"type": "object", "required": ["iri"], "properties": {
            "iri": {"type": "string", "description": "A term such as :Order or a full IRI."}}}}},
    {"type": "function", "function": {
        "name": "get_total_spend",
        "description": "Total spend with a completeness check across every registered source. Returns the answer contract.",
        "parameters": {"type": "object", "properties": {
            "customer_name": {"type": "string", "description": "Full name, e.g. 'Carolyn Inworth'."},
            "cid": {"type": "integer", "description": "Customer id, if known."},
            "state": {"type": "string", "description": "Full state name, e.g. 'Wisconsin'."}}}}},
]


def call_tool(name: str, args: dict) -> str:
    if name == "get_ontology":
        return get_ontology()
    if name == "search_ontology":
        return search_ontology(str(args.get("term", "")))
    if name == "describe_class":
        return describe_class(str(args.get("iri", "")))
    if name == "get_total_spend":
        try:
            return json.dumps(get_total_spend(args.get("customer_name"), args.get("cid"), args.get("state")))
        except (ValueError, RuntimeError, requests.RequestException) as e:
            return json.dumps({"complete": False, "value": None, "error": str(e)[:500]})
    if name == "run_sparql":
        return run_sparql(args.get("query", ""), bool(args.get("reasoning", False)))
    return f"Error: unknown tool {name}"


def ask(question: str, log=None) -> str:
    """Same loop as Chapter 14, with the new prompt and tools. `log` collects (tool, args, result)."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": question}]
    for _ in range(v1.MAX_STEPS):
        msg = v1.client.chat.completions.create(model=v1.MODEL, messages=messages, tools=TOOLS).choices[0].message
        if not msg.tool_calls:
            return msg.content or "(no answer)"
        messages.append(msg.model_dump(exclude_none=True))
        for call in msg.tool_calls:
            try:
                args = json.loads(call.function.arguments or "{}")
                result = call_tool(call.function.name, args)
            except json.JSONDecodeError:
                args, result = {}, "Error: tool arguments were not valid JSON."
            if log is not None:
                log.append((call.function.name, args, result))
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
    return "Stopped: too many tool calls without a final answer. Try rephrasing the question."


if __name__ == "__main__":
    argv = [a for a in sys.argv[1:] if a != "-v"]
    direct = {"--spend": "customer_name", "--spend-cid": "cid", "--spend-state": "state"}
    if len(argv) >= 2 and argv[0] in direct:
        key, val = direct[argv[0]], " ".join(argv[1:])
        print(json.dumps(get_total_spend(**{key: int(val) if key == "cid" else val}), indent=2))
    elif len(argv) >= 2 and argv[0] == "--search":
        print(search_ontology(" ".join(argv[1:])))
    elif argv:
        print(ask(" ".join(argv)))
    else:
        print(f"c360 agent v2 ({v1.MODEL}). Ask a question; press Enter on an empty line to quit.")
        while q := input("\n> ").strip():
            print(ask(q))
