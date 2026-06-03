"""
Tests for MOD-6: blocking reasons reference.

Spec: moderation/openapi.yaml GET/POST/PATCH/DELETE /api/v1/blocking-reasons
Canon: moderation-flows.md#blocking-reasons

DoD test names (exact):
  list_returns_active_reasons          — happy path
  inactive_reasons_not_visible         — deactivated filtered out
  referenced_reason_cannot_be_deleted  — soft-deactivation preserves references
"""
from __future__ import annotations

import os
from uuid import uuid4, UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from backend.auth import create_moderator_token
from backend.main import app
from backend.modules.moderation.models import BlockingReason, ProductModeration

_TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5434/tochkamod_test",
)

_LIST_URL = "/api/v1/blocking-reasons"
_DETAIL_URL = "/api/v1/blocking-reasons/{reason_id}"


def _auth() -> dict:
    return {"Authorization": f"Bearer {create_moderator_token(uuid4())}"}


async def _seed_reason(*, hard_block: bool = False, is_active: bool = True, title: str = "Test") -> BlockingReason:
    engine = create_async_engine(_TEST_DB_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        row = BlockingReason(
            code=f"REASON_{uuid4().hex[:8].upper()}",
            title=title,
            hard_block=hard_block,
            is_active=is_active,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
    await engine.dispose()
    return row


async def _seed_ticket_with_reason(reason_id: UUID) -> ProductModeration:
    engine = create_async_engine(_TEST_DB_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        row = ProductModeration(
            product_id=uuid4(),
            seller_id=uuid4(),
            status="BLOCKED",
            queue_priority=3,
            json_before=None,
            json_after={"title": "Test", "skus": []},
            blocking_reason_ids=[str(reason_id)],
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
    await engine.dispose()
    return row


async def _get_reason(reason_id: UUID) -> BlockingReason | None:
    engine = create_async_engine(_TEST_DB_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        result = await db.execute(select(BlockingReason).where(BlockingReason.id == reason_id))
        row = result.scalar_one_or_none()
    await engine.dispose()
    return row


# ────────────────────────────────────────────────────────────────────────────
# DoD tests
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_returns_active_reasons():
    """
    GET /api/v1/blocking-reasons returns active reasons with id, title, hard_block.

    Verifies:
    - 200 OK, list of objects
    - Each object has id, code, title, hard_block, is_active
    - Seeded active reason appears in list
    """
    r1 = await _seed_reason(hard_block=False, title="Bad description")
    r2 = await _seed_reason(hard_block=True, title="Counterfeit")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(_LIST_URL, headers=_auth())

    assert resp.status_code == 200, resp.text
    items = resp.json()
    ids = {item["id"] for item in items}

    assert str(r1.id) in ids
    assert str(r2.id) in ids

    # Check response shape
    item = next(i for i in items if i["id"] == str(r1.id))
    assert "id" in item
    assert "title" in item
    assert "hard_block" in item
    assert "is_active" in item
    assert item["is_active"] is True


@pytest.mark.asyncio
async def test_inactive_reasons_not_visible():
    """
    Deactivated reasons (is_active=False) are not returned by default GET.

    By default is_active=True filter applies.
    Pass ?is_active=false to see deactivated ones.
    """
    active = await _seed_reason(is_active=True, title="Active reason")
    inactive = await _seed_reason(is_active=False, title="Inactive reason")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp_default = await client.get(_LIST_URL, headers=_auth())
        resp_inactive = await client.get(_LIST_URL, params={"is_active": "false"}, headers=_auth())

    assert resp_default.status_code == 200
    default_ids = {i["id"] for i in resp_default.json()}
    assert str(active.id) in default_ids
    assert str(inactive.id) not in default_ids

    inactive_ids = {i["id"] for i in resp_inactive.json()}
    assert str(inactive.id) in inactive_ids
    assert str(active.id) not in inactive_ids


@pytest.mark.asyncio
async def test_referenced_reason_cannot_be_deleted():
    """
    Deleting a reason referenced in product_moderation performs soft-deactivation.

    Physical delete is never done. The UUID in blocking_reason_ids JSON survives.
    The reason is hidden from the list (is_active=False) but the reference is intact.
    """
    reason = await _seed_reason(is_active=True)
    ticket = await _seed_ticket_with_reason(reason.id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.delete(_DETAIL_URL.format(reason_id=reason.id), headers=_auth())

    assert resp.status_code == 204, resp.text

    # Reason still exists in DB, just deactivated
    row = await _get_reason(reason.id)
    assert row is not None, "Reason must not be physically deleted"
    assert row.is_active is False, "Reason must be deactivated"

    # Historical reference in product_moderation preserved
    engine = create_async_engine(_TEST_DB_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        result = await db.execute(select(ProductModeration).where(ProductModeration.id == ticket.id))
        t = result.scalar_one()
        assert str(reason.id) in t.blocking_reason_ids
    await engine.dispose()

    # No longer visible in default list
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        list_resp = await client.get(_LIST_URL, headers=_auth())
    ids = {i["id"] for i in list_resp.json()}
    assert str(reason.id) not in ids


# ────────────────────────────────────────────────────────────────────────────
# Extra tests
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_filter_by_hard_block_true():
    """?hard_block=true returns only hard-block reasons."""
    soft = await _seed_reason(hard_block=False)
    hard = await _seed_reason(hard_block=True)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(_LIST_URL, params={"hard_block": "true"}, headers=_auth())

    assert resp.status_code == 200
    ids = {i["id"] for i in resp.json()}
    assert str(hard.id) in ids
    assert str(soft.id) not in ids


@pytest.mark.asyncio
async def test_create_blocking_reason():
    """POST creates a new reason, returns 201 with full object."""
    body = {"code": "BAD_PHOTOS", "title": "Photos are blurry", "hard_block": False}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(_LIST_URL, json=body, headers=_auth())

    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["code"] == "BAD_PHOTOS"
    assert data["hard_block"] is False
    assert data["is_active"] is True
    assert "id" in data


@pytest.mark.asyncio
async def test_create_duplicate_code_returns_409():
    """POST with duplicate code → 409 CODE_EXISTS."""
    await _seed_reason()

    # Force specific code via POST
    body = {"code": "UNIQUE_CODE_TEST", "title": "First", "hard_block": False}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r1 = await client.post(_LIST_URL, json=body, headers=_auth())
        r2 = await client.post(_LIST_URL, json=body, headers=_auth())

    assert r1.status_code == 201
    assert r2.status_code == 409
    assert r2.json()["code"] == "CODE_EXISTS"


@pytest.mark.asyncio
async def test_patch_blocking_reason():
    """PATCH updates title and is_active."""
    reason = await _seed_reason(title="Original", is_active=True)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.patch(
            _DETAIL_URL.format(reason_id=reason.id),
            json={"title": "Updated title", "is_active": False},
            headers=_auth(),
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["title"] == "Updated title"
    assert data["is_active"] is False


@pytest.mark.asyncio
async def test_unauthenticated_returns_401():
    """No JWT → 401."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(_LIST_URL)
    assert resp.status_code == 401
