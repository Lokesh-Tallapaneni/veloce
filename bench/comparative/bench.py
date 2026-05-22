"""Compare Veloce against Flask + FastAPI on a workload.

Each workload maps to one tiny app per framework; the orchestrator runs
all three under the same single-worker uvicorn in randomised order,
discards a leading cold-cache round, and prints a median table.

This is the harness used to track Veloce's Phase-3 performance picture.
The picture is per-metric and per-rival rather than monolithic — for
example, synchronous Flask under `asgiref.WsgiToAsgi` has a structurally
lower p50/p99 at low concurrency, so the Phase-3 rule of thumb is read
as "Veloce wins the metrics that matter for the deployment Veloce
targets" rather than "Veloce wins every column in every row". See
`docs/bench/README.md` "Caveats" for the deployment model and how to
read the recorded numbers.

Usage:
    python -m bench.comparative.bench <workload> [--runs N] [--duration S]
                                                 [--concurrency C] [--seed N]
Workloads:
    json-hello — GET / returning {"hello": "world"}
    path-param — GET /items/{id} returning {"id": id, "name": "item-id"}
"""

from __future__ import annotations

import argparse
import asyncio
import random
import statistics
import sys
from dataclasses import dataclass

from bench.comparative.runner import RunResult, measure


@dataclass(frozen=True)
class Workload:
    """One bench workload — three apps + the URL path + a body substring.

    `expected_substring`, if set, must appear in the first ready
    response's body. The readiness probe checks this once per run and
    raises before any timed window starts. A *substring* (not byte-exact
    equality) catches gross misbehaviour — a 200 + empty body, or wrong
    payload — without tripping on the three frameworks' slightly
    different JSON whitespace / key ordering.
    """

    path: str
    apps: dict[str, str]
    expected_substring: bytes | None


# workload -> Workload
WORKLOADS: dict[str, Workload] = {
    "json-hello": Workload(
        path="/",
        apps={
            "veloce": "bench.comparative.apps.veloce_json",
            "fastapi": "bench.comparative.apps.fastapi_json",
            "flask": "bench.comparative.apps.flask_json",
        },
        expected_substring=b'"hello"',
    ),
    "path-param": Workload(
        path="/items/42",
        apps={
            "veloce": "bench.comparative.apps.veloce_path",
            "fastapi": "bench.comparative.apps.fastapi_path",
            "flask": "bench.comparative.apps.flask_path",
        },
        expected_substring=b"item-42",
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
    rng: random.Random,
    discard_cold_round: bool,
) -> list[Summary]:
    if workload not in WORKLOADS:
        raise SystemExit(f"unknown workload {workload!r}; known: {', '.join(WORKLOADS)}")
    spec = WORKLOADS[workload]
    # An extra leading round whose results are discarded — its purpose
    # is only to fill the OS file cache, Python bytecode cache, kernel
    # TCP cache, etc. so the first *measured* round does not pay the
    # cold-start tax. The per-round shuffle does not fix this on its
    # own: whichever framework lands first in the very first round
    # still eats the cold cache.
    measured_rounds = runs
    total_rounds = runs + (1 if discard_cold_round else 0)
    schedule: list[tuple[str, str, bool]] = []  # (fw, module, measured?)
    for r in range(total_rounds):
        items = list(spec.apps.items())
        rng.shuffle(items)
        measured = not (discard_cold_round and r == 0)
        schedule.extend((fw, mod, measured) for fw, mod in items)

    by_framework: dict[str, list[RunResult]] = {fw: [] for fw in spec.apps}
    for framework, module, measured in schedule:
        result = await measure(
            framework,
            module,
            duration_s=duration_s,
            concurrency=concurrency,
            path=spec.path,
            expected_substring=spec.expected_substring,
        )
        if not measured:
            print(
                f"  [{framework:>8}] cold-cache round (discarded): "
                f"{result.rps:>9,.0f} rps  p50={result.p50_us:>6.0f} us  "
                f"p99={result.p99_us:>6.0f} us  errors={result.errors}",
                flush=True,
            )
            continue
        by_framework[framework].append(result)
        print(
            f"  [{framework:>8}] run {len(by_framework[framework])}/{measured_rounds}: "
            f"{result.rps:>9,.0f} rps  p50={result.p50_us:>6.0f} us  "
            f"p99={result.p99_us:>6.0f} us  errors={result.errors}",
            flush=True,
        )

    return [_summarise(by_framework[fw]) for fw in spec.apps]


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
    # Defaults match the methodology recorded in `docs/bench/<workload>.md`
    # so a bare invocation reproduces those numbers without surprises.
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--duration", type=float, default=5.0)
    # The in-process httpx + asyncio load generator saturates around
    # ~12 concurrent connections on Windows with the stdlib event loop;
    # beyond that the *client* becomes the bottleneck and all
    # frameworks look identical. 8 is well under that ceiling and gives
    # a discriminating server-side measurement; raise it only if you
    # know your client can drive harder load.
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="seed for the run-order shuffle; if unset, a random seed is "
        "picked and printed for traceability",
    )
    parser.add_argument(
        "--no-cold-round",
        dest="discard_cold_round",
        action="store_false",
        help="skip the extra leading cold-cache round (default: discard it)",
    )
    parser.set_defaults(discard_cold_round=True)
    args = parser.parse_args()
    seed = args.seed if args.seed is not None else random.SystemRandom().randint(0, 2**31 - 1)
    print(f"seed: {seed}", flush=True)
    rng = random.Random(seed)
    summaries = asyncio.run(
        run_workload(
            args.workload,
            args.runs,
            args.duration,
            args.concurrency,
            rng,
            args.discard_cold_round,
        )
    )
    print_table(args.workload, summaries)
    return 0


if __name__ == "__main__":
    sys.exit(main())
