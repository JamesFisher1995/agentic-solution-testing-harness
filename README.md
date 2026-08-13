# Agentic Solution Testing Harness

A framework for measuring the quality of AI-powered analysis tools across four dimensions: **Consistency**, **Integrity**, **Completeness**, and **Reliability**.

Originally built to validate an AI test impact analyser, the harnesses are solution-agnostic — any tool that accepts a change request and returns structured analysis can be tested by configuring the endpoint paths and response field names.

---

## The Four Harnesses

| Harness | Question it answers |
|---|---|
| **Consistency** | Does the AI give the same answer if you run the same input twice? |
| **Integrity** | Does the AI stay calm when there is nothing to find — no hallucinated impact? |
| **Completeness** | When there IS a real impact, does the AI find all of it? |
| **Reliability** | If a real defect exists, does the AI flag the right tests to catch it? |

---

## Quick Start

```bash
# 1. Copy the example env and fill in your solution's URL
cp .env.example .env

# 2. Copy a harness config and fill in your settings
cp harnesses/consistency/config.example.json harnesses/consistency/config.json

# 3. Run a harness
python3 harnesses/consistency/harness/cli.py --config harnesses/consistency/config.json
python3 harnesses/integrity/run.py --config harnesses/integrity/config.json
python3 harnesses/reliability/run.py --config harnesses/reliability/config.json
python3 harnesses/completeness/run.py --config harnesses/completeness/config.json
```

Results are written to the `runs/` directory (gitignored by default).

---

## Adapting to Your Solution

Two things need configuring to point the harnesses at a different solution:

### 1. Endpoint paths

Add an `endpoints` block to your config if your solution uses different API paths:

```json
"endpoints": {
  "submit": "/api/analyse",
  "poll": "/api/analyse/{run_id}",
  "delete": "/api/analyse/{run_id}"
}
```

### 2. Response field names

Add a `response_schema` block to map the harnesses' generic field names to your solution's actual JSON field names:

```json
"response_schema": {
  "tests_field": "test_cases",
  "test_id_field": "test_id",
  "action_field": "recommended_action",
  "score_field": "composite_score",
  "risk_field": "overall_risk",
  "gaps_field": "coverage_gaps",
  "gap_component_field": "component",
  "summary_field": "summary"
}
```

The defaults shown above match the agentic-ai-stlc impact analyser. Only override what differs.

---

## Harness Details

### Consistency (`harnesses/consistency/`)

Runs the same input N times and measures how stable the outputs are across eight weighted dimensions (test set stability, action agreement, score stability, ranking stability, risk agreement, summary stability, coverage gap stability, generated TC consistency). A weighted score is calculated and banded as Excellent / Good / Fair / Poor.

**Pass threshold:** ≥ 0.90 (configurable via evaluator weights)

### Integrity (`harnesses/integrity/`)

Verifies the AI does not hallucinate impact for low-impact changes, and correctly identifies high-impact changes. Each CR is given a type (`low_impact` or `high_impact`) and a set of checks.

**Low-impact checks:** risk must be Low, Must/Should Run count must be ≤ threshold, generated TCs must be ≤ threshold.  
**High-impact checks:** risk must be Critical/High, no Exclude actions on key tests.

### Reliability (`harnesses/reliability/`)

Defines inline defect scenarios — a simulated broken change request — and asserts that the AI flags the correct tests as Must Run. No external system needed; the CR is defined inline in the config.

**Pass threshold:** 100% detection rate across all scenarios.

### Completeness (`harnesses/completeness/`)

Defines a gold standard per CR: which tests must be flagged, which must never be excluded, and which coverage gaps must be identified (each requiring a generated test case). Three mechanical checks per run:

1. **Test recall** — were all must-flag tests recommended?
2. **False exclusions** — were any must-not-exclude tests wrongly excluded?
3. **Gap recall** — were all gold-standard coverage gaps identified, each with a generated TC?

**Pass thresholds:** recall ≥ 90%, false exclusions = 0, gap recall ≥ 80% (all configurable).

---

## Project Structure

```
.
├── shared/
│   ├── client.py       # Generic HTTP client — endpoint paths configurable
│   └── dotenv.py       # Minimal .env loader for running outside Docker
├── harnesses/
│   ├── consistency/
│   │   ├── config.example.json
│   │   └── harness/    # Multi-agent package (Selector → Runner → Evaluator)
│   ├── integrity/
│   │   ├── config.example.json
│   │   └── run.py
│   ├── reliability/
│   │   ├── config.example.json
│   │   └── run.py
│   └── completeness/
│       ├── config.example.json
│       └── run.py
└── runs/               # Output (gitignored)
```

---

## Dependencies

```
httpx
```

Install with: `pip install httpx`

The consistency harness also uses only the standard library beyond httpx. The integrity, reliability, and completeness harnesses are single-file scripts with no additional dependencies.
