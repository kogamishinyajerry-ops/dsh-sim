# canonical-json-v1 input-integrity review

Base: `d48f39bdf4aff67e01047fea120f410175d3c21b` (2026-09-22).
Related contract: CONVENTIONS §3.1; version baseline FR-27.

## Defects and narrow change

1. Two distinct dictionary keys can normalize to the same NFC key. The old
   implementation silently overwrites a value: `{"é": 1, "e\u0301": 2}` and
   `{"é": 1}` both serialize to `{"é":1}` and have the same input digest. This
   is data loss before hashing, NOT a cryptographic SHA-256 collision.
2. Sorting mixed string/non-string keys runs before type validation and raises
   TypeError instead of the documented CanonicalizationError.
3. Lone surrogate code points escape canonical_dumps and fail later during UTF-8
   hashing. Reject them at the canonicalization boundary.

The patch rejects ambiguous/invalid inputs. It preserves existing valid JSON
serialization, array order, valid NFC equivalence and public function signatures.
No engineering thresholds, reference answers or existing test vectors change.

## Frozen-layer approval and compatibility

This is an explicit proposed correction to the frozen canonical module. It needs
maintainer/contract-owner approval before merge; the PR does not waive that gate.
`CANONICAL_VERSION` stays `canonical-json-v1` because unambiguous valid-input
bytes and digests are unchanged. Existing records created from ambiguous input
cannot be repaired from their digest alone. Retain history; investigate against
original input and create a reviewed revision rather than silently rehashing it.

## Executed validation

Environment: isolated Python 3.13.5, no solver, no network dependencies.
The local copies of the two baseline files were verified against Git blob hashes:

- canonical.py: `6d57a1f9dc152bdf0822ef84270ed4c94587be5f`
- tests/test_canonical.py: `db507f94fc77bd84d8c10ff0c83829dab54a9abb`

Executed the original 23 canonical tests plus 20 new input-integrity cases in
an isolated test directory (without the API-wide conftest):

- Baseline: **18 failed, 25 passed**.
- Patched: **43 passed**.

These are focused module results, not a rerun of the repository's 150-test claim.
Full-repository integration, DSH host and Windows/STAR execution: **NOT_RUN**.

## Required before merge

In a correctly provisioned isolated project environment:

```bash
python -m pytest tests/test_canonical.py tests/test_canonical_integrity.py -m mock -q
python -m pytest tests -m mock -q
```

Check that HTTP callers map CanonicalizationError to a validation response; this
PR normalizes the module exception, but does not claim to fix API error mapping.
