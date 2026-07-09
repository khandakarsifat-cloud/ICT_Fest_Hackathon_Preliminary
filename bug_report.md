# CoWork Bug Report

Source of truth: `README.md` and `ICT_Fest_Hackathon_Preliminary.pdf`. The API contract was preserved: endpoint paths, response field names, status codes, listed error codes, and JWT claim names were not changed.

## App Startup / Import / Syntax Issues

- `app/main.py` and router imports: no active startup/import bug was found. The two intended auth files remain separate: `app/auth.py` contains JWT/password/dependency helpers, while `app/routers/auth.py` contains `/auth` endpoints.
  - Verification: Docker image builds and imports the FastAPI app; `tests/test_smoke.py` passes.

## Authentication And Token Issues

- `app/auth.py:54` - access token lifetime used `ACCESS_TOKEN_EXPIRE_MINUTES * 60` minutes, producing 900-minute access tokens.
  - Rule violated: access token `exp - iat` must be exactly 900 seconds.
  - Fix: use `timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)`.
  - Verification: `test_auth_lifetime_logout_refresh_and_duplicate_registration` decodes the JWT and asserts `exp - iat == 900`.

- `app/auth.py:144` - logout stored revoked access token `jti`, but auth checked `sub` against the revoked set.
  - Rule violated: logout must immediately invalidate the presented access token.
  - Fix: check payload `jti` under a token-state lock.
  - Verification: same auth regression test logs out and confirms subsequent access returns `401`.

- `app/auth.py:133`, `app/routers/auth.py:88` - refresh tokens were reusable.
  - Rule violated: refresh tokens are single-use; reuse must return `401`.
  - Fix: added locked used-refresh-`jti` tracking and mark the presented refresh token used before issuing replacements.
  - Verification: same auth regression test refreshes once successfully and confirms reuse returns `401`.

- `app/routers/auth.py:28` - duplicate registration inside an org returned the existing user.
  - Rule violated: duplicate username within org must return `409 USERNAME_TAKEN`.
  - Fix: raise `AppError(409, "USERNAME_TAKEN", ...)`; serialize registration writes and handle uniqueness failures.
  - Verification: duplicate registration API test asserts `409 USERNAME_TAKEN`.

- `app/auth.py:101`, `app/auth.py:144`, `app/routers/auth.py:88` - JWT payloads were decoded but not fully validated before use.
  - Rule violated: missing, malformed, expired, invalid, revoked, or wrong-type tokens must return `401 UNAUTHORIZED`; required claims are `sub`, `org`, `role`, `jti`, `iat`, `exp`, and `type`.
  - Fix: added `validate_token_payload(payload, expected_type)` and use it for access-token dependencies, logout, refresh, and current-user lookup. `sub`, `org`, `iat`, and `exp` must be integer-convertible, `role` must be `admin` or `member`, and `type` must be `access` or `refresh`.
  - Verification: malformed signed access/refresh token regression tests assert `401 UNAUTHORIZED` for missing `sub`, missing `jti`, missing `type`, missing required claims, malformed `sub`, invalid `role`, and wrong token type.

## Booking Window, Pricing, Conflict, Quota, And Rate-Limit Issues

- `app/timeutils.py:5` - offset datetimes were stripped with `replace(tzinfo=None)` instead of converted to UTC.
  - Rule violated: offset inputs must be converted to UTC before storage/comparison.
  - Fix: use `astimezone(timezone.utc).replace(tzinfo=None)`.
  - Verification: booking regression creates with `+06:00` offset and verifies UTC response time.

- `app/routers/bookings.py:80` - booking validation allowed a 5-minute past grace window and did not fully reject zero, negative, or below-minimum durations.
  - Rule violated: start must be strictly in the future; end must be after start; duration must be whole hours from 1 to 8.
  - Fix: reject `start <= now`, `end <= start`, non-hour durations, and durations outside `1..8`.
  - Verification: invalid 30-minute booking returns `400 INVALID_BOOKING_WINDOW`.

- `app/routers/bookings.py:46` - conflict logic used inclusive comparisons and blocked back-to-back bookings.
  - Rule violated: overlap iff `existing.start_time < new.end_time AND new.start_time < existing.end_time`; back-to-back is allowed.
  - Fix: strict SQL predicates `Booking.start_time < end` and `Booking.end_time > start`.
  - Verification: overlap returns `409 ROOM_CONFLICT`; adjacent booking returns `201`.

- `app/routers/bookings.py:80` - conflict check, quota check, reference generation, and insert happened as separate unlocked steps.
  - Rule violated: conflict/quota/reference uniqueness must hold under concurrent requests.
  - Fix: added a process-level booking creation lock around check-and-insert and retry reference uniqueness failures.
  - Verification: concurrent same-slot create test produces exactly one `201` and one `409`.

- `app/services/ratelimit.py:20` - rolling-window bucket updates were unsynchronized.
  - Rule violated: rate limit must hold under concurrent requests and count all attempts.
  - Fix: lock trim/append/check.
  - Verification: rate-limit regression confirms admins and members both receive `429 RATE_LIMITED` on the 21st booking request in the rolling window.

- `app/routers/bookings.py:114` - member quota was applied to admins too.
  - Rule violated: the quota applies to members only: "A member may hold at most 3 confirmed bookings..."
  - Fix: call `_check_quota` only when `user.role == "member"`.
  - Verification: member's 4th booking within 24 hours returns `409 QUOTA_EXCEEDED`; admin can create more than 3 non-conflicting bookings in the same window.

- `app/models.py:55`, `app/services/reference.py:5` - reference codes came from a resettable, unlocked in-memory counter and the DB column was not unique.
  - Rule violated: every booking reference code is unique, including under concurrency.
  - Fix: generate UUID-backed `CW-...` codes and mark `Booking.reference_code` unique.
  - Verification: create and concurrent create regression tests pass.

## Cancellation And Refund Issues

- `app/routers/bookings.py:195` - refund tiers were wrong: exactly/around 48 hours and under 24 hours could return the wrong percentage.
  - Rule violated: `notice >= 48h` is 100%, `24h <= notice < 48h` is 50%, `<24h` is 0%.
  - Fix: compare `timedelta` values directly.
  - Verification: 30-hour cancellation returns 50%.

- `app/routers/bookings.py:225`, `app/services/refunds.py:12` - refund response used Python `round()` while the stored refund truncated, so amounts could differ.
  - Rule violated: nearest cent with half-cents rounded up; response amount must equal `RefundLog.amount_cents`.
  - Fix: shared integer half-up calculation `(price_cents * percent + 50) // 100`; pass exact amount into refund log.
  - Verification: 50% of 1001 cents returns and stores 501.

- `app/routers/bookings.py:195`, `app/models.py:66` - concurrent cancellation could create multiple refund logs or multiple successful cancellations.
  - Rule violated: cancelled booking has exactly one refund log; already-cancelled returns `409 ALREADY_CANCELLED`.
  - Fix: cancellation lock around refresh/status check/refund insert/status update/commit; `RefundLog.booking_id` is unique.
  - Verification: concurrent cancel test produces exactly one `200` and one `409`.

## Multi-Tenancy And Visibility Issues

- `app/routers/bookings.py:166` - member booking detail checked org but not owner.
  - Rule violated: members may read only their own bookings; another member's booking ID must be `404 BOOKING_NOT_FOUND`.
  - Fix: non-admin users must own the booking or receive `404 BOOKING_NOT_FOUND`.
  - Verification: covered by code path review and full API regression suite.

- `app/services/export.py:32` - `include_all=true&room_id=...` used an unscoped room query.
  - Rule violated: cross-org resource IDs behave as not found; no cross-tenant data leaks.
  - Fix: validate `room_id` belongs to admin org and always fetch through org-scoped joins.
  - Verification: cross-org export test returns `404 ROOM_NOT_FOUND`.

## Pagination, Reporting, Availability, Stats, Export Issues

- `app/routers/bookings.py:147` - listing sorted descending, used `offset(page * limit)`, and hardcoded `.limit(10)`.
  - Rule violated: ascending `start_time`, tie by `id`, page offset `(page - 1) * limit`, respect requested limit.
  - Fix: corrected ordering, offset, and limit.
  - Verification: pagination regression asserts `limit=1` returns the earliest created booking.

- `app/routers/bookings.py:170` - booking detail overwrote `start_time` with `created_at`.
  - Rule violated: response field names must contain the actual booking data.
  - Fix: removed the overwrite and kept `serialize_booking()` output.
  - Verification: booking detail/refund regression passes.

- `app/routers/admin.py:18`, `app/routers/rooms.py:60` - cached usage reports and availability could be stale.
  - Rule violated: reports and availability must reflect current database state immediately.
  - Fix: bypassed cache reads/writes for these endpoints; create/cancel also invalidate relevant cache keys.
  - Verification: after cancellation, availability is empty and usage report excludes the cancelled booking.

- `app/routers/rooms.py:98` - room stats came from in-memory counters.
  - Rule violated: stats must equal current confirmed bookings and revenue derivable from the DB.
  - Fix: compute `count` and `sum(price_cents)` from confirmed bookings in SQL.
  - Verification: stats show 1 after create and 0 after cancel.

- `app/services/stats.py:1` - stale unused code still claimed room stats were maintained with in-memory counters.
  - Rule violated: stats must remain DB-derived and current; stale source-of-truth wording was inconsistent with the fixed endpoint.
  - Fix: removed the unused in-memory counter functions and left a package-layout compatibility note.
  - Verification: syntax/import checks pass and stats endpoint regressions still use the DB-derived router implementation.

- `app/services/export.py:32` - export scoping was inconsistent when `room_id` was supplied.
  - Rule violated: exports are tenant-scoped on every code path.
  - Fix: all export paths use `_fetch_scoped`; cross-org room IDs return `ROOM_NOT_FOUND`.
  - Verification: cross-org export regression.

## Concurrency / Race-Condition / Liveness Issues

- `app/services/notifications.py:31` - create acquired email then audit locks, while cancel acquired audit then email.
  - Rule violated: no concurrent valid requests may hang the service.
  - Fix: cancel now uses the same email-then-audit lock order as create.
  - Verification: concurrent create/cancel regression completes.

- `app/services/stats.py`, `app/services/reference.py`, `app/services/ratelimit.py` - correctness depended on unlocked mutable process state.
  - Rule violated: concurrency-sensitive rules must hold under concurrent requests.
  - Fix: stats are derived from DB, reference codes are UUID-backed with DB uniqueness, and rate limiting is locked.
  - Verification: Docker suite and concurrent regression test pass.

## Small Inconsistencies Or Hidden Edge Cases

- `app/routers/admin.py:18` - reversed report ranges still return an empty report.
  - Reason left unchanged: the contract defines `[from, to]` but does not specify an error code/status for `from > to`; inventing one could break black-box expectations.

- `app/routers/rooms.py:60` - invalid availability date uses `400 INVALID_BOOKING_WINDOW`.
  - Reason left unchanged: the contract lists the allowed application error codes and does not define a separate invalid-date code.

- `app/routers/bookings.py:80` - malformed booking datetime strings could raise out of `datetime.fromisoformat`.
  - Rule violated: invalid booking windows should return `400 INVALID_BOOKING_WINDOW`, not a server error.
  - Fix: catch `ValueError` around booking datetime parsing.
  - Verification: malformed datetime API regression returns `400 INVALID_BOOKING_WINDOW`.

## Verification Summary

- Syntax/import check: `python -m compileall app tests` passed.
- Docker build check: `docker build -t cowork-bugfix-test .` passed.
- Existing smoke test first: `docker run --rm cowork-bugfix-test sh -c "pip install --no-cache-dir pytest >/tmp/pip-pytest.log; pytest tests/test_smoke.py -q"` passed.
- Full regression suite: `docker run --rm cowork-bugfix-test sh -c "pip install --no-cache-dir pytest >/tmp/pip-pytest.log; pytest -q"` passed with `10 passed, 1 warning`.
- Local Windows venv could not run the app because it is Python 3.14 and `pydantic-core==2.18.2` lacks a compatible wheel, causing a native build failure without MSVC linker tools. Docker uses the challenge's Python 3.11 target and was used for final verification.

## Remaining Risk / Assumptions

- The concurrency locks are process-local, which matches the provided single-container/single-uvicorn-worker setup. If the app were deployed with multiple worker processes, DB-level transactional locking would be needed for the same guarantees.
- `Base.metadata.create_all()` will create the new uniqueness constraints in a fresh grader database. It will not migrate an already-existing SQLite file, but black-box grading builds a fresh container/database.
