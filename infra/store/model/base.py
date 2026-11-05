"""Platform-owned declarative metadata and PostgreSQL column types."""

from __future__ import annotations

from datetime import UTC

from sqlalchemy import BigInteger, DateTime
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import TypeDecorator


class PlatformBase(DeclarativeBase):
    """Metadata owned exclusively by the new platform."""


class UTCDateTime(TypeDecorator):
    """Timezone-aware ``DateTime`` that defensively normalizes to UTC.

    PostgreSQL ``timestamptz`` already preserves the offset; this guards
    against any naive datetime slipping in by stamping UTC on bind/result.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


# Dynamic interface content (dict / list / nested model) → JSONB.
JSON_VARIANT = JSONB

# 64-bit autoincrement PK for config tables (BIGSERIAL on PostgreSQL).
AUTO_PK = BigInteger
