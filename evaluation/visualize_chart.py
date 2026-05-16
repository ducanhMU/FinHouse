"""
Render evaluation summaries as PNG charts.

Reads `<run_dir>/summary.json` (flat dict {metric: score}) and produces:

    <run_dir>/bar.png       — horizontal bar chart, color-coded by layer
    <run_dir>/radar.png     — 3-axis radar (E2E / RAG / Agent averages)

Optional: compare multiple runs with `--compare` to emit a grouped bar
chart for A/B analysis.

Usage:

    # Single run
    python -m evaluation.visualize_chart \\
        evaluation/results/2026-05-14_10-30_default/summary.json

    # A/B compare
    python -m evaluation.visualize_chart \\
        --compare \\
        evaluation/results/2026-05-14_10-30_v1/summary.json \\
        evaluation/results/2026-05-14_11-00_v2/summary.json
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

log = logging.getLogger("finhouse.eval.viz")


_LAYER_COLORS = {
    "e2e":   "#4C72B0",   # blue
    "rag":   "#55A868",   # green
    "agent": "#C44E52",   # red
}


def _load_summary(path: Path) -> dict:
    raw = json.loads(path.read_text(encoding="utf-8"))
    # Strip aggregate-only keys if present (we use flat dict already)
    return {k: float(v) for k, v in raw.items() if isinstance(v, (int, float))}


def _color_for(metric_name: str) -> str:
    head = metric_name.split(".", 1)[0]
    return _LAYER_COLORS.get(head, "#888888")


def _short_name(metric_name: str) -> str:
    """e2e.correctness → 'correctness (e2e)' for vertical bar labels."""
    if "." in metric_name:
        layer, m = metric_name.split(".", 1)
        return f"{m}\n[{layer}]"
    return metric_name


# ── plot: single-run horizontal bar chart ─────────────────────


def plot_bar(summary_path: Path, out_path: Path | None = None) -> Path:
    import matplotlib.pyplot as plt   # local import — heavy

    data = _load_summary(summary_path)
    if not data:
        raise SystemExit(f"no numeric metrics in {summary_path}")

    items = sorted(data.items(), key=lambda kv: (kv[0].split(".", 1)[0], kv[0]))
    names  = [k for k, _ in items]
    values = [v for _, v in items]
    colors = [_color_for(k) for k in names]

    plt.figure(figsize=(11, max(4, 0.35 * len(items) + 1.5)))
    bars = plt.barh(range(len(items)), values, color=colors)
    plt.yticks(range(len(items)), names, fontsize=9)
    plt.xlim(0, 1)
    plt.xlabel("Score")
    plt.title(f"FinHouse Benchmark — {summary_path.parent.name}")
    for b, v in zip(bars, values):
        plt.text(min(v + 0.01, 0.98), b.get_y() + b.get_height() / 2,
                 f"{v:.2f}", va="center", fontsize=8)
    plt.grid(axis="x", linestyle=":", alpha=0.5)
    plt.tight_layout()

    out_path = out_path or (summary_path.parent / "bar.png")
    plt.savefig(out_path, dpi=120)
    plt.close()
    log.info("saved %s", out_path)
    return out_path


# ── plot: radar overview ──────────────────────────────────────


def plot_radar(summary_path: Path, out_path: Path | None = None) -> Path:
    import matplotlib.pyplot as plt
    import math

    data = _load_summary(summary_path)
    layers: dict[str, list[float]] = {"e2e": [], "rag": [], "agent": []}
    for k, v in data.items():
        head = k.split(".", 1)[0]
        if head in layers:
            layers[head].append(v)
    avgs = {k: (sum(vs) / len(vs) if vs else 0.0) for k, vs in layers.items()}

    labels = list(avgs.keys())
    values = list(avgs.values())
    n = len(labels)
    angles = [i * 2 * math.pi / n for i in range(n)]
    values_loop = values + values[:1]
    angles_loop = angles + angles[:1]

    fig = plt.figure(figsize=(6, 6))
    ax = fig.add_subplot(111, polar=True)
    ax.plot(angles_loop, values_loop, "-o", color="#333")
    ax.fill(angles_loop, values_loop, alpha=0.25, color="#888")
    ax.set_xticks(angles)
    ax.set_xticklabels([l.upper() for l in labels])
    ax.set_ylim(0, 1)
    ax.set_yticks([0.25, 0.5, 0.75, 1.0])
    ax.set_yticklabels(["0.25", "0.50", "0.75", "1.00"], fontsize=8)
    ax.set_title(f"Layer averages — {summary_path.parent.name}", pad=20)

    for ang, val, lab in zip(angles, values, labels):
        ax.text(ang, val + 0.05, f"{val:.2f}", ha="center", fontsize=9)

    out_path = out_path or (summary_path.parent / "radar.png")
    plt.tight_layout()
    plt.savefig(out_path, dpi=120)
    plt.close()
    log.info("saved %s", out_path)
    return out_path


# ── plot: A/B comparison grouped bar ──────────────────────────


def plot_compare(summary_paths: list[Path], out_path: Path) -> Path:
    import matplotlib.pyplot as plt

    datasets = [(p.parent.name, _load_summary(p)) for p in summary_paths]
    all_metrics: list[str] = []
    seen: set[str] = set()
    for _, d in datasets:
        for k in d:
            if k not in seen:
                seen.add(k)
                all_metrics.append(k)
    all_metrics.sort(key=lambda k: (k.split(".", 1)[0], k))

    n_metrics = len(all_metrics)
    n_runs = len(datasets)
    width = 0.8 / n_runs

    fig_h = max(5, 0.35 * n_metrics + 2)
    plt.figure(figsize=(12, fig_h))
    for i, (name, d) in enumerate(datasets):
        ys = [d.get(m, 0.0) for m in all_metrics]
        xs = [j + (i - (n_runs - 1) / 2) * width for j in range(n_metrics)]
        plt.barh(xs, ys, height=width, label=name)
    plt.yticks(range(n_metrics), all_metrics, fontsize=8)
    plt.xlim(0, 1)
    plt.xlabel("Score")
    plt.title("FinHouse Benchmark — A/B comparison")
    plt.legend(loc="lower right", fontsize=8)
    plt.grid(axis="x", linestyle=":", alpha=0.5)
    plt.tight_layout()
    plt.savefig(out_path, dpi=120)
    plt.close()
    log.info("saved %s", out_path)
    return out_path


# ── CLI ───────────────────────────────────────────────────────


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("paths", type=Path, nargs="+",
                   help="One or more summary.json paths")
    p.add_argument("--compare", action="store_true",
                   help="Emit A/B grouped bar chart instead of single-run charts")
    p.add_argument("--out", type=Path, default=None,
                   help="Output PNG path (defaults to <run_dir>/bar.png)")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.compare:
        if len(args.paths) < 2:
            raise SystemExit("--compare needs ≥2 summary paths")
        out = args.out or args.paths[0].parent.parent / "compare.png"
        plot_compare(args.paths, out)
        return

    for path in args.paths:
        plot_bar(path, args.out if len(args.paths) == 1 else None)
        plot_radar(path)


if __name__ == "__main__":
    main()
