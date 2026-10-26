import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from orch.persistence import LockError, ProcessLock, StateError, StateStore
from orch.reporting import ReportStore, TaskLogger


TEST_ROOT = Path(__file__).resolve().parent


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()

    def test_state_atomic_round_trip_and_failed_replace_preserves_previous_state(self):
        store = StateStore(self.base)
        self.assertEqual(store.load(), {})
        store.save({"fase": "verifica", "testo": "città"})
        self.assertEqual(store.load()["testo"], "città")
        with patch("orch.persistence.os.replace", side_effect=OSError("disco pieno")):
            with self.assertRaises(OSError):
                store.save({"fase": "modello"})
        self.assertEqual(store.load()["fase"], "verifica")
        self.assertEqual(list(self.base.glob("*.tmp")), [])

    def test_corrupt_or_non_object_state_is_not_silently_replaced(self):
        store = StateStore(self.base)
        for text in ("{broken", "[]", "null"):
            with self.subTest(text=text):
                store.path.write_text(text, encoding="utf-8")
                with self.assertRaises(StateError):
                    store.load()
                self.assertEqual(store.path.read_text(encoding="utf-8"), text)

    def test_lock_writes_pid_rejects_live_owner_and_releases(self):
        with ProcessLock(self.base, alive=lambda pid: True):
            data = json.loads((self.base / "orchestratore.lock").read_text(encoding="utf-8"))
            self.assertEqual(data["pid"], os.getpid())
            with self.assertRaises(LockError):
                with ProcessLock(self.base, alive=lambda pid: True):
                    self.fail("Lock doppio")
        self.assertFalse((self.base / "orchestratore.lock").exists())

    def test_stale_pid_lock_is_replaced(self):
        path = self.base / "orchestratore.lock"
        path.write_text("12345", encoding="utf-8")
        with ProcessLock(self.base, alive=lambda pid: False):
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["pid"], os.getpid())
        self.assertFalse(path.exists())

    def test_invalid_lock_is_preserved_with_clear_error(self):
        path = self.base / "orchestratore.lock"
        path.write_text("non valido", encoding="utf-8")
        with self.assertRaisesRegex(LockError, "Lock non valido"):
            with ProcessLock(self.base):
                self.fail("Lock corrotto acquisito")
        self.assertEqual(path.read_text(encoding="utf-8"), "non valido")

    def event(self, event_id="one", outcome="in corso"):
        return {"event_id": event_id, "at": "2026-10-22T12:00:00+00:00", "tipo": "fase",
                "messaggio": "Messaggio", "run_id": "run-one", "task": {
                    "id": "001", "titolo": "Task", "esito": outcome, "pezzi_completati": 1,
                    "pezzi_totali": 2, "rimandi_totali": 2, "bocciature": 1,
                    "inizio": "2026-10-22T11:00:00+00:00", "fine": None,
                    "commit": "hash", "push": "push fallito", "stash": "stash@{0}",
                    "attese": [{"strumento": "codex", "inizio": "11:00", "fino_a": "12:00", "fine": None}]}}

    def test_report_is_deduplicated_and_regenerates_latest_task_snapshot(self):
        store = ReportStore(self.base)
        first = self.event()
        store.record(first)
        store.record(first)
        store.record(self.event("two", "fatta"))
        self.assertEqual(len(store.events("2026-10-22")), 2)
        raw_lines = (self.base / "report" / "eventi-2026-10-22.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len([json.loads(line) for line in raw_lines]), 2)
        path = store.regenerate("2026-10-22")
        text = path.read_text(encoding="utf-8")
        for expected in ("## 1. 001: Task", "Esito: fatta", "1/2", "Rimandi: 2", "correzioni del controllore: 1",
                         "stash@{0}", "push fallito", "Attesa limite codex"):
            self.assertIn(expected, text)

    def test_truncated_event_tail_does_not_lose_following_events(self):
        store = ReportStore(self.base)
        store.record(self.event())
        path = self.base / "report" / "eventi-2026-10-22.jsonl"
        with path.open("ab") as stream:
            stream.write(b'{"interrotto":"\xc3')
        store.record(self.event("two", "fatta"))
        self.assertEqual(len(store.events("2026-10-22")), 2)
        self.assertEqual(len([json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]), 2)

    def test_task_and_general_logs_keep_full_text_and_safe_filename(self):
        TaskLogger(self.base).write("2026-10-22T12:00:00+00:00", "Output completo\nseconda riga",
                                    task_id="../001", day="2026-10-22")
        paths = list((self.base / "log").glob("*.log"))
        self.assertEqual(len(paths), 2)
        self.assertTrue(all("seconda riga" in p.read_text(encoding="utf-8") for p in paths))
