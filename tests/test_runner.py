from dataclasses import replace
import json

import pytest

from jobradar.config import Settings
from jobradar.http import SourceError
from jobradar.runner import execute
from jobradar.state import StateError, StateStore
from jobradar.telegram import TelegramError


class StaticSource:
    name = "fixture_source"

    def __init__(self, projects):
        self.projects = projects

    def fetch(self, client, *, now):
        return self.projects


class BrokenSource:
    name = "broken_source"

    def fetch(self, client, *, now):
        raise SourceError("unrecognized_listing_layout")


class Sender:
    def __init__(self, error=None):
        self.messages = []
        self.error = error

    def send_message(self, message):
        if self.error:
            raise self.error
        self.messages.append(message)


def test_broken_source_does_not_stop_other_source(tmp_path, project, now):
    sender = Sender()
    result = execute(Settings(), [BrokenSource(), StaticSource([project])], object(), StateStore(tmp_path / "state.json"), telegram=sender, now=now, sleep=lambda _: None)
    assert result.sent == 1 and len(sender.messages) == 1
    assert result.source_errors == ["broken_source"]
    assert result.exit_code == 0  # Source errors are nonfatal, but remain visible.
    assert result.source_error_codes == {"broken_source": "unrecognized_listing_layout"}


def test_duplicate_within_batch_and_next_run_not_sent(tmp_path, project, now):
    path = tmp_path / "state.json"
    source = StaticSource([project, project])
    sender = Sender()
    result = execute(Settings(), [source], object(), StateStore(path), telegram=sender, now=now, sleep=lambda _: None)
    assert result.sent == 1
    second = execute(Settings(), [source], object(), StateStore(path), telegram=sender, now=now, sleep=lambda _: None)
    assert second.sent == 0 and second.duplicates == 1 and len(sender.messages) == 1


def test_dry_run_does_not_modify_state(tmp_path, project, now):
    path = tmp_path / "state.json"
    result = execute(Settings(), [StaticSource([project])], object(), StateStore(path), now=now)
    assert result.eligible == 1 and result.sent == 0 and not path.exists()


@pytest.mark.parametrize("uncertain", [True, False])
def test_telegram_error_preserves_only_uncertain_reservation(tmp_path, project, now, uncertain):
    path = tmp_path / "state.json"
    result = execute(Settings(), [StaticSource([project])], object(), StateStore(path), telegram=Sender(TelegramError("failure", uncertain=uncertain)), now=now)
    assert result.sent == 0 and result.delivery_error == "failure"
    assert StateStore(path).contains(project) == uncertain


def test_uncertain_post_is_not_resent_on_next_run(tmp_path, project, now):
    path = tmp_path / "state.json"
    source = StaticSource([project])
    execute(Settings(), [source], object(), StateStore(path), telegram=Sender(TelegramError("timeout", uncertain=True)), now=now)
    sender = Sender()
    execute(Settings(), [source], object(), StateStore(path), telegram=sender, now=now)
    assert sender.messages == []


def test_send_limit_does_not_mark_unsent_as_seen(tmp_path, project, now):
    state = StateStore(tmp_path / "state.json")
    result = execute(Settings(max_send=1), [StaticSource([project, replace(project, source_id="456", url="https://freelance.ru/task/view/456")])], object(), state, telegram=Sender(), now=now, sleep=lambda _: None)
    assert result.sent == 1 and len(state.entries) == 1


def test_reservation_save_failure_prevents_send(tmp_path, project, now, monkeypatch):
    state = StateStore(tmp_path / "state.json")
    def fail():
        raise StateError("state_save_failed")
    monkeypatch.setattr(state, "save", fail)
    sender = Sender()
    with pytest.raises(StateError):
        execute(Settings(), [StaticSource([project])], object(), state, telegram=sender, now=now)
    assert sender.messages == []


def test_cli_offline_check_needs_no_secrets_and_writes_nothing(tmp_path, fixture_dir, monkeypatch, capsys):
    from jobradar.__main__ import main
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    for key in ("HABR_FREELANCE_ENABLED", "FL_RU_ENABLED", "TELEGRAM_PUBLIC_ENABLED"):
        monkeypatch.setenv(key, "false")
    code = main(["check", "--fixtures", str(fixture_dir), "--now", "2026-09-21T12:00:00Z", "--json"])
    output = json.loads(capsys.readouterr().out)
    assert code == 0 and output["summary"]["checked"] == 3
    assert output["summary"]["eligible"] == 1
    assert output["summary"]["relevance_pass"] == 2
    assert set(output["summary"]["source_counts"]) == {"freelance_ru"}
    assert list(tmp_path.iterdir()) == []


def test_cli_missing_credentials_fails_before_network(tmp_path, monkeypatch, capsys):
    from jobradar.__main__ import main
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert main(["run"]) == 2
    assert list(tmp_path.iterdir()) == []
