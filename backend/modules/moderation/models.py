"""
Moderation service models.

product_moderation — moderation queue per product.
processed_events   — idempotency log (deduplication by idempotency_key).

State machine (canon moderation-flows.md):
  PENDING → IN_REVIEW → MODERATED
                      → BLOCKED → (EDITED) → PENDING
                      → HARD_BLOCKED (terminal; only super-admin can reset)
  MODERATED → (EDITED) → PENDING
  PENDING/IN_REVIEW → (DELETED) → record deleted
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSON, UUID as PG_UUID

from backend.database import Base


class ProductModeration(Base):
    __tablename__ = "product_moderation"

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    product_id = Column(PG_UUID(as_uuid=True), nullable=False, unique=True, index=True)
    seller_id = Column(PG_UUID(as_uuid=True), nullable=False)

    status = Column(
        String(32), nullable=False, default="PENDING"
    )  # PENDING | IN_REVIEW | MODERATED | BLOCKED | HARD_BLOCKED

    queue_priority = Column(Integer, nullable=False, default=3)

    # ADR: json_before + json_after snapshot chosen over delta or full-only.
    # Two snapshots allow exact incident reconstruction without replay.
    # Delta is cheaper in storage but harder to read; full-only loses history.
    json_before = Column(JSON, nullable=True)   # null on CREATED
    json_after = Column(JSON, nullable=False)

    moderator_id = Column(PG_UUID(as_uuid=True), nullable=True)
    # MOD-2: set when moderator claims the ticket (SELECT FOR UPDATE SKIP LOCKED)
    claimed_at = Column(DateTime(timezone=True), nullable=True)
    # = claimed_at + IN_REVIEW_TTL_MINUTES; auto-return to PENDING when expired
    claim_expires_at = Column(DateTime(timezone=True), nullable=True)

    date_created = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    date_updated = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class ProcessedEvent(Base):
    """Idempotency log — deduplicate B2B events by idempotency_key."""
    __tablename__ = "processed_events"

    idempotency_key = Column(PG_UUID(as_uuid=True), primary_key=True)
    event_type = Column(String(32), nullable=False)
    processed_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
