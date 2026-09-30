import pytest
import sqlite3

from app.bot import (
    CREATE_LIGHTING,
    CREATE_SIZES,
    group_page_keyboard,
    group_page_text,
    keyboard_for,
    scenario_keyboard,
    user_actions_keyboard,
)
from app.database import ROLE_ADMIN, ROLE_EDITOR, ROLE_OWNER, ROLE_VIEWER, MediaRepository, validate_group_id


def test_group_id_validation():
    assert validate_group_id("60123") == "60123"
    for value in ("12345", "61000", "6000", "60abc", " 60123 "):
        if value == " 60123 ":
            assert validate_group_id(value) == "60123"
        else:
            with pytest.raises(ValueError):
                validate_group_id(value)


def test_group_lifecycle_and_file_ids(tmp_path):
    repository = MediaRepository(tmp_path / "test.sqlite3")
    group_id = repository.create_group("60001", "відпустка", "500", "Full")
    assert group_id.startswith("60")
    repository.add_media(group_id, "photo", "photo-file-id")
    repository.add_media(group_id, "video", "video-file-id")
    repository.set_comment(group_id, "оновлений коментар")
    group = repository.get_group(group_id)
    assert group is not None
    assert group.comment == "оновлений коментар"
    assert (group.size, group.lighting) == ("500", "Full")
    assert [(item.media_type, item.file_id) for item in group.media] == [
        ("photo", "photo-file-id"),
        ("video", "video-file-id"),
    ]
    repository.close()


def test_unknown_group_is_rejected(tmp_path):
    repository = MediaRepository(tmp_path / "test.sqlite3")
    with pytest.raises(KeyError):
        repository.add_media("60123", "photo", "id")
    assert repository.get_group("60123") is None
    repository.close()


def test_group_id_is_user_supplied_and_unique(tmp_path):
    repository = MediaRepository(tmp_path / "manual.sqlite3")
    assert repository.create_group("60042", "manual") == "60042"
    with pytest.raises(ValueError):
        repository.create_group("60042")
    with pytest.raises(ValueError):
        repository.create_group("6012", "invalid")
    repository.close()


def test_group_can_be_renamed_without_losing_media(tmp_path):
    repository = MediaRepository(tmp_path / "rename.sqlite3")
    repository.create_group("60042", "manual", "250", "Light")
    repository.add_media("60042", "photo", "photo-file-id")
    assert repository.rename_group("60042", "60043") == "60043"
    assert repository.get_group("60042") is None
    renamed = repository.get_group("60043")
    assert renamed is not None
    assert renamed.comment == "manual"
    assert (renamed.size, renamed.lighting) == ("250", "Light")
    assert [item.file_id for item in renamed.media] == ["photo-file-id"]
    with pytest.raises(ValueError, match="вже існує"):
        repository.rename_group("60043", "60043")
    repository.close()


def test_group_listing_is_sorted_and_group_deletion_removes_media(tmp_path):
    repository = MediaRepository(tmp_path / "list.sqlite3")
    for group_id in ("60020", "60003", "60011"):
        repository.create_group(group_id)
    repository.add_media("60011", "video", "video-file-id")
    assert [row["group_id"] for row in repository.list_groups()] == ["60003", "60011", "60020"]
    repository.delete_group("60011")
    assert repository.get_group("60011") is None
    assert repository.connection.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 0
    repository.close()


def test_roles_and_owner_cannot_be_removed(tmp_path):
    repository = MediaRepository(tmp_path / "roles.sqlite3")
    repository.add_user(1, ROLE_OWNER, "Owner")
    repository.add_user(2, ROLE_ADMIN, "Admin")
    repository.add_user(3, ROLE_EDITOR, "Editor")
    repository.add_user(4, ROLE_VIEWER, "Viewer")
    assert [repository.get_user_role(i) for i in range(1, 5)] == [
        ROLE_OWNER, ROLE_ADMIN, ROLE_EDITOR, ROLE_VIEWER
    ]
    repository.remove_user(1)
    assert repository.get_user_role(1) == ROLE_OWNER
    repository.remove_user(2)
    assert repository.get_user_role(2) is None
    repository.close()


def test_role_update_preserves_saved_names_and_metadata(tmp_path):
    repository = MediaRepository(tmp_path / "user-update.sqlite3")
    repository.add_user(10, ROLE_VIEWER, "Олена", "olena", added_by=1)
    repository.add_user(10, ROLE_EDITOR, added_by=2, updated_by=2)
    user = repository.get_user_record(10)
    assert user is not None
    assert user["role"] == ROLE_EDITOR
    assert user["display_name"] == "Олена"
    assert user["username"] == "olena"
    assert user["added_by"] == 1
    assert user["updated_by"] == 2
    repository.close()


def test_role_keyboards_are_scoped_and_scenario_keyboard_contains_navigation():
    owner = {button.text for row in keyboard_for(ROLE_OWNER).keyboard for button in row}
    viewer = {button.text for row in keyboard_for(ROLE_VIEWER).keyboard for button in row}
    scenario = {button.text for row in scenario_keyboard("250", "500").keyboard for button in row}
    assert "Керувати користувачами" in owner
    assert "Редагувати осмос" in owner
    assert "Керувати користувачами" not in viewer
    assert "Створити осмос" not in viewer
    assert scenario == {"250", "500", "Назад", "Головне меню"}


def test_user_action_keyboard_has_no_group_management():
    buttons = {
        button.text
        for row in user_actions_keyboard(123, allow_manage=True).inline_keyboard
        for button in row
    }
    assert buttons == {"Змінити роль", "Видалити користувача", "До списку"}


def test_creation_type_choices_are_explicit_buttons():
    assert CREATE_SIZES == ("250", "500", "750")
    assert CREATE_LIGHTING == ("Full", "Light")


def test_osmos_list_is_sorted_and_paginated_by_ten(tmp_path):
    repository = MediaRepository(tmp_path / "pages.sqlite3")
    for number in reversed(range(12)):
        repository.create_group(f"60{number:03d}")

    page_one = group_page_keyboard(repository, 0)
    page_two = group_page_keyboard(repository, 1)
    first_ids = [row[0].text for row in page_one.inline_keyboard[:10]]
    second_ids = [row[0].text for row in page_two.inline_keyboard[:2]]
    assert first_ids == [f"60{number:03d}" for number in range(10)]
    assert second_ids == ["60010", "60011"]
    assert page_one.inline_keyboard[-1][0].callback_data == "osmos_page:1"
    assert page_two.inline_keyboard[-1][0].callback_data == "osmos_page:0"
    assert "сторінка 1/2" in group_page_text(repository, 0)
    repository.close()


def test_existing_database_is_migrated_with_new_metadata_columns(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE groups (
            group_id TEXT PRIMARY KEY,
            comment TEXT,
            created_at TEXT NOT NULL
        );
        INSERT INTO groups VALUES ('60001', 'legacy comment', '2025-01-01T00:00:00+00:00');
        CREATE TABLE media (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id TEXT NOT NULL REFERENCES groups(group_id) ON DELETE CASCADE,
            media_type TEXT NOT NULL,
            file_id TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE users (
            user_id INTEGER PRIMARY KEY,
            role INTEGER NOT NULL,
            display_name TEXT,
            username TEXT,
            created_at TEXT NOT NULL
        );
        INSERT INTO users VALUES (10, 4, 'Legacy user', '', '2025-01-01T00:00:00+00:00');
        """
    )
    connection.commit()
    connection.close()

    repository = MediaRepository(path)
    group = repository.get_group("60001")
    user = repository.get_user_record(10)
    assert group is not None and group.comment == "legacy comment"
    assert user is not None and user["display_name"] == "Legacy user"
    assert user["updated_at"] == user["created_at"]
    repository.close()
