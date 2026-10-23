"""MARZ file library: saved documents to open, search and compare."""
import difflib
import re
import time

from tools import reply, tool

MAX_SAVED = 100


def save_items(ctx, items):
    for item in items:
        ctx.store.execute(
            "INSERT OR REPLACE INTO files(user_id, name, kind, chars, text, "
            "created) VALUES(?, ?, ?, ?, ?, ?)",
            (ctx.uid, item["name"], item["kind"], len(item["text"]),
             item["text"], time.time()),
        )
    if items:
        ctx.store.execute(
            "DELETE FROM files WHERE user_id=? AND id NOT IN (SELECT id "
            "FROM files WHERE user_id=? ORDER BY id DESC LIMIT ?)",
            (ctx.uid, ctx.uid, MAX_SAVED),
        )


def listing(ctx):
    return ctx.store.query(
        "SELECT id, name, kind, chars, created FROM files WHERE user_id=? "
        "ORDER BY id DESC",
        (ctx.uid,),
    )


def names(ctx, limit=15):
    return [row["name"] for row in listing(ctx)[:limit]]


def find_file(ctx, ref):
    """Return matching files (with text) by number or by name."""
    rows = ctx.store.query(
        "SELECT id, name, kind, text FROM files WHERE user_id=? "
        "ORDER BY id DESC",
        (ctx.uid,),
    )
    ref = str(ref).strip().lower()
    if ref.isdigit():
        return [r for r in rows if r["id"] == int(ref)]
    if not ref:
        return []
    words = ref.split()
    return (
        [r for r in rows if r["name"].lower() == ref]
        or [r for r in rows if ref in r["name"].lower()]
        or [r for r in rows if all(w in r["name"].lower() for w in words)]
    )


def delete(ctx, fid):
    ctx.store.execute(
        "DELETE FROM files WHERE id=? AND user_id=?", (fid, ctx.uid)
    )


@tool("listFiles", "listFiles()", "List the saved documents in the library.")
def list_files(ctx):
    rows = listing(ctx)
    if not rows:
        return reply("The library is empty. Files the user attaches in "
                     "chat are saved here.")
    return reply("Saved files: " + ", ".join(
        f"{r['name']} ({r['chars']} chars)" for r in rows[:30]) + ".")


@tool("openFile", "openFile(name)",
      "Open a saved document by name and read its text. Use it to read, "
      "summarise or extract text from a file.")
def open_file(ctx, name=""):
    rows = find_file(ctx, name)
    if not rows:
        return reply("No saved file matches " + str(name) + ".", ok=False)
    if len(rows) > 1:
        return reply("Several files match: " + ", ".join(
            r["name"] for r in rows[:6]) + ". Ask which one.", ok=False)
    row = rows[0]
    text = row["text"]
    more = " (shortened)" if len(text) > 6000 else ""
    return reply(f"File {row['name']}{more}:\n{text[:6000]}",
                 data={"id": row["id"], "name": row["name"]})


@tool("searchFiles", "searchFiles(query)",
      "Find where a word or phrase appears across the saved documents.")
def search_files(ctx, query=""):
    term = str(query).strip()
    if not term:
        return reply("What should I search for?", ok=False)
    pattern, hits = re.compile(re.escape(term), re.I), []
    for row in ctx.store.query(
            "SELECT name, text FROM files WHERE user_id=? ORDER BY id DESC",
            (ctx.uid,)):
        found = list(pattern.finditer(row["text"]))
        if found:
            text = row["text"]
            snippets = [
                text[max(0, m.start() - 50):m.end() + 50].replace("\n", " ")
                for m in found[:2]
            ]
            hits.append((len(found), row["name"], snippets))
    if not hits:
        return reply("No saved file mentions " + term + ".")
    hits.sort(reverse=True)
    lines = [f"{name}: {count} match(es), e.g. ..." + " ... ".join(snips)
             for count, name, snips in hits[:8]]
    return reply("\n".join(lines))


@tool("compareFiles", "compareFiles(first, second)",
      "Compare two saved documents and list the differences.")
def compare_files(ctx, first="", second=""):
    one, two = find_file(ctx, first), find_file(ctx, second)
    if not one or not two:
        return reply("Could not find both files in the library.", ok=False)
    a = one[0]["text"].splitlines()[:2000]
    b = two[0]["text"].splitlines()[:2000]
    ratio = difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()
    diff = list(difflib.unified_diff(
        a, b, one[0]["name"], two[0]["name"], n=0, lineterm=""))
    if not diff:
        return reply("The two files have identical text.")
    shown = "\n".join(diff[:40])[:3000]
    return reply(f"Similarity {ratio:.0%}. Differences:\n{shown}")
