import pytest

from app.database import MediaRepository, validate_group_id


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
    group_id = repository.create_group("відпустка")
    assert group_id.startswith("60")
    repository.add_media(group_id, "photo", "photo-file-id")
    repository.add_media(group_id, "video", "video-file-id")
    repository.set_comment(group_id, "оновлений коментар")
    group = repository.get_group(group_id)
    assert group is not None
    assert group.comment == "оновлений коментар"
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
