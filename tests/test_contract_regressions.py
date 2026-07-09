from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt
from fastapi.testclient import TestClient

from app.config import JWT_ALGORITHM, JWT_SECRET
from app.main import app


client = TestClient(app)


def _future(hours: int) -> datetime:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).replace(
        minute=0, second=0, microsecond=0
    )


def _register_login(org: str, username: str = "alice") -> tuple[dict, dict]:
    reg = client.post(
        "/auth/register",
        json={"org_name": org, "username": username, "password": "pw12345"},
    )
    assert reg.status_code == 201, reg.text
    login = client.post(
        "/auth/login",
        json={"org_name": org, "username": username, "password": "pw12345"},
    )
    assert login.status_code == 200, login.text
    return reg.json(), login.json()


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _room(headers: dict, rate: int = 1000) -> int:
    response = client.post(
        "/rooms",
        json={"name": f"Room {uuid4().hex}", "capacity": 4, "hourly_rate_cents": rate},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _book(headers: dict, room_id: int, start: datetime, hours: int = 1):
    return client.post(
        "/bookings",
        json={
            "room_id": room_id,
            "start_time": start.isoformat(),
            "end_time": (start + timedelta(hours=hours)).isoformat(),
        },
        headers=headers,
    )


def _signed_token(overrides: dict | None = None, missing: set[str] | None = None) -> str:
    now = int(datetime.now(timezone.utc).timestamp())
    payload = {
        "sub": "1",
        "org": 1,
        "role": "admin",
        "jti": uuid4().hex,
        "iat": now,
        "exp": now + 900,
        "type": "access",
    }
    if overrides:
        payload.update(overrides)
    for claim in missing or set():
        payload.pop(claim, None)
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _assert_unauthorized(response):
    assert response.status_code == 401, response.text
    assert response.json()["code"] == "UNAUTHORIZED"


def test_auth_lifetime_logout_refresh_and_duplicate_registration():
    org = f"auth-{uuid4().hex}"
    _register_login(org)
    duplicate = client.post(
        "/auth/register",
        json={"org_name": org, "username": "alice", "password": "pw12345"},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "USERNAME_TAKEN"

    _, tokens = _register_login(f"tokens-{uuid4().hex}")
    access_payload = jwt.decode(tokens["access_token"], JWT_SECRET, algorithms=[JWT_ALGORITHM])
    assert access_payload["exp"] - access_payload["iat"] == 900

    refreshed = client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert refreshed.status_code == 200, refreshed.text
    reused = client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert reused.status_code == 401

    logout = client.post("/auth/logout", headers=_headers(tokens["access_token"]))
    assert logout.status_code == 200
    after_logout = client.get("/rooms", headers=_headers(tokens["access_token"]))
    assert after_logout.status_code == 401


def test_malformed_signed_access_tokens_return_401():
    cases = [
        _signed_token({"sub": "abc"}),
        _signed_token(missing={"sub"}),
        _signed_token(missing={"jti"}),
        _signed_token(missing={"type"}),
        _signed_token({"type": "nonsense"}),
        _signed_token({"role": "owner"}),
        _signed_token(missing={"org"}),
        _signed_token(missing={"iat"}),
        _signed_token(missing={"exp"}),
    ]
    for token in cases:
        _assert_unauthorized(client.get("/rooms", headers=_headers(token)))


def test_malformed_signed_refresh_tokens_return_401():
    base = {"type": "refresh"}
    cases = [
        _signed_token({**base, "sub": "abc"}),
        _signed_token(base, missing={"sub"}),
        _signed_token(base, missing={"jti"}),
        _signed_token(base, missing={"type"}),
        _signed_token({"type": "access"}),
        _signed_token({**base, "role": "owner"}),
        _signed_token(base, missing={"org"}),
        _signed_token(base, missing={"iat"}),
        _signed_token(base, missing={"exp"}),
    ]
    for token in cases:
        _assert_unauthorized(client.post("/auth/refresh", json={"refresh_token": token}))


def test_booking_window_utc_overlap_back_to_back_and_pagination():
    org = f"booking-{uuid4().hex}"
    _, tokens = _register_login(org)
    headers = _headers(tokens["access_token"])
    room_id = _room(headers)

    start = _future(72)
    offset_start = start.astimezone(timezone(timedelta(hours=6)))
    offset_end = (start + timedelta(hours=1)).astimezone(timezone(timedelta(hours=6)))
    created = client.post(
        "/bookings",
        json={
            "room_id": room_id,
            "start_time": offset_start.isoformat(),
            "end_time": offset_end.isoformat(),
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    assert created.json()["start_time"].startswith(start.isoformat().replace("+00:00", ""))

    overlap = _book(headers, room_id, start + timedelta(minutes=30), 1)
    assert overlap.status_code == 409
    assert overlap.json()["code"] == "ROOM_CONFLICT"

    back_to_back = _book(headers, room_id, start + timedelta(hours=1), 1)
    assert back_to_back.status_code == 201, back_to_back.text

    invalid = client.post(
        "/bookings",
        json={
            "room_id": room_id,
            "start_time": _future(90).isoformat(),
            "end_time": (_future(90) + timedelta(minutes=30)).isoformat(),
        },
        headers=headers,
    )
    assert invalid.status_code == 400
    assert invalid.json()["code"] == "INVALID_BOOKING_WINDOW"

    malformed = client.post(
        "/bookings",
        json={"room_id": room_id, "start_time": "not-a-date", "end_time": _future(91).isoformat()},
        headers=headers,
    )
    assert malformed.status_code == 400
    assert malformed.json()["code"] == "INVALID_BOOKING_WINDOW"

    listing = client.get("/bookings?page=1&limit=1", headers=headers)
    assert listing.status_code == 200
    body = listing.json()
    assert body["limit"] == 1
    assert body["total"] >= 2
    assert len(body["items"]) == 1
    assert body["items"][0]["id"] == created.json()["id"]


def test_cancel_refund_amount_and_live_reads():
    org = f"refund-{uuid4().hex}"
    _, tokens = _register_login(org)
    headers = _headers(tokens["access_token"])
    room_id = _room(headers, rate=1001)
    start = _future(30)

    booking = _book(headers, room_id, start, 1)
    assert booking.status_code == 201, booking.text
    booking_id = booking.json()["id"]

    assert client.get(f"/rooms/{room_id}/stats", headers=headers).json()[
        "total_confirmed_bookings"
    ] == 1
    date = start.date().isoformat()
    assert len(client.get(f"/rooms/{room_id}/availability?date={date}", headers=headers).json()["busy"]) == 1

    cancelled = client.post(f"/bookings/{booking_id}/cancel", headers=headers)
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["refund_percent"] == 50
    assert cancelled.json()["refund_amount_cents"] == 501

    detail = client.get(f"/bookings/{booking_id}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["refunds"][0]["amount_cents"] == cancelled.json()["refund_amount_cents"]

    assert client.post(f"/bookings/{booking_id}/cancel", headers=headers).status_code == 409
    assert client.get(f"/rooms/{room_id}/stats", headers=headers).json()[
        "total_confirmed_bookings"
    ] == 0
    assert client.get(f"/rooms/{room_id}/availability?date={date}", headers=headers).json()["busy"] == []
    report = client.get(f"/admin/usage-report?from={date}&to={date}", headers=headers)
    assert report.status_code == 200
    assert report.json()["rooms"][0]["confirmed_bookings"] == 0


def test_member_quota_applies_but_admin_is_exempt():
    org = f"quota-{uuid4().hex}"
    _, admin_tokens = _register_login(org, "admin")
    admin_headers = _headers(admin_tokens["access_token"])
    room_id = _room(admin_headers)

    _register_login(org, "member")
    member_login = client.post(
        "/auth/login",
        json={"org_name": org, "username": "member", "password": "pw12345"},
    )
    member_headers = _headers(member_login.json()["access_token"])

    member_start = _future(1)
    for hour in range(3):
        response = _book(member_headers, room_id, member_start + timedelta(hours=hour), 1)
        assert response.status_code == 201, response.text
    quota_response = _book(member_headers, room_id, member_start + timedelta(hours=3), 1)
    assert quota_response.status_code == 409
    assert quota_response.json()["code"] == "QUOTA_EXCEEDED"

    admin_start = _future(10)
    for hour in range(4):
        response = _book(admin_headers, room_id, admin_start + timedelta(hours=hour), 1)
        assert response.status_code == 201, response.text


def test_rate_limit_applies_to_admins_and_members():
    org = f"ratelimit-{uuid4().hex}"
    _, admin_tokens = _register_login(org, "admin")
    admin_headers = _headers(admin_tokens["access_token"])
    _register_login(org, "member")
    member_login = client.post(
        "/auth/login",
        json={"org_name": org, "username": "member", "password": "pw12345"},
    )
    member_headers = _headers(member_login.json()["access_token"])

    def exhaust(headers: dict) -> dict:
        latest = None
        for _ in range(21):
            latest = client.post(
                "/bookings",
                json={
                    "room_id": 999999,
                    "start_time": _future(72).isoformat(),
                    "end_time": (_future(73)).isoformat(),
                },
                headers=headers,
            )
        assert latest is not None
        return latest.json() | {"status_code": latest.status_code}

    admin_limited = exhaust(admin_headers)
    assert admin_limited["status_code"] == 429
    assert admin_limited["code"] == "RATE_LIMITED"

    member_limited = exhaust(member_headers)
    assert member_limited["status_code"] == 429
    assert member_limited["code"] == "RATE_LIMITED"


def test_cross_org_room_ids_are_not_found_for_export_and_booking_create():
    org_a = f"tenant-a-{uuid4().hex}"
    _, tokens_a = _register_login(org_a)
    headers_a = _headers(tokens_a["access_token"])
    room_a = _room(headers_a)

    org_b = f"tenant-b-{uuid4().hex}"
    _, tokens_b = _register_login(org_b)
    headers_b = _headers(tokens_b["access_token"])

    create_cross_org = _book(headers_b, room_a, _future(96), 1)
    assert create_cross_org.status_code == 404
    assert create_cross_org.json()["code"] == "ROOM_NOT_FOUND"

    export_cross_org = client.get(
        f"/admin/export?include_all=true&room_id={room_a}",
        headers=headers_b,
    )
    assert export_cross_org.status_code == 404
    assert export_cross_org.json()["code"] == "ROOM_NOT_FOUND"


def test_concurrent_create_and_cancel_keep_single_winner():
    org = f"concurrent-{uuid4().hex}"
    _, tokens = _register_login(org)
    headers = _headers(tokens["access_token"])
    room_id = _room(headers)
    start = _future(120)
    payload = {
        "room_id": room_id,
        "start_time": start.isoformat(),
        "end_time": (start + timedelta(hours=1)).isoformat(),
    }

    def create_once():
        with TestClient(app) as local_client:
            return local_client.post("/bookings", json=payload, headers=headers)

    with ThreadPoolExecutor(max_workers=2) as pool:
        created = list(pool.map(lambda _: create_once(), range(2)))
    assert sorted(response.status_code for response in created) == [201, 409]
    booking_id = next(response.json()["id"] for response in created if response.status_code == 201)

    def cancel_once():
        with TestClient(app) as local_client:
            return local_client.post(f"/bookings/{booking_id}/cancel", headers=headers)

    with ThreadPoolExecutor(max_workers=2) as pool:
        cancelled = list(pool.map(lambda _: cancel_once(), range(2)))
    assert sorted(response.status_code for response in cancelled) == [200, 409]
