# Codex Audit Restart Handoff

> **SUPERSEDED 2026-08-12.** Everything below describes the state on
> 2026-08-11, before remediation. Its "Next action after restart" list is
> completed work, and four of its claims are now false (A-04 closed, F-01
> through F-03 and PF-01 closed, Slice 1 cleared to gate Slice 2, worktree
> clean and pushed). Kept as the record of what was true then.
>
> **Current handoff: `scratch/codex_audit_handoff.md`.**


Saved: 2026-08-11 (America/Anchorage)

## Role and posture

Codex is the independent adversarial auditor for FilmCraft V1. Claude Code is the implementation owner. Produce findings by default, not patches. Preserve reproducible evidence and mutation-test important safeguards.

Source charter:

`C:\Users\Lucas_Admin\Downloads\FILMCRAFT_V1_CODEX_ADVERSARIAL_AUDIT_CHARTER.md`

## Completed work

### Pass A — Director contract

Full report:

`C:\Users\Lucas_Admin\video_automation\scratch\codex_adversarial_audit_pass_a.md`

Remediation verification status:

- A-01 force compile bypass: closed.
- A-03 frontend-only warning dismissal: closed.
- Warning/compile safeguard tests are now mutation-sensitive.
- Residual A-04 remains High: `normalize_warnings()` trusts an incoming Critic `id`. Changed target/kind/detail with the same supplied ID inherits the old disposition and reports zero unresolved findings.
- Verification suite observed: 103 passed, 0 skipped.

### Pass F — Project isolation and persistence

Full report:

`C:\Users\Lucas_Admin\video_automation\scratch\codex_adversarial_audit_pass_f.md`

Slice 1 is **not safe to gate Slice 2**.

Confirmed blockers:

1. **F-01 Critical:** `save_current_project()` loads through the bound context but saves local JSON through the process-wide active pointer. With global A and bound B, B's storyboard overwrites A and B remains unchanged. Reproduced twice.
2. **F-02 High:** background jobs are keyed globally by logical stage name and status returns the complete global registry. A blocks B's same-named job; B sees A's status. Reproduced twice.
3. **F-03 High:** frontend `/api/metadata`, `/api/audio/peaks`, and `/api/assemble/status` reads omit explicit project identity and stale-response rejection.
4. **PF-01 Probable High:** reference registries use process-global `config.REFERENCES_CONFIG`, bypassing the bound context.

Safeguards confirmed:

- `ContextVar` worker capture and rebinding work. Removing the worker bind caused 1 isolation-test failure (18 passed).
- `_context_for()` rejects unknown explicit IDs.
- Bound `config` path helpers work; failures come from higher-level callers bypassing them.

Focused Pass-F baseline:

- 65 passed, 0 skipped.

## Next action after restart

Wait for the implementation owner's remediation of F-01 through F-03 (and ideally PF-01), then independently:

1. Inspect the changes without editing implementation files.
2. Repeat the bound-B/global-A `save_current_project()` reproduction twice.
3. Start the same logical stage concurrently under A and B and verify both start with isolated status/logs.
4. Verify every project-scoped frontend read sends `X-Project-Id` and rejects stale replies.
5. Reproduce the reference-registry path issue or close it as not a finding.
6. Mutation-test the application-level save wrapper and job namespace/status filtering.
7. Update the Pass-F report with remediation status.

## Workspace condition

- The implementation owner's worktree contains extensive uncommitted changes.
- Codex did not modify production implementation files.
- Codex added only the audit reports and this restart handoff under `scratch/`.
- Temporary mutation directories were removed after testing.
- Do not assume a clean Git worktree and do not discard unrelated changes.
