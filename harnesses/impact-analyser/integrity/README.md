# Integrity Harness

Validates that the AI test impact analyser does **not hallucinate impact** for change requests that are known to be low-risk or have no application impact.

## What it does

The harness runs a known low-impact CR through the analyser N times and checks three thresholds on every run:

| Check | Threshold | Why |
|---|---|---|
| `overall_risk` | Must be `Low` | A zero/low-impact change should never produce High or Critical risk |
| `recommended_action` | No `Must Run` or `Should Run` | No regression tests should be flagged for something with no application impact |
| Generated test cases | ≤ 1 | A trivial or documentation-only change needs no new tests |

Each run is marked **PASS** or **FAIL**. The overall **integrity score** is `passed_runs / total_runs`. A score of 1.0 means the analyser correctly handled the low-impact CR every time.

## When to use it

- After any change to the scoring prompts — to confirm the fix didn't introduce over-inflation
- After upgrading the LLM model — to verify the new model doesn't hallucinate impact
- As a smoke test before a release — to confirm the analyser is behaving sensibly on known-safe CRs

## Prerequisites

- Docker stack running (`docker compose up -d`)
- Python 3.10+
- `httpx` installed (`pip3 install httpx`)
- ADO credentials configured (either in `config.json` or as environment variables)

## How to run

From the **repo root**:

```bash
python3 testing/integrity-harness/run_integrity.py
```

With a custom number of runs:

```bash
python3 testing/integrity-harness/run_integrity.py --iterations 10
```

With a custom config file:

```bash
python3 testing/integrity-harness/run_integrity.py --config testing/integrity-harness/config.json
```

## Output

The harness prints a summary table to the terminal and writes two files to `integrity-harness/runs/<timestamp>/`:

- `integrity_report.json` — machine-readable results for every run
- `summary.txt` — human-readable pass/fail table

Example output:

```
============================================================
  INTEGRITY HARNESS REPORT
============================================================
  CR:              160
  Runs:            5
  Passed:          4
  Failed:          1
  Integrity score: 0.800  [Good]

  Per-run results
  Run   Status   Risk       Must/Should   Gen TCs   Time
  --------------------------------------------------------
  1     PASS     Low        0             1         30.3s
  2     PASS     Low        0             1         27.1s
  3     PASS     Low        0             1         27.1s
  4     FAIL     Low        0             2         24.1s
  5     PASS     Low        0             1         30.1s
============================================================
```

## Configuration

Edit `testing/integrity-harness/config.json` to change the CR under test or adjust thresholds:

```json
{
  "selector": {
    "cr_id": "160",
    "mode": "ado"
  },
  "runner": {
    "iterations": 5
  },
  "thresholds": {
    "allowed_overall_risk": ["Low"],
    "forbidden_actions": ["Must Run", "Should Run"],
    "max_generated_test_cases": 1
  }
}
```

### ADO credentials

ADO credentials can be set in `config.json` under `selector`, or overridden via environment variables:

```bash
export ADO_BASE_URL=https://dev.azure.com/<your-org>
export ADO_PROJECT_KEY=<your-project>
export ADO_TOKEN=<your-pat-token>
```

## Current test CRs

| CR | ADO ID | Description | Why it's a good integrity test |
|---|---|---|---|
| CR-013 | 160 | Update Internal CR Numbering Convention Documentation | Documentation-only change — explicitly states no application impact |
| CR-011 | 158 | Correct Hint Text Wording on Step 2 Postcode Field | Copy-only change — no validation, data model, or structural changes |

## Interpreting results

| Integrity score | Band | Meaning |
|---|---|---|
| 1.000 | Excellent | Analyser correctly handles the low-impact CR every run |
| 0.800+ | Good | Minor variance in generated test case count — risk scoring is correct |
| 0.600–0.799 | Fair | Occasional over-inflation — investigate which threshold is failing |
| < 0.600 | Poor | Consistent hallucination of impact — scoring prompts need attention |

### Known findings

**CR-011 (ADO 158)** — Currently fails with integrity score 0.000. The analyser assigns `overall_risk = High` on every run due to TC-005 ("Validation - Step 2: Contact Information") scoring `relevance=0.90, dependency=0.90`. Root cause: the dependency score OR clause fires on structural proximity without distinguishing between a structural change (adding a field) and a value change (updating hint text wording). See `docs/consistency-factors.md` for the full investigation.

**CR-013 (ADO 160)** — Passes on risk every run (`overall_risk = Low`). Fails occasionally on generated test case count (1–3 per run). The TCG generates documentation review tests rather than application tests, which is arguably correct behaviour but exceeds the threshold.
