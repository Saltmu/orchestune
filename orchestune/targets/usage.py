"""Shared usage provider used by local dispatch and completion."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from orchestune.models import Usage
from orchestune.targets.contracts import DispatchHandle


def _extract_usage_from_dict(data: Any) -> Usage | None:
    if not isinstance(data, dict):
        return None
    usage_data = data.get("usage") if isinstance(data.get("usage"), dict) else data
    if not isinstance(usage_data, dict):
        return None
    if "input_tokens" not in usage_data and "output_tokens" not in usage_data:
        return None
    try:
        input_tokens = int(usage_data.get("input_tokens", 0))
        output_tokens = int(usage_data.get("output_tokens", 0))
        total_tokens = int(usage_data.get("total_tokens", input_tokens + output_tokens))
        model = (
            str(data["model"])
            if data.get("model") is not None
            else (
                str(usage_data["model"])
                if usage_data.get("model") is not None
                else None
            )
        )
        cost_val = data.get("total_cost_combined")
        if cost_val is None:
            cost_val = data.get("cost_usd")
        if cost_val is None and usage_data is not data:
            cost_val = usage_data.get("total_cost_combined")
        if cost_val is None and usage_data is not data:
            cost_val = usage_data.get("cost_usd")
        cost_usd = float(cost_val) if cost_val is not None else None
        return Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            model=model,
            cost_usd=cost_usd,
        )
    except (TypeError, ValueError):
        return None


def _parse_usage_from_log(log_path: Path) -> Usage | None:
    if not log_path.exists() or log_path.stat().st_size == 0:
        return None
    try:
        content = log_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    for line in reversed(lines):
        if line.startswith("{") and line.endswith("}"):
            try:
                data = json.loads(line)
                usage = _extract_usage_from_dict(data)
                if usage is not None:
                    return usage
            except Exception:
                pass
    try:
        data = json.loads(content.strip())
        usage = _extract_usage_from_dict(data)
        if usage is not None:
            return usage
    except Exception:
        pass
    return None


class LocalUsageProvider:
    def __init__(self, log_dir: str | Path):
        self._log_dir = Path(log_dir)

    def collect_usage(self, handle: DispatchHandle) -> Usage | None:
        if not handle.branch_name:
            return None
        slug = handle.branch_name.replace("/", "-")
        return _parse_usage_from_log(self._log_dir / f"{slug}.log")
