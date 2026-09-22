# DSH panel proxy transport review (FR-32)

Base: d48f39bdf4aff67e01047fea120f410175d3c21b, 2026-09-22.

## User-visible failure

The native plugin client sets each panel's API base to `/dsh-sim/api`.
`panels/shared/api.js` sends JSON POST bodies and Idempotency-Key. The original
plugin proxy passes the method but neither the body, Content-Type nor the key.
A successful GET health check therefore does not demonstrate working panel
commands, including confirmations, authorization and review actions.

## Baseline reproduction

The original lib/index.js was verified against Git blob
115ce6695938e2258739c306a740e4c37d825437. Its upstreamOf/proxyFetch functions
were evaluated unchanged in an isolated Node VM with a fetch recorder:

- POST method retained: yes.
- Request body / Content-Type / Idempotency-Key retained: **no / no / no**.
- A crafted path could resolve outside the configured upstream origin.
  This was a URL calculation only, not a network request.

## Patch

Extract transport into lib/proxy.mjs, imported by the existing host entry point.
Preserve small POST bodies byte-for-byte, forward only required headers, retain
the existing dev identity handling, and return upstream trace IDs to the panel.
Constrain URL resolution to the configured origin and API/static path prefixes.
Allow GET/HEAD/POST for API resources and GET/HEAD for static panels. Surface
local 403/405/413 failures instead of reporting every input error as 503.

The default **1 MiB limit is for small panel commands**, not simulation results
or .sim file uploading. Large artifacts, streaming responses and broader method
support need a separate explicit transport contract. No new dependencies.

## Executed tests

Node v22.16.0: **16 passed** using the built-in Node test runner.
Fifteen tests use URL checks or deterministic fetch recorders; one uses an actual ephemeral
127.0.0.1 HTTP server to verify method, path, UTF-8 JSON bytes and idempotency key
at the upstream. The host entry point passed syntax checking.

```bash
node --test plugin/dsh-sim-plugin/tests/proxy.test.mjs
```

**NOT_RUN:** DSH host loading, browser UI rendering, Windows deployment, actual
engineering API authorization/review workflow and real solver execution. This
transport test does not replace end-to-end user acceptance. Keep the PR draft
until those host/API paths have been exercised.

## Security boundary remains explicit

The existing X-Dev headers remain development-only self-asserted identity.
This PR does not turn them into trusted authentication. Do not expose this
plugin/service as a production multi-user approval system. Follow up with
server-authenticated identity, project/node isolation, controlled read timeouts
and streaming/large-artifact handling before that rollout.

## Manual acceptance

In an isolated DSH development profile, verify that executor and reviewer panels
can submit commands; the upstream sees unchanged JSON and the same idempotency
key. Agent identities must still receive 403 for human-only actions. Repeating
a write with the same key must follow the API's existing idempotency contract.
Check that application 422/409 errors remain visible and retain trace IDs;
verify that offline services show UNAVAILABLE rather than a success badge.
