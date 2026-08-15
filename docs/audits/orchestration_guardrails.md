# Build/audit orchestration guardrails

Read this before starting a round, on either side.

Claude Code builds; Codex audits adversarially. Coordinated automatically, that
pair can loop indefinitely, because each remediation is itself new code for the
next round to find fault with. These are the conditions under which a round is
worth running, what a finding and a fix must contain to count, and when to stop
and ask a human.

The numbers below are not invented. They come from the six rounds run against
slices 0-4 between 2026-08-11 and 2026-08-13, recorded in the reports beside
this file.

## What those rounds actually showed

| Slice | Rounds | Severity trend | Note |
|---|---|---|---|
| Director (Pass A) | 2 | Critical → High | Round 2 found A-04, a gap in round 1's fix |
| Isolation (Pass F) | 2 | Critical → clear | Pass G cleared it |
| Approval (S2) | 4 | Critical → Critical → High → clear | Rounds 2 and 3 were each caused by the previous fix |
| Generation (S4) | 3 | Critical → High → Medium | Round 2 caused by round 1's fix |

Two findings from that table drive everything below.

**Remediation reliably introduces the next defect** — three slices out of four.
A first round that closes cleanly is not the expected outcome, so a second round
is normal and a third is the point at which to start asking whether the approach
is wrong rather than the implementation.

**Severity decays; finding count does not.** Every round produced roughly three
findings. What fell was how bad they were: double-charging and cross-project
overwrites first, then stranded work, then billing-accounting fidelity. The
severity ceiling is the stop signal. Counting findings will never terminate.

## Hard stops

- **Three rounds per slice, then escalate.** Slice 2 took four; the fourth was a
  Windows-only `os.replace` denial that cannot occur on Cloud Run. That round
  should have been excluded by policy, not discovered and then argued about.
- **Stop when a round yields no Critical or High finding that is reachable in
  the deployed environment.** Reachability, not existence.
- **Escalate when the severity ceiling has not decreased for two consecutive
  rounds.** That is a stall: the two agents disagree about something iteration
  will not settle.
- **Cap tokens per slice.** The marginal round is worth progressively less and
  costs the same.

## A finding must contain

Reject before it becomes a round if it:

- reproduces fewer than twice;
- is not reachable on Linux / Cloud Run (record as Low; never loop on it);
- cites no contract clause;
- ships no mutation that fails a test — unless filed explicitly as a *test gap*;
- appears on the closed list without new evidence.

The closed list and the out-of-scope list live in `scratch/codex_audit_handoff.md`
and must be updated at the end of every round. A restarted agent that cannot see
them will re-litigate settled work: that happened on 2026-08-12, when a restarted
auditor was handed a handoff describing the pre-remediation state and would have
re-audited the wrong slice.

## A fix must contain

This is the side that caused most of the churn, and the builder is the one that
caused it.

- **A test that fails under a faithful mutation of that fix.** Binary, cheap,
  and it caught three bad tests written by the builder in this period — tests
  that passed for the wrong reason and proved nothing.
- **A mutation that reproduces the original defect.** One builder mutation
  reordered fields and so accidentally hid the bug it was meant to restore; it
  "passed" meaninglessly.
- **No new public interface during a remediation round.** S4-R02 exists because
  the builder changed failure semantics (`fail` → `in_doubt`) while fixing
  S4-01. Behaviour changes belong in a build round; a fix round fixes.

## Reporting the baseline honestly

A round's result is only meaningful against a known baseline, so:

- **State the suite result with every hand-off**, as passed / skipped / failed.
  A pass count alone has twice been read as a clean run when it was not.
- **A red suite may be merged, but never silently.** If failures are left in
  place, the commit or hand-off MUST name them and say why they are acceptable.
  Slice 7 landed at `5a0d37a` with seven failing tests and no mention; the work
  was correct and the failures were environmental, but establishing that cost a
  diagnosis that the commit message could have saved.
- **Environmental failures are not product defects, and the distinction is the
  reporter's job to make.** `tests/conftest.py` prints an ENVIRONMENT INCOMPLETE
  banner naming the interpreter and the missing capability; if that banner is
  present, fix the environment before reporting a defect.
- **Check the interpreter before concluding a dependency is installed.** `pip
  install` reporting "already satisfied" is evidence about whichever Python it
  ran under, which on a multi-profile or multi-machine setup is routinely not
  the one running the tests.

## Loop breakers

- **The builder rejecting a finding is a human decision, not another round.**
  The S4-01 remediation deliberately departed from the auditor's suggested fix,
  because a derived idempotency key would have blocked a legitimate re-buy. That
  resolved well only because a human was in the loop. Automated, it ping-pongs.
- **The same finding ID reopening twice ends the loop.** S2-03 reopened once,
  which meant the fix was incomplete. Twice would mean the approach is wrong.
- **Anything touching spend escalates**, whatever the severity trend. Money is
  where "probably fine" is not good enough.

## Configuration

```yaml
max_rounds_per_slice: 3
escalate_if: severity_ceiling_unchanged_for: 2
auto_close_if: no_reachable_critical_or_high: true
finding_min_reproductions: 2
fix_requires_failing_mutation: true
new_public_api_in_fix_round: forbidden
spend_related_finding: always_escalate
same_finding_reopened: 2          # ends the loop
out_of_scope:
  - workstation_only_conditions    # Windows os.replace denial, etc.
  - cross_process_locking          # documented as not provided
  - lost_update_prevention         # documented as not provided
```

## One caveat

Every round so far found something real. None was wasted in the sense of finding
nothing. The case for capping is that the *severity* of what a round finds falls
below the cost of running it — not that rounds stop finding defects.

So gate on severity and reachability. Let the round count be the backstop, not
the primary rule.
