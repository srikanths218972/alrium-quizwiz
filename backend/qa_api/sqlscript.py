"""Runs psql-style SQL scripts without a psql client.

Supports exactly what the repository's scripts use: one statement at a time in autocommit
(as psql does), \\gset into variables, :var and :'var' substitution. Other lines starting with
a backslash (psql meta-commands such as \\if) are skipped, so callers pass every variable.
Only for trusted files from this repository.
"""

import re
from typing import Any

import psycopg


def substitute(text: str, variables: dict[str, str]) -> str:
    def literal(m: re.Match[str]) -> str:
        return "'" + variables[m.group(1)].replace("'", "''") + "'" if m.group(1) in variables else m.group(0)

    def raw(m: re.Match[str]) -> str:
        return variables[m.group(1)] if m.group(1) in variables else m.group(0)

    text = re.sub(r"(?<!:):'(\w+)'", literal, text)
    return re.sub(r"(?<![:\w]):(\w+)\b", raw, text)


def split_statements(text: str) -> list[str]:
    """Splits SQL on top-level semicolons, respecting quotes, dollar quotes and comments (as psql does)."""
    out: list[str] = []
    cur: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if text.startswith("--", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            cur.append(text[i:j])
            i = j
        elif ch == "'":
            j = i + 1
            while j < n:
                if text[j] == "'" and text[j + 1 : j + 2] == "'":
                    j += 2
                elif text[j] == "'":
                    break
                else:
                    j += 1
            cur.append(text[i : j + 1])
            i = j + 1
        elif ch == "$" and (m := re.match(r"\$[A-Za-z_]*\$", text[i:])):
            tag = m.group(0)
            j = text.find(tag, i + len(tag))
            j = n if j < 0 else j + len(tag)
            cur.append(text[i:j])
            i = j
        elif ch == ";":
            out.append("".join(cur))
            cur = []
            i += 1
        else:
            cur.append(ch)
            i += 1
    out.append("".join(cur))
    return [st for st in out if re.sub(r"--[^\n]*", "", st).strip()]


def run_script(conn: psycopg.Connection[Any], text: str, variables: dict[str, str] | None = None) -> list[tuple[Any, ...]]:
    """Runs a psql-style script. Lines starting with a backslash are psql meta-commands and skipped."""
    variables = dict(variables or {})
    buf: list[str] = []
    last: list[tuple[Any, ...]] = []

    def flush(gset: bool) -> None:
        nonlocal last
        sql_text = substitute("\n".join(buf), variables)
        buf.clear()
        if not sql_text.strip():
            return
        rows: list[tuple[Any, ...]] = []
        names: list[str] = []
        for statement in split_statements(sql_text):
            cur = conn.execute(statement)
            if cur.description:
                rows, names = cur.fetchall(), [d.name for d in cur.description]
        last = rows
        if gset:
            for name, value in zip(names, rows[0], strict=True):
                variables[name] = str(value)

    for line in text.splitlines():
        if line.rstrip().endswith("\\gset"):
            buf.append(line.rstrip()[: -len("\\gset")])
            flush(True)
        elif line.lstrip().startswith("\\"):
            continue
        else:
            buf.append(line)
    flush(False)
    return last
