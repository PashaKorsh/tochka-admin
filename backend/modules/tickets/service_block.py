"""
TicketService.soft_block — MOD-4 soft-block flow.

Canon: moderation-flows.md#soft-block
Spec:  moderation/openapi.yaml POST /api/v1/tickets/{ticket_id}/block
B2B:   b2b/openapi.yaml POST /api/v1/moderation/events

Guards (in order):
  1. Ticket exists → 404
  2. status == IN_REVIEW → 409 NOT_IN_REVIEW
  3. moderator_id == caller → 403 NOT_ASSIGNED
  4. All blocking_reason_ids exist → 400 UNKNOWN_REASON
  5. No reason has hard_block=True (soft-block endpoint) → 400 HARD_BLOCK_REASON

After guards:
  6. DELETE old field_reports for this ticket
  7. INSERT new field_reports
  8. UPDATE ticket: status=BLOCKED, decision_at, moderator_comment, blocking_reason_ids
  9. POST BLOCKED event to B2B /api/v1/moderation/events (primary reason = first ID)
     B2B failure → rollback to IN_REVIEW, raise 502

ADR (field_reports storage):
  Separate table with FK chosen over JSON column in product_moderation.
  Criteria: analytics (COUNT/GROUP BY field_path/severity without JSON extraction),
  and clear schema evolution (add columns without migrating JSONB data).
  Trade-off: slightly larger payload to reconstruct the full picture, but
  JOIN is cheap for the ticket detail view.

ADR (hard_block routing for soft-block endpoint):
  If a reason with hard_block=True is passed to /block → 400 HARD_BLOCK_REASON.
  Rationale: explicit rejection prevents accidental permanent blocks. Moderator
  must consciously use a separate hard-block flow (MOD-5). Routing silently
  to hard-block would violate least-surprise principle.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional
from uuid import UUID, uuid4

import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import B2B_URL, MOD_TO_B2B_KEY
from backend.modules.moderation.models import BlockingReason, FieldReport, ProductModeration
from backend.modules.queue.schemas import TicketResponse
from backend.modules.queue.service import _to_response


class SoftBlockService:

    @staticmethod
    async def soft_block(
        db: AsyncSession,
        *,
        ticket_id: UUID,
        moderator_id: UUID,
        blocking_reason_ids: List[UUID],
        comment: Optional[str] = None,
        field_reports: Optional[List[dict]] = None,
        b2b_client: Optional[httpx.AsyncClient] = None,
    ) -> TicketResponse:
        """
        Soft-block a moderation ticket: IN_REVIEW → BLOCKED.

        Raises:
          LookupError("NOT_FOUND")           — ticket not found → 404
          ValueError("NOT_IN_REVIEW")        — status != IN_REVIEW → 409
          PermissionError("NOT_ASSIGNED")    — not this moderator's ticket → 403
          LookupError("UNKNOWN_REASON")      — any blocking_reason_id not found → 400
          ValueError("HARD_BLOCK_REASON")    — reason has hard_block=True → 400
          RuntimeError("B2B_UNAVAILABLE")    — B2B rejected/unreachable → 502
        """
        # Guard 1: ticket exists
        result = await db.execute(
            select(ProductModeration).where(ProductModeration.id == ticket_id)
        )
        ticket = result.scalar_one_or_none()
        if ticket is None:
            raise LookupError("NOT_FOUND")

        # Guard 2: status must be IN_REVIEW
        if ticket.status != "IN_REVIEW":
            raise ValueError("NOT_IN_REVIEW")

        # Guard 3: must be assigned to caller
        if ticket.moderator_id != moderator_id:
            raise PermissionError("NOT_ASSIGNED")

        # Guard 4+5: validate all blocking reasons exist and are soft-block
        reasons = []
        for rid in blocking_reason_ids:
            res = await db.execute(
                select(BlockingReason).where(
                    BlockingReason.id == rid,
                    BlockingReason.is_active == True,  # noqa: E712
                )
            )
            reason = res.scalar_one_or_none()
            if reason is None:
                raise LookupError("UNKNOWN_REASON")
            if reason.hard_block:
                raise ValueError("HARD_BLOCK_REASON")
            reasons.append(reason)

        now = datetime.now(timezone.utc)

        # Delete old field_reports (re-decision replaces them)
        await db.execute(
            delete(FieldReport).where(FieldReport.product_moderation_id == ticket_id)
        )

        # Insert new field_reports
        for fr in (field_reports or []):
            db.add(FieldReport(
                product_moderation_id=ticket_id,
                field_path=fr["field_path"],
                message=fr["message"],
                severity=fr.get("severity", "ERROR"),
            ))

        # Update ticket
        ticket.status = "BLOCKED"
        ticket.decision_at = now
        ticket.moderator_comment = comment
        ticket.blocking_reason_ids = [str(rid) for rid in blocking_reason_ids]
        ticket.date_updated = now
        await db.commit()
        await db.refresh(ticket)

        # Send BLOCKED event to B2B (primary reason = first)
        primary_reason = reasons[0]
        event_body = {
            "idempotency_key": str(uuid4()),
            "product_id": str(ticket.product_id),
            "event_type": "BLOCKED",
            "occurred_at": now.isoformat(),
            "moderator_id": str(moderator_id),
            "moderator_comment": comment,
            "blocking_reason_id": str(primary_reason.id),
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
            ticket.blocking_reason_ids = None
            ticket.date_updated = now
            await db.commit()
            raise RuntimeError("B2B_UNAVAILABLE") from exc

        return _to_response(ticket)
