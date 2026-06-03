"""
Tests for MOD-2: get-next-card (queue/claim).

Spec: moderation/openapi.yaml POST /api/v1/queue/claim
Canon: moderation-flows.md#get-next-card

DoD test names (exact):
  next_returns_oldest_pending — claim returns oldest PENDING, transitions to IN_REVIEW
  concurrent_two_moderators_get_different_cards — SELECT FOR UPDATE SKIP LOCKED
  empty_queue_returns_204 — no PENDING → 204
  moderator_already_has_in_review_returns_409 — active ticket → 409
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4, UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from backend.auth import create_moderator_token
from backend.main import app
from backend.modules.moderation.models import ProductModeration

_TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5434/tochkamod_test",
)


def _token(moderator_id: UUID) -> str:
    return create_moderator_token(moderator_id)


def _auth(moderator_id: UUID) -> dict:
    return {"Authorization": f"Bearer {_token(moderator_id)}"}


async def _seed_product(
    status: str, product_id=None, queue_priority=3, moderator_id=None
) -> ProductModeration:
    """Insert a product_moderation row."""
    pid = product_id or uuid4()
    engine = create_async_engine(_TEST_DB_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        row = ProductModeration(
            product_id=pid,
            seller_id=uuid4(),
            status=status,
            queue_priority=queue_priority,
            json_before=None,
            json_after={"title": "Test"},
            moderator_id=moderator_id,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
    await engine.dispose()
    return row


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


@pytest.mark.asyncio
async def test_next_returns_oldest_pending():
    """Claim oldest PENDING → transitions to IN_REVIEW, assigned to moderator."""
    mod_id = uuid4()
    p1 = uuid4()

    await _seed_product("PENDING", product_id=p1, queue_priority=1)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/v1/queue/claim", headers=_auth(mod_id))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["product_id"] == str(p1)
    assert body["status"] == "IN_REVIEW"
    assert body["assigned_moderator_id"] == str(mod_id)
    assert body["claimed_at"] is not None
    assert body["claim_expires_at"] is not None
    assert body["kind"] == "CREATE"  # json_before is None


@pytest.mark.asyncio
async def test_concurrent_two_moderators_get_different_cards():
    """Two moderators claiming simultaneously get different tickets (SKIP LOCKED)."""
    mod1 = uuid4()
    mod2 = uuid4()
    p1 = uuid4()
    p2 = uuid4()

    await _seed_product("PENDING", product_id=p1, queue_priority=1)
    await _seed_product("PENDING", product_id=p2, queue_priority=1)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r1 = await client.post("/api/v1/queue/claim", headers=_auth(mod1))
        r2 = await client.post("/api/v1/queue/claim", headers=_auth(mod2))

    assert r1.status_code == 200
    assert r2.status_code == 200

    product_id_1 = UUID(r1.json()["product_id"])
    product_id_2 = UUID(r2.json()["product_id"])

    assert product_id_1 != product_id_2, "Two moderators should get different tickets"


@pytest.mark.asyncio
async def test_empty_queue_returns_204():
    """No PENDING tickets → 204 No Content."""
    mod_id = uuid4()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/v1/queue/claim", headers=_auth(mod_id))

    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_moderator_already_has_in_review_returns_409():
    """Moderator with active IN_REVIEW ticket cannot claim another → 409."""
    mod_id = uuid4()
    p1 = uuid4()
    p2 = uuid4()

    # Moderator already has IN_REVIEW ticket
    await _seed_product("IN_REVIEW", product_id=p1, moderator_id=mod_id)
    # Another PENDING ticket available
    await _seed_product("PENDING", product_id=p2, queue_priority=1)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/v1/queue/claim", headers=_auth(mod_id))

    assert resp.status_code == 409
    assert resp.json()["code"] == "MODERATOR_ALREADY_IN_REVIEW"


@pytest.mark.asyncio
async def test_claim_with_queue_priority_filter():
    """Filtering by queue_priority returns only that queue."""
    mod_id = uuid4()
    p1 = uuid4()
    p2 = uuid4()

    await _seed_product("PENDING", product_id=p1, queue_priority=1)
    await _seed_product("PENDING", product_id=p2, queue_priority=2)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/queue/claim",
            json={"queue_priority": 2},
            headers=_auth(mod_id),
        )

    assert resp.status_code == 200
    body = resp.json()
    assert UUID(body["product_id"]) == p2
    assert body["queue_priority"] == 2


@pytest.mark.asyncio
async def test_expired_in_review_returns_to_pending():
    """IN_REVIEW ticket with claim_expires_at < now is released back to PENDING."""
    mod_id = uuid4()
    other_mod = uuid4()
    p1 = uuid4()

    # Create IN_REVIEW ticket with expired claim
    await _seed_product("IN_REVIEW", product_id=p1, moderator_id=mod_id)

    # Manually expire it
    engine = create_async_engine(_TEST_DB_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        result = await db.execute(
            select(ProductModeration).where(ProductModeration.product_id == p1)
        )
        row = result.scalar_one()
        row.claimed_at = datetime.now(timezone.utc) - timedelta(minutes=31)
        row.claim_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        await db.commit()
    await engine.dispose()

    # Another moderator should be able to claim it (after expiry release)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/v1/queue/claim", headers=_auth(other_mod))

    assert resp.status_code == 200
    body = resp.json()
    assert UUID(body["product_id"]) == p1
    assert body["assigned_moderator_id"] == str(other_mod)


@pytest.mark.asyncio
async def test_kind_edit_when_json_before_present():
    """If json_before is not None, kind should be EDIT."""
    mod_id = uuid4()
    p1 = uuid4()

    engine = create_async_engine(_TEST_DB_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        row = ProductModeration(
            product_id=p1,
            seller_id=uuid4(),
            status="PENDING",
            queue_priority=3,
            json_before={"title": "Old"},
            json_after={"title": "New"},
        )
        db.add(row)
        await db.commit()
    await engine.dispose()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/v1/queue/claim", headers=_auth(mod_id))

    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "EDIT"
