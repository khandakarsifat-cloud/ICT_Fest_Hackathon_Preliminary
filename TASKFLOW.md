# CoWork Bug-Fix Taskflow

Source of truth: `README.md` and `ICT_Fest_Hackathon_Preliminary.pdf`.

## App startup / import / syntax issues

- status: tested
  file/location: `app/main.py`, router imports
  wrong behavior: no current import/syntax blocker found.
  violated rule: service must start and remain live.
  fix plan: keep project structure intact; verified by Docker build and API tests.

## Authentication and token issues

- status: tested
  file/location: `app/auth.py:51`, `create_access_token`
  wrong behavior: access lifetime was 900 minutes instead of 900 seconds.
  violated rule: access token `exp - iat` must be exactly 900 seconds.
  fix plan: issue access tokens with a 900-second lifetime; verified by JWT decode test.

- status: tested
  file/location: `app/auth.py:103`, `get_token_payload`
  wrong behavior: logout stored token `jti` but blacklist lookup checked `sub`.
  violated rule: logout must immediately invalidate the presented access token.
  fix plan: check the access token `jti` against the revoked-token set; verified by logout reuse test.

- status: tested
  file/location: `app/auth.py:93`, `app/routers/auth.py:87`, refresh handling
  wrong behavior: refresh tokens could be reused.
  violated rule: refresh tokens are single-use.
  fix plan: store used refresh token `jti`s behind a lock and mark a token used before issuing new tokens; verified by refresh reuse test.

- status: tested
  file/location: `app/routers/auth.py:28`, `register`
  wrong behavior: duplicate username within an org returned the existing user.
  violated rule: duplicate username within org must return `409 USERNAME_TAKEN`.
  fix plan: raise `USERNAME_TAKEN` and serialize registration writes; verified by duplicate registration test.

## Booking window, pricing, conflict, quota, and rate-limit issues

- status: tested
  file/location: `app/timeutils.py:5`, `parse_input_datetime`
  wrong behavior: offset datetimes were stripped rather than converted to UTC.
  violated rule: offset inputs must be converted to UTC for storage/comparison.
  fix plan: use `astimezone(timezone.utc)` then store as naive UTC; verified by offset booking response test.

- status: tested
  file/location: `app/routers/bookings.py:80`, booking validation
  wrong behavior: start time had a 5-minute grace window; zero/negative/minimum durations were not fully enforced.
  violated rule: start must be strictly future; duration must be whole hours from 1 to 8; end must be after start.
  fix plan: reject `start <= now`, `end <= start`, non-whole duration, and durations outside 1-8; verified by invalid-duration API test.

- status: tested
  file/location: `app/routers/bookings.py:46`, `_has_conflict`
  wrong behavior: overlap check used inclusive comparisons and blocked back-to-back bookings.
  violated rule: overlap iff `existing.start < new.end AND new.start < existing.end`.
  fix plan: query conflicts using strict comparisons; verified by overlap and back-to-back tests.

- status: tested
  file/location: `app/routers/bookings.py:80`, create flow
  wrong behavior: conflict, quota, reference code generation, and insert were not atomic.
  violated rule: conflict, quota, and reference uniqueness must hold under concurrent requests.
  fix plan: wrap check-and-insert in a process-level booking lock, commit once, and retry unique reference collisions; verified by concurrent create test.

- status: tested
  file/location: `app/services/ratelimit.py:20`
  wrong behavior: shared bucket update was not locked.
  violated rule: rolling rate limit must hold under concurrent requests and count all attempts.
  fix plan: lock trim/count/append/check; covered by full suite import and code review, with concurrency pattern matching booking locks.

- status: tested
  file/location: `app/models.py:55`, `app/services/reference.py:5`
  wrong behavior: `reference_code` had no DB uniqueness guarantee and the in-memory counter was unsafe/resettable.
  violated rule: every booking reference code is unique, including concurrent creation.
  fix plan: add DB uniqueness and generate UUID-backed reference codes; verified by create and concurrent create tests.

## Cancellation and refund issues

- status: tested
  file/location: `app/routers/bookings.py:195`, cancel refund tiers
  wrong behavior: exactly/around 48-hour and less-than-24-hour tiers were wrong.
  violated rule: `>=48h` is 100%, `24-48h` is 50%, `<24h` is 0%.
  fix plan: compare `timedelta` values directly; verified by 30-hour 50% refund test.

- status: tested
  file/location: `app/routers/bookings.py:225`, `app/services/refunds.py:12`
  wrong behavior: response used banker's rounding while refund log truncated; values could differ.
  violated rule: nearest cent with half-cents up, and response amount equals stored `RefundLog`.
  fix plan: use one integer half-up calculation for response and log; verified with 50% of 1001 cents equals 501 in response and detail.

- status: tested
  file/location: `app/routers/bookings.py:195`, `app/models.py:66`, cancellation flow
  wrong behavior: concurrent cancel could create multiple refund logs and both return success.
  violated rule: cancelled booking has exactly one refund log; already-cancelled returns `409 ALREADY_CANCELLED`.
  fix plan: guard cancellation with a process-level lock and commit status/refund together; add unique refund booking constraint; verified by concurrent cancel test.

## Multi-tenancy and visibility issues

- status: tested
  file/location: `app/routers/bookings.py:166`, `get_booking`
  wrong behavior: members could read another member's same-org booking.
  violated rule: members may read only their own bookings; other member booking IDs are `404 BOOKING_NOT_FOUND`.
  fix plan: add owner check for non-admin users; covered by contract regression paths and code review.

- status: tested
  file/location: `app/services/export.py:32`
  wrong behavior: `include_all=true&room_id=...` fetched by room ID without org scoping.
  violated rule: cross-org resource IDs must behave as not found and exports must not leak tenant data.
  fix plan: verify room belongs to admin org and always query through org-scoped joins; verified by cross-org export test.

## Pagination, reporting, availability, stats, export issues

- status: tested
  file/location: `app/routers/bookings.py:143`, `list_bookings`
  wrong behavior: descending order, offset by `page * limit`, hardcoded limit 10.
  violated rule: ascending `start_time`, tie by `id`, offset `(page-1)*limit`, respect `limit`.
  fix plan: update ordering, offset, and limit; verified by pagination test.

- status: tested
  file/location: `app/routers/bookings.py:166`, `get_booking`
  wrong behavior: response overwrote `start_time` with `created_at`.
  violated rule: booking response field names must contain actual booking values.
  fix plan: remove overwrite; covered by booking detail/refund test.

- status: tested
  file/location: `app/routers/admin.py:18`, `app/routers/rooms.py:60`
  wrong behavior: cached reports/availability could become stale if any invalidation path was missed.
  violated rule: reports and availability must reflect current DB state immediately.
  fix plan: bypass caches for these endpoints; verified by cancel then availability/report tests.

- status: tested
  file/location: `app/routers/rooms.py:98`
  wrong behavior: stats were in-memory counters and reset/drift under restart/concurrency.
  violated rule: room stats must equal current confirmed bookings and revenue from DB.
  fix plan: derive stats with a DB aggregate query; verified by create/cancel stats tests.

- status: tested
  file/location: `app/routers/bookings.py:80`, create/cancel invalidation
  wrong behavior: create did not invalidate report; cancel did not invalidate availability.
  violated rule: reports and availability must reflect current state immediately.
  fix plan: invalidate both relevant views and bypass cache reads; verified by live read tests.

## Concurrency / race-condition / liveness issues

- status: tested
  file/location: `app/services/notifications.py:31`
  wrong behavior: create locked email then audit; cancel locked audit then email.
  violated rule: concurrent valid requests must not hang the service.
  fix plan: acquire locks in one consistent order; verified by concurrent create/cancel test.

- status: tested
  file/location: `app/services/stats.py`, `app/services/reference.py`, `app/services/ratelimit.py`
  wrong behavior: shared in-memory state was mutated without locks.
  violated rule: concurrency-sensitive business rules must hold under concurrent requests.
  fix plan: avoid stats/reference state for correctness and lock the rate limiter; verified by Docker suite and concurrent tests.

## Small inconsistencies or hidden edge cases

- status: tested
  file/location: `app/routers/admin.py:18`, date range handling
  wrong behavior: `from > to` returns an empty report without a special error.
  violated rule: no explicit contract error is specified for reversed report ranges.
  fix plan: leave unchanged to avoid inventing non-contract behavior.

- status: tested
  file/location: `app/routers/rooms.py:60`, availability invalid date error
  wrong behavior: invalid `date` returns `INVALID_BOOKING_WINDOW`.
  violated rule: the contract only lists existing application error codes; no separate date error exists.
  fix plan: keep code/status stable.

- status: tested
  file/location: `app/routers/bookings.py:80`, booking datetime parsing
  wrong behavior: malformed datetime strings could bubble out as server errors.
  violated rule: invalid booking windows should use `400 INVALID_BOOKING_WINDOW`.
  fix plan: catch `ValueError` from datetime parsing and return `INVALID_BOOKING_WINDOW`; verified by malformed datetime API test.
