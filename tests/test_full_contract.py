import csv
import io
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta, timezone
from uuid import uuid4

import jwt
import pytest
from fastapi.testclient import TestClient

from app import auth
from app.config import JWT_ALGORITHM, JWT_SECRET
from app.database import SessionLocal
from app.main import app
from app.models import Booking, RefundLog
from app.services import ratelimit


client = TestClient(app)


@pytest.fixture(autouse=True)
def clear_process_state():
    with auth._token_state_lock:
        auth._revoked_tokens.clear()
        auth._used_refresh_tokens.clear()
    with ratelimit._lock:
        ratelimit._buckets.clear()


def _future(hours: int, minute: int = 0) -> datetime:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).replace(
        minute=minute, second=0, microsecond=0
    )


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _register(org: str, username: str = "alice", password: str = "pw12345"):
    return client.post(
        "/auth/register",
        json={"org_name": org, "username": username, "password": password},
    )


def _login(org: str, username: str = "alice", password: str = "pw12345") -> dict:
    response = client.post(
        "/auth/login",
        json={"org_name": org, "username": username, "password": password},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _register_login(org: str, username: str = "alice", password: str = "pw12345"):
    registered = _register(org, username, password)
    assert registered.status_code == 201, registered.text
    return registered.json(), _login(org, username, password)


def _create_room(headers: dict, name: str | None = None, rate: int = 1000) -> dict:
    response = client.post(
        "/rooms",
        json={
            "name": name or f"Room {uuid4().hex}",
            "capacity": 4,
            "hourly_rate_cents": rate,
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


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


def _signed_token(
    overrides: dict | None = None,
    missing: set[str] | None = None,
    token_type: str = "access",
) -> str:
    now = int(datetime.now(timezone.utc).timestamp())
    payload = {
        "sub": "1",
        "org": 1,
        "role": "admin",
        "jti": uuid4().hex,
        "iat": now,
        "exp": now + (900 if token_type == "access" else 7 * 24 * 60 * 60),
        "type": token_type,
    }
    if overrides:
        payload.update(overrides)
    for claim in missing or set():
        payload.pop(claim, None)
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _assert_code(response, status: int, code: str):
    assert response.status_code == status, response.text
    assert response.json()["code"] == code


def test_auth_contract_lifetimes_rotation_logout_and_bad_tokens():
    org = f"auth-{uuid4().hex}"

    first = _register(org, "admin")
    assert first.status_code == 201, first.text
    assert first.json()["role"] == "admin"

    second = _register(org, "member")
    assert second.status_code == 201, second.text
    assert second.json()["role"] == "member"

    duplicate = _register(org, "member")
    _assert_code(duplicate, 409, "USERNAME_TAKEN")

    tokens = _login(org, "admin")
    assert set(tokens) == {"access_token", "refresh_token", "token_type"}
    assert tokens["token_type"] == "bearer"

    bad_login = client.post(
        "/auth/login",
        json={"org_name": org, "username": "admin", "password": "wrong"},
    )
    _assert_code(bad_login, 401, "INVALID_CREDENTIALS")

    access_payload = jwt.decode(tokens["access_token"], JWT_SECRET, algorithms=[JWT_ALGORITHM])
    refresh_payload = jwt.decode(tokens["refresh_token"], JWT_SECRET, algorithms=[JWT_ALGORITHM])
    required = {"sub", "org", "role", "jti", "iat", "exp", "type"}
    assert required <= access_payload.keys()
    assert access_payload["exp"] - access_payload["iat"] == 900
    assert refresh_payload["exp"] - refresh_payload["iat"] == 7 * 24 * 60 * 60

    refreshed = client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["token_type"] == "bearer"
    assert refreshed.json()["access_token"] != tokens["access_token"]
    assert refreshed.json()["refresh_token"] != tokens["refresh_token"]

    reused = client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    _assert_code(reused, 401, "UNAUTHORIZED")

    refresh_as_access = client.get("/rooms", headers=_headers(refreshed.json()["refresh_token"]))
    _assert_code(refresh_as_access, 401, "UNAUTHORIZED")
    access_as_refresh = client.post(
        "/auth/refresh", json={"refresh_token": refreshed.json()["access_token"]}
    )
    _assert_code(access_as_refresh, 401, "UNAUTHORIZED")

    logout = client.post("/auth/logout", headers=_headers(refreshed.json()["access_token"]))
    assert logout.status_code == 200, logout.text
    after_logout = client.get("/rooms", headers=_headers(refreshed.json()["access_token"]))
    _assert_code(after_logout, 401, "UNAUTHORIZED")

    malformed_access = [
        _signed_token(missing={"sub"}),
        _signed_token({"sub": "not-int"}),
        _signed_token(missing={"jti"}),
        _signed_token(missing={"type"}),
        _signed_token({"type": "bogus"}),
        _signed_token({"role": "owner"}),
        _signed_token(missing={"org"}),
    ]
    for token in malformed_access:
        _assert_code(client.get("/rooms", headers=_headers(token)), 401, "UNAUTHORIZED")


def test_rooms_are_admin_scoped_and_cross_org_ids_are_hidden():
    org_a = f"rooms-a-{uuid4().hex}"
    _, admin_tokens = _register_login(org_a, "admin")
    admin_headers = _headers(admin_tokens["access_token"])
    _register_login(org_a, "member")
    member_headers = _headers(_login(org_a, "member")["access_token"])

    org_b = f"rooms-b-{uuid4().hex}"
    _, other_tokens = _register_login(org_b, "admin")
    other_headers = _headers(other_tokens["access_token"])

    room_a = _create_room(admin_headers, "Org A room")
    room_b = _create_room(other_headers, "Org B room")

    member_create = client.post(
        "/rooms",
        json={"name": "Nope", "capacity": 2, "hourly_rate_cents": 500},
        headers=member_headers,
    )
    _assert_code(member_create, 403, "FORBIDDEN")

    room_ids_a = {room["id"] for room in client.get("/rooms", headers=admin_headers).json()}
    room_ids_b = {room["id"] for room in client.get("/rooms", headers=other_headers).json()}
    assert room_a["id"] in room_ids_a
    assert room_b["id"] not in room_ids_a
    assert room_b["id"] in room_ids_b

    cross_availability = client.get(
        f"/rooms/{room_b['id']}/availability?date={_future(48).date().isoformat()}",
        headers=admin_headers,
    )
    _assert_code(cross_availability, 404, "ROOM_NOT_FOUND")


def test_booking_creation_windows_conflicts_quota_rate_limit_and_references():
    org = f"booking-{uuid4().hex}"
    _, admin_tokens = _register_login(org, "admin")
    admin_headers = _headers(admin_tokens["access_token"])
    _register_login(org, "member")
    member_headers = _headers(_login(org, "member")["access_token"])
    room_id = _create_room(admin_headers, rate=1234)["id"]
    other_room_id = _create_room(admin_headers, rate=900)["id"]

    start = _future(72)
    created = _book(admin_headers, room_id, start, 2)
    assert created.status_code == 201, created.text
    assert created.json()["price_cents"] == 2468
    assert created.json()["start_time"].endswith("+00:00")

    naive_start = _future(80).replace(tzinfo=None)
    naive = _book(admin_headers, room_id, naive_start, 1)
    assert naive.status_code == 201, naive.text
    assert naive.json()["start_time"].endswith("+00:00")

    offset_start = _future(90).astimezone(timezone(timedelta(hours=6)))
    offset = _book(admin_headers, room_id, offset_start, 1)
    assert offset.status_code == 201, offset.text
    assert offset.json()["start_time"].startswith(
        offset_start.astimezone(timezone.utc).replace(tzinfo=None).isoformat()
    )

    invalid_payloads = [
        (_future(-1), _future(1), "past start"),
        (_future(100), _future(99), "end before start"),
        (_future(101), _future(101) + timedelta(minutes=30), "non-whole hour"),
        (_future(102), _future(102) + timedelta(minutes=30), "less than one hour"),
        (_future(103), _future(103) + timedelta(hours=9), "more than eight hours"),
    ]
    for invalid_start, invalid_end, label in invalid_payloads:
        response = client.post(
            "/bookings",
            json={
                "room_id": room_id,
                "start_time": invalid_start.isoformat(),
                "end_time": invalid_end.isoformat(),
            },
            headers=admin_headers,
        )
        _assert_code(response, 400, "INVALID_BOOKING_WINDOW"), label

    malformed = client.post(
        "/bookings",
        json={"room_id": room_id, "start_time": "not-a-date", "end_time": _future(110).isoformat()},
        headers=admin_headers,
    )
    _assert_code(malformed, 400, "INVALID_BOOKING_WINDOW")

    overlap = _book(admin_headers, room_id, start + timedelta(minutes=30), 1)
    _assert_code(overlap, 409, "ROOM_CONFLICT")
    back_to_back = _book(admin_headers, room_id, start + timedelta(hours=2), 1)
    assert back_to_back.status_code == 201, back_to_back.text

    references = {created.json()["reference_code"], naive.json()["reference_code"]}
    for hours in range(120, 128):
        response = _book(admin_headers, other_room_id, _future(hours), 1)
        assert response.status_code == 201, response.text
        references.add(response.json()["reference_code"])
    assert len(references) == 10

    member_start = _future(2)
    for hour in (0, 2, 4):
        response = _book(member_headers, other_room_id, member_start + timedelta(hours=hour), 1)
        assert response.status_code == 201, response.text
    outside_window = _book(member_headers, other_room_id, _future(30), 1)
    assert outside_window.status_code == 201, outside_window.text
    quota = _book(member_headers, other_room_id, member_start + timedelta(hours=6), 1)
    _assert_code(quota, 409, "QUOTA_EXCEEDED")

    admin_extra = _book(admin_headers, other_room_id, _future(20), 1)
    assert admin_extra.status_code == 201, admin_extra.text

    _, limited_tokens = _register_login(f"rate-{uuid4().hex}", "admin")
    limited_headers = _headers(limited_tokens["access_token"])
    latest = None
    for _ in range(21):
        latest = client.post(
            "/bookings",
            json={
                "room_id": 999999,
                "start_time": _future(200).isoformat(),
                "end_time": _future(201).isoformat(),
            },
            headers=limited_headers,
        )
    _assert_code(latest, 429, "RATE_LIMITED")


def test_listing_detail_visibility_and_pagination_contract():
    org = f"visibility-{uuid4().hex}"
    _, admin_tokens = _register_login(org, "admin")
    admin_headers = _headers(admin_tokens["access_token"])
    _register_login(org, "member1")
    _register_login(org, "member2")
    member1_headers = _headers(_login(org, "member1")["access_token"])
    member2_headers = _headers(_login(org, "member2")["access_token"])
    room1 = _create_room(admin_headers)["id"]
    room2 = _create_room(admin_headers)["id"]

    same_start = _future(72)
    later = _future(75)
    first = _book(member1_headers, room1, same_start, 1)
    second = _book(member1_headers, room2, same_start, 1)
    third = _book(member1_headers, room1, later, 1)
    assert [first.status_code, second.status_code, third.status_code] == [201, 201, 201]

    other_member_booking = _book(member2_headers, room2, _future(80), 1)
    assert other_member_booking.status_code == 201, other_member_booking.text

    page1 = client.get("/bookings?page=1&limit=2", headers=member1_headers)
    page2 = client.get("/bookings?page=2&limit=2", headers=member1_headers)
    assert page1.status_code == 200, page1.text
    assert page2.status_code == 200, page2.text
    page1_body = page1.json()
    page2_body = page2.json()
    assert page1_body["page"] == 1
    assert page1_body["limit"] == 2
    assert page1_body["total"] == 3
    ids = [item["id"] for item in page1_body["items"] + page2_body["items"]]
    assert ids == [first.json()["id"], second.json()["id"], third.json()["id"]]
    assert len(ids) == len(set(ids))

    denied_detail = client.get(
        f"/bookings/{other_member_booking.json()['id']}", headers=member1_headers
    )
    _assert_code(denied_detail, 404, "BOOKING_NOT_FOUND")
    admin_detail = client.get(
        f"/bookings/{other_member_booking.json()['id']}", headers=admin_headers
    )
    assert admin_detail.status_code == 200, admin_detail.text

    org_b = f"visibility-b-{uuid4().hex}"
    _, b_tokens = _register_login(org_b, "admin")
    b_headers = _headers(b_tokens["access_token"])
    b_room = _create_room(b_headers)["id"]
    b_booking = _book(b_headers, b_room, _future(90), 1)
    assert b_booking.status_code == 201, b_booking.text
    cross_detail = client.get(f"/bookings/{b_booking.json()['id']}", headers=admin_headers)
    _assert_code(cross_detail, 404, "BOOKING_NOT_FOUND")


def test_cancellation_refund_tiers_permissions_and_refund_log_integrity():
    org = f"cancel-{uuid4().hex}"
    _, admin_tokens = _register_login(org, "admin")
    admin_headers = _headers(admin_tokens["access_token"])
    _register_login(org, "owner")
    _register_login(org, "other")
    owner_headers = _headers(_login(org, "owner")["access_token"])
    other_headers = _headers(_login(org, "other")["access_token"])
    room = _create_room(admin_headers, rate=1001)["id"]

    owner_booking = _book(owner_headers, room, _future(30), 1)
    assert owner_booking.status_code == 201, owner_booking.text
    denied = client.post(f"/bookings/{owner_booking.json()['id']}/cancel", headers=other_headers)
    _assert_code(denied, 404, "BOOKING_NOT_FOUND")

    owner_cancel = client.post(f"/bookings/{owner_booking.json()['id']}/cancel", headers=owner_headers)
    assert owner_cancel.status_code == 200, owner_cancel.text
    assert owner_cancel.json()["refund_percent"] == 50
    assert owner_cancel.json()["refund_amount_cents"] == 501
    again = client.post(f"/bookings/{owner_booking.json()['id']}/cancel", headers=owner_headers)
    _assert_code(again, 409, "ALREADY_CANCELLED")

    full_refund = _book(admin_headers, room, _future(72), 1)
    no_refund = _book(admin_headers, room, _future(2), 1)
    assert full_refund.status_code == 201, full_refund.text
    assert no_refund.status_code == 201, no_refund.text
    full_cancel = client.post(f"/bookings/{full_refund.json()['id']}/cancel", headers=admin_headers)
    no_cancel = client.post(f"/bookings/{no_refund.json()['id']}/cancel", headers=admin_headers)
    assert full_cancel.json()["refund_percent"] == 100
    assert full_cancel.json()["refund_amount_cents"] == 1001
    assert no_cancel.json()["refund_percent"] == 0
    assert no_cancel.json()["refund_amount_cents"] == 0

    db = SessionLocal()
    try:
        logs = db.query(RefundLog).filter(RefundLog.booking_id == owner_booking.json()["id"]).all()
        assert len(logs) == 1
        assert logs[0].amount_cents == owner_cancel.json()["refund_amount_cents"]
        cancelled_booking = db.query(Booking).filter(Booking.id == owner_booking.json()["id"]).one()
        assert cancelled_booking.status == "cancelled"
    finally:
        db.close()

    org_b = f"cancel-b-{uuid4().hex}"
    _, b_tokens = _register_login(org_b, "admin")
    b_headers = _headers(b_tokens["access_token"])
    b_room = _create_room(b_headers)["id"]
    b_booking = _book(b_headers, b_room, _future(100), 1)
    assert b_booking.status_code == 201, b_booking.text
    cross_cancel = client.post(f"/bookings/{b_booking.json()['id']}/cancel", headers=admin_headers)
    _assert_code(cross_cancel, 404, "BOOKING_NOT_FOUND")


def test_availability_stats_usage_report_and_export_are_live_and_scoped():
    org = f"report-{uuid4().hex}"
    _, admin_tokens = _register_login(org, "admin")
    admin_headers = _headers(admin_tokens["access_token"])
    _register_login(org, "member")
    member_headers = _headers(_login(org, "member")["access_token"])
    room_a = _create_room(admin_headers, "A", rate=700)["id"]
    room_b = _create_room(admin_headers, "B", rate=900)["id"]
    zero_room = _create_room(admin_headers, "Zero", rate=1100)["id"]

    member_report = client.get(
        f"/admin/usage-report?from={_future(48).date()}&to={_future(49).date()}",
        headers=member_headers,
    )
    _assert_code(member_report, 403, "FORBIDDEN")
    member_export = client.get("/admin/export", headers=member_headers)
    _assert_code(member_export, 403, "FORBIDDEN")

    base_day = (datetime.now(timezone.utc) + timedelta(days=5)).date()
    start_a = datetime.combine(base_day, time(8), tzinfo=timezone.utc)
    cancelled_start = start_a + timedelta(hours=3)
    start_b = start_a + timedelta(hours=5)
    admin_booking = _book(admin_headers, room_a, start_a, 2)
    member_booking = _book(member_headers, room_b, start_b, 1)
    cancelled = _book(admin_headers, room_a, cancelled_start, 1)
    assert admin_booking.status_code == 201, admin_booking.text
    assert member_booking.status_code == 201, member_booking.text
    assert cancelled.status_code == 201, cancelled.text

    date = start_a.date().isoformat()
    availability = client.get(f"/rooms/{room_a}/availability?date={date}", headers=admin_headers)
    assert availability.status_code == 200, availability.text
    assert [busy["start_time"] for busy in availability.json()["busy"]] == [
        admin_booking.json()["start_time"],
        cancelled.json()["start_time"],
    ]

    invalid_date = client.get(f"/rooms/{room_a}/availability?date=bad-date", headers=admin_headers)
    _assert_code(invalid_date, 400, "INVALID_BOOKING_WINDOW")

    stats_before = client.get(f"/rooms/{room_a}/stats", headers=admin_headers)
    assert stats_before.json()["total_confirmed_bookings"] == 2
    assert stats_before.json()["total_revenue_cents"] == 2100

    cancelled_response = client.post(f"/bookings/{cancelled.json()['id']}/cancel", headers=admin_headers)
    assert cancelled_response.status_code == 200, cancelled_response.text
    stats_after = client.get(f"/rooms/{room_a}/stats", headers=admin_headers)
    assert stats_after.json()["total_confirmed_bookings"] == 1
    assert stats_after.json()["total_revenue_cents"] == 1400

    availability_after = client.get(f"/rooms/{room_a}/availability?date={date}", headers=admin_headers)
    assert availability_after.json()["busy"] == [
        {"start_time": admin_booking.json()["start_time"], "end_time": admin_booking.json()["end_time"]}
    ]

    report = client.get(
        f"/admin/usage-report?from={start_a.date().isoformat()}&to={start_b.date().isoformat()}",
        headers=admin_headers,
    )
    assert report.status_code == 200, report.text
    rows = {row["room_id"]: row for row in report.json()["rooms"]}
    assert rows[room_a]["confirmed_bookings"] == 1
    assert rows[room_a]["revenue_cents"] == 1400
    assert rows[room_b]["confirmed_bookings"] == 1
    assert rows[zero_room]["confirmed_bookings"] == 0

    own_export = client.get("/admin/export?include_all=false", headers=admin_headers)
    all_export = client.get("/admin/export?include_all=true", headers=admin_headers)
    filtered_export = client.get(
        f"/admin/export?include_all=true&room_id={room_b}", headers=admin_headers
    )
    assert own_export.status_code == 200, own_export.text
    assert all_export.status_code == 200, all_export.text
    assert filtered_export.status_code == 200, filtered_export.text

    own_rows = list(csv.DictReader(io.StringIO(own_export.text)))
    all_rows = list(csv.DictReader(io.StringIO(all_export.text)))
    filtered_rows = list(csv.DictReader(io.StringIO(filtered_export.text)))
    assert own_export.text.splitlines()[0] == (
        "id,reference_code,room_id,user_id,start_time,end_time,status,price_cents"
    )
    assert str(admin_booking.json()["id"]) in {row["id"] for row in own_rows}
    assert str(member_booking.json()["id"]) not in {row["id"] for row in own_rows}
    assert {str(admin_booking.json()["id"]), str(member_booking.json()["id"])} <= {
        row["id"] for row in all_rows
    }
    assert {row["room_id"] for row in filtered_rows} == {str(room_b)}

    org_b = f"report-b-{uuid4().hex}"
    _, b_tokens = _register_login(org_b, "admin")
    b_headers = _headers(b_tokens["access_token"])
    b_room = _create_room(b_headers)["id"]
    b_booking = _book(b_headers, b_room, start_a, 1)
    assert b_booking.status_code == 201, b_booking.text

    scoped_report = client.get(
        f"/admin/usage-report?from={start_a.date().isoformat()}&to={start_a.date().isoformat()}",
        headers=admin_headers,
    )
    assert b_room not in {row["room_id"] for row in scoped_report.json()["rooms"]}
    cross_export = client.get(
        f"/admin/export?include_all=true&room_id={b_room}", headers=admin_headers
    )
    _assert_code(cross_export, 404, "ROOM_NOT_FOUND")


def test_concurrent_conflict_quota_refresh_cancel_and_liveness():
    org = f"concurrent-{uuid4().hex}"
    _, admin_tokens = _register_login(org, "admin")
    admin_headers = _headers(admin_tokens["access_token"])
    _register_login(org, "member")
    member_headers = _headers(_login(org, "member")["access_token"])
    room = _create_room(admin_headers)["id"]
    quota_room = _create_room(admin_headers)["id"]

    start = _future(120)
    payload = {
        "room_id": room,
        "start_time": start.isoformat(),
        "end_time": (start + timedelta(hours=1)).isoformat(),
    }

    def create_same_slot():
        with TestClient(app) as local_client:
            return local_client.post("/bookings", json=payload, headers=admin_headers).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        create_statuses = list(pool.map(lambda _: create_same_slot(), range(2)))
    assert sorted(create_statuses) == [201, 409]

    quota_start = _future(2)

    def create_quota(index: int):
        with TestClient(app) as local_client:
            start_time = quota_start + timedelta(hours=index * 2)
            response = local_client.post(
                "/bookings",
                json={
                    "room_id": quota_room,
                    "start_time": start_time.isoformat(),
                    "end_time": (start_time + timedelta(hours=1)).isoformat(),
                },
                headers=member_headers,
            )
            return response.status_code

    with ThreadPoolExecutor(max_workers=5) as pool:
        quota_statuses = list(pool.map(create_quota, range(5)))
    assert quota_statuses.count(201) == 3
    assert quota_statuses.count(409) == 2

    db = SessionLocal()
    try:
        confirmed = (
            db.query(Booking)
            .filter(Booking.room_id == quota_room, Booking.status == "confirmed")
            .count()
        )
        assert confirmed == 3
    finally:
        db.close()

    _, refresh_tokens = _register_login(f"refresh-race-{uuid4().hex}", "admin")

    def refresh_once():
        with TestClient(app) as local_client:
            return local_client.post(
                "/auth/refresh", json={"refresh_token": refresh_tokens["refresh_token"]}
            ).status_code

    with ThreadPoolExecutor(max_workers=3) as pool:
        refresh_statuses = list(pool.map(lambda _: refresh_once(), range(3)))
    assert refresh_statuses.count(200) == 1
    assert refresh_statuses.count(401) == 2

    cancel_booking = _book(admin_headers, room, _future(150), 1)
    assert cancel_booking.status_code == 201, cancel_booking.text
    booking_id = cancel_booking.json()["id"]

    def cancel_once():
        with TestClient(app) as local_client:
            return local_client.post(f"/bookings/{booking_id}/cancel", headers=admin_headers).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        cancel_statuses = list(pool.map(lambda _: cancel_once(), range(2)))
    assert sorted(cancel_statuses) == [200, 409]

    db = SessionLocal()
    try:
        assert db.query(RefundLog).filter(RefundLog.booking_id == booking_id).count() == 1
    finally:
        db.close()

    def valid_create_cancel(index: int):
        with TestClient(app) as local_client:
            start_time = _future(180 + index * 2)
            created = local_client.post(
                "/bookings",
                json={
                    "room_id": room,
                    "start_time": start_time.isoformat(),
                    "end_time": (start_time + timedelta(hours=1)).isoformat(),
                },
                headers=admin_headers,
            )
            if created.status_code != 201:
                return created.status_code
            cancelled = local_client.post(
                f"/bookings/{created.json()['id']}/cancel", headers=admin_headers
            )
            return cancelled.status_code

    with ThreadPoolExecutor(max_workers=3) as pool:
        lifecycle_statuses = list(pool.map(valid_create_cancel, range(3)))
    assert lifecycle_statuses == [200, 200, 200]
