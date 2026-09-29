"""事件存储持久化与并发测试。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.eventstore import ConcurrencyError, EventStore

ROOT = Path(__file__).resolve().parents[1]
EVENTS = set(json.loads((ROOT / "contracts" / "domain.json").read_text(encoding="utf-8"))["events"])


def _event(etype="AGREEMENT_DRAFTED", aggregate_id="agreement_version-x", version=1, **payload):
    return {
        "event_id": f"evt-{etype}-{aggregate_id}-{version}-{len(payload)}",
        "event_type": etype,
        "occurred_at": "2026-09-29T10:00:00+08:00",
        "aggregate_id": aggregate_id,
        "version": version,
        "payload": payload or {"note": "x"},
    }


class EventStoreTest(unittest.TestCase):
    def test_append_assigns_monotonic_seq_and_version(self) -> None:
        store = EventStore(None, EVENTS)
        e1 = store.append(_event(version=1))
        e2 = store.append(_event(version=2))
        self.assertEqual((e1["seq"], e2["seq"]), (1, 2))
        self.assertEqual(store.version_of("agreement_version-x"), 2)

    def test_version_gap_is_rejected(self) -> None:
        store = EventStore(None, EVENTS)
        store.append(_event(version=1))
        with self.assertRaises(ConcurrencyError):
            store.append(_event(version=3))

    def test_duplicate_event_id_is_rejected(self) -> None:
        store = EventStore(None, EVENTS)
        first = _event(version=1)
        store.append(first)
        # 同一 event_id 即使挂到别的聚合也拒绝（幂等键全局唯一）
        clone = dict(first, aggregate_id="agreement_version-y", version=1)
        with self.assertRaises(ValueError):
            store.append(clone)

    def test_persistence_and_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "log.jsonl"
            s1 = EventStore(path, EVENTS)
            s1.append(_event(version=1))
            s1.append(_event(version=2))
            s1.append(_event(aggregate_id="agreement_version-z",
                             etype="AGREEMENT_ACTIVATED", version=1))
            s2 = EventStore(path, EVENTS)
            self.assertEqual(len(s2), 3)
            self.assertEqual(s2.version_of("agreement_version-x"), 2)
            self.assertEqual([e["seq"] for e in s2.all_events()], [1, 2, 3])

    def test_events_are_filterable_per_aggregate(self) -> None:
        store = EventStore(None, EVENTS)
        store.append(_event(version=1))
        store.append(_event(aggregate_id="agreement_version-z",
                            etype="AGREEMENT_ACTIVATED", version=1))
        self.assertEqual(len(store.events("agreement_version-z")), 1)


if __name__ == "__main__":
    unittest.main()
