"""append-only 事件存储：内存实现与 JSONL 文件实现。

事件只能追加，不能改写；每个聚合的 version 从 1 单调递增，用于确定性回放。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Iterator


class EventStore:
    def append(self, event: dict[str, Any]) -> None:
        raise NotImplementedError

    def iter_events(self) -> Iterator[dict[str, Any]]:
        raise NotImplementedError

    def next_version(self, aggregate_id: str) -> int:
        raise NotImplementedError


class InMemoryEventStore(EventStore):
    def __init__(self) -> None:
        self._events: list[dict[str, Any]] = []
        self._versions: dict[str, int] = {}
        self._event_ids: set[str] = set()
        self._lock = threading.RLock()

    def append(self, event: dict[str, Any]) -> None:
        with self._lock:
            if event["event_id"] in self._event_ids:
                raise ValueError(f"事件ID重复: {event['event_id']}")
            expected = self._versions.get(event["aggregate_id"], 0) + 1
            if event["version"] != expected:
                raise ValueError(_conflict_message(event, expected))
            self._versions[event["aggregate_id"]] = event["version"]
            self._event_ids.add(event["event_id"])
            self._events.append(dict(event))

    def iter_events(self) -> Iterator[dict[str, Any]]:
        with self._lock:
            return iter([dict(e) for e in self._events])

    def next_version(self, aggregate_id: str) -> int:
        with self._lock:
            return self._versions.get(aggregate_id, 0) + 1

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)


class JsonlEventStore(EventStore):
    """每行一个信封 JSON；启动时校验 version 单调并按记录顺序回放。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._events: list[dict[str, Any]] = []
        self._versions: dict[str, int] = {}
        self._event_ids: set[str] = set()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self._load()

    def _load(self) -> None:
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            event = json.loads(line)
            expected = self._versions.get(event["aggregate_id"], 0) + 1
            if event["version"] != expected:
                raise ValueError(
                    f"日志 {self.path.name} 中聚合 {event['aggregate_id']} "
                    f"version 不连续（期望 {expected}，收到 {event['version']}）"
                )
            if event["event_id"] in self._event_ids:
                raise ValueError(f"日志中事件ID重复: {event['event_id']}")
            self._versions[event["aggregate_id"]] = event["version"]
            self._event_ids.add(event["event_id"])
            self._events.append(event)

    def append(self, event: dict[str, Any]) -> None:
        with self._lock:
            expected = self._versions.get(event["aggregate_id"], 0) + 1
            if event["version"] != expected:
                raise ValueError(_conflict_message(event, expected))
            if event["event_id"] in self._event_ids:
                raise ValueError(f"事件ID重复: {event['event_id']}")
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
            self._versions[event["aggregate_id"]] = event["version"]
            self._event_ids.add(event["event_id"])
            self._events.append(dict(event))

    def iter_events(self) -> Iterator[dict[str, Any]]:
        with self._lock:
            return iter([dict(e) for e in self._events])

    def next_version(self, aggregate_id: str) -> int:
        with self._lock:
            return self._versions.get(aggregate_id, 0) + 1

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)


def _conflict_message(event: dict[str, Any], expected: int) -> str:
    return (
        f"聚合 {event['aggregate_id']} version 冲突: 期望 {expected}，"
        f"收到 {event['version']}"
    )
