"""
Executor - runs the LLM-generated action plan with Playwright (sync API).
Includes Healwright-style self-healing: on selector failure, the live DOM is
sent to the LLM which proposes a replacement selector, and the step is retried.
"""
import re
import time
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from core.nim_client import NimClient

HEAL_SYSTEM = """You are a Playwright locator repair engine. Given a failed CSS selector,
the step's intent, and a simplified DOM snapshot, return the best replacement CSS selector.
Return JSON: {"selector": "<css>", "confidence": 0.0-1.0, "reason": "<one line>"}"""

REPORT_DIR = Path("reports")


def _simplify_dom(html: str, limit: int = 12000) -> str:
    """Strip scripts/styles/svg and crush whitespace so the DOM fits in a prompt."""
    html = re.sub(r"<(script|style|svg|noscript)[\s\S]*?</\1>", "", html, flags=re.I)
    html = re.sub(r"<!--[\s\S]*?-->", "", html)
    html = re.sub(r"\s{2,}", " ", html)
    return html[:limit]


class Executor:
    def __init__(self, nim: NimClient, headless: bool = True, slow_mo: int = 150):
        self.nim = nim
        self.headless = headless
        self.slow_mo = slow_mo
        self.captured: dict = {}       # save_as -> extracted data
        self.step_log: list = []       # per-step results for the report
        self.heals: list = []          # self-healing events

    # ---------------------------------------------------------------- helpers
    def _log(self, step, status, detail=""):
        entry = {"step": step, "status": status, "detail": str(detail)[:300]}
        self.step_log.append(entry)
        icon = {"pass": "PASS", "fail": "FAIL", "healed": "HEAL"}.get(status, status)
        print(f"  [{icon}] {step.get('action')} {step.get('selector', step.get('url',''))} {detail}")

    def _heal_selector(self, page, step) -> str | None:
        dom = _simplify_dom(page.content())
        try:
            fix = self.nim.chat_json(
                HEAL_SYSTEM,
                f"Failed selector: {step.get('selector')}\n"
                f"Step intent: {step.get('description','(none)')}\n"
                f"Action type: {step.get('action')}\n"
                f"DOM snapshot:\n{dom}",
                max_tokens=400,
            )
            if fix.get("confidence", 0) >= 0.5 and fix.get("selector"):
                self.heals.append({"old": step.get("selector"), "new": fix["selector"],
                                   "reason": fix.get("reason", "")})
                return fix["selector"]
        except Exception as e:
            print(f"  [HEAL] healing call failed: {e}")
        return None

    # ---------------------------------------------------------------- actions
    def _do_step(self, page, step):
        act = step["action"]
        sel = step.get("selector")
        if act == "goto":
            page.goto(step["url"], wait_until="domcontentloaded", timeout=45000)
        elif act == "fill":
            page.fill(sel, step["value"], timeout=10000)
        elif act == "press":
            page.press(sel, step.get("key", "Enter"), timeout=10000)
        elif act == "click":
            page.click(sel, timeout=10000)
        elif act == "select":
            page.select_option(sel, step["value"], timeout=10000)
        elif act == "wait":
            time.sleep(step.get("seconds", 2))
        elif act == "wait_for":
            page.wait_for_selector(sel, timeout=15000)
        elif act == "extract":
            if step.get("all"):
                els = page.query_selector_all(sel)
                data = [e.inner_text().strip() for e in els[:20]]
            else:
                el = page.query_selector(sel)
                data = el.inner_text().strip() if el else None
            self.captured[step.get("save_as", "extract")] = data
        elif act == "screenshot":
            REPORT_DIR.mkdir(exist_ok=True)
            page.screenshot(path=str(REPORT_DIR / f"{step.get('name','shot')}.png"), full_page=False)
        else:
            raise ValueError(f"Unknown action: {act}")

    # ---------------------------------------------------------------- runner
    def run(self, plan: dict) -> dict:
        self.captured, self.step_log, self.heals = {}, [], []
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless, slow_mo=self.slow_mo)
            ctx = browser.new_context(
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
                viewport={"width": 1366, "height": 850},
                locale="en-IN",
            )
            page = ctx.new_page()
            failed = False
            for step in plan["steps"]:
                try:
                    self._do_step(page, step)
                    self._log(step, "pass")
                except (PWTimeout, Exception) as e:
                    if step.get("selector"):
                        new_sel = self._heal_selector(page, step)
                        if new_sel:
                            try:
                                step = {**step, "selector": new_sel}
                                self._do_step(page, step)
                                self._log(step, "healed", f"selector healed -> {new_sel}")
                                continue
                            except Exception as e2:
                                e = e2
                    self._log(step, "fail", e)
                    failed = True
                    try:
                        REPORT_DIR.mkdir(exist_ok=True)
                        test_name = plan.get("test_name", "unnamed")
                        page.screenshot(
                            path=str(REPORT_DIR / f"{test_name}_failure.png")
                        )
                    except Exception:
                        pass
                    break
            browser.close()
        return {
            "test_name": plan.get("test_name", "unnamed"),
            "execution_status": "failed" if failed else "completed",
            "captured": self.captured,
            "steps": self.step_log,
            "heals": self.heals,
        }
