"""
Repository for Telegram user registrations.

Maps Telegram chat_ids to the app so SU knows who to message.
"""
import uuid
from typing import Any, Optional

from sqlalchemy import select

from app.database import async_session
from app.orm import TelegramUserRow


class TelegramUserRepo:
    """CRUD for the telegram_users table."""

    @staticmethod
    async def upsert(
        telegram_chat_id: int,
        telegram_user_id: int,
        telegram_username: Optional[str] = None,
    ) -> str:
        """Insert or update a Telegram user registration. Returns row id."""
        async with async_session() as session:
            stmt = select(TelegramUserRow).where(
                TelegramUserRow.telegram_chat_id == telegram_chat_id
            )
            result = await session.execute(stmt)
            existing = result.scalar_one_or_none()

            if existing:
                existing.telegram_user_id = telegram_user_id
                existing.telegram_username = telegram_username
                await session.commit()
                return existing.id

            row_id = str(uuid.uuid4())
            row = TelegramUserRow(
                id=row_id,
                telegram_chat_id=telegram_chat_id,
                telegram_user_id=telegram_user_id,
                telegram_username=telegram_username,
            )
            session.add(row)
            await session.commit()
            return row_id

    @staticmethod
    async def list_all() -> list[dict[str, Any]]:
        """Return all registered Telegram users."""
        async with async_session() as session:
            result = await session.execute(select(TelegramUserRow))
            return [row.to_dict() for row in result.scalars().all()]

    @staticmethod
    async def get_by_chat_id(chat_id: int) -> Optional[dict[str, Any]]:
        """Look up a user by Telegram chat_id."""
        async with async_session() as session:
            result = await session.execute(
                select(TelegramUserRow).where(
                    TelegramUserRow.telegram_chat_id == chat_id
                )
            )
            row = result.scalar_one_or_none()
            return row.to_dict() if row else None
