"""
Tests for MOD-1: receive-product-events.

Spec: moderation/openapi.yaml POST /api/v1/b2b/events
Canon: moderation-flows.md#receive-product-events

DoD test names (exact):
  created_pending              — PRODUCT_CREATED → product_moderation row with PENDING
  edited_returns_to_review     — PRODUCT_EDITED after MODERATED → status back to PENDING
  edited_updates_in_review     — PRODUCT_EDITED while IN_REVIEW → fields updated, PENDING
  deleted_archived             — PRODUCT_DELETED removes the product_moderation row
  duplicate_event_no_side_effects — same idempotency_key → 202, no second effect
  missing_service_header_401   — no X-Service-Key → 401

Auth: X-Service-Key: dev-service-key
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from backend.main import app
from backend.modules.moderation.models import ProcessedEvent, ProductModeration

_TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5434/tochkamod_test",
)

_SERVICE_KEY = os.getenv("B2B_TO_MOD_KEY", "dev-service-key")
_HEADERS = {"X-Service-Key": _SERVICE_KEY}

_URL = "/api/v1/b2b/events"


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _event(event_type: str, payload: dict, *, idempotency_key=None) -> dict:
    return {
        "event_type": event_type,
        "idempotency_key": str(idempotency_key or uuid4()),
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "payload": payload,
    }


def _created_payload(product_id=None, seller_id=None, queue_priority=3) -> dict:
    return {
        "product_id": str(product_id or uuid4()),
        "seller_id": str(seller_id or uuid4()),
        "queue_priority": queue_priority,
        "json_after": {"title": "Test Product", "skus": [{"active_quantity": 5}]},
    }


def _edited_payload(product_id, seller_id=None, qty_after=5) -> dict:
    return {
        "product_id": str(product_id),
        "seller_id": str(seller_id or uuid4()),
        "queue_priority": 3,
        "json_before": {"title": "Old Title"},
        "json_after": {"title": "New Title", "skus": [{"active_quantity": qty_after}]},
    }


def _deleted_payload(product_id) -> dict:
    return {"product_id": str(product_id)}


async def _get_record(product_id) -> ProductModeration | None:
    engine = create_async_engine(_TEST_DB_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        result = await db.execute(
            select(ProductModeration).where(ProductModeration.product_id == product_id)
        )
        row = result.scalar_one_or_none()
    await engine.dispose()
    return row


async def _get_processed_event(key) -> ProcessedEvent | None:
    engine = create_async_engine(_TEST_DB_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        result = await db.execute(
            select(ProcessedEvent).where(ProcessedEvent.idempotency_key == key)
        )
        row = result.scalar_one_or_none()
    await engine.dispose()
    return row


async def _seed_product(status: str, product_id=None, seller_id=None) -> ProductModeration:
    """Insert a product_moderation row directly (bypasses the API)."""
    pid = product_id or uuid4()
    sid = seller_id or uuid4()
    engine = create_async_engine(_TEST_DB_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        row = ProductModeration(
            product_id=pid,
            seller_id=sid,
            status=status,
            queue_priority=3,
            json_before=None,
            json_after={"title": "Seeded Product", "skus": [{"active_quantity": 5}]},
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
    await engine.dispose()
    return row


# ──────────────────────────────────────────────────────────────────────────────
# DoD tests (exact names required)
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_created_pending():
    """
    PRODUCT_CREATED creates a product_moderation row with status=PENDING.

    Verifies:
    - 202 Accepted
    - product_moderation row exists with status=PENDING
    - json_before is null, json_after contains the product snapshot
    - idempotency_key stored in processed_events
    """
    product_id = uuid4()
    key = uuid4()
    payload = _created_payload(product_id=product_id, queue_priority=1)
    body = _event("PRODUCT_CREATED", payload, idempotency_key=key)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(_URL, json=body, headers=_HEADERS)

    assert resp.status_code == 202, resp.text

    row = await _get_record(product_id)
    assert row is not None, "product_moderation row must be created"
    assert row.status == "PENDING"
    assert row.json_before is None
    assert row.json_after is not None
    assert row.queue_priority == 1

    pe = await _get_processed_event(key)
    assert pe is not None, "idempotency_key must be stored in processed_events"


@pytest.mark.asyncio
async def test_edited_returns_to_review():
    """
    PRODUCT_EDITED after MODERATED returns the product to PENDING (queue_priority=3).

    Canon: MODERATED → (EDITED) → PENDING with recalculated priority.
    """
    product_id = uuid4()
    seeded = await _seed_product("MODERATED", product_id=product_id)

    payload = _edited_payload(product_id=product_id, qty_after=5)
    body = _event("PRODUCT_EDITED", payload)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(_URL, json=body, headers=_HEADERS)

    assert resp.status_code == 202, resp.text

    row = await _get_record(product_id)
    assert row is not None
    assert row.status == "PENDING"
    assert row.queue_priority == 3   # MODERATED + qty > 0 → priority 3
    assert row.json_before is not None
    assert row.json_after["title"] == "New Title"
    assert row.moderator_id is None  # reset on re-queue


@pytest.mark.asyncio
async def test_edited_updates_in_review():
    """
    PRODUCT_EDITED while IN_REVIEW updates content but keeps current priority.

    Canon: PENDING/IN_REVIEW — repeated EDITED → save current priority.
    """
    product_id = uuid4()
    await _seed_product("IN_REVIEW", product_id=product_id)

    # Manually set moderator_id to verify it gets cleared
    engine = create_async_engine(_TEST_DB_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        result = await db.execute(
            select(ProductModeration).where(ProductModeration.product_id == product_id)
        )
        row = result.scalar_one()
        row.moderator_id = uuid4()
        await db.commit()
    await engine.dispose()

    payload = _edited_payload(product_id=product_id, qty_after=3)
    body = _event("PRODUCT_EDITED", payload)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(_URL, json=body, headers=_HEADERS)

    assert resp.status_code == 202, resp.text

    row = await _get_record(product_id)
    assert row is not None
    assert row.status == "PENDING"
    assert row.queue_priority == 3   # kept from original seeded value
    assert row.json_after["title"] == "New Title"
    assert row.moderator_id is None  # cleared on re-queue


@pytest.mark.asyncio
async def test_deleted_archived():
    """
    PRODUCT_DELETED removes the product_moderation row.

    Canon: DELETED from any status → DELETE row (idempotent: OK if not found).
    Even HARD_BLOCKED rows are deleted on DELETED.
    """
    product_id = uuid4()
    await _seed_product("PENDING", product_id=product_id)

    payload = _deleted_payload(product_id=product_id)
    body = _event("PRODUCT_DELETED", payload)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(_URL, json=body, headers=_HEADERS)

    assert resp.status_code == 202, resp.text

    row = await _get_record(product_id)
    assert row is None, "product_moderation row must be deleted"


@pytest.mark.asyncio
async def test_duplicate_event_no_side_effects():
    """
    Repeating the same idempotency_key returns 202 without side effects.

    The product_moderation record is created once. Sending the same event again
    does NOT create a second record or update the existing one.
    """
    product_id = uuid4()
    key = uuid4()
    payload = _created_payload(product_id=product_id, queue_priority=2)
    body = _event("PRODUCT_CREATED", payload, idempotency_key=key)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r1 = await client.post(_URL, json=body, headers=_HEADERS)
        r2 = await client.post(_URL, json=body, headers=_HEADERS)  # same key

    assert r1.status_code == 202, r1.text
    assert r2.status_code == 202, r2.text   # idempotent replay

    row = await _get_record(product_id)
    assert row is not None
    assert row.queue_priority == 2   # unchanged by duplicate


@pytest.mark.asyncio
async def test_missing_service_header_401():
    """
    Request without X-Service-Key → 401 UNAUTHORIZED.
    """
    payload = _created_payload()
    body = _event("PRODUCT_CREATED", payload)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(_URL, json=body)   # no X-Service-Key

    assert resp.status_code == 401, resp.text
    assert resp.json()["code"] == "UNAUTHORIZED"


# ──────────────────────────────────────────────────────────────────────────────
# Extra quality tests
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_created_twice_same_product_id_is_noop():
    """
    Two CREATED events with different idempotency_keys for the same product_id.
    The second is treated as a no-op (product already exists in moderation queue).
    """
    product_id = uuid4()
    payload = _created_payload(product_id=product_id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r1 = await client.post(_URL, json=_event("PRODUCT_CREATED", payload), headers=_HEADERS)
        r2 = await client.post(_URL, json=_event("PRODUCT_CREATED", payload), headers=_HEADERS)

    assert r1.status_code == 202
    assert r2.status_code == 202   # no error — treated as no-op

    row = await _get_record(product_id)
    assert row is not None
    assert row.status == "PENDING"  # unchanged by second CREATED


@pytest.mark.asyncio
async def test_deleted_nonexistent_product_is_idempotent():
    """
    DELETED for a product not in the queue → 202 OK (idempotent).
    """
    payload = _deleted_payload(product_id=uuid4())
    body = _event("PRODUCT_DELETED", payload)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(_URL, json=body, headers=_HEADERS)

    assert resp.status_code == 202, resp.text


@pytest.mark.asyncio
async def test_hard_blocked_immune_to_edited():
    """
    PRODUCT_EDITED on HARD_BLOCKED product → 202 OK, record unchanged.
    """
    product_id = uuid4()
    await _seed_product("HARD_BLOCKED", product_id=product_id)

    payload = _edited_payload(product_id=product_id, qty_after=10)
    body = _event("PRODUCT_EDITED", payload)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(_URL, json=body, headers=_HEADERS)

    assert resp.status_code == 202, resp.text

    row = await _get_record(product_id)
    assert row is not None
    assert row.status == "HARD_BLOCKED"  # unchanged


@pytest.mark.asyncio
async def test_edited_blocked_product_gets_priority_2():
    """
    PRODUCT_EDITED on BLOCKED product → PENDING with queue_priority=2 (canon rule).
    Seller fixed the issue → high-priority re-review.
    """
    product_id = uuid4()
    await _seed_product("BLOCKED", product_id=product_id)

    payload = _edited_payload(product_id=product_id, qty_after=5)
    body = _event("PRODUCT_EDITED", payload)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(_URL, json=body, headers=_HEADERS)

    assert resp.status_code == 202
    row = await _get_record(product_id)
    assert row.status == "PENDING"
    assert row.queue_priority == 2


@pytest.mark.asyncio
async def test_invalid_service_key_returns_401():
    """Wrong X-Service-Key value → 401."""
    payload = _created_payload()
    body = _event("PRODUCT_CREATED", payload)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(_URL, json=body, headers={"X-Service-Key": "wrong-key"})

    assert resp.status_code == 401
