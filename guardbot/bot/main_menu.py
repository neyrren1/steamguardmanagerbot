"""Shared builders and delivery helpers for the main bot menu."""

from collections.abc import Sequence
from dataclasses import dataclass
from html import escape
from typing import Any

from aiogram import Bot
from aiogram.enums import ButtonStyle
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    DisabledButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputRichBlockButtons,
    InputRichBlockDetails,
    InputRichBlockDivider,
    InputRichBlockParagraph,
    InputRichMessage,
    RichMessageButton,
    RichTextBold,
    RichTextButton,
    RichTextCode,
    RichTextItalic,
)

from guardbot.config import logger


@dataclass(frozen=True)
class QuickAccountView:
    """Non-secret account state needed by a quick-action row."""

    account_id: int
    account_name: str
    is_pinned: bool
    has_guard: bool
    has_confirmations: bool


@dataclass(frozen=True)
class MainMenuView:
    """Non-secret values required to render the main menu."""

    display_name: str
    telegram_id: int | None
    account_ids: tuple[int, ...]
    accounts_with_2fa: int
    accounts_logged: int
    quick_accounts: tuple[QuickAccountView, ...]

    @property
    def accounts_count(self) -> int:
        return len(self.account_ids)


def build_main_menu_view(
    display_name: str,
    accounts: Sequence[Any],
    *,
    telegram_id: int | None = None,
) -> MainMenuView:
    """Reduce ORM account objects to the non-secret menu view model."""
    account_list = tuple(
        sorted(
            accounts,
            key=lambda account: (
                not bool(getattr(account, "is_pinned", False)),
                int(getattr(account, "sort_order", 0) or 0),
                int(account.id),
            ),
        )
    )
    return MainMenuView(
        display_name=display_name,
        telegram_id=telegram_id,
        account_ids=tuple(int(account.id) for account in account_list),
        accounts_with_2fa=sum(bool(account.shared_secret) for account in account_list),
        accounts_logged=sum(
            bool(
                account.access_token
                and account.refresh_token
                and account.password
                and account.password.strip()
            )
            for account in account_list
        ),
        quick_accounts=tuple(
            QuickAccountView(
                account_id=int(account.id),
                account_name=str(account.account_name),
                is_pinned=bool(getattr(account, "is_pinned", False)),
                has_guard=bool(account.shared_secret),
                has_confirmations=bool(
                    account.identity_secret and account.access_token
                ),
            )
            for account in account_list[:3]
        ),
    )


def _short_account_name(account: QuickAccountView, limit: int = 10) -> str:
    prefix = "📌 " if account.is_pinned else ""
    available = max(1, limit - len(prefix))
    name = account.account_name
    if len(name) > available:
        name = f"{name[: max(1, available - 1)]}…"
    return f"{prefix}{name}"


def _quick_account_rows(view: MainMenuView) -> list[InputRichBlockButtons]:
    rows = []
    for account in view.quick_accounts:
        guard_button = (
            RichMessageButton(
                text="SG-код",
                callback_data=f"action_get_code_{account.account_id}",
            )
            if account.has_guard
            else RichMessageButton(text="SG-код —", disabled=DisabledButton())
        )
        confirmations_button = (
            RichMessageButton(
                text="Подтв.",
                callback_data=f"confirmations_menu_{account.account_id}",
            )
            if account.has_confirmations
            else RichMessageButton(
                text="Подтв. —",
                disabled=DisabledButton(),
            )
        )
        rows.append(
            InputRichBlockButtons(
                buttons=[
                    RichMessageButton(
                        text=_short_account_name(account),
                        callback_data=f"account_detail_{account.account_id}",
                    ),
                    guard_button,
                    confirmations_button,
                ]
            )
        )
    return rows


def build_main_menu_rich(view: MainMenuView) -> InputRichMessage:
    """Build the production main menu with Telegram Rich Message primitives."""
    telegram_id = str(view.telegram_id) if view.telegram_id is not None else "—"
    blocks = [
        InputRichBlockParagraph(
            text=[
                "👋 Привет, ",
                RichTextItalic(text=view.display_name),
                "!\n\n",
                (
                    "🎮 Steam Guard Manager Bot - ваш надежный помощник для "
                    "управления Steam аккаунтами и Steam Guard кодами.\n\n"
                ),
                "📊 Ваша статистика:\n",
                "├─ UID: ",
                RichTextCode(text=telegram_id),
                (
                    f"\n├─ Аккаунтов: {view.accounts_count}\n"
                    f"├─ С 2FA: {view.accounts_with_2fa}\n"
                    f"└─ Авторизовано: {view.accounts_logged}"
                ),
            ]
        ),
        InputRichBlockDetails(
            summary=RichTextBold(text="Список быстрых команд"),
            is_open=False,
            blocks=[
                InputRichBlockParagraph(
                    text=[
                        RichTextCode(text="/gc [аккаунт]"),
                        " — Guard-код\n",
                        RichTextCode(text="/add"),
                        " — добавить аккаунт\n",
                        RichTextCode(text="/conf [аккаунт]"),
                        " — подтверждения\n",
                        RichTextCode(text="/trades [аккаунт]"),
                        " — трейды\n",
                        RichTextCode(text="/inv [аккаунт]"),
                        " — инвентарь\n",
                        RichTextCode(text="/login [аккаунт]"),
                        " — вход",
                    ]
                )
            ],
        ),
        InputRichBlockDivider(),
        InputRichBlockButtons(
            buttons=[
                RichMessageButton(
                    text="➕ Добавить аккаунт",
                    callback_data="add_account",
                    style=ButtonStyle.SUCCESS,
                )
            ]
        ),
        InputRichBlockButtons(
            buttons=[
                RichMessageButton(
                    text="👥 Мои аккаунты",
                    callback_data="my_accounts",
                )
            ]
        ),
    ]

    if view.quick_accounts:
        blocks.extend(_quick_account_rows(view))

    blocks.extend(
        [
            InputRichBlockButtons(
                buttons=[
                    RichMessageButton(
                        text="📦 Импорт аккаунтов",
                        callback_data="bulk_import_menu",
                    ),
                    RichMessageButton(
                        text="📁 Импорт mafile",
                        callback_data="mafile_instruction",
                    ),
                ]
            ),
        ]
    )

    if view.accounts_count > 1:
        blocks.append(
            InputRichBlockButtons(
                buttons=[
                    RichMessageButton(
                        text="📂 Группы",
                        callback_data="groups_menu",
                    )
                ]
            )
        )

    blocks.extend(
        [
            InputRichBlockButtons(
                buttons=[
                    RichMessageButton(
                        text="⚙️ Настройки",
                        callback_data="settings",
                    )
                ]
            ),
            InputRichBlockButtons(
                buttons=[
                    RichMessageButton(
                        text="🔐 Guard-коды",
                        switch_inline_query_current_chat="/gc ",
                    )
                ]
            ),
            InputRichBlockParagraph(
                text=[
                    RichTextButton(
                        button=RichMessageButton(
                            text="ℹ️ Информация",
                            callback_data="info",
                        )
                    ),
                    "   ",
                    RichTextButton(
                        button=RichMessageButton(
                            text="📚 Инструкция",
                            callback_data="instruction",
                        )
                    ),
                ]
            ),
        ]
    )
    return InputRichMessage(blocks=blocks)


def build_main_menu_legacy(
    view: MainMenuView,
) -> tuple[str, InlineKeyboardMarkup]:
    """Build the previous text menu as a safe fallback."""
    telegram_id = str(view.telegram_id) if view.telegram_id is not None else "—"
    welcome_text = (
        f"👋 Привет, <i>{escape(view.display_name)}</i>!\n\n"
        "🎮 Steam Guard Manager Bot - ваш надежный помощник для управления "
        "Steam аккаунтами и Steam Guard кодами.\n\n"
        "📊 Ваша статистика:\n"
        f"├─ UID: <code>{telegram_id}</code>\n"
        f"├─ Аккаунтов: {view.accounts_count}\n"
        f"├─ С 2FA: {view.accounts_with_2fa}\n"
        f"└─ Авторизовано: {view.accounts_logged}\n\n"
        "⚡ <b>Список быстрых команд:</b>\n"
        "<code>/gc [аккаунт]</code> — получить Steam Guard-код\n"
        "<code>/add</code> — добавить аккаунт\n"
        "<code>/conf [аккаунт]</code> — посмотреть подтверждения\n"
        "<code>/trades [аккаунт]</code> — открыть меню трейдов\n"
        "<code>/inv [аккаунт]</code> — открыть инвентарь\n"
        "<code>/login [аккаунт]</code> — войти в аккаунт"
    )

    keyboard_buttons = [
        [InlineKeyboardButton(text="➕ Добавить аккаунт", callback_data="add_account")],
        [InlineKeyboardButton(text="👥 Мои аккаунты", callback_data="my_accounts")],
    ]

    for account in view.quick_accounts:
        row = [
            InlineKeyboardButton(
                text=_short_account_name(account),
                callback_data=f"account_detail_{account.account_id}",
            )
        ]
        if account.has_guard:
            row.append(
                InlineKeyboardButton(
                    text="SG-код",
                    callback_data=f"action_get_code_{account.account_id}",
                )
            )
        if account.has_confirmations:
            row.append(
                InlineKeyboardButton(
                    text="Подтверждения",
                    callback_data=f"confirmations_menu_{account.account_id}",
                )
            )
        keyboard_buttons.append(row)

    keyboard_buttons.append(
        [
            InlineKeyboardButton(
                text="📦 Импорт аккаунтов",
                callback_data="bulk_import_menu",
            ),
            InlineKeyboardButton(
                text="📁 Импорт mafile",
                callback_data="mafile_instruction",
            ),
        ]
    )
    if view.accounts_count > 1:
        keyboard_buttons.append(
            [InlineKeyboardButton(text="📂 Группы", callback_data="groups_menu")]
        )
    keyboard_buttons.extend(
        [
            [InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings")],
            [
                InlineKeyboardButton(
                    text="🔐 Guard-коды",
                    switch_inline_query_current_chat="/gc ",
                )
            ],
            [
                InlineKeyboardButton(text="ℹ️ Информация", callback_data="info"),
                InlineKeyboardButton(text="📚 Инструкция", callback_data="instruction"),
            ],
        ]
    )
    return welcome_text, InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)


async def send_main_menu(
    bot: Bot | Any,
    chat_id: int,
    view: MainMenuView,
    message_thread_id: int | None = None,
    business_connection_id: str | None = None,
) -> Any:
    """Send the rich menu, falling back to the legacy text menu on API rejection."""
    try:
        return await bot.send_rich_message(
            chat_id=chat_id,
            message_thread_id=message_thread_id,
            business_connection_id=business_connection_id,
            rich_message=build_main_menu_rich(view),
        )
    except TelegramBadRequest as exc:
        logger.warning("Rich main menu rejected; using legacy fallback: %s", exc)
        text, keyboard = build_main_menu_legacy(view)
        return await bot.send_message(
            chat_id=chat_id,
            message_thread_id=message_thread_id,
            business_connection_id=business_connection_id,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )


async def edit_main_menu(message: Any, view: MainMenuView) -> Any:
    """Replace the current bot message with a rich menu and safe legacy fallback."""
    try:
        return await message.edit_text(
            text=None,
            rich_message=build_main_menu_rich(view),
        )
    except TelegramBadRequest as exc:
        logger.warning("Rich main menu edit rejected; using legacy fallback: %s", exc)
        text, keyboard = build_main_menu_legacy(view)
        return await message.edit_text(
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
