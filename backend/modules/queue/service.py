"""
QueueService — MOD-2 get-next-card flow.

Canon: moderation-flows.md#get-next-card
Spec:  moderation/openapi.yaml POST /api/v1/queue/claim

Concurrency strategy (ADR in PR):
  SELECT FOR UPDATE SKIP LOCKED inside a single transaction.
  The UPDATE (status=IN_REVIEW) is committed in the same transaction.
  Two concurrent moderators cannot get the same ticket because the first
  transaction holds the row lock and the second skips it.

Timeout recovery:
  Before claiming, expired IN_REVIEW tickets (claim_expires_at < now) are
  returned to PENDING atomically. This keeps the queue fresh without requiring
  a separate cron job for the MVP.

409 guard:
  If the requesting moderator already has an active IN_REVIEW ticket,
  return 409 MODERATOR_ALREADY_IN_REVIEW (force finish current ticket first).
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.modules.moderation.models import ProductModeration
from backend.modules.queue.schemas import TicketResponse

IN_REVIEW_TTL_MINUTES: int = int(os.getenv("IN_REVIEW_TTL_MINUTES", "30"))


def _to_response(row: ProductModeration) -> TicketResponse:
    kind = "CREATE" if row.json_before is None else "EDIT"
    return TicketResponse(
        id=row.id,
        product_id=row.product_id,
        seller_id=row.seller_id,
        kind=kind,
        status=row.status,
        queue_priority=row.queue_priority,
        assigned_moderator_id=row.moderator_id,
        claimed_at=row.claimed_at,
        claim_expires_at=row.claim_expires_at,
        created_at=row.date_created,
        updated_at=row.date_updated,
    )


class QueueService:
    @staticmethod
    async def claim_next(
        db: AsyncSession,
        *,
        moderator_id: UUID,
        queue_priority: Optional[int] = None,
    ) -> TicketResponse | None:
        """
        Atomically claim the next PENDING ticket.

        Returns:
          TicketResponse — ticket is now IN_REVIEW, assigned to moderator_id.
          None           — queue is empty (caller returns 204).

        Raises:
          ValueError("MODERATOR_ALREADY_IN_REVIEW") — moderator has an active ticket.

        Algorithm (inside one transaction):
          1. Release expired IN_REVIEW tickets back to PENDING.
          2. Check if moderator already has an active IN_REVIEW ticket → 409.
          3. SELECT FOR UPDATE SKIP LOCKED (oldest PENDING by date_updated ASC).
             If queue_priority given, filter by it;
             else auto-prioritize: try 1→2→3→4, return first non-empty.
          4. UPDATE found row: status=IN_REVIEW, moderator_id, claimed_at, claim_expires_at.
          5. COMMIT.
        """
        now = datetime.now(timezone.utc)
        expires_cutoff = now

        # Step 1 — release expired IN_REVIEW tickets
        await db.execute(
            update(ProductModeration)
            .where(
                ProductModeration.status == "IN_REVIEW",
                ProductModeration.claim_expires_at < expires_cutoff,
            )
            .values(
                status="PENDING",
                moderator_id=None,
                claimed_at=None,
                claim_expires_at=None,
                date_updated=now,
            )
        )

        # Step 2 — check if moderator already has an active IN_REVIEW ticket
        existing = await db.execute(
            select(ProductModeration).where(
                ProductModeration.status == "IN_REVIEW",
                ProductModeration.moderator_id == moderator_id,
            )
        )
        if existing.scalar_one_or_none() is not None:
            raise ValueError("MODERATOR_ALREADY_IN_REVIEW")

        # Step 3 — SELECT FOR UPDATE SKIP LOCKED
        priorities = [queue_priority] if queue_priority else [1, 2, 3, 4]
        found: ProductModeration | None = None

        for priority in priorities:
            stmt = (
                select(ProductModeration)
                .where(
                    ProductModeration.status == "PENDING",
                    ProductModeration.queue_priority == priority,
                )
                .order_by(ProductModeration.date_updated.asc())
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            result = await db.execute(stmt)
            row = result.scalar_one_or_none()
            if row is not None:
                found = row
                break

        if found is None:
            await db.commit()
            return None  # empty queue

        # Step 4 — claim the ticket
        claim_time = now
        expire_time = claim_time + timedelta(minutes=IN_REVIEW_TTL_MINUTES)

        found.status = "IN_REVIEW"
        found.moderator_id = moderator_id
        found.claimed_at = claim_time
        found.claim_expires_at = expire_time
        found.date_updated = now

        await db.commit()
        return _to_response(found)
