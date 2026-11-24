"""Make a workflow file with syntax faults parse again, so the rest can be drawn.

`ast.parse` and `libcst.parse_module` stop at the first fault and give no tree,
so one missing bracket takes the whole diagram away. `repair_source` asks the
stock parser where it stopped, replaces that statement by something that
parses, and asks again. No line is added or removed, so every line number of
what is left stays true, and the faults come back with the text they replaced.

The repaired text is for drawing only. It is never run and never written back
to the user's file: a masked statement is a `pass`, a broken header keeps its
name and loses its arguments.

Nothing here reads the wording of a parser message to decide what to do. The
decisions rest on the exception class (`IndentationError`, `TabError`), on the
line it names, and on a small scan of the text (`_Scan`) that says where
statements start and end and whether a string or bracket is left open. The
messages are passed on to the caller unchanged.

One user fault should be one reported fault. Three things serve that:

- a statement is replaced whole, with the lines its bracket holds, and a
  statement that is not a header takes the deeper lines under it along;
- a block with two indents is repaired on the side that moves fewer
  statements, which is the side the user got wrong more often than the line
  the parser names;
- a triple-quoted string that is left open is found by its quote, not by the
  line the parser names (the last quote of the file), and what the parser
  refuses in its text joins that one fault.
"""

from __future__ import annotations

import ast
import keyword
import re
import warnings
from bisect import bisect_right
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal

__all__ = ["RepairedSource", "SourceFault", "repair_source"]

Repair = Literal["masked", "stubbed", "reindented", "filled"]


@dataclass(frozen=True)
class SourceFault:
    """One fault of the user's text and what was put in its place.

    `repair` says how much of `source` is still in the repaired text:

    - ``"masked"``: the statement is a `pass` now, nothing of it is left;
    - ``"stubbed"``: a header or decorator kept its keyword and name and lost
      the rest (`class Name:`, `def name(*_a, **_k):`, `@name`, `if True:`);
      the body under it is untouched;
    - ``"reindented"``: only leading whitespace changed;
    - ``"filled"``: a header with nothing under it was given a `...` body.

    `first_line` is the line to look up the owner of (the task or workflow
    the fault is in): it holds the replacement, where `line` may be blank now.
    """

    line: int  # 1-based line of the fault, within first_line..last_line: the
    #            one the parser named, unless the cause was found on another
    column: int | None  # 1-based, when the parser gave one for `line`
    message: str  # the parser's own message
    first_line: int  # first line of what was replaced
    last_line: int  # last line of what was replaced
    source: str  # the original text of lines first_line..last_line
    repair: Repair


@dataclass(frozen=True)
class RepairedSource:
    text: str  # parses with ast.parse when `parsed` is true
    faults: tuple[SourceFault, ...]  # in line order, never overlapping
    parsed: bool  # False: gave up, `text` is the last attempt


def repair_source(text: str, *, max_rounds: int = 40) -> RepairedSource:
    """Return `text` with every statement the parser refuses replaced.

    The result has the line count of `text`, and every line outside the
    faults' spans is the input line. One round is one parse and work linear
    in the text; after `max_rounds` repairs that still do not parse, the last
    attempt comes back with `parsed` false. Never raises.

    A leading byte order mark is dropped without a fault: Python ignores it
    when it reads a file, and `ast.parse` refuses it in a `str`.
    """
    try:
        return _repair(text, max_rounds)
    except Exception:  # a fault in the repair must not cost the caller its fallback
        return RepairedSource(text=text, faults=(), parsed=False)


def _repair(text: str, max_rounds: int) -> RepairedSource:
    work = _Work(text)
    parsed = False
    max_rounds = max(max_rounds, 0)  # the text is parsed once in any case
    for done in range(max_rounds + 1):
        refusal = _refusal(work.text())
        if refusal is None:
            parsed = True
            break
        if done == max_rounds or not isinstance(refusal, (SyntaxError, ValueError)):
            break  # out of rounds, or too deep for the parser: nothing to mask
        if not work.round(refusal):
            break
    return RepairedSource(text=work.text(), faults=work.faults(), parsed=parsed)


def _refusal(text: str) -> Exception | None:
    """What the parser raises for `text`, or None when it parses."""
    try:
        with warnings.catch_warnings():
            # an invalid escape is a warning; under `-W error` it would be a fault
            warnings.simplefilter("ignore")
            ast.parse(text)
    except Exception as exc:  # SyntaxError; ValueError for a null byte; RecursionError
        return exc
    return None


# --------------------------------------------------------------------------
# The scan: where statements start and end, without the parser
# --------------------------------------------------------------------------

_WS = " \t\f"  # what the tokenizer takes for indentation
_LINE_END = re.compile(r"(\r\n|\r|\n)")  # the tokenizer's line ends, no others
_MARK = re.compile(r"""[#()\[\]{}'"]""")
_TRIPLE = re.compile("'''|" + '"""')
_STRING_END = {
    "'": re.compile(r"(?:[^'\\]|\\.)*'"),
    '"': re.compile(r'(?:[^"\\]|\\.)*"'),
    "'''": re.compile(r"(?:[^'\\]|\\.|'(?!''))*'''"),
    '"""': re.compile(r'(?:[^"\\]|\\.|"(?!""))*"""'),
}
_NAME = r"[^\W\d]\w*"
# the keywords that start a statement with a block under it
_HEADER = re.compile(
    r"(async[ \t]+def|async[ \t]+for|async[ \t]+with|class|def|if|elif|else|try"
    r"|except[ \t]*\*|except|finally|for|while|with|match|case)\b"
)


def _lead(line: str) -> str:
    return line[: len(line) - len(line.lstrip(_WS))]


def _width(line: str, tab: int = 8) -> int:
    """The column the tokenizer gives the first character of `line`."""
    col = 0
    for ch in line:
        if ch == " ":
            col += 1
        elif ch == "\t":
            col = (col // tab + 1) * tab
        elif ch == "\f":
            col = 0
        else:
            break
    return col


def _glues(line: str) -> bool:
    """True when `line` ends with a backslash that holds the next line to it."""
    return (len(line) - len(line.rstrip("\\"))) % 2 == 1


class _Scan:
    """Statements of a text that may not parse.

    A line starts a statement when no bracket, string or backslash holds it to
    the line before. Brackets are counted, not matched, and a one-line string
    with no end stops at the end of its line, so one fault does not hide the
    statements after it. Two things can still run to the end of the text: a
    bracket and a triple-quoted string that are never closed. `ragged`,
    `open_line` and `open_quote` say so, and the caller then guesses the
    extent from the indentation instead.
    """

    def __init__(self, lines: list[str]) -> None:
        starts: list[bool] = []
        code_end: list[int] = []  # where the comment starts, or the length
        quotes: list[tuple[int, int]] = []  # every triple quote, in order
        opened: list[int] = []  # the lines of the brackets that are open
        delim: str | None = None  # the string a line starts in
        glued = False  # a backslash ended the line before
        for i, line in enumerate(lines):
            starts.append(delim is None and not opened and not glued)
            pos, end = 0, len(line)
            while True:
                if delim is not None:
                    closing = _STRING_END[delim].match(line, pos)
                    if closing is None:
                        if len(delim) == 1 and not _glues(line):
                            delim = None  # no end: it stops with its line
                        break
                    pos = closing.end()
                    if len(delim) == 3:
                        quotes.append((i, pos - 3))
                    delim = None
                mark = _MARK.search(line, pos)
                if mark is None:
                    break
                ch, pos = mark.group(), mark.end()
                if ch == "#":
                    end = mark.start()
                    break
                if ch in "([{":
                    opened.append(i)
                elif ch in ")]}":
                    if opened:
                        opened.pop()
                elif line.startswith(ch * 3, mark.start()):
                    delim, pos = ch * 3, mark.start() + 3
                    quotes.append((i, mark.start()))
                elif _STRING_END[ch].match(line, pos) or not _TRIPLE.search(line, pos):
                    delim = ch
                # else an apostrophe in a sentence: with a string left open
                # above, text is read as code, and the triple quote behind it
                # on the line has to be counted
            code_end.append(end)
            glued = delim is None and end == len(line) and _glues(line)

        self.lines = lines
        self.code_end = code_end
        self.firsts: list[int] = []  # first line of each statement
        self.lasts: list[int] = []  # last line of each statement
        for i, line in enumerate(lines):
            if starts[i]:
                if len(self.lasts) < len(self.firsts):
                    self.lasts.append(i - 1)
                if line[: code_end[i]].strip(_WS):
                    self.firsts.append(i)
        if len(self.lasts) < len(self.firsts):
            self.lasts.append(len(lines) - 1)
        self.quotes = quotes
        self.open_quote = delim is not None and len(delim) == 3
        self.open_line = opened[0] if opened else None
        # the last statement never ends: its extent is not known
        self.ragged = delim is not None or bool(opened) or glued
        self._widths: list[int] | None = None

    def holding(self, line: int) -> int:
        """Index of the statement that holds `line`, or of the last one before it."""
        return max(0, bisect_right(self.firsts, line) - 1)

    def code(self, line: int) -> str:
        return self.lines[line][: self.code_end[line]].rstrip(_WS)

    def is_header(self, statement: int) -> bool:
        return self.code(self.lasts[statement]).endswith(":")

    def keyword(self, statement: int) -> str:
        """The keyword `statement` starts with, without spaces, or ""."""
        found = _HEADER.match(self.lines[self.firsts[statement]].lstrip(_WS))
        return "".join(found.group().split()) if found else ""

    def widths(self) -> list[int]:
        if self._widths is None:
            self._widths = [_width(self.lines[first]) for first in self.firsts]
        return self._widths

    def above(self, statement: int) -> Iterator[int]:
        """The headers `statement` stands under, the nearest first."""
        widths = self.widths()
        level = widths[statement]
        for i in range(statement - 1, -1, -1):
            if widths[i] < level:
                level = widths[i]
                yield i


def _may_follow(width: int, before: int, header: bool, levels: list[int]) -> bool:
    """True when a statement at `width` may follow one at `before` under `levels`."""
    if header:
        return width > before
    return width == before or (width < before and width in levels)


def _usual_step(widths: list[int]) -> int:
    """The step by which most blocks of a text are indented."""
    steps: Counter[int] = Counter()
    levels = [0]
    for width in widths:
        while levels[-1] > width:
            levels.pop()
        if width > levels[-1]:
            steps[width - levels[-1]] += 1
            levels.append(width)
    return steps.most_common(1)[0][0] if steps else 0


# --------------------------------------------------------------------------
# What a statement is replaced by
# --------------------------------------------------------------------------

_DEFINITION = re.compile(rf"(async[ \t]+def|def|class)[ \t]+({_NAME})")
_DECORATOR = re.compile(rf"@[ \t]*({_NAME}(?:[ \t]*\.[ \t]*{_NAME})*)")
_TRY = re.compile(r"([ \t\f]*)try[ \t]*:")
# A clause is replaced by one that takes the same place in its statement, so
# the clauses around it stay valid: an `else` is still an `else`.
_CLAUSE_STUB = {
    "elif": "elif True:",
    "else": "else:",
    "try": "try:",
    "except": "except Exception:",
    "except*": "except* Exception:",
    "finally": "finally:",
    "match": "match 0:",
    "case": "case _:",
}
# what a clause may come after, at its own indent
_FOLLOWS = {
    "elif": ("if", "elif"),
    "else": ("if", "elif", "for", "asyncfor", "while", "try", "except", "except*"),
    "except": ("try", "except"),
    "except*": ("try", "except*"),
    "finally": ("try", "except", "except*", "else"),
}
_ANY_HEADER = "if True:"
_MASK = "pass"
_BODY = " ..."
_RANK = {"reindented": 0, "filled": 1, "stubbed": 2, "masked": 3}

# The one text of our own. A triple-quoted string with no end is reported by
# the parser at the last quote of the file; when the scan finds the quote that
# really is open (`_Work._open_quote`), the parser's message belongs to
# another line and may be about something else, so the fault gets this one. It
# is the parser's wording without its "(detected at line N)".
_UNCLOSED_STRING = "unterminated triple-quoted string literal"

# How a quote looks that cannot open, or cannot close, a string.
_MAY_PRECEDE_STRING = "=([{,:+-*/%|&^~<>@;'\""
_LAST_WORD = re.compile(r"\w+$")
_WORD_START = re.compile(r"\w")
_STRING_PREFIX = re.compile(r"(?i:[rbuft]|br|rb|fr|rf|tr|rt)")
_KEYWORDS_BEFORE_STRING = frozenset(
    "and assert await case del elif else from if in is not or print raise return yield".split()
)
# A line that is code and not a sentence: an import, a decorator, the header
# of a definition, an assignment, a call that fills its line.
_TARGET = rf"{_NAME}(?:\.{_NAME}|\[[^\]]*\])*"
_CODE = re.compile(
    rf"from[ \t]+[.\w]+[ \t]+import[ \t]"
    rf"|import[ \t]+[.\w]+[ \t]*(?:$|,|\#|as[ \t])"
    rf"|@[.\w]+[ \t]*(?:$|\#|\(.*[(,)\[{{][ \t]*$)"
    rf"|(?:async[ \t]+)?def[ \t]+{_NAME}[ \t]*\("
    rf"|class[ \t]+{_NAME}[ \t]*(?:$|[(:])"
    rf"|{_TARGET}(?:[ \t]*,[ \t]*{_TARGET})*[ \t]*(?::[ \t]*[.\w]+(?:\[.*\])?[ \t]*)?"
    r"[-+*/%&|^@]?=(?!=)"
    rf"|{_NAME}(?:\.{_NAME})*\(.*[(,)\[{{][ \t]*$"
)
_QUOTED = re.compile(r"""("[^"]*"|'[^']*'|\#.*)""")
_WORDS = re.compile(rf"{_NAME}(?:[ \t]+{_NAME})+")
# what the parser cannot even read: a null byte, a lone surrogate
_UNREADABLE = re.compile("[\0" + chr(0xD800) + "-" + chr(0xDFFF) + "]")
_BOM = chr(0xFEFF)


def _reads_as_sentence(line: str) -> bool:
    """True for three words in a row that are no keywords.

    No single fault leaves that in code: a missing comma or operator puts two
    names side by side, not three.
    """
    for words in _WORDS.findall(_QUOTED.sub('""', line)):
        run = 0
        for word in words.split():
            run = 0 if keyword.iskeyword(word) else run + 1
            if run == 3:
                return True
    return False


@dataclass
class _Span:
    first: int
    last: int
    line: int
    column: int | None
    message: str
    repair: Repair
    # A string left open: the lines text_from..first-1 and last+1..text_to may
    # still be its text (-1: none on that side).
    text_from: int = -1
    text_to: int = -1


class _Work:
    def __init__(self, text: str) -> None:
        parts = _LINE_END.split(text[1:] if text.startswith(_BOM) else text)
        self.original = parts[0::2]
        self.ends = parts[1::2] + [""]
        self.lines = list(self.original)
        self.spans: list[_Span] = []

    def text(self) -> str:
        out = []
        after_cr = False  # the line before ended on a lone carriage return
        for line, end in zip(self.lines, self.ends):
            if after_cr and not line and end == "\n":
                # A blanked line between "\r" and "\n": left empty, the two
                # would read as one line end and the text would lose a line.
                line = " "
            out.append(line + end)
            after_cr = end == "\r"
        return "".join(out)

    def faults(self) -> tuple[SourceFault, ...]:
        out = []
        for span in sorted(self.spans, key=lambda s: s.first):
            rows = range(span.first, span.last + 1)
            source = "".join(self.original[i] + self.ends[i] for i in rows)
            out.append(
                SourceFault(
                    line=span.line + 1,
                    column=span.column,
                    message=span.message,
                    first_line=span.first + 1,
                    last_line=span.last + 1,
                    source=source[: len(source) - len(self.ends[span.last])],
                    repair=span.repair,
                )
            )
        return tuple(out)

    # ---- one round ----------------------------------------------------

    def round(self, exc: Exception) -> bool:
        """Repair what `exc` names. False when nothing could be changed."""
        scan = _Scan(self.lines)
        line = self._named_line(exc)
        if line is None or not scan.firsts:
            return False
        message = exc.msg if isinstance(exc, SyntaxError) else str(exc)
        offset = getattr(exc, "offset", None)
        column = offset if isinstance(offset, int) and offset > 0 else None
        fault = _Span(line, line, line, column, message, "masked")

        opening = self._open_quote(scan)
        if opening is not None:
            # Everything the parser says from that quote on is said about text
            # it reads inside out. The quote is the fault, wherever it stopped;
            # and when the quote may be a closing one, so is what it says
            # about the lines before it, back to the string above.
            row = scan.quotes[opening][0]
            before = scan.quotes[opening - 1][0] if opening else -1
            if line >= row or (line > before and not self._cannot_close(scan.quotes[opening])):
                return self._mask_string(scan, opening, fault)
        if self._string_text(scan, fault):
            return True
        if isinstance(exc, TabError) and self._untab(scan, fault):
            return True
        if isinstance(exc, IndentationError) and self._reindent(scan, fault):
            return True
        return self._mask(scan, fault)

    def _named_line(self, exc: Exception) -> int | None:
        lineno = getattr(exc, "lineno", None)
        if isinstance(lineno, int) and lineno >= 1:
            return min(lineno, len(self.lines)) - 1
        # No line: a null byte or a lone surrogate, which the parser refuses
        # before it reads anything. The line that holds one is the fault.
        for i, line in enumerate(self.lines):
            if _UNREADABLE.search(line):
                return i
        return None

    # ---- strings ------------------------------------------------------

    def _open_quote(self, scan: _Scan) -> int | None:
        """Which of `scan.quotes` is open, when the text ends inside a string.

        The quotes come in pairs, so with one string left open their number
        is odd and the parser pairs every quote after it the wrong way round:
        code is read as string and string as code, down to the last quote,
        which it reports. Each quote is tried as the one left open, and the
        reading in which the fewest quotes sit where no opening or closing
        quote can sit wins. A tie goes to the later quote, which is the
        reading closer to the parser's own.
        """
        if not scan.open_quote:
            return None
        quotes = scan.quotes
        opens = [self._cannot_open(q) for q in quotes]
        closes = [self._cannot_close(q) for q in quotes]
        # read as scanned: even positions open, odd positions close
        before = 0  # misplaced quotes ahead of the candidate, as scanned
        after = sum(closes[i] if i % 2 == 0 else opens[i] for i in range(len(quotes)))
        best, best_score = len(quotes) - 1, -1
        for i in range(0, len(quotes), 2):
            after -= closes[i]
            # the candidate is the odd one: it may be either end of a string
            score = before + min(opens[i], closes[i]) + after
            if best_score < 0 or score <= best_score:
                best, best_score = i, score
            before += opens[i]
            if i + 1 < len(quotes):
                after -= opens[i + 1]
                before += closes[i + 1]
        return best

    def _cannot_open(self, quote: tuple[int, int]) -> bool:
        """True when no string can open at `quote`: a sentence stands before it."""
        before = self.lines[quote[0]][: quote[1]]
        text = before.rstrip(_WS)
        if not text or text[-1] in _MAY_PRECEDE_STRING:
            return False
        word = _LAST_WORD.search(text)
        if word is None:
            return True
        if len(text) == len(before):  # nothing between the word and the quote
            return not _STRING_PREFIX.fullmatch(word.group())
        return word.group() not in _KEYWORDS_BEFORE_STRING

    def _cannot_close(self, quote: tuple[int, int]) -> bool:
        """True when no string can close at `quote`.

        A word or a backslash follows it, or it ends a line whose text before
        it ends with `=` or `(`: that is how a string starts.
        """
        line = self.lines[quote[0]]
        after = line[quote[1] + 3 :]
        if after[:1] == "\\" or _WORD_START.match(after):
            return True
        return not after.strip(_WS) and line[: quote[1]].rstrip(_WS)[-1:] in ("=", "(")

    def _mask_string(self, scan: _Scan, opening: int, fault: _Span) -> bool:
        """Mask the statement of the quote that has no partner.

        Nothing says where the text of its string ends, or starts when the
        quote is the closing one. The line of the quote is masked, and the
        span remembers how far the text may reach: to the quotes before and
        after it, which belong to other strings.
        """
        quotes = scan.quotes
        row, col = quotes[opening]
        if fault.line != row:
            fault = _Span(row, row, row, col + 1, _UNCLOSED_STRING, "masked")
        first = last = scan.firsts[scan.holding(row)]
        if not self._cannot_close(quotes[opening]):
            fault.text_from = quotes[opening - 1][0] + 1 if opening else 0
        if not self._cannot_open(quotes[opening]):
            later = quotes[opening + 1 :]
            fault.text_to = later[0][0] - 1 if later else len(self.lines) - 1
            last = min(self._continuation_end(row, _width(self.lines[first])), fault.text_to)
        lines = [_lead(self.lines[first]) + _MASK] + [""] * (max(last, row) - first)
        return self._apply(first, lines, fault)

    def _string_text(self, scan: _Scan, fault: _Span) -> bool:
        """Give a fault in the text of a string left open to that string.

        The lines around the masked quote are read as code now: what the
        parser refuses there is most likely a sentence of the string, not a
        second fault. So a fault within reach of an open string joins it,
        with every line in between. It does not when a line in between is
        plainly code (`_CODE`) at the indent of the string or left of it,
        unless the refused line reads as a sentence: such a line of code is
        where the string ends.
        """
        index = scan.holding(fault.line)
        first, last = scan.firsts[index], scan.lasts[index]
        if scan.ragged and index == len(scan.firsts) - 1:
            last = max(first, fault.line)
        for span in self.spans:
            if span.last < first <= span.text_to:
                between = range(span.last + 1, first + 1)
                start, stop = span.last + 1, min(last, span.text_to)
            elif 0 <= span.text_from <= first and last < span.first:
                between = range(span.first - 1, first - 1, -1)
                start, stop = first, span.first - 1
            else:
                continue
            if not _reads_as_sentence(self.lines[fault.line]):
                width = _width(self.lines[span.first])
                for row in between:
                    line = self.lines[row]
                    if _width(line) <= width and _CODE.match(line.lstrip(_WS)):
                        if row > span.last:
                            span.text_to = row - 1
                        else:
                            span.text_from = row + 1
                        return False
            return self._apply(start, [""] * (stop - start + 1), span, span)
        return False

    # ---- statements ---------------------------------------------------

    def _mask(self, scan: _Scan, fault: _Span) -> bool:
        """Replace the statement that holds the fault's line."""
        index = scan.holding(fault.line)
        first, last = scan.firsts[index], scan.lasts[index]
        open_ended = scan.ragged and index == len(scan.firsts) - 1
        if open_ended:
            # The statement never ends, so the scan cannot say what belongs
            # to it: it goes as far as the line the parser names.
            row = fault.line
            if self._on_its_own(scan, first, row):
                if self._replace(scan, None, row, row, fault, False):
                    return True
            last = max(first, row)
        elif self._stray_clause(scan, index, fault) or self._bare_try(scan, index, fault):
            return True
        widths = scan.widths()
        while True:
            if self._replace(scan, index, first, last, fault, open_ended):
                return True
            # It is a `pass` already and the parser still stops here: the
            # fault is in what this line follows, the statement before it at
            # its level or above.
            width = widths[index]
            index -= 1
            while index >= 0 and widths[index] > width:
                index -= 1
            if index < 0:
                return False
            first, last, open_ended = scan.firsts[index], scan.lasts[index], False
            fault.line, fault.column = first, None

    def _on_its_own(self, scan: _Scan, first: int, row: int) -> bool:
        """True when line `row` is no part of the unended statement at `first`.

        The parser names a line below the bracket that is left open, for a
        fault of its own. It is another statement when it does not look like
        the rest of this one: under a header, or not indented past it. The
        open bracket is reported in a later round.
        """
        if scan.open_line is None or row <= scan.open_line or not self.lines[row].strip(_WS):
            return False
        head = self.lines[first]
        if _HEADER.match(head.lstrip(_WS)):
            return True
        return row > self._continuation_end(first, _width(head))

    def _replace(
        self, scan: _Scan, index: int | None, first: int, last: int, fault: _Span, open_ended: bool
    ) -> bool:
        """Put a stub or a `pass` in place of the lines `first`..`last`.

        `index` is the statement they are, None for a line taken by itself.
        `open_ended` says that `last` is a guess: the statement has no end.
        """
        head = self.lines[first]
        width = _width(head)
        code = head.lstrip(_WS)
        definition = _DEFINITION.match(code)
        header = _HEADER.match(code)
        decorator = _DECORATOR.match(code)
        if open_ended and header:
            last = self._header_end(scan, last, width)
        body = self._deeper(last, width)
        # (text, repair, may stand under a decorator)
        tries: list[tuple[str, Repair, bool]] = []
        if definition:
            kind, name = definition.groups()
            kind = " ".join(kind.split())
            stub = f"class {name}:" if kind == "class" else f"{kind} {name}(*_a, **_k):"
            tries.append((stub if body else stub + _BODY, "stubbed", True))
        elif header:
            clause = "".join(header.group().split())
            if body or clause not in ("match", "case"):
                stub = _CLAUSE_STUB.get(clause, _ANY_HEADER)
                tries.append((stub if body else stub + _BODY, "stubbed", False))
        elif decorator:
            tries.append(("@" + "".join(decorator.group(1).split()), "stubbed", True))
        if body and (header or (not open_ended and scan.code(last).endswith(":"))):
            tries.append((_ANY_HEADER, "stubbed", False))
        else:
            if body:
                last = self._continuation_end(last, width)
            tries.append((_MASK, "masked", False))

        for text, repair, decorated in tries:
            lines = [_lead(head) + text] + [""] * (last - first)
            top = first
            if not decorated and index is not None:
                # a decorator must stand on a definition: it goes with it
                above = index
                while above > 0 and self.lines[scan.firsts[above - 1]].lstrip(_WS)[:1] == "@":
                    above -= 1
                    top = scan.firsts[above]
                lines = [""] * (first - top) + lines
            fault.repair = repair
            if self._apply(top, lines, fault):
                return True
        return False

    def _deeper(self, last: int, width: int) -> bool:
        """True when the next line of code after `last` is indented past `width`."""
        for line in self.lines[last + 1 :]:
            text = line.strip(_WS)
            if text and not text.startswith("#"):
                return _width(line) > width
        return False

    def _header_end(self, scan: _Scan, last: int, width: int) -> int:
        """Last line of a header whose bracket is never closed.

        A header that ends its line on `(` or `,` goes on below; it ends on
        the first line that ends with a colon, as long as the lines look like
        its continuation. Without one, the header is its own line.
        """
        if not scan.code(last).endswith(("(", "[", "{", ",")):
            return last
        for i in range(last + 1, len(self.lines)):
            text = self.lines[i].strip(_WS)
            if not text or text.startswith("#"):
                continue
            indent = _width(self.lines[i])
            if indent < width or (indent == width and text[0] not in ")]}-"):
                break
            if scan.code(i).endswith(":"):
                return i
        return last

    def _continuation_end(self, last: int, width: int) -> int:
        """Last of the lines under `last` that belong to its statement.

        A statement that is not a header has no body: deeper lines under it
        are the rest of it, and so is a closing bracket at its own indent.
        """
        end = last
        for i in range(last + 1, len(self.lines)):
            text = self.lines[i].strip(_WS)
            if not text or text.startswith("#"):
                continue
            indent = _width(self.lines[i])
            if indent < width or (indent == width and text[0] not in ")]}"):
                break
            end = i
            if indent == width:
                break
        return end

    def _stray_clause(self, scan: _Scan, index: int, fault: _Span) -> bool:
        """Bring an `else` or `except` at the wrong indent back under its statement.

        The parser refuses it with a plain syntax fault, not one of
        indentation: where it stands there is nothing it can continue.
        """
        opens = _FOLLOWS.get(scan.keyword(index))
        if opens is None or index == 0:
            return False
        widths = scan.widths()
        home = -1
        for above in (index - 1, *scan.above(index - 1)):
            if scan.keyword(above) in opens:
                if widths[above] == widths[index]:
                    return False  # it stands where it may: the fault is another
                if home < 0:
                    home = above
        if home < 0:
            return False
        after = index + 1
        if scan.is_header(index) and (after == len(widths) or widths[after] <= widths[home]):
            return False  # nothing would be under it there
        return self._set_lead(scan, [index], _lead(self.lines[scan.firsts[home]]), fault)

    def _bare_try(self, scan: _Scan, index: int, fault: _Span) -> bool:
        """Make a `try` with no `except` or `finally` a plain block.

        The parser names the statement after the block, or the last one of
        the text, and neither has the fault. A `pass` there would change
        nothing and cost a statement.
        """
        widths = scan.widths()
        # the blocks that end before the named statement, and at the end of the text
        ended = [(i, widths[index]) for i in (index - 1, *scan.above(index - 1))] if index else []
        if index == len(widths) - 1:
            ended += [(i, 0) for i in scan.above(index)]
        for i, floor in ended:
            row = scan.firsts[i]
            found = _TRY.match(self.lines[row])
            if found and widths[i] >= floor and not self._handled(scan, i):
                fault.line, fault.column, fault.repair = row, None, "stubbed"
                line = found.group(1) + _ANY_HEADER + self.lines[row][found.end() :]
                return self._apply(row, [line], fault)
        return False

    def _handled(self, scan: _Scan, statement: int) -> bool:
        """True when the `try` at `statement` has an `except` or a `finally`."""
        widths = scan.widths()
        for i in range(statement + 1, len(widths)):
            if widths[i] <= widths[statement]:
                return widths[i] == widths[statement] and scan.keyword(i) in (
                    "except",
                    "except*",
                    "finally",
                )
        return False

    # ---- indentation --------------------------------------------------

    def _reindent(self, scan: _Scan, fault: _Span) -> bool:
        """Repair a fault of indentation; False when the line has none."""
        index = scan.holding(fault.line)
        widths = scan.widths()
        width = widths[index]
        levels = [0]  # the indents open above the statement
        models = [-1]  # a statement at each of them
        outer = [0]  # the indents open above the statement before it
        for i in range(index):
            if i == index - 1:
                outer = list(levels)
            while levels[-1] > widths[i]:
                levels.pop()
                models.pop()
            if levels[-1] < widths[i]:
                levels.append(widths[i])
                models.append(i)
        top = levels[-1]
        under_header = index > 0 and scan.is_header(index - 1)

        if width > top and not under_header:
            return self._two_indents(scan, fault, index, len(levels) - 1, levels, models, outer)
        if width < top and width not in levels:
            # a dedent to no level: it was meant for the nearest one
            near = min(range(len(levels)), key=lambda i: (abs(levels[i] - width), -i))
            return self._two_indents(scan, fault, index, near, levels, models, outer)
        # No odd indent: a header has nothing under it.
        if under_header and width <= top:
            if self._bring_under(scan, fault, index):
                return True
            index -= 1
        elif not scan.is_header(index) or self._deeper(scan.lasts[index], width):
            return False
        first, last = scan.firsts[index], scan.lasts[index]
        lines = self.lines[first:last] + [scan.code(last) + _BODY]
        fault.line, fault.column, fault.repair = first, None, "filled"
        return self._apply(first, lines, fault)

    def _two_indents(
        self,
        scan: _Scan,
        fault: _Span,
        index: int,
        at: int,
        levels: list[int],
        models: list[int],
        outer: list[int],
    ) -> bool:
        """Bring a statement and the level `levels[at]` it was meant for together.

        A block with two indents has one fault, and it may be on either side:
        the statement the parser names, or the ones before it at the level.
        Four moves can settle it. The one that moves the fewest statements is
        taken; of two that move as many, the one that ends on a usual step of
        the text, then the one that moves what the parser named.
        """
        widths = scan.widths()
        width, other = widths[index], levels[at]
        here = _lead(self.lines[scan.firsts[index]])
        there = _lead(self.lines[scan.firsts[models[at]]]) if models[at] >= 0 else ""
        moves: list[tuple[list[int], str]] = []
        # 1. the named statement alone, when the next one can follow it there
        after = index + 1
        if after == len(widths) or _may_follow(
            widths[after], other, scan.is_header(index), levels[: at + 1]
        ):
            moves.append(([index], there))
        # 2. the statement before it alone, when it fits at this indent
        if index > 1 and widths[index - 1] == other:
            if _may_follow(width, widths[index - 2], scan.is_header(index - 2), outer):
                moves.append(([index - 1], here))
        # 3. every statement of the level, when all of them are before it
        low, high = min(width, other), max(width, other)
        start = stop = index
        while start > 0 and widths[start - 1] >= low:
            start -= 1
        while stop < len(widths) and widths[stop] >= low:
            stop += 1
        level = [i for i in range(start, stop) if widths[i] == other]
        if at > 0 and level and level[-1] < index and levels[at - 1] < width:
            if not any(low < widths[i] < high for i in range(start, index)):
                moves.append((level, here))
        # 4. the named statement with the ones that follow it at its indent
        run = []
        for i in range(index, len(widths)):
            if widths[i] < width:
                break
            if widths[i] == width:
                run.append(i)
        moves.append((run, there))

        step = _usual_step(widths)
        statements, lead = min(
            moves, key=lambda move: (len(move[0]), step > 0 and _width(move[1]) % step > 0)
        )
        if index not in statements:
            fault.line, fault.column = scan.firsts[statements[0]], None
        return self._set_lead(scan, statements, lead, fault)

    def _bring_under(self, scan: _Scan, fault: _Span, index: int) -> bool:
        """Indent the statement after an empty header, when it is its body.

        It is when the text goes on at an indent that has no other place:
        deeper than the header, and not under the statement itself.
        """
        widths = scan.widths()
        under, after = widths[index - 1], index + 1
        if after == len(widths):
            return False
        if not scan.is_header(index):
            model = after
        else:
            model = after  # the statement is a header: look past its own block
            while model < len(widths) and widths[model] >= widths[after]:
                model += 1
            if model == len(widths) or widths[after] <= widths[index]:
                return False
        if not under < widths[model] or (model > after and widths[model] >= widths[after]):
            return False
        return self._set_lead(scan, [index], _lead(self.lines[scan.firsts[model]]), fault)

    def _set_lead(self, scan: _Scan, statements: list[int], lead: str, fault: _Span) -> bool:
        first, last = scan.firsts[statements[0]], scan.firsts[statements[-1]]
        lines = self.lines[first : last + 1]
        for statement in statements:
            row = scan.firsts[statement]
            lines[row - first] = lead + self.lines[row].lstrip(_WS)
        fault.repair = "reindented"
        return self._apply(first, lines, fault)

    def _untab(self, scan: _Scan, fault: _Span) -> bool:
        """Tabs and spaces that do not agree: spaces everywhere.

        The tab size is the one under which the fewest statements sit at an
        indent that their block does not have; 8 is the parser's own.
        """
        rows = [row for row in scan.firsts if "\t" in _lead(self.lines[row])]
        if not rows:
            return False
        headers = [scan.is_header(i) for i in range(len(scan.firsts))]

        def odd(tab: int) -> int:
            count, levels, opened = 0, [0], False
            for i, row in enumerate(scan.firsts):
                width = _width(self.lines[row], tab)
                while levels[-1] > width:
                    levels.pop()
                if (width > levels[-1]) != opened:
                    count += 1
                if width > levels[-1]:
                    levels.append(width)
                opened = headers[i]
            return count

        tab = min((8, 4, 2), key=odd)
        first, last = rows[0], rows[-1]
        lines = self.lines[first : last + 1]
        for row in rows:
            line = self.lines[row]
            lines[row - first] = " " * _width(line, tab) + line.lstrip(_WS)
        fault.repair = "reindented"
        return self._apply(first, lines, fault)

    # ---- bookkeeping --------------------------------------------------

    def _apply(self, first: int, lines: list[str], fault: _Span, into: _Span | None = None) -> bool:
        """Put `lines` at `first`; False when that changes nothing.

        The lines join the span `into`, or the spans they touch, or become
        the span of `fault`. One line is in one span, and the fault that was
        found first keeps its line and message.
        """
        last = first + len(lines) - 1
        if self.lines[first : last + 1] == lines:
            return False
        self.lines[first : last + 1] = lines
        merged = into if into is not None else fault
        if into is None:
            fault.first, fault.last = first, last
            if not first <= fault.line <= last:
                fault.line, fault.column = first, None  # the cause was not on the named line
        else:
            into.first, into.last = min(into.first, first), max(into.last, last)
        for span in [s for s in self.spans if s is not merged]:
            if span.first <= merged.last and merged.first <= span.last:
                self.spans.remove(span)
                if _RANK[span.repair] > _RANK[merged.repair]:
                    merged.repair = span.repair
                if into is None:
                    merged.line, merged.column, merged.message = (
                        span.line,
                        span.column,
                        span.message,
                    )
                merged.first = min(merged.first, span.first)
                merged.last = max(merged.last, span.last)
                if merged.text_from < 0 and merged.text_to < 0:
                    merged.text_from, merged.text_to = span.text_from, span.text_to
        if merged not in self.spans:
            self.spans.append(merged)
        return True
