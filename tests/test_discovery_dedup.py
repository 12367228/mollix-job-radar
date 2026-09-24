from dataclasses import replace
from datetime import timedelta

import pytest

from jobradar.deduplication import deduplicate_projects, fingerprint, normalized_title, normalized_url
from jobradar.config import Settings
from jobradar.runner import execute
from jobradar.state import StateStore


def repost(project, **overrides):
    return replace(project, source="telegram_public", source_id="test_channel_20", channel="test_channel",
                   url="https://t.me/test_channel/20", external_url=project.url + "?utm_source=tg",
                   origin_kind="repost", **overrides)


def test_fingerprint_uses_normalized_title_and_original_url(project):
    original = replace(project, title="Разработать API: Python и React")
    copy = repost(original, title="РАЗРАБОТАТЬ API — Python и React")
    assert normalized_title(original.title) == normalized_title(copy.title)
    assert fingerprint(original) == fingerprint(copy)
    assert normalized_url("https://www.fl.ru/projects/123/title.html?utm_source=tg#text") == "https://fl.ru/projects/123"


@pytest.mark.parametrize("reverse", [False, True])
def test_marketplace_wins_even_when_repost_title_changed(project, reverse):
    copy = repost(project, title="Ищем разработчика для готового Python бота")
    batch = [copy, project] if reverse else [project, copy]
    assert deduplicate_projects(batch) == [project]


def test_direct_post_beats_repost_without_inventing_customer_authorship(project):
    original = replace(project, source="telegram_public", source_id="client_channel_1", channel="client_channel",
                       url="https://t.me/client_channel/1", origin_kind="direct_post")
    copy = repost(original)
    assert deduplicate_projects([copy, original]) == [original]


def test_title_fallback_when_copy_has_no_external_url(project):
    copy = replace(repost(project), external_url=None)
    assert deduplicate_projects([copy, project]) == [project]


def test_different_marketplace_orders_with_same_title_are_not_merged(project):
    other = replace(project, source_id="456", url="https://freelance.ru/task/view/456")
    assert len(deduplicate_projects([project, other])) == 2
    copy = replace(repost(project), external_url=None)
    assert len(deduplicate_projects([project, other, copy])) == 3


def test_short_generic_titles_without_links_are_not_merged(project):
    first = replace(project, title="Python", source="telegram_public", source_id="channel_one_1",
                    url="https://t.me/channel_one/1", origin_kind="direct_post")
    second = replace(first, source_id="channel_two_2", url="https://t.me/channel_two/2")
    assert len(deduplicate_projects([first, second])) == 2


def test_unknown_external_service_url_does_not_merge_unrelated_titles(project):
    first = replace(repost(project), external_url="https://example.invalid/service")
    second = replace(first, source_id="other_channel_1", url="https://t.me/other_channel/1", title="Другой заказ React API")
    assert len(deduplicate_projects([first, second])) == 2


def test_original_date_is_not_renewed_by_repost_and_state_is_unchanged(project, now, tmp_path):
    old = replace(project, published_at=now - timedelta(days=3))
    copy = repost(old, published_at=now)
    class Source:
        def __init__(self, name, rows):
            self.name, self.rows = name, rows
        def fetch(self, http, *, now):
            return self.rows
    path = tmp_path / "state.json"
    path.write_text('{"version":1,"entries":{}}', encoding="utf-8")
    before = path.read_bytes()
    result = execute(Settings(), [Source("telegram_public", [copy]), Source("freelance_ru", [old])],
                     object(), StateStore(path), now=now)
    assert result.fetched == 2 and result.checked == 1 and result.fresh == 0
    assert result.eligible == result.sent == 0
    assert result.candidates[0][0] == old and path.read_bytes() == before
    assert result.source_metrics["telegram_public"]["fresh_count"] == 1
