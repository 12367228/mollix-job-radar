from .base import SourceAdapter
from .freelance_ru import FreelanceRuAdapter
from .weblancer import WeblancerAdapter
from .fl_ru import FlRuAdapter
from .habr_freelance import HabrFreelanceAdapter
from .telegram_public import TelegramPublicAdapter
from ..config import Settings


def configured_sources(settings: Settings) -> list[SourceAdapter]:
    sources = [FreelanceRuAdapter(), HabrFreelanceAdapter(), FlRuAdapter(),
               TelegramPublicAdapter(settings.telegram_public_channels), WeblancerAdapter()]
    for source in sources:
        source.enabled = getattr(settings, source.name + "_enabled")
    return sources


def enabled_sources(*, weblancer_enabled: bool = False) -> list[SourceAdapter]:
    return [source for source in configured_sources(Settings(weblancer_enabled=weblancer_enabled)) if source.enabled]
