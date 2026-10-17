"""Typed conversational states for Telegram workflows."""

from aiogram.fsm.state import State, StatesGroup


class AccountAddFlow(StatesGroup):
    """Collect credentials for a new Steam account."""

    waiting_credentials = State()


class UnlockFlow(StatesGroup):
    """Collect the bot-local unlock password."""

    waiting_password = State()


class GroupFlow(StatesGroup):
    """Collect account-group names for create and rename operations."""

    waiting_name = State()
    waiting_rename = State()


class NoteFlow(StatesGroup):
    """Typed state for editing an account note."""

    waiting_note = State()


class TradeNotificationFlow(StatesGroup):
    """Typed state for configuring the trade notification interval."""

    waiting_interval = State()


class CustomPriceFlow(StatesGroup):
    """Typed state for a one-shot custom Steam listing price."""

    waiting_price = State()


class BotPasswordFlow(StatesGroup):
    """Collect a bot-local password without persisting the secret in FSM."""

    waiting_password = State()


class ProxyFlow(StatesGroup):
    """Collect an account proxy without persisting credentials in FSM."""

    waiting_proxy = State()


class AccountMaFileImportFlow(StatesGroup):
    """Collect a maFile document for an existing owned account."""

    waiting_document = State()


class BulkImportFlow(StatesGroup):
    """Collect bulk account credentials without persisting them in FSM."""

    waiting_credentials = State()


class LoginPasswordFlow(StatesGroup):
    """Collect a missing Steam password without persisting it in FSM."""

    waiting_password = State()
