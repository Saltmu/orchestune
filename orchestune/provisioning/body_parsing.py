"""Subtask issue body parser and reversibility validation."""

from __future__ import annotations

import base64
import re
from typing import Any

import yaml

from orchestune.dag.models import VALID_MODEL_TIERS, SubTask
from orchestune.dispatch.attempt_record import attempt_from_body
from orchestune.footprint_paths import normalize_footprint_path
from orchestune.issue_parsing import (
    FOOTPRINT_BLOCK_PATTERN,
    REMEDIATION_MARKER,
    normalize_newlines,
    remediation_record_from_body,
)
from orchestune.provisioning.rendering import (
    _PLACEHOLDER_PATTERN,
    _render_issue_body,
)

_REPLAN_GEN_PATTERN = re.compile(
    r"^<!-- orchestune:replan-generation plan_revision=replan-v1:sha256:[0-9a-f]{64} "
    r"subtask_id_b64=([A-Za-z0-9_-]+) -->\n?"
)

_LAUNCH_ATTEMPT_MARKER = "<!-- orchestune:launch-attempt -->"
_SYMBOLS_WARNING_HEADER = (
    "\n\n---\n\n⚠️ **symbols未検出**: 以下のシンボルは、Footprintに列挙されたファイル内に"
    "見つかりませんでした。このsubtaskで新規追加する予定であれば問題ありません"
    "が、リファクタによる改名・移動で古い名称が残っている可能性もあるため、"
    "着手前にコードを確認してください。\n"
)


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys."""

    def construct_mapping(self, node: Any, deep: bool = False) -> dict[Any, Any]:
        if isinstance(node, yaml.MappingNode):
            seen: set[Any] = set()
            for key_node, _ in node.value:
                key = self.construct_object(key_node, deep=True)
                if key in seen:
                    raise ValueError(f"duplicate key in YAML: {key!r}")
                seen.add(key)
        return super().construct_mapping(node, deep=deep)


def validate_reversible_subtask(subtask: SubTask | dict[str, Any]) -> None:
    """Validate that subtask bullet lists can be reversibly restored.

    Rejects items containing '\\n- ', items with leading/trailing whitespace,
    empty string items, and reserved empty placeholder '特になし'.
    """
    for field_name in ("proposed_changes", "acceptance_criteria", "verification_plan"):
        raw_items = (
            getattr(subtask, field_name)
            if isinstance(subtask, SubTask)
            else subtask.get(field_name, ())
        )
        if isinstance(raw_items, str):
            continue
        for item in raw_items:
            if not isinstance(item, str):
                raise ValueError(f"item in {field_name} must be a string: {item!r}")
            if item == "":
                raise ValueError(f"empty string item in {field_name}")
            if item != item.strip():
                raise ValueError(
                    f"leading or trailing whitespace in {field_name} item: {item!r}"
                )
            if item.strip() == "特になし":
                raise ValueError(
                    f"item in {field_name} cannot be '特になし' (reserved for empty section placeholder)"
                )
            if "\n- " in item:
                raise ValueError(
                    f"item in {field_name} contains newline followed by '- ': {item!r}"
                )


def _strip_replan_marker(body: str) -> tuple[str, str | None]:
    match = _REPLAN_GEN_PATTERN.match(body)
    if not match:
        return body, None
    b64_str = match.group(1)
    padded = b64_str + "=" * (-len(b64_str) % 4)
    subtask_id = base64.urlsafe_b64decode(padded).decode("utf-8")
    return body[match.end() :], subtask_id


def _strip_launch_attempt(body: str) -> str:
    if _LAUNCH_ATTEMPT_MARKER not in body:
        return body
    attempt = attempt_from_body(body)
    if attempt is None:
        raise ValueError("malformed launch-attempt journal")
    marker_pos = body.rfind(_LAUNCH_ATTEMPT_MARKER)
    prefix = body[:marker_pos].rstrip("\n")
    suffix = body[marker_pos:]
    if not suffix.strip().endswith("```"):
        raise ValueError("launch-attempt journal must be at end of body")
    return prefix


def _strip_remediation_section(body: str) -> str:
    if REMEDIATION_MARKER not in body:
        return body
    record = remediation_record_from_body(body)
    if record is None:
        raise ValueError("malformed remediation record")
    expected_header = (
        "\n\n## 補修タスクの根拠\n\n"
        f"- Kind: {record['kind']}\n"
        f"- Parent acceptance: {', '.join(record['parent_acceptance'])}\n"
        f"- Origins: {', '.join(record['origins'])}\n"
        f"- Reason: {record['reason']}\n\n"
    )
    if expected_header not in body:
        raise ValueError(
            "remediation human-oriented section does not match expected format"
        )
    header_pos = body.rfind(expected_header)
    return body[:header_pos]


def _strip_symbols_warning(body: str) -> str:
    if _SYMBOLS_WARNING_HEADER not in body:
        return body
    parts = body.rsplit(_SYMBOLS_WARNING_HEADER, 1)
    warning_content = parts[1].strip()
    lines = [line.strip() for line in warning_content.splitlines() if line.strip()]
    if not lines or not all(
        line.startswith("- `") and line.endswith("`") for line in lines
    ):
        raise ValueError("symbols warning does not match expected format")
    return parts[0]


def _sanitize_known_additions(body: str) -> tuple[str, str | None]:
    body = _strip_launch_attempt(body)
    body = _strip_remediation_section(body)
    body = _strip_symbols_warning(body)
    body, replan_id = _strip_replan_marker(body)
    return body, replan_id


def _parse_bullet_list(text: str) -> list[str]:
    stripped = text.strip()
    if stripped == "特になし":
        return []
    lines = text.splitlines()
    items: list[str] = []
    current: list[str] = []
    for line in lines:
        if line.startswith("- "):
            if current:
                items.append("\n".join(current))
            current = [line[2:]]
        elif current:
            current.append(line)
        elif line.strip():
            raise ValueError(f"invalid bullet list line: {line!r}")
    if current:
        items.append("\n".join(current))
    return items


def _extract_footprint_data(
    body: str, template: str
) -> tuple[dict[str, Any], re.Match[str], re.Match[str]]:
    t_match = FOOTPRINT_BLOCK_PATTERN.search(template)
    b_match = FOOTPRINT_BLOCK_PATTERN.search(body)
    if not t_match or not b_match:
        raise ValueError("Footprint block not found in template or body")

    t_before = template[: t_match.start()].rstrip()
    t_header = t_before.splitlines()[-1] if t_before.splitlines() else ""
    if t_header and t_header not in body[: b_match.start()]:
        raise ValueError("Footprint fence position in body does not match template")

    data = yaml.load(b_match.group(1), Loader=_UniqueKeyLoader)
    if not isinstance(data, dict):
        raise ValueError("Footprint block must be a YAML mapping")
    return data, t_match, b_match


def _coerce_footprint_plan_keys(data: dict[str, Any]) -> dict[str, Any]:
    subtask_id = data.get("subtask_id")
    if not isinstance(subtask_id, str) or not subtask_id.strip():
        raise ValueError(f"invalid subtask_id in Footprint: {subtask_id!r}")

    footprint = data.get("footprint")
    if not isinstance(footprint, list) or not all(
        isinstance(p, str) for p in footprint
    ):
        raise ValueError("footprint must be a list of strings")

    symbols = data.get("symbols")
    if not isinstance(symbols, list) or not all(isinstance(s, str) for s in symbols):
        raise ValueError("symbols must be a list of strings")

    depends_on = data.get("depends_on")
    if not isinstance(depends_on, list) or not all(
        isinstance(d, str) for d in depends_on
    ):
        raise ValueError("depends_on must be a list of strings")

    raw_parent = data.get("parent_issue_number")
    parent_issue_number = None
    if raw_parent is not None:
        if (
            isinstance(raw_parent, bool)
            or not isinstance(raw_parent, int)
            or raw_parent <= 0
        ):
            raise ValueError(
                f"invalid parent_issue_number in Footprint: {raw_parent!r}"
            )
        parent_issue_number = raw_parent

    model_tier = data.get("model_tier")
    if model_tier is not None and model_tier not in VALID_MODEL_TIERS:
        raise ValueError(f"invalid model_tier in Footprint: {model_tier!r}")

    return {
        "subtask_id": subtask_id.strip(),
        "footprint": tuple(normalize_footprint_path(p) for p in footprint),
        "symbols": tuple(symbols),
        "depends_on": tuple(depends_on),
        "shared_contract": data.get("shared_contract"),
        "writes_shared_contract": bool(data.get("writes_shared_contract", False)),
        "parent_issue_number": parent_issue_number,
        "execution_profile": data.get("execution_profile"),
        "model_tier": model_tier,
    }


def _validate_template_placeholders(template: str) -> None:
    required = {
        "overview",
        "proposed_changes",
        "acceptance_criteria",
        "verification_plan",
    }
    found = set(_PLACEHOLDER_PATTERN.findall(template))
    if not required.issubset(found):
        raise ValueError(f"template missing required placeholders: {required - found}")
    # Check for adjacent placeholders without delimiters
    if re.search(r"\{\{[a-z_]+\}\}\{\{[a-z_]+\}\}", template):
        raise ValueError("template contains adjacent placeholders without delimiters")


def extract_subtask_section(template: str, body: str, placeholder: str) -> str:
    """Extract a single section from body corresponding to placeholder in template."""
    template = normalize_newlines(template)
    body = normalize_newlines(body)
    ph = f"{{{{{placeholder}}}}}"
    if ph not in template:
        raise ValueError(f"placeholder '{placeholder}' not found in template")

    idx = template.index(ph)
    prefix_tokens = _PLACEHOLDER_PATTERN.split(template[:idx])
    suffix_tokens = _PLACEHOLDER_PATTERN.split(template[idx + len(ph) :])
    delim_before = prefix_tokens[-1]
    delim_after = suffix_tokens[0]

    if delim_before not in body or delim_after not in body:
        raise ValueError(f"delimiters for '{placeholder}' not found in body")
    start = body.index(delim_before) + len(delim_before)
    end = body.index(delim_after, start)
    return body[start:end]


def _match_prose(template_prefix: str, body_prefix: str) -> dict[str, str] | None:
    """Match body prefix against template prefix structure and return captured placeholders.

    Note: The regex uses non-greedy capture groups and fullmatch, which produces at most
    one valid match. Ambiguities or mismatches are strictly caught by subsequent
    roundtrip verification (_verify_roundtrip_match).
    """
    segments: list[str] = []
    keys: list[str] = []
    last_end = 0
    for ph_match in _PLACEHOLDER_PATTERN.finditer(template_prefix):
        segments.append(template_prefix[last_end : ph_match.start()])
        keys.append(ph_match.group(1))
        last_end = ph_match.end()
    trailing_delim = template_prefix[last_end:]

    # Construct regex with capture groups
    pattern_parts: list[str] = []
    for i, seg in enumerate(segments):
        pattern_parts.append(re.escape(seg))
        pattern_parts.append(f"(?P<p_{i}>.*?)")
    pattern_parts.append(re.escape(trailing_delim))
    regex = re.compile("^" + "".join(pattern_parts) + "$", re.DOTALL)

    match = regex.fullmatch(body_prefix)
    if not match:
        return None
    return {keys[i]: match.group(f"p_{i}") for i in range(len(keys))}


def _verify_roundtrip_match(
    subtask: SubTask,
    template: str,
    sanitized_body: str,
    b_match: re.Match[str],
    parent_issue_number: int | None,
) -> None:
    # Note: Footprint YAML block is masked because formatting differences (flow vs block
    # sequences, extra runtime keys added by claim/amend like recompute_count/claim_id)
    # are tolerated. Exact semantic equality of canonical plan keys is enforced separately
    # via _coerce_footprint_plan_keys and _reconcile_subtask. Extra YAML keys not part of the
    # canonical 9 plan keys are ignored, and fields not stored in child body (e.g. risk,
    # risk_reasons) retain their parent plan values.
    expected = _render_issue_body(subtask, template, parent_issue_number)
    exp_b_match = FOOTPRINT_BLOCK_PATTERN.search(expected)
    if not exp_b_match:
        raise ValueError("could not render expected Footprint block")

    exp_masked = (
        expected[: exp_b_match.start()] + "__FP__" + expected[exp_b_match.end() :]
    )
    act_masked = (
        sanitized_body[: b_match.start()] + "__FP__" + sanitized_body[b_match.end() :]
    )

    if exp_masked != act_masked:
        exp_lines = exp_masked.splitlines()
        act_lines = act_masked.splitlines()
        for idx, (exp_l, act_l) in enumerate(zip(exp_lines, act_lines, strict=False)):
            if exp_l != act_l:
                raise ValueError(
                    f"Body mismatch at line {idx + 1}: expected {exp_l!r}, got {act_l!r}"
                )
        if len(exp_lines) != len(act_lines):
            raise ValueError(
                f"Body mismatch line count: expected {len(exp_lines)} lines, got {len(act_lines)} lines"
            )


def _extract_prose_fields(
    captured: dict[str, str], title: str, subtask_id: str
) -> tuple[str, str, list[str], list[str], list[str]]:
    description = captured.get("description", "")
    if not description and title:
        prefix = f"[FEAT] {subtask_id}: "
        if title.startswith(prefix):
            description = title.removeprefix(prefix).strip()
        elif title.startswith(f"{subtask_id}: "):
            description = title.removeprefix(f"{subtask_id}: ").strip()

    overview = captured["overview"]
    changes = _parse_bullet_list(captured["proposed_changes"])
    criteria = _parse_bullet_list(captured["acceptance_criteria"])
    verify = _parse_bullet_list(captured["verification_plan"])
    return description, overview, changes, criteria, verify


def _create_subtask_model(
    fp_keys: dict[str, Any],
    description: str,
    overview: str,
    changes: list[str],
    criteria: list[str],
    verify: list[str],
) -> SubTask:
    return SubTask(
        id=fp_keys["subtask_id"],
        description=description,
        overview=overview,
        proposed_changes=tuple(changes),
        acceptance_criteria=tuple(criteria),
        verification_plan=tuple(verify),
        footprint=fp_keys["footprint"],
        symbols=fp_keys["symbols"],
        depends_on=fp_keys["depends_on"],
        risk=False,
        risk_reasons=(),
        shared_contract=fp_keys["shared_contract"],
        writes_shared_contract=fp_keys["writes_shared_contract"],
        execution_profile=fp_keys["execution_profile"],
        model_tier=fp_keys["model_tier"],
    )


def _assemble_subtask_result(
    subtask: SubTask,
    parent_issue_number: int | None,
) -> dict[str, Any]:
    return {
        "subtask_id": subtask.id,
        "description": subtask.description,
        "overview": subtask.overview,
        "proposed_changes": list(subtask.proposed_changes),
        "acceptance_criteria": list(subtask.acceptance_criteria),
        "verification_plan": list(subtask.verification_plan),
        "footprint": list(subtask.footprint),
        "symbols": list(subtask.symbols),
        "depends_on": list(subtask.depends_on),
        "shared_contract": subtask.shared_contract,
        "writes_shared_contract": subtask.writes_shared_contract,
        "parent_issue_number": parent_issue_number,
        "execution_profile": subtask.execution_profile,
        "model_tier": subtask.model_tier,
    }


def parse_subtask_issue_body(template: str, title: str, body: str) -> dict[str, Any]:
    """Parse a subtask issue body into plan keys and prose."""
    template = normalize_newlines(template)
    body = normalize_newlines(body)
    _validate_template_placeholders(template)

    sanitized_body, replan_id = _sanitize_known_additions(body)
    fp_raw, t_match, b_match = _extract_footprint_data(sanitized_body, template)
    fp_keys = _coerce_footprint_plan_keys(fp_raw)

    if replan_id and replan_id != fp_keys["subtask_id"]:
        raise ValueError(
            f"replan generation subtask_id mismatch: {replan_id!r} != {fp_keys['subtask_id']!r}"
        )

    t_prefix = template[: t_match.start()]
    b_prefix = sanitized_body[: b_match.start()]

    captured = _match_prose(t_prefix, b_prefix)
    if captured is None:
        raise ValueError("could not match body structure to template")

    description, overview, changes, criteria, verify = _extract_prose_fields(
        captured, title, fp_keys["subtask_id"]
    )

    subtask = _create_subtask_model(
        fp_keys, description, overview, changes, criteria, verify
    )

    _verify_roundtrip_match(
        subtask,
        template,
        sanitized_body,
        b_match,
        fp_keys["parent_issue_number"],
    )

    return _assemble_subtask_result(subtask, fp_keys["parent_issue_number"])


parse_bullet_list = _parse_bullet_list

__all__ = [
    "extract_subtask_section",
    "parse_bullet_list",
    "parse_subtask_issue_body",
    "validate_reversible_subtask",
]
