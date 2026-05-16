from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from loguru import logger
from sqlalchemy import Column, DateTime, Index, Integer, String, Text, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from src.domain.entities import Conversation, FunnelStep, Message, Role, _utcnow
from src.domain.interfaces import IConversationRepository


# ---------------------------------------------------------------------------
# SQLAlchemy ORM model
# ---------------------------------------------------------------------------


class Base(DeclarativeBase):
    pass


class ConversationModel(Base):
    __tablename__ = "conversations"

    user_id = Column(String, primary_key=True, nullable=False)
    messages_json = Column(Text, nullable=False, default="[]")
    funnel_step = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime, nullable=False, default=_utcnow, onupdate=_utcnow
    )

    # WhatsApp-specific state fields (added in Phase 2)
    pending_action = Column(String, nullable=True)
    pending_action_expires_at = Column(DateTime, nullable=True)
    operator_mode_until = Column(DateTime, nullable=True)
    qualification_facts_json = Column(Text, nullable=False, default="{}")
    stall_counter = Column(Integer, nullable=False, default=0)
    objections_handled_json = Column(Text, nullable=False, default="[]")
    readiness_score = Column(Integer, nullable=False, default=0)

    # Composite index for the idle-reminder scan, which filters on
    # (updated_at, funnel_step) and prefixes user_id with 'wa:'. Without
    # this index every 5-minute scan does a full table scan, which is the
    # difference between 10 ms and several seconds once the conversations
    # table grows past a few thousand rows.
    __table_args__ = (
        Index(
            "ix_conversations_updated_funnel",
            "updated_at",
            "funnel_step",
        ),
    )


async def migrate_conversation_table(engine: AsyncEngine) -> None:
    """Add new columns + idle-scan index to conversations (idempotent).

    The PRAGMA inspection path runs only on SQLite; on Postgres the dialect
    raises and we fall through harmlessly. ``Base.metadata.create_all`` has
    already shaped a brand-new database — this function only retrofits
    older on-disk SQLite files that pre-date the new columns/index.
    """
    new_columns = [
        ("pending_action", "TEXT"),
        ("pending_action_expires_at", "DATETIME"),
        ("operator_mode_until", "DATETIME"),
        ("qualification_facts_json", "TEXT DEFAULT '{}'"),
        ("stall_counter", "INTEGER DEFAULT 0"),
        ("objections_handled_json", "TEXT DEFAULT '[]'"),
        ("readiness_score", "INTEGER DEFAULT 0"),
    ]
    dialect = engine.dialect.name
    async with engine.begin() as conn:
        if dialect == "sqlite":
            result = await conn.execute(text("PRAGMA table_info(conversations)"))
            rows = result.fetchall()
            existing_cols = {row[1] for row in rows}
            for col_name, col_type in new_columns:
                if col_name not in existing_cols:
                    try:
                        await conn.execute(
                            text(f"ALTER TABLE conversations ADD COLUMN {col_name} {col_type}")
                        )
                        logger.info(f"Migration: added column conversations.{col_name}")
                    except Exception as exc:
                        logger.warning(f"Migration: could not add {col_name}: {exc}")
        # Idempotent: CREATE INDEX IF NOT EXISTS is supported by both
        # SQLite and Postgres, so the scan stays fast regardless of where
        # we're running.
        try:
            await conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_conversations_updated_funnel "
                    "ON conversations (updated_at, funnel_step)"
                )
            )
        except Exception as exc:
            logger.warning(f"Migration: index ix_conversations_updated_funnel: {exc}")


# ---------------------------------------------------------------------------
# Repository implementation
# ---------------------------------------------------------------------------


class ConversationRepository(IConversationRepository):
    """SQLAlchemy + aiosqlite implementation of IConversationRepository."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_conversation(self, user_id: str) -> Optional[Conversation]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(ConversationModel).where(
                    ConversationModel.user_id == user_id
                )
            )
            model = result.scalar_one_or_none()
            if model is None:
                return None
            return self._model_to_entity(model)

    async def save_conversation(self, conversation: Conversation) -> None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(ConversationModel).where(
                    ConversationModel.user_id == conversation.user_id
                )
            )
            model = result.scalar_one_or_none()
            now = _utcnow()
            messages_json = json.dumps(
                [msg.to_dict() for msg in conversation.messages], ensure_ascii=False
            )
            qualification_facts_json = json.dumps(
                conversation.qualification_facts, ensure_ascii=False
            )
            objections_handled_json = json.dumps(
                conversation.objections_handled, ensure_ascii=False
            )

            if model is None:
                model = ConversationModel(
                    user_id=conversation.user_id,
                    messages_json=messages_json,
                    funnel_step=conversation.funnel_step.value,
                    created_at=conversation.created_at,
                    updated_at=now,
                    pending_action=conversation.pending_action,
                    pending_action_expires_at=conversation.pending_action_expires_at,
                    operator_mode_until=conversation.operator_mode_until,
                    qualification_facts_json=qualification_facts_json,
                    stall_counter=conversation.stall_counter,
                    objections_handled_json=objections_handled_json,
                    readiness_score=conversation.readiness_score,
                )
                session.add(model)
            else:
                model.messages_json = messages_json
                model.funnel_step = conversation.funnel_step.value
                model.updated_at = now
                model.pending_action = conversation.pending_action
                model.pending_action_expires_at = conversation.pending_action_expires_at
                model.operator_mode_until = conversation.operator_mode_until
                model.qualification_facts_json = qualification_facts_json
                model.stall_counter = conversation.stall_counter
                model.objections_handled_json = objections_handled_json
                model.readiness_score = conversation.readiness_score
            try:
                await session.commit()
            except Exception as exc:
                await session.rollback()
                logger.error(
                    f"Failed to save conversation {conversation.user_id}: {exc}"
                )
                raise

    async def list_all(self) -> list[Conversation]:
        """Return all stored conversations.

        Kept for compatibility; prefer ``list_idle_candidates`` for scheduled
        scans so the bot does not deserialize the entire conversation table
        every interval as the user base grows.
        """
        async with self._session_factory() as session:
            result = await session.execute(select(ConversationModel))
            models = result.scalars().all()
            return [self._model_to_entity(m) for m in models]

    async def list_idle_candidates(
        self,
        updated_after,
        updated_before,
        funnel_min: int,
        funnel_max: int,
    ) -> list[Conversation]:
        """Filtered scan for the idle-reminder service.

        Pushes the time-window and funnel-step predicates into SQL so we
        deserialize only the handful of rows that could possibly need a
        reminder, not the entire conversations table. With a few thousand
        chats this is the difference between a 10 ms query and parsing
        megabytes of JSON history every five minutes.
        """
        async with self._session_factory() as session:
            stmt = select(ConversationModel).where(
                ConversationModel.updated_at >= updated_after,
                ConversationModel.updated_at <= updated_before,
                ConversationModel.funnel_step >= funnel_min,
                ConversationModel.funnel_step <= funnel_max,
                ConversationModel.user_id.like("wa:%"),
            )
            result = await session.execute(stmt)
            models = result.scalars().all()
            return [self._model_to_entity(m) for m in models]

    async def delete_conversation(self, user_id: str) -> None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(ConversationModel).where(
                    ConversationModel.user_id == user_id
                )
            )
            model = result.scalar_one_or_none()
            if model is not None:
                await session.delete(model)
                try:
                    await session.commit()
                    logger.info(f"Deleted conversation for user {user_id}")
                except Exception as exc:
                    await session.rollback()
                    logger.error(
                        f"Failed to delete conversation {user_id}: {exc}"
                    )
                    raise

    # ------------------------------------------------------------------
    # Serialisation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _model_to_entity(model: ConversationModel) -> Conversation:
        try:
            raw_messages = json.loads(model.messages_json or "[]")
            messages = [Message.from_dict(m) for m in raw_messages]
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.warning(
                f"Could not deserialise messages for user {model.user_id}: {exc}"
            )
            messages = []

        try:
            funnel_step = FunnelStep(model.funnel_step or 0)
        except ValueError:
            funnel_step = FunnelStep.NOT_STARTED

        # Safely deserialize JSON state fields (may be missing on old rows)
        try:
            qualification_facts = json.loads(
                getattr(model, "qualification_facts_json", None) or "{}"
            )
            if not isinstance(qualification_facts, dict):
                qualification_facts = {}
        except (json.JSONDecodeError, TypeError):
            qualification_facts = {}

        try:
            objections_handled = json.loads(
                getattr(model, "objections_handled_json", None) or "[]"
            )
            if not isinstance(objections_handled, list):
                objections_handled = []
        except (json.JSONDecodeError, TypeError):
            objections_handled = []

        return Conversation(
            user_id=model.user_id,
            messages=messages,
            funnel_step=funnel_step,
            created_at=model.created_at or _utcnow(),
            updated_at=model.updated_at or _utcnow(),
            pending_action=getattr(model, "pending_action", None),
            pending_action_expires_at=getattr(model, "pending_action_expires_at", None),
            operator_mode_until=getattr(model, "operator_mode_until", None),
            qualification_facts=qualification_facts,
            stall_counter=getattr(model, "stall_counter", 0) or 0,
            objections_handled=objections_handled,
            readiness_score=getattr(model, "readiness_score", 0) or 0,
        )
