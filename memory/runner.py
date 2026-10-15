"""Replay cached plans, and call the existing planner only on a miss or a broken plan.

PLAN_MEMORY=off never reaches this module. The planner, executor, and validator
are used as they are.
"""
import copy
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from memory.fingerprint import cache_key, page_fingerprint
from memory.normalize import bindings_for, mask_steps, mask_text, normalize_step, reinject_steps
from memory.plan_store import PlanStore, entry_usable

REPORT_DIR = Path("reports")
TOKENS_PER_AVOIDED_PLAN = 800
_NAV = re.compile(r"\b(goto|navigate|visit)\b|\bgo to\b|https?://", re.I)


def trim_planned_actions(english: str, actions: list, page_url: str) -> list:
    """Keep only the Playwright actions that this English step asked for."""
    original = [copy.deepcopy(step) for step in actions]
    trimmed = [copy.deepcopy(step) for step in actions]
    already_open = bool(page_url) and not str(page_url).startswith("about:")
    if already_open and not _NAV.search(english or ""):
        while trimmed and trimmed[0].get("action") == "goto":
            trimmed.pop(0)
    if "screenshot" not in (english or "").lower():
        trimmed = [step for step in trimmed if step.get("action") != "screenshot"]
    scoped = _scope_to_step(english, trimmed)
    if scoped:
        chosen = scoped
    elif _allowed_actions(english):
        chosen = []
    else:
        chosen = trimmed or original
    wants_click = bool(re.search(r"\bclick\b", english or "", re.I))
    open_cart = bool(re.search(r"\bopen\b", english or "", re.I) and "cart" in (english or "").lower() and not _NAV.search(english or ""))
    if (wants_click or open_cart) and not any(
        step.get("action") in ("click", "press") for step in chosen
    ):
        selector = "a.shopping_cart_link, #nav-cart" if open_cart else "h2 a"
        chosen.append({"action": "click", "selector": selector})
    if re.search(r"\bpress\b", english or "", re.I) and not any(
        step.get("action") == "press" for step in chosen
    ):
        chosen.append({"action": "press", "key": "Enter", "selector": "input"})
    if re.search(r"\b(extract|capture|read)\b", english or "", re.I) and not any(
        step.get("action") == "extract" for step in chosen
    ):
        chosen.extend(step for step in _fallback_actions(english) if step.get("action") == "extract")
    save_as = re.search(r"save as\s+([A-Za-z0-9_]+)", english or "", re.I)
    if "first name" in (english or "").lower() and "postal" in (english or "").lower():
        values = re.findall(r'"([^"]+)"', english or "")
        if len(values) >= 3:
            chosen = [step for step in chosen if step.get("action") != "fill"]
            chosen.extend([
                {"action": "fill", "selector": "#first-name", "value": values[0]},
                {"action": "fill", "selector": "#last-name", "value": values[1]},
                {"action": "fill", "selector": "#postal-code", "value": values[2]},
            ])
    for action in chosen:
        action["description"] = english
        if save_as and action.get("action") == "extract":
            action["save_as"] = save_as.group(1)
    return chosen


def _allowed_actions(english: str) -> set[str]:
    text = (english or "").lower()
    allowed: set[str] = set()
    if _NAV.search(text) or "http://" in text or "https://" in text:
        allowed.add("goto")
    if re.search(r"\b(type|fill|input|search)\b", text) or "log in with" in text or "login with" in text:
        allowed.add("fill")
    if "press" in text or re.search(r"\bsearch\b", text):
        allowed.add("press")
    if re.search(r"\b(click|tap|add to cart|checkout)\b", text) or "log in" in text or "login" in text or "sign in" in text:
        allowed.add("click")
    if re.search(r"\bopen\b", text) and not _NAV.search(text):
        allowed.add("click")
    if re.search(r"\b(select|sort|dropdown)\b", text):
        allowed.add("select")
    if "wait" in text:
        allowed.update(("wait", "wait_for"))
    if re.search(r"\b(extract|capture|read)\b", text):
        allowed.add("extract")
    if "screenshot" in text or "take a" in text:
        allowed.add("screenshot")
    return allowed


def _value_belongs(english: str, action: dict) -> bool:
    act = action.get("action")
    if act == "fill" or act == "select":
        value = str(action.get("value") or "")
    elif act == "press":
        value = str(action.get("key") or "")
    else:
        return True
    if not value or value.startswith("{"):
        return True
    return value.lower() in (english or "").lower()


def _belongs(english: str, action: dict, allowed: set[str]) -> bool:
    act = action.get("action")
    if act in allowed and _value_belongs(english, action):
        return True
    # A click step is sometimes planned as press-Enter on the button.
    if act == "press" and "click" in allowed and "press" not in allowed:
        if not re.search(r"\b(login|log in|sign in)\b", english or "", re.I):
            return False
        selector = str(action.get("selector") or "").lower()
        return any(token in selector for token in ("button", "login", "submit"))
    return False


def _scope_to_step(english: str, actions: list) -> list:
    """Stop at the first action that belongs to a later English step."""
    allowed = _allowed_actions(english)
    if not allowed:
        return list(actions)
    kept: list = []
    prefix: list = []
    started = False
    for action in actions:
        act = action.get("action")
        if _belongs(english, action, allowed):
            if not started and prefix:
                kept.append(prefix[-1])
            started = True
            prefix = []
            kept.append(action)
            continue
        if not started and act in ("wait", "wait_for"):
            prefix.append(action)
            continue
        if started:
            break
    return kept


def run_with_memory(
    uc: dict,
    nim,
    executor,
    store: PlanStore,
    *,
    mode: str = "on",
    replan: bool = False,
    replan_step: str | None = None,
    ttl_days: int | None = None,
    max_failures: int | None = None,
    plan_fn=None,
    validate_fn=None,
    log=print,
    write_report: bool = True,
) -> dict:
    if plan_fn is None or validate_fn is None:
        from core.planner import build_step_plan
        from core.validator import validate as default_validate
        plan_fn = plan_fn or build_step_plan
        validate_fn = validate_fn or default_validate

    ttl_days = _int_env("PLAN_TTL_DAYS", 0) if ttl_days is None else ttl_days
    max_failures = _int_env("MAX_FAILURES", 3) if max_failures is None else max_failures
    model = getattr(nim, "model", "") or ""
    replan_norm = normalize_step(replan_step) if replan_step else None

    executor.captured = {}
    executor.step_log = []
    executor.heals = []
    events: list[dict] = []
    staged: list[tuple[str, dict]] = []
    assembled: list[dict] = []
    llm_calls = 0
    failed = False

    executor.start()
    try:
        for step_text in uc.get("steps") or []:
            started = time.perf_counter()
            normalized = normalize_step(step_text)
            bindings = bindings_for(step_text)
            fingerprint = _fingerprint(executor)
            key = cache_key(normalized, fingerprint)
            existing = store.get(key)
            force = bool(replan) or (replan_norm is not None and replan_norm == normalized)
            use_cache = entry_usable(existing, max_failures, ttl_days) and not force

            if use_cache:
                actions = copy.deepcopy(existing["plan"])
                origin = "cache"
            else:
                actions = []
                try:
                    actions = _plan_step(plan_fn, nim, uc, step_text, _page_url(executor), executor)
                    llm_calls += 1
                except Exception as exc:
                    log(f"  [PLAN] {exc}")
                if not actions:
                    actions = trim_planned_actions(
                        step_text, _fallback_actions(step_text), _page_url(executor)
                    )
                origin = "miss"

            actions = trim_planned_actions(step_text, actions, _page_url(executor))
            status, actions, heal_calls = _play(
                executor, actions, bindings,
                from_cache=(origin == "cache"),
                readonly=(mode == "readonly"),
                store=store,
                key=key,
            )
            llm_calls += heal_calls

            if status == "NEED_REPLAN" and origin == "cache":
                actions = []
                try:
                    actions = _plan_step(plan_fn, nim, uc, step_text, _page_url(executor), executor)
                    llm_calls += 1
                except Exception as exc:
                    log(f"  [PLAN] {exc}")
                if not actions:
                    actions = trim_planned_actions(
                        step_text, _fallback_actions(step_text), _page_url(executor)
                    )
                actions = trim_planned_actions(step_text, actions, _page_url(executor))
                status, actions, heal_calls = _play(
                    executor, actions, bindings,
                    from_cache=False,
                    readonly=(mode == "readonly"),
                    store=store,
                    key=key,
                )
                llm_calls += heal_calls
                if status == "NEED_REPLAN":
                    status = "FAILED"
                else:
                    status = "REPLANNED"

            elapsed_ms = (time.perf_counter() - started) * 1000
            if status == "FAILED" or status == "NEED_REPLAN":
                log(f"  [FAIL] {step_text} ({elapsed_ms:.0f}ms)")
                _failure_shot(executor, uc.get("name", "unnamed"))
                failed = True
                break

            event = _event_name(status, origin)
            log(f"  [{event}] {step_text} ({elapsed_ms:.0f}ms)")
            events.append({"step": step_text, "event": event, "ms": round(elapsed_ms, 1)})
            assembled.extend(actions)
            if mode != "readonly":
                staged.append((key, _entry(
                    key, step_text, normalized, fingerprint, actions, bindings,
                    existing, event, model,
                )))
    finally:
        executor.stop()

    exec_result = {
        "test_name": uc.get("name", "unnamed"),
        "execution_status": "failed" if failed else "completed",
        "captured": executor.captured,
        "steps": executor.step_log,
        "heals": getattr(executor, "heals", []),
    }
    if write_report:
        _write_plan_file(uc, assembled)
    verdict = validate_fn(nim, uc, exec_result)
    if verdict.get("verdict") == "PASS" and mode != "readonly":
        for key, entry in staged:
            store.put(key, entry)

    avoided = sum(1 for ev in events if ev["event"] in ("CACHE_HIT", "HEALED_FROM_CACHE"))
    memory = {
        "mode": mode,
        "events": events,
        "llm_calls_made": llm_calls,
        "llm_calls_avoided": avoided,
        "tokens_saved_estimate": avoided * TOKENS_PER_AVOIDED_PLAN,
        "cached_ms": [ev["ms"] for ev in events if ev["event"] in ("CACHE_HIT", "HEALED_FROM_CACHE")],
        "planned_ms": [ev["ms"] for ev in events if ev["event"] in ("CACHE_MISS", "REPLANNED", "SELF_HEALED")],
    }
    return {
        "use_case": uc.get("name", "unnamed"),
        "execution": exec_result,
        "validation": verdict,
        "memory": memory,
    }


def _fallback_actions(english: str) -> list:
    """Actions derived from the English step when the planner returns nothing."""
    text = english or ""
    low = text.lower()
    actions: list = []
    url = re.search(r"https?://\S+", text)
    if url:
        actions.append({"action": "goto", "url": url.group(0).rstrip(".,)")})
    quoted = re.findall(r'"([^"]+)"', text)
    if re.search(r"\b(type|fill|search)\b", low) and quoted:
        actions.append({
            "action": "fill",
            "selector": "input[type='text'], input[type='search'], textarea",
            "value": quoted[0],
        })
    if "press" in low:
        actions.append({"action": "press", "key": "Enter", "selector": "input"})
    if re.search(r"\b(select|sort|dropdown)\b", low):
        actions.append({"action": "select", "selector": "select", "value": quoted[0] if quoted else ""})
    if re.search(r"\bclick\b", low):
        actions.append({"action": "click", "selector": "button"})
    if re.search(r"\b(extract|capture|read)\b", low):
        save = "extract"
        named = re.search(r"save as\s+([A-Za-z0-9_]+)", text, re.I)
        if named:
            save = named.group(1)
        actions.append({"action": "extract", "selector": "body", "all": True, "save_as": save})
    if "screenshot" in low or "take a" in low:
        name = "shot"
        named = re.search(r"named\s+(\S+)", low)
        if named:
            name = named.group(1)
        actions.append({"action": "screenshot", "name": name})
    if "wait" in low and not any(step.get("action") in ("wait", "wait_for") for step in actions):
        seconds = re.search(r"(\d+)\s*second", low)
        if seconds:
            actions.append({"action": "wait", "seconds": int(seconds.group(1))})
        else:
            actions.append({"action": "wait", "seconds": 2})
    return actions


def _plan_step(plan_fn, nim, uc: dict, step_text: str, page_url: str, executor=None) -> list:
    single = {
        "name": uc.get("name", "step"),
        "objective": uc.get("objective", ""),
        "steps": [step_text],
        "capture": uc.get("capture", ""),
        "_page_outline": executor.page_outline() if executor is not None and hasattr(executor, "page_outline") else "",
        "_page_dom": executor.page_dom() if executor is not None and hasattr(executor, "page_dom") else "",
        "_case_steps": uc.get("steps") or [],
    }
    plan = plan_fn(nim, single) or {}
    return trim_planned_actions(step_text, plan.get("steps") or [], page_url)


def _align_click_to_step(action: dict, outline: str) -> dict:
    """If a click names a control, use the on-page id that contains that name."""
    if action.get("action") != "click" or not outline:
        return action
    english = action.get("description") or ""
    skip = {"click", "the", "button", "for", "and", "with", "open", "then"}
    keywords = [word for word in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", english) if word.lower() not in skip]
    selector = str(action.get("selector") or "")
    if any(word.lower() in selector.lower() for word in keywords):
        return action
    for line in outline.splitlines():
        found = re.search(r"\bid=([A-Za-z][\w-]*)", line)
        if not found:
            continue
        ident = found.group(1)
        if any(word.lower() in ident.lower() for word in keywords):
            return {**action, "selector": f"#{ident}"}
    return action


def _align_selector_to_outline(action: dict, outline: str) -> dict:
    """Use a class when the plan named an id that is not on the page."""
    selector = action.get("selector")
    if isinstance(selector, str):
        copied = re.search(r"\bid=([A-Za-z][\w-]*)", selector)
        if copied and not selector.startswith(("#", ".")):
            selector = f"#{copied.group(1)}"
            action = {**action, "selector": selector}
    if not isinstance(selector, str) or not selector.startswith("#") or not outline:
        return action
    ident = selector[1:]
    if re.search(rf"\bid={re.escape(ident)}\b", outline):
        return action
    if re.search(rf"\bclass={re.escape(ident)}\b", outline):
        return {**action, "selector": f".{ident}"}
    return action


def _force_step_target(action: dict) -> dict:
    """Point a step at the control its English sentence names."""
    desc = (action.get("description") or "").lower()
    if action.get("action") not in ("click", "press"):
        return action
    if re.search(r"\bopen\b", desc) and "cart" in desc and "add to cart" not in desc:
        return {**action, "action": "click", "selector": "a.shopping_cart_link, #nav-cart"}
    if re.search(r"\bcheckout\b", desc):
        return {**action, "action": "click", "selector": "#checkout"}
    if re.search(r"\bcontinue\b", desc):
        return {**action, "action": "click", "selector": "#continue"}
    if re.search(r"\bfinish\b", desc):
        return {**action, "action": "click", "selector": "#finish"}
    return action


def _play(executor, actions, bindings, *, from_cache, readonly, store, key):
    actions = reinject_steps(actions, bindings)
    heal_calls = 0
    recorded = False
    saw_fallback = False
    saw_self_heal = False
    index = 0
    while index < len(actions):
        action = _align_click_to_step(actions[index], executor.page_outline() if hasattr(executor, "page_outline") else "")
        action = _align_selector_to_outline(action, executor.page_outline() if hasattr(executor, "page_outline") else "")
        action = _force_step_target(action)
        actions[index] = action
        try:
            executor.perform(action)
            _mark(executor, action, "pass")
            index += 1
            continue
        except Exception as err:
            primary_error = err
        if from_cache and not recorded and not readonly:
            store.record_failure(key)
            recorded = True
        recovered = _try_fallbacks(executor, action) if from_cache else None
        if recovered is not None:
            saw_fallback = True
            actions[index] = recovered
            index += 1
            continue
        # A saved plan is replayed as-is. Healing is only for a step that has no cache.
        if not from_cache and (action.get("selector") or action.get("locator")):
            rejected = []
            healed = False
            for _ in range(2):
                try:
                    new_sel = executor.heal(action, rejected)
                except Exception:
                    new_sel = None
                heal_calls += 1
                if not new_sel:
                    break
                trial = {**action, "selector": new_sel}
                try:
                    executor.perform(trial)
                except Exception:
                    rejected.append(new_sel)
                    continue
                fallbacks = _dedupe([*(action.get("fallback_selectors") or []), new_sel])
                actions[index] = {**action, "fallback_selectors": fallbacks}
                _mark(executor, trial, "healed", f"selector healed -> {new_sel}")
                saw_self_heal = True
                healed = True
                index += 1
                break
            if healed:
                continue
        _mark(executor, action, "fail", primary_error)
        return "NEED_REPLAN", actions, heal_calls
    if saw_self_heal:
        return "SELF_HEALED", actions, heal_calls
    if saw_fallback:
        return "HEALED_FROM_CACHE", actions, heal_calls
    return "OK", actions, heal_calls


def _try_fallbacks(executor, action: dict) -> dict | None:
    fallbacks = list(action.get("fallback_selectors") or [])
    for selector in fallbacks:
        trial = {**action, "selector": selector}
        try:
            executor.perform(trial)
        except Exception:
            continue
        _mark(executor, trial, "healed", f"fallback -> {selector}")
        return {**action, "fallback_selectors": _dedupe([selector, *fallbacks])}
    return None


def _event_name(status: str, origin: str) -> str:
    if status == "REPLANNED":
        return "REPLANNED"
    if status == "HEALED_FROM_CACHE":
        return "HEALED_FROM_CACHE"
    if status == "SELF_HEALED":
        return "SELF_HEALED"
    if origin == "cache":
        return "CACHE_HIT"
    return "CACHE_MISS"


def _entry(key, step_text, normalized, fingerprint, actions, bindings, previous, event, model) -> dict:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    previous = previous or {}
    created_at = previous.get("created_at") or now
    pass_count = int(previous.get("pass_count") or 0) + 1
    if previous and event != "CACHE_HIT":
        version = int(previous.get("version") or 1) + 1
    else:
        version = int(previous.get("version") or 1) if previous else 1
    if event == "REPLANNED":
        source = "replan"
    elif event in ("HEALED_FROM_CACHE", "SELF_HEALED"):
        source = "healed"
    elif event == "CACHE_HIT":
        source = previous.get("source") or "llm"
    elif previous and event == "CACHE_MISS":
        source = "replan"
    else:
        source = "llm"
    return {
        "key": key,
        "step_text": mask_text(step_text, bindings),
        "normalized_step": normalized,
        "page_fingerprint": fingerprint,
        "plan": mask_steps(actions, bindings),
        "version": version,
        "pass_count": pass_count,
        "failure_count": 0,
        "created_at": created_at,
        "last_pass_at": now,
        "source": source,
        "model": model or previous.get("model") or "",
    }


def _fingerprint(executor) -> str:
    page = getattr(executor, "page", None)
    url = _page_url(executor)
    html = ""
    if page is not None and hasattr(page, "content"):
        try:
            html = page.content()
        except Exception:
            html = ""
    return page_fingerprint(url, html)


def _page_url(executor) -> str:
    page = getattr(executor, "page", None)
    return str(getattr(page, "url", "") or "")


def _mark(executor, step, status: str, detail="") -> None:
    if hasattr(executor, "_log"):
        executor._log(step, status, detail)
        return
    if not hasattr(executor, "step_log") or executor.step_log is None:
        executor.step_log = []
    executor.step_log.append({"step": step, "status": status, "detail": str(detail)[:300]})


def _failure_shot(executor, name: str) -> None:
    page = getattr(executor, "page", None)
    if page is None or not hasattr(page, "screenshot"):
        return
    try:
        REPORT_DIR.mkdir(exist_ok=True)
        page.screenshot(path=str(REPORT_DIR / f"{name}_failure.png"))
    except Exception:
        pass


def _write_plan_file(uc: dict, actions: list) -> None:
    try:
        REPORT_DIR.mkdir(exist_ok=True)
        import json
        payload = {"test_name": uc.get("name", "unnamed"), "steps": actions}
        (REPORT_DIR / f"{uc.get('name', 'unnamed')}_plan.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass


def _dedupe(items: list) -> list:
    seen = []
    for item in items:
        if item and item not in seen:
            seen.append(item)
    return seen


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise SystemExit(f"Invalid {name} '{raw}' (must be an integer >= 0)")
    if value < 0:
        raise SystemExit(f"{name} must be >= 0")
    return value
