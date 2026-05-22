"""Compare Veloce against Flask + FastAPI on a workload.

Per the project's Phase-3 contract a feature is not "done" until Veloce
beats both Flask and FastAPI on the median of >=3 runs. This runner
takes a workload name (each maps to one app module per framework), runs
each framework `runs` times in randomised order to avoid order bias, and
prints a comparison table.

Usage:
    python -m bench.comparative.bench <workload> [--runs N] [--duration S]
                                                 [--concurrency C]
Workloads:
    json-hello — GET / returning {"hello": "world"}
"""

from __future__ import annotations

import argparse
import asyncio
import random
import statistics
import sys
from dataclasses import dataclass

from bench.comparative.runner import RunResult, measure

# workload -> (request_path, {framework: module_path})
WORKLOADS: dict[str, tuple[str, dict[str, str]]] = {
    "json-hello": (
        "/",
        {
            "veloce": "bench.comparative.apps.veloce_json",
            "fastapi": "bench.comparative.apps.fastapi_json",
            "flask": "bench.comparative.apps.flask_json",
        },
    ),
    "path-param": (
        "/items/42",
        {
            "veloce": "bench.comparative.apps.veloce_path",
            "fastapi": "bench.comparative.apps.fastapi_path",
            "flask": "bench.comparative.apps.flask_path",
        },
    ),
}


@dataclass
class Summary:
    framework: str
    rps_median: float
    p50_us_median: float
    p99_us_median: float
    runs: int
    errors: int


def _summarise(results: list[RunResult]) -> Summary:
    return Summary(
        framework=results[0].framework,
        rps_median=statistics.median(r.rps for r in results),
        p50_us_median=statistics.median(r.p50_us for r in results),
        p99_us_median=statistics.median(r.p99_us for r in results),
        runs=len(results),
        errors=sum(r.errors for r in results),
    )


async def run_workload(
    workload: str,
    runs: int,
    duration_s: float,
    concurrency: int,
) -> list[Summary]:
    if workload not in WORKLOADS:
        raise SystemExit(f"unknown workload {workload!r}; known: {', '.join(WORKLOADS)}")
    path, frameworks = WORKLOADS[workload]
    # Randomise run order so a warmup advantage cannot accrue to one framework.
    schedule: list[tuple[str, str]] = []
    for _ in range(runs):
        items = list(frameworks.items())
        random.shuffle(items)
        schedule.extend(items)

    by_framework: dict[str, list[RunResult]] = {fw: [] for fw in frameworks}
    for framework, module in schedule:
        result = await measure(
            framework,
            module,
            duration_s=duration_s,
            concurrency=concurrency,
            path=path,
        )
        by_framework[framework].append(result)
        print(
            f"  [{framework:>8}] run {len(by_framework[framework])}/{runs}: "
            f"{result.rps:>9,.0f} rps  p50={result.p50_us:>6.0f} us  "
            f"p99={result.p99_us:>6.0f} us  errors={result.errors}",
            flush=True,
        )

    return [_summarise(by_framework[fw]) for fw in frameworks]


def print_table(workload: str, summaries: list[Summary]) -> None:
    print()
    print(f"== workload: {workload} ==")
    print(f"{'framework':<10} {'rps (med)':>12} {'p50 us':>10} {'p99 us':>10} {'errors':>8}")
    for s in summaries:
        print(
            f"{s.framework:<10} {s.rps_median:>12,.0f} {s.p50_us_median:>10.0f} "
            f"{s.p99_us_median:>10.0f} {s.errors:>8}"
        )
    print()
    veloce = next((s for s in summaries if s.framework == "veloce"), None)
    if veloce is None:
        return
    rivals = [s for s in summaries if s.framework != "veloce"]
    losses_rps = [s.framework for s in rivals if s.rps_median > veloce.rps_median]
    losses_p99 = [s.framework for s in rivals if s.p99_us_median < veloce.p99_us_median]
    if losses_rps or losses_p99:
        print("Veloce does NOT beat:")
        if losses_rps:
            print(f"  rps  — {', '.join(losses_rps)}")
        if losses_p99:
            print(f"  p99  — {', '.join(losses_p99)}")
    else:
        print("Veloce beats both rivals on rps and p99 (median).")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workload", choices=sorted(WORKLOADS))
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--duration", type=float, default=4.0)
    # The in-process httpx + asyncio load generator saturates around
    # ~12 concurrent connections on Windows with the stdlib event loop;
    # beyond that the *client* becomes the bottleneck and all
    # frameworks look identical. 8 is well under that ceiling and gives
    # a discriminating server-side measurement; raise it only if you
    # know your client can drive harder load.
    parser.add_argument("--concurrency", type=int, default=8)
    args = parser.parse_args()
    summaries = asyncio.run(run_workload(args.workload, args.runs, args.duration, args.concurrency))
    print_table(args.workload, summaries)
    return 0


if __name__ == "__main__":
    sys.exit(main())
