# TidalPlayer low-risk hardening design

## Goal

Ship a first hardening batch that corrects isolated, testable defects without changing Tidal playback admission, LavaLink request ownership, controller transitions, Audio event reconciliation, or task teardown. Spotify remains supported and unchanged.

## Stage 1 scope

The first commit may change only these behaviors:

- Normalize Tidal search-cache keys while preserving the original provider query.
- Match non-Latin titles and artists with Unicode-aware normalization.
- Use the shared duration formatter in the Components V2 controller.
- Reject malformed numeric Tidal track, album, and video identifiers at the URL boundary.
- Synchronize OAuth snapshot reads with replacements and clears.
- Validate YouTube playlist metadata and pagination response shapes before indexing them.
- Sanitize provider failure logs so API keys, signed URLs, and credentials cannot appear.
- Report both displayed and total queue counts when queue output is truncated.
- Acknowledge controller interactions before Config, player, or Discord network work.
- Remove redundant zero-length sleeps and internal helpers only when repository search and tests prove they have no production caller.
- Add the official end-user data statement and describe the default Tidal stream quality accurately. This stage does not change the configured stream quality.

Each behavior change starts with a focused regression test. Existing commands, Config keys, public URLs, Spotify behavior, Tidal authentication, queue order, autoplay behavior, and LavaLink loading remain compatible.

## Deferred Stage 2 scope

The second batch will handle changes with a wider state or cancellation surface:

- asynchronous cog teardown and executor-future draining;
- Audio lifecycle reconciliation for skip, natural completion, disconnect, and queue changes;
- pending duplicate reservations and cancellation-safe queue commits;
- shorter guild-lock critical sections;
- event-authoritative controller replacement;
- just-in-time signed URL resolution for batches;
- explicit Tidal Track versus Video routing;
- node-readiness and LavaLink request-ownership changes;
- shared batch cancellation tokens and transient failure quarantine.

Stage 1 must not partially implement these items.

## Error handling and security

Provider logs will record operation, media identifier where safe, HTTP status, exception class, and elapsed time. They will not record request URLs, query strings, API keys, OAuth tokens, signed Tidal URLs, cookies, or raw exception text that may contain those values.

YouTube and URL parsing failures will return the cog's existing user-facing error paths. They will not fall through into Tidal text search when the input is a malformed provider URL.

## Verification

Before pushing to `main`:

1. Run each new regression test in its failing state, apply the fix, and rerun it.
2. Run `python -m pytest TidalPlayer/tests -q` in an environment containing the development requirements.
3. Run Ruff on every modified Python file.
4. Compile every modified TidalPlayer module.
5. Inspect the final diff for Stage 2 changes, unrelated cogs, credentials, generated files, and accidental Spotify changes.
6. Commit only the approved TidalPlayer files and this design document, then push the tested commit to `origin/main` without force.

If the full suite fails, do not push until the failure is fixed or proven to be an unrelated environment problem and the affected TidalPlayer behavior has an equivalent clean verification run.
