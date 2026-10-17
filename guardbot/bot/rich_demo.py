"""Experimental Telegram Rich Message used to validate the new UI primitives."""

from typing import Any

from aiogram import Bot
from aiogram.enums import ButtonStyle
from aiogram.types import (
    DisabledButton,
    InputRichBlockButtons,
    InputRichBlockDivider,
    InputRichBlockFooter,
    InputRichBlockParagraph,
    InputRichBlockSectionHeading,
    InputRichMessage,
    RichMessageButton,
    RichTextBold,
    RichTextButton,
)

RICH_DEMO_CALLBACK_PREFIX = "rich_demo:"

_FEEDBACK: dict[str, tuple[str, bool]] = {
    "inline": ("Компактная кнопка внутри текста работает.", False),
    "primary": ("Синяя primary-кнопка работает.", False),
    "success": ("Зелёная callback-кнопка работает.", False),
    "danger": ("Красная кнопка работает. Это только демонстрация.", True),
}


def build_rich_demo_message() -> InputRichMessage:
    """Build a self-contained showcase of Telegram Rich Message controls."""
    return InputRichMessage(
        blocks=[
            InputRichBlockSectionHeading(
                text=["🛡️ ", RichTextBold(text="Steam Guard Manager")],
                size=3,
            ),
            InputRichBlockParagraph(
                text=(
                    "Экспериментальный интерфейс на Telegram Rich Messages. "
                    "Эта команда не обращается к Steam и не изменяет данные."
                )
            ),
            InputRichBlockDivider(),
            InputRichBlockParagraph(
                text=[
                    "Встроенная кнопка: ",
                    RichTextButton(
                        button=RichMessageButton(
                            text="Проверить",
                            callback_data=f"{RICH_DEMO_CALLBACK_PREFIX}inline",
                            style=ButtonStyle.SUCCESS,
                        )
                    ),
                    "  ",
                    RichTextButton(
                        button=RichMessageButton(
                            text="Premium-владелец",
                            style=ButtonStyle.PRIMARY,
                            disabled=DisabledButton(),
                        )
                    ),
                ]
            ),
            InputRichBlockParagraph(
                text=[
                    RichTextBold(text="Цветные действия"),
                    " адаптируются к светлой и тёмной теме Telegram.",
                ]
            ),
            InputRichBlockButtons(
                buttons=[
                    RichMessageButton(
                        text="Основное действие",
                        callback_data=f"{RICH_DEMO_CALLBACK_PREFIX}primary",
                        style=ButtonStyle.PRIMARY,
                    ),
                    RichMessageButton(
                        text="Подтвердить",
                        callback_data=f"{RICH_DEMO_CALLBACK_PREFIX}success",
                        style=ButtonStyle.SUCCESS,
                    ),
                ],
                align="center",
            ),
            InputRichBlockButtons(
                buttons=[
                    RichMessageButton(
                        text="Опасное действие",
                        callback_data=f"{RICH_DEMO_CALLBACK_PREFIX}danger",
                        style=ButtonStyle.DANGER,
                    ),
                    RichMessageButton(
                        text="Документация",
                        url="https://core.telegram.org/bots/api#richmessagebutton",
                    ),
                ],
                align="center",
            ),
            InputRichBlockFooter(
                text="/rich_demo · тестовый экран без операций со Steam"
            ),
        ]
    )


def get_rich_demo_feedback(action: str) -> tuple[str, bool] | None:
    """Return callback text and whether Telegram should show an alert."""
    return _FEEDBACK.get(action)


async def send_rich_demo(
    bot: Bot | Any,
    chat_id: int,
    message_thread_id: int | None = None,
    business_connection_id: str | None = None,
) -> Any:
    """Send the demo through Telegram's dedicated Rich Message method."""
    return await bot.send_rich_message(
        chat_id=chat_id,
        message_thread_id=message_thread_id,
        business_connection_id=business_connection_id,
        rich_message=build_rich_demo_message(),
    )
