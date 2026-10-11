from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError
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
from .database import (
    ROLE_ADMIN,
    ROLE_EDITOR,
    ROLE_NAMES,
    ROLE_OWNER,
    ROLE_VIEWER,
    MediaRepository,
    validate_group_id,
)

router = Router()

ROLE_LABELS = {
    ROLE_OWNER: "власник",
    ROLE_ADMIN: "адміністратор",
    ROLE_EDITOR: "редактор медіа",
    ROLE_VIEWER: "переглядач",
}
CREATE_SIZES = ("250", "500", "750")
CREATE_LIGHTING = ("Full", "Light")
ROLE_BUTTONS = {
    "Адміністратор": ROLE_ADMIN,
    "Редактор медіа": ROLE_EDITOR,
    "Переглядач": ROLE_VIEWER,
}


def scenario_keyboard(*buttons: str) -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(text=button)] for button in buttons]
    rows.append([KeyboardButton(text="Назад"), KeyboardButton(text="Головне меню")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


def keyboard_for(role: int | None) -> ReplyKeyboardMarkup:
    rows: list[list[KeyboardButton]] = []
    if role in (ROLE_OWNER, ROLE_ADMIN, ROLE_EDITOR):
        rows.append([KeyboardButton(text="Створити осмос"), KeyboardButton(text="Редагувати осмос")])
    if role in (ROLE_OWNER, ROLE_ADMIN):
        rows.append([KeyboardButton(text="Керувати користувачами")])
    if role is not None:
        rows.append([KeyboardButton(text="Знайти осмос"), KeyboardButton(text="Список осмосів")])
    rows.append([KeyboardButton(text="Назад"), KeyboardButton(text="Головне меню")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


def role_keyboard(actor_role: int) -> ReplyKeyboardMarkup:
    choices = list(ROLE_BUTTONS)
    return scenario_keyboard(*choices)


class CreateStates(StatesGroup):
    waiting_id = State()
    waiting_size = State()
    waiting_lighting = State()
    waiting_media = State()
    waiting_comment = State()


class EditStates(StatesGroup):
    waiting_id = State()
    waiting_action = State()
    waiting_new_id = State()
    waiting_size = State()
    waiting_lighting = State()
    adding_media = State()
    replacing_media = State()
    waiting_comment = State()


class AddMediaStates(StatesGroup):
    waiting_id = State()
    waiting_media = State()


class GetStates(StatesGroup):
    waiting_id = State()


class UserStates(StatesGroup):
    waiting_contact = State()
    waiting_role = State()


CONFIRM_NAVIGATION = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="Так, вийти", callback_data="nav_yes"),
    InlineKeyboardButton(text="Ні", callback_data="nav_no"),
]])


def confirmation_keyboard(yes: str, no: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Так, підтверджую", callback_data=yes),
        InlineKeyboardButton(text="Ні", callback_data=no),
    ]])


def group_page_keyboard(repository: MediaRepository, page: int) -> InlineKeyboardMarkup:
    all_groups = repository.list_groups()
    page_size = 10
    page_count = max(1, (len(all_groups) + page_size - 1) // page_size)
    page = max(0, min(page, page_count - 1))
    entries = all_groups[page * page_size:(page + 1) * page_size]
    rows = [
        [InlineKeyboardButton(text=row["group_id"], callback_data=f"osmos_view:{row['group_id']}")]
        for row in entries
    ]
    paging: list[InlineKeyboardButton] = []
    if page > 0:
        paging.append(InlineKeyboardButton(text="‹ Назад", callback_data=f"osmos_page:{page - 1}"))
    if page < page_count - 1:
        paging.append(InlineKeyboardButton(text="Далі ›", callback_data=f"osmos_page:{page + 1}"))
    if paging:
        rows.append(paging)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def group_page_text(repository: MediaRepository, page: int) -> str:
    entries = repository.list_groups()
    page_count = max(1, (len(entries) + 9) // 10)
    page = max(0, min(page, page_count - 1))
    current = entries[page * 10:(page + 1) * 10]
    interval = f"{current[0]['group_id']}–{current[-1]['group_id']}" if current else "немає"
    return f"Збережені осмоси — сторінка {page + 1}/{page_count}, ID: {interval}"


def _name(row) -> str:
    if int(row["role"]) == ROLE_OWNER:
        return "Власник"
    return row["display_name"] or row["username"] or f"user_{row['user_id']}"


def user_list_keyboard(repository: MediaRepository) -> InlineKeyboardMarkup:
    rows = []
    for user in repository.list_users():
        rows.append([InlineKeyboardButton(
            text=f"{_name(user)} ({user['user_id']})"[:60],
            callback_data=f"user_select:{user['user_id']}",
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def user_actions_keyboard(user_id: int, allow_manage: bool) -> InlineKeyboardMarkup:
    rows = []
    if allow_manage:
        rows.extend([
            [InlineKeyboardButton(text="Змінити роль", callback_data=f"user_role:{user_id}")],
            [InlineKeyboardButton(text="Видалити користувача", callback_data=f"user_delete:{user_id}")],
        ])
    rows.append([InlineKeyboardButton(text="До списку", callback_data="users_list")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _role(message: Message, repository: MediaRepository) -> int | None:
    return repository.get_user_role(message.from_user.id) if message.from_user else None


async def _blocked(message: Message, repository: MediaRepository) -> bool:
    if _role(message, repository) is None:
        await message.answer("Доступ заборонено. Попросіть власника додати вас.")
        return True
    return False


async def _send_menu(message: Message, repository: MediaRepository, greeting: bool = False) -> None:
    role = _role(message, repository)
    if role is None:
        await _blocked(message, repository)
        return
    descriptions = {
        ROLE_OWNER: "Повний доступ: користувачі, осмоси, медіа та коментарі.",
        ROLE_ADMIN: "Повний доступ, крім керування власником.",
        ROLE_EDITOR: "Створення й редагування осмосів та перегляд усіх осмосів.",
        ROLE_VIEWER: "Перегляд осмосів і медіа.",
    }
    text = f"Ваша роль: {ROLE_LABELS[role]}.\n{descriptions[role]}" if greeting else "Головне меню."
    await message.answer(text, reply_markup=keyboard_for(role))


async def _show_group(bot: Bot, message: Message, repository: MediaRepository, group_id: str) -> None:
    group = repository.get_group(group_id)
    if group is None:
        await message.answer("Осмос із таким ID не знайдено.")
        return
    type_label = " ".join(part for part in (group.size, group.lighting) if part) or "не вказано"
    await message.answer(
        f"Осмос {group.group_id}\n"
        f"Тип: {type_label}\n"
        f"Коментар: {group.comment or 'відсутній'}\n"
        f"Медіа: {len(group.media)}"
    )
    for item in group.media:
        if item.media_type == "photo":
            await bot.send_photo(message.chat.id, item.file_id)
        else:
            await bot.send_video(message.chat.id, item.file_id)


async def _previous_scene(message: Message, state_name: str) -> None:
    prompts = {
        "CreateStates:waiting_id": ("Введіть ID осмосу у форматі 60xxx:", scenario_keyboard()),
        "CreateStates:waiting_size": ("Оберіть число типу осмосу:", scenario_keyboard(*CREATE_SIZES)),
        "CreateStates:waiting_lighting": ("Оберіть Full або Light:", scenario_keyboard(*CREATE_LIGHTING)),
        "CreateStates:waiting_media": (
            "Надішліть фото/відео, або завершіть додавання кнопкою.",
            scenario_keyboard("Готово", "Пропустити медіа"),
        ),
        "CreateStates:waiting_comment": ("Введіть коментар або пропустіть:", scenario_keyboard("Пропустити коментар")),
        "EditStates:waiting_id": ("Введіть ID осмосу для редагування:", scenario_keyboard()),
        "EditStates:waiting_action": (
            "Оберіть дію для осмосу:",
            scenario_keyboard("Змінити ID", "Змінити тип", "Додати медіа", "Замінити медіа",
                              "Очистити медіа", "Змінити коментар", "Видалити осмос"),
        ),
        "EditStates:waiting_new_id": ("Введіть новий ID осмосу:", scenario_keyboard()),
        "EditStates:waiting_size": ("Оберіть число типу осмосу:", scenario_keyboard(*CREATE_SIZES)),
        "EditStates:waiting_lighting": ("Оберіть Full або Light:", scenario_keyboard(*CREATE_LIGHTING)),
        "EditStates:waiting_comment": ("Введіть новий коментар:", scenario_keyboard()),
        "EditStates:adding_media": (
            "Надішліть ще фото/відео або завершіть додавання.",
            scenario_keyboard("Готово"),
        ),
        "EditStates:replacing_media": ("Надішліть нове фото або відео:", scenario_keyboard()),
        "AddMediaStates:waiting_id": ("Введіть ID осмосу:", scenario_keyboard()),
        "GetStates:waiting_id": ("Введіть ID осмосу:", scenario_keyboard()),
    }
    prompt, markup = prompts.get(state_name, ("Повернення до попереднього кроку.", scenario_keyboard()))
    await message.answer(prompt, reply_markup=markup)


PREVIOUS_STATES = {
    "CreateStates:waiting_size": "CreateStates:waiting_id",
    "CreateStates:waiting_lighting": "CreateStates:waiting_size",
    "CreateStates:waiting_media": "CreateStates:waiting_lighting",
    "CreateStates:waiting_comment": "CreateStates:waiting_media",
    "EditStates:waiting_action": "EditStates:waiting_id",
    "EditStates:waiting_new_id": "EditStates:waiting_action",
    "EditStates:waiting_size": "EditStates:waiting_action",
    "EditStates:waiting_lighting": "EditStates:waiting_size",
    "EditStates:adding_media": "EditStates:waiting_action",
    "EditStates:replacing_media": "EditStates:waiting_action",
    "EditStates:waiting_comment": "EditStates:waiting_action",
    "AddMediaStates:waiting_media": "AddMediaStates:waiting_id",
}


async def _navigate(message: Message, state: FSMContext, repository: MediaRepository, to_main: bool) -> None:
    current = await state.get_state()
    data = await state.get_data()
    target = None if to_main else PREVIOUS_STATES.get(current or "")
    if current and data:
        await state.update_data(_nav_target="main" if to_main or not target else target)
        await message.answer("Незбережений текст або дані буде втрачено. Продовжити?", reply_markup=CONFIRM_NAVIGATION)
        return
    if target:
        await state.set_state(target)
        await _previous_scene(message, target)
        return
    await state.clear()
    await _send_menu(message, repository)


@router.message(Command("start"))
async def start(message: Message, repository: MediaRepository) -> None:
    await _send_menu(message, repository, greeting=True)


@router.message(Command("cancel"))
@router.message(F.text == "Скасувати")
async def cancel(message: Message, state: FSMContext, repository: MediaRepository) -> None:
    await _navigate(message, state, repository, to_main=True)


@router.message(F.text == "Назад")
async def go_back(message: Message, state: FSMContext, repository: MediaRepository) -> None:
    await _navigate(message, state, repository, to_main=False)


@router.message(F.text == "Головне меню")
async def go_main(message: Message, state: FSMContext, repository: MediaRepository) -> None:
    await _navigate(message, state, repository, to_main=True)


@router.callback_query(F.data == "nav_yes")
async def nav_yes(callback: CallbackQuery, state: FSMContext, repository: MediaRepository) -> None:
    data = await state.get_data()
    target = data.pop("_nav_target", "main")
    await state.clear()
    await callback.answer()
    if callback.message:
        if target != "main":
            await state.update_data(**data)
            await state.set_state(target)
            await _previous_scene(callback.message, target)
        else:
            role = repository.get_user_role(callback.from_user.id)
            if role is None:
                await callback.message.answer("Доступ заборонено.")
            else:
                await callback.message.answer("Головне меню.", reply_markup=keyboard_for(role))


@router.callback_query(F.data == "nav_no")
async def nav_no(callback: CallbackQuery) -> None:
    await callback.answer("Продовжуємо сценарій")


@router.message(F.text == "Створити осмос")
@router.message(Command("new"))
async def create_start(message: Message, state: FSMContext, repository: MediaRepository) -> None:
    if await _blocked(message, repository) or _role(message, repository) not in (ROLE_OWNER, ROLE_ADMIN, ROLE_EDITOR):
        return
    await state.clear()
    await state.set_state(CreateStates.waiting_id)
    await message.answer("Введіть ID осмосу вручну у форматі 60xxx:", reply_markup=scenario_keyboard())


@router.message(CreateStates.waiting_id, F.text)
async def create_id(message: Message, state: FSMContext, repository: MediaRepository) -> None:
    try:
        group_id = validate_group_id(message.text)
    except ValueError:
        await message.answer("ID має містити рівно 5 цифр і починатися з 60.")
        return
    if repository.group_exists(group_id):
        await message.answer(f"Осмос {group_id} уже існує. Введіть інший ID:", reply_markup=scenario_keyboard())
        return
    await state.update_data(group_id=group_id, media=[])
    await state.set_state(CreateStates.waiting_size)
    await message.answer("Оберіть число типу осмосу:", reply_markup=scenario_keyboard(*CREATE_SIZES))


@router.message(CreateStates.waiting_size, F.text.in_(CREATE_SIZES))
async def create_size(message: Message, state: FSMContext) -> None:
    await state.update_data(size=message.text)
    await state.set_state(CreateStates.waiting_lighting)
    await message.answer("Оберіть Full або Light:", reply_markup=scenario_keyboard(*CREATE_LIGHTING))


@router.message(CreateStates.waiting_lighting, F.text.in_(CREATE_LIGHTING))
async def create_lighting(message: Message, state: FSMContext) -> None:
    await state.update_data(lighting=message.text)
    await state.set_state(CreateStates.waiting_media)
    await message.answer(
        "Надішліть фото/відео. Коли завершите — натисніть «Готово», або пропустіть медіа.",
        reply_markup=scenario_keyboard("Готово", "Пропустити медіа"),
    )


@router.message(CreateStates.waiting_media, F.photo | F.video)
async def create_media(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    media = list(data.get("media", []))
    if message.photo:
        media.append(("photo", message.photo[-1].file_id))
    elif message.video:
        media.append(("video", message.video.file_id))
    await state.update_data(media=media)
    await message.answer(f"Медіа додано: {len(media)}.", reply_markup=scenario_keyboard("Готово", "Пропустити медіа"))


@router.message(CreateStates.waiting_media, F.text.in_({"Готово", "Пропустити медіа"}))
async def create_media_done(message: Message, state: FSMContext) -> None:
    await state.set_state(CreateStates.waiting_comment)
    await message.answer("Введіть коментар або натисніть «Пропустити коментар».",
                         reply_markup=scenario_keyboard("Пропустити коментар"))


@router.message(CreateStates.waiting_comment, Command("skip"))
@router.message(CreateStates.waiting_comment, F.text.in_({"Пропустити коментар"}))
@router.message(CreateStates.waiting_comment, F.text)
async def create_finish(message: Message, state: FSMContext, repository: MediaRepository) -> None:
    data = await state.get_data()
    comment = None if message.text in {"/skip", "Пропустити коментар"} else message.text
    try:
        group_id = repository.create_group(data["group_id"], comment, data["size"], data["lighting"])
        for media_type, file_id in data.get("media", []):
            repository.add_media(group_id, media_type, file_id)
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await state.clear()
    await message.answer(f"Осмос {group_id} створено.", reply_markup=keyboard_for(_role(message, repository)))


@router.message(F.text == "Редагувати осмос")
async def edit_start(message: Message, state: FSMContext, repository: MediaRepository) -> None:
    if await _blocked(message, repository) or _role(message, repository) not in (ROLE_OWNER, ROLE_ADMIN, ROLE_EDITOR):
        return
    await state.clear()
    await state.set_state(EditStates.waiting_id)
    await message.answer("Введіть ID осмосу для редагування:", reply_markup=scenario_keyboard())


def _edit_actions(group_id: str) -> ReplyKeyboardMarkup:
    return scenario_keyboard("Змінити ID", "Змінити тип", "Додати медіа", "Замінити медіа",
                            "Очистити медіа", "Змінити коментар", "Видалити осмос")


@router.message(EditStates.waiting_id, F.text)
async def edit_select(message: Message, state: FSMContext, repository: MediaRepository) -> None:
    try:
        group_id = validate_group_id(message.text)
        group = repository.get_group(group_id)
        if group is None:
            raise KeyError(group_id)
    except (ValueError, KeyError):
        await message.answer("Осмос не знайдено. Введіть ID у форматі 60xxx.")
        return
    await state.update_data(group_id=group_id)
    await state.set_state(EditStates.waiting_action)
    await message.answer(f"Редагування осмосу {group_id}. Оберіть, що змінити:",
                         reply_markup=_edit_actions(group_id))


@router.message(EditStates.waiting_action, F.text == "Змінити ID")
async def edit_id_start(message: Message, state: FSMContext) -> None:
    await state.set_state(EditStates.waiting_new_id)
    await message.answer("Введіть новий ID осмосу у форматі 60xxx:", reply_markup=scenario_keyboard())


@router.message(EditStates.waiting_new_id, F.text)
async def edit_id_save(message: Message, state: FSMContext, repository: MediaRepository) -> None:
    data = await state.get_data()
    try:
        new_id = repository.rename_group(data["group_id"], message.text)
    except (ValueError, KeyError) as exc:
        await message.answer(str(exc) or "Осмос не знайдено.")
        return
    await state.update_data(group_id=new_id)
    await state.set_state(EditStates.waiting_action)
    await message.answer(f"ID змінено на {new_id}.", reply_markup=_edit_actions(new_id))


@router.message(EditStates.waiting_action, F.text == "Змінити тип")
async def edit_type_start(message: Message, state: FSMContext) -> None:
    await state.set_state(EditStates.waiting_size)
    await message.answer("Оберіть число типу осмосу:", reply_markup=scenario_keyboard(*CREATE_SIZES))


@router.message(EditStates.waiting_size, F.text.in_(CREATE_SIZES))
async def edit_type_size(message: Message, state: FSMContext) -> None:
    await state.update_data(size=message.text)
    await state.set_state(EditStates.waiting_lighting)
    await message.answer("Оберіть Full або Light:", reply_markup=scenario_keyboard(*CREATE_LIGHTING))


@router.message(EditStates.waiting_lighting, F.text.in_(CREATE_LIGHTING))
async def edit_type_save(message: Message, state: FSMContext, repository: MediaRepository) -> None:
    data = await state.get_data()
    repository.set_type(data["group_id"], data["size"], message.text)
    await state.set_state(EditStates.waiting_action)
    await message.answer("Тип осмосу змінено.", reply_markup=_edit_actions(data["group_id"]))


@router.message(EditStates.waiting_action, F.text.in_({"Додати медіа", "Замінити медіа"}))
async def edit_media_start(message: Message, state: FSMContext) -> None:
    replace = message.text == "Замінити медіа"
    await state.update_data(replace_media=replace)
    await state.set_state(EditStates.replacing_media if replace else EditStates.adding_media)
    await message.answer("Надішліть фото або відео.", reply_markup=scenario_keyboard())


@router.message(EditStates.adding_media, F.photo | F.video)
@router.message(EditStates.replacing_media, F.photo | F.video)
async def edit_media_save(message: Message, state: FSMContext, repository: MediaRepository) -> None:
    data = await state.get_data()
    if data.get("replace_media"):
        repository.clear_media(data["group_id"])
    if message.photo:
        repository.add_media(data["group_id"], "photo", message.photo[-1].file_id)
    elif message.video:
        repository.add_media(data["group_id"], "video", message.video.file_id)
    await state.update_data(replace_media=False)
    await state.set_state(EditStates.adding_media)
    await message.answer("Медіа додано. Надішліть ще або натисніть «Готово».",
                         reply_markup=scenario_keyboard("Готово"))


@router.message(EditStates.adding_media, F.text == "Готово")
async def edit_media_done(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.set_state(EditStates.waiting_action)
    await message.answer("Медіа осмосу оновлено.", reply_markup=_edit_actions(data["group_id"]))


@router.message(EditStates.waiting_action, F.text == "Очистити медіа")
async def clear_media_confirm(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    buttons = confirmation_keyboard(f"clear_media_yes:{data['group_id']}", "clear_media_no")
    await message.answer(f"Очистити всі фото та відео осмосу {data['group_id']}?", reply_markup=buttons)


@router.callback_query(F.data.startswith("clear_media_yes:"))
async def clear_media_yes(callback: CallbackQuery, state: FSMContext, repository: MediaRepository) -> None:
    if repository.get_user_role(callback.from_user.id) not in (ROLE_OWNER, ROLE_ADMIN, ROLE_EDITOR):
        await callback.answer("Недостатньо прав", show_alert=True)
        return
    group_id = callback.data.split(":", 1)[1]
    repository.clear_media(group_id)
    await callback.answer("Медіа очищено")
    if callback.message:
        await callback.message.edit_text(f"Медіа осмосу {group_id} очищено.")


@router.callback_query(F.data == "clear_media_no")
async def clear_media_no(callback: CallbackQuery) -> None:
    await callback.answer("Скасовано")
    if callback.message:
        await callback.message.edit_text("Очищення медіа скасовано.")


@router.message(EditStates.waiting_action, F.text == "Змінити коментар")
async def edit_comment_start(message: Message, state: FSMContext) -> None:
    await state.set_state(EditStates.waiting_comment)
    await message.answer("Введіть новий коментар або /skip щоб очистити:", reply_markup=scenario_keyboard())


@router.message(Command("comment"))
async def comment_command(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    repository: MediaRepository,
) -> None:
    if await _blocked(message, repository) or _role(message, repository) not in (
        ROLE_OWNER, ROLE_ADMIN, ROLE_EDITOR,
    ):
        return
    group_id = (command.args or "").strip()
    try:
        group_id = validate_group_id(group_id)
        if not repository.group_exists(group_id):
            raise KeyError(group_id)
    except (ValueError, KeyError):
        await message.answer("Вкажіть існуючий ID осмосу, наприклад: /comment 60123")
        return
    await state.clear()
    await state.update_data(group_id=group_id)
    await state.set_state(EditStates.waiting_comment)
    await message.answer("Введіть новий коментар або /skip щоб очистити:", reply_markup=scenario_keyboard())


@router.message(EditStates.waiting_comment, Command("skip"))
@router.message(EditStates.waiting_comment, F.text)
async def edit_comment_save(message: Message, state: FSMContext, repository: MediaRepository) -> None:
    data = await state.get_data()
    repository.set_comment(
        data["group_id"],
        "" if message.text in {"/skip", "Пропустити коментар"} else message.text,
    )
    await state.set_state(EditStates.waiting_action)
    await message.answer("Коментар змінено.", reply_markup=_edit_actions(data["group_id"]))


@router.message(EditStates.waiting_action, F.text == "Видалити осмос")
async def delete_osmos_confirm(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    markup = confirmation_keyboard(f"delete_osmos_yes:{data['group_id']}", "delete_osmos_no")
    await message.answer(f"Ви впевнені, що хочете видалити осмос {data['group_id']}?", reply_markup=markup)


@router.callback_query(F.data.startswith("delete_osmos_yes:"))
async def delete_osmos_yes(callback: CallbackQuery, state: FSMContext, repository: MediaRepository) -> None:
    if repository.get_user_role(callback.from_user.id) not in (ROLE_OWNER, ROLE_ADMIN, ROLE_EDITOR):
        await callback.answer("Недостатньо прав", show_alert=True)
        return
    group_id = callback.data.split(":", 1)[1]
    repository.delete_group(group_id)
    await state.clear()
    await callback.answer("Осмос видалено")
    if callback.message:
        role = repository.get_user_role(callback.from_user.id)
        await callback.message.edit_text(f"Осмос {group_id} видалено.")
        await callback.message.answer("Головне меню.", reply_markup=keyboard_for(role))


@router.callback_query(F.data == "delete_osmos_no")
async def delete_osmos_no(callback: CallbackQuery) -> None:
    await callback.answer("Видалення скасовано")
    if callback.message:
        await callback.message.edit_text("Видалення осмосу скасовано.")


@router.message(F.text == "Додати медіа")
@router.message(Command("add"))
async def add_media_start(
    message: Message,
    state: FSMContext,
    repository: MediaRepository,
    command: CommandObject | None = None,
) -> None:
    if await _blocked(message, repository) or _role(message, repository) not in (ROLE_OWNER, ROLE_ADMIN, ROLE_EDITOR):
        return
    group_id = (command.args or "").strip() if command else ""
    if group_id:
        try:
            group_id = validate_group_id(group_id)
            if not repository.group_exists(group_id):
                raise KeyError
        except (ValueError, KeyError):
            await message.answer("Вкажіть існуючий ID осмосу у форматі 60xxx.")
            return
        await state.update_data(group_id=group_id)
        await state.set_state(AddMediaStates.waiting_media)
        await message.answer("Надішліть фото або відео.", reply_markup=scenario_keyboard())
    else:
        await state.set_state(AddMediaStates.waiting_id)
        await message.answer("Введіть ID осмосу:", reply_markup=scenario_keyboard())


@router.message(AddMediaStates.waiting_id, F.text)
async def add_media_id(message: Message, state: FSMContext, repository: MediaRepository) -> None:
    try:
        group_id = validate_group_id(message.text)
        if not repository.group_exists(group_id):
            raise KeyError
    except (ValueError, KeyError):
        await message.answer("Осмос не знайдено. Введіть ID у форматі 60xxx.")
        return
    await state.update_data(group_id=group_id)
    await state.set_state(AddMediaStates.waiting_media)
    await message.answer("Надішліть фото або відео.", reply_markup=scenario_keyboard())


@router.message(AddMediaStates.waiting_media, F.photo | F.video)
async def add_media_save(message: Message, state: FSMContext, repository: MediaRepository) -> None:
    data = await state.get_data()
    if message.photo:
        repository.add_media(data["group_id"], "photo", message.photo[-1].file_id)
    else:
        repository.add_media(data["group_id"], "video", message.video.file_id)
    await state.clear()
    await message.answer("Медіа збережено.", reply_markup=keyboard_for(_role(message, repository)))


@router.message(F.text == "Знайти осмос")
@router.message(Command("get"))
async def find_start(
    message: Message,
    state: FSMContext,
    repository: MediaRepository,
    command: CommandObject | None = None,
    bot: Bot | None = None,
) -> None:
    if await _blocked(message, repository):
        return
    group_id = (command.args or "").strip() if command else ""
    if group_id:
        try:
            group_id = validate_group_id(group_id)
        except ValueError:
            await message.answer("ID має бути у форматі 60xxx.")
            return
        if bot:
            await _show_group(bot, message, repository, group_id)
        return
    await state.set_state(GetStates.waiting_id)
    await message.answer("Введіть ID осмосу:", reply_markup=scenario_keyboard())


@router.message(GetStates.waiting_id, F.text)
async def find_id(message: Message, state: FSMContext, repository: MediaRepository, bot: Bot) -> None:
    try:
        group_id = validate_group_id(message.text)
    except ValueError:
        await message.answer("ID має бути у форматі 60xxx.")
        return
    await state.clear()
    await _show_group(bot, message, repository, group_id)


@router.message(F.text == "Список осмосів")
async def list_osmos(message: Message, repository: MediaRepository) -> None:
    if await _blocked(message, repository):
        return
    await message.answer(group_page_text(repository, 0), reply_markup=group_page_keyboard(repository, 0))


@router.callback_query(F.data.startswith("osmos_page:"))
async def page_osmos(callback: CallbackQuery, repository: MediaRepository) -> None:
    if repository.get_user_role(callback.from_user.id) is None:
        await callback.answer("Доступ заборонено", show_alert=True)
        return
    page = int(callback.data.split(":", 1)[1])
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(group_page_text(repository, page), reply_markup=group_page_keyboard(repository, page))


@router.callback_query(F.data.startswith("osmos_view:"))
async def view_osmos(callback: CallbackQuery, repository: MediaRepository, bot: Bot) -> None:
    group_id = callback.data.split(":", 1)[1]
    if repository.get_user_role(callback.from_user.id) is None:
        await callback.answer("Доступ заборонено", show_alert=True)
        return
    group = repository.get_group(group_id)
    await callback.answer()
    if group is None:
        if callback.message:
            await callback.message.answer("Осмос не знайдено.")
        return
    if callback.message:
        await _show_group(bot, callback.message, repository, group_id)


def _format_user_list(repository: MediaRepository) -> str:
    users = repository.list_users()
    if not users:
        return "Користувачів ще немає."
    lines = ["Користувачі:"]
    for user in users:
        added = f" (додано: {user['added_by']})" if user["added_by"] else " (додано вручну)"
        lines.append(f"- {_name(user)} ({user['user_id']}) — {ROLE_LABELS[int(user['role'])]}{added}")
    return "\n".join(lines)


@router.message(F.text == "Керувати користувачами")
async def manage_users(message: Message, state: FSMContext, repository: MediaRepository) -> None:
    if _role(message, repository) not in (ROLE_OWNER, ROLE_ADMIN):
        await _blocked(message, repository)
        return
    await state.clear()
    await message.answer(_format_user_list(repository), reply_markup=user_list_keyboard(repository))
    await message.answer("Оберіть користувача або додайте нового:",
                         reply_markup=scenario_keyboard("Додати користувача"))


@router.callback_query(F.data == "users_list")
async def users_list(callback: CallbackQuery, repository: MediaRepository) -> None:
    if repository.get_user_role(callback.from_user.id) not in (ROLE_OWNER, ROLE_ADMIN):
        await callback.answer("Недостатньо прав", show_alert=True)
        return
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(_format_user_list(repository), reply_markup=user_list_keyboard(repository))


@router.callback_query(F.data.startswith("user_select:"))
async def user_select(callback: CallbackQuery, repository: MediaRepository) -> None:
    actor_role = repository.get_user_role(callback.from_user.id)
    if actor_role not in (ROLE_OWNER, ROLE_ADMIN):
        await callback.answer("Недостатньо прав", show_alert=True)
        return
    user_id = int(callback.data.split(":", 1)[1])
    user = repository.get_user_record(user_id)
    if user is None:
        await callback.answer("Користувача не знайдено", show_alert=True)
        return
    allow_manage = int(user["role"]) != ROLE_OWNER
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            f"{_name(user)} ({user_id})\nРоль: {ROLE_LABELS[int(user['role'])]}",
            reply_markup=user_actions_keyboard(user_id, allow_manage),
        )


@router.callback_query(F.data.startswith("user_role:"))
async def user_role_start(callback: CallbackQuery, state: FSMContext, repository: MediaRepository) -> None:
    actor_role = repository.get_user_role(callback.from_user.id)
    user_id = int(callback.data.split(":", 1)[1])
    target_role = repository.get_user_role(user_id)
    if actor_role not in (ROLE_OWNER, ROLE_ADMIN) or target_role is None:
        await callback.answer("Користувача не знайдено", show_alert=True)
        return
    if target_role == ROLE_OWNER:
        await callback.answer("Недостатньо прав", show_alert=True)
        return
    await state.clear()
    await state.update_data(user_id=user_id, action="role")
    await state.set_state(UserStates.waiting_role)
    await callback.answer()
    if callback.message:
        await callback.message.answer("Оберіть роль:", reply_markup=role_keyboard(actor_role))


@router.callback_query(F.data.startswith("user_delete:"))
async def user_delete(callback: CallbackQuery, repository: MediaRepository, bot: Bot) -> None:
    actor_role = repository.get_user_role(callback.from_user.id)
    user_id = int(callback.data.split(":", 1)[1])
    target_role = repository.get_user_role(user_id)
    if actor_role not in (ROLE_OWNER, ROLE_ADMIN) or target_role in (None, ROLE_OWNER):
        await callback.answer("Цього користувача не можна видалити", show_alert=True)
        return
    repository.remove_user(user_id)
    try:
        await bot.send_message(user_id, "Ваш доступ до бота скасовано.")
    except TelegramAPIError:
        logging.exception("Could not notify removed user %s", user_id)
    await callback.answer("Користувача видалено")
    if callback.message:
        await callback.message.edit_text(_format_user_list(repository), reply_markup=user_list_keyboard(repository))


@router.message(F.text == "Додати користувача")
async def add_user_start(message: Message, state: FSMContext, repository: MediaRepository) -> None:
    if _role(message, repository) not in (ROLE_OWNER, ROLE_ADMIN):
        await _blocked(message, repository)
        return
    await state.clear()
    await state.update_data(action="add")
    await state.set_state(UserStates.waiting_contact)
    await message.answer(
        "Надішліть контакт іншого користувача звичайним повідомленням. Власний контакт не приймається.",
        reply_markup=scenario_keyboard(),
    )


def _is_self_contact(message: Message) -> bool:
    return bool(message.contact and message.from_user and message.contact.user_id == message.from_user.id)


@router.message(UserStates.waiting_contact, F.contact)
async def user_contact(message: Message, state: FSMContext, repository: MediaRepository) -> None:
    actor_role = _role(message, repository)
    if actor_role not in (ROLE_OWNER, ROLE_ADMIN):
        await _blocked(message, repository)
        return
    if not message.contact.user_id:
        await message.answer("У контакті немає Telegram ID. Надішліть інший контакт.")
        return
    if _is_self_contact(message):
        await message.answer("Не можна додати себе. Надішліть контакт іншого користувача.")
        return
    user_id = message.contact.user_id
    current_role = repository.get_user_role(user_id)
    if current_role == ROLE_OWNER:
        await message.answer("У вас немає прав змінювати цього користувача.")
        return
    data = await state.get_data()
    await state.update_data(
        user_id=user_id,
        name=" ".join(
            part for part in (message.contact.first_name, message.contact.last_name) if part
        ),
        username=getattr(message.contact, "username", "") or "",
        action=data.get("action", "add"),
    )
    await state.set_state(UserStates.waiting_role)
    await message.answer("Оберіть роль:", reply_markup=role_keyboard(actor_role))


@router.message(UserStates.waiting_role, F.text.in_(ROLE_BUTTONS))
async def user_role_save(message: Message, state: FSMContext, repository: MediaRepository, bot: Bot) -> None:
    actor_role = _role(message, repository)
    role = ROLE_BUTTONS[message.text]
    if actor_role not in (ROLE_OWNER, ROLE_ADMIN):
        await message.answer("Ця роль недоступна.")
        return
    data = await state.get_data()
    user_id = int(data["user_id"])
    repository.add_user(
        user_id,
        role,
        data.get("name", ""),
        data.get("username", ""),
        added_by=message.from_user.id if data.get("action") == "add" else None,
        updated_by=message.from_user.id,
    )
    try:
        await bot.send_message(user_id, f"Ваш доступ до бота підтверджено. Роль: {ROLE_LABELS[role]}.")
    except TelegramAPIError:
        logging.exception("Could not notify user %s about bot access", user_id)
    await state.clear()
    await message.answer("Роль призначено/оновлено.", reply_markup=keyboard_for(actor_role))


@router.message(F.text.in_(ROLE_BUTTONS))
async def reject_unexpected_role(message: Message, repository: MediaRepository) -> None:
    if await _blocked(message, repository):
        return


@router.message()
async def unknown_message(message: Message, repository: MediaRepository) -> None:
    if _role(message, repository) is None:
        await _blocked(message, repository)
    else:
        await message.answer("Скористайтеся кнопками меню або командою /start.")


async def main() -> None:
    settings = load_settings()
    repository = MediaRepository(Path(settings.database_path))
    if settings.owner_id is not None:
        repository.add_user(settings.owner_id, ROLE_OWNER)
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
