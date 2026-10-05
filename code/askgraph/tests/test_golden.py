"""Golden tests. Live: they hit Stardog (and one hits the LLM).

Deterministic checks call stardog.py directly with the few-shot example queries, so the
examples the model learns from are themselves proven correct. One end-to-end test runs the
full agent loop.
"""
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from askgraph import stardog  # noqa: E402
from askgraph.agent import build_system_prompt, run  # noqa: E402
from askgraph.config import PROMPTS_DIR, load_settings  # noqa: E402

S = load_settings()
HR = S.with_overrides(user="hr")
MKT = S.with_overrides(user="marketing")


def example(name: str) -> str:
    return (PROMPTS_DIR / "examples" / f"{name}.sparql").read_text()


# ---------------------------------------------------------------- offline

@pytest.mark.parametrize("q", [
    "INSERT DATA { <a> <b> <c> }",
    "PREFIX : <x:>\nDELETE WHERE { ?s ?p ?o }",
    "DROP GRAPH <virtual://redshift_c360>",
    "CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }",
])
def test_guard_rejects_non_reads(q):
    with pytest.raises(ValueError):
        stardog.guard(q)


def test_guard_adds_and_caps_limit():
    assert stardog.guard("SELECT * WHERE { ?s ?p ?o }").endswith("LIMIT 100")
    assert stardog.guard("SELECT * WHERE { ?s ?p ?o } LIMIT 5000").endswith("LIMIT 100")
    assert stardog.guard("SELECT * WHERE { ?s ?p ?o } LIMIT 7").endswith("LIMIT 7")
    # keywords inside literals/IRIs are not commands
    stardog.guard('SELECT * WHERE { ?s <urn:create#x> "Delete me" } LIMIT 1')


def test_prompt_assembly():
    p = build_system_prompt(S)
    assert "database kit-c360" in p and "LIMIT (<= 100)" in p
    assert "# Q: Who are the top spenders in Wisconsin?" in p
    assert "big_spenders_by_state" in p
    assert "{ ?c :name ?name" in p            # SPARQL braces survive placeholder filling


# ---------------------------------------------------------------- live Stardog

def test_top_spenders_wisconsin():
    r = stardog.run_sparql(HR, example("top_spenders"))
    assert r.error is None, r.error
    top = [(row["name"], round(float(row["spend"]), 2)) for row in r.rows[:2]]
    assert top == [("Carolyn Inworth", 502529.42), ("Renaud Brosio", 207537.22)]


def test_big_spenders_by_state_reasoning():
    r = stardog.run_sparql(HR, example("big_spenders_by_state"), reasoning=True)
    assert r.error is None, r.error
    assert r.row_count > 0
    assert set(r.columns) == {"state", "big_spenders"}


def test_ssn_visible_to_hr_only():
    q = example("customer_ssn")
    hr = stardog.run_sparql(HR, q)
    assert hr.error is None, hr.error
    assert [row["ssn"] for row in hr.rows] == ["609-38-5573"]
    mkt = stardog.run_sparql(MKT, q)
    assert mkt.error is None, mkt.error
    assert mkt.row_count == 0                  # named-graph security hides the PII graph


def test_ontology_lists_big_spender():
    text = stardog.get_ontology(HR)
    assert ":Big_Spender" in text


# ---------------------------------------------------------------- live end-to-end (LLM)

def test_agent_end_to_end_top_spender():
    events = list(run("Who are the top spenders in Wisconsin?", HR))
    kinds = [e.kind for e in events]
    assert "tool_call" in kinds and kinds[-2:] == ["answer", "usage"]
    answer = events[-2].text
    assert "Carolyn Inworth" in answer
    assert re.search(r"502,?529", answer), answer
    assert events[-1].queries >= 1
