"""Atomic JSON state and a process lock, without a database."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
from hashlib import sha256

from .deduplication import fingerprint, normalized_url
from .models import Project
from .scoring import Assessment


class StateError(RuntimeError):
    pass


@contextmanager
def state_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise StateError("state_locked: another run or a stale lock exists") from None
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(str(os.getpid()))
        yield
    finally:
        lock_path.unlink(missing_ok=True)


class StateStore:
    def __init__(self, path: Path, *, limit: int = 10000):
        if limit < 1:
            raise StateError("invalid_history_limit")
        self.path, self.limit = Path(path), limit
        self.entries: dict[str, dict] = {}
        self.fingerprints: dict[str, dict] = {}
        self._snapshot = self._serialized()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            if self.path.stat().st_size > 256 * 1024 * 1024:
                raise ValueError("state_too_large")
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") not in {1, 2} or not isinstance(data.get("entries"), dict):
                raise ValueError("invalid_state_schema")
            for key, item in data["entries"].items():
                if not isinstance(item, dict) or key != f"{item['source']}:{item['source_id']}":
                    raise ValueError("invalid_state_entry")
                if item.get("status") not in {"pending", "sent"}:
                    raise ValueError("invalid_state_status")
                timestamp = item.get("sent_at") if item["status"] == "sent" else item.get("reserved_at")
                if not isinstance(timestamp, str) or datetime.fromisoformat(timestamp).tzinfo is None:
                    raise ValueError("invalid_state_timestamp")
            self.entries = data["entries"]
            if data["version"] == 1:
                # Migration is in memory only. A dry-run never rewrites real state.
                for key, entry in self.entries.items():
                    prefixes = {"fl_ru": "https://fl.ru/projects/", "freelance_ru": "https://freelance.ru/task/view/",
                                "habr_freelance": "https://freelance.habr.com/tasks/"}
                    prefix = prefixes.get(entry["source"])
                    url = prefix + entry["source_id"] if prefix and entry["source_id"].isdigit() else None
                    fp = sha256(("legacy:" + key).encode()).hexdigest()
                    self.fingerprints[fp] = {
                        "source": entry["source"], "source_id": entry["source_id"], "normalized_url": url,
                        "first_seen_at": entry.get("reserved_at") or entry["sent_at"],
                        "sent_at": entry.get("sent_at"), "status": entry["status"],
                    }
            else:
                if not isinstance(data.get("fingerprints"), dict):
                    raise ValueError("missing_fingerprint_history")
                self.fingerprints = data["fingerprints"]
            for fp, item in self.fingerprints.items():
                if not re.fullmatch(r"[a-f0-9]{64}", fp) or not isinstance(item, dict):
                    raise ValueError("invalid_fingerprint")
                if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", item["source"]) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", item["source_id"]):
                    raise ValueError("invalid_history_identity")
                if item["status"] not in {"seen", "pending", "sent"}:
                    raise ValueError("invalid_history_status")
                if datetime.fromisoformat(item["first_seen_at"]).tzinfo is None:
                    raise ValueError("invalid_history_timestamp")
                if item["status"] == "sent":
                    if datetime.fromisoformat(item["sent_at"]).tzinfo is None:
                        raise ValueError("invalid_sent_timestamp")
                elif item["sent_at"] is not None:
                    raise ValueError("unexpected_sent_timestamp")
                if item["normalized_url"] is not None and normalized_url(item["normalized_url"]) != item["normalized_url"]:
                    raise ValueError("invalid_history_url")
            for key, entry in self.entries.items():
                if not any(f"{h['source']}:{h['source_id']}" == key and h["status"] == entry["status"]
                           and h["sent_at"] == entry.get("sent_at") for h in self.fingerprints.values()):
                    raise ValueError("inconsistent_delivery_history")
            self._snapshot = self._serialized()
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            # Fail closed: silently starting from scratch would duplicate all messages.
            raise StateError("state_unreadable_or_invalid: restore state before sending") from None

    def contains(self, project: Project) -> bool:
        return project.key in self.entries or any(item["status"] in {"pending", "sent"} for _, item in self._matching(project))

    def _matching(self, project: Project):
        fp = fingerprint(project)
        urls = {normalized_url(project.url)}
        if project.direct_url:
            urls.add(normalized_url(project.direct_url))
        for key, item in self.fingerprints.items():
            if (key == fp or f"{item['source']}:{item['source_id']}" == project.key
                    or item["normalized_url"] in urls):
                yield key, item

    def observe(self, project: Project, *, now: datetime) -> dict:
        """Remember first observation during run only; check never calls this."""
        existing = list(self._matching(project))
        if existing:
            return existing[0][1]
        record = {
            "source": project.source, "source_id": project.source_id,
            "normalized_url": normalized_url(project.direct_url or project.url),
            "first_seen_at": now.astimezone(timezone.utc).isoformat(), "sent_at": None, "status": "seen",
        }
        self.fingerprints[fingerprint(project)] = record
        return record

    def _prune_for_reservation(self) -> None:
        remove_count = len(self.entries) - self.limit + 1
        if remove_count <= 0:
            return
        sent = sorted((item["sent_at"], key) for key, item in self.entries.items() if item["status"] == "sent")
        if len(sent) < remove_count:
            raise StateError("too_many_pending_deliveries: reconcile uncertain sends")
        for _, key in sent[:remove_count]:
            del self.entries[key]

    def reserve(self, project: Project, assessment: Assessment, *, now: datetime) -> bool:
        if self.contains(project):
            return False
        self._prune_for_reservation()
        history = self.observe(project, now=now)
        # An unsent observation may have come from another source. Keep its first_seen_at.
        history.update(source=project.source, source_id=project.source_id, status="pending")
        self.entries[project.key] = {
            "source": project.source, "source_id": project.source_id,
            "status": "pending", "reserved_at": now.astimezone(timezone.utc).isoformat(), "sent_at": None,
            "assessment": assessment.to_dict(),
        }
        self.save()
        return True

    def mark_sent(self, project: Project, *, now: datetime) -> None:
        entry = self.entries[project.key]
        if entry["status"] == "sent":
            return
        entry["status"] = "sent"
        entry["sent_at"] = now.astimezone(timezone.utc).isoformat()
        for _, item in self._matching(project):
            item.update(status="sent", sent_at=entry["sent_at"])
        self.save()

    def release(self, project: Project) -> None:
        """Only after a confirmed rejection; never after an ambiguous timeout."""
        if self.entries.get(project.key, {}).get("status") == "pending":
            del self.entries[project.key]
            for _, item in self._matching(project):
                if item["status"] == "pending":
                    item["status"] = "seen"
            self.save()

    def _serialized(self) -> str:
        # Only verbose delivery entries are bounded. Compact identities never expire.
        return json.dumps({"version": 2, "entries": self.entries, "fingerprints": self.fingerprints},
                          ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    def save(self) -> bool:
        content = self._serialized()
        if content == self._snapshot:
            return False
        temp_path: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=self.path.parent, suffix=".tmp", delete=False) as file:
                temp_path = Path(file.name)
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp_path, self.path)
            self._snapshot = content
            return True
        except OSError:
            raise StateError("state_save_failed: sending stopped") from None
        finally:
            if temp_path:
                temp_path.unlink(missing_ok=True)
