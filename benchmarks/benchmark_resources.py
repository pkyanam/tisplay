"""Compare Kitty encoding latency and peak RSS across source checkouts.

Example: ``python benchmarks/benchmark_resources.py baseline=/tmp/old/src current=src``.
Each sample runs in a fresh process so ``ru_maxrss`` is comparable.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def worker(source: str | None) -> dict[str, float | int]:
    if source:
        sys.path.insert(0, str(Path(source).resolve()))
    sys.path.insert(0, str(ROOT))
    from benchmarks.benchmark_render import detailed_frame
    from tisplay.terminal import KittyRenderer
    import resource

    frames = [detailed_frame(motion=i) for i in range(5)]
    renderer = KittyRenderer(first_image_id=50)
    renderer.render(frames[0], 100, 35)  # warm up
    times: list[float] = []
    sizes: list[int] = []
    for frame in frames[1:]:
        start = time.perf_counter()
        output = renderer.render(frame, 100, 35)
        times.append((time.perf_counter() - start) * 1000)
        sizes.append(len(output))
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform != "darwin":  # Linux reports KiB; macOS reports bytes.
        peak *= 1024
    return {"median_ms": statistics.median(times), "mean_ms": statistics.mean(times),
            "mean_payload_bytes": round(statistics.mean(sizes)), "peak_rss_bytes": peak}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sources", nargs="*", help="label=source/src entries")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--source", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(worker(args.source)))
        return
    if not args.sources:
        parser.error("provide at least one label=source/src entry")

    sources = [item.split("=", 1) for item in args.sources]
    results: dict[str, list[dict[str, float | int]]] = {label: [] for label, _ in sources}
    # Alternate checkout order to reduce warm/cool system bias.
    for index in range(args.repeats):
        ordered = sources if index % 2 == 0 else list(reversed(sources))
        for label, path in ordered:
            run = subprocess.run([sys.executable, __file__, "--worker", "--source", path],
                                 cwd=ROOT, capture_output=True, text=True, check=True)
            results[label].append(json.loads(run.stdout))
    summary = {}
    for label, runs in results.items():
        summary[label] = {
            "median_ms": statistics.median(run["median_ms"] for run in runs),
            "median_peak_rss_bytes": statistics.median(run["peak_rss_bytes"] for run in runs),
            "mean_payload_bytes": statistics.mean(run["mean_payload_bytes"] for run in runs),
            "runs": runs,
        }
    print(json.dumps({"python": sys.version.split()[0], "platform": platform.platform(),
                      "pillow": __import__("PIL").__version__, "frame": "1600x900 desktop-like",
                      "renders_per_process": 4, "repeats": args.repeats, "results": summary}, indent=2))


if __name__ == "__main__":
    main()
