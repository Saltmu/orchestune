"""Posted-trigger restoration and round selection for offline review evidence (#1210)."""

from __future__ import annotations

from typing import Any

import pytest

from orchestune.review.markers import build_trigger_body
from orchestune.review.rounds import (
    ReviewRoundContext,
    build_restorable_trigger_body,
    parse_trigger,
    plan_next_round,
    previous_round_window,
    restore_triggers,
    select_round,
    trigger_comment_ids,
)
from orchestune.review.snapshot import (
    EvidenceContractError,
    InsufficientEvidenceError,
    RoundLimitError,
)

HEAD = "a" * 40
HEAD2 = "b" * 40


def trigger(
    id_: int,
    round_num: int,
    at: str,
    bot: str = "claude",
    head: str | None = HEAD,
    login: str = "claude[bot]",
    reply: str = "",
) -> dict[str, Any]:
    body = build_trigger_body(reply, bot, round_num, head or HEAD)
    if head is None:
        body = "\n".join(
            line for line in body.splitlines() if "review-head" not in line
        )
    return {"id": id_, "body": body, "created_at": at, "user": {"login": login}}


def at(minute: int) -> str:
    return f"2026-10-04T03:{minute:02d}:00Z"


def select(comments, **kwargs) -> ReviewRoundContext:
    kwargs.setdefault("repository", "owner/repo")
    kwargs.setdefault("pr_number", 7)
    return select_round(restore_triggers(comments), **kwargs)


def test_built_trigger_round_trips_through_the_parser() -> None:
    item = trigger(5, 2, at(10), bot="codex", reply="<!-- orchestune:review-reply -->")
    parsed = parse_trigger(item)
    assert parsed is not None
    assert (parsed.id, parsed.reviewer, parsed.round) == (5, "codex", 2)
    assert (parsed.created_at, parsed.requested_head_sha) == (at(10), HEAD)


def test_builder_strips_stale_markers_and_keeps_one_of_each() -> None:
    stale = build_trigger_body("", "claude", 1, HEAD2)
    body = build_trigger_body(f"reply\n{stale}", "claude", 2, HEAD)
    assert body.count("orchestune:review-trigger") == 1
    assert body.count("orchestune:review-round 2") == 1
    assert body.count(f"orchestune:review-head {HEAD}") == 1
    assert HEAD2 not in body and "review-round 1" not in body


def test_builder_rejects_an_invalid_head() -> None:
    with pytest.raises(ValueError, match="40-character"):
        build_trigger_body("", "claude", 1, "abc")


def test_non_trigger_comment_is_ignored() -> None:
    assert parse_trigger({"id": 1, "body": "looks good", "created_at": at(1)}) is None
    assert parse_trigger({"id": 1, "body": None}) is None


@pytest.mark.parametrize("wrapper", ["```\n{m}\n```", "> {m}", "    {m}", "x {m}"])
def test_marker_in_code_quote_or_inline_text_is_not_a_trigger(wrapper: str) -> None:
    marker = "<!-- orchestune:review-trigger bot=claude -->"
    body = f"{wrapper.format(m=marker)}\n<!-- orchestune:review-round 3 -->"
    item = {"id": 9, "body": body, "created_at": at(1)}
    assert parse_trigger(item) is None
    assert 9 not in trigger_comment_ids([item])


def test_marker_after_a_closed_fence_is_still_read() -> None:
    body = build_trigger_body("```\ncode\n```", "claude", 1, HEAD)
    assert parse_trigger({"id": 1, "body": body, "created_at": at(1)}) is not None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b.replace(
            "review-round 1", "review-round 1 -->\n<!-- orchestune:review-round 2"
        ),
        lambda b: b + "\n<!-- orchestune:review-round 2 -->",
        lambda b: b + f"\n<!-- orchestune:review-head {HEAD2} -->",
        lambda b: b + "\n<!-- orchestune:review-trigger bot=codex -->",
        lambda b: b.replace("review-round 1", "review-round x"),
        lambda b: "\n".join(x for x in b.splitlines() if "review-round" not in x),
        lambda b: b.replace(f"review-head {HEAD}", "review-head 1234"),
        lambda b: b.replace("bot=claude", "bot=other"),
    ],
)
def test_inconsistent_markers_are_rejected(mutate) -> None:
    body = mutate(build_trigger_body("", "claude", 1, HEAD))
    with pytest.raises(EvidenceContractError):
        parse_trigger({"id": 1, "body": body, "created_at": at(1)})


def test_trigger_requires_id_and_valid_created_at() -> None:
    body = build_trigger_body("", "claude", 1, HEAD)
    with pytest.raises(EvidenceContractError, match="id"):
        parse_trigger({"body": body, "created_at": at(1)})
    for created in (None, "", "2026-10-04T03:00:00", "x"):
        with pytest.raises(EvidenceContractError, match="created_at"):
            parse_trigger({"id": 1, "body": body, "created_at": created})


def test_missing_head_marker_keeps_requested_head_unknown() -> None:
    parsed = parse_trigger(trigger(1, 1, at(1), head=None))
    assert parsed is not None and parsed.requested_head_sha is None


def test_triggers_are_ordered_by_round_across_bots() -> None:
    comments = [trigger(2, 2, at(10), bot="codex"), trigger(1, 1, at(1))]
    assert [(t.round, t.reviewer) for t in restore_triggers(comments)] == [
        (1, "claude"),
        (2, "codex"),
    ]


def test_identical_duplicate_page_entries_are_collapsed() -> None:
    item = trigger(1, 1, at(1))
    assert len(restore_triggers([item, dict(item)])) == 1


def test_same_id_with_different_content_is_ambiguous() -> None:
    first, second = trigger(1, 1, at(1)), trigger(1, 2, at(2))
    with pytest.raises(EvidenceContractError, match="different content"):
        restore_triggers([first, second])


def test_two_triggers_for_the_same_round_are_ambiguous() -> None:
    with pytest.raises(EvidenceContractError, match="multiple triggers"):
        restore_triggers([trigger(1, 1, at(1)), trigger(2, 1, at(2))])


def test_round_order_must_agree_with_creation_time() -> None:
    with pytest.raises(EvidenceContractError, match="contradicts"):
        restore_triggers([trigger(1, 1, at(5)), trigger(2, 2, at(1))])


def test_trigger_comment_ids_cover_all_bots_and_bare_mentions() -> None:
    comments = [
        trigger(1, 1, at(1), bot="claude", login="codex"),
        trigger(2, 2, at(2), bot="codex"),
        {"id": 3, "body": "@codex review", "created_at": at(3)},
        {"id": 4, "body": "Looks fine", "created_at": at(4)},
    ]
    assert trigger_comment_ids(comments) == {1, 2, 3}


def test_latest_posted_round_is_selected_pr_wide() -> None:
    comments = [trigger(1, 1, at(1)), trigger(2, 2, at(10), bot="codex")]
    context = select(comments)
    assert context == ReviewRoundContext(
        repository="owner/repo",
        pr_number=7,
        reviewer="codex",
        round=2,
        trigger_id=2,
        started_at=at(10),
        ended_at=None,
        requested_head_sha=HEAD,
        is_latest=True,
    )


def test_explicit_past_round_gets_an_upper_boundary() -> None:
    comments = [trigger(1, 1, at(1)), trigger(2, 2, at(10), bot="codex")]
    context = select(comments, requested_round=1)
    assert (context.round, context.ended_at, context.is_latest) == (1, at(10), False)
    assert context.reviewer == "claude"


def test_selection_without_a_trigger_is_insufficient_evidence() -> None:
    with pytest.raises(InsufficientEvidenceError, match="trigger"):
        select([{"id": 1, "body": "hi", "created_at": at(1)}])


def test_explicit_round_that_was_never_posted_is_rejected() -> None:
    with pytest.raises(EvidenceContractError, match="round 2"):
        select([trigger(1, 1, at(1))], requested_round=2)


def test_round_over_the_limit_raises_the_limit_error() -> None:
    comments = [trigger(i, i, at(i)) for i in range(1, 7)]
    with pytest.raises(RoundLimitError):
        select(comments)
    with pytest.raises(RoundLimitError):
        select(comments[:2], requested_round=6)
    assert select(comments[:5]).round == 5
    with pytest.raises(RoundLimitError):
        select(comments[:5], max_rounds=4)


def test_next_round_follows_the_pr_wide_maximum() -> None:
    comments = restore_triggers([trigger(1, 1, at(1)), trigger(2, 2, at(2))])
    assert plan_next_round(comments, bot="claude") == 3
    assert plan_next_round([], bot="codex") == 1


def test_explicit_next_round_must_be_the_next_round() -> None:
    triggers = restore_triggers([trigger(1, 1, at(1))])
    assert plan_next_round(triggers, bot="claude", explicit_round=2) == 2
    for wrong in (1, 3):
        with pytest.raises(EvidenceContractError, match="next round 2"):
            plan_next_round(triggers, bot="claude", explicit_round=wrong)


def test_next_round_over_the_limit_cannot_be_relaxed_by_max_rounds() -> None:
    triggers = restore_triggers([trigger(i, i, at(i)) for i in range(1, 6)])
    with pytest.raises(RoundLimitError):
        plan_next_round(triggers, bot="claude")
    with pytest.raises(RoundLimitError):
        plan_next_round(triggers, bot="claude", max_rounds=5)


def test_reviewer_is_inherited_unless_switch_is_explicit() -> None:
    triggers = restore_triggers([trigger(1, 1, at(1))])
    assert plan_next_round(triggers, bot="claude") == 2
    with pytest.raises(EvidenceContractError, match="switch-reviewer"):
        plan_next_round(triggers, bot="codex")
    assert plan_next_round(triggers, bot="codex", switch_reviewer=True) == 2


def test_strict_window_bounds_the_previous_round_by_the_next_trigger() -> None:
    comments = [trigger(1, 1, at(1)), trigger(2, 2, at(10), bot="codex")]
    window = previous_round_window(comments, 2, strict=True)
    assert (window.previous_round, window.reviewer) == (1, "claude")
    assert (window.started_at, window.ended_at) == (at(1), at(10))
    assert window.exclude_ids == {1, 2}
    pre_post = previous_round_window(comments[:1], 2, strict=True)
    assert pre_post.ended_at is None


def test_window_requires_the_previous_trigger() -> None:
    for strict in (True, False):
        with pytest.raises(ValueError, match="previous round trigger is missing"):
            previous_round_window([trigger(2, 2, at(2))], 4, strict=strict)


def test_lenient_window_tolerates_legacy_triggers_like_the_online_path() -> None:
    legacy = {
        "id": 1,
        "created_at": at(1),
        "body": "@claude review\n<!-- orchestune:review-trigger bot=claude -->\n"
        "<!-- orchestune:review-round 1 -->",
    }
    duplicate = {**legacy, "id": 2, "created_at": at(2)}
    window = previous_round_window([legacy, duplicate], 2, strict=False)
    assert window.started_at == at(2)  # online picks the latest same-round trigger
    assert window.exclude_ids == {1, 2}
    with pytest.raises(EvidenceContractError):
        previous_round_window([legacy, duplicate], 2, strict=True)


def test_lenient_window_requires_a_previous_trigger_timestamp() -> None:
    item = trigger(1, 1, at(1))
    item["created_at"] = ""
    with pytest.raises(ValueError, match="timestamp is missing"):
        previous_round_window([item], 2, strict=False)


MARKERS = (
    "<!-- orchestune:review-trigger bot=claude -->\n"
    "<!-- orchestune:review-round 3 -->\n"
    f"<!-- orchestune:review-head {HEAD} -->"
)


@pytest.mark.parametrize(
    "body",
    [
        # a shorter fence line inside a longer fence does not close it
        f"````\n```\n{MARKERS}\n````",
        f"~~~~\n~~~\n{MARKERS}\n~~~~",
        # a different character never closes the fence
        f"```\n~~~\n{MARKERS}\n```",
        # a fence line carrying an info string is not a closer
        f"```\n```text\n{MARKERS}\n```",
        # an unclosed fence runs to the end of the comment
        f"```\n{MARKERS}",
    ],
)
def test_markers_inside_a_markdown_fence_are_not_a_trigger(body: str) -> None:
    item = {"id": 9, "body": body, "created_at": at(1)}
    assert parse_trigger(item) is None
    assert 9 not in trigger_comment_ids([item])


@pytest.mark.parametrize(
    "body",
    [
        f"````\nexample\n`````\n{MARKERS}",  # a longer closing fence closes it
        f"```\nexample\n```  \n{MARKERS}",  # trailing spaces are allowed
        f"~~~\nexample\n~~~~\n{MARKERS}",
    ],
)
def test_markers_after_a_properly_closed_fence_are_still_read(body: str) -> None:
    item = {"id": 9, "body": f"@claude review\n{body}", "created_at": at(1)}
    parsed = parse_trigger(item)
    assert parsed is not None and parsed.round == 3


@pytest.mark.parametrize("opener", ["```lang`x", "``` a`b"])
def test_backtick_opener_with_a_backtick_info_string_is_not_a_fence(
    opener: str,
) -> None:
    item = {
        "id": 9,
        "body": f"@claude review\n{opener}\n{MARKERS}",
        "created_at": at(1),
    }
    parsed = parse_trigger(item)
    assert parsed is not None and parsed.round == 3


def test_tilde_opener_may_contain_backticks_in_its_info_string() -> None:
    body = f"~~~ a`b\n{MARKERS}\n~~~"
    assert parse_trigger({"id": 9, "body": body, "created_at": at(1)}) is None


INDENTED_MARKERS = "\n".join(f"  {line}" for line in MARKERS.splitlines())


@pytest.mark.parametrize(
    "body",
    [
        # a fenced example nested under a list item
        f"- example\n- ```\n{INDENTED_MARKERS}\n  ```",
        f"1. example:\n   ```text\n{INDENTED_MARKERS}\n   ```",
        # indented marker lines are never a trigger (real markers start at column 0)
        INDENTED_MARKERS,
        f"- item\n{INDENTED_MARKERS}",
    ],
)
def test_markers_nested_under_a_list_or_indented_are_not_a_trigger(body: str) -> None:
    item = {"id": 9, "body": body, "created_at": at(1)}
    assert parse_trigger(item) is None
    assert 9 not in trigger_comment_ids([item])


def test_real_markers_after_a_list_nested_fenced_example_are_still_read() -> None:
    reply = f"- example\n- ```\n{INDENTED_MARKERS}\n  ```\n"
    body = build_trigger_body(reply, "claude", 4, HEAD)
    parsed = parse_trigger({"id": 9, "body": body, "created_at": at(1)})
    assert parsed is not None and parsed.round == 4


@pytest.mark.parametrize(
    "reply",
    [
        "- example\n- ```\n  code that is never closed",
        "1. step\n   ```text\n   never closed\n\ntrailing prose",
        "- ```\n  open\n- another item",
    ],
)
def test_unclosed_list_fence_ends_with_its_list_item(reply: str) -> None:
    body = build_trigger_body(reply, "claude", 5, HEAD)
    parsed = parse_trigger({"id": 9, "body": body, "created_at": at(1)})
    assert parsed is not None and parsed.round == 5


def test_list_prefix_cannot_close_a_top_level_fence() -> None:
    body = f"```\n- ```\n{MARKERS}\n```"
    assert parse_trigger({"id": 9, "body": body, "created_at": at(1)}) is None


def test_blank_lines_do_not_end_a_list_fence() -> None:
    body = f"- ```\n\n  still code\n{INDENTED_MARKERS}\n  ```"
    assert parse_trigger({"id": 9, "body": body, "created_at": at(1)}) is None


@pytest.mark.parametrize(
    "fence_opener",
    [
        " ```",
        "  ```",
        "   ```",
        " ~~~",
        "   ~~~",
    ],
)
def test_unclosed_indented_top_level_fence_is_not_closed_by_column0_markers(
    fence_opener: str,
) -> None:
    body = f"{fence_opener}\n{MARKERS}"
    item = {"id": 9, "body": body, "created_at": at(1)}
    assert parse_trigger(item) is None
    assert 9 not in trigger_comment_ids([item])


def test_list_item_ended_by_blank_line_makes_subsequent_indented_fence_top_level() -> (
    None
):
    # Indent 1 is less than content column 2, so the list item ends and the fence is top-level.
    body = f"- a\n\n ```\n{MARKERS}"
    item = {"id": 9, "body": body, "created_at": at(1)}
    assert parse_trigger(item) is None
    assert 9 not in trigger_comment_ids([item])


def test_lazy_continuation_inside_list_item_fence_restores_trigger() -> None:
    reply = "- a\nb\n  ```\n  code"
    body = build_trigger_body(reply, "claude", 5, HEAD)
    parsed = parse_trigger({"id": 9, "body": body, "created_at": at(1)})
    assert parsed is not None and parsed.round == 5


def test_nested_list_item_fence_closes_on_return_to_outer_item() -> None:
    reply = "- item 1\n  - item 2\n    ```\n    code\n  outer item text\n"
    body = build_trigger_body(reply, "claude", 5, HEAD)
    parsed = parse_trigger({"id": 9, "body": body, "created_at": at(1)})
    assert parsed is not None and parsed.round == 5


def test_build_restorable_trigger_body_rejects_unclosed_top_level_fence() -> None:
    reply = "```\nunclosed code block"
    with pytest.raises(EvidenceContractError, match="fence"):
        build_restorable_trigger_body(reply, "claude", 1, HEAD)


def test_build_restorable_trigger_body_succeeds_on_valid_reply() -> None:
    reply = "```\nclosed\n```"
    body = build_restorable_trigger_body(reply, "claude", 1, HEAD)
    parsed = parse_trigger({"id": 1, "body": body, "created_at": at(1)})
    assert parsed is not None and parsed.round == 1
