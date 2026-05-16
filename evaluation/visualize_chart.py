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


# ── plot: token consumption ───────────────────────────────────


_COMP_ORDER = [
    "rewriter", "orchestrator", "rag", "db", "web", "visualize", "collector",
]
# Distinct color per component for the stacked breakdown.
_COMP_COLORS = {
    "rewriter":     "#4C72B0",
    "orchestrator": "#DD8452",
    "rag":          "#55A868",
    "db":           "#C44E52",
    "web":          "#8172B3",
    "visualize":    "#937860",
    "collector":    "#DA8BC3",
}


def _resolve_tokens_path(path: Path) -> Path:
    """Accept a tokens.json or the run dir that contains it."""
    if path.is_dir():
        path = path / "tokens.json"
    if not path.exists():
        raise SystemExit(f"no tokens.json at {path}")
    return path


def _grouped_minavgmax(ax, labels: list[str], stats: list[dict], title: str):
    """Render min/avg/max grouped bars for `labels` onto `ax`."""
    import numpy as np

    x = np.arange(len(labels))
    w = 0.27
    for off, key, color in (
        (-w, "min", "#9ecae1"),
        (0.0, "avg", "#4C72B0"),
        (w,  "max", "#08519c"),
    ):
        vals = [s.get(key, 0.0) for s in stats]
        bars = ax.bar(x + off, vals, w, label=key, color=color)
        for b, v in zip(bars, vals):
            if v:
                ax.text(b.get_x() + b.get_width() / 2, v,
                        f"{int(v)}", ha="center", va="bottom", fontsize=7)
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=20, ha="right", fontsize=9)
    ax.set_ylabel("Total tokens")
    ax.set_title(title)
    ax.legend(fontsize=8)
    ax.grid(axis="y", linestyle=":", alpha=0.5)


def plot_tokens(tokens_path: Path) -> list[Path]:
    """From a run's tokens.json emit:

        tokens_component.png — min/avg/max per component + whole-turn
        tokens_category.png  — min/avg/max per question category
        tokens_breakdown.png — stacked avg-token mix per category
    """
    import matplotlib.pyplot as plt
    import numpy as np

    tokens_path = _resolve_tokens_path(tokens_path)
    data = json.loads(tokens_path.read_text(encoding="utf-8"))
    run_dir = tokens_path.parent
    by_comp = data.get("by_component", {})
    overall = data.get("overall_per_turn", {})
    by_cat  = data.get("by_category", {})
    outputs: list[Path] = []

    # 1) per-component (+ whole turn) min/avg/max
    comp_labels = [c for c in _COMP_ORDER if c in by_comp]
    comp_stats  = [by_comp[c]["total"] for c in comp_labels]
    if overall.get("turns"):
        comp_labels.append("WHOLE\n/turn")
        comp_stats.append(overall["total"])
    if comp_labels:
        fig, ax = plt.subplots(figsize=(max(7, 1.3 * len(comp_labels)), 5))
        _grouped_minavgmax(
            ax, comp_labels, comp_stats,
            f"Token / component — {run_dir.name}",
        )
        fig.tight_layout()
        p = run_dir / "tokens_component.png"
        fig.savefig(p, dpi=120)
        plt.close(fig)
        log.info("saved %s", p)
        outputs.append(p)

    # 2) per-category whole-turn min/avg/max
    if by_cat:
        cats = list(by_cat.keys())
        fig, ax = plt.subplots(figsize=(max(7, 1.4 * len(cats)), 5))
        _grouped_minavgmax(
            ax, cats, [by_cat[c]["total"] for c in cats],
            f"Token / question category — {run_dir.name}",
        )
        fig.tight_layout()
        p = run_dir / "tokens_category.png"
        fig.savefig(p, dpi=120)
        plt.close(fig)
        log.info("saved %s", p)
        outputs.append(p)

        # 3) stacked component mix (avg tokens/turn) per category
        fig, ax = plt.subplots(figsize=(max(7, 1.4 * len(cats)), 5))
        x = np.arange(len(cats))
        bottom = np.zeros(len(cats))
        for comp in _COMP_ORDER:
            vals = np.array([
                by_cat[c].get("by_component_avg", {}).get(comp, 0.0)
                for c in cats
            ])
            if not vals.any():
                continue
            ax.bar(x, vals, 0.6, bottom=bottom, label=comp,
                   color=_COMP_COLORS.get(comp, "#888"))
            bottom += vals
        ax.set_xticks(x)
        ax.set_xticklabels(cats, rotation=20, ha="right", fontsize=9)
        ax.set_ylabel("Avg tokens / turn")
        ax.set_title(f"Component mix per category — {run_dir.name}")
        ax.legend(fontsize=8, ncol=2)
        ax.grid(axis="y", linestyle=":", alpha=0.5)
        fig.tight_layout()
        p = run_dir / "tokens_breakdown.png"
        fig.savefig(p, dpi=120)
        plt.close(fig)
        log.info("saved %s", p)
        outputs.append(p)

    if not outputs:
        raise SystemExit(f"no token data to plot in {tokens_path}")
    return outputs


# ── CLI ───────────────────────────────────────────────────────


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("paths", type=Path, nargs="+",
                   help="summary.json paths, or run dirs / tokens.json with --tokens")
    p.add_argument("--compare", action="store_true",
                   help="Emit A/B grouped bar chart instead of single-run charts")
    p.add_argument("--tokens", action="store_true",
                   help="Plot token consumption from tokens.json instead of scores")
    p.add_argument("--out", type=Path, default=None,
                   help="Output PNG path (defaults to <run_dir>/bar.png)")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.tokens:
        for path in args.paths:
            plot_tokens(path)
        return

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
