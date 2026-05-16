"""FinHouse evaluation framework.

Modules:
    judges      — LLM-as-judge helpers with structured JSON output
    metrics.e2e — Layer A (End-to-End) metrics
    metrics.rag — Layer B (RAG / RAGAS) metrics
    metrics.agent — Layer C (Agent / tool) metrics
    runner      — main benchmark runner (invokes the graph + computes metrics)
    visualize_chart — bar chart from summary.json

Run from FinHouse repo root (needs api/ deps installed — i.e. the
finhouse-api Docker container or equivalent venv):

    python -m evaluation.runner --testset evaluation/testset/ \\
                                --output  evaluation/results/2026-05-14
"""
