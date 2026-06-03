"""
BlockService — MOD-4 soft-block + MOD-5 hard-block.

Canon: moderation-flows.md#soft-block, #hard-block
Spec:  moderation/openapi.yaml POST /api/v1/tickets/{ticket_id}/block
B2B:   b2b/openapi.yaml POST /api/v1/moderation/events

Routing (same endpoint, determined by blocking reason's hard_block flag):
  ALL reasons have hard_block=False → BLOCKED    (soft-block, MOD-4)
  ALL reasons have hard_block=True  → HARD_BLOCKED (hard-block, MOD-5)
  MIXED                              → 400 MIXED_REASONS (must not mix)

Guards (in order):
  1. Ticket exists → 404
  2. status != HARD_BLOCKED (terminal) → 403 HARD_BLOCKED_TERMINAL
  3. status == IN_REVIEW → 409 NOT_IN_REVIEW
  4. moderator_id == caller → 403 NOT_ASSIGNED
  5. All blocking_reason_ids exist and are active → 400 UNKNOWN_REASON
  6. No mixed hard/soft reasons → 400 MIXED_REASONS

After guards:
  7. DELETE old field_reports for this ticket
  8. INSERT new field_reports
  9. UPDATE ticket: status=BLOCKED|HARD_BLOCKED, decision_at, comment, reason_ids
  10. POST BLOCKED event to B2B /api/v1/moderation/events (hard_block= True|False)
      B2B failure for soft-block → rollback to IN_REVIEW, raise 502
      B2B failure for hard-block → log and keep HARD_BLOCKED (terminal must not un-block)

ADR (field_reports storage):
  Separate table (FK product_moderation.id) chosen over JSON column.
  Enables GROUP BY field_path/severity analytics without JSONB extraction.
  Trade-off: slightly larger query for ticket detail, but JOIN is trivial at this scale.

ADR (hard_block irreversibility):
  Terminal status enforced via guard #2 on every mutating endpoint (approve, block).
  Alternative — separate archive table — was rejected: higher migration cost, harder joins.
  Alternative — is_terminal flag — was rejected: two fields to check instead of one status string.
  Guard #2 is the single chokepoint; audit is easy (grep for HARD_BLOCKED_TERMINAL raises).
  Data-fix via Django Admin / psql with explicit audit log is the only sanctioned bypass.
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


async def _load_and_validate_reasons(
    db: AsyncSession,
    blocking_reason_ids: List[UUID],
) -> List[BlockingReason]:
    """
    Load all reasons, raise on missing/inactive or mixed hard/soft.

    Raises:
      LookupError("UNKNOWN_REASON")  — any ID not found or inactive
      ValueError("MIXED_REASONS")    — mix of hard_block=True and hard_block=False
    """
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
        reasons.append(reason)

    hard_flags = {r.hard_block for r in reasons}
    if len(hard_flags) > 1:
        raise ValueError("MIXED_REASONS")

    return reasons


async def _apply_b2b_event(
    client: httpx.AsyncClient,
    *,
    product_id: UUID,
    moderator_id: UUID,
    primary_reason: BlockingReason,
    comment: Optional[str],
    hard_block: bool,
    occurred_at: datetime,
) -> None:
    resp = await client.post(
        f"{B2B_URL}/api/v1/moderation/events",
        json={
            "idempotency_key": str(uuid4()),
            "product_id": str(product_id),
            "event_type": "BLOCKED",
            "occurred_at": occurred_at.isoformat(),
            "moderator_id": str(moderator_id),
            "moderator_comment": comment,
            "blocking_reason_id": str(primary_reason.id),
            "hard_block": hard_block,
        },
        headers={"X-Service-Key": MOD_TO_B2B_KEY},
        timeout=10.0,
    )
    resp.raise_for_status()


class BlockService:

    @staticmethod
    async def block(
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
        Block a moderation ticket. Routes to soft (BLOCKED) or hard (HARD_BLOCKED)
        based on the blocking reasons' hard_block flag.

        Raises:
          LookupError("NOT_FOUND")            — ticket not found → 404
          ValueError("HARD_BLOCKED_TERMINAL") — ticket is HARD_BLOCKED (terminal) → 403
          ValueError("NOT_IN_REVIEW")         — status != IN_REVIEW → 409
          PermissionError("NOT_ASSIGNED")     — not this moderator's ticket → 403
          LookupError("UNKNOWN_REASON")       — any blocking_reason_id not found → 400
          ValueError("MIXED_REASONS")         — mix of hard and soft reasons → 400
          RuntimeError("B2B_UNAVAILABLE")     — B2B unreachable (soft-block only) → 502
        """
        # Guard 1: ticket exists
        result = await db.execute(
            select(ProductModeration).where(ProductModeration.id == ticket_id)
        )
        ticket = result.scalar_one_or_none()
        if ticket is None:
            raise LookupError("NOT_FOUND")

        # Guard 2: terminal state — nothing can modify a HARD_BLOCKED ticket
        if ticket.status == "HARD_BLOCKED":
            raise ValueError("HARD_BLOCKED_TERMINAL")

        # Guard 3: status must be IN_REVIEW
        if ticket.status != "IN_REVIEW":
            raise ValueError("NOT_IN_REVIEW")

        # Guard 4: must be assigned to caller
        if ticket.moderator_id != moderator_id:
            raise PermissionError("NOT_ASSIGNED")

        # Guard 5+6: validate reasons
        reasons = await _load_and_validate_reasons(db, blocking_reason_ids)
        is_hard = reasons[0].hard_block
        new_status = "HARD_BLOCKED" if is_hard else "BLOCKED"

        now = datetime.now(timezone.utc)

        # Apply field_reports (delete old, insert new)
        await db.execute(
            delete(FieldReport).where(FieldReport.product_moderation_id == ticket_id)
        )
        for fr in (field_reports or []):
            db.add(FieldReport(
                product_moderation_id=ticket_id,
                field_path=fr["field_path"],
                message=fr["message"],
                severity=fr.get("severity", "ERROR"),
            ))

        # Update ticket
        ticket.status = new_status
        ticket.decision_at = now
        ticket.moderator_comment = comment
        ticket.blocking_reason_ids = [str(rid) for rid in blocking_reason_ids]
        ticket.date_updated = now
        await db.commit()
        await db.refresh(ticket)

        # Send BLOCKED event to B2B
        primary_reason = reasons[0]
        own_client = b2b_client is None
        client = b2b_client or httpx.AsyncClient()
        try:
            try:
                await _apply_b2b_event(
                    client,
                    product_id=ticket.product_id,
                    moderator_id=moderator_id,
                    primary_reason=primary_reason,
                    comment=comment,
                    hard_block=is_hard,
                    occurred_at=now,
                )
            finally:
                if own_client:
                    await client.aclose()

        except Exception as exc:
            if is_hard:
                # Hard-block is terminal — do NOT roll back. B2B can retry or admin fixes.
                # The product is already HARD_BLOCKED in our DB which is the source of truth.
                pass
            else:
                # Soft-block: roll back so moderator can retry
                ticket.status = "IN_REVIEW"
                ticket.decision_at = None
                ticket.moderator_comment = None
                ticket.blocking_reason_ids = None
                ticket.date_updated = now
                await db.commit()
                raise RuntimeError("B2B_UNAVAILABLE") from exc

        return _to_response(ticket)
