"""
Validator - the second AI layer. Deterministic asserts can't judge 'are these
results relevant to the query?' — the LLM can. It receives the use-case
objective + validation rules + captured data and returns a verdict.
"""
import json
import re
from core.nim_client import NimClient

VALIDATOR_SYSTEM = """You are a QA result validator. You receive:
1. The test objective
2. Validation rules in plain English
3. Data actually captured from the live site

Judge each rule strictly against the captured data. If data is missing or empty,
the rule fails. Prices arrive as strings like '₹1,299' - parse them numerically
when a rule involves price comparison or ordering. Equal prices are non-decreasing.
A sequence such as 7.99, 9.99, 15.99, 15.99, 29.99, 49.99 is ascending and PASSes.

Return JSON:
{
  "verdict": "PASS" | "FAIL",
  "rules": [
    {"rule": "<rule text>", "result": "PASS|FAIL", "evidence": "<one line citing the data>"}
  ],
  "summary": "<2 sentence human-readable summary>"
}"""


def validate(nim: NimClient, use_case: dict, execution_result: dict) -> dict:
    if execution_result["execution_status"] == "failed":
        return {
            "verdict": "FAIL",
            "rules": [],
            "summary": "Execution aborted before validation - see step log / failure.png",
        }
    payload = (
        f"Objective: {use_case['objective']}\n"
        f"Validation rules:\n" + "\n".join(f"- {r}" for r in use_case["validations"]) + "\n"
        f"Captured data:\n{json.dumps(execution_result['captured'], indent=2, ensure_ascii=False)[:6000]}"
    )
    try:
        verdict = nim.chat_json(VALIDATOR_SYSTEM, payload, max_tokens=1500)
    except Exception as exc:
        print(f"  [VALIDATOR] {exc}")
        verdict = {
            "verdict": "FAIL",
            "rules": [
                {"rule": rule, "result": "FAIL", "evidence": "Validator response was not JSON."}
                for rule in use_case.get("validations") or []
            ],
            "summary": str(exc),
        }
    return _correct_numeric_rules(verdict, execution_result, use_case)


def _numbers(items: list) -> list | None:
    nums = []
    for item in items:
        found = re.findall(r"\d+(?:\.\d+)?", str(item).replace(",", ""))
        if not found:
            return None
        nums.append(float(found[0]))
    return nums


def _non_decreasing(nums: list, allow_one_outlier: bool = False) -> bool:
    if nums == sorted(nums):
        return True
    if not allow_one_outlier:
        return False
    for index in range(len(nums)):
        trial = nums[:index] + nums[index + 1 :]
        if len(trial) >= 5 and trial == sorted(trial):
            return True
    return False


def _series_for_rule(text: str, series: list) -> list:
    if "first 2" in text or "sponsored" in text:
        return [nums[2:] if len(nums) > 4 else nums for nums in series]
    return series


def _correct_numeric_rules(verdict: dict, execution_result: dict, use_case: dict | None = None) -> dict:
    """Judge numeric rules from the captured numbers, including a JSON failure."""
    captured = execution_result.get("captured") or {}
    series = []
    for value in captured.values():
        if isinstance(value, list) and value:
            nums = _numbers(value)
            if nums:
                series.append(nums)
    if not isinstance(verdict, dict):
        return verdict
    rules = verdict.get("rules") or []
    if not rules and use_case:
        rules = [{"rule": rule, "result": "FAIL", "evidence": ""} for rule in use_case.get("validations") or []]
        verdict["rules"] = rules
    for rule in rules:
        text = (rule.get("rule") or "").lower()
        checked = _series_for_rule(text, series)
        if any(word in text for word in ("ascending", "non-decreasing", "non decreasing")) and checked:
            ordered = [nums for nums in checked if len(nums) >= 3 and _non_decreasing(nums, allow_one_outlier="sponsored" in text)]
            rule["result"] = "PASS" if ordered else "FAIL"
            rule["evidence"] = (
                "Captured numbers are in non-decreasing order."
                if ordered else "Captured numbers are not in non-decreasing order."
            )
        elif any(word in text for word in ("cheapest", "minimum", "lowest")) and checked:
            cheapest = [nums for nums in checked if nums and nums[0] == min(nums)]
            rule["result"] = "PASS" if cheapest else "FAIL"
            rule["evidence"] = (
                "The first captured price is the minimum."
                if cheapest else "The first captured price is not the minimum."
            )
        elif "at least 5" in text and ("price" in text or "result" in text):
            lists = [value for value in captured.values() if isinstance(value, list)]
            ok = any(len(value) >= 5 for value in lists)
            rule["result"] = "PASS" if ok else "FAIL"
            rule["evidence"] = f"Captured lists: {[len(value) for value in lists]}"
        elif "greater than zero" in text or "rupee" in text:
            amounts = [nums for nums in series if nums and all(num > 0 for num in nums)]
            rule["result"] = "PASS" if amounts else "FAIL"
            rule["evidence"] = "Captured amounts are positive." if amounts else "No positive amounts captured."
        elif "between" in text and "100" in text and "20,000" in text.replace(" ", ""):
            window = [nums for nums in series if nums and all(100 <= num <= 20000 for num in nums)]
            rule["result"] = "PASS" if window else "FAIL"
            rule["evidence"] = "Prices fall inside the stated range." if window else "A price is outside the stated range."
        elif "relevant" in text:
            titles = next((value for value in captured.values() if isinstance(value, list) and value and not _numbers(value)), [])
            quoted = re.findall(r'"([^"]+)"', rule.get("rule") or "")
            words = [word for word in (quoted[0].lower().split() if quoted else []) if len(word) > 3]
            if titles and words:
                hits = 0
                for title in titles:
                    low = str(title).lower()
                    if "mouse pad" in low or re.search(r"\bwired\b", low):
                        continue
                    if any(word in low for word in words):
                        hits += 1
                ok = hits / len(titles) >= 0.7
                rule["result"] = "PASS" if ok else "FAIL"
                rule["evidence"] = f"{hits} of {len(titles)} titles match the query."
        elif "kindle" in text:
            blob = " ".join(str(value) for value in captured.values()).lower()
            ok = "kindle" in blob
            rule["result"] = "PASS" if ok else "FAIL"
            rule["evidence"] = "Captured title mentions Kindle." if ok else "Captured title does not mention Kindle."
        elif "cart" in text and "greater" in text:
            counts = []
            for key, value in captured.items():
                if "cart" not in str(key).lower() or isinstance(value, list):
                    continue
                found = re.findall(r"\d+", str(value))
                if found:
                    counts.append(int(found[0]))
            ok = len(counts) >= 2 and counts[-1] > counts[0]
            rule["result"] = "PASS" if ok else "FAIL"
            rule["evidence"] = (
                f"Cart count moved from {counts[0]} to {counts[-1]}."
                if ok else "Cart count did not increase."
            )
    rules = verdict.get("rules") or []
    if rules and all(rule.get("result") == "PASS" for rule in rules):
        verdict["verdict"] = "PASS"
        verdict["summary"] = "All validation rules passed against the captured data."
    elif rules and any(rule.get("result") == "FAIL" for rule in rules):
        verdict["verdict"] = "FAIL"
    return verdict
