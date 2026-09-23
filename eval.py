#!/usr/bin/env python3
"""eval.py - golden tests for the c360 agent (Chapter 15).

Each case is a competency question with an answer verified in plain SQL (DBeaver).
The harness asks agent_v2, pulls the numbers out of the answer, compares them within a
tolerance, and checks completeness: a spend answer must come from get_total_spend, and
if that contract says complete=false, the answer must say "Partial".

Usage:  python eval.py            (all cases; each one calls the LLM and Stardog)
        python eval.py --direct   (spend cases only, straight through get_total_spend, no LLM)
Exit code 0 when every case passes, so you can run it in CI.
"""
import json, re, sys
import agent_v2

CASES = [  # expected values: SQL over Aurora + Redshift, after Chapter 15 step 6
    {"q": "What is Carolyn Inworth's total spend?", "number": 504779.42,
     "spend": {"customer_name": "Carolyn Inworth"}},
    {"q": "What is the total spend of all customers in Wisconsin?", "number": 839445.23,
     "spend": {"state": "Wisconsin"}},
    {"q": "How many customers live in Texas?", "number": 53},
    {"q": "How many purchases are recorded in Redshift?", "number": 2500},
    {"q": "Who is the top spender in Wisconsin?", "text": "Carolyn Inworth"},
]
TOLERANCE = 0.01


def numbers(text: str) -> list:
    return [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", text)]


def check_answer(case: dict, answer: str, log: list) -> list:
    """Return a list of failure reasons (empty = PASS)."""
    fails = []
    if "number" in case and not any(abs(n - case["number"]) <= TOLERANCE for n in numbers(answer)):
        fails.append(f"expected {case['number']:,} in the answer")
    if "text" in case and case["text"].lower() not in answer.lower():
        fails.append(f"expected '{case['text']}' in the answer")
    if "spend" in case:  # completeness assertion
        contracts = [json.loads(r) for name, _, r in log if name == "get_total_spend"]
        if not contracts:
            fails.append("spend was not computed by get_total_spend (no completeness check)")
        elif not contracts[-1].get("complete"):
            if "partial" not in answer.lower():
                fails.append("contract says incomplete but the answer does not say 'Partial'")
            fails.append("incomplete: " + contracts[-1].get("summary", ""))
    return fails


def run(direct: bool) -> int:
    failed = 0
    for case in CASES:
        if direct:
            if "spend" not in case:
                continue
            c = agent_v2.get_total_spend(**case["spend"])
            answer, log = c["summary"], [("get_total_spend", case["spend"], json.dumps(c))]
        else:
            log = []
            answer = agent_v2.ask(case["q"], log)
        fails = check_answer(case, answer, log)
        failed += bool(fails)
        print(f"{'FAIL' if fails else 'PASS'}  {case['q']}")
        for f in fails:
            print(f"      - {f}")
        if fails:
            print(f"      answer: {answer[:300]}")
    print(f"\n{failed} failed" if failed else "\nall passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run("--direct" in sys.argv))
