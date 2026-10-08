"""Chat markdown to the stored document."""
import pytest

from app.services.bundle.markdown import to_doc
from app.services.message_content import normalize_content

USERS = {"1": ("mention", 7, "alice")}


def resolve(author_id):
    return USERS.get(author_id, ("text", "@unknown"))


def convert(markdown):
    doc = to_doc(markdown, resolve)
    assert normalize_content(doc, allow_empty=True) is not None
    return doc["content"]


def text(value, *marks):
    node = {"type": "text", "text": value}
    if marks:
        node["marks"] = [{"type": m} if isinstance(m, str) else m for m in marks]
    return node


def paragraph(*nodes):
    return {"type": "paragraph", "content": list(nodes)} if nodes else {"type": "paragraph"}


def test_each_line_is_a_paragraph_and_blank_lines_are_empty_paragraphs():
    assert convert("one\n\ntwo") == [paragraph(text("one")), paragraph(), paragraph(text("two"))]


def test_windows_line_endings():
    assert convert("a\r\nb") == [paragraph(text("a")), paragraph(text("b"))]


def test_emphasis_marks():
    assert convert("**b** *i* _i2_ ~~s~~") == [paragraph(
        text("b", "bold"), text(" "), text("i", "italic"), text(" "),
        text("i2", "italic"), text(" "), text("s", "strike"))]


def test_marks_nest():
    assert convert("***x***") == [paragraph(text("x", "bold", "italic"))]
    assert convert("**a *b* c**") == [paragraph(
        text("a ", "bold"), text("b", "bold", "italic"), text(" c", "bold"))]


def test_inline_code_is_not_parsed_further():
    assert convert("`a *b* <@1>`") == [paragraph(text("a *b* <@1>", "code"))]


def test_code_inside_emphasis_keeps_both_marks():
    assert convert("**`x`**") == [paragraph(text("x", "bold", "code"))]


def test_link_only_for_http_urls():
    link = {"type": "link", "attrs": {"href": "https://a.com/p"}}
    assert convert("[docs](https://a.com/p)") == [paragraph(text("docs", link))]
    assert convert("[x](javascript:alert(1))") == [paragraph(text("[x](javascript:alert(1))"))]
    assert convert("[x](/relative)") == [paragraph(text("[x](/relative)"))]


def test_link_text_can_carry_emphasis():
    link = {"type": "link", "attrs": {"href": "https://a.com"}}
    assert convert("[**hi**](https://a.com)") == [paragraph(text("hi", link, "bold"))]


def test_bare_urls_stay_plain_and_intact():
    assert convert("see https://a.com/x_y_z now") == [paragraph(text("see https://a.com/x_y_z now"))]
    assert convert("*https://a.com/x_y*") == [paragraph(text("https://a.com/x_y", "italic"))]
    assert convert("**https://a.com/x**") == [paragraph(text("https://a.com/x", "bold"))]


def test_unmatched_delimiters_stay_literal():
    assert convert("a ** b") == [paragraph(text("a ** b"))]
    assert convert("`open") == [paragraph(text("`open"))]
    assert convert("2 * 3 * 4") == [paragraph(text("2 * 3 * 4"))]
    assert convert("snake_case_name") == [paragraph(text("snake_case_name"))]
    assert convert("**bold*") == [paragraph(text("**bold*"))]


def test_unsupported_syntax_stays_literal():
    for source in ("__under__", "||spoiler||", "# heading", "- item", "1. item"):
        assert convert(source) == [paragraph(text(source))]


def test_backslash_escapes():
    assert convert(r"\*not italic\*") == [paragraph(text("*not italic*"))]


def test_mentions():
    assert convert("hi <@1> and <@!1> and <@2>") == [paragraph(
        text("hi "), {"type": "mention", "attrs": {"id": "7", "label": "alice"}},
        text(" and "), {"type": "mention", "attrs": {"id": "7", "label": "alice"}},
        text(" and @unknown"))]


def test_mention_inside_bold_is_a_node_without_marks():
    nodes = convert("**<@1> hi**")[0]["content"]
    assert nodes[0] == {"type": "mention", "attrs": {"id": "7", "label": "alice"}}
    assert nodes[1] == text(" hi", "bold")


def test_fenced_code_block_with_language():
    assert convert("```py\nprint(1)\nx = 2\n```\nafter") == [
        {"type": "codeBlock", "attrs": {"language": "py"},
         "content": [text("print(1)\nx = 2")]},
        paragraph(text("after"))]


def test_fenced_code_block_without_language_and_unterminated():
    assert convert("```\na\n```") == [{"type": "codeBlock", "content": [text("a")]}]
    assert convert("```\na\n**b**") == [{"type": "codeBlock", "content": [text("a\n**b**")]}]


def test_single_line_fence():
    assert convert("```x = 1```") == [{"type": "codeBlock", "content": [text("x = 1")]}]


def test_quote_lines_form_one_blockquote():
    assert convert("> a\n> **b**\nplain") == [
        {"type": "blockquote", "content": [paragraph(text("a")), paragraph(text("b", "bold"))]},
        paragraph(text("plain"))]


def test_triple_quote_takes_the_rest_of_the_message():
    assert convert("before\n>>> a\nb\n\nc") == [
        paragraph(text("before")),
        {"type": "blockquote", "content": [
            paragraph(text("a")), paragraph(text("b")), paragraph(), paragraph(text("c"))]}]


def test_adjacent_text_with_the_same_marks_merges():
    nodes = convert("a *b* *c*")[0]["content"]
    assert nodes == [text("a "), text("b", "italic"), text(" "), text("c", "italic")]
    nodes = convert("a \\* b")[0]["content"]
    assert nodes == [text("a * b")]


@pytest.mark.parametrize("source", ["", "\n", "**", "``", "> ", "```", "<@1>"])
def test_never_emits_empty_text_nodes(source):
    def walk(node):
        if node["type"] == "text":
            assert node["text"]
        for child in node.get("content", []):
            walk(child)
    for block in convert(source):
        walk(block)
