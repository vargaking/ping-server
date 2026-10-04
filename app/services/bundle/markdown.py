"""Chat markdown (Discord flavour, not CommonMark) to the stored document."""
import re
from typing import Callable

# ("mention", user id, username) or ("text", "@name")
Mention = tuple
MentionResolver = Callable[[str], Mention]

_MENTION = re.compile(r"<@!?([\w-]+)>")
_BARE_URL = re.compile(r"https?://[^\s<>]+")
_ESCAPABLE = "\\*_~`|[]"
_TRAILING_URL_PUNCTUATION = "*_~|.,;:!?'\""


def to_doc(markdown: str, resolve_mention: MentionResolver) -> dict:
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return {"type": "doc", "content": _blocks(lines, resolve_mention)}


def _blocks(lines: list[str], resolve: MentionResolver) -> list[dict]:
    blocks: list[dict] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            block, i = _code_block(lines, i)
            blocks.append(block)
        elif line.startswith(">>>") and line[3:4] in ("", " "):
            rest = [line[4:]] + lines[i + 1:]
            blocks.append(_quote(rest, resolve))
            break
        elif line.startswith("> ") or line == ">":
            quoted = []
            while i < len(lines) and (lines[i].startswith("> ") or lines[i] == ">"):
                quoted.append(lines[i][2:])
                i += 1
            blocks.append(_quote(quoted, resolve))
        else:
            blocks.append(_paragraph(line, resolve))
            i += 1
    return blocks


def _quote(lines: list[str], resolve: MentionResolver) -> dict:
    return {"type": "blockquote", "content": [_paragraph(line, resolve) for line in lines]}


def _code_block(lines: list[str], start: int) -> tuple[dict, int]:
    first = lines[start][3:]
    language = ""
    body: list[str] = []
    closed_here = first.rstrip().endswith("```") and len(first.strip()) >= 3
    if closed_here:
        body = [first.rstrip()[:-3]]
        next_line = start + 1
    else:
        match = re.fullmatch(r"([\w+#.-]+)\s*", first)
        if match:
            language = match.group(1)
        elif first:
            body = [first]
        next_line = start + 1
        while next_line < len(lines):
            line = lines[next_line]
            next_line += 1
            if line.rstrip().endswith("```"):
                tail = line.rstrip()[:-3]
                if tail:
                    body.append(tail)
                break
            body.append(line)
    text = "\n".join(body)
    block: dict = {"type": "codeBlock"}
    if language:
        block["attrs"] = {"language": language}
    if text:
        block["content"] = [{"type": "text", "text": text}]
    return block, next_line


def _paragraph(line: str, resolve: MentionResolver) -> dict:
    nodes = _merge(_inline(line, resolve, ()))
    return {"type": "paragraph", "content": nodes} if nodes else {"type": "paragraph"}


Marks = tuple  # of (type, href or None), outermost first


def _text(text: str, marks: Marks) -> list:
    return [(text, marks)] if text else []


def _with(marks: Marks, mark: tuple) -> Marks:
    return marks if mark in marks else marks + (mark,)


def _inline(text: str, resolve: MentionResolver, marks: Marks) -> list:
    """Pieces of the line: (text, marks) for text and a node dict for a mention."""
    out: list = []
    plain: list[str] = []

    def flush():
        out.extend(_text("".join(plain), marks))
        plain.clear()

    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text) and text[i + 1] in _ESCAPABLE:
            plain.append(text[i + 1])
            i += 2
        elif ch == "`":
            end = _code_span(text, i)
            if end is None:
                run = _run(text, i)
                plain.append(text[i:i + run])
                i += run
            else:
                flush()
                run, close, stop = end
                out.extend(_text(_code_text(text[i + run:close]), _with(marks, ("code", None))))
                i = stop
        elif ch == "<" and (mention := _MENTION.match(text, i)):
            flush()
            resolved = resolve(mention.group(1))
            if resolved[0] == "mention":
                out.append({"type": "mention",
                            "attrs": {"id": str(resolved[1]), "label": resolved[2]}})
            else:
                out.extend(_text(resolved[1], marks))
            i = mention.end()
        elif ch == "[" and (link := _link(text, i)):
            flush()
            label, href, stop = link
            out.extend(_inline(label, resolve, _with(marks, ("link", href))))
            i = stop
        elif ch == "h" and (url := _BARE_URL.match(text, i)) and _url_start_ok(text, i):
            literal = _trim_url(url.group())
            plain.append(literal)
            i += len(literal)
        elif ch in "*_~":
            handled = _emphasis(text, i, resolve, marks)
            if handled is None:
                run = _run(text, i)
                plain.append(text[i:i + run])
                i += run
            else:
                flush()
                pieces, stop = handled
                out.extend(pieces)
                i = stop
        else:
            plain.append(ch)
            i += 1
    flush()
    return out


def _url_start_ok(text: str, i: int) -> bool:
    return i == 0 or not text[i - 1].isalnum()


def _trim_url(url: str) -> str:
    while url and (url[-1] in _TRAILING_URL_PUNCTUATION
                   or (url[-1] == ")" and url.count(")") > url.count("("))):
        url = url[:-1]
    return url


def _run(text: str, i: int) -> int:
    j = i
    while j < len(text) and text[j] == text[i]:
        j += 1
    return j - i


def _code_span(text: str, i: int) -> tuple[int, int, int] | None:
    """(opening run, start of the closing run, end of the closing run) for a
    backtick span that opens at *i*."""
    run = _run(text, i)
    j = i + run
    while j < len(text):
        if text[j] == "`":
            closing = _run(text, j)
            if closing == run:
                return (run, j, j + run) if j > i + run else None
            j += closing
        else:
            j += 1
    return None


def _code_text(text: str) -> str:
    if len(text) > 2 and text[0] == " " and text[-1] == " " and text.strip():
        return text[1:-1]
    return text


def _link(text: str, i: int) -> tuple[str, str, int] | None:
    depth = 0
    j = i
    while j < len(text):
        if text[j] == "\\":
            j += 2
            continue
        if text[j] == "[":
            depth += 1
        elif text[j] == "]":
            depth -= 1
            if depth == 0:
                break
        j += 1
    else:
        return None
    if j + 1 >= len(text) or text[j + 1] != "(" or j == i + 1:
        return None
    k = j + 2
    depth = 1
    while k < len(text) and depth:
        depth += {"(": 1, ")": -1}.get(text[k], 0)
        k += 1
    if depth:
        return None
    href = text[j + 2:k - 1].strip()
    if not re.fullmatch(r"https?://\S+", href):
        return None
    return text[i + 1:j], href, k


_EMPHASIS = {
    ("*", 1): ("italic",), ("*", 2): ("bold",), ("*", 3): ("bold", "italic"),
    ("_", 1): ("italic",), ("~", 2): ("strike",),
}


def _emphasis(text: str, i: int, resolve: MentionResolver, marks: Marks):
    char = text[i]
    run = _run(text, i)
    types = _EMPHASIS.get((char, run))
    if types is None:
        return None
    start = i + run
    if start >= len(text) or text[start].isspace():
        return None
    if char == "_" and i > 0 and text[i - 1].isalnum():
        return None
    close = _find_close(text, start, char, run)
    if close is None:
        return None
    inner = text[start:close]
    stop = close + run
    for mark in types:
        marks = _with(marks, (mark, None))
    return _inline(inner, resolve, marks), stop


def _find_close(text: str, start: int, char: str, run: int) -> int | None:
    j = start
    while j < len(text):
        ch = text[j]
        if ch == "\\":
            j += 2
        elif ch == "`":
            span = _code_span(text, j)
            j = span[2] if span else j + _run(text, j)
        elif ch == "h" and (url := _BARE_URL.match(text, j)) and _url_start_ok(text, j):
            j += max(len(_trim_url(url.group())), 1)
        elif ch == char:
            length = _run(text, j)
            intraword = char == "_" and j + length < len(text) and text[j + length].isalnum()
            if length == run and not text[j - 1].isspace() and not intraword:
                return j
            j += length
        else:
            j += 1
    return None


def _merge(pieces: list) -> list[dict]:
    nodes: list[dict] = []
    last_marks = None
    for piece in pieces:
        if isinstance(piece, dict):
            nodes.append(piece)
            last_marks = None
            continue
        text, marks = piece
        if not text:
            continue
        if nodes and last_marks == marks and nodes[-1]["type"] == "text":
            nodes[-1]["text"] += text
            continue
        node: dict = {"type": "text", "text": text}
        if marks:
            node["marks"] = [_mark(m) for m in marks]
        nodes.append(node)
        last_marks = marks
    return nodes


def _mark(mark: tuple) -> dict:
    kind, href = mark
    return {"type": kind, "attrs": {"href": href}} if href else {"type": kind}
