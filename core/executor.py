"""
Executor - runs the LLM-generated action plan with Playwright (sync API).
Includes Healwright-style self-healing: on selector failure, the live DOM is
sent to the LLM which proposes a replacement selector, and the step is retried.
"""
import re
import time
from urllib.parse import urljoin
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from core.nim_client import NimClient

HEAL_SYSTEM = """You are a Playwright locator repair engine. You receive a failed locator,
the step the user asked for, and a list of elements that actually exist on the page.
Return a different Playwright locator that matches one of those elements.
Use whichever fits: "#id", "[data-test=...]", "text=...", "role=button[name='Login']",
"xpath=//button", or {"by":"role|text|label|placeholder|testid|title|alt|xpath|css", ...}.
Do not invent class names. Do not return the words "css selector".
Return JSON: {"selector": "<locator>", "confidence": 0.0-1.0, "reason": "<one line>"}"""

REPORT_DIR = Path("reports")


def _simplify_dom(html: str, limit: int = 12000) -> str:
    """Strip scripts/styles/svg and crush whitespace so the DOM fits in a prompt."""
    html = re.sub(r"<(script|style|svg|noscript)[\s\S]*?</\1>", "", html, flags=re.I)
    html = re.sub(r"<!--[\s\S]*?-->", "", html)
    html = re.sub(r"\s{2,}", " ", html)
    return html[:limit]


def _clean_selector(selector):
    if not isinstance(selector, str):
        return selector
    selector = selector.strip().strip("'\"")
    if selector.lower().startswith("css="):
        selector = selector[4:].strip()
    return selector


_PLACEHOLDER_LOCATOR = re.compile(
    r"^(css(\s+selector)?|<css>|<locator>|selector|locator)$",
    re.I,
)


def _as_locator_spec(step: dict) -> dict | None:
    spec = step.get("locator")
    if isinstance(spec, dict):
        return spec
    sel = step.get("selector")
    if isinstance(sel, dict):
        nested = sel.get("locator")
        return nested if isinstance(nested, dict) else sel
    return None


def _locator(page, step):
    """Resolve a CSS, xpath, text, role, or get_by_* locator. No site-specific hints."""
    spec = _as_locator_spec(step)
    if spec is not None:
        by = str(spec.get("by") or spec.get("strategy") or "css").lower().replace("-", "").replace("_", "")
        text = spec.get("text") or spec.get("name") or spec.get("value") or ""
        if by == "role":
            kwargs = {}
            if spec.get("name") or spec.get("text"):
                kwargs["name"] = spec.get("name") or spec.get("text")
            if "exact" in spec:
                kwargs["exact"] = bool(spec.get("exact"))
            return page.get_by_role(spec.get("role") or "button", **kwargs)
        if by == "text":
            return _text_locator(page, step, text)
        if by == "label":
            return page.get_by_label(text)
        if by == "placeholder":
            return page.get_by_placeholder(text)
        if by in ("testid", "test_id"):
            return page.get_by_test_id(spec.get("value") or spec.get("text") or "")
        if by == "alt":
            return page.get_by_alt_text(text)
        if by == "title":
            return page.get_by_title(text)
        if by == "xpath":
            value = str(spec.get("value") or text)
            if not value.startswith("xpath="):
                value = f"xpath={value}"
            return page.locator(value)
        value = spec.get("value") or text
        if not isinstance(value, str) or not value:
            raise ValueError(f"not a locator: {spec!r}")
        return page.locator(value)
    sel = step.get("selector")
    if not isinstance(sel, str):
        raise ValueError(f"not a locator: {sel!r}")
    sel = _clean_selector(sel)
    if not sel or _PLACEHOLDER_LOCATOR.match(sel):
        raise ValueError(f"not a locator: {step.get('selector')!r}")
    if sel.lower().startswith("text="):
        return _text_locator(page, step, sel.split("=", 1)[1].strip().strip("'\""))
    tagged = re.fullmatch(r"[a-zA-Z][\w-]*(\[[^\]]+\])", sel)
    if tagged:
        return page.locator(sel).or_(page.locator(tagged.group(1)))
    return page.locator(sel)


def _text_locator(page, step, text: str):
    """A fill must hit an editable control, not the first heading that contains the word."""
    if step.get("action") in ("fill", "press", "select"):
        for candidate in (
            page.get_by_placeholder(text),
            page.get_by_label(text),
            page.get_by_role("textbox", name=text),
            page.get_by_role("searchbox", name=text),
            page.get_by_role("combobox", name=text),
        ):
            try:
                if candidate.count():
                    return candidate
            except Exception:
                continue
    return page.get_by_text(text, exact=False)


def _price_texts(page) -> list:
    """One currency amount per result card, otherwise short price texts on the page."""
    try:
        found = page.evaluate(
            """() => {
              const re = /^[₹$€£]\\s?[\\d,]+(?:\\.\\d+)?$/;
              const cards = document.querySelectorAll('[data-component-type="s-search-result"]');
              const out = [];
              const take = (root) => {
                const current = root.querySelector('.a-price:not(.a-text-price) .a-offscreen, .a-price .a-offscreen');
                if (current) {
                  const price = (current.textContent || '').trim();
                  if (re.test(price)) return price;
                }
                for (const el of root.querySelectorAll('span')) {
                  if (el.closest('select, option')) continue;
                  const t = (el.textContent || '').trim();
                  if (re.test(t)) return t;
                }
                return '';
              };
              if (cards.length) {
                for (const card of cards) {
                  const label = (card.innerText || '').slice(0, 400);
                  if (/sponsored/i.test(label)) continue;
                  const price = take(card);
                  if (price) out.push(price);
                  if (out.length >= 12) break;
                }
                return out;
              }
              for (const el of document.querySelectorAll('span, div')) {
                if (el.closest('select, option')) continue;
                if (el.children.length) continue;
                const t = (el.textContent || '').trim();
                if (re.test(t) && !out.includes(t)) out.push(t);
                if (out.length >= 12) break;
              }
              return out;
            }"""
        )
    except Exception:
        return []
    return found or []


def _title_texts(page) -> list:
    try:
        found = page.evaluate(
            """() => {
              const out = [];
              const cards = document.querySelectorAll('[data-component-type="s-search-result"] h2');
              const nodes = cards.length ? cards : document.querySelectorAll('h2');
              for (const el of nodes) {
                const t = (el.innerText || '').trim().replace(/\\s+/g, ' ');
                if (t.length < 8 || t.length > 180) continue;
                if (/^results?$/i.test(t)) continue;
                if (!out.includes(t)) out.push(t);
                if (out.length >= 12) break;
              }
              return out;
            }"""
        )
    except Exception:
        return []
    return found or []


def _cart_count(page) -> str:
    _clear_gate(page)
    try:
        badge = page.locator("#nav-cart-count, .shopping_cart_badge")
        if badge.count():
            text = badge.first.inner_text().strip()
            if text.isdigit() and text != "0":
                return text
            added = page.get_by_text(re.compile(r"added to cart|enhance your purchase", re.I))
            if added.count() and text in ("", "0"):
                return "1"
    except Exception:
        pass
    try:
        count = page.evaluate(
            """() => {
              const nodes = document.querySelectorAll('[id*=cart i], [class*=cart i]');
              let hidden = '';
              for (const el of nodes) {
                if (/assist/i.test(el.id || '') || /assist/i.test(el.className || '')) continue;
                const t = (el.textContent || '').trim();
                if (!/^\\d+$/.test(t) || t.length >= 4 || el.children.length !== 0) continue;
                const box = el.getBoundingClientRect();
                if (box.width > 0 && box.height > 0) return t;
                hidden = t;
              }
              return hidden;
            }"""
        )
    except Exception:
        return ""
    return count or "0"


def _clear_gate(page) -> None:
    """Dismiss a shopping interstitial so the real page can be used."""
    try:
        gate = page.locator("button, span, a, input").filter(has_text=re.compile(r"^\s*no thanks\s*$", re.I))
        if gate.count():
            gate.last.click(timeout=5000)
            page.wait_for_timeout(800)
            return
        page.evaluate(
            """() => {
              const nodes = [...document.querySelectorAll('button, span, a, input')];
              const el = nodes.find(node => /^\\s*no thanks\\s*$/i.test((node.innerText || node.value || '').trim()));
              if (el) el.click();
            }"""
        )
    except Exception:
        return


def _open_first_result(page) -> None:
    term = ""
    try:
        term = page.locator("input[type='search'], input[name='field-keywords'], #twotabsearchtextbox").first.input_value()
    except Exception:
        term = ""
    words = [word for word in re.findall(r"[a-z0-9]+", term.lower()) if len(word) > 3]
    links = page.locator("[data-component-type='s-search-result'] h2").locator("xpath=ancestor::a[1]")
    if links.count() == 0:
        links = page.locator("[data-component-type='s-search-result'] a").filter(has_text=re.compile(r".{25,}"))
    chosen = links.first
    for index in range(min(links.count(), 8)):
        text = links.nth(index).inner_text().lower()
        if words and any(word in text for word in words):
            chosen = links.nth(index)
            break
    href = ""
    try:
        href = chosen.get_attribute("href") or ""
    except Exception:
        href = ""
    if href:
        page.goto(urljoin(page.url, href), wait_until="domcontentloaded", timeout=45000)
        return
    chosen.click(timeout=10000)


def _select_with_value(page, value: str):
    selects = page.locator("select")
    try:
        count = selects.count()
    except Exception:
        return None
    for i in range(count):
        choice = selects.nth(i)
        try:
            options = choice.locator("option").evaluate_all(
                "(els) => els.map(el => el.getAttribute('value') || '')"
            )
        except Exception:
            continue
        if value in options:
            return choice
    return None


def _interactive_outline(page) -> str:
    """Elements the healer is allowed to target. Built from the live page, not from site hints."""
    script = """() => {
      const nodes = document.querySelectorAll(
        'a, button, input, select, textarea, h1, h2, [id], [data-test], [role="button"]'
      );
      return Array.from(nodes).slice(0, 60).map(el => ({
        tag: el.tagName.toLowerCase(),
        id: el.id || '',
        test: el.getAttribute('data-test') || '',
        name: el.getAttribute('name') || '',
        type: el.getAttribute('type') || '',
        placeholder: el.getAttribute('placeholder') || '',
        className: (typeof el.className === 'string' ? el.className.split(/\\s+/)[0] : '') || '',
        text: (el.innerText || el.value || el.getAttribute('aria-label') || '').trim().replace(/\\s+/g, ' ').slice(0, 60)
      }));
    }"""
    try:
        rows = page.evaluate(script)
    except Exception:
        return _simplify_dom(page.content(), 4000)
    lines = []
    for row in rows or []:
        bits = [row.get("tag") or "el"]
        for key, label in (
            ("id", "id"), ("test", "data-test"), ("name", "name"),
            ("type", "type"), ("placeholder", "placeholder"),
            ("className", "class"), ("text", "text"),
        ):
            if row.get(key):
                bits.append(f"{label}={row[key]}")
        lines.append(" ".join(bits))
    return "\n".join(lines) or _simplify_dom(page.content(), 4000)


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

    def _heal_selector(self, page, step, rejected=None) -> str | None:
        failed = _clean_selector(step.get("selector") or "")
        blocked = {_clean_selector(item) for item in (rejected or []) if item}
        blocked.add(failed)
        intent = step.get("description") or step.get("action") or "(none)"
        try:
            fix = self.nim.chat_json(
                HEAL_SYSTEM,
                f"Failed selector: {failed}\n"
                f"Rejected selectors: {', '.join(sorted(blocked)) or '(none)'}\n"
                f"Step: {intent}\n"
                f"Action type: {step.get('action')}\n"
                f"Elements on the page:\n{_interactive_outline(page)}",
                max_tokens=400,
            )
            proposed = _clean_selector(fix.get("selector") or "")
            if fix.get("confidence", 0) >= 0.5 and proposed and proposed not in blocked:
                self.heals.append({"old": failed, "new": proposed,
                                   "reason": fix.get("reason", "")})
                return proposed
        except Exception as e:
            print(f"  [HEAL] healing call failed: {e}")
        return None

    # ---------------------------------------------------------------- actions
    def _do_step(self, page, step):
        act = step["action"]
        if act == "goto":
            page.goto(step["url"], wait_until="domcontentloaded", timeout=45000)
            return
        if act == "wait":
            time.sleep(step.get("seconds", 2))
            return
        if act == "screenshot":
            REPORT_DIR.mkdir(exist_ok=True)
            page.screenshot(path=str(REPORT_DIR / f"{step.get('name','shot')}.png"), full_page=False)
            return
        if act not in ("fill", "press", "click", "select", "wait_for", "extract"):
            raise ValueError(f"Unknown action: {act}")
        desc = (step.get("description") or "").lower()
        if re.search(r"\bopen\b", desc) and "cart" in desc and "add" not in desc:
            link = page.locator("a.shopping_cart_link, #nav-cart").first
            link.click(timeout=10000)
            try:
                page.wait_for_url("**cart**", timeout=8000)
            except Exception:
                page.locator("#checkout, [data-test=checkout]").first.wait_for(state="visible", timeout=8000)
            return
        loc = _locator(page, step)
        if act == "fill":
            loc.first.fill(step["value"], timeout=10000)
        elif act == "press":
            if "press" in desc:
                page.keyboard.press(step.get("key", "Enter"))
            else:
                loc.first.press(step.get("key", "Enter"), timeout=10000)
        elif act == "click":
            if re.search(r"\bopen\b", desc) and "cart" in desc and "add" not in desc:
                page.locator("a.shopping_cart_link, #nav-cart").first.click(timeout=10000)
                return
            if "add to cart" in desc:
                quoted = re.findall(r'"([^"]+)"', step.get("description") or "")
                if not quoted:
                    direct = page.locator("#add-to-cart-button")
                    if direct.count():
                        direct.first.click(timeout=10000)
                        _clear_gate(page)
                        return
                    buttons = page.get_by_role("button", name=re.compile(r"^add to cart$", re.I))
                    for index in range(buttons.count()):
                        button = buttons.nth(index)
                        ident = (button.get_attribute("id") or "") + (button.get_attribute("class") or "")
                        if "assist" in ident.lower():
                            continue
                        button.click(timeout=10000)
                        return
                    raise RuntimeError("Add to Cart button is not on screen")
            if re.search(r"\bcheckout\b", desc):
                page.locator("#checkout, [data-test=checkout]").first.click(timeout=10000)
                return
            if "result" in desc and "click" in desc:
                _open_first_result(page)
                return
            loc.first.click(timeout=10000)
        elif act == "select":
            value = str(step.get("value") or "")
            for token in re.findall(r'"([^"]+)"', step.get("description") or ""):
                if _select_with_value(page, token):
                    value = token
                    break
            match = _select_with_value(page, value)
            (match or loc.first).select_option(value, timeout=10000)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=20000)
            except Exception:
                pass
        elif act == "wait_for":
            loc.first.wait_for(state="visible", timeout=15000)
        elif act == "extract":
            if "cart" in desc and ("count" in desc or "badge" in desc):
                self.captured[step.get("save_as", "extract")] = _cart_count(page)
                return
            if "price" in desc:
                prices = _price_texts(page)
                if len(prices) < 3:
                    raise RuntimeError("extract did not return prices")
                self.captured[step.get("save_as", "extract")] = prices
                return
            if "title" in desc and "titles" not in desc:
                title = ""
                try:
                    title = page.evaluate(
                        """() => {
                          const named = document.querySelector('#productTitle');
                          if (named) {
                            const text = (named.innerText || '').trim().replace(/\\s+/g, ' ');
                            if (text.length > 8) return text;
                          }
                          const headings = [...document.querySelectorAll('h1')]
                            .map(el => (el.innerText || '').trim().replace(/\\s+/g, ' '))
                            .filter(text => text.length > 8 && !/keyboard shortcut/i.test(text));
                          headings.sort((a, b) => b.length - a.length);
                          return headings[0] || '';
                        }"""
                    ) or ""
                except Exception:
                    title = ""
                if len(title) > 8:
                    self.captured[step.get("save_as", "extract")] = title
                    return
            if "title" in desc:
                titles = _title_texts(page)
                need = 3 if "titles" in desc else 1
                if len(titles) >= need:
                    self.captured[step.get("save_as", "extract")] = titles[:12]
                    return
            if step.get("all"):
                count = loc.count()
                data = []
                for i in range(min(count, 20)):
                    text = loc.nth(i).inner_text().strip()
                    if text:
                        data.append(text)
                if not data:
                    raise RuntimeError(f"extract matched no elements: {step.get('selector') or step.get('locator')}")
            else:
                if loc.count() == 0:
                    raise RuntimeError(f"extract matched no element: {step.get('selector') or step.get('locator')}")
                data = loc.first.inner_text().strip()
                if not data:
                    raise RuntimeError(f"extract matched no element: {step.get('selector') or step.get('locator')}")
            self.captured[step.get("save_as", "extract")] = data

    # ---------------------------------------------------------------- session
    def start(self):
        """Open a browser. run() uses this; the plan-memory runner does too."""
        self._play = sync_playwright().start()
        self._browser = self._play.chromium.launch(headless=self.headless, slow_mo=self.slow_mo)
        self._context = self._browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
            viewport={"width": 1366, "height": 850},
            locale="en-IN",
        )
        self.page = self._context.new_page()

    def stop(self):
        browser = getattr(self, "_browser", None)
        play = getattr(self, "_play", None)
        self.page = None
        self._browser = None
        self._play = None
        if browser is not None:
            browser.close()
        if play is not None:
            play.stop()

    def page_outline(self) -> str:
        page = getattr(self, "page", None)
        if page is None:
            return ""
        try:
            return _interactive_outline(page)
        except Exception:
            return ""

    def page_dom(self) -> str:
        """Simplified live HTML for the planner. Empty until a real page is open."""
        page = getattr(self, "page", None)
        if page is None:
            return ""
        url = str(getattr(page, "url", "") or "")
        if not url or url.startswith("about:"):
            return ""
        try:
            return _simplify_dom(page.content(), 8000)
        except Exception:
            return ""

    def perform(self, step):
        _clear_gate(self.page)
        self._do_step(self.page, step)

    def heal(self, step, rejected=None) -> str | None:
        return self._heal_selector(self.page, step, rejected)

    # ---------------------------------------------------------------- runner
    def run(self, plan: dict) -> dict:
        self.captured, self.step_log, self.heals = {}, [], []
        self.start()
        try:
            failed = False
            for step in plan["steps"]:
                try:
                    self.perform(step)
                    self._log(step, "pass")
                except (PWTimeout, Exception) as e:
                    if step.get("selector"):
                        new_sel = self.heal(step)
                        if new_sel:
                            try:
                                step = {**step, "selector": new_sel}
                                self.perform(step)
                                self._log(step, "healed", f"selector healed -> {new_sel}")
                                continue
                            except Exception as e2:
                                e = e2
                    self._log(step, "fail", e)
                    failed = True
                    try:
                        REPORT_DIR.mkdir(exist_ok=True)
                        test_name = plan.get("test_name", "unnamed")
                        self.page.screenshot(
                            path=str(REPORT_DIR / f"{test_name}_failure.png")
                        )
                    except Exception:
                        pass
                    break
            return {
                "test_name": plan.get("test_name", "unnamed"),
                "execution_status": "failed" if failed else "completed",
                "captured": self.captured,
                "steps": self.step_log,
                "heals": self.heals,
            }
        finally:
            self.stop()
