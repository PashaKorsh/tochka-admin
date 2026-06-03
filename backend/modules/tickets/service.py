"""
TicketService — MOD-3 approve-product flow.

Canon: moderation-flows.md#approve-product
Spec:  moderation/openapi.yaml POST /api/v1/tickets/{ticket_id}/approve
B2B:   b2b/openapi.yaml POST /api/v1/moderation/events

Guards (in order):
  1. Ticket exists → 404
  2. status == IN_REVIEW → 409 (catches approve_after_edited: EDITED resets to PENDING)
  3. moderator_id == caller  → 403 (IDOR guard)
  4. json_after has at least one SKU → 409 (approve_without_sku)

After guards:
  5. UPDATE status=APPROVED, decision_at, moderator_comment
  6. POST event MODERATED to B2B /api/v1/moderation/events
     - B2B failure → rollback to IN_REVIEW, raise 502

ADR (event delivery):
  Synchronous POST in the same request handler was chosen over outbox-pattern or
  event-bus for MVP. Rationale:
  - Zero extra infra (no MQ/outbox table/cron).
  - Moderator gets immediate feedback if B2B is down (no silent loss).
  - If B2B is down we roll back to IN_REVIEW — moderator retries.
  Trade-off: if Moderation DB commits but B2B call fails after commit, we retry
  manually. For MVP the simplicity wins; outbox-pattern is the next step.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import B2B_URL, MOD_TO_B2B_KEY
from backend.modules.moderation.models import ProductModeration
from backend.modules.queue.schemas import TicketResponse
from backend.modules.queue.service import _to_response


def _has_skus(json_after: dict) -> bool:
    """Return True if json_after snapshot contains at least one SKU entry."""
    return bool(json_after.get("skus"))


class TicketService:

    @staticmethod
    async def approve(
        db: AsyncSession,
        *,
        ticket_id: UUID,
        moderator_id: UUID,
        comment: Optional[str] = None,
        b2b_client: Optional[httpx.AsyncClient] = None,
    ) -> TicketResponse:
        """
        Approve a moderation ticket: IN_REVIEW → APPROVED, emit MODERATED event to B2B.

        Raises:
          LookupError("NOT_FOUND")              — ticket not found → 404
          ValueError("NOT_IN_REVIEW")           — status != IN_REVIEW → 409
          PermissionError("NOT_ASSIGNED")       — not this moderator's ticket → 403
          ValueError("NO_SKU")                  — json_after has no SKUs → 409
          RuntimeError("B2B_UNAVAILABLE")       — B2B rejected/unreachable → 502
        """
        result = await db.execute(
            select(ProductModeration).where(ProductModeration.id == ticket_id)
        )
        ticket = result.scalar_one_or_none()

        if ticket is None:
            raise LookupError("NOT_FOUND")

        if ticket.status == "HARD_BLOCKED":
            raise ValueError("HARD_BLOCKED_TERMINAL")

        if ticket.status != "IN_REVIEW":
            raise ValueError("NOT_IN_REVIEW")

        if ticket.moderator_id != moderator_id:
            raise PermissionError("NOT_ASSIGNED")

        if not _has_skus(ticket.json_after or {}):
            raise ValueError("NO_SKU")

        now = datetime.now(timezone.utc)

        # Commit approve decision
        ticket.status = "APPROVED"
        ticket.decision_at = now
        ticket.moderator_comment = comment
        ticket.date_updated = now
        await db.commit()
        await db.refresh(ticket)

        # idempotency_key = ticket_id (stable UUID) — identical on retry so B2B deduplicates.
        # uuid4() would differ on each retry and risk double-publication in the catalog.
        event_body = {
            "idempotency_key": str(ticket_id),
            "product_id": str(ticket.product_id),
            "event_type": "MODERATED",
            "occurred_at": now.isoformat(),
            "moderator_id": str(moderator_id),
            "moderator_comment": comment,
            "hard_block": False,
        }

        try:
            client = b2b_client or httpx.AsyncClient()
            should_close = b2b_client is None
            try:
                resp = await client.post(
                    f"{B2B_URL}/api/v1/moderation/events",
                    json=event_body,
                    headers={"X-Service-Key": MOD_TO_B2B_KEY},
                    timeout=10.0,
                )
                resp.raise_for_status()
            finally:
                if should_close:
                    await client.aclose()

        except Exception as exc:
            # Roll back to IN_REVIEW so moderator can retry
            ticket.status = "IN_REVIEW"
            ticket.decision_at = None
            ticket.moderator_comment = None
            ticket.date_updated = now
            await db.commit()
            raise RuntimeError("B2B_UNAVAILABLE") from exc

        return _to_response(ticket)
