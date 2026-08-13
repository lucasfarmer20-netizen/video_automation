# FilmCraft adversarial audits

Durable Codex audit reports live in this directory so they travel with the code
and are available to remote collaborators.

Each report should record:

- the audited branch, commit, and implementation diff;
- the applicable charter and contract;
- confirmed and probable defects;
- exact reproductions and evidence;
- test gaps and mutation-test results;
- rejected findings and compensating safeguards;
- whether production repository files were modified.

Before starting a round, on either side, read
[orchestration guardrails](orchestration_guardrails.md): when a round is worth
running, what a finding and a fix must contain to count, and when to stop and
ask a human.

Current reports:

- [Slice 4 remediation re-audit](filmcraft_v1_slice_4_remediation_reaudit.md)

The governing documents are:

- [`docs/FILMCRAFT_V1_CODEX_ADVERSARIAL_AUDIT_CHARTER.md`](../FILMCRAFT_V1_CODEX_ADVERSARIAL_AUDIT_CHARTER.md)
- [`docs/FILMCRAFT_V1_CODE_IMPLEMENTATION_CONTRACT.md`](../FILMCRAFT_V1_CODE_IMPLEMENTATION_CONTRACT.md)

