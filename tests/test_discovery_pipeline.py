from dataclasses import replace
from datetime import timedelta

import pytest

from jobradar.config import ConfigError, Settings
from jobradar.http import SourceError
from jobradar.normalization import parse_publication_time
from jobradar.runner import execute
from jobradar.scoring import assess_project
from jobradar.sources import configured_sources
from jobradar.sources.fl_ru import FlRuAdapter
from jobradar.state import StateStore


@pytest.mark.parametrize("text,seconds", [
    ("2 часа 15 минут назад", 8100), ("7 минут назад", 420), ("1 час назад", 3600),
    ("30 секунд назад", 30), ("только что", 0), ("2026-09-21T11:00:00Z", 3600),
    ("21.09.2026 14:00", 3600), ("вчера", None), ("2026-09-21", None),
    ("3 дня назад", None), ("bad", None), (None, None),
])
def test_publication_time_requires_hour_precision(text, seconds, now):
    value = parse_publication_time(text, now)
    assert value == (now - timedelta(seconds=seconds) if seconds is not None else None)


@pytest.mark.parametrize("age", [None, -1, 24.001])
def test_unreliable_future_or_stale_date_is_never_eligible(project, now, age):
    a = assess_project(replace(project, published_at=None if age is None else now - timedelta(hours=age)), now=now)
    assert not a.eligible and a.label == "SKIP" and a.score <= 5


def test_source_metrics_on_all_offline_fixtures(fixture_dir, now, tmp_path):
    settings = Settings(telegram_public_channels=("freelansim_ru",))
    result = execute(settings, configured_sources(settings), None, StateStore(tmp_path / "state.json"), fixtures=fixture_dir, now=now)
    metrics = result.source_metrics
    assert result.exit_code == 0 and not result.source_errors
    assert metrics["freelance_ru"]["fetched"] == 3
    assert metrics["habr_freelance"]["fetched"] == 2
    assert metrics["fl_ru"]["fetched"] == 2
    assert metrics["telegram_public"]["fetched"] == 7
    assert metrics["weblancer"]["status"] == "disabled"
    assert result.fetched == 14 and result.checked == 13
    assert result.summary()["discovery_duplicates"] == 1
    assert metrics["telegram_public"]["relevant_count"] == 3
    assert metrics["telegram_public"]["eligible_count"] == 0
    assert metrics["fl_ru"]["fresh_count"] == 0  # Listing age awaits the original publication field.
    assert metrics["fl_ru"]["parse_errors"] == 1
    assert not (tmp_path / "state.json").exists()


def test_partial_page_failure_preserves_other_pages_and_deduplicates(fixture_dir, now, tmp_path):
    adapter = FlRuAdapter()
    html = (fixture_dir / "fl_ru.html").read_text(encoding="utf-8")
    class Public:
        def get_listing(self, url, hosts):
            if "saity" in url:
                return "<html><title>unexpected layout</title></html>"
            return html
    result = execute(Settings(), [adapter], Public(), StateStore(tmp_path / "state.json"), now=now)
    metrics = result.source_metrics["fl_ru"]
    assert result.exit_code == 0 and result.checked == 2
    assert metrics["status"] == "partial" and metrics["pages_fetched"] == 2
    assert metrics["parse_errors"] == 3  # two bad cards plus one unrecognized page
    assert len(metrics["page_errors"]) == 1 and result.source_errors == ["fl_ru"]


def test_access_restriction_does_not_try_other_pages(now):
    urls = []
    class Public:
        def get_listing(self, url, hosts):
            urls.append(url)
            raise SourceError("access_restricted")
    with pytest.raises(SourceError, match="access_restricted"):
        FlRuAdapter().fetch(Public(), now=now)
    assert len(urls) == 1


def test_source_config_and_channel_list(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for source in ("FREELANCE_RU", "HABR_FREELANCE", "FL_RU", "TELEGRAM_PUBLIC", "WEBLANCER"):
        monkeypatch.setenv(source + "_ENABLED", "false")
    monkeypatch.setenv("TELEGRAM_PUBLIC_CHANNELS", "test_channel, another_channel,TEST_CHANNEL")
    settings = Settings.from_env()
    assert settings.telegram_public_channels == ("test_channel", "another_channel")
    assert all(not s.enabled for s in configured_sources(settings))
    assert settings.max_project_age_hours == 24 and settings.hot_project_age_hours == 6


@pytest.mark.parametrize("value", ["", "https://t.me/test_channel", "../private", "@username", "a,b", "test_channel?before=1"])
def test_channel_config_rejects_non_username_input(tmp_path, monkeypatch, value):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_PUBLIC_CHANNELS", value)
    with pytest.raises(ConfigError, match="TELEGRAM_PUBLIC_CHANNELS"):
        Settings.from_env()
