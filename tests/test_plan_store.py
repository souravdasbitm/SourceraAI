import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from memory.plan_store import PlanStore, entry_usable


class PlanStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = PlanStore("saucedemo_usecases", root=self.tmp.name, app_version="1")

    def tearDown(self):
        self.tmp.cleanup()

    def test_put_get_failure_invalidate_list_clear(self):
        self.store.put("k", {"plan": [{"action": "click", "selector": "#ok"}], "failure_count": 0})
        self.assertEqual(self.store.get("k")["plan"][0]["selector"], "#ok")
        self.assertIsNone(self.store.get("missing"))
        self.store.record_failure("k")
        self.store.record_failure("missing")
        self.assertEqual(self.store.get("k")["failure_count"], 1)
        self.assertEqual(len(self.store.list()), 1)
        self.store.invalidate("k")
        self.assertIsNone(self.store.get("k"))
        self.store.put("other", {"plan": []})
        self.store.clear()
        self.assertEqual(self.store.list(), [])

    def test_app_version_change_drops_entries(self):
        self.store.put("k", {"plan": [{"action": "goto", "url": "https://example.test"}]})
        reopened = PlanStore("saucedemo_usecases", root=self.tmp.name, app_version="2")
        self.assertEqual(reopened.list(), [])
        self.assertEqual(reopened.get("k"), None)

    def test_file_is_json(self):
        self.store.put("k", {"plan": [], "source": "llm"})
        text = self.store.path.read_text(encoding="utf-8")
        self.assertIn("\n", text)
        self.assertIn('"entries"', text)

    def test_entry_usable_respects_failures_and_ttl(self):
        now = datetime.now(timezone.utc)
        fresh = {"failure_count": 0, "last_pass_at": now.isoformat(timespec="seconds")}
        tired = {"failure_count": 3, "last_pass_at": now.isoformat(timespec="seconds")}
        stale = {
            "failure_count": 0,
            "last_pass_at": (now - timedelta(days=31)).isoformat(timespec="seconds"),
        }
        self.assertTrue(entry_usable(fresh, max_failures=3, ttl_days=30, now=now))
        self.assertFalse(entry_usable(tired, max_failures=3, ttl_days=30, now=now))
        self.assertFalse(entry_usable(stale, max_failures=3, ttl_days=30, now=now))
        self.assertTrue(entry_usable(stale, max_failures=3, ttl_days=0, now=now))
        self.assertFalse(entry_usable(None, max_failures=3, ttl_days=0, now=now))


if __name__ == "__main__":
    unittest.main()
