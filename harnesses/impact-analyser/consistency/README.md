# Consistency Harness — Impact Analyser

A standalone **multi-agent harness** that drives the running Impact Analyser over
its HTTP API, runs the **same change request through the analyser N times**, and
scores how **consistent** the resulting impact reports are across runs.

The analyser pipeline is LLM-driven, so the same CR can yield different impact
reports on each run. This harness quantifies that drift — replacing the slow,
manual "set up → click Analyse → eyeball one report" loop with an automated,
repeatable consistency benchmark.

It never imports application internals. It speaks HTTP only, to:

| Service | Default URL | Used for |
|---|---|---|
| `agent-bus-api` | `http://localhost:8000` | submit + poll the analysis (`/analyse/cr`) |
| `integration-service` | `http://localhost:3001` | fetch a CR + its tests from ADO |

---

## How it works

Three cooperating agents, wired by a plain sequential orchestrator:

```
SelectorAgent  ──▶  RunnerAgent  ──▶  EvaluatorAgent  ──▶  artifacts
  pick + freeze       submit the same      score consistency     selected_cr.json
  the CR + its        frozen CR N times,   across the N           raw/run_NN.json
  existing tests      poll each to done    reports               consistency_report.json
                                                                  summary_<run_id>.md
```

### The two things that make the measurement valid

1. **Cache bypass.** The analyser keys a response cache on
   `(cr_id, title, description, acceptance_criteria)`. Re-submitting the same CR
   would return the first run's stored report verbatim (`cache_hit: true`) and
   measure nothing. We verified that **`cr_id` never reaches any LLM prompt** —
   it's only an identifier. So the RunnerAgent appends a **random 8-character hex
   nonce** to the `cr_id` (e.g. `152#a3f9c12b`) while keeping
   title/description/acceptance-criteria byte-identical. Each run is a genuine
   cache miss; the LLM sees identical input. This also prevents stale runs from
   a previous session sharing the same cache key.

2. **Frozen input.** The SelectorAgent fetches/loads the CR and its existing tests
   **once**, then the RunnerAgent feeds that same object to every run. The
   analyser scores no tests unless `existing_tests` is supplied, so the same
   test set must be fed every time or the comparison is meaningless.

### Consistency dimensions

Each dimension reduces to a score in **[0, 1]** (1 = identical across runs). They
blend into one headline number using configurable weights — every component is
kept in the output so the score is explainable, never a black box.

Dimensions are displayed in weight order (highest to lowest) in the report.

| # | Dimension | What it measures | Default weight |
|---|---|---|---|
| 1 | `test_set_stability` | Are the same tests chosen every run? | 0.30 |
| 2 | `action_agreement` | Does each test get the same Must/Should/Consider/Exclude every run? | 0.25 |
| 3 | `coverage_gap_stability` | Are the same gaps in test coverage identified every run? | 0.15 |
| 4 | `generated_test_case_consistency` | Are a consistent number of new test cases generated, covering the same topics? | 0.11 |
| 5 | `risk_agreement` | Is the overall risk rating (Critical/High/Medium/Low) the same every run? | 0.09 |
| 6 | `ranking_stability` | Does the overall order of all tests stay the same across runs? | 0.06 |
| 7 | `score_stability` | Is the LLM's relevance score for each test consistent? Score drift near a threshold can cause the action to flip. | 0.03 |
| 8 | `summary_stability` | Are the must-run, should-run and exclude counts consistent across runs? | 0.01 |

A dimension that's undefined for the data is **dropped from the blend** and its
weight removed from the denominator — never scored as a misleading 0 or 1.

**Score bands:** ≥0.90 Excellent · ≥0.75 Good · ≥0.50 Fair · else Poor

---

## Batch mode

To run multiple CRs in one go, add a `batch` array to `config.json`. Each entry
inherits all fields from `selector` and only needs to specify what differs
(typically just `cr_id`):

```json
{
  "selector": {
    "mode": "ado",
    "source": "ado",
    "test_case_tags": ["Regression"],
    "integration_url": "https://dev.azure.com/your-org",
    "integration_project": "YourProject",
    "integration_token": "<PAT>"
  },
  "batch": [
    { "cr_id": "148" },
    { "cr_id": "149" },
    { "cr_id": "150" }
  ],
  "runner": { "iterations": 50 }
}
```

A `batch_summary.md` is written alongside the per-CR reports with a combined
results table and per-dimension breakdown across all CRs.

---

## Runbook — running it against the live app

### 1. Bring the analyser up

From the repo root:

```bash
cp .env.example .env
# set OPENAI_API_KEY (or another provider)
docker compose down -v              # wipe DB for a clean slate
docker compose build --no-cache
docker compose up -d
pnpm db:seed
```

All 6 services must be healthy: `web` (3000), `agent_bus_api` (8000),
`integration_service` (3001), `reranker` (8001), `postgres` (5432), `elasticmq` (9324).

After a fresh DB, log into the UI at http://localhost:3000 with `admin@stlc.local` /
`Admin1234` and re-save the ADO integration config (Settings → Integrations).

### 2. Install the harness deps

```bash
cd testing/consistency-harness
pip install -r requirements.txt
```

### 3. Configure

```bash
cp config.example.json config.json     # config.json is gitignored
```

Edit `config.json`. Two ways to supply the CR:

**ADO (live — recommended):**
```json
"selector": {
  "mode": "ado",
  "cr_id": "152",
  "source": "ado",
  "test_case_tags": ["Regression"],
  "integration_url": "https://dev.azure.com/your-org",
  "integration_project": "YourProject",
  "integration_token": "<PAT with Work Items: Read>"
}
```

**Inline (most reproducible):**
```json
"selector": { "mode": "inline", "cr_file": "testing/consistency-harness/examples/sample_cr.json" }
```

### 4. Run

Always run from the **repo root** so the output path resolves correctly:

```bash
cd /path/to/agentic-ai-stlc
caffeinate -i python3 -m harness.cli --config testing/consistency-harness/config.json
```

`caffeinate -i` prevents the laptop sleeping mid-run. For long batch runs
(10 CRs × 50 iterations) keep the laptop plugged in.

### 5. Read the results

Artifacts land in `consistency-harness/runs/<timestamp>/`:

- **`summary_<run_id>.md`** — start here: headline score, dimensions table,
  AI-written narrative, classification & summary, recommended improvements.
- `consistency_report.json` — same data, machine-readable.
- `raw/run_NN.json` — each individual run's full impact report.
- `selected_cr.json` — the exact frozen input fed to every run.

For batch runs, each CR gets its own subfolder (`cr_148/`, `cr_149/`, …) plus a
`batch_summary.md` at the top level.

### AI-generated narrative and improvements

The report includes:
- A plain-English **Analysis** paragraph interpreting the scores
- A **Recommended Improvements** section listing concrete actions for any
  dimension scoring below 0.75

Both are generated automatically using the **Kilo AI free tier** — no account or
API key required (`KILO_API_KEY=anonymous` by default).

To disable in CI or air-gapped environments:
```bash
KILO_API_KEY=off python3 -m harness.cli --config testing/consistency-harness/config.json
```

### ADO PAT tokens

PAT tokens expire regularly. If the CR search or integration fails, generate a
new PAT in ADO (Work Items: Read scope) and update:
1. `integration_token` in `config.json`
2. `ADO_TOKEN` in `.env`
3. Restart the integration service: `docker compose up -d --force-recreate integration_service`
4. Re-save the integration config in the UI (Settings → Integrations)

---

## Developing / testing the harness itself

The whole agent stack is exercised under pytest using an `httpx.MockTransport`
fake — no app, DB, or LLM required:

```bash
cd testing/consistency-harness
pip install -r requirements.txt
python -m pytest -q
```

---

## Layout

```
consistency-harness/
├── harness/
│   ├── config.py          dataclass config + JSON/env loading
│   ├── client.py          async HTTP client for both services
│   ├── metrics.py         pure-Python stats (Jaccard, stdev, Spearman, …)
│   ├── narrative.py       Kilo AI narrative + improvements + TC topic similarity
│   ├── report.py          Markdown summary renderer (sorted by weight)
│   ├── orchestrator.py    wires the agents, writes artifacts, batch support
│   ├── cli.py             `python3 -m harness.cli`
│   └── agents/
│       ├── base.py        shared types (SelectedCR, etc.)
│       ├── selector.py    obtain + freeze the CR (ADO or inline)
│       ├── runner.py      submit N times + poll (random nonce cache bypass)
│       └── evaluator.py   score all 8 consistency dimensions
├── tests/                 fakes-based suite (no app needed)
├── examples/sample_cr.json
├── config.example.json
└── requirements.txt
```

---

## Planned extensions

### Pipeline Failure RCA mode

A second `analyser_type` is planned to test consistency of the Pipeline Failure
RCA feature using the same infrastructure. It will be enabled via:

```json
{ "analyser_type": "pipeline_rca" }
```

RCA-specific dimensions will measure whether the same root cause, failure type,
severity, blast radius, and fix recommendations are produced across runs.
The orchestrator, runner, report renderer, and narrative generation are all
reused unchanged — only the selector agent and evaluator dimensions differ.
