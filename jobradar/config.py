from dataclasses import dataclass, field
import os
from pathlib import Path
import re

from dotenv import load_dotenv

DEFAULT_TELEGRAM_CHANNELS = ("freelansim_ru", "digitaltender", "job_for_bots", "freelancetaverna")


class ConfigError(ValueError):
    pass


def _integer(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        raise ConfigError(f"{name}: expected an integer") from None
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name}: expected {minimum}..{maximum}")
    return value


def _boolean(name: str, default: bool) -> bool:
    value = os.getenv(name, str(default)).strip().lower()
    if value not in {"true", "false"}:
        raise ConfigError(f"{name}: expected true or false")
    return value == "true"


def _channels() -> tuple[str, ...]:
    names = tuple(dict.fromkeys(n.strip().lower() for n in os.getenv(
        "TELEGRAM_PUBLIC_CHANNELS", ",".join(DEFAULT_TELEGRAM_CHANNELS)).split(",") if n.strip()))
    if not 1 <= len(names) <= 20 or any(not re.fullmatch(r"[a-z][a-z0-9_]{4,31}", n) for n in names):
        raise ConfigError("TELEGRAM_PUBLIC_CHANNELS: expected 1..20 public channel usernames without URLs")
    return names


@dataclass(frozen=True)
class Settings:
    telegram_token: str = field(default="", repr=False)
    telegram_chat_id: str = field(default="", repr=False)
    state_path: Path = Path("data/state.json")
    max_send: int = 10
    history_limit: int = 10000
    max_project_age_hours: int = 24
    hot_project_age_hours: int = 6
    telegram_min_score: int = 7
    telegram_max_responses: int = 20
    telegram_allow_stack_mismatch: bool = False
    freelance_ru_enabled: bool = True
    habr_freelance_enabled: bool = True
    fl_ru_enabled: bool = True
    telegram_public_enabled: bool = True
    telegram_public_channels: tuple[str, ...] = DEFAULT_TELEGRAM_CHANNELS
    weblancer_enabled: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        # Do not search parent directories for unrelated credentials.
        load_dotenv(Path.cwd() / ".env", override=False, encoding="utf-8-sig")
        max_age = _integer("MAX_PROJECT_AGE_HOURS", 24, 1, 720)
        return cls(
            telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
            state_path=Path(os.getenv("JOBRADAR_STATE_PATH", "data/state.json")),
            max_send=_integer("JOBRADAR_MAX_SEND", 10, 1, 50),
            history_limit=_integer("JOBRADAR_HISTORY_LIMIT", 10000, 100, 50000),
            max_project_age_hours=max_age,
            hot_project_age_hours=_integer("HOT_PROJECT_AGE_HOURS", 6, 1, max_age),
            telegram_min_score=_integer("TELEGRAM_MIN_SCORE", 7, 6, 10),
            telegram_max_responses=_integer("TELEGRAM_MAX_RESPONSES", 20, 0, 10000),
            telegram_allow_stack_mismatch=_boolean("TELEGRAM_ALLOW_STACK_MISMATCH", False),
            freelance_ru_enabled=_boolean("FREELANCE_RU_ENABLED", True),
            habr_freelance_enabled=_boolean("HABR_FREELANCE_ENABLED", True),
            fl_ru_enabled=_boolean("FL_RU_ENABLED", True),
            telegram_public_enabled=_boolean("TELEGRAM_PUBLIC_ENABLED", True),
            telegram_public_channels=_channels(),
            weblancer_enabled=_boolean("WEBLANCER_ENABLED", False),
        )

    def require_telegram_token(self) -> None:
        if not re.fullmatch(r"\d{5,20}:[A-Za-z0-9_-]{20,100}", self.telegram_token):
            raise ConfigError("Set a valid TELEGRAM_BOT_TOKEN in .env or GitHub Secrets")

    def require_telegram(self) -> None:
        self.require_telegram_token()
        # One private owner chat, not a channel or a group.
        if not re.fullmatch(r"[1-9]\d{0,19}", self.telegram_chat_id):
            raise ConfigError("TELEGRAM_CHAT_ID must be the positive numeric ID of your private chat")
