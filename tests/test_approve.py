"""
Tests for MOD-3: approve-product.

Spec: moderation/openapi.yaml POST /api/v1/tickets/{ticket_id}/approve
Canon: moderation-flows.md#approve-product

DoD test names (exact):
  approve_transitions_to_moderated_and_emits_event — happy path
  approve_others_card_returns_403                  — IDOR guard
  approve_after_edited_returns_409                 — status guard (PENDING after edit)
  approve_without_sku_returns_409                  — SKU guard
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4, UUID

import pytest
from httpx import ASGITransport, AsyncClient, Response
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

_URL = "/api/v1/tickets/{ticket_id}/approve"


def _auth(moderator_id: UUID) -> dict:
    return {"Authorization": f"Bearer {create_moderator_token(moderator_id)}"}


async def _seed(
    *,
    status: str,
    moderator_id=None,
    skus: list | None = None,
) -> ProductModeration:
    """Seed a product_moderation row and return it."""
    if skus is None:
        skus = [{"active_quantity": 5}]

    engine = create_async_engine(_TEST_DB_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        row = ProductModeration(
            product_id=uuid4(),
            seller_id=uuid4(),
            status=status,
            queue_priority=3,
            json_before=None,
            json_after={"title": "Test", "skus": skus},
            moderator_id=moderator_id,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
    await engine.dispose()
    return row


async def _get_record(ticket_id: UUID) -> ProductModeration | None:
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
    """Context manager: patch httpx.AsyncClient so B2B returns 204."""
    mock_client = AsyncMock()
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.aclose = AsyncMock()
    return patch("backend.modules.tickets.service.httpx.AsyncClient", return_value=mock_client), mock_client


def _mock_b2b_fail():
    """Context manager: patch httpx.AsyncClient so B2B raises a connection error."""
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=Exception("B2B down"))
    mock_client.aclose = AsyncMock()
    return patch("backend.modules.tickets.service.httpx.AsyncClient", return_value=mock_client)


# ────────────────────────────────────────────────────────────────────────────
# DoD tests
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_approve_transitions_to_moderated_and_emits_event():
    """
    Happy path: IN_REVIEW ticket with SKUs → status=APPROVED, B2B gets MODERATED event.

    Verifies:
    - 200 OK with TicketResponse body (status=APPROVED)
    - DB record updated (status=APPROVED, decision_at not null)
    - httpx.AsyncClient.post called once with correct event_type=MODERATED
    """
    mod_id = uuid4()
    ticket = await _seed(status="IN_REVIEW", moderator_id=mod_id)

    cm, mock_client = _mock_b2b_ok()
    with cm:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                _URL.format(ticket_id=ticket.id),
                json={"comment": "Looks good"},
                headers=_auth(mod_id),
            )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "APPROVED"
    assert body["id"] == str(ticket.id)

    # DB state
    row = await _get_record(ticket.id)
    assert row.status == "APPROVED"
    assert row.decision_at is not None
    assert row.moderator_comment == "Looks good"

    # B2B event emitted
    mock_client.post.assert_awaited_once()
    call_kwargs = mock_client.post.call_args
    body_sent = call_kwargs[1]["json"] if "json" in call_kwargs[1] else call_kwargs.kwargs["json"]
    assert body_sent["event_type"] == "MODERATED"
    assert body_sent["product_id"] == str(ticket.product_id)


@pytest.mark.asyncio
async def test_approve_others_card_returns_403():
    """
    Moderator cannot approve a ticket assigned to a different moderator → 403.

    Canon: moderator_id == current moderator is required.
    """
    owner_mod = uuid4()
    caller_mod = uuid4()
    ticket = await _seed(status="IN_REVIEW", moderator_id=owner_mod)

    cm, _ = _mock_b2b_ok()
    with cm:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                _URL.format(ticket_id=ticket.id),
                headers=_auth(caller_mod),
            )

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "NOT_ASSIGNED"

    # DB unchanged
    row = await _get_record(ticket.id)
    assert row.status == "IN_REVIEW"


@pytest.mark.asyncio
async def test_approve_after_edited_returns_409():
    """
    If the seller edited the product while it was IN_REVIEW, the MOD-1 handler
    transitions status back to PENDING and clears moderator_id.
    The moderator's approve attempt on the same ticket → 409 NOT_IN_REVIEW.
    """
    mod_id = uuid4()
    # Simulate the result of PRODUCT_EDITED arriving: status=PENDING, moderator_id cleared
    ticket = await _seed(status="PENDING", moderator_id=None)

    cm, _ = _mock_b2b_ok()
    with cm:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                _URL.format(ticket_id=ticket.id),
                headers=_auth(mod_id),
            )

    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "NOT_IN_REVIEW"


@pytest.mark.asyncio
async def test_approve_without_sku_returns_409():
    """
    Product with no SKUs in json_after snapshot cannot be approved → 409 NO_SKU.

    Canon: "Если у товара 0 SKU → 409 Conflict".
    """
    mod_id = uuid4()
    ticket = await _seed(status="IN_REVIEW", moderator_id=mod_id, skus=[])

    cm, _ = _mock_b2b_ok()
    with cm:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                _URL.format(ticket_id=ticket.id),
                headers=_auth(mod_id),
            )

    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "NO_SKU"

    # DB unchanged
    row = await _get_record(ticket.id)
    assert row.status == "IN_REVIEW"


# ────────────────────────────────────────────────────────────────────────────
# Extra tests
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_approve_nonexistent_ticket_returns_404():
    """Ticket not found → 404 NOT_FOUND."""
    mod_id = uuid4()
    fake_id = uuid4()

    cm, _ = _mock_b2b_ok()
    with cm:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                _URL.format(ticket_id=fake_id),
                headers=_auth(mod_id),
            )

    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_approve_b2b_failure_rolls_back_to_in_review():
    """
    If B2B rejects the MODERATED event, status must roll back to IN_REVIEW.
    Moderator gets 502 and can retry.
    """
    mod_id = uuid4()
    ticket = await _seed(status="IN_REVIEW", moderator_id=mod_id)

    fail_cm = _mock_b2b_fail()
    with fail_cm:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                _URL.format(ticket_id=ticket.id),
                headers=_auth(mod_id),
            )

    assert resp.status_code == 502, resp.text
    assert resp.json()["code"] == "B2B_UNAVAILABLE"

    # DB rolled back
    row = await _get_record(ticket.id)
    assert row.status == "IN_REVIEW"


@pytest.mark.asyncio
async def test_approve_without_auth_returns_401():
    """No Authorization header → 401 UNAUTHORIZED."""
    ticket = await _seed(status="IN_REVIEW", moderator_id=uuid4())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(_URL.format(ticket_id=ticket.id))

    assert resp.status_code == 401
