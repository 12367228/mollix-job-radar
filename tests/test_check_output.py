from dataclasses import replace
from datetime import datetime, timezone
import json

import pytest

from jobradar.__main__ import main
from jobradar.config import Settings
from jobradar.http import SourceError
from jobradar.runner import execute
from jobradar.state import StateStore


class StaticSource:
    name = "freelance_ru"

    def __init__(self, projects):
        self.projects = projects

    def fetch(self, client, *, now):
        return self.projects


@pytest.mark.parametrize("flags,skips", [([], 5), (["--verbose"], 14), (["--json"], 14)])
def test_check_output_limits_and_metadata(tmp_path, project, monkeypatch, capsys, flags, skips):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WEBLANCER_ENABLED", "false")
    good = replace(project, published_at=datetime.now(timezone.utc))
    irrelevant = [replace(good, source_id=str(i + 10), url=f"https://freelance.ru/task/view/{i + 10}", title=f"PowerPoint слайды {i}", description=None) for i in range(12)]
    close = [replace(good, source_id=str(i + 50), url=f"https://freelance.ru/task/view/{i + 50}", title="Python", description=None,
                     published_at=None, responses_count=None, budget_min=None, budget_max=None) for i in range(2)]
    def enabled(settings):
        assert not settings.weblancer_enabled
        return [StaticSource(irrelevant + close + [good])]
    monkeypatch.setattr("jobradar.__main__.configured_sources", enabled)
    class NoNetwork:
        def close(self):
            pass
    monkeypatch.setattr("jobradar.__main__.PublicHTTPClient", NoNetwork)
    def forbidden(*args, **kwargs):
        raise AssertionError("check must never create a Telegram client")
    monkeypatch.setattr("jobradar.__main__.TelegramClient", forbidden)
    assert main(["check", *flags]) == 0
    out = capsys.readouterr().out
    if "--json" in flags:
        data = json.loads(out)
        assert len(data["projects"]) == skips + 1
        assert data["summary"]["relevance_pass"] == 3
        assert sum(not p["assessment"]["scored"] for p in data["projects"]) == 12
    else:
        assert out.startswith("SOURCE SUMMARY\n")
        assert out.count("DECISION: SKIP") == skips and out.count("DECISION: FIRE") == 1
        assert out.index("TITLE: Python") < out.index("TITLE: PowerPoint")
        for label in ("SOURCE:", "RELEVANCE:", "SCORE:", "DECISION:", "TITLE:", "URL:",
                      "BUDGET:", "AGE:", "RESPONSES:", "REASONS:", "RISK FLAGS:"):
            assert out.count(label) == skips + 1
    assert list(tmp_path.iterdir()) == []


def test_all_sources_failed_is_a_visible_nonfatal_warning(tmp_path, now):
    class Restricted:
        name = "weblancer"
        def fetch(self, client, *, now):
            raise SourceError("access_restricted")
    result = execute(Settings(), [Restricted()], object(), StateStore(tmp_path / "state.json"), now=now)
    assert result.exit_code == 0 and result.checked == result.sent == 0
    assert result.summary()["source_error_codes"] == {"weblancer": "access_restricted"}
