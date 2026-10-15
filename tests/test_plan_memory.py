import tempfile
import unittest
from datetime import datetime, timezone

from memory.fingerprint import cache_key, page_fingerprint
from memory.normalize import normalize_step
from memory.plan_store import PlanStore
from memory.runner import run_with_memory, trim_planned_actions

PAGE_URL = "https://www.saucedemo.com/inventory.html"
PAGE_HTML = "<main><h1>Inventory</h1><form name=\"login\"></form></main>"


class FakePage:
    url = PAGE_URL

    def content(self):
        return PAGE_HTML


class FakeExecutor:
    def __init__(self, fail=None, heal_selector=None):
        self.fail = set(fail or [])
        self.heal_selector = heal_selector
        self.page = None
        self.captured = {}
        self.step_log = []
        self.heals = []
        self.events = []

    def start(self):
        self.page = FakePage()

    def stop(self):
        self.page = None

    def perform(self, step):
        self.events.append(("perform", step.get("selector")))
        if step.get("selector") in self.fail:
            raise RuntimeError(f"fail {step.get('selector')}")

    def heal(self, step, rejected=None):
        self.events.append(("heal", step.get("selector")))
        if not self.heal_selector or self.heal_selector in (rejected or []):
            return None
        self.heals.append({"old": step.get("selector"), "new": self.heal_selector, "reason": "fake"})
        return self.heal_selector


class FakeNim:
    model = "fake-model"


def _pass(_nim, _uc, _execution):
    return {"verdict": "PASS", "rules": [], "summary": "ok"}


class PlanMemoryFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = PlanStore("demo", root=self.tmp.name, app_version="")
        self.nim = FakeNim()

    def tearDown(self):
        self.tmp.cleanup()

    def test_second_run_does_not_call_the_planner(self):
        calls = []

        def plan_fn(_nim, uc):
            text = uc["steps"][0]
            calls.append(text)
            if "extract" in text.lower():
                return {"test_name": "T", "steps": [
                    {"action": "extract", "selector": ".title", "all": True, "save_as": "product_titles"}
                ]}
            return {"test_name": "T", "steps": [{"action": "click", "selector": "#login-button"}]}

        uc = {
            "name": "SD_login",
            "objective": "see products",
            "steps": [
                "Click the Login button",
                "Extract the visible product titles",
            ],
        }
        validates = []

        def validate(nim, use_case, execution):
            validates.append(execution["execution_status"])
            return _pass(nim, use_case, execution)

        first = run_with_memory(
            uc, self.nim, FakeExecutor(), self.store,
            plan_fn=plan_fn, validate_fn=validate, log=lambda *_: None, write_report=False,
        )
        self.assertEqual(calls, uc["steps"])
        self.assertEqual(first["memory"]["llm_calls_made"], 2)
        self.assertEqual(first["memory"]["llm_calls_avoided"], 0)

        second = run_with_memory(
            uc, self.nim, FakeExecutor(), self.store,
            plan_fn=plan_fn, validate_fn=validate, log=lambda *_: None, write_report=False,
        )
        self.assertEqual(calls, uc["steps"])
        self.assertEqual(second["memory"]["llm_calls_made"], 0)
        self.assertEqual(second["memory"]["llm_calls_avoided"], 2)
        self.assertEqual(
            [ev["event"] for ev in second["memory"]["events"]],
            ["CACHE_HIT", "CACHE_HIT"],
        )
        self.assertEqual(validates, ["completed", "completed"])

    def test_broken_locator_walks_the_failure_ladder_in_order(self):
        step = "Click the Login button"
        fingerprint = page_fingerprint(PAGE_URL, PAGE_HTML)
        key = cache_key(normalize_step(step), fingerprint)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.store.put(key, {
            "key": key,
            "step_text": step,
            "normalized_step": normalize_step(step),
            "page_fingerprint": fingerprint,
            "plan": [{
                "action": "click",
                "selector": "#broken",
                "fallback_selectors": ["#fb1", "#fb2"],
            }],
            "version": 1,
            "pass_count": 1,
            "failure_count": 0,
            "created_at": now,
            "last_pass_at": now,
            "source": "llm",
            "model": "fake-model",
        })
        timeline = []
        executor = FakeExecutor(fail={"#broken", "#fb1", "#fb2", "#healed"}, heal_selector="#healed")
        executor.events = timeline
        original = self.store.record_failure

        def record(key_):
            timeline.append("record_failure")
            return original(key_)

        self.store.record_failure = record

        def plan_fn(_nim, _uc):
            timeline.append("plan")
            return {"test_name": "T", "steps": [{"action": "click", "selector": "#fresh"}]}

        uc = {"name": "SD_login", "objective": "login", "steps": [step]}
        result = run_with_memory(
            uc, self.nim, executor, self.store,
            plan_fn=plan_fn, validate_fn=_pass, log=lambda *_: None, write_report=False,
        )
        self.assertEqual(timeline, [
            ("perform", "#broken"),
            "record_failure",
            ("perform", "#fb1"),
            ("perform", "#fb2"),
            "plan",
            ("perform", "#fresh"),
        ])
        self.assertEqual(result["memory"]["events"][0]["event"], "REPLANNED")
        saved = self.store.get(key)
        self.assertEqual(saved["source"], "replan")
        self.assertEqual(saved["version"], 2)
        self.assertEqual(saved["failure_count"], 0)
        self.assertEqual(saved["plan"][0]["selector"], "#fresh")

    def test_miss_heals_on_the_fly_and_a_cached_replay_does_not(self):
        step = "Click the Login button"
        plans = []

        def plan_fn(_nim, _uc):
            plans.append("plan")
            return {"test_name": "T", "steps": [{"action": "click", "selector": "#broken"}]}

        first = FakeExecutor(fail={"#broken"}, heal_selector="#ok")
        run_with_memory(
            {"name": "T", "objective": "", "steps": [step]},
            self.nim, first, self.store,
            plan_fn=plan_fn, validate_fn=_pass, log=lambda *_: None, write_report=False,
        )
        saved = self.store.list()[0]
        self.assertEqual(saved["source"], "healed")
        self.assertIn("#ok", saved["plan"][0]["fallback_selectors"])
        self.assertEqual(plans, ["plan"])
        self.assertEqual(first.events[0], ("perform", "#broken"))
        self.assertIn(("heal", "#broken"), first.events)

        second = FakeExecutor(fail={"#broken"}, heal_selector="#should-not-heal")
        result = run_with_memory(
            {"name": "T", "objective": "", "steps": [step]},
            self.nim, second, self.store,
            plan_fn=plan_fn, validate_fn=_pass, log=lambda *_: None, write_report=False,
        )
        self.assertEqual(plans, ["plan"])
        self.assertEqual(second.events, [("perform", "#broken"), ("perform", "#ok")])
        self.assertEqual(result["memory"]["events"][0]["event"], "HEALED_FROM_CACHE")
        self.assertEqual(result["memory"]["llm_calls_made"], 0)

    def test_failed_validation_does_not_cache_a_plan(self):
        calls = []

        def plan_fn(_nim, uc):
            calls.append(uc["steps"][0])
            return {"test_name": "T", "steps": [
                {"action": "fill", "selector": "#password", "value": "secret_sauce"}
            ]}

        uc = {"name": "T", "objective": "", "steps": ['Type "secret_sauce" into the Password field']}

        def fail(_nim, _uc, _execution):
            return {"verdict": "FAIL", "rules": [], "summary": "no"}

        run_with_memory(
            uc, self.nim, FakeExecutor(), self.store,
            plan_fn=plan_fn, validate_fn=fail, log=lambda *_: None, write_report=False,
        )
        self.assertEqual(self.store.list(), [])
        run_with_memory(
            uc, self.nim, FakeExecutor(), self.store,
            plan_fn=plan_fn, validate_fn=_pass, log=lambda *_: None, write_report=False,
        )
        blob = self.store.path.read_text(encoding="utf-8")
        self.assertNotIn("secret_sauce", blob)
        self.assertIn("{q1}", blob)
        self.assertEqual(len(calls), 2)

    def test_trim_drops_extra_goto_once_the_page_is_open(self):
        actions = [
            {"action": "goto", "url": "https://www.saucedemo.com"},
            {"action": "click", "selector": "#login-button"},
            {"action": "screenshot", "name": "shot"},
        ]
        trimmed = trim_planned_actions(
            "Click the Login button", actions, "https://www.saucedemo.com/inventory.html"
        )
        self.assertEqual([step["action"] for step in trimmed], ["click"])

    def test_open_step_does_not_keep_the_login_that_follows(self):
        actions = [
            {"action": "goto", "url": "https://www.saucedemo.com"},
            {"action": "fill", "selector": "#user-name", "value": "standard_user"},
            {"action": "fill", "selector": "#password", "value": "secret_sauce"},
            {"action": "click", "selector": "#login-button"},
            {"action": "extract", "selector": ".inventory_item_name"},
        ]
        trimmed = trim_planned_actions("Open https://www.saucedemo.com", actions, "about:blank")
        self.assertEqual([step["action"] for step in trimmed], ["goto"])

    def test_username_step_does_not_keep_password_or_login(self):
        actions = [
            {"action": "fill", "selector": "#user-name", "value": "standard_user"},
            {"action": "fill", "selector": "#password", "value": "secret_sauce"},
            {"action": "click", "selector": "#login-button"},
        ]
        trimmed = trim_planned_actions(
            'Type "standard_user" into the Username field', actions, "https://www.saucedemo.com/"
        )
        self.assertEqual([(step["action"], step.get("value")) for step in trimmed], [("fill", "standard_user")])

    def test_login_step_keeps_both_fields_and_the_click(self):
        actions = [
            {"action": "fill", "selector": "#user-name", "value": "standard_user"},
            {"action": "fill", "selector": "#password", "value": "secret_sauce"},
            {"action": "click", "selector": "#login-button"},
            {"action": "extract", "selector": ".inventory_item_name"},
        ]
        trimmed = trim_planned_actions(
            'Log in with username "standard_user" and password "secret_sauce"',
            actions,
            "https://www.saucedemo.com/",
        )
        self.assertEqual([step["action"] for step in trimmed], ["fill", "fill", "click"])

    def test_click_step_keeps_a_button_press_and_drops_earlier_fills(self):
        actions = [
            {"action": "fill", "selector": "#user-name", "value": "standard_user"},
            {"action": "press", "selector": "#login-button", "key": "Enter"},
            {"action": "extract", "selector": ".inventory_item_name"},
        ]
        trimmed = trim_planned_actions(
            "Click the Login button", actions, "https://www.saucedemo.com/"
        )
        self.assertEqual([step["action"] for step in trimmed], ["press"])

    def test_cache_miss_heals_on_the_fly(self):
        def plan_fn(_nim, _uc):
            return {"test_name": "T", "steps": [{"action": "click", "selector": "css selector"}]}

        executor = FakeExecutor(fail={"css selector"}, heal_selector="#login-button")
        result = run_with_memory(
            {"name": "T", "objective": "", "steps": ["Click the Login button"]},
            self.nim, executor, self.store,
            plan_fn=plan_fn, validate_fn=_pass, log=lambda *_: None, write_report=False,
        )
        self.assertEqual(executor.events[0], ("perform", "css selector"))
        self.assertIn(("heal", "css selector"), executor.events)
        self.assertEqual(result["memory"]["events"][0]["event"], "SELF_HEALED")


if __name__ == "__main__":
    unittest.main()
