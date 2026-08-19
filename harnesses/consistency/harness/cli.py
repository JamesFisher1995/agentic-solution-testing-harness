"""Command-line entry point.

Examples
--------
    # health-check the analyser API before a real run
    python -m harness.cli --config config.json --check-health

    # inline CR, 5 iterations (config supplies the cr_file)
    python -m harness.cli --config config.json

    # override iterations and target a remote analyser
    python -m harness.cli --config config.json --iterations 10 \
        --agent-url http://10.0.0.5:8000
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

# Add repo root to path so shared/ and harness package imports resolve
_HARNESS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_HARNESS_DIR)))
sys.path.insert(0, _REPO_ROOT)

from shared.client import SolutionClient  # noqa: E402
from shared.dotenv import load_dotenv  # noqa: E402
from harnesses.consistency.harness.config import Config  # noqa: E402
from harnesses.consistency.harness.orchestrator import Orchestrator  # noqa: E402

load_dotenv(os.path.join(_REPO_ROOT, ".env"))


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="consistency-harness", description=__doc__)
    p.add_argument("--config", help="path to a JSON config file")
    p.add_argument("--iterations", type=int, help="override runner.iterations")
    p.add_argument("--agent-url", help="override agent-bus-api base URL")
    p.add_argument("--integration-url", help="override integration-service base URL")
    p.add_argument("--tenant-id", help="override tenant_id")
    p.add_argument("--cr-file", help="override selector.cr_file (implies inline mode)")
    p.add_argument("--output-dir", help="override output directory")
    p.add_argument("--no-cache-bypass", action="store_true",
                   help="submit identical cr_id every run (will hit the cache)")
    p.add_argument("--check-health", action="store_true",
                   help="probe the analyser /health and exit")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def _apply_overrides(cfg: Config, args: argparse.Namespace) -> None:
    if args.iterations is not None:
        cfg.runner.iterations = args.iterations
    if args.agent_url:
        cfg.agent_base_url = args.agent_url
    if args.integration_url:
        cfg.integration_base_url = args.integration_url
    if args.tenant_id:
        cfg.tenant_id = args.tenant_id
    if args.output_dir:
        cfg.output_dir = args.output_dir
    if args.cr_file:
        cfg.selector.mode = "inline"
        cfg.selector.cr_file = args.cr_file
    if args.no_cache_bypass:
        cfg.runner.bypass_cache = False


async def _check_health(cfg: Config) -> int:
    async with SolutionClient(cfg.agent_base_url, cfg.integration_base_url, cfg.tenant_id) as c:
        try:
            res = await c.health()
            print(f"OK  {cfg.agent_base_url}/health -> {res}")
            return 0
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL {cfg.agent_base_url}/health -> {type(exc).__name__}: {exc}")
            return 1


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    cfg = Config.load(args.config)
    _apply_overrides(cfg, args)

    if args.check_health:
        return asyncio.run(_check_health(cfg))

    orch = Orchestrator(cfg)

    if cfg.batch:
        try:
            outcomes, batch_dir = asyncio.run(orch.run_batch())
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("harness").error("batch run failed: %s", exc)
            return 2
        print(f"\nBatch complete — {len(outcomes)}/{len(cfg.batch)} CRs succeeded")
        for report, cr_dir in outcomes:
            score_s = f"{report.overall_score:.3f} ({report.band})" if report.sufficient else "insufficient data"
            print(f"  {cr_dir}: {score_s}")
        print(f"\nBatch artifacts: {batch_dir}")
        print(f"  -> {batch_dir}/batch_summary.md")
        return 0 if outcomes else 3

    try:
        report, out_dir = asyncio.run(orch.run())
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("harness").error("run failed: %s", exc)
        return 2

    if report.sufficient:
        print(f"\nOverall consistency: {report.overall_score:.3f} ({report.band})")
    else:
        print("\nInsufficient successful runs to score consistency "
              f"({report.runs_evaluated}/{report.runs_total}).")
    print(f"Artifacts: {out_dir}")
    print(f"  -> {out_dir}/summary.md")
    return 0 if report.runs_evaluated >= 1 else 3


if __name__ == "__main__":
    raise SystemExit(main())
