from jobradar.config import ConfigError, Settings
import pytest


def test_config_repr_hides_both_credentials():
    assert "sensitive" not in repr(Settings(telegram_token="sensitive", telegram_chat_id="sensitive"))


@pytest.mark.parametrize("chat", ["", "@channel", "-100123456", "not-a-number"])
def test_private_owner_chat_only(chat):
    with pytest.raises(ConfigError):
        Settings(telegram_token="0" * 8 + ":" + "x" * 35, telegram_chat_id=chat).require_telegram()


def test_invalid_options_and_environment_precedence(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("JOBRADAR_MAX_SEND=3\n", encoding="utf-8")
    monkeypatch.setenv("JOBRADAR_MAX_SEND", "4")
    assert Settings.from_env().max_send == 4
    monkeypatch.setenv("JOBRADAR_MAX_SEND", "-1")
    with pytest.raises(ConfigError):
        Settings.from_env()


def test_does_not_load_parent_env(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=parent-secret\n", encoding="utf-8")
    child = tmp_path / "child"
    child.mkdir()
    monkeypatch.chdir(child)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert Settings.from_env().telegram_token == ""


def test_chat_id_loaded_from_dotenv_without_echoing_credentials(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.setenv(name, "temporary-test-value")
        monkeypatch.delenv(name)
    token = "0" * 8 + ":" + "x" * 35
    (tmp_path / ".env").write_text(f'TELEGRAM_BOT_TOKEN="{token}"\nTELEGRAM_CHAT_ID=" 987654321 "\n', encoding="utf-8-sig")
    config = Settings.from_env()
    config.require_telegram()
    assert config.telegram_chat_id == "987654321"
    assert config.telegram_token == token
    output = capsys.readouterr()
    assert output.out == output.err == ""
    assert token not in repr(config) and config.telegram_chat_id not in repr(config)


def test_radar_defaults_and_dotenv_settings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in ("MAX_PROJECT_AGE_HOURS", "HOT_PROJECT_AGE_HOURS", "WEBLANCER_ENABLED"):
        monkeypatch.setenv(key, "temporary")
        monkeypatch.delenv(key)
    monkeypatch.setenv("JOBRADAR_MAX_AGE_DAYS", "7")  # obsolete setting must not restore seven days
    defaults = Settings.from_env()
    assert (defaults.max_project_age_hours, defaults.hot_project_age_hours, defaults.weblancer_enabled) == (24, 6, False)
    (tmp_path / ".env").write_text("MAX_PROJECT_AGE_HOURS=12\nHOT_PROJECT_AGE_HOURS=3\nWEBLANCER_ENABLED=true\n", encoding="utf-8")
    configured = Settings.from_env()
    assert (configured.max_project_age_hours, configured.hot_project_age_hours, configured.weblancer_enabled) == (12, 3, True)


@pytest.mark.parametrize("key,value", [("MAX_PROJECT_AGE_HOURS", "0"), ("HOT_PROJECT_AGE_HOURS", "25"),
                                      ("HOT_PROJECT_AGE_HOURS", "0"), ("WEBLANCER_ENABLED", "maybe")])
def test_invalid_radar_config_fails_safely(tmp_path, monkeypatch, key, value):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MAX_PROJECT_AGE_HOURS", "24")
    monkeypatch.setenv("HOT_PROJECT_AGE_HOURS", "6")
    monkeypatch.setenv("WEBLANCER_ENABLED", "false")
    monkeypatch.setenv(key, value)
    with pytest.raises(ConfigError):
        Settings.from_env()
