"""Issue本文からのタスク定義パース。"""

from __future__ import annotations

import re
import sys

import yaml

from orchestune.models import IssueRecord, Task

BASE_PRIORITY = {"low": 1.0, "medium": 2.0, "high": 3.0}

FOOTPRINT_BLOCK_PATTERN = re.compile(r"```yaml\s*\n(.*?)```", re.DOTALL)

# Embedded in every parent (EPIC) issue `provisioning.py` creates, and required
# (in addition to an exact title match) before an orphan-recovery lookup is
# allowed to adopt an existing issue as "our" parent: an unrelated issue that
# coincidentally has the same title has no way to also have this exact literal
# string in its body. Lives here (L2) rather than in `provisioning.py` (L4) so
# that `dispatch_cycle.py` (L3) can validate a `--parent-issue` number against
# it without depending on the entrypoint-layer `provisioning` module.
PARENT_MARKER = "<!-- orchestune:decomposition-plan-parent -->"


def is_epic_issue(issue: IssueRecord) -> bool:
    """`issue`がEPIC（親）Issueと構造的に一致するかを判定する。

    `provisioning.py`の持続済みparent番号検証は、特定のplanの`metadata.title`との
    厳密な一致まで要求するが、dispatch実行時（`--parent-issue`検証）にはどのplanの
    親かという情報がなく番号しか分からないため、「本物のEPICらしい構造を持つか」
    という緩い判定に留める。
    """
    return issue.title.startswith("[EPIC] ") and PARENT_MARKER in issue.body


def parse_task_from_issue(
    issue: IssueRecord,
    issue_to_subtask_id: dict[int, str] | None = None,
) -> Task:
    subtask_id = ""
    footprint: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    yaml_error = False

    match = FOOTPRINT_BLOCK_PATTERN.search(issue.body)
    if match:
        try:
            data = yaml.safe_load(match.group(1))
            if isinstance(data, dict):
                subtask_id = str(data.get("subtask_id", ""))
                footprint = tuple(str(f) for f in (data.get("footprint") or []))
                symbols = tuple(str(s) for s in (data.get("symbols") or []))
        except yaml.YAMLError as e:
            print(
                f"Warning: Failed to parse YAML from issue #{issue.number}: {e}",
                file=sys.stderr,
            )
            yaml_error = True

    if issue_to_subtask_id is not None and issue.blocked_by:
        depends_on = tuple(
            issue_to_subtask_id[num]
            for num in issue.blocked_by
            if num in issue_to_subtask_id
        )
    else:
        if match and not yaml_error:
            try:
                data = yaml.safe_load(match.group(1))
                if isinstance(data, dict):
                    depends_on = tuple(str(d) for d in (data.get("depends_on") or []))
            except Exception:
                pass

    priority = "medium"
    has_unknown_priority_label = False
    risk = False
    progress_partial = False
    for label in issue.labels:
        if label.startswith("priority:"):
            candidate = label.split(":", 1)[1]
            if candidate in BASE_PRIORITY:
                priority = candidate
            else:
                has_unknown_priority_label = True
                print(
                    f"Warning: Unknown priority label '{label}' on issue "
                    f"#{issue.number}; falling back to 'medium'.",
                    file=sys.stderr,
                )
        elif label == "risk:flagged":
            risk = True
        elif label == "progress:partial":
            progress_partial = True

    if has_unknown_priority_label:
        priority = "medium"

    parent_number = None
    parent_state = None
    if issue.parent:
        parent_number = issue.parent.get("number")
        parent_state = issue.parent.get("state")

    return Task(
        issue_number=issue.number,
        subtask_id=subtask_id,
        footprint=footprint,
        symbols=symbols,
        risk=risk,
        priority=priority,
        progress_partial=progress_partial,
        status_labels=tuple(issue.labels),
        created_at=issue.created_at,
        depends_on=depends_on,
        yaml_error=yaml_error,
        parent_number=parent_number,
        issue_state=issue.state,
        parent_state=parent_state,
    )
