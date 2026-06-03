"""
Tests for MOD-4: soft-block.

Spec: moderation/openapi.yaml POST /api/v1/tickets/{ticket_id}/block
Canon: moderation-flows.md#soft-block

DoD test names (exact):
  soft_block_transitions_to_blocked_with_field_reports — happy path
  soft_block_emits_event_to_b2b                       — B2B event BLOCKED + hard_block=false
  soft_block_unknown_reason_returns_400               — invalid blocking_reason_id
  soft_block_others_card_returns_403                  — IDOR guard
  soft_block_invalid_field_name_returns_400           — FieldReport validation error
"""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4, UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from backend.auth import create_moderator_token
from backend.main import app
from backend.modules.moderation.models import BlockingReason, FieldReport, ProductModeration

_TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5434/tochkamod_test",
)

_URL = "/api/v1/tickets/{ticket_id}/block"


def _auth(moderator_id: UUID) -> dict:
    return {"Authorization": f"Bearer {create_moderator_token(moderator_id)}"}


async def _seed_ticket(
    *,
    status: str,
    moderator_id=None,
) -> ProductModeration:
    engine = create_async_engine(_TEST_DB_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        row = ProductModeration(
            product_id=uuid4(),
            seller_id=uuid4(),
            status=status,
            queue_priority=3,
            json_before=None,
            json_after={"title": "Test", "skus": [{"active_quantity": 5}]},
            moderator_id=moderator_id,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
    await engine.dispose()
    return row


async def _seed_reason(*, hard_block: bool = False, is_active: bool = True) -> BlockingReason:
    engine = create_async_engine(_TEST_DB_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        row = BlockingReason(
            code=f"REASON_{uuid4().hex[:8].upper()}",
            title="Test reason",
            description="Used in tests",
            hard_block=hard_block,
            is_active=is_active,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
    await engine.dispose()
    return row


async def _get_field_reports(ticket_id: UUID) -> list[FieldReport]:
    engine = create_async_engine(_TEST_DB_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        result = await db.execute(
            select(FieldReport).where(FieldReport.product_moderation_id == ticket_id)
        )
        rows = list(result.scalars())
    await engine.dispose()
    return rows


async def _get_ticket(ticket_id: UUID) -> ProductModeration | None:
    engine = create_async_engine(_TEST_DB_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        result = await db.execute(
            select(ProductModeration).where(ProductModeration.id == ticket_id)
        )
        row = result.scalar_one_or_none()
    await engine.dispose()
    return row


def _mock_b2b_ok():
    mock_client = AsyncMock()
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.aclose = AsyncMock()
    return patch("backend.modules.tickets.service_block.httpx.AsyncClient", return_value=mock_client), mock_client


# ────────────────────────────────────────────────────────────────────────────
# DoD tests
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_soft_block_transitions_to_blocked_with_field_reports():
    """
    Happy path: IN_REVIEW → BLOCKED, field_reports saved, TicketResponse returned.

    Verifies:
    - 200 OK with status=BLOCKED in body
    - DB ticket has status=BLOCKED, decision_at set, blocking_reason_ids stored
    - FieldReport rows created in DB
    """
    mod_id = uuid4()
    ticket = await _seed_ticket(status="IN_REVIEW", moderator_id=mod_id)
    reason = await _seed_reason(hard_block=False)

    body = {
        "blocking_reason_ids": [str(reason.id)],
        "comment": "Description copied",
        "field_reports": [
            {"field_path": "description", "message": "Text copied from another product", "severity": "ERROR"},
            {"field_path": "images[0]", "message": "Blurry image", "severity": "WARNING"},
        ],
    }

    cm, _ = _mock_b2b_ok()
    with cm:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(_URL.format(ticket_id=ticket.id), json=body, headers=_auth(mod_id))

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "BLOCKED"
    assert data["id"] == str(ticket.id)

    row = await _get_ticket(ticket.id)
    assert row.status == "BLOCKED"
    assert row.decision_at is not None
    assert row.moderator_comment == "Description copied"
    assert str(reason.id) in row.blocking_reason_ids

    reports = await _get_field_reports(ticket.id)
    assert len(reports) == 2
    paths = {r.field_path for r in reports}
    assert "description" in paths
    assert "images[0]" in paths


@pytest.mark.asyncio
async def test_soft_block_emits_event_to_b2b():
    """
    BLOCKED event is sent to B2B with event_type=BLOCKED and hard_block=False.
    """
    mod_id = uuid4()
    ticket = await _seed_ticket(status="IN_REVIEW", moderator_id=mod_id)
    reason = await _seed_reason(hard_block=False)

    cm, mock_client = _mock_b2b_ok()
    with cm:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                _URL.format(ticket_id=ticket.id),
                json={"blocking_reason_ids": [str(reason.id)]},
                headers=_auth(mod_id),
            )

    assert resp.status_code == 200, resp.text

    mock_client.post.assert_awaited_once()
    event = mock_client.post.call_args.kwargs["json"]
    assert event["event_type"] == "BLOCKED"
    assert event["hard_block"] is False
    assert event["blocking_reason_id"] == str(reason.id)
    assert event["product_id"] == str(ticket.product_id)


@pytest.mark.asyncio
async def test_soft_block_unknown_reason_returns_400():
    """
    Non-existent blocking_reason_id → 400 UNKNOWN_REASON.
    """
    mod_id = uuid4()
    ticket = await _seed_ticket(status="IN_REVIEW", moderator_id=mod_id)

    cm, _ = _mock_b2b_ok()
    with cm:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                _URL.format(ticket_id=ticket.id),
                json={"blocking_reason_ids": [str(uuid4())]},
                headers=_auth(mod_id),
            )

    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "UNKNOWN_REASON"

    row = await _get_ticket(ticket.id)
    assert row.status == "IN_REVIEW"


@pytest.mark.asyncio
async def test_soft_block_others_card_returns_403():
    """
    Moderator cannot soft-block a ticket assigned to someone else → 403.
    """
    owner_mod = uuid4()
    caller_mod = uuid4()
    ticket = await _seed_ticket(status="IN_REVIEW", moderator_id=owner_mod)
    reason = await _seed_reason(hard_block=False)

    cm, _ = _mock_b2b_ok()
    with cm:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                _URL.format(ticket_id=ticket.id),
                json={"blocking_reason_ids": [str(reason.id)]},
                headers=_auth(caller_mod),
            )

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "NOT_ASSIGNED"

    row = await _get_ticket(ticket.id)
    assert row.status == "IN_REVIEW"


@pytest.mark.asyncio
async def test_soft_block_invalid_field_name_returns_400():
    """
    field_reports[].severity outside allowed enum → 400 INVALID_REQUEST.
    (spec: severity ∈ {INFO, WARNING, ERROR})
    """
    mod_id = uuid4()
    ticket = await _seed_ticket(status="IN_REVIEW", moderator_id=mod_id)
    reason = await _seed_reason(hard_block=False)

    cm, _ = _mock_b2b_ok()
    with cm:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                _URL.format(ticket_id=ticket.id),
                json={
                    "blocking_reason_ids": [str(reason.id)],
                    "field_reports": [
                        {"field_path": "title", "message": "Bad", "severity": "CRITICAL"}  # invalid
                    ],
                },
                headers=_auth(mod_id),
            )

    assert resp.status_code == 400, resp.text


# ────────────────────────────────────────────────────────────────────────────
# Extra tests
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_soft_block_hard_reason_returns_400():
    """
    Attempting soft-block with a hard_block=True reason → 400 HARD_BLOCK_REASON.
    Must use hard-block flow (MOD-5) for that reason.
    """
    mod_id = uuid4()
    ticket = await _seed_ticket(status="IN_REVIEW", moderator_id=mod_id)
    hard_reason = await _seed_reason(hard_block=True)

    cm, _ = _mock_b2b_ok()
    with cm:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                _URL.format(ticket_id=ticket.id),
                json={"blocking_reason_ids": [str(hard_reason.id)]},
                headers=_auth(mod_id),
            )

    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "HARD_BLOCK_REASON"

    row = await _get_ticket(ticket.id)
    assert row.status == "IN_REVIEW"


@pytest.mark.asyncio
async def test_soft_block_not_in_review_returns_409():
    """Ticket in PENDING status (e.g. after seller edit) → 409 NOT_IN_REVIEW."""
    mod_id = uuid4()
    ticket = await _seed_ticket(status="PENDING", moderator_id=None)
    reason = await _seed_reason(hard_block=False)

    cm, _ = _mock_b2b_ok()
    with cm:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                _URL.format(ticket_id=ticket.id),
                json={"blocking_reason_ids": [str(reason.id)]},
                headers=_auth(mod_id),
            )

    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "NOT_IN_REVIEW"


@pytest.mark.asyncio
async def test_soft_block_multiple_reasons():
    """Multiple valid soft-block reasons are accepted; all IDs stored."""
    mod_id = uuid4()
    ticket = await _seed_ticket(status="IN_REVIEW", moderator_id=mod_id)
    r1 = await _seed_reason(hard_block=False)
    r2 = await _seed_reason(hard_block=False)

    cm, mock_client = _mock_b2b_ok()
    with cm:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                _URL.format(ticket_id=ticket.id),
                json={"blocking_reason_ids": [str(r1.id), str(r2.id)]},
                headers=_auth(mod_id),
            )

    assert resp.status_code == 200, resp.text
    row = await _get_ticket(ticket.id)
    assert str(r1.id) in row.blocking_reason_ids
    assert str(r2.id) in row.blocking_reason_ids
    # B2B gets primary (first) reason
    event = mock_client.post.call_args.kwargs["json"]
    assert event["blocking_reason_id"] == str(r1.id)
