"""
Tests for MOD-5: hard-block.

Spec: moderation/openapi.yaml POST /api/v1/tickets/{ticket_id}/block
Canon: moderation-flows.md#hard-block

Same endpoint as soft-block (/block), routing determined by reason.hard_block=True.

DoD test names (exact):
  hard_block_transitions_to_terminal_and_emits_event  — happy path
  hard_block_event_carries_hard_block_true             — event flag verification
  any_modify_on_hard_blocked_returns_403               — terminal state guard
  edited_event_on_hard_blocked_is_ignored              — B2B EDITED ignored
  deleted_event_removes_hard_blocked                   — B2B DELETED removes record
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
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

_BLOCK_URL = "/api/v1/tickets/{ticket_id}/block"
_APPROVE_URL = "/api/v1/tickets/{ticket_id}/approve"
_EVENTS_URL = "/api/v1/b2b/events"
_SERVICE_KEY = os.getenv("B2B_TO_MOD_KEY", "dev-service-key")


def _auth(moderator_id: UUID) -> dict:
    return {"Authorization": f"Bearer {create_moderator_token(moderator_id)}"}


async def _seed_ticket(*, status: str, moderator_id=None) -> ProductModeration:
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


async def _seed_reason(*, hard_block: bool) -> BlockingReason:
    engine = create_async_engine(_TEST_DB_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        row = BlockingReason(
            code=f"REASON_{uuid4().hex[:8].upper()}",
            title="Hard reason" if hard_block else "Soft reason",
            hard_block=hard_block,
            is_active=True,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
    await engine.dispose()
    return row


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


def _b2b_event(event_type: str, product_id: UUID) -> dict:
    return {
        "event_type": event_type,
        "idempotency_key": str(uuid4()),
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "payload": {"product_id": str(product_id), "seller_id": str(uuid4())},
    }


# ────────────────────────────────────────────────────────────────────────────
# DoD tests
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_hard_block_transitions_to_terminal_and_emits_event():
    """
    Happy path: IN_REVIEW + hard_block reason → HARD_BLOCKED, B2B event emitted.

    Verifies:
    - 200 OK with status=HARD_BLOCKED
    - DB record status=HARD_BLOCKED, decision_at set
    - B2B event called once
    """
    mod_id = uuid4()
    ticket = await _seed_ticket(status="IN_REVIEW", moderator_id=mod_id)
    reason = await _seed_reason(hard_block=True)

    cm, mock_client = _mock_b2b_ok()
    with cm:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                _BLOCK_URL.format(ticket_id=ticket.id),
                json={"blocking_reason_ids": [str(reason.id)], "comment": "Counterfeit"},
                headers=_auth(mod_id),
            )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "HARD_BLOCKED"

    row = await _get_ticket(ticket.id)
    assert row.status == "HARD_BLOCKED"
    assert row.decision_at is not None
    assert row.moderator_comment == "Counterfeit"

    mock_client.post.assert_awaited_once()


@pytest.mark.asyncio
async def test_hard_block_event_carries_hard_block_true():
    """
    The B2B event must have event_type=BLOCKED and hard_block=True.
    Verifies the flag distinguishes hard-block from soft-block in B2B's handling.
    """
    mod_id = uuid4()
    ticket = await _seed_ticket(status="IN_REVIEW", moderator_id=mod_id)
    reason = await _seed_reason(hard_block=True)

    cm, mock_client = _mock_b2b_ok()
    with cm:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.post(
                _BLOCK_URL.format(ticket_id=ticket.id),
                json={"blocking_reason_ids": [str(reason.id)]},
                headers=_auth(mod_id),
            )

    event = mock_client.post.call_args.kwargs["json"]
    assert event["event_type"] == "BLOCKED"
    assert event["hard_block"] is True
    assert event["blocking_reason_id"] == str(reason.id)


@pytest.mark.asyncio
async def test_any_modify_on_hard_blocked_returns_403():
    """
    Any attempt to approve or block a HARD_BLOCKED ticket must return 403.

    Verifies:
    - POST /approve on HARD_BLOCKED → 403 HARD_BLOCKED_TERMINAL
    - POST /block on HARD_BLOCKED → 403 HARD_BLOCKED_TERMINAL
    """
    mod_id = uuid4()
    ticket = await _seed_ticket(status="HARD_BLOCKED", moderator_id=mod_id)
    reason = await _seed_reason(hard_block=True)

    cm, _ = _mock_b2b_ok()
    with cm:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r_approve = await client.post(
                _APPROVE_URL.format(ticket_id=ticket.id),
                headers=_auth(mod_id),
            )
            r_block = await client.post(
                _BLOCK_URL.format(ticket_id=ticket.id),
                json={"blocking_reason_ids": [str(reason.id)]},
                headers=_auth(mod_id),
            )

    assert r_approve.status_code == 403, r_approve.text
    assert r_approve.json()["code"] == "HARD_BLOCKED_TERMINAL"

    assert r_block.status_code == 403, r_block.text
    assert r_block.json()["code"] == "HARD_BLOCKED_TERMINAL"

    # DB unchanged
    row = await _get_ticket(ticket.id)
    assert row.status == "HARD_BLOCKED"


@pytest.mark.asyncio
async def test_edited_event_on_hard_blocked_is_ignored():
    """
    PRODUCT_EDITED event from B2B on a HARD_BLOCKED product → 202 OK, status unchanged.

    Canon: HARD_BLOCKED is immune to EDITED events (terminal state).
    The ModerationService._handle_edited ignores HARD_BLOCKED records.
    """
    # Seed a HARD_BLOCKED ticket
    ticket = await _seed_ticket(status="HARD_BLOCKED", moderator_id=None)
    product_id = ticket.product_id

    edited_payload = {
        "product_id": str(product_id),
        "seller_id": str(uuid4()),
        "json_before": {"title": "Old"},
        "json_after": {"title": "New", "skus": [{"active_quantity": 10}]},
    }
    body = {
        "event_type": "PRODUCT_EDITED",
        "idempotency_key": str(uuid4()),
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "payload": edited_payload,
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(_EVENTS_URL, json=body, headers={"X-Service-Key": _SERVICE_KEY})

    assert resp.status_code == 202, resp.text

    # Status must remain HARD_BLOCKED
    row = await _get_ticket(ticket.id)
    assert row.status == "HARD_BLOCKED"
    assert row.json_after["title"] == "Test"  # unchanged


@pytest.mark.asyncio
async def test_deleted_event_removes_hard_blocked():
    """
    PRODUCT_DELETED event removes the HARD_BLOCKED record.

    Canon: DELETED from any status → delete the product_moderation row.
    The product itself in B2B stays hard-blocked; Moderation only tracks the ticket.
    """
    ticket = await _seed_ticket(status="HARD_BLOCKED", moderator_id=None)
    product_id = ticket.product_id

    body = {
        "event_type": "PRODUCT_DELETED",
        "idempotency_key": str(uuid4()),
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "payload": {"product_id": str(product_id)},
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(_EVENTS_URL, json=body, headers={"X-Service-Key": _SERVICE_KEY})

    assert resp.status_code == 202, resp.text

    row = await _get_ticket(ticket.id)
    assert row is None, "HARD_BLOCKED record must be deleted on PRODUCT_DELETED"


# ────────────────────────────────────────────────────────────────────────────
# Extra tests
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mixed_hard_soft_reasons_returns_400():
    """Mixing hard and soft reasons in one request → 400 MIXED_REASONS."""
    mod_id = uuid4()
    ticket = await _seed_ticket(status="IN_REVIEW", moderator_id=mod_id)
    hard = await _seed_reason(hard_block=True)
    soft = await _seed_reason(hard_block=False)

    cm, _ = _mock_b2b_ok()
    with cm:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                _BLOCK_URL.format(ticket_id=ticket.id),
                json={"blocking_reason_ids": [str(hard.id), str(soft.id)]},
                headers=_auth(mod_id),
            )

    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "MIXED_REASONS"

    row = await _get_ticket(ticket.id)
    assert row.status == "IN_REVIEW"


@pytest.mark.asyncio
async def test_hard_block_b2b_failure_keeps_hard_blocked():
    """
    If B2B call fails after hard-block DB commit, status stays HARD_BLOCKED.
    Terminal state must not roll back — B2B can retry the event.
    """
    mod_id = uuid4()
    ticket = await _seed_ticket(status="IN_REVIEW", moderator_id=mod_id)
    reason = await _seed_reason(hard_block=True)

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=Exception("B2B down"))
    mock_client.aclose = AsyncMock()

    with patch("backend.modules.tickets.service_block.httpx.AsyncClient", return_value=mock_client):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                _BLOCK_URL.format(ticket_id=ticket.id),
                json={"blocking_reason_ids": [str(reason.id)]},
                headers=_auth(mod_id),
            )

    # Hard-block is terminal — even if B2B fails, we return 200 (status committed)
    assert resp.status_code == 200, resp.text
    row = await _get_ticket(ticket.id)
    assert row.status == "HARD_BLOCKED"
