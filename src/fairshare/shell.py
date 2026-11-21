"""fairshare's block of aliases in a user's shell file (decision 31): pure text in, text out.
The block is fairshare's only edit to a shell file. It sits between two marker lines, at the
end of the file so that it overrides the user's own alias lines above it, which it never
changes. Removing the block gives back the file as it was."""
from __future__ import annotations

import re
import shlex

START = "# >>> fairshare aliases >>>"
END = "# <<< fairshare aliases <<<"
COMMAND = "fairshare"
_START = re.compile(r"^" + re.escape(START) + r"[ \t]*$", re.M)
_END = re.compile(r"^" + re.escape(END) + r"[ \t]*(\n|$)", re.M)
_NAME = re.compile(r"^\s*alias\s+([\w.-]+)=", re.M)


def alias_line(name: str, words: list[str] | tuple[str, ...]) -> str:
    """`alias NAME='WORDS...'`, quoted as a shell reads it back."""
    return f"alias {name}={shlex.quote(shlex.join(words))}"


def run_words(account_id: str, prefix=(), args=()) -> list[str]:
    """The words an alias runs to start a session on `account_id` through fairshare."""
    return [*prefix, COMMAND, "run", "-a", account_id, *args]


def span(text: str) -> tuple[int, int] | None:
    """Where the block is: from the start of its first marker line to the end of its last (its
    newline included); None when there is none. Raises ValueError for a start marker with no end
    marker after it, or more than one block: a text fairshare cannot change safely."""
    starts = list(_START.finditer(text))
    if not starts:
        if _END.search(text):
            raise ValueError("fairshare's end marker without its start marker")
        return None
    if len(starts) > 1:
        raise ValueError("more than one fairshare block")
    end = _END.search(text, starts[0].end())
    if end is None:
        raise ValueError("fairshare's start marker without its end marker")
    return starts[0].start(), end.end()


def block_lines(text: str) -> list[str] | None:
    """The lines inside the block, None when there is none."""
    if (s := span(text)) is None:
        return None
    return text[s[0]:s[1]].removesuffix("\n").split("\n")[1:-1]


def entry(line: str) -> tuple[str | None, str | None]:
    """A block line's alias name and the account it starts (as `alias_line(name,
    run_words(account))` writes it); None for what the line does not say."""
    name = m.group(1) if (m := _NAME.match(line)) else None
    try:
        value = shlex.split(line[m.end():], comments=True) if m else []
        words = shlex.split(value[0]) if len(value) == 1 else []
    except ValueError:
        words = []
    run = run_words("")[:-1]
    return name, (words[len(run)] if len(words) == len(run) + 1 and words[:-1] == run else None)


def names(lines: list[str]) -> set[str]:
    """The alias names those lines define."""
    return {m.group(1) for line in lines if (m := _NAME.match(line))}


def defined(text: str) -> set[str]:
    """Every alias name a shell file's lines define."""
    return set(_NAME.findall(text))


def start_line(text: str) -> int | None:
    """The 1-based line of the block's start marker, None when there is none."""
    s = span(text)
    return None if s is None else text.count("\n", 0, s[0]) + 1


def with_block(text: str, lines: list[str]) -> str:
    """`text` whose block holds `lines`: only what is between the markers changes when there is
    a block; else one newline and the block are added at the end. No lines: no block."""
    if not lines:
        return without_block(text)
    body = "".join(line + "\n" for line in lines)
    if (s := span(text)) is None:
        return f"{text}\n{START}\n{body}{END}\n"
    inner = text.index("\n", s[0]) + 1
    end = _END.search(text, inner).start()
    return text[:inner] + body + text[end:]


def without_block(text: str) -> str:
    """`text` without the block and the one newline `with_block` put before it."""
    if (s := span(text)) is None:
        return text
    a, b = s
    if a and text[a - 1] == "\n":
        a -= 1
    return text[:a] + text[b:]
