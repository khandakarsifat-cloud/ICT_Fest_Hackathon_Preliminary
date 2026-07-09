# CoWork Final Bug Report

Source of truth: `README.md` and `ICT_Fest_Hackathon_Preliminary.pdf`.

The API contract was preserved: endpoint paths, status codes, error codes, JSON field names, CSV header, and JWT claim names were not changed.

## App Startup / Import / Structure

- File/location: `app/auth.py`, `app/routers/auth.py`
  - Bug/issue: the project must keep authentication helpers and authentication routes separated. Mixing these responsibilities or importing through the wrong module path can break startup and route registration.
  - Fix: final structure keeps two separate files: `app/auth.py` contains JWT creation/validation, password hashing, token revocation, and auth dependencies; `app/routers/auth.py` contains `/auth/register`, `/auth/login`, `/auth/refresh`, and `/auth/logout` endpoints.
  - Verification: `app/routers/auth.py:8-18` imports helpers with `from ..auth import ...`; app-level modules use package-relative imports such as `app/main.py:4-6`.

- File/location: `app/main.py:8`
  - Bug/issue: schema creation is handled with `Base.metadata.create_all()`, which creates missing tables/constraints for a fresh SQLite database but does not migrate old existing SQLite schemas.
  - Fix: no migration system was introduced because that is outside the challenge scope. Final verification should use a fresh Docker volume.
  - Verification: Docker verification uses `docker compose down -v` before final testing.

## Authentication and JWT Bugs

- File/location: `app/auth.py:54-64`
  - Bug: access tokens previously had the wrong lifetime because the configured minute value was applied incorrectly.
  - Why incorrect: the contract requires access token `exp - iat` to be exactly 900 seconds.
  - Fix: access token expiry is now calculated from `timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)` and serialized into the JWT `exp` claim.
  - Verification: contract tests decode access tokens and assert `exp - iat == 900`.

- File/location: `app/auth.py:30-32`, `app/auth.py:91-124`, `app/auth.py:144-153`, `app/auth.py:156-165`, `app/routers/auth.py:87-98`
  - Bug: malformed but validly signed JWTs could be accepted too far into the request path or raise Python exceptions such as `KeyError`, `ValueError`, or `TypeError`, causing a 500 instead of a contract error.
  - Why incorrect: the contract requires signed tokens to contain claims `sub`, `org`, `role`, `jti`, `iat`, `exp`, and `type`; missing, malformed, wrong-type, expired, invalid, or revoked tokens must return `401 UNAUTHORIZED`.
  - Fix: added centralized `validate_token_payload(payload, expected_type)` validation. It verifies required claims, token type, integer-convertible `sub`/`org`/`iat`/`exp`, valid role, and non-empty string `jti`. Access dependencies, current-user lookup, logout, and refresh all use the validated payload path.
  - Verification: regression tests cover missing `sub`, non-integer `sub`, missing `jti`, missing `type`, invalid `type`, invalid `role`, missing `org`, and wrong token type.

- File/location: `app/auth.py:127-130`, `app/auth.py:144-153`, `app/routers/auth.py:101-104`
  - Bug: logout recorded a revoked token identifier but authenticated requests did not consistently reject the logged-out access token.
  - Why incorrect: the contract requires logout to immediately invalidate the presented access token; later use must return `401 UNAUTHORIZED`.
  - Fix: logout validates the access token payload and stores its `jti`; `get_token_payload()` checks the presented access token `jti` against the revoked set under `_token_state_lock`.
  - Verification: contract tests log out and then call an authenticated endpoint with the same access token, expecting `401 UNAUTHORIZED`.

- File/location: `app/auth.py:26-27`, `app/auth.py:133-141`, `app/routers/auth.py:87-98`
  - Bug: refresh tokens could be reused.
  - Why incorrect: the contract requires refresh tokens to be single-use; reuse must return `401 UNAUTHORIZED`.
  - Fix: refresh token `jti`s are recorded in `_used_refresh_tokens` under `_token_state_lock`; reuse raises `401 UNAUTHORIZED` before issuing replacement tokens.
  - Verification: contract tests refresh once successfully, then reuse the old refresh token and expect `401 UNAUTHORIZED`; concurrent refresh reuse tests allow only one success.

- File/location: `app/routers/auth.py:28-65`, `app/models.py:24-33`
  - Bug: duplicate registration within an organization could return an existing user or race incorrectly.
  - Why incorrect: duplicate username within the same org must return `409 USERNAME_TAKEN`; first user in a new org must be `admin`, later users must be `member`.
  - Fix: registration is serialized with `_register_lock`, checks for an existing `(org_id, username)`, raises `USERNAME_TAKEN`, and also handles database uniqueness failures. The database has `UniqueConstraint("org_id", "username")`.
  - Verification: contract tests verify first registration role `admin`, second same-org registration role `member`, and duplicate username `409 USERNAME_TAKEN`.

## Booking Window, Pricing, Conflict, Quota, and Rate-Limit Bugs

- File/location: `app/timeutils.py:5-14`, `app/routers/bookings.py:87-104`
  - Bug: offset datetimes were not normalized correctly, malformed datetime strings could escape as server errors, and booking windows allowed invalid durations or past starts.
  - Why incorrect: the contract requires offset datetimes to be converted to UTC, naive datetimes to be treated as UTC, start time to be strictly future, `end_time > start_time`, and whole-hour duration from 1 to 8 hours. Invalid booking windows must return `400 INVALID_BOOKING_WINDOW`.
  - Fix: `parse_input_datetime()` converts aware datetimes to UTC before storing them as naive UTC. Booking creation catches parse errors and validates strict future start, positive duration, whole hours, and 1-8 hour bounds.
  - Verification: contract tests cover naive datetime, offset datetime, past start, `end_time <= start_time`, non-whole-hour duration, less than 1 hour, more than 8 hours, and invalid datetime strings.

- File/location: `app/routers/bookings.py:46-57`
  - Bug: room conflict detection used the wrong overlap semantics and could reject back-to-back bookings.
  - Why incorrect: the contract defines overlap as `existing.start_time < new.end_time AND new.start_time < existing.end_time`; back-to-back bookings are allowed.
  - Fix: conflict detection now uses strict SQL predicates `Booking.start_time < end` and `Booking.end_time > start` for confirmed bookings.
  - Verification: contract tests verify overlapping bookings return `409 ROOM_CONFLICT` and back-to-back bookings return `201`.

- File/location: `app/routers/bookings.py:60-76`, `app/routers/bookings.py:106-137`
  - Bug: conflict checks, quota checks, reference generation, and insert could race during concurrent booking creation.
  - Why incorrect: no double-booking, member quota, and reference-code uniqueness must hold under concurrent requests.
  - Fix: booking creation uses `_booking_create_lock` around room lookup, conflict check, member quota check, reference creation, and insert/commit. Reference uniqueness failures are retried.
  - Verification: concurrency tests for the same room and slot produce exactly one `201` and one `409 ROOM_CONFLICT`; quota race tests keep final confirmed member bookings at 3.

- File/location: `app/routers/bookings.py:114-115`
  - Bug: the 3-booking quota applied to admins.
  - Why incorrect: the contract says a member may hold at most 3 confirmed bookings in the next 24 hours; admins are not subject to the member quota.
  - Fix: `_check_quota()` is called only when `user.role == "member"`.
  - Verification: contract tests verify a member's 4th in-window booking returns `409 QUOTA_EXCEEDED` while an admin can exceed the member quota.

- File/location: `app/services/ratelimit.py:20-29`
  - Bug: per-user rolling-window rate-limit buckets were mutated without synchronization.
  - Why incorrect: `POST /bookings` is limited to 20 requests per 60 seconds per user, all attempts count, and the rule must hold under concurrent requests.
  - Fix: trim, append, store, and limit check are performed under `_lock`.
  - Verification: contract tests send 21 booking attempts for the same user and expect `429 RATE_LIMITED` on the excess attempt.

- File/location: `app/services/reference.py:5-6`, `app/models.py:46-57`
  - Bug: booking reference codes were generated from unsafe process state and lacked a database uniqueness guarantee.
  - Why incorrect: every booking `reference_code` must be unique, including under concurrent creation.
  - Fix: reference codes are UUID-backed (`CW-...`) and `Booking.reference_code` is marked `unique=True`.
  - Verification: contract tests create many bookings and assert unique reference codes; concurrency tests cover simultaneous creation.

## Cancellation and Refund Bugs

- File/location: `app/routers/bookings.py:198-245`
  - Bug: cancellation could race, allowing multiple successful cancellations or multiple refund logs for one booking.
  - Why incorrect: cancelling an already cancelled booking must return `409 ALREADY_CANCELLED`; a cancelled booking must have exactly one `RefundLog`.
  - Fix: cancellation uses `_booking_cancel_lock`, refreshes the booking state under the lock, rejects already-cancelled bookings, writes the refund, changes status, and commits atomically for the process. `RefundLog.booking_id` is unique in `app/models.py:62-69`.
  - Verification: concurrent cancellation tests produce exactly one `200`, one `409 ALREADY_CANCELLED`, and one refund log.

- File/location: `app/routers/bookings.py:220-229`
  - Bug: refund tier boundaries around 24 and 48 hours were incorrect.
  - Why incorrect: the contract requires notice `>= 48h` to refund 100%, `24h <= notice < 48h` to refund 50%, and `< 24h` to refund 0%.
  - Fix: refund percent is selected by direct `timedelta` comparisons using the contract boundaries.
  - Verification: contract tests cover 100%, 50%, and 0% refund tiers.

- File/location: `app/services/refunds.py:12-23`, `app/routers/bookings.py:229-230`
  - Bug: refund amount calculation and stored refund amount could diverge, and half-cent cases were not rounded according to the contract.
  - Why incorrect: refund amounts must round to the nearest cent with half-cents up, and the cancel response amount must equal the stored `RefundLog.amount_cents`.
  - Fix: one shared integer formula `(price_cents * percent + 50) // 100` is used, and the computed amount is passed directly to `log_refund()`.
  - Verification: contract tests verify 50% of 1001 cents returns and stores 501 cents.

## Multi-Tenancy and Visibility Bugs

- File/location: `app/routers/bookings.py:175-184`, `app/routers/bookings.py:204-213`
  - Bug: booking detail and cancellation paths did not fully enforce member ownership.
  - Why incorrect: members may read and cancel only their own bookings; another member's booking ID must behave as not found with `404 BOOKING_NOT_FOUND`.
  - Fix: booking lookup is org-scoped through `Room.org_id`; non-admin users must also own the booking or receive `BOOKING_NOT_FOUND`.
  - Verification: contract tests verify member read/cancel denial for another member's booking and admin access to same-org bookings.

- File/location: `app/routers/rooms.py:28-32`, `app/routers/rooms.py:35-56`, `app/routers/rooms.py:59-113`
  - Bug: room list, room creation, availability, and stats needed consistent organization scoping.
  - Why incorrect: cross-org room IDs must behave as non-existent with `404 ROOM_NOT_FOUND`, and no cross-tenant data may leak.
  - Fix: room reads use `_get_org_room()` or direct `Room.org_id == user.org_id` filters; room creation requires `require_admin`.
  - Verification: contract tests verify admins create rooms, members cannot create rooms, users list only own-org rooms, and cross-org room IDs return `ROOM_NOT_FOUND`.

- File/location: `app/services/export.py:23-47`
  - Bug: CSV export with `room_id` could query by room ID without first proving the room belonged to the admin's org.
  - Why incorrect: exports are tenant-scoped; cross-org `room_id` must return `404 ROOM_NOT_FOUND` and must not leak data.
  - Fix: `generate_export()` validates `room_id` with both `Room.id` and `Room.org_id`, then all export rows are fetched through an org-scoped join.
  - Verification: contract tests verify `include_all=false`, `include_all=true`, `room_id` filtering, and cross-org `room_id` rejection.

## Pagination, Reporting, Availability, Stats, Export Bugs

- File/location: `app/routers/bookings.py:146-166`
  - Bug: booking listing used incorrect ordering, offset, or limit handling.
  - Why incorrect: `GET /bookings` must return only the caller's own bookings, sorted by ascending `start_time` and ascending `id`, with offset `(page - 1) * limit`, and include `items`, `page`, `limit`, and `total`.
  - Fix: listing now filters by `Booking.user_id == user.id`, orders by `Booking.start_time.asc(), Booking.id.asc()`, offsets with `(page - 1) * limit`, respects `limit`, and returns `total`.
  - Verification: contract tests verify pagination pages do not skip or repeat and preserve ordering.

- File/location: `app/routers/bookings.py:186-195`
  - Bug: booking detail serialization previously risked returning incorrect booking fields.
  - Why incorrect: response field names must contain the actual booking values, and detail must include `refunds`.
  - Fix: booking detail uses `serialize_booking()` and adds refund entries without overwriting booking fields.
  - Verification: contract tests read booking detail and verify refund data after cancellation.

- File/location: `app/routers/admin.py:17-56`
  - Bug: usage reports could be stale or fail to include zero-booking rooms.
  - Why incorrect: the report must include all rooms in the admin's org, count only confirmed bookings with UTC start dates in `[from, to]` inclusive, exclude cancelled bookings, and reflect current state immediately.
  - Fix: usage report computes current data directly from the database per org room and confirmed bookings in the requested UTC date range.
  - Verification: contract tests verify zero-booking rooms, cancellation exclusion, current-state reads, admin-only access, inclusive date range behavior, and tenant scoping.

- File/location: `app/routers/rooms.py:59-94`
  - Bug: availability could be stale or include cancelled bookings.
  - Why incorrect: availability must show only confirmed bookings starting on the requested UTC date, sorted ascending, and reflect cancellations immediately.
  - Fix: availability validates the date, scopes the room to the caller's org, queries confirmed bookings directly from the database for that UTC day, and orders ascending.
  - Verification: contract tests verify sorted busy intervals, invalid date `400 INVALID_BOOKING_WINDOW`, cancelled bookings disappearing immediately, and cross-org room `404 ROOM_NOT_FOUND`.

- File/location: `app/routers/rooms.py:97-113`, `app/services/stats.py:1-6`
  - Bug: room stats previously depended on in-memory counters that could be stale after cancellation, restart, or concurrent activity.
  - Why incorrect: `/rooms/{id}/stats` must always equal current confirmed bookings and summed `price_cents` derivable from the database.
  - Fix: stats are now derived directly from the database in `app/routers/rooms.py` with `count()` and `sum(price_cents)` over confirmed bookings. `app/services/stats.py` is only kept as a layout-preserving placeholder explaining that stats are DB-derived.
  - Verification: contract tests verify stats after creates and cancellations and confirm cancelled bookings are excluded.

- File/location: `app/services/export.py:11-20`, `app/services/export.py:49-65`
  - Bug: CSV export needed to preserve the exact contract header and UTC datetime formatting.
  - Why incorrect: the contract requires the exact header `id,reference_code,room_id,user_id,start_time,end_time,status,price_cents`.
  - Fix: export uses a fixed `EXPORT_HEADER` and serializes datetimes through `iso_utc()`.
  - Verification: contract tests assert the exact CSV header and scoped row behavior.

## Concurrency / Liveness Bugs

- File/location: `app/routers/bookings.py:27-28`, `app/routers/bookings.py:106-137`, `app/routers/bookings.py:215-234`
  - Bug: booking creation and cancellation rules could fail under concurrent requests.
  - Why incorrect: no double-booking, member quota, reference uniqueness, single refund log, and already-cancelled behavior must hold under concurrency.
  - Fix: process-local locks protect create and cancel critical sections in the expected single-container challenge setup.
  - Verification: concurrency tests cover same-slot create, member quota race, refresh-token reuse race, same-booking cancel race, and concurrent valid create/cancel operations.

- File/location: `app/services/notifications.py:24-35`
  - Bug: notification side-effect locks could be acquired in inconsistent order.
  - Why incorrect: no combination of concurrent valid requests may hang the service.
  - Fix: create and cancel notification paths both acquire `_email_lock` before `_audit_lock`.
  - Verification: concurrent create/cancel lifecycle tests complete without hanging.

## Docker Verification Summary

- Docker image builds successfully.
- App starts through `docker compose up --build`.
- `/health` returns `{"status":"ok"}`.
- Syntax/import/startup checks were run in Docker/Python 3.11:
  - `python -m compileall app`
  - `python -c 'from app.main import app; print(app.title)'`
- Regression/API tests were run inside the Docker image:
  - `pytest tests/test_smoke.py -q` passed with `1 passed`.
  - `pytest tests/test_full_contract.py -q` passed with `7 passed`.
  - `pytest -q` passed with `17 passed`.
- Final verification should run `docker compose down -v` before testing to ensure a fresh SQLite volume.

## Remaining Risks / Assumptions

- Process-local locks match the expected single-container Docker challenge setup. A multi-worker production deployment would require database-level locking or transactional constraints for the same concurrency guarantees.
- Token revocation, used-refresh-token tracking, and rate-limit buckets are in-memory process state, which is acceptable for the challenge's single-process scope but not sufficient for distributed production.
- `Base.metadata.create_all()` creates tables and constraints for a fresh SQLite database, but it does not migrate old existing SQLite schemas. Use a fresh Docker volume for final verification and grading.
