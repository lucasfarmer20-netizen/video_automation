"""Mutation-sensitivity check for the compile route's quote binding.

Two defects, one after the other, and the second was caused by the fix for the
first.

ROUND 1. `compile_director_coverage` accepted no plan identity, so it dispatched
`director.load_plan(beat_id)` -- whatever was current at DISPATCH -- against a
price the human had confirmed for some earlier plan. Q1 restores that.

ROUND 2. The binding that closed it read `if plan_signature and plan_signature
!= current_signature`, with `plan_signature: str = ""`. Optional meant
UNENFORCED: omitting the parameter, or sending it empty, skipped the comparison
and dispatched anyway -- nine paid shots against consent given for two. That is
Gate 1's own defect in a new place. There, `s.approved and bool(...)` let a
truthy STRING clear the gate; here, `if plan_signature and ...` let a FALSY one
skip it. Both made enforcement conditional on the truthiness of the input, and
both let the caller opt out by supplying nothing. Q8 restores that.

`director.signature_is_explicit` is the answer, named after and reasoned from
`manifest.approval_is_explicit`: the value is what consent looks like, or it is
not consent.

Every mutation declares the tests that must be among the failures, so a mutation
killed by an unrelated assertion is reported MISFIRED rather than KILLED. The
two that restore a real defect also declare `expect_output`, because the defect
is a DISPATCH and a run that merely fails a status assertion proves nothing
about whether money was spent.

Run from the repo root:  python scratch/mutate_quote_binding.py
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
MAIN = ROOT / "backend/main.py"
DIRECTOR = ROOT / "backend/director.py"

TARGET = "tests/test_compile_quote_binding.py"

# The dispatch assertion's own failure message. Requiring it in the output is
# what proves a mutation died on "money was spent" and not on "expected 409, got
# 200" -- the distinction this project keeps having to relearn.
DISPATCHED = "dispatched with shot counts"

# The round-1 binding, as it stands in the route.
MISMATCH_BLOCK = '''        if (director.signature_is_explicit(plan_signature)
                and plan_signature != current_signature):
            return JSONResponse(status_code=409, content={
                "ok": False,
                "error": (f"the plan for {beat_id} changed after you were quoted a "
                          f"price, so nothing was compiled and nothing was charged. "
                          f"Review the current plan and confirm its cost again."),
                "signature_mismatch": True,
                "quoted_signature": plan_signature,
                "plan_signature": current_signature,
            })'''

MISMATCH_TEST = ("        if (director.signature_is_explicit(plan_signature)\n"
                 "                and plan_signature != current_signature):")

UNSIGNED_TEST = "        if not director.signature_is_explicit(plan_signature):"

# (name, [(file, old, new), ...], expect_fail, expect_output)
MUTATIONS = [
    # --- round 1: the quote is not bound to a plan --------------------------
    # `current_signature` stays, because the draft branch reads it -- removing
    # it too would be a NameError, which is a crash and not the defect.
    ("Q1  round-1 defect: no binding at all",
     [(MAIN, MISMATCH_BLOCK, "")],
     ["test_a_plan_replaced_after_the_quote_is_not_compiled",
      "test_the_refusal_hands_back_the_signature_needed_to_re_quote",
      "test_an_edit_that_does_not_change_cost_still_invalidates_the_quote",
      "test_consent_is_checked_before_the_plan_s_own_state"],
     DISPATCHED),
    ("Q2  refuse every compile that carries a signature",
     [(MAIN, MISMATCH_TEST,
       "        if director.signature_is_explicit(plan_signature):")],
     ["test_the_quoted_plan_compiles"], ""),
    ("Q3  compare the shape of the signatures, not the signatures",
     [(MAIN, MISMATCH_TEST,
       "        if (director.signature_is_explicit(plan_signature)\n"
       "                and len(plan_signature) != len(current_signature)):")],
     ["test_a_plan_replaced_after_the_quote_is_not_compiled",
      "test_an_edit_that_does_not_change_cost_still_invalidates_the_quote"], ""),
    ("Q4  refuse, but with a 200 the caller will read as success",
     [(MAIN,
       '            return JSONResponse(status_code=409, content={\n'
       '                "ok": False,\n'
       '                "error": (f"the plan for {beat_id} changed after you were quoted a "',
       '            return JSONResponse(status_code=200, content={\n'
       '                "ok": False,\n'
       '                "error": (f"the plan for {beat_id} changed after you were quoted a "')],
     ["test_a_plan_replaced_after_the_quote_is_not_compiled"], ""),
    ("Q5  drop the discriminator the client branches on",
     [(MAIN, '                "signature_mismatch": True,\n                "quoted_signature": plan_signature,',
       '                "quoted_signature": plan_signature,')],
     ["test_a_plan_replaced_after_the_quote_is_not_compiled",
      "test_consent_is_checked_before_the_plan_s_own_state"], ""),
    # Anchored on `quoted_signature` too: the draft refusal also returns
    # `plan_signature: current_signature`, and a two-hit anchor mutates nothing.
    ("Q6  refuse without saying what the plan is now, so no re-quote is possible",
     [(MAIN, '                "quoted_signature": plan_signature,\n'
             '                "plan_signature": current_signature,',
       '                "quoted_signature": plan_signature,\n'
       '                "plan_signature": "",')],
     ["test_the_refusal_hands_back_the_signature_needed_to_re_quote"], ""),

    # --- round 2: optional meant unenforced ---------------------------------
    ("Q8  round-2 defect: let an unsigned request through",
     [(MAIN, UNSIGNED_TEST, "        if False:")],
     ["test_an_unsigned_compile_is_refused",
      "test_an_empty_signature_is_refused_exactly_like_a_missing_one",
      "test_a_malformed_signature_is_refused_and_not_reported_as_a_mismatch",
      "test_the_unsigned_refusal_does_not_hand_back_the_signature"],
     DISPATCHED),
    ("Q9  truthiness again: only an EMPTY signature is refused",
     [(MAIN, UNSIGNED_TEST, "        if not plan_signature:")],
     ["test_a_malformed_signature_is_refused_and_not_reported_as_a_mismatch"], ""),
    ("Q10 accept anything non-empty as a signature",
     [(DIRECTOR,
       "    return isinstance(value, str) and _SIGNATURE_RE.fullmatch(value) is not None",
       "    return isinstance(value, str) and len(value) > 0")],
     ["test_a_malformed_signature_is_refused_and_not_reported_as_a_mismatch"], ""),
    ("Q11 accept a signature-shaped prefix, so a truncated hash passes",
     [(DIRECTOR, 'r"[0-9a-f]{16}"', 'r"[0-9a-f]{1,16}"')],
     ["test_a_malformed_signature_is_refused_and_not_reported_as_a_mismatch"], ""),
    ("Q12 report an unsigned request as a stale quote",
     [(MAIN, '                "signature_missing": True,',
       '                "signature_mismatch": True,')],
     ["test_an_unsigned_compile_is_refused"], ""),
    ("Q13 hand the signature back to a caller that never asked a human",
     [(MAIN, '                "signature_missing": True,\n            })',
       '                "signature_missing": True,\n'
       '                "plan_signature": current_signature,\n            })')],
     ["test_the_unsigned_refusal_does_not_hand_back_the_signature"], ""),

    # Ordering, as a real move rather than a short-circuit: demanding a
    # signature BEFORE the draft gate tells a plan that was never approved to
    # send a signature it cannot have.
    ("Q14 demand a signature before telling a draft plan to lock",
     [(MAIN,
       "        if not director.signature_is_explicit(plan_signature):\n"
       "            return JSONResponse(status_code=400, content={",
       "        if False:\n"
       "            return JSONResponse(status_code=400, content={"),
      (MAIN,
       "        # §11.5: an approved plan must not silently mutate after approval.",
       "        if not director.signature_is_explicit(plan_signature):\n"
       "            return JSONResponse(status_code=400, content={\n"
       '                "ok": False,\n'
       '                "error": "unsigned",\n'
       '                "signature_missing": True,\n'
       "            })\n"
       "        # §11.5: an approved plan must not silently mutate after approval.")],
     ["test_the_draft_gate_still_answers_first_for_an_unsigned_caller"], ""),
]


def run_suite() -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", TARGET, "-q"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    problems: list[str] = []
    for name, edits, expect_fail, expect_output in MUTATIONS:
        originals = {path: path.read_text(encoding="utf-8") for path, _, _ in edits}

        stale = [old for path, old, _ in edits if originals[path].count(old) != 1]
        if stale:
            print(f"STALE    {name}: {len(stale)} anchor(s) did not match exactly once")
            problems.append(f"{name}: stale anchor")
            continue

        try:
            pending = dict(originals)
            for path, old, new in edits:
                pending[path] = pending[path].replace(old, new)
            for path, text in pending.items():
                path.write_text(text, encoding="utf-8")
            code, out = run_suite()
        finally:
            for path, text in originals.items():
                path.write_text(text, encoding="utf-8")

        failed = [l.strip() for l in out.splitlines() if l.strip().startswith("FAILED")]
        totals = [l.strip() for l in out.splitlines()
                  if " passed" in l or (" failed" in l and "=" not in l)]
        verdict = "KILLED  " if code != 0 else "SURVIVED"

        unmet = [w for w in expect_fail if not any(w in f for f in failed)]
        if code == 0:
            problems.append(f"{name}: survived")
        elif unmet:
            verdict = "MISFIRED"
            problems.append(f"{name}: killed, but not by {unmet}")
        elif expect_output and expect_output not in out:
            verdict = "MISFIRED"
            problems.append(
                f"{name}: killed without ever evaluating the dispatch assertion "
                f"(its message is absent from the output)")

        print(f"{verdict} {name}")
        for line in totals[-1:]:
            print("         ", line)
        for line in failed[:8]:
            print("          -", line[:120])
        if expect_output:
            print(f"          died on the dispatch assertion: "
                  f"{'yes' if expect_output in out else 'NO'}")

    print()
    if problems:
        print(f"{len(problems)} mutation(s) did not prove what they stand for:")
        for p in problems:
            print("  -", p)
        return 1
    print(f"All {len(MUTATIONS)} mutations killed, each by the assertion it stands for.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
