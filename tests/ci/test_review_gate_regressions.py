"""Regression coverage for review-gate Markdown context and publication freshness."""

from __future__ import annotations

import pytest

from tests.ci.test_review_gate import (
    HEAD,
    OLD,
    FakeApi,
    assert_fails,
    comment,
    decide,
    publish,
    pull,
    verdict_text,
)


@pytest.mark.parametrize("pseudo_closer", ["    ```", "\t```"])
def test_indented_backticks_do_not_close_a_fenced_example(pseudo_closer: str) -> None:
    text = "\n".join(("```text", "example", pseudo_closer, verdict_text(), "```"))
    assert_fails(decide([comment(text)]), "no reviewer verdict")


@pytest.mark.parametrize("opening", ["> Example verdict:", "> > Nested example verdict:"])
def test_lazy_blockquote_continuations_do_not_become_verdicts(opening: str) -> None:
    text = opening + "\n" + verdict_text()
    assert_fails(decide([comment(text)]), "no reviewer verdict")


def test_backtick_in_quoted_fence_info_keeps_the_line_a_lazy_quote_paragraph() -> None:
    text = "> ```example`text\n" + verdict_text()
    assert_fails(decide([comment(text)]), "no reviewer verdict")


@pytest.mark.parametrize("marker", ["2.", "2)", "0.", "10."])
@pytest.mark.parametrize("example", ["```", "<!--"])
def test_noninterrupting_list_marker_cannot_end_a_quoted_paragraph(
    marker: str, example: str
) -> None:
    text = f"> Example paragraph\n> {marker} {example}\n" + verdict_text()
    assert_fails(decide([comment(text)]), "no reviewer verdict")


@pytest.mark.parametrize("marker", ["-", "1."])
def test_inline_html_comment_survives_lazy_list_continuations(marker: str) -> None:
    text = f"{marker} Example <!--\n" + verdict_text() + "\n-->"
    assert_fails(decide([comment(text)]), "no reviewer verdict")


@pytest.mark.parametrize("prefix", ["- <!--", "1. <!--", "> <!--", "- > <!--"])
def test_container_exit_cannot_expose_an_unclosed_html_comment(prefix: str) -> None:
    text = prefix + "\n" + verdict_text() + "\n-->"
    assert_fails(decide([comment(text)]), "no reviewer verdict")


@pytest.mark.parametrize("prefix", ["- <!--", "1. <!--", "> <!--", "- > <!--"])
def test_comment_closer_outside_its_container_cannot_expose_a_verdict(prefix: str) -> None:
    # Markdown renders this out-of-container closer as --&gt;, leaving the HTML
    # comment open. It cannot make the following verdict visible to the reader.
    text = prefix + "\n-->\n" + verdict_text()
    assert_fails(decide([comment(text)]), "no reviewer verdict")


@pytest.mark.parametrize(
    ("prefix", "closer"),
    [("- <!--", "  -->"), ("1. <!--", "   -->"), ("> <!--", "> -->"), ("- > <!--", "  > -->")],
)
def test_verdict_after_comment_closes_inside_its_container_counts(prefix: str, closer: str) -> None:
    text = prefix + "\n" + closer + "\n" + verdict_text()
    assert decide([comment(text)]).passed


@pytest.mark.parametrize("marker", ["-", "*", "1."])
@pytest.mark.parametrize("padding", ["", " ", "\t"])
def test_empty_quoted_list_fence_deindent_starts_a_lazy_quoted_paragraph(
    marker: str, padding: str
) -> None:
    indent = " " * (len(marker) + 1)
    text = f"> {marker}{padding}\n> {indent}```\n> Example\n" + verdict_text()
    assert_fails(decide([comment(text)]), "no reviewer verdict")


def test_nested_quote_marker_inside_quoted_fence_is_literal_content() -> None:
    text = "> ```\n> > ```\n> ```\n> Example\n" + verdict_text()
    assert_fails(decide([comment(text)]), "no reviewer verdict")


def test_list_wrapped_fenced_verdict_example_stays_hidden() -> None:
    indented = "\n".join("  " + line for line in verdict_text().splitlines())
    text = "- ```text\n\n" + indented + "\n  ```\n"
    assert_fails(decide([comment(text)]), "no reviewer verdict")


def test_numbered_list_wrapped_fenced_verdict_example_stays_hidden() -> None:
    indented = "\n".join("   " + line for line in verdict_text().splitlines())
    text = "1. ```text\n\n" + indented + "\n   ```\n"
    assert_fails(decide([comment(text)]), "no reviewer verdict")


def test_list_fence_closer_uses_indentation_relative_to_the_list_content() -> None:
    valid = "- ```text\n  example\n     ```\n" + verdict_text()
    assert decide([comment(valid)]).passed
    indented = "\n".join("  " + line for line in verdict_text().splitlines())
    pseudo = "- ```text\n      ```\n" + indented + "\n  ```\n"
    assert_fails(decide([comment(pseudo)]), "no reviewer verdict")
    unindented = "- ```text\n      ```\n" + verdict_text() + "\n  ```\n"
    assert decide([comment(unindented)]).passed


def test_list_wrapped_lazy_quote_verdict_example_stays_hidden() -> None:
    indented = "\n".join("  " + line for line in verdict_text().splitlines())
    assert_fails(decide([comment("- > Example\n" + indented)]), "no reviewer verdict")


@pytest.mark.parametrize("opening", ["- > Example", "1. > Example", "* > Example", "- - > Example"])
def test_unindented_lazy_verdict_after_list_wrapped_quote_stays_hidden(opening: str) -> None:
    assert_fails(decide([comment(opening + "\n" + verdict_text())]), "no reviewer verdict")


def test_unindented_verdict_after_list_wrapped_quoted_fence_counts() -> None:
    assert decide([comment("- > ```\n" + verdict_text())]).passed


@pytest.mark.parametrize(
    ("opening", "continuation"),
    [
        ("- > ```", "  "),
        ("1. > ```", "   "),
        ("- - > ```", "    "),
    ],
    ids=["bullet", "numbered", "nested-list"],
)
def test_list_wrapped_quoted_fence_then_lazy_paragraph_stays_hidden(
    opening: str, continuation: str
) -> None:
    text = (
        opening
        + "\n"
        + continuation
        + "> ```\n"
        + continuation
        + "> Example\n"
        + verdict_text()
    )
    assert_fails(decide([comment(text)]), "no reviewer verdict")


def test_quoted_list_fence_deindent_reprocesses_the_quoted_paragraph() -> None:
    text = "> - ```\n> Example\n" + verdict_text()
    assert_fails(decide([comment(text)]), "no reviewer verdict")


def test_list_fence_deindent_reprocesses_the_following_quote() -> None:
    text = "- ```\n> Example\n  ```\n" + verdict_text()
    assert_fails(decide([comment(text)]), "no reviewer verdict")


def test_list_context_survives_a_fence_before_a_quoted_fence_and_lazy_paragraph() -> None:
    text = "- ```\n  ```\n  > ```\n> Example\n" + verdict_text()
    assert_fails(decide([comment(text)]), "no reviewer verdict")


@pytest.mark.parametrize(
    "quoted_example",
    [
        "> <!--\n> hidden\n> -->\n> Example verdict:\n",
        "> ```\n> sample\n> ```\n> Example verdict:\n",
    ],
)
def test_quote_local_comment_and_fence_syntax_cannot_expose_a_lazy_verdict(
    quoted_example: str,
) -> None:
    assert_fails(decide([comment(quoted_example + verdict_text())]), "no reviewer verdict")


@pytest.mark.parametrize(
    ("opening", "closing"),
    [("```text", "```"), ("<!--", "-->")],
    ids=["fence", "html-comment"],
)
def test_top_level_block_interrupting_a_quote_keeps_its_verdict_example_hidden(
    opening: str, closing: str
) -> None:
    text = f"> Example\n{opening}\n\n" + verdict_text() + f"\n{closing}\n"
    assert_fails(decide([comment(text)]), "no reviewer verdict")


@pytest.mark.parametrize(
    "prefix",
    [
        "> ```\n> sample\n> ```\n",
        "> <!--\n> sample\n> -->\n",
        "> Example\n# Review result\n",
        "> Example\n<!-- block ends here -->\n",
    ],
)
def test_valid_block_interrupts_end_lazy_quote_context(prefix: str) -> None:
    assert decide([comment(prefix + verdict_text())]).passed


def test_comment_marker_inside_a_fence_does_not_escape_through_an_indented_pseudo_closer() -> None:
    text = "\n".join(("```html", "<!-- literal example", "    ```", verdict_text(), "```"))
    assert_fails(decide([comment(text)]), "no reviewer verdict")


@pytest.mark.parametrize(
    "prefix",
    [
        "```text\nexample only\n```\n",
        "> Example only:\n\n",
        "> Example only:\n>\n",
        "<!--\n> ``` example only\n-->\n",
    ],
)
def test_genuine_verdicts_after_valid_markdown_block_endings_count(prefix: str) -> None:
    assert decide([comment(prefix + verdict_text())]).passed


def test_tight_numbered_sibling_fence_keeps_verdict_hidden() -> None:
    indented = "\n".join("   " + line for line in (verdict_text() + "\n## Evidence").splitlines())
    text = "1. Finding\n2. ~~~text\n" + indented + "\n   ~~~"
    assert_fails(decide([comment(text)]), "no reviewer verdict")


def test_tight_numbered_sibling_quote_keeps_lazy_verdict_hidden() -> None:
    text = "1. Finding\n2. > Example\n" + verdict_text() + "\n## Evidence"
    assert_fails(decide([comment(text)]), "no reviewer verdict")


def test_noninterrupting_numbered_marker_keeps_lazy_quote_verdict_hidden() -> None:
    text = "> Example\n2. Finding\n" + verdict_text() + "\n## Evidence"
    assert_fails(decide([comment(text)]), "no reviewer verdict")


def test_thematic_break_before_indented_fence_keeps_verdict_hidden() -> None:
    text = "* * *\n  ~~~\n" + verdict_text() + "\n## Evidence\n  ~~~"
    assert_fails(decide([comment(text)]), "no reviewer verdict")


def test_inline_unclosed_comment_cannot_hide_a_real_fence() -> None:
    text = "x <!--\n~~~\n-->\n" + verdict_text() + "\n## Evidence"
    assert_fails(decide([comment(text)]), "no reviewer verdict")


@pytest.mark.parametrize("first_comment", ["<!--\n-->", "<!-- closed -->"])
def test_reopened_inline_comment_cannot_hide_a_real_fence(first_comment: str) -> None:
    text = first_comment + " x <!--\n~~~\n-->\n" + verdict_text() + "\n## Evidence"
    assert_fails(decide([comment(text)]), "no reviewer verdict")


@pytest.mark.parametrize(
    "prefix",
    ["1. Finding\n2. Finding\n\n", "* * *\n", "<!-- closed -->\n"],
    ids=["tight-numbered-list", "thematic-break", "closed-html-comment"],
)
def test_genuine_verdict_after_list_break_or_comment_counts(prefix: str) -> None:
    assert decide([comment(prefix + verdict_text() + "\n## Evidence")]).passed


class ChangingPullApi(FakeApi):
    def __init__(self, pulls: list[dict], comments: list[dict], **kwargs) -> None:
        super().__init__(pulls[0], comments, **kwargs)
        self._pulls = iter(pulls)

    def get_json(self, path: str) -> dict:
        self.paths.append(path)
        return next(self._pulls)


def test_head_change_before_normal_publication_suppresses_the_status() -> None:
    api = ChangingPullApi([pull(sha=HEAD), pull(sha=OLD)], [comment()])
    assert publish(api) is None
    assert api.posts == []


def test_pr_closed_before_normal_publication_suppresses_the_status() -> None:
    api = ChangingPullApi([pull(), pull(state="closed")], [comment()])
    assert publish(api) is None
    assert api.posts == []


def test_head_change_before_best_effort_error_publication_suppresses_the_status() -> None:
    api = ChangingPullApi(
        [pull(sha=HEAD), pull(sha=OLD)],
        [],
        comments_error=RuntimeError("comment read failed"),
    )
    with pytest.raises(RuntimeError, match="comment read failed"):
        publish(api)
    assert api.posts == []


def test_pr_closed_before_best_effort_error_publication_suppresses_the_status() -> None:
    api = ChangingPullApi(
        [pull(), pull(state="closed")],
        [],
        comments_error=RuntimeError("comment read failed"),
    )
    with pytest.raises(RuntimeError, match="comment read failed"):
        publish(api)
    assert api.posts == []


def test_head_change_after_failed_normal_publish_suppresses_the_error_status() -> None:
    api = ChangingPullApi(
        [pull(sha=HEAD), pull(sha=HEAD), pull(sha=OLD)],
        [comment()],
        post_errors=[RuntimeError("publish failed")],
    )
    with pytest.raises(RuntimeError, match="publish failed"):
        publish(api)
    assert api.posts == []
