# SourceaAI — LLM-Driven Test Automation with NVIDIA Build (NIM)

Natural-language test cases -> AI-generated Playwright plans -> live execution
with AI self-healing locators -> AI semantic validation -> report.

```
usecases/*.yaml (plain English)
        │
        ▼
┌─ Planner (NIM LLM) ─────────┐   converts English steps to a JSON action plan
└─────────────┬───────────────┘
              ▼
┌─ Executor (Playwright) ─────┐   runs the plan; on selector failure sends the
│  + Self-Healing (NIM LLM)   │   live DOM to the LLM and retries with the fix
└─────────────┬───────────────┘
              ▼
┌─ Validator (NIM LLM) ───────┐   semantic assertions: "are results RELEVANT?",
└─────────────┬───────────────┘   "are prices actually ascending?"
              ▼
reports/report.md + results.json + screenshots
```

## Why this architecture

- **Semantic assertions** — classic automation can assert `count > 5`; it cannot
  assert "results are relevant to 'wireless mouse'". The validator LLM can.
- **Self-healing** — when Amazon changes a locator, the executor ships the DOM
  to the LLM and retries. Healing events are logged in the report (auditable).
- **Provider-agnostic** — OpenAI-compatible client. Same code runs against
  NIM (hosted, free tier) or Ollama (air-gapped on-prem) by changing 2 env vars.

## Setup (10 minutes)

```bash
# 1. Get a free API key at https://build.nvidia.com  (no credit card)

# 2. Configure via .env (loaded automatically)
cp .env.example .env          # then paste your nvapi- key into .env

# 3. Install
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium

# 4. Run
python run_poc.py                # all use cases, headless (sequential)
python run_poc.py --headed       # watch the browser (better for demos)
python run_poc.py --only UC2     # single use case
python run_poc.py --parallel usecases --workers 3   # tests within each YAML in parallel
python run_poc.py --parallel files                  # YAML files in parallel
python run_poc.py --parallel all --workers 2        # all use cases in one pool
```

`.env` knobs: `NVIDIA_API_KEY`, `NIM_MODEL`, `NIM_BASE_URL`, `HEADLESS`,
`SLOW_MO` (set `SLOW_MO=500` for stakeholder demos so actions are visible),
`PARALLEL_MODE` (`off` / `files` / `usecases` / `all`), `PARALLEL_WORKERS`.
CLI `--parallel` / `--workers` override the env values. Real environment
variables override `.env`. Never commit `.env` — it is already in `.gitignore`.

### Switch to fully local (air-gapped) mode

Edit `.env`:

```bash
NIM_BASE_URL=http://localhost:11434/v1
NIM_MODEL=qwen2.5:3b
# NVIDIA_API_KEY not needed
```

## The 3 use cases (`usecases/amazon_usecases.yaml`)

| # | Use case | AI validation performed |
|---|----------|------------------------|
| UC1 | Product search | ≥70% of result titles semantically relevant to the query |
| UC2 | Sort by price low→high | Prices parsed from `₹x,xxx` strings and checked ascending |
| UC3 | Add to cart | Product is Kindle-related AND cart badge incremented |

Add a new test = add a YAML block in plain English. No code.

## Known constraints (say these in the demo before anyone asks)

1. **Amazon bot detection** — Amazon may serve a CAPTCHA to automated browsers.
   Mitigations already in the code: real user-agent, viewport, slow_mo pacing.
   If a run hits CAPTCHA, use `--headed`, solve it once, re-run. For a client
   POC, point the same framework at the client's own web app instead — this is
   a framework demo, Amazon is just a familiar stand-in.
2. **NIM free tier** — ~40 requests/min; the runner spaces calls and backs off
   on 429. Free tier is licensed for dev/test/eval only, NOT production traffic.
3. **Data hygiene** — never send client data through the hosted endpoint;
   the Ollama switch above is the answer to data-residency questions.
4. **Determinism** — planner temperature is 0.1; generated plans are saved to
   `reports/*_plan.json` so a reviewed plan can be replayed/frozen later.

## CI / GitHub Actions

Workflow at `.github/workflows/openqa-poc.yml`:

- **Triggers:** manual run from the Actions tab (with a UC1/UC2/UC3/all input)
  and a weekday 09:00 IST schedule. Deliberately NOT on push/PR — each run
  spends free-tier LLM quota and scrapes Amazon.
- **Secret required:** repo → Settings → Secrets and variables → Actions →
  new secret `NVIDIA_API_KEY` (the workflow injects it; `.env` is not used in CI).
- **Outputs:** `report.md` is rendered on the run's Summary page; report,
  results.json, plans, and screenshots are uploaded as an artifact even when
  the run fails; the job exits red if any use case failed.
- **Expectation:** GitHub datacenter IPs get CAPTCHA'd by Amazon more often
  than your laptop — intermittent CI failures on Amazon are normal. The CI leg
  proves the pipeline pattern; the reliable target is a client's own app.

## Suggested model choices on build.nvidia.com

| Purpose | Model | Why |
|---|---|---|
| Default (fast) | `meta/llama-3.2-11b-vision-instruct` | quick planning; free-tier friendly |
| Stronger reasoning | `nvidia/nemotron-3-nano-30b-a3b` | better structured output when Flash is enough |
| Heavy / slow | `deepseek-ai/deepseek-v4-pro-0813` | strong quality but can take 1–2+ min per call |

Confirm IDs on [build.nvidia.com](https://build.nvidia.com) — catalog churn is common; a listed model can still 404 until enabled for your key.
