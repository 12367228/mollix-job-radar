from dataclasses import replace
from datetime import timedelta
import json

import pytest

from jobradar.scoring import score_project
from jobradar.state import StateError, StateStore, state_lock


def test_dedup_persists_source_and_id(tmp_path, project, now):
    path = tmp_path / "state.json"
    state = StateStore(path)
    score = score_project(project, now=now)
    assert state.reserve(project, score, now=now)
    assert not state.reserve(project, score, now=now)
    state.mark_sent(project, now=now)
    loaded = StateStore(path)
    assert loaded.contains(project)
    assert loaded.contains(replace(project, source="weblancer"))  # Same canonical card URL.
    item = loaded.entries[project.key]
    assert item["sent_at"] == now.isoformat()
    assert item["assessment"]["reasons"]


def test_pending_is_not_resent_and_definitive_failure_can_release(tmp_path, project, now):
    state = StateStore(tmp_path / "state.json")
    state.reserve(project, score_project(project, now=now), now=now)
    loaded = StateStore(state.path)
    assert loaded.contains(project)
    assert loaded.entries[project.key]["sent_at"] is None
    loaded.release(project)
    assert not StateStore(state.path).contains(project)


def test_no_rewrite_when_unchanged(tmp_path, project, now):
    state = StateStore(tmp_path / "state.json")
    state.reserve(project, score_project(project, now=now), now=now)
    before = state.path.stat().st_mtime_ns
    assert state.save() is False
    assert state.path.stat().st_mtime_ns == before


def test_bounded_history_keeps_pending_and_newest_sent(tmp_path, project, now):
    state = StateStore(tmp_path / "state.json", limit=3)
    score = score_project(project, now=now)
    state.reserve(project, score, now=now)
    for i in range(1, 5):
        p = replace(project, source_id=str(i), url=f"https://freelance.ru/task/view/{i}")
        state.reserve(p, score, now=now + timedelta(seconds=i))
        state.mark_sent(p, now=now + timedelta(seconds=i))
    assert set(state.entries) == {project.key, "freelance_ru:3", "freelance_ru:4"}


@pytest.mark.parametrize("content", ["{broken", "[]", '{"version": 2, "entries": {}}', '{"version":1,"entries":{"x":{}}}'])
def test_corrupt_state_does_not_reset(tmp_path, content):
    path = tmp_path / "state.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(StateError):
        StateStore(path)
    assert path.read_text(encoding="utf-8") == content


def test_atomic_save_failure_keeps_previous_file(tmp_path, project, now, monkeypatch):
    import jobradar.state as module
    state = StateStore(tmp_path / "state.json")
    state.reserve(project, score_project(project, now=now), now=now)
    before = state.path.read_bytes()
    def fail(*args):
        raise OSError("disk failed")
    monkeypatch.setattr(module.os, "replace", fail)
    with pytest.raises(StateError):
        state.mark_sent(project, now=now)
    assert state.path.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))


def test_process_lock_is_exclusive_and_released(tmp_path):
    path = tmp_path / "state.json"
    with state_lock(path):
        with pytest.raises(StateError):
            with state_lock(path):
                pass
    with state_lock(path):
        pass


def test_pending_overflow_fails_instead_of_forgetting_uncertain_delivery(tmp_path, project, now):
    state = StateStore(tmp_path / "state.json", limit=1)
    score = score_project(project, now=now)
    state.reserve(project, score, now=now)
    with pytest.raises(StateError):
        state.reserve(replace(project, source_id="456", url="https://freelance.ru/task/view/456"), score, now=now)
