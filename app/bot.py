from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from .config import load_settings
from .database import MediaRepository, validate_group_id

router = Router()

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Створити групу"), KeyboardButton(text="Додати медіа")],
        [KeyboardButton(text="Змінити коментар"), KeyboardButton(text="Знайти групу")],
        [KeyboardButton(text="Скасувати")],
    ],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="Оберіть дію",
)

CANCEL_CONFIRMATION_KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="Так, скасувати", callback_data="cancel_confirm"),
            InlineKeyboardButton(text="Ні", callback_data="cancel_reject"),
        ]
    ]
)


class CommentStates(StatesGroup):
    waiting_comment = State()


class EditCommentStates(StatesGroup):
    waiting_comment = State()


class MediaStates(StatesGroup):
    waiting_media = State()


class InputStates(StatesGroup):
    waiting_add_group = State()
    waiting_comment_group = State()
    waiting_get_group = State()


def _argument(command: CommandObject) -> str | None:
    return command.args.strip() if command.args else None


async def _send_group(bot: Bot, message: Message, repository: MediaRepository, group_id: str) -> None:
    group = repository.get_group(group_id)
    if group is None:
        await message.answer("Групу з таким ID не знайдено.")
        return
    await message.answer(
        f"Група {group.group_id}\n"
        f"Коментар: {group.comment or 'відсутній'}\n"
        f"Медіа: {len(group.media)}"
    )
    for item in group.media:
        if item.media_type == "photo":
            await bot.send_photo(message.chat.id, item.file_id)
        else:
            await bot.send_video(message.chat.id, item.file_id)


async def send_start_message(message: Message) -> None:
    await message.answer(
        "Вітаю! Команди:\n"
        "/new — створити групу (потім введіть коментар або /skip)\n"
        "/add 60xxx — додати фото або відео\n"
        "/comment 60xxx — змінити коментар\n"
        "/get 60xxx — знайти групу та показати всі медіа\n\n"
        "Можна користуватися кнопками нижче. Для приватного використання "
        "відкрийте цього бота в особистому чаті Telegram."
        ,
        reply_markup=MAIN_KEYBOARD,
    )


@router.message(Command("start"))
async def start(message: Message) -> None:
    await send_start_message(message)


@router.message(Command("cancel"))
async def cancel_command(message: Message, state: FSMContext) -> None:
    await message.answer(
        "Дійсно скасувати поточну дію?",
        reply_markup=CANCEL_CONFIRMATION_KEYBOARD,
    )


@router.message(F.text == "Скасувати")
async def cancel_button(message: Message, state: FSMContext) -> None:
    await message.answer(
        "Дійсно скасувати поточну дію?",
        reply_markup=CANCEL_CONFIRMATION_KEYBOARD,
    )


@router.callback_query(F.data == "cancel_confirm")
async def confirm_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    if callback.message:
        await callback.message.edit_text("Дію скасовано.")
        await callback.message.answer(
            "Повернулися до головного меню.",
            reply_markup=MAIN_KEYBOARD,
        )


@router.callback_query(F.data == "cancel_reject")
async def reject_cancel(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message:
        await callback.message.edit_text("Скасування не підтверджено.")


@router.message(F.text == "Створити групу")
async def new_group_button(message: Message, state: FSMContext, repository: MediaRepository) -> None:
    await new_group(message, state, repository)


@router.message(Command("new"))
async def new_group(message: Message, state: FSMContext, repository: MediaRepository) -> None:
    await state.set_state(CommentStates.waiting_comment)
    await message.answer("Введіть коментар до нової групи або /skip:")


@router.message(F.text == "Додати медіа")
async def add_media_button(message: Message, state: FSMContext) -> None:
    await state.set_state(InputStates.waiting_add_group)
    await message.answer("Введіть ID групи, наприклад: 60123")


@router.message(InputStates.waiting_add_group, F.text)
async def add_media_button_group(
    message: Message, state: FSMContext, repository: MediaRepository
) -> None:
    try:
        group_id = validate_group_id(message.text)
        if not repository.group_exists(group_id):
            raise KeyError(group_id)
    except (ValueError, KeyError):
        await message.answer("Групу не знайдено. Введіть ID у форматі 60xxx.")
        return
    await state.update_data(group_id=group_id)
    await state.set_state(MediaStates.waiting_media)
    await message.answer("Надішліть фото або відео.")


@router.message(CommentStates.waiting_comment, Command("skip"))
async def create_without_comment(message: Message, state: FSMContext, repository: MediaRepository) -> None:
    group_id = repository.create_group()
    await state.clear()
    await message.answer(f"Групу створено. Її ID: {group_id}\nДодайте медіа командою /add {group_id}.")


@router.message(CommentStates.waiting_comment, F.text)
async def create_with_comment(message: Message, state: FSMContext, repository: MediaRepository) -> None:
    group_id = repository.create_group(message.text)
    await state.clear()
    await message.answer(f"Групу створено. Її ID: {group_id}\nДодайте медіа командою /add {group_id}.")


@router.message(Command("add"))
async def add_media_start(message: Message, command: CommandObject, state: FSMContext, repository: MediaRepository) -> None:
    try:
        group_id = validate_group_id(_argument(command) or "")
        if not repository.group_exists(group_id):
            raise KeyError(group_id)
    except (ValueError, KeyError):
        await message.answer("Вкажіть існуючий ID у форматі 60xxx: /add 60123")
        return
    await state.update_data(group_id=group_id)
    await state.set_state(MediaStates.waiting_media)
    await message.answer("Надішліть фото або відео. Для скасування — /cancel.")


@router.message(MediaStates.waiting_media, F.photo)
async def save_photo(message: Message, state: FSMContext, repository: MediaRepository) -> None:
    data = await state.get_data()
    repository.add_media(data["group_id"], "photo", message.photo[-1].file_id)
    await state.clear()
    await message.answer("Фото збережено без завантаження файлу.")


@router.message(MediaStates.waiting_media, F.video)
async def save_video(message: Message, state: FSMContext, repository: MediaRepository) -> None:
    data = await state.get_data()
    repository.add_media(data["group_id"], "video", message.video.file_id)
    await state.clear()
    await message.answer("Відео збережено без завантаження файлу.")


@router.message(MediaStates.waiting_media)
async def unsupported_media(message: Message) -> None:
    await message.answer("Очікую фото або відео. Для скасування — /cancel.")


@router.message(Command("comment"))
async def comment_start(message: Message, command: CommandObject, state: FSMContext, repository: MediaRepository) -> None:
    try:
        group_id = validate_group_id(_argument(command) or "")
        if not repository.group_exists(group_id):
            raise KeyError(group_id)
    except (ValueError, KeyError):
        await message.answer("Вкажіть існуючий ID у форматі 60xxx: /comment 60123")
        return
    await state.update_data(group_id=group_id)
    await state.set_state(EditCommentStates.waiting_comment)
    await message.answer("Введіть новий коментар:")


@router.message(F.text == "Змінити коментар")
async def comment_button(message: Message, state: FSMContext) -> None:
    await state.set_state(InputStates.waiting_comment_group)
    await message.answer("Введіть ID групи, наприклад: 60123")


@router.message(F.text == "Знайти групу")
async def get_group_button(message: Message, state: FSMContext) -> None:
    await state.set_state(InputStates.waiting_get_group)
    await message.answer("Введіть ID групи, наприклад: 60123")


@router.message(InputStates.waiting_comment_group, F.text)
async def comment_button_group(
    message: Message, state: FSMContext, repository: MediaRepository
) -> None:
    try:
        group_id = validate_group_id(message.text)
        if not repository.group_exists(group_id):
            raise KeyError(group_id)
    except (ValueError, KeyError):
        await message.answer("Групу не знайдено. Введіть ID у форматі 60xxx.")
        return
    await state.update_data(group_id=group_id)
    await state.set_state(EditCommentStates.waiting_comment)
    await message.answer("Введіть новий коментар:")


@router.message(InputStates.waiting_get_group, F.text)
async def get_group_button_group(
    message: Message, state: FSMContext, repository: MediaRepository, bot: Bot
) -> None:
    try:
        group_id = validate_group_id(message.text)
    except ValueError:
        await message.answer("ID має бути у форматі 60xxx.")
        return
    await state.clear()
    await _send_group(bot, message, repository, group_id)


@router.message(EditCommentStates.waiting_comment, F.text)
async def update_comment(message: Message, state: FSMContext, repository: MediaRepository) -> None:
    data = await state.get_data()
    repository.set_comment(data["group_id"], message.text)
    await state.clear()
    await message.answer(f"Коментар групи {data['group_id']} оновлено.")


@router.message(Command("get"))
async def get_group(message: Message, command: CommandObject, repository: MediaRepository, bot: Bot) -> None:
    try:
        group_id = validate_group_id(_argument(command) or "")
    except ValueError:
        await message.answer("ID має бути у форматі 60xxx: /get 60123")
        return
    await _send_group(bot, message, repository, group_id)


async def main() -> None:
    settings = load_settings()
    repository = MediaRepository(Path(settings.database_path))
    bot = Bot(settings.bot_token)
    dispatcher = Dispatcher()
    dispatcher["repository"] = repository
    dispatcher.include_router(router)
    try:
        await dispatcher.start_polling(bot)
    finally:
        repository.close()
        await bot.session.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
