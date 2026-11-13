#!/usr/bin/env python3
"""Print a Kibana saved objects NDJSON export in a form that diffs well.

Kibana exports each object on one line, with the dashboard's panels and controls as JSON strings inside it,
so `git diff` shows one changed line for any change to a dashboard. This prints every object as indented
JSON with sorted keys, expands the embedded JSON strings, and splits ES|QL queries into one array entry per
pipe command. Nothing is dropped, so any change to the file shows up in the output.

Use it on its own:

    python tools/ndjson_diff.py postfix_kibana_dashboard.ndjson

or as a git textconv driver, so `git diff`, `git log -p` and `git show` print this form for the NDJSON files
(.gitattributes in the repo assigns them the kibana-ndjson diff driver):

    git config diff.kibana-ndjson.textconv "python tools/ndjson_diff.py"
"""
import json
import sys

QUERY_KEYS = {"esql", "esqlQuery"}


def split_esql(query):
    """Split on | outside quoted strings."""
    parts, cur, quote, i = [], [], None, 0
    while i < len(query):
        c = query[i]
        if quote:
            cur.append(c)
            if c == "\\" and i + 1 < len(query):
                cur.append(query[i + 1])
                i += 1
            elif c == quote:
                quote = None
        elif c in "\"'":
            quote = c
            cur.append(c)
        elif c == "|":
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(c)
        i += 1
    parts.append("".join(cur).strip())
    return [p if n == 0 else "| " + p for n, p in enumerate(parts)]


def expand(value, key=None):
    if isinstance(value, dict):
        return {k: expand(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [expand(v) for v in value]
    if isinstance(value, str):
        if key in QUERY_KEYS and "|" in value:
            return split_esql(value)
        s = value.strip()
        if s[:1] in "{[" and s[-1:] in "}]":
            try:
                return {"(embedded JSON)": expand(json.loads(s), key)}
            except ValueError:
                pass
    return value


def render(path, out):
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                out.write("# line %d is not JSON\n%s" % (n, line))
                continue
            title = obj.get("attributes", {}).get("title", "")
            out.write("#### %s %s %s\n" % (obj.get("type", "?"), obj.get("id", "?"), title))
            out.write(json.dumps(expand(obj), indent=2, sort_keys=True, ensure_ascii=False))
            out.write("\n\n")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    out = sys.stdout
    if hasattr(out, "reconfigure"):
        out.reconfigure(encoding="utf-8", newline="\n")
    for p in sys.argv[1:]:
        render(p, out)
