"""
Planner - converts a natural-language use case into a structured, executable action plan.
This is the 'AI writes the test' layer of the POC.
"""
from core.nim_client import NimClient

PLANNER_SYSTEM = """You are a senior test automation engineer. You convert natural-language
e-commerce test cases into a JSON action plan executable by a Playwright runner.

Allowed actions (use ONLY these):
- {"action": "goto", "url": "<url>"}
- {"action": "fill", "selector": "<css>", "value": "<text>", "description": "<what/why>"}
- {"action": "press", "selector": "<css>", "key": "Enter", "description": "..."}
- {"action": "click", "selector": "<css>", "description": "..."}
- {"action": "wait", "seconds": <n>}
- {"action": "wait_for", "selector": "<css>", "description": "..."}
- {"action": "extract", "selector": "<css>", "all": true|false, "save_as": "<variable_name>", "description": "..."}
- {"action": "screenshot", "name": "<file_stem>"}

Amazon.in selector hints (prefer these, they are stable):
- Search box: input#twotabsearchtextbox
- Search submit: input#nav-search-submit-button
- Search result titles: div[data-component-type='s-search-result'] h2 span
- Search result prices: div[data-component-type='s-search-result'] span.a-price > span.a-offscreen
- First result link: div[data-component-type='s-search-result'] h2 a
- Add to cart (product page): #add-to-cart-button
- Cart count badge: #nav-cart-count
- Sort dropdown: select#s-result-sort-select  (use action "select" NOT click: {"action":"select","selector":"select#s-result-sort-select","value":"price-asc-rank"})
- Cart page item titles: div.sc-list-item-content span.a-truncate-cut

SauceDemo (https://www.saucedemo.com) selector hints (prefer these, they are stable):
- Username: #user-name
- Password: #password
- Login button: #login-button
- Inventory titles: .inventory_item_name
- Inventory prices: .inventory_item_price
- Sort dropdown: select.product_sort_container  (use action "select"; value "lohi" = price low→high, "hilo" = high→low, "az", "za")
- Add Sauce Labs Backpack: #add-to-cart-sauce-labs-backpack
- Cart badge: .shopping_cart_badge
- Cart link: .shopping_cart_link
- Cart item titles: .inventory_item_name  (also on cart page)
- Checkout: #checkout
- First name: #first-name
- Last name: #last-name
- Postal code: #postal-code
- Continue: #continue
- Item total: .summary_subtotal_label
- Finish: #finish
- Complete header: .complete-header

Rules:
- Always start with goto and end with a screenshot.
- Add wait_for before extracting or clicking dynamic content.
- extract with "all": true returns a list of texts; save_as names the variable for validation.
- Keep plans minimal: 6-12 steps.
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
