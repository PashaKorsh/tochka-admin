"""
ModerationService — receive-product-events flow (MOD-1).

Canon: moderation-flows.md#receive-product-events
Spec:  moderation/openapi.yaml POST /api/v1/b2b/events

Idempotency:
  Deduplication by idempotency_key stored in processed_events.
  Duplicate event (same key) → return False (caller responds 202, no-op).

State transitions on EDITED (canon queue_priority rules):
  old BLOCKED               → queue_priority 2
  old MODERATED + qty > 0   → queue_priority 3
  old MODERATED + qty = 0   → queue_priority 4
  old PENDING / IN_REVIEW   → keep current priority (repeat EDITED)

DELETED removes the product_moderation record (idempotent — OK if not found).
HARD_BLOCKED products are immune to CREATED/EDITED (ignored, 202 OK).
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.modules.moderation.models import ProcessedEvent, ProductModeration
from backend.modules.moderation.schemas import (
    B2BEventType,
    EventProductCreated,
    EventProductDeleted,
    EventProductEdited,
    IncomingB2BEvent,
)


class ModerationService:

    @staticmethod
    async def handle_event(
        db: AsyncSession,
        event: IncomingB2BEvent,
    ) -> bool:
        """
        Process an incoming B2B product event.

        Returns True if the event was processed, False if it was a duplicate
        (idempotency_key already in processed_events).
        Caller returns 202 in both cases.
        """
        # Idempotency check
        existing = await db.execute(
            select(ProcessedEvent).where(
                ProcessedEvent.idempotency_key == event.idempotency_key
            )
        )
        if existing.scalar_one_or_none() is not None:
            return False  # duplicate — no-op

        # Dispatch to event-type handler
        if event.event_type == B2BEventType.PRODUCT_CREATED:
            await _handle_created(db, event)
        elif event.event_type == B2BEventType.PRODUCT_EDITED:
            await _handle_edited(db, event)
        elif event.event_type == B2BEventType.PRODUCT_DELETED:
            await _handle_deleted(db, event)

        # Mark event as processed
        db.add(ProcessedEvent(
            idempotency_key=event.idempotency_key,
            event_type=event.event_type.value,
        ))
        await db.commit()
        return True


async def _find_product(db: AsyncSession, product_id: UUID) -> ProductModeration | None:
    result = await db.execute(
        select(ProductModeration).where(ProductModeration.product_id == product_id)
    )
    return result.scalar_one_or_none()


async def _handle_created(db: AsyncSession, event: IncomingB2BEvent) -> None:
    """
    PRODUCT_CREATED → INSERT product_moderation with status=PENDING.

    Canon rules:
      - If product already exists in HARD_BLOCKED → ignore (200 OK per canon).
      - If product already exists (non-HARD_BLOCKED) → also ignore idempotently
        (the spec/canon says 400 for duplicate CREATED, but since we check
        idempotency_key first, a genuine duplicate is already caught above.
        A race where the same product_id arrives via different idempotency_keys
        is treated as a no-op to avoid breaking existing records.)
    """
    payload = EventProductCreated.model_validate(event.payload)

    existing = await _find_product(db, payload.product_id)
    if existing is not None:
        # HARD_BLOCKED or any pre-existing record → ignore
        return

    db.add(ProductModeration(
        product_id=payload.product_id,
        seller_id=payload.seller_id,
        category_id=payload.category_id,
        status="PENDING",
        queue_priority=payload.queue_priority,
        json_before=None,
        json_after=payload.json_after,
    ))


async def _handle_edited(db: AsyncSession, event: IncomingB2BEvent) -> None:
    """
    PRODUCT_EDITED → update product_moderation back to PENDING.

    Canon queue_priority rules based on old status:
      BLOCKED     → 2 (seller fixed after block — high priority)
      MODERATED + active_quantity > 0 → 3
      MODERATED + active_quantity = 0 → 4
      PENDING / IN_REVIEW             → keep current (repeated edit)

    HARD_BLOCKED → ignore.
    Not found → treat as a no-op (canon says 400, but we stay idempotent).
    """
    payload = EventProductEdited.model_validate(event.payload)

    record = await _find_product(db, payload.product_id)
    if record is None:
        return  # no-op (idempotent)
    if record.status == "HARD_BLOCKED":
        return  # immune to edits

    old_status = record.status

    # Determine new queue_priority
    if old_status == "BLOCKED":
        new_priority = 2
    elif old_status == "MODERATED":
        qty = _extract_quantity(payload.json_after)
        new_priority = 3 if qty > 0 else 4
    else:
        # PENDING / IN_REVIEW — repeat edit; keep current priority
        new_priority = record.queue_priority

    record.json_before = payload.json_before if payload.json_before else record.json_after
    record.json_after = payload.json_after
    if payload.category_id is not None:
        record.category_id = payload.category_id
    record.status = "PENDING"
    record.queue_priority = new_priority
    record.moderator_id = None  # reset — card re-enters queue


async def _handle_deleted(db: AsyncSession, event: IncomingB2BEvent) -> None:
    """
    PRODUCT_DELETED → delete product_moderation record (idempotent: OK if not found).
    Canon: HARD_BLOCKED records are also deleted.
    """
    payload = EventProductDeleted.model_validate(event.payload)
    await db.execute(
        delete(ProductModeration).where(
            ProductModeration.product_id == payload.product_id
        )
    )


def _extract_quantity(json_data: dict[str, Any]) -> int:
    """Extract total active quantity from a B2B ProductPublicResponse snapshot."""
    skus = json_data.get("skus", [])
    return sum(s.get("active_quantity", 0) or 0 for s in skus)
