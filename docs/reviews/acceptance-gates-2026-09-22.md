# Fail-closed ACCEPT / selected-attempt evidence review

Base: `d48f39bdf4aff67e01047fea120f410175d3c21b` (2026-09-22).
Related requirements: FR-19..23; CONVENTIONS §3.2, §4.

## Findings

`api/services/review_service.py:decide_review` rejects FAIL/INSUFFICIENT,
UNCONFIRMED, non-SUCCEEDED execution and MOCK run artifacts. This negative list
omits NOT_CHECKED, OUT_OF_SCOPE and missing/UNKNOWN evidence modes.
`evidence/bundle.py:compute_completeness` finds artifacts and verifications by
run_id, not the selected attempt, so a previous attempt can satisfy presence.

A green Run projection must not substitute for a passing independent Verification
that references committed evidence from the same current attempt.

## Patch

Add a dependency-free pure policy at `review/acceptance.py` and invoke it from
`review/closeout.py:bundle_blockers`, which the existing human ACCEPT path calls.
Require affirmative SUCCEEDED / PASS / IN_SCOPE, a selected attempt, committed
REAL evidence for that attempt, passing current-attempt verifications and
nonempty source references within that committed evidence set. Return stable,
structured blocker keys. Do not grant agent approval or change thresholds.

The old checks remain as defense in depth. In particular, the old service can
still conservatively reject historical MOCK artifacts; this PR does not relax
that behavior. REQUEST_CHANGES and REJECT remain available.

## Validation status

- **TESTED:** 34 dependency-free pure-policy cases, Python 3.13.5; 34 passed.
- **TESTED:** syntax compilation of changed Python files.
- **IMPLEMENTED, NOT_RUN:** 5 API regressions reusing the existing explicitly
  REAL-tagged synthetic gate fixture (3 rejection cases, 2 non-ACCEPT cases).
- **NOT_RUN:** full repository suite, SQLite/HTTP integration of this change,
  DSH plugin, Windows runtime and real STAR solver acceptance.

No synthetic fixture is presented as real CFD evidence. Original tests and
thresholds have not been weakened or removed. Keep the PR draft until the
complete API suite has been rerun:

```bash
python -m pytest tests/test_acceptance_policy.py tests/test_acceptance_api_regressions.py tests/test_review_flow.py -m mock -q
python -m pytest tests -m mock -q
```

## Intentionally not solved in this PR

This is a live-state/selected-attempt gate, not full snapshot attestation. Separate
follow-ups remain necessary:

1. Bind approval to the **frozen** selected-attempt and verification snapshot,
   recompute manifest digests, verify stored byte lengths/hashes, and reject
   post-freeze completion without rebuilding and resubmitting the bundle.
2. Propagate evidence provenance through preparation, templates, metrics and
   reports; do not infer all evidence is REAL solely from Run artifacts.
3. Validate the exact TaskSpec capability package digest/release/rules. The current
   verifier and bundle paths still hard-code buffer_chamber/0.1.0 in places.
4. Persist and validate close_evidence_artifact_ids; currently close_issue only
   checks nonemptiness and does not retain those IDs as closure evidence.
5. Make WAL replay commit before local acknowledgement. replay_wal currently
   acknowledges each record before its final session.commit(). Add crash tests.
6. Replace unconditional X-Dev identity parsing with explicit local-dev mode and
   trusted server identity before team rollout; add project/node authorization.

## Product acceptance recommendation

Conditional go for local, explicitly MOCK workflow exploration. No acceptance as
 a production/multi-user unattended industrial solver service yet. StarCliAdapter
implements probe/inspect but its preparation/execution/collection methods still
raise NotImplementedError. The next delivery should be one constrained, real,
replayable STAR task family rather than more platform UI or many stub adapters.
