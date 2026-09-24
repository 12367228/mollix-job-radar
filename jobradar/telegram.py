from datetime import datetime
import html
import json
import logging
import re
import time

import httpx

from .config import Settings
from .http import USER_AGENT, create_http_client, retry_delay
from .logging_utils import event, telegram_network_error
from .models import Project
from .normalization import MSK
from .reply_templates import generate_reply, recommended_price
from .scoring import Assessment

SOURCE_LABELS = {"freelance_ru": "Freelance.ru", "weblancer": "Weblancer", "fl_ru": "FL.ru",
                 "habr_freelance": "Хабр Фриланс", "telegram_public": "Telegram — публичный пост"}


def truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit - 1].rstrip() + "…"


def display_money(value) -> str:
    return f"{value:,.2f}".rstrip("0").rstrip(".").replace(",", " ")


def format_budget(project: Project) -> str:
    low, high = project.budget_min, project.budget_max
    currency = project.currency or "валюта не указана"
    if low is None and high is None:
        return "не указан"
    if low == high:
        return f"{display_money(low)} {currency}"
    if low is None:
        return f"до {display_money(high)} {currency}"
    if high is None:
        return f"от {display_money(low)} {currency}"
    return f"{display_money(low)}–{display_money(high)} {currency}"


def format_message(project: Project, assessment: Assessment) -> str:
    if not project.direct_url:
        raise ValueError("missing_direct_project_url")
    esc = html.escape
    heading = "🔥 БРАТЬ" if assessment.score >= 8 else "🟡 ПОСМОТРЕТЬ"
    date = project.published_at.astimezone(MSK).strftime("%d.%m.%Y %H:%M МСК") if project.published_at else "неизвестно"
    if project.source == "weblancer" and project.published_at:
        date = project.published_at.astimezone(MSK).strftime("%d.%m.%Y (время не указано)")
    positives = [r for r in assessment.reasons if r.points > 0 and r.code != "base"][:6]
    cautions = [r for r in assessment.reasons if r.points < 0 or r.code in {"unknown_date", "foreign_budget", "above_target", "future_date"}][:4]
    lines = [
        f"<b>{heading} — {assessment.score}/10</b>", "",
        f"<b>{esc(truncate(project.title, 180))}</b>", "",
        f"💰 Бюджет: {esc(format_budget(project))}", f"🕒 Опубликовано: {date}",
        f"🌐 Источник: {esc(SOURCE_LABELS.get(project.source, project.source))}",
        f"ID: {esc(project.source_id)} | Decision: {assessment.label}",
        f"STACK_COMPATIBILITY: {esc(assessment.stack_compatibility)}",
        f"Возраст: {assessment.age_hours:.2f} ч" if assessment.age_hours is not None else "Возраст: неизвестен",
        f"👥 Откликов: {project.responses_count if project.responses_count is not None else 'неизвестно'}", "",
        esc(truncate(project.description or "Описание отсутствует.", 500)), "", "<b>Почему подходит:</b>",
        *[f"• {esc(truncate(r.text, 140))} ({r.points:+d})" for r in positives],
    ]
    if cautions:
        lines.extend(["", "<b>Что проверить:</b>", *[f"• {esc(truncate(r.text, 120))} ({r.points:+d})" for r in cautions]])
    lines.extend(["", "<b>Risk flags:</b> " + esc(", ".join(assessment.risk_flags) or "нет")])
    lines.extend([
        "", f"Сложность: {assessment.complexity}/10 (предварительно)", "", "<b>Рекомендуемая цена:</b>",
        esc(recommended_price(project, assessment)), "", "<b>ГОТОВЫЙ ОТКЛИК:</b>",
        esc(generate_reply(project)), "", f'<a href="{esc(project.direct_url, quote=True)}">🔗 Открыть заказ</a>',
    ])
    message = "\n".join(lines)
    # Telegram counts text after parsing entities. Bound text conservatively in UTF-16 units.
    from bs4 import BeautifulSoup
    visible = BeautifulSoup(message, "html.parser").get_text()
    if len(visible.encode("utf-16-le")) // 2 > 4096:
        raise ValueError("telegram_message_too_long")
    return message


class TelegramError(RuntimeError):
    def __init__(self, code: str, *, uncertain: bool = False):
        super().__init__(code)
        self.uncertain = uncertain


NO_CHAT_UPDATES = "Откройте бота в Telegram, нажмите Start или отправьте сообщение и повторите команду."


def _telegram_url(token: str, method: str) -> str:
    if method not in {"getMe", "getUpdates", "sendMessage"}:
        raise TelegramError("telegram_unsupported_method")
    return f"https://api.telegram.org/bot{token}/{method}"


def _get_telegram_result(settings: Settings, method: str, *, client: httpx.Client | None = None, clock=time.monotonic):
    settings.require_telegram_token()
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    owns_client = client is None
    client = client if client is not None else create_http_client()
    try:
        client.cookies.clear()
        # No offset (including negative offsets) and no allowed_updates: preserve the queue/filter.
        with client.stream("GET", _telegram_url(settings.telegram_token, method),
                           params={"limit": 100, "timeout": 0} if method == "getUpdates" else None, follow_redirects=False,
                           headers={"User-Agent": USER_AGENT}) as response:
            if response.status_code != 200:
                event("telegram_http_error", telegram_endpoint=method, http_status=response.status_code)
            if response.status_code in (401, 404):
                raise TelegramError("Проверьте TELEGRAM_BOT_TOKEN в локальном .env: Telegram отклонил запрос.")
            if response.status_code == 409:
                raise TelegramError("getUpdates недоступен: проверьте, не использует ли бот webhook или другой polling-процесс. Webhook не изменён.")
            if response.status_code == 429:
                raise TelegramError("Telegram ограничил частоту запросов. Повторите команду позже.")
            if response.status_code != 200:
                raise TelegramError("Не удалось получить сообщения Telegram. Повторите команду позже.")
            chunks, size, started = [], 0, clock()
            for chunk in response.iter_bytes(chunk_size=8192):
                size += len(chunk)
                if size > 1024 * 1024 or clock() - started > 45:
                    raise TelegramError("Ответ Telegram превысил лимит размера или времени чтения.")
                chunks.append(chunk)
            try:
                data = json.loads(b"".join(chunks))
            except (ValueError, UnicodeError):
                raise TelegramError("Telegram вернул некорректный ответ.") from None
            if not isinstance(data, dict) or data.get("ok") is not True:
                event("telegram_api_error", telegram_endpoint=method, http_status=response.status_code)
                raise TelegramError("Telegram вернул некорректный ответ.")
            return data.get("result")
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError) as error:
        telegram_network_error(method, error, token=settings.telegram_token, chat_id=settings.telegram_chat_id)
        raise TelegramError("telegram_connection_failed") from None
    except httpx.RequestError as error:
        telegram_network_error(method, error, token=settings.telegram_token, chat_id=settings.telegram_chat_id)
        raise TelegramError("telegram_request_failed") from None
    finally:
        if owns_client:
            client.close()


def get_me(settings: Settings, *, client: httpx.Client | None = None, clock=time.monotonic) -> dict:
    """Read-only API identity check; the caller decides which fields may be displayed."""
    result = _get_telegram_result(settings, "getMe", client=client, clock=clock)
    if not isinstance(result, dict) or type(result.get("id")) is not int or result.get("is_bot") is not True:
        raise TelegramError("telegram_invalid_response")
    return result


def get_recent_chats(settings: Settings, *, client: httpx.Client | None = None, clock=time.monotonic) -> list[dict]:
    """Return only safe chat metadata; do not acknowledge or discard updates."""
    result = _get_telegram_result(settings, "getUpdates", client=client, clock=clock)
    if not isinstance(result, list):
        raise TelegramError("Telegram вернул некорректный ответ.")
    return _chats_from_updates(result)


def _chats_from_updates(updates: list) -> list[dict]:
    candidates = []
    for update in updates:
        if not isinstance(update, dict):
            continue
        update_id = update.get("update_id", 0)
        update_id = update_id if type(update_id) is int else 0
        for kind in ("message", "edited_message", "channel_post", "edited_channel_post", "business_message", "edited_business_message"):
            message = update.get(kind)
            if not isinstance(message, dict) or not isinstance(message.get("chat"), dict):
                continue
            chat = message["chat"]
            chat_id, chat_type = chat.get("id"), chat.get("type")
            if type(chat_id) is not int or chat_id == 0 or abs(chat_id) >= 2 ** 63:
                continue
            if chat_type not in ("private", "group", "supergroup", "channel"):
                continue
            item = {"chat_id": chat_id, "type": chat_type}
            username = chat.get("username")
            # Only Telegram username characters; never echo arbitrary server-supplied text.
            if isinstance(username, str) and re.fullmatch(r"[A-Za-z0-9_]{1,32}", username):
                item["username"] = username
            date = message.get("edit_date", message.get("date", 0))
            date = date if type(date) is int else 0
            candidates.append((date, update_id, item))
    chats = {}
    for _, _, item in sorted(candidates, key=lambda candidate: candidate[:2], reverse=True):
        chats.setdefault(item["chat_id"], item)
    return list(chats.values())


class TelegramClient:
    def __init__(self, settings: Settings, *, client: httpx.Client | None = None, sleep=time.sleep, clock=time.monotonic):
        settings.require_telegram()
        self._token, self._chat_id = settings.telegram_token, settings.telegram_chat_id
        for name in ("httpx", "httpcore"):
            logging.getLogger(name).setLevel(logging.CRITICAL)
        self.client = client if client is not None else create_http_client()
        self.owns_client, self.sleep = client is None, sleep
        self.clock = clock

    def close(self) -> None:
        if self.owns_client:
            self.client.close()

    def send_message(self, message: str) -> None:
        # Do not retry ambiguous POST failures: sendMessage has no idempotency key.
        endpoint = _telegram_url(self._token, "sendMessage")
        for attempt in range(3):
            self.client.cookies.clear()
            try:
                with self.client.stream("POST", endpoint, follow_redirects=False, headers={"User-Agent": USER_AGENT}, json={
                    "chat_id": self._chat_id, "text": message, "parse_mode": "HTML",
                    "link_preview_options": {"is_disabled": True},
                }) as response:
                    if response.status_code != 200:
                        event("telegram_http_error", telegram_endpoint="sendMessage", http_status=response.status_code)
                    chunks, total, started = [], 0, self.clock()
                    for chunk in response.iter_bytes(chunk_size=8192):
                        total += len(chunk)
                        if total > 65536:
                            raise TelegramError("telegram_response_too_large", uncertain=True)
                        if self.clock() - started > 45:
                            raise TelegramError("telegram_response_deadline", uncertain=True)
                        chunks.append(chunk)
                    try:
                        data = json.loads(b"".join(chunks))
                        if not isinstance(data, dict):
                            raise ValueError
                    except (ValueError, UnicodeError):
                        raise TelegramError("telegram_invalid_response", uncertain=True) from None
                    if data.get("ok") is False:
                        event("telegram_api_error", telegram_endpoint="sendMessage", http_status=response.status_code)
                    if response.status_code == 200 and data.get("ok") is True:
                        result = data.get("result")
                        if not isinstance(result, dict) or not isinstance(result.get("message_id"), int):
                            raise TelegramError("telegram_missing_confirmation", uncertain=True)
                        return
                    if data.get("ok") is False and (response.status_code == 429 or data.get("error_code") == 429):
                        parameters = data.get("parameters") or {}
                        value = parameters.get("retry_after") if isinstance(parameters, dict) else None
                        delay = retry_delay(str(value) if value is not None else response.headers.get("Retry-After"), 2 ** attempt)
                        if attempt < 2 and delay <= 30:
                            self.sleep(delay)
                            continue
                        raise TelegramError("telegram_rate_limited")
                    # A Telegram error explicitly says no message was delivered.
                    if data.get("ok") is False and response.status_code < 500:
                        raise TelegramError("telegram_rejected")
                    raise TelegramError("telegram_delivery_uncertain", uncertain=True)
            except (httpx.ConnectError, httpx.ConnectTimeout) as error:
                telegram_network_error("sendMessage", error, token=self._token, chat_id=self._chat_id)
                if attempt == 2:
                    raise TelegramError("telegram_connection_failed") from None
                self.sleep(2 ** attempt)
            except httpx.RequestError as error:
                telegram_network_error("sendMessage", error, token=self._token, chat_id=self._chat_id)
                raise TelegramError("telegram_delivery_uncertain", uncertain=True) from None
        raise TelegramError("telegram_failed")
