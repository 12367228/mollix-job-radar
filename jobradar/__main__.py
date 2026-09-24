import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

from .config import ConfigError, Settings
from .http import PublicHTTPClient
from .logging_utils import configure_logging, event
from .runner import candidate_order, execute
from .sources import configured_sources
from .state import StateError, StateStore, state_lock
from .telegram import NO_CHAT_UPDATES, TelegramClient, TelegramError, format_budget, format_message, get_recent_chats


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Mollix Job Radar — публичные фриланс-заказы")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="Проверить и оценить заказы без отправки и записи state")
    check.add_argument("--fixtures", type=Path, help="Офлайн-проверка на sanitised HTML fixtures")
    check.add_argument("--now", type=str, help="Фиксированное ISO-время только для --fixtures")
    check.add_argument("--json", action="store_true", help="Полный результат и причины в JSON")
    check.add_argument("--verbose", action="store_true", help="Все проекты, включая все SKIP (по умолчанию не более пяти SKIP)")
    check.add_argument("--preview", action="store_true", help="Показать HTML Telegram-уведомлений для подходящих заказов")
    views = check.add_mutually_exclusive_group()
    views.add_argument("--top", action="store_true", help="Только FIRE/GOOD: до 10, score DESC / age ASC / responses ASC")
    views.add_argument("--watchlist", action="store_true", help="Диагностика: GOOD 6 и STACK_MISMATCH, без отправки")
    commands.add_parser("run", help="Отправить владельцу новые подходящие заказы")
    commands.add_parser("test-telegram", help="Отправить одно тестовое сообщение владельцу")
    commands.add_parser("telegram-chat-id", help="Показать ID, тип и username чатов из последних сообщений боту")
    return parser


def check_candidates(result, *, verbose: bool = False, top: bool = False, watchlist: bool = False):
    if watchlist:
        yield from sorted((item for item in result.candidates if
                           (item[1].label == "GOOD" and item[1].score == 6)
                           or (item[1].scored and "STACK_MISMATCH" in item[1].risk_flags)), key=candidate_order)
        return
    if top:
        yield from sorted((item for item in result.candidates if item[1].eligible), key=candidate_order)[:10]
        return
    skipped = 0
    for candidate in result.candidates:
        if candidate[1].label == "SKIP":
            if not verbose and skipped >= 5:
                continue
            skipped += 1
        yield candidate


def print_candidate(project, assessment, duplicate: bool) -> None:
    score = f"{assessment.score}/10" if assessment.scored else "— (scoring не выполнялся)"
    age = "неизвестен" if assessment.age_hours is None else f"{assessment.age_hours:.1f} ч"
    if assessment.hot:
        age += " (HOT)"
    print(f"SOURCE: {project.source} | RELEVANCE: {'PASS' if assessment.relevance_gate else 'SKIP'} | SCORE: {score} | DECISION: {assessment.label}")
    print(f"TITLE: {project.title}" + (" [уже в state]" if duplicate else ""))
    print(f"SOURCE_ID: {project.source_id}")
    print(f"DIRECT_URL: {project.direct_url or 'не подтверждён'}")
    print(f"PUBLISHED_AT: {project.published_at.isoformat() if project.published_at else 'неизвестно'}")
    print(f"PUBLICATION_CONFIRMED: {str(project.publication_confirmed).lower()}")
    print(f"PUBLICATION_EVIDENCE: {project.publication_evidence or 'нет'}")
    print(f"AGE_HOURS: {assessment.age_hours:.6f}" if assessment.age_hours is not None else "AGE_HOURS: неизвестно")
    print(f"OPEN_STATUS: {project.open_status} | STATUS_EVIDENCE: {project.status_evidence or 'нет'}")
    if project.channel:
        print(f"CHANNEL: {project.channel}")
    print(f"BUDGET: {format_budget(project)} | AGE: {age} | RESPONSES: {project.responses_count if project.responses_count is not None else 'неизвестно'}")
    print(f"RESPONSES_COUNT: {project.responses_count if project.responses_count is not None else 'неизвестно'}")
    print(f"STACK_COMPATIBILITY: {assessment.stack_compatibility}")
    print(f"STACK_DETAILS: {', '.join(assessment.stack_details) or 'неизвестно'}")
    print(f"STACK_REASON: {assessment.stack_reason}")
    print(f"TELEGRAM_DELIVERABLE: {str(assessment.telegram_deliverable and not duplicate).lower()}")
    print(f"DELIVERY_BLOCKERS: {', '.join(assessment.delivery_blockers) or 'нет'}")
    print(f"COMPLEXITY: {assessment.complexity}/10" if assessment.scored else "COMPLEXITY: не оценивалась")
    print(f"REASONS: {assessment.relevance_reason}")
    for reason in assessment.reasons:
        print(f"  {reason.points:+d} {reason.text}")
    print("RISK FLAGS: " + (", ".join(assessment.risk_flags) or "нет") + "\n")


def print_source_summary(result) -> None:
    print("SOURCE SUMMARY")
    for source, stats in result.source_metrics.items():
        if stats["status"] == "disabled":
            print(f"{source:17} disabled")
            continue
        error = result.source_error_codes.get(source)
        print(f"{source:17} fetched={stats['fetched']} relevant={stats['relevant_count']} "
              f"fresh={stats['fresh_count']} eligible={stats['eligible_count']} "
              f"parse_errors={stats['parse_errors']} status={stats['status']}" + (f" error={error}" if error else ""))
    summary = result.summary()
    print(" ".join(f"{key}={summary[key]}" for key in ("total_fetched", "unique", "fresh", "relevance_pass", "eligible", "FIRE", "GOOD",
                                                       "telegram_deliverable", "filtered_by_competition", "filtered_by_stack", "filtered_by_open_status")))
    print()


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    configure_logging()
    args = build_parser().parse_args(argv)
    try:
        settings = Settings.from_env()
        if args.command == "telegram-chat-id":
            chats = get_recent_chats(settings)
            if not chats:
                print(NO_CHAT_UPDATES)
                return 0
            for chat in chats:
                print(json.dumps(chat, ensure_ascii=False))
            return 0
        if args.command != "check":
            settings.require_telegram()
        with ExitStack() as stack:
            telegram = None
            if args.command != "check":
                telegram = TelegramClient(settings)
                stack.callback(telegram.close)
            if args.command == "test-telegram":
                telegram.send_message("✅ <b>Mollix Job Radar</b>\nТестовое сообщение. Telegram подключён.")
                event("telegram_test_ok")
                return 0
            fixtures = getattr(args, "fixtures", None)
            now = None
            if getattr(args, "now", None):
                if not fixtures:
                    raise ConfigError("--now is only available with --fixtures")
                try:
                    now = datetime.fromisoformat(args.now.replace("Z", "+00:00"))
                    if now.tzinfo is None:
                        raise ValueError
                except ValueError:
                    raise ConfigError("--now requires an ISO datetime with timezone") from None
            http = None
            if not fixtures:
                http = PublicHTTPClient()
                stack.callback(http.close)
            if args.command == "run":
                stack.enter_context(state_lock(settings.state_path))
            state = StateStore(settings.state_path, limit=settings.history_limit)
            result = execute(settings, configured_sources(settings), http, state,
                             telegram=telegram, fixtures=fixtures, now=now)
            if args.command == "check":
                if args.json:
                    print(json.dumps({"summary": result.summary(), "projects": [
                        {"project": project.to_dict(), "assessment": assessment.to_dict(), "already_seen": duplicate}
                        for project, assessment, duplicate in (check_candidates(result, top=args.top, watchlist=args.watchlist)
                                                               if args.top or args.watchlist else result.candidates)
                    ]}, ensure_ascii=False, indent=2))
                else:
                    print_source_summary(result)
                    for project, assessment, duplicate in check_candidates(result, verbose=args.verbose, top=args.top, watchlist=args.watchlist):
                        print_candidate(project, assessment, duplicate)
                        if args.preview and assessment.eligible:
                            print("\n" + format_message(project, assessment) + "\n")
                    print(json.dumps(result.summary(), ensure_ascii=False))
            return result.exit_code
    except (ConfigError, StateError, TelegramError) as error:
        event("fatal", code=str(error))
        return 2
    except KeyboardInterrupt:
        event("interrupted")
        return 130
    except Exception:
        # Deliberately no traceback: URL-bearing network exceptions can contain credentials.
        event("fatal", code="unexpected_error")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
