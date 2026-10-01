"""Read-only by default operator recovery command."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from orchestune.recovery.contracts import RecoveryRequest
from orchestune.recovery.service import recover_claim


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Inspect or recover one stopped local claim"
    )
    parser.add_argument("--issue", type=int, required=True)
    parser.add_argument("--claim-id")
    parser.add_argument("--reason")
    parser.add_argument(
        "--apply", action="store_true", help="apply the previewed recovery"
    )
    parser.add_argument(
        "--restore-marker",
        action="store_true",
        help="restore ownership marker instead of releasing",
    )
    parser.add_argument("--state", type=Path)
    parser.add_argument("--timeout", type=float, default=0)
    args = parser.parse_args(argv)
    result = recover_claim(
        RecoveryRequest(
            args.issue,
            args.claim_id,
            args.reason,
            args.apply,
            args.restore_marker,
            args.state,
            args.timeout,
        )
    )
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    if not result.success:
        print(
            "Next: stop/reconcile the agent, or restore the marker and resume complete with its saved ID."
        )
    return 0 if result.success else 43
