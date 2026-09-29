"""追加式事件存储。

业务事实只追加、不改写：每个聚合有单调递增的 version，全局有单调递增的
seq（接收顺序）。持久化为单个 JSONL 文件，服务重启后通过重放恢复全部状态。
"""
from __future__ import annotations

import json
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any

from src.envelope import validate_event


class ConcurrencyError(RuntimeError):
    """聚合版本与预期不符（已被其他写入推进）。"""


class EventStore:
    def __init__(self, path: str | Path | None, allowed_events: set[str]) -> None:
        self._path = Path(path) if path else None
        self._allowed_events = set(allowed_events)
        self._lock = threading.RLock()
        self._events: list[dict[str, Any]] = []
        self._versions: dict[str, int] = defaultdict(int)
        self._event_ids: set[str] = set()
        if self._path is not None and self._path.exists():
            self._load()

    @property
    def lock(self) -> threading.RLock:
        """服务在一次命令内需要读取投影再追加时，持有此锁保证原子性。"""
        return self._lock

    def _load(self) -> None:
        assert self._path is not None
        with self._path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                event = json.loads(line)
                self._verify(event, require_seq=True)
                self._events.append(event)
                self._versions[event["aggregate_id"]] += 1
                self._event_ids.add(event["event_id"])
        self._events.sort(key=lambda e: e["seq"])

    def _verify(self, event: dict[str, Any], *, require_seq: bool = False) -> None:
        errors = validate_event(event, self._allowed_events)
        if errors:
            raise ValueError("; ".join(errors))
        if require_seq:
            if "seq" not in event or not isinstance(event["seq"], int) or event["seq"] < 1:
                raise ValueError("seq 必须是正整数")

    def append(self, event: dict[str, Any], *, expected_version: int | None = None) -> dict[str, Any]:
        """追加一条事件；version 必须等于该聚合当前版本加一。"""
        with self._lock:
            self._verify(event)
            aggregate_id = event["aggregate_id"]
            current = self._versions[aggregate_id]
            if event["version"] != current + 1:
                raise ConcurrencyError(
                    f"聚合 {aggregate_id} 版本冲突：期望 {current + 1}，收到 {event['version']}"
                )
            if expected_version is not None and expected_version != current:
                raise ConcurrencyError(
                    f"聚合 {aggregate_id} 预期基准版本 {expected_version}，实际 {current}"
                )
            if event["event_id"] in self._event_ids:
                raise ValueError(f"event_id 重复: {event['event_id']}")
            event = dict(event)
            event["seq"] = len(self._events) + 1
            if self._path is not None:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(event, ensure_ascii=False) + "\n")
                    fh.flush()
            self._events.append(event)
            self._versions[aggregate_id] = event["version"]
            self._event_ids.add(event["event_id"])
            return event

    def events(self, aggregate_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if aggregate_id is None:
                return list(self._events)
            return [e for e in self._events if e["aggregate_id"] == aggregate_id]

    def all_events(self) -> list[dict[str, Any]]:
        return self.events()

    def version_of(self, aggregate_id: str) -> int:
        with self._lock:
            return self._versions[aggregate_id]

    def next_version(self, aggregate_id: str) -> int:
        return self.version_of(aggregate_id) + 1

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)
