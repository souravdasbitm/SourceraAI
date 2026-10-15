"""
Planner - converts a natural-language use case into a structured, executable action plan.
This is the 'AI writes the test' layer of the POC.
"""
from core.nim_client import NimClient

PLANNER_SYSTEM = """You are a senior test automation engineer. You convert natural-language
e-commerce test cases into a JSON action plan executable by a Playwright runner.

Allowed actions (use ONLY these). A target is a real Playwright locator, never the words "css selector":
- {"action": "goto", "url": "<url>"}
- {"action": "fill", "selector": "<locator>", "value": "<text>", "description": "<what/why>"}
- {"action": "press", "selector": "<locator>", "key": "Enter", "description": "..."}
- {"action": "click", "selector": "<locator>", "description": "..."}
- {"action": "select", "selector": "<locator>", "value": "<option value>", "description": "..."}
- {"action": "wait", "seconds": <n>}
- {"action": "wait_for", "selector": "<locator>", "description": "..."}
- {"action": "extract", "selector": "<locator>", "all": true|false, "save_as": "<variable_name>", "description": "..."}
- {"action": "screenshot", "name": "<file_stem>"}

"

Rules:
- Always start with goto and end with a screenshot.
- Add wait_for before extracting or clicking dynamic content.
- extract with "all": true returns a list of texts; save_as names the variable for validation.
- Keep plans minimal: 6-12 steps.
Return JSON: {"test_name": "...", "steps": [ ... ]}"""

STEP_RULES = """
Rules for this call:
- The message is ONE step of a longer test. Return only the Playwright actions that step requires.
- Do not carry out later steps. No extra login, fill, click, extract, or navigation.
- Include "goto" only when this step opens or navigates to a URL.
- Include "screenshot" only when this step asks for a screenshot.
- Every fill or select value must be text that appears in this step.
- The selector must be a real locator. Never return the placeholder "<locator>" or the words "css selector".
- Usually 1-3 actions.
Return JSON: {"test_name": "...", "steps": [ ... ]}"""


def build_plan(nim: NimClient, use_case: dict) -> dict:
    user_msg = (
        f"Test case name: {use_case['name']}\n"
        f"Objective: {use_case['objective']}\n"
        f"Steps in plain English:\n"
        + "\n".join(f"- {s}" for s in use_case["steps"])
        + f"\nData to capture for validation: {use_case.get('capture', 'any relevant output')}"
    )
    plan = nim.chat_json(PLANNER_SYSTEM, user_msg, max_tokens=2500)
    return plan


def build_step_plan(nim: NimClient, use_case: dict) -> dict:
    """Plan a single English step. The full-test rules in build_plan stay for PLAN_MEMORY=off."""
    step = (use_case.get("steps") or [""])[0]
    hints = PLANNER_SYSTEM.split("Rules:", 1)[0]
    outline = (use_case.get("_page_outline") or "").strip()
    dom = (use_case.get("_page_dom") or "").strip()
    case_steps = use_case.get("_case_steps") or []
    user_msg = (
        f"Test case name: {use_case.get('name', 'step')}\n"
        f"Objective: {use_case.get('objective', '')}\n"
    )
    if case_steps:
        user_msg += "Full test case:\n" + "\n".join(f"- {item}" for item in case_steps) + "\n"
    user_msg += (
        f"The browser is already on the page unless this step says to open a URL.\n"
        f"Plan only this step:\n- {step}"
    )
    if outline:
        user_msg += (
            "\nElements on the current page. Use one of these. Do not invent a locator.\n"
            "For fill or press, the tag must be input, textarea, or select.\n"
            f"{outline}"
        )
    if dom:
        user_msg += f"\nCurrent DOM:\n{dom[:4000]}"
    return nim.chat_json(hints + STEP_RULES, user_msg, max_tokens=1600)
