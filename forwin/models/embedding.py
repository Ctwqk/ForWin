"""Immutable, content-addressed vectors independent of Canon point generations."""
from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class EmbeddingCacheEntry(Base):
    __tablename__ = "embedding_cache_entries"

    input_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    embedding_identity: Mapped[str] = mapped_column(String(64), primary_key=True)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    vector_json: Mapped[str] = mapped_column(Text, nullable=False)
