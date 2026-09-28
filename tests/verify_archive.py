"""Consistency checks on a real archive (no network): python tests/verify_archive.py archive/petra"""

import collections
import json
import sqlite3
import sys
from pathlib import Path


def jsonl(p: Path):
    with open(p, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def main(root: Path) -> int:
    bad = 0
    library = root.resolve().parent
    posts = list(jsonl(root / "data" / "posts.jsonl"))
    raw_posts = list((root / "raw" / "posts").glob("*.json.gz"))
    print(f"posts.jsonl: {len(posts)}, raw posts: {len(raw_posts)}")
    bad += len(posts) != len(raw_posts)

    ctx = {c["id"]: c for c in jsonl(root / "data" / "context.jsonl")}
    pc = {c["id"]: c for c in jsonl(root / "data" / "post-comments.jsonl")}
    mine = {c["id"]: c for c in jsonl(root / "data" / "comments.jsonl")}
    known = {**ctx, **pc, **mine}
    status = collections.Counter(c["contextStatus"] for c in mine.values())
    print(f"comments of the user: {len(mine)}; context status: {dict(status)}")

    chain_bad = text_missing = 0
    for c in mine.values():
        if c["contextStatus"] != "ok":
            continue
        anc = c["ancestorIds"]
        if c["level"] and (len(anc) != c["level"] or (anc and known.get(anc[0], {}).get("level", 0) not in (0, None))):
            chain_bad += 1
        for i in anc + c["replyIds"]:
            if i not in known:
                text_missing += 1
    print(f"chains with wrong length/root: {chain_bad}; ancestor/reply ids without text: {text_missing}")
    bad += text_missing > 0

    media = list(jsonl(root / "data" / "media.jsonl"))
    missing_files = [m for m in media if m.get("path") and not (library / m["path"]).exists()]
    print(f"media of the archive: {len(media)} keys, with files: {sum(1 for m in media if m.get('path'))}, "
          f"files missing on disk: {len(missing_files)}")
    bad += len(missing_files) > 0

    view = root / ".state" / "view.sqlite"
    if view.exists():
        db = sqlite3.connect(f"file:{view.as_posix()}?mode=ro", uri=True)
        n_feed = db.execute("SELECT COUNT(*) FROM comments WHERE feed=1").fetchone()[0]
        n_loc = db.execute("SELECT COUNT(*) FROM comment_loc").fetchone()[0]
        n_posts = db.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        print(f"view.sqlite: posts {n_posts}, user comments {n_feed}, placed on month pages {n_loc}")
        bad += n_feed != len(mine) or n_loc != len(mine) or n_posts != len(posts)
        n_all = db.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
        try:
            docs = dict(db.execute("SELECT kind, COUNT(*) FROM search_docs GROUP BY kind").fetchall())
            n_vocab = db.execute("SELECT COUNT(*) FROM vocab").fetchone()[0]
        except sqlite3.OperationalError:
            docs, n_vocab = {}, 0
        print(f"search index: docs {docs}, vocabulary {n_vocab}")
        bad += (docs.get("p", 0) != len(posts) or docs.get("c", 0) != len(mine)
                or docs.get("c", 0) + docs.get("o", 0) != n_all or n_vocab == 0)
        db.close()
    else:
        print("view.sqlite: missing")
        bad += 1
    if (root / "site").exists():
        print("legacy site/ still present")
        bad += 1

    rep = json.loads((root / ".state" / "render-report.json").read_text(encoding="utf-8"))
    for k in ("unsupported", "errors", "unknownReactions"):
        print(f"report {k}: { {n: v['count'] for n, v in rep.get(k, {}).items()} }")
    print("OK" if not bad else f"PROBLEMS: {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1] if len(sys.argv) > 1 else "archive/petra")))
