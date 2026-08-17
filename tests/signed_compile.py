"""Post a compile the way a real caller has to: saying which plan it approved.

``POST /api/director/compile/{beat_id}`` requires ``plan_signature``. It was
optional for one round and optional meant unenforced -- ``if plan_signature and
...`` skipped the comparison for an omitted or empty value, so an unsigned
request dispatched whatever plan was on disk. There is no unsigned path to keep
compatible now, so every caller that wants a compile loads the plan and sends
its approved signature. That is what the studio does; this is that, for tests.

Deliberately a helper and not a fixture default. The signature is READ FROM DISK
at call time, so a test that swaps the plan between quoting and confirming can
still send the OLD signature by capturing it first -- which is the entire point
of ``tests/test_compile_quote_binding.py``. A helper that always sent the
current signature would make that race untestable, which is how a binding ends
up with no test that can fail.
"""
from __future__ import annotations

from backend import director


def signature_for(beat_id: str) -> str:
    """The signature of the plan currently saved for ``beat_id``."""
    plan = director.load_plan(beat_id)
    if not plan:
        raise AssertionError(
            f"no plan saved for {beat_id}; a signed compile needs one to sign")
    return director.plan_signature(plan)


def compile_beat(client, beat_id: str, signature: str | None = None, **kwargs):
    """POST a compile for ``beat_id``, signed.

    ``signature`` defaults to the plan as it stands right now. Pass one
    explicitly to reproduce a caller holding a stale quote.
    """
    params = {"plan_signature": signature if signature is not None
              else signature_for(beat_id)}
    return client.post(f"/api/director/compile/{beat_id}", params=params, **kwargs)
