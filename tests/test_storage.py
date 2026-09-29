"""事件存储：append-only 约束、聚合版本单调、JSONL 重放一致。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.storage import InMemoryEventStore, JsonlEventStore


def _event(aggregate_id: str, version: int, eid: str = "e") -> dict:
    return {
        "event_id": eid,
        "event_type": "OBLIGATION_ACCEPTED",
        "occurred_at": "2026-09-20T09:00:00+08:00",
        "aggregate_id": aggregate_id,
        "version": version,
        "payload": {},
    }


class InMemoryStoreTest(unittest.TestCase):
    def test_version_must_be_monotonic_per_aggregate(self) -> None:
        store = InMemoryEventStore()
        store.append(_event("a", 1, "e1"))
        with self.assertRaisesRegex(ValueError, "version 冲突"):
            store.append(_event("a", 1, "e2"))
        store.append(_event("a", 2, "e3"))
        # 不同聚合各自计数
        store.append(_event("b", 1, "e4"))
        self.assertEqual(store.next_version("a"), 3)
        self.assertEqual(store.next_version("b"), 2)
        self.assertEqual(store.next_version("c"), 1)

    def test_event_id_unique(self) -> None:
        store = InMemoryEventStore()
        store.append(_event("a", 1, "dup"))
        with self.assertRaisesRegex(ValueError, "事件ID重复"):
            store.append(_event("b", 1, "dup"))

    def test_events_are_not_mutated_through_iteration(self) -> None:
        store = InMemoryEventStore()
        store.append(_event("a", 1, "e1"))
        for event in store.iter_events():
            event["payload"] = {"tampered": True}
        self.assertEqual(
            list(store.iter_events())[0]["payload"], {})


class JsonlStoreTest(unittest.TestCase):
    def test_round_trip_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.jsonl"
            store = JsonlEventStore(path)
            store.append(_event("a", 1, "e1"))
            store.append(_event("a", 2, "e2"))
            store.append(_event("b", 1, "e3"))
            # 新实例从同一日志重建，状态一致
            replayed = JsonlEventStore(path)
            self.assertEqual(len(replayed), 3)
            original = [json.dumps(e, sort_keys=True) for e in store.iter_events()]
            loaded = [json.dumps(e, sort_keys=True) for e in replayed.iter_events()]
            self.assertEqual(original, loaded)

    def test_corrupted_sequence_detected_on_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.jsonl"
            path.write_text(
                json.dumps(_event("a", 1, "e1")) + "\n"
                + json.dumps(_event("a", 3, "e3")) + "\n",
                encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "version 不连续"):
                JsonlEventStore(path)

    def test_appending_keeps_existing_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.jsonl"
            JsonlEventStore(path).append(_event("a", 1, "e1"))
            store = JsonlEventStore(path)
            store.append(_event("b", 1, "e2"))
            lines = path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 2)


if __name__ == "__main__":
    unittest.main()
