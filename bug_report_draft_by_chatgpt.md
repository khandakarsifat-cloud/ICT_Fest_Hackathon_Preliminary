# CoWork Bug Report Draft

Source of truth: `ICT_Fest_Hackathon_Preliminary.pdf` and `README.md`. The API contract must not change: same paths, status codes, error codes, JSON field names, and JWT claims.

## High-level note about the two `auth.py` files

There are two different files named `auth.py` in the project:

- `app/auth.py` should contain JWT creation/verification, password hashing, and auth dependencies.
- `app/routers/auth.py` should contain the `/auth/register`, `/auth/login`, `/auth/refresh`, and `/auth/logout` endpoints.

Keep them in separate folders. If they are flattened into one folder, one overwrites the other and imports break.

---

## 1. `app/timeutils.py` - UTC offset is parsed incorrectly

**Problem:** `parse_input_datetime()` removes timezone info with `replace(tzinfo=None)`. That does not convert the time to UTC. For example, `10:00+06:00` becomes `10:00 UTC` instead of `04:00 UTC`.

**Why wrong:** The rules say offset datetimes must be converted to UTC before storage/comparison.

**Probable fix:** When a timezone offset exists, convert using `astimezone(timezone.utc)`, then remove tzinfo for DB storage.

---

## 2. `app/auth.py` - access tokens last 900 minutes, not 900 seconds

**Problem:** `create_access_token()` uses `timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES * 60)`. With `ACCESS_TOKEN_EXPIRE_MINUTES = 15`, this makes the token lifetime 900 minutes.

**Why wrong:** Access token `exp - iat` must be exactly 900 seconds.

**Probable fix:** Use `timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)` or directly add `900` seconds.

---

## 3. `app/auth.py` - logout blacklist checks the wrong claim

**Problem:** Logout stores the token `jti`, but later `get_token_payload()` checks `payload["sub"]` against the revoked-token set.

**Why wrong:** A logged-out token can still be used because the stored value and checked value are different.

**Probable fix:** Check `payload["jti"]` against the revoked-token set.

---

## 4. `app/routers/auth.py` + `app/auth.py` - refresh tokens are not single-use

**Problem:** `/auth/refresh` decodes a refresh token and issues new tokens, but it never invalidates the used refresh token.

**Why wrong:** The rules require refresh tokens to be single-use. Reusing the same refresh token must return 401.

**Probable fix:** Store used refresh-token `jti`s. On refresh: reject if already used, otherwise mark it used before issuing the new access and refresh tokens. Use a lock or DB table so concurrent reuse cannot both succeed.

---

## 5. `app/routers/auth.py` - duplicate registration returns existing user instead of 409

**Problem:** If the username already exists in the org, `register()` returns that user object.

**Why wrong:** Duplicate username within the org must return `409 USERNAME_TAKEN`.

**Probable fix:** Raise `AppError(409, "USERNAME_TAKEN", ...)` instead of returning the existing user. Also catch DB `IntegrityError` for concurrent duplicate registration.

---

## 6. `app/routers/bookings.py` - start time has an illegal 5-minute grace window

**Problem:** The code rejects only if `start <= now - 5 minutes`.

**Why wrong:** The rules say `start_time` must be strictly in the future with no grace window.

**Probable fix:** Reject whenever `start <= now`.

---

## 7. `app/routers/bookings.py` - invalid booking duration can be accepted

**Problem:** The code checks whole hours and maximum 8 hours, but misses `end_time <= start_time` and minimum 1 hour. Zero-hour and negative bookings can slip through.

**Why wrong:** End must be strictly after start, and duration must be whole hours from 1 to 8.

**Probable fix:** First reject `end <= start`; then check total seconds is a multiple of 3600; then require `1 <= duration_hours <= 8`.

---

## 8. `app/routers/bookings.py` - conflict logic blocks back-to-back bookings

**Problem:** The conflict check uses `b.start_time <= end and start <= b.end_time`.

**Why wrong:** The official overlap rule is `existing.start_time < new.end_time AND new.start_time < existing.end_time`. Back-to-back bookings must be allowed.

**Probable fix:** Use strict `<` comparisons.

---

## 9. `app/routers/bookings.py` - double booking and quota checks are not concurrency-safe

**Problem:** Conflict check, quota check, reference generation, and insert happen as separate steps without a transaction/lock.

**Why wrong:** Two concurrent requests can both see no conflict/quota issue and both insert.

**Probable fix:** Put check-and-create inside one critical section. For SQLite, a practical fix is a global booking lock or `BEGIN IMMEDIATE` transaction around conflict check, quota check, reference generation, and insert. Keep it short and do not call slow notifications inside the lock.

---

## 10. `app/routers/bookings.py` - usage-report cache is not invalidated after booking creation

**Problem:** Creating a booking invalidates availability cache but not usage-report cache.

**Why wrong:** Usage report must reflect current state immediately.

**Probable fix:** Invalidate the org report cache after create, or remove the report cache.

---

## 11. `app/routers/bookings.py` - booking list ordering and pagination are wrong

**Problem:** List endpoint sorts by descending start time, uses `offset(page * limit)`, and hardcodes `.limit(10)`.

**Why wrong:** It must sort ascending by start time then id; page N starts at `(N - 1) * limit`; it must respect requested `limit`.

**Probable fix:** `order_by(start_time.asc(), id.asc()).offset((page - 1) * limit).limit(limit)`.

---

## 12. `app/routers/bookings.py` - members can read another member's booking in same org

**Problem:** `GET /bookings/{id}` checks org, but does not check owner for members.

**Why wrong:** Members may read only their own bookings. Another member's booking id must behave as `404 BOOKING_NOT_FOUND`.

**Probable fix:** After fetching, if caller is not admin and `booking.user_id != user.id`, return `404 BOOKING_NOT_FOUND`.

---

## 13. `app/routers/bookings.py` - single booking response overwrites `start_time` with `created_at`

**Problem:** `response["start_time"] = iso_utc(booking.created_at)`.

**Why wrong:** The response schema expects booking start time, not creation time.

**Probable fix:** Remove that overwrite. `serialize_booking()` already returns the correct start time.

---

## 14. `app/routers/bookings.py` - refund percentage tiers are wrong

**Problem:** Exact 48 hours gets 50% because the code uses `notice_hours > 48`; less than 24 hours gets 50% instead of 0%.

**Why wrong:** The policy is `notice >= 48h -> 100%`, `24h <= notice < 48h -> 50%`, `<24h -> 0%`.

**Probable fix:** Compare `notice` directly against `timedelta(hours=48)` and `timedelta(hours=24)`.

---

## 15. `app/routers/bookings.py` and `app/services/refunds.py` - refund rounding is wrong/inconsistent

**Problem:** The endpoint uses Python `round()` and the refund service truncates using `int(...)`.

**Why wrong:** The rules require nearest cent with half-cents rounded up. Example: 50% of 1001 must be 501. The response amount must equal the RefundLog amount.

**Probable fix:** Calculate cents using integer half-up formula, e.g. `(price_cents * percent + 50) // 100`, and use the same calculated value for both response and RefundLog.

---

## 16. `app/routers/bookings.py` and `app/services/refunds.py` - concurrent cancel can create multiple refunds

**Problem:** Status check, refund log creation, and status update are not atomic. `log_refund()` commits before booking status changes.

**Why wrong:** A cancelled booking must have exactly one RefundLog entry, even under concurrent cancel requests.

**Probable fix:** Lock/transaction around cancel. Check status, calculate refund, add exactly one RefundLog, set status to cancelled, commit once. Add a unique constraint on `RefundLog.booking_id` as a safety net.

---

## 17. `app/routers/bookings.py` - availability cache is not invalidated after cancel

**Problem:** Cancel invalidates report cache but not availability cache.

**Why wrong:** Availability must reflect current state immediately; a cancelled booking should disappear from busy intervals.

**Probable fix:** On cancel, invalidate availability for that room and booking start date.

---

## 18. `app/services/reference.py` and `app/models.py` - reference codes are not safely unique

**Problem:** Reference codes use an in-memory counter with no lock and the DB column is not unique. The counter also resets on app restart while the DB may persist.

**Why wrong:** Every booking reference code must be unique, including concurrent creation.

**Probable fix:** Add a unique constraint to `Booking.reference_code`. Generate references with a collision-resistant method or lock/retry on DB uniqueness failure.

---

## 19. `app/services/ratelimit.py` - rate limiter is not concurrency-safe

**Problem:** The per-user bucket is a shared dictionary/list updated without a lock.

**Why wrong:** Under concurrent requests, more than 20 booking requests in 60 seconds can be accepted.

**Probable fix:** Wrap trim-count-append-check in a lock. Keep counting all requests, including failed/excess ones.

---

## 20. `app/services/stats.py` and `app/routers/rooms.py` - room stats are stored in memory, not derived from DB

**Problem:** `/rooms/{id}/stats` reads an in-memory counter. It resets on restart and can lose updates under concurrency.

**Why wrong:** Stats must always equal current confirmed bookings and revenue derivable from the booking table.

**Probable fix:** Query the database in the stats endpoint: count confirmed bookings for the room and sum `price_cents`. This is simpler and correct.

---

## 21. `app/services/notifications.py` - deadlock risk

**Problem:** `notify_created()` locks email then audit. `notify_cancelled()` locks audit then email.

**Why wrong:** One create request and one cancel request can each hold one lock and wait forever, violating liveness.

**Probable fix:** Always acquire locks in the same order, or use one lock, or avoid nested locks.

---

## 22. `app/services/export.py` - cross-org data leak when `include_all=true&room_id=...`

**Problem:** If `include_all` is true and `room_id` is provided, it calls `fetch_bookings_raw()` which filters only by room id, not organization.

**Why wrong:** Multi-tenancy rule says users/admins must only access their own org's data; cross-org IDs should behave as 404.

**Probable fix:** Always verify the room belongs to the admin's org before exporting, and always include org filtering in export queries.

---

## 23. `app/cache.py`, `app/routers/admin.py`, `app/routers/rooms.py` - caches risk stale immediate reads

**Problem:** Cached usage reports/availability may remain after state changes unless every create/cancel path invalidates them perfectly.

**Why wrong:** Usage report and availability must reflect current state immediately.

**Probable fix:** Easiest safe fix: remove these caches for the competition. If keeping them, invalidate report on create/cancel and availability on create/cancel.

---

## 24. `app/models.py` - DB constraints are missing for concurrency guarantees

**Problem:** `Booking.reference_code` is indexed but not unique; `RefundLog.booking_id` is not unique.

**Why wrong:** The contract requires unique booking reference codes and exactly one refund log for cancelled bookings.

**Probable fix:** Add `unique=True` to `Booking.reference_code` and make `RefundLog.booking_id` unique. Still keep application-level locking/retry.

---

## 25. Possible packaging/import issue - two `auth.py` files must not occupy the same folder

**Problem:** The challenge has `app/auth.py` and `app/routers/auth.py`. If both are uploaded/copied as just `auth.py`, the router file can overwrite the helper file, or imports like `from ..auth` can fail outside the router package.

**Why wrong:** The app needs both files: one for security helpers and one for routes.

**Probable fix:** Restore the folder structure exactly:

```text
app/auth.py
app/routers/auth.py
```

Use `from ..auth import ...` inside `app/routers/auth.py` and `from .auth import ...` only in files directly under `app/`.
