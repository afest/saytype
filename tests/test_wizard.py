"""Тесты мастера первого запуска (T-263).

GUI здесь не поднимается — проверяется то, что решает, увидит человек мастер
или нет. Ошибка в этом месте стоит дорого в обе стороны: не показать мастер
новичку значит бросить его перед пустым окном, а показать его тому, кто
пользуется приложением полгода, — навязаться с давно принятыми решениями.
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def fresh_profile(tmp_path, monkeypatch):
    """Чистый профиль: ни настроек, ни следов прежних версий."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    import importlib

    from saytype import profile

    importlib.reload(profile)
    window = importlib.import_module("saytype.transcribe_ui_window")
    importlib.reload(window)
    return window


def test_wizard_shown_on_clean_profile(fresh_profile):
    """Новый пользователь: мастер должен открыться."""
    settings = fresh_profile.load_settings_dict()
    assert settings["wizard_done"] is False


def test_wizard_skipped_for_existing_user(fresh_profile):
    """У того, кто уже настроил приложение, мастер не всплывает после обновления.

    Признак «уже пользовался» — тот же, что для выбора модели в T-259: файл
    настроек существует и в нём есть hotkey.
    """
    window = fresh_profile
    s = window.get_settings()
    s.setValue("hotkey", "<ctrl>+<shift>+q")
    s.sync()
    assert window.SETTINGS_FILE.exists()

    settings = window.load_settings_dict()

    assert settings["wizard_done"] is True, "существующему пользователю мастер не нужен"
    # И решение запоминается, чтобы не зависеть от эвристики в следующий раз
    assert window.get_settings().contains("wizard_done")


def test_wizard_flag_survives_round_trip(fresh_profile):
    """Пройденный мастер остаётся пройденным после сохранения настроек."""
    window = fresh_profile
    settings = window.load_settings_dict()
    settings["wizard_done"] = True
    settings["mic_device"] = "Микрофон (DJI MIC MINI)"
    window.save_settings_dict(settings)

    again = window.load_settings_dict()
    assert again["wizard_done"] is True
    assert again["mic_device"] == "Микрофон (DJI MIC MINI)"


def test_mic_device_defaults_to_system(fresh_profile):
    """Пустое значение = системный микрофон, как было до появления выбора."""
    settings = fresh_profile.load_settings_dict()
    assert settings["mic_device"] == ""


def test_input_devices_are_deduplicated():
    """Список для человека: микрофоны, а не хост-API одного устройства."""
    from saytype import wizard

    devices = wizard.input_devices()
    names = [d["name"] for d in devices]
    assert len(names) == len(set(names)), "одно имя не должно повторяться"
    assert all(d["index"] >= 0 for d in devices)


def test_system_language_is_supported_code():
    from saytype import wizard

    assert wizard.system_language() in ("ru", "en")
