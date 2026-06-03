"""
Moderation service models.

product_moderation — moderation queue per product.
processed_events   — idempotency log (deduplication by idempotency_key).

State machine (canon moderation-flows.md):
  PENDING → IN_REVIEW → APPROVED
                      → BLOCKED → (EDITED) → PENDING
                      → HARD_BLOCKED (terminal; only super-admin can reset)
  APPROVED → (EDITED) → PENDING
  PENDING/IN_REVIEW → (DELETED) → record deleted
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSON, UUID as PG_UUID

from backend.database import Base


class ProductModeration(Base):
    __tablename__ = "product_moderation"

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    product_id = Column(PG_UUID(as_uuid=True), nullable=False, unique=True, index=True)
    seller_id = Column(PG_UUID(as_uuid=True), nullable=False)
    category_id = Column(PG_UUID(as_uuid=True), nullable=True)

    status = Column(
        String(32), nullable=False, default="PENDING"
    )  # PENDING | IN_REVIEW | APPROVED | BLOCKED | HARD_BLOCKED

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
    # MOD-3/4: set when moderator makes a decision (approve/block)
    decision_at = Column(DateTime(timezone=True), nullable=True)
    moderator_comment = Column(String(2000), nullable=True)
    # MOD-4/5: blocking reason UUIDs stored as JSON array (spec: blocking_reason_ids)
    blocking_reason_ids = Column(JSON, nullable=True)

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


class BlockingReason(Base):
    """
    Reference table of blocking reasons — spec moderation/openapi.yaml#BlockingReasonResponse.
    hard_block=True → HARD_BLOCKED (terminal); hard_block=False → BLOCKED (seller can fix).
    """
    __tablename__ = "blocking_reasons"

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code = Column(String(64), nullable=False, unique=True)
    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    hard_block = Column(Boolean, nullable=False, default=False)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class FieldReport(Base):
    """
    Per-field moderator remarks for a ticket decision — spec moderation/openapi.yaml#FieldReport.
    ADR: separate table chosen over JSON column for analytics (filter by field_path, severity).
    JSON column would be simpler but breaks GROUP BY field_path queries.
    """
    __tablename__ = "field_reports"

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    product_moderation_id = Column(
        PG_UUID(as_uuid=True),
        ForeignKey("product_moderation.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    field_path = Column(String(500), nullable=False)
    message = Column(String(1000), nullable=False)
    severity = Column(String(16), nullable=False, default="ERROR")  # INFO | WARNING | ERROR


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
