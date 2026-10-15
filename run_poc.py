"""
SourceaAI orchestrator.
Flow per use case:  YAML (English) -> Planner LLM -> Playwright Executor
                    -> Validator LLM -> Markdown + JSON report

Usage:
  export NVIDIA_API_KEY=nvapi-xxxx          # from build.nvidia.com
  python run_poc.py                          # all use cases, headless (sequential)
  python run_poc.py --headed                 # watch the browser
  python run_poc.py --only UC1               # run one use case
  python run_poc.py --parallel usecases --workers 3
  python run_poc.py --parallel files
  python run_poc.py --parallel all
"""
import argparse
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import yaml

from core.nim_client import NimClient
from core.planner import build_plan
from core.executor import Executor
from core.validator import validate

REPORT_DIR = Path("reports")
USECASE_DIR = Path("usecases")
PARALLEL_MODES = ("off", "files", "usecases", "all")


def _log(msg: str = "") -> None:
    """Process-aware stdout (Playwright sync is not thread-safe; we use processes)."""
    text = f"[pid={os.getpid()}] {msg}"
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode("ascii"), flush=True)


def _avg_ms(values: list) -> str:
    if not values:
        return "n/a"
    return f"{sum(values) / len(values):.0f} ms"


def verdict_counts(results: list) -> tuple[int, int]:
    passed = sum(1 for r in results if r.get("validation", {}).get("verdict") == "PASS")
    return passed, len(results) - passed


def render_markdown(results: list) -> str:
    passed, failed = verdict_counts(results)
    lines = [f"# SourceaAI Report - {datetime.now():%Y-%m-%d %H:%M}",
             "", "Engine: NVIDIA Build (NIM) | Runner: Playwright | Pattern: Plan -> Execute -> Validate",
             f"Passed: {passed} | Failed: {failed} | Total: {len(results)}", ""]
    for r in results:
        v = r["validation"]
        lines.append(f"## {r['use_case']} — **{v.get('verdict','?')}**")
        lines.append(f"- Execution: {r['execution']['execution_status']}")
        if r["execution"]["heals"]:
            for h in r["execution"]["heals"]:
                lines.append(f"- Self-healed selector: `{h['old']}` -> `{h['new']}` ({h['reason']})")
        for rule in v.get("rules", []):
            lines.append(f"- [{rule['result']}] {rule['rule']} — _{rule.get('evidence','')}_")
        lines.append(f"\n> {v.get('summary','')}\n")
    lines.extend(_memory_summary(results))
    return "\n".join(lines)


def _memory_summary(results: list) -> list:
    blocks = [r["memory"] for r in results if r.get("memory")]
    if not blocks:
        return []
    counts = {"CACHE_HIT": 0, "CACHE_MISS": 0, "HEALED_FROM_CACHE": 0, "REPLANNED": 0, "SELF_HEALED": 0}
    made = avoided = tokens = 0
    cached_ms: list = []
    planned_ms: list = []
    for block in blocks:
        for event in block.get("events") or []:
            name = event.get("event")
            if name in counts:
                counts[name] += 1
        made += int(block.get("llm_calls_made") or 0)
        avoided += int(block.get("llm_calls_avoided") or 0)
        tokens += int(block.get("tokens_saved_estimate") or 0)
        cached_ms.extend(block.get("cached_ms") or [])
        planned_ms.extend(block.get("planned_ms") or [])
    total = sum(counts.values())
    reused = counts["CACHE_HIT"] + counts["HEALED_FROM_CACHE"]
    rate = (100.0 * reused / total) if total else 0.0
    return [
        "## Plan memory",
        f"- Mode: {blocks[0].get('mode', 'on')}",
        f"- Cache hit rate: {rate:.0f}% "
        f"({reused} reused / {total} steps; hits {counts['CACHE_HIT']}, "
        f"healed from cache {counts['HEALED_FROM_CACHE']}, "
        f"misses {counts['CACHE_MISS']}, replanned {counts['REPLANNED']}, "
        f"self-healed {counts['SELF_HEALED']})",
        f"- LLM calls made: {made} | avoided: {avoided}",
        f"- Estimated tokens saved: {tokens} (800 per avoided planner call)",
        f"- Average step time: cached {_avg_ms(cached_ms)} | planned {_avg_ms(planned_ms)}",
        "",
    ]


def resolve_parallel_config(args) -> tuple[str, int]:
    """CLI overrides env; env overrides defaults."""
    env_mode = os.getenv("PARALLEL_MODE", "off").strip().lower()
    mode = (args.parallel if args.parallel is not None else env_mode) or "off"
    if mode not in PARALLEL_MODES:
        sys.exit(f"Invalid PARALLEL_MODE '{mode}'. Choose from: {', '.join(PARALLEL_MODES)}")

    env_workers = os.getenv("PARALLEL_WORKERS", "2")
    workers_raw = args.workers if args.workers is not None else env_workers
    try:
        workers = int(workers_raw)
    except (TypeError, ValueError):
        sys.exit(f"Invalid PARALLEL_WORKERS '{workers_raw}' (must be an integer >= 1)")
    if workers < 1:
        sys.exit("PARALLEL_WORKERS must be >= 1")
    return mode, workers


def load_usecase_files() -> list[tuple[Path, list]]:
    """Load all usecases/*.yaml|yml as (path, use_cases) pairs, sorted by filename."""
    paths = sorted(USECASE_DIR.glob("*.yaml")) + sorted(USECASE_DIR.glob("*.yml"))
    # de-dupe if both extensions somehow match same stem (unlikely)
    seen = set()
    files = []
    for path in paths:
        if path.name in seen:
            continue
        seen.add(path.name)
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        if not isinstance(data, list):
            sys.exit(f"Use-case file must be a YAML list: {path}")
        tagged = []
        for item in data:
            if isinstance(item, dict):
                tagged.append({**item, "_suite": path.stem})
            else:
                tagged.append(item)
        files.append((path, tagged))
    if not files:
        sys.exit(f"No use-case YAML files found in {USECASE_DIR}/")
    return files


def filter_use_cases(use_cases: list, only: str | None) -> list:
    if not only:
        return use_cases
    filtered = [u for u in use_cases if u["name"].startswith(only)]
    return filtered


def run_use_case(uc: dict, headed: bool, slow_mo: int, replan: bool = False,
                 replan_step: str | None = None) -> dict:
    """Plan -> execute -> validate one use case. Own NimClient + browser per process.

    PLAN_MEMORY=off keeps the original single planner call. on/readonly replay
    stored steps and call the planner only on a miss.
    """
    mem_mode = os.getenv("PLAN_MEMORY", "on").strip().lower()
    if mem_mode not in ("on", "off", "readonly"):
        sys.exit(f"Invalid PLAN_MEMORY '{mem_mode}'. Choose from: on, off, readonly")
    if mem_mode == "off":
        return _run_use_case_direct(uc, headed, slow_mo)

    from memory.plan_store import PlanStore
    from memory.runner import run_with_memory

    nim = NimClient()
    _log(f"=== {uc['name']} ===")
    env_headless = os.getenv("HEADLESS", "true").lower() != "false"
    execu = Executor(nim, headless=(not headed) and env_headless, slow_mo=slow_mo)
    store = PlanStore(uc.get("_suite") or "default")
    try:
        result = run_with_memory(
            uc, nim, execu, store,
            mode=mem_mode, replan=replan, replan_step=replan_step, log=_log,
        )
    except Exception as exc:
        _log(f"        VERDICT: FAIL  - {exc}")
        return {
            "use_case": uc["name"],
            "execution": {"execution_status": "failed", "captured": {}, "steps": [], "heals": []},
            "validation": {"verdict": "FAIL", "rules": [], "summary": str(exc)},
        }
    verdict = result["validation"]
    _log(f"        VERDICT: {verdict.get('verdict')}  - {verdict.get('summary','')}")
    return result


def _run_use_case_direct(uc: dict, headed: bool, slow_mo: int) -> dict:
    """Original path: one planner call for the whole use case."""
    nim = NimClient()
    _log(f"=== {uc['name']} ===")
    _log("  [1/3] Planning with NIM ...")
    plan = build_plan(nim, uc)
    REPORT_DIR.mkdir(exist_ok=True)
    (REPORT_DIR / f"{uc['name']}_plan.json").write_text(
        json.dumps(plan, indent=2), encoding="utf-8"
    )
    _log(f"        {len(plan['steps'])} steps generated")

    _log("  [2/3] Executing with Playwright ...")
    env_headless = os.getenv("HEADLESS", "true").lower() != "false"
    execu = Executor(nim, headless=(not headed) and env_headless, slow_mo=slow_mo)
    exec_result = execu.run(plan)

    _log("  [3/3] Validating with NIM ...")
    verdict = validate(nim, uc, exec_result)
    _log(f"        VERDICT: {verdict.get('verdict')}  - {verdict.get('summary','')}")

    return {"use_case": uc["name"], "execution": exec_result, "validation": verdict}


def _run_indexed(payload: dict) -> tuple[int, dict]:
    """Picklable worker entry for ProcessPoolExecutor (Windows-safe)."""
    return payload["index"], run_use_case(
        payload["uc"], payload["headed"], payload["slow_mo"],
        payload.get("replan", False), payload.get("replan_step"),
    )


def run_sequential(use_cases: list, headed: bool, slow_mo: int,
                   replan: bool = False, replan_step: str | None = None) -> list:
    results = []
    for uc in use_cases:
        results.append(run_use_case(uc, headed, slow_mo, replan, replan_step))
        time.sleep(3)  # polite to free-tier rate limit between cases
    return results


def run_parallel_pool(use_cases: list, headed: bool, slow_mo: int, workers: int,
                      replan: bool = False, replan_step: str | None = None) -> list:
    """Run use cases in parallel processes (true concurrent Playwright browsers)."""
    if not use_cases:
        return []
    if len(use_cases) == 1 or workers == 1:
        return [run_use_case(uc, headed, slow_mo, replan, replan_step) for uc in use_cases]

    n_workers = min(workers, len(use_cases))
    _log(f"Starting ProcessPoolExecutor with {n_workers} worker(s) ...")
    payloads = [
        {"index": i, "uc": uc, "headed": headed, "slow_mo": slow_mo,
         "replan": replan, "replan_step": replan_step}
        for i, uc in enumerate(use_cases)
    ]
    indexed: dict[int, dict] = {}
    with ProcessPoolExecutor(max_workers=n_workers) as pool:
        futures = [pool.submit(_run_indexed, p) for p in payloads]
        for fut in as_completed(futures):
            idx, result = fut.result()
            indexed[idx] = result
            _log(f"Finished: {result['use_case']}")
    return [indexed[i] for i in range(len(use_cases))]


def _run_file_worker(payload: dict) -> tuple[str, list]:
    """Picklable: run one YAML file's use cases sequentially inside a process."""
    path_name = payload["path_name"]
    use_cases = payload["use_cases"]
    headed = payload["headed"]
    slow_mo = payload["slow_mo"]
    replan = payload.get("replan", False)
    replan_step = payload.get("replan_step")
    _log(f"--- File: {path_name} ({len(use_cases)} use case(s), sequential in this process) ---")
    file_results = [run_use_case(uc, headed, slow_mo, replan, replan_step) for uc in use_cases]
    return path_name, file_results


def run_files_parallel(
    file_groups: list[tuple[Path, list]], headed: bool, slow_mo: int, workers: int,
    replan: bool = False, replan_step: str | None = None,
) -> list:
    """YAML files in parallel processes; use cases inside each file stay sequential."""
    active = [(path, ucs) for path, ucs in file_groups if ucs]
    if not active:
        return []
    if len(active) == 1 or workers == 1:
        results = []
        for path, ucs in active:
            _log(f"--- File: {path.name} ({len(ucs)} use case(s), sequential) ---")
            results.extend(run_use_case(uc, headed, slow_mo, replan, replan_step) for uc in ucs)
        return results

    n_workers = min(workers, len(active))
    _log(f"Starting ProcessPoolExecutor with {n_workers} file-worker(s) ...")
    payloads = [
        {
            "path_name": path.name,
            "use_cases": ucs,
            "headed": headed,
            "slow_mo": slow_mo,
            "replan": replan,
            "replan_step": replan_step,
        }
        for path, ucs in active
    ]
    by_name: dict[str, list] = {}
    with ProcessPoolExecutor(max_workers=n_workers) as pool:
        futures = [pool.submit(_run_file_worker, p) for p in payloads]
        for fut in as_completed(futures):
            name, file_results = fut.result()
            by_name[name] = file_results

    results = []
    for path, _ in active:
        results.extend(by_name.get(path.name, []))
    return results


def run_usecases_parallel(
    file_groups: list[tuple[Path, list]], headed: bool, slow_mo: int, workers: int,
    replan: bool = False, replan_step: str | None = None,
) -> list:
    """YAML files sequential; use cases within each YAML in parallel processes."""
    results = []
    for path, use_cases in file_groups:
        if not use_cases:
            continue
        _log(f"--- File: {path.name} ({len(use_cases)} use case(s), parallel) ---")
        results.extend(run_parallel_pool(use_cases, headed, slow_mo, workers, replan, replan_step))
    return results


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="SourceaAI — plan / execute / validate")
    ap.add_argument("--headed", action="store_true", help="run with visible browser")
    ap.add_argument("--only", help="run use cases by name prefix, e.g. UC2")
    ap.add_argument(
        "--parallel",
        choices=PARALLEL_MODES,
        default=None,
        help="parallel mode (overrides PARALLEL_MODE env). "
             "off=sequential, files=YAML files in parallel, "
             "usecases=tests within each YAML in parallel, all=flatten into one pool",
    )
    ap.add_argument(
        "--workers",
        type=int,
        default=None,
        help="max parallel workers (overrides PARALLEL_WORKERS env, default 2)",
    )
    ap.add_argument("--replan", action="store_true",
                    help="ignore plan memory and refresh every step")
    ap.add_argument("--replan-step", default=None, metavar="TEXT",
                    help="refresh one English step; other steps still use memory")
    args = ap.parse_args()

    mode, workers = resolve_parallel_config(args)
    slow_mo = int(os.getenv("SLOW_MO", "150"))

    file_groups = load_usecase_files()
    file_groups = [(p, filter_use_cases(ucs, args.only)) for p, ucs in file_groups]
    file_groups = [(p, ucs) for p, ucs in file_groups if ucs]
    if not file_groups:
        sys.exit(f"No use case matching '{args.only}'")

    all_ucs = [uc for _, ucs in file_groups for uc in ucs]
    mem_mode = os.getenv("PLAN_MEMORY", "on")
    print(
        f"Parallel mode: {mode} | workers: {workers} | plan memory: {mem_mode} | "
        f"files: {len(file_groups)} | use cases: {len(all_ucs)}"
    )

    REPORT_DIR.mkdir(exist_ok=True)

    if mode == "off":
        results = run_sequential(all_ucs, args.headed, slow_mo, args.replan, args.replan_step)
    elif mode == "files":
        results = run_files_parallel(
            file_groups, args.headed, slow_mo, workers, args.replan, args.replan_step
        )
    elif mode == "usecases":
        results = run_usecases_parallel(
            file_groups, args.headed, slow_mo, workers, args.replan, args.replan_step
        )
    else:  # all
        results = run_parallel_pool(
            all_ucs, args.headed, slow_mo, workers, args.replan, args.replan_step
        )

    (REPORT_DIR / "results.json").write_text(
        json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    md = render_markdown(results)
    (REPORT_DIR / "report.md").write_text(md, encoding="utf-8")
    passed, failed_n = verdict_counts(results)
    print(f"\nPassed: {passed} | Failed: {failed_n} | Total: {len(results)}")
    print(f"Done. Reports in {REPORT_DIR}/ (report.md, results.json, screenshots)")
    sys.exit(1 if failed_n else 0)


if __name__ == "__main__":
    main()
