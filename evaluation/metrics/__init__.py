"""Metric implementations split by evaluation layer.

Each `score_<layer>(case, actual)` returns:
    {"<metric_name>": float in [0,1], ...}

Aggregation (mean across cases) lives in evaluation/runner.py, not here.
"""
