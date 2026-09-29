"""Archive guard: never let a sync overwrite archived posts and comments with DTF's placeholders.

What DTF returns when materials go away (checked 2026-09-28, details in docs/DTF_API.md):
- deleted account: `subsite` still answers 200 with name "Аккаунт удален", `isRemovedByUserRequest`, a placeholder
  avatar, nickname null; frozen account: "Аккаунт заморожен", `isFrozen`. A missing account answers 404.
- their posts stay in the timeline as "Статья удалена" with one text block "Этот материал был удалён по просьбе
  автора." (post flag `isRemovedByUserRequest`, comments counter 0); their comments come as "Комментарий недоступен".
- a live author may "wipe" a post by editing it into "статья удалена" or a comment into ".".
- comments: "Комментарий удалён модератором" (isRemoved + isRemovedByModerator), "Комментарий удалён автором поста"
  (isRemoved), "Комментарий недоступен" (hidden or deleted by its author, no flags).

A sync keeps the archived version of anything that turned into a placeholder or vanished and marks it with
`_site: {state, at}` (state: removed | wiped | gone | unavailable | moderator | author-deleted | ...). Account-level
problems and mass losses raise `GuardTrip`: the sync stops before writing anything and waits for the user.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any

from .normalize import html_to_text
from .util import COMMENTS, POSTS, count_label, now_ts

COMMENTS_LIMIT = 20            # own comments lost in one sync before it stops and asks
ACCEPTABLE = ("posts-mass", "comments-mass")   # kinds the user may confirm; account problems can only be rechecked

ACCOUNT_STUBS = {"аккаунт удален": "deleted", "аккаунт заморожен": "frozen"}
POST_STUB_TITLES = {"статья удалена", "пост удален", "материал удален", "запись удалена"}
POST_STUB_TEXT = ("этот материал был удален", "статья удалена", "материал удален")
COMMENT_STUBS = {"комментарий недоступен": "unavailable", "комментарий удален модератором": "moderator",
                 "комментарий удален автором поста": "removed", "комментарий удален": "removed"}

STATE_TITLES = {
    "removed": "удалён на DTF", "wiped": "стёрт на DTF", "gone": "пропал с DTF", "unavailable": "недоступен на DTF",
    "moderator": "удалён модератором", "author-deleted": "автор удалил аккаунт", "author-frozen": "аккаунт автора заморожен",
    "hidden": "скрыт на DTF",
}

KIND_TITLES = {
    "account-deleted": "аккаунт удалён на DTF",
    "account-frozen": "аккаунт заморожен на DTF",
    "account-missing": "аккаунт не найден на DTF",
    "posts-mass": "на DTF пропало много постов",
    "comments-mass": "на DTF пропало много комментариев",
}
HINTS = {   # what the user can do, in the app (the sync tab's guard card)
    "account-deleted": "Если аккаунт вернут, нажмите «Проверить снова». До тех пор архив остаётся как есть, а автосинхронизация "
                       "для него не запускается.",
    "account-frozen": "Заморозку могут снять — тогда нажмите «Проверить снова». До тех пор архив остаётся как есть, а "
                      "автосинхронизация для него не запускается.",
    "account-missing": "Возможно, аккаунт удалён или DTF временно отвечает с ошибками. Попробуйте «Проверить снова» позже.",
    "posts-mass": "Если посты удалили вы сами или их убрала модерация, нажмите «Продолжить и сохранить удалённое»: они "
                  "будут отмечены как удалённые на DTF, а в архиве останутся сохранённые версии. Если это сбой DTF, "
                  "проверьте снова позже.",
    "comments-mass": "Если комментарии удалили вы сами или модерация, нажмите «Продолжить и сохранить удалённое»: они "
                     "будут отмечены как удалённые на DTF, а в архиве останутся сохранённые тексты. Если это сбой DTF, "
                     "проверьте снова позже.",
}
CLI_HINT = ("Защита архива остановила синхронизацию, архив не изменён. Посмотрите подробности в LDTF (вкладка "
            "«Синхронизация») или в `status`. Если материалы удалили вы сами или модерация, запустите "
            "sync --accept-deletions: пропавшее будет отмечено, в архиве останутся сохранённые версии.")
STOPPED = "синхронизация остановлена защитой архива"


def title(kind: str | None) -> str:
    """What stopped the sync, for headings: "аккаунт удалён на DTF"."""
    return KIND_TITLES.get(kind or "", "нужна проверка")


def hint(kind: str | None) -> str:
    return HINTS.get(kind or "", "")


class GuardTrip(Exception):
    """Stops a sync before it writes anything that would replace archived materials with placeholders."""

    def __init__(self, kind: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.details = details or {}

    def as_meta(self) -> dict:
        return {"kind": self.kind, "message": self.message, "details": self.details, "ts": now_ts()}


def norm(s: Any) -> str:
    t = " ".join(str(s or "").replace("ё", "е").replace("Ё", "Е").lower().split())
    return t.rstrip(".!… ")


def state_title(state: str | None) -> str:
    return STATE_TITLES.get(state or "", state or "")


def posts_limit(archived: int) -> int:
    """Posts that may disappear in one sync before it stops: 3 for small archives, up to 10 for big ones."""
    return max(3, min(math.ceil(archived / 10), 10))


# ---------------------------------------------------------------------- accounts
def account_problem(prof: Any) -> str | None:
    """'deleted' | 'frozen' | None for a `subsite` answer."""
    if not isinstance(prof, dict):
        return None
    if prof.get("isRemovedByUserRequest"):
        return "deleted"
    if prof.get("isFrozen"):
        return "frozen"
    if not prof.get("nickname") and not prof.get("uri"):   # the placeholder name alone could be a joke nickname
        return ACCOUNT_STUBS.get(norm(prof.get("name")))
    return None


# ---------------------------------------------------------------------- posts
def post_text(p: dict) -> str:
    out: list[str] = []
    for b in p.get("blocks") or []:
        d = b.get("data") if isinstance(b, dict) and isinstance(b.get("data"), dict) else {}
        for k in ("text", "title", "description"):
            if isinstance(d.get(k), str):
                out.append(html_to_text(d[k]))
        if isinstance(d.get("items"), list):
            out.extend(html_to_text(x) for x in d["items"] if isinstance(x, str))
    return "\n".join(t for t in out if t).strip()


def _media_count(p: dict) -> int:
    n = 0
    for b in p.get("blocks") or []:
        if isinstance(b, dict) and b.get("type") in ("media", "video", "audio"):
            items = (b.get("data") or {}).get("items")
            n += len(items) if isinstance(items, list) else 1
    return n


def post_stub(p: dict) -> str | None:
    """'removed' when DTF (or the author) replaced the post with a placeholder."""
    if not isinstance(p, dict):
        return None
    if p.get("isRemovedByUserRequest"):
        return "removed"
    if len(p.get("blocks") or []) <= 2:
        if norm(p.get("title")) in POST_STUB_TITLES or norm(post_text(p)).startswith(POST_STUB_TEXT):
            return "removed"
    return None


def post_wiped(old: dict | None, new: dict) -> bool:
    """The author edited a real post into (almost) nothing."""
    if not old or post_stub(old):
        return False
    w_old = len(post_text(old)) + 150 * _media_count(old)
    w_new = len(post_text(new)) + 150 * _media_count(new)
    if w_old < 300 or w_new >= 0.2 * w_old:
        return False
    return (len(new.get("blocks") or []) <= 3 or "удал" in norm(new.get("title"))
            or "удал" in norm(post_text(new))[:200])


def post_loss(old: dict | None, new: dict) -> str | None:
    """Why the fetched version must not replace the archived one (None: it may)."""
    if old is None or post_stub(old):
        return None
    if post_stub(new):
        return "removed"
    if post_wiped(old, new):
        return "wiped"
    return None


# ---------------------------------------------------------------------- comments
def comment_stub(c: dict) -> str | None:
    """Placeholder reason for a comment whose text DTF no longer shows, else None."""
    if not isinstance(c, dict):
        return None
    raw = str(c.get("text") or "").strip()
    t = norm(raw)
    if t not in COMMENT_STUBS and (raw or c.get("media")):   # "." is a wipe (see degraded), not a placeholder
        return None
    a = c.get("author") or {}
    if c.get("isRemovedByModerator"):
        return "moderator"
    if c.get("isHiddenByBan"):
        return "hidden"
    if a.get("isRemovedByUserRequest"):
        return "author-deleted"
    if a.get("isFrozen"):
        return "author-frozen"
    if c.get("isRemoved"):
        return "removed"
    return COMMENT_STUBS.get(t, "removed")


def degraded(old: dict | None, new: dict) -> str | None:
    """Why `new` must not replace the archived `old` comment (None: it may)."""
    if not old or comment_stub(old):
        return None
    why = comment_stub(new)
    if why:
        return why
    ot, nt = len((old.get("text") or "").strip()), len((new.get("text") or "").strip())
    if ot >= 40 and nt <= 3 and not new.get("media"):
        return "wiped"
    return None


def keep(old: dict, state: str, ts: int | None = None) -> dict:
    """The archived version, marked with what happened to it on the site (first sighting wins)."""
    if old.get("_site"):
        return old
    return {**old, "_site": {"state": state, "at": ts or now_ts()}}


def merge_items(old_items: list[dict] | None, new_items: list[dict], ts: int | None = None,
                uid: int | None = None) -> tuple[list[dict], Counter]:
    """Fresh comment tree/branch merged with the archived one: comments that turned into placeholders keep their
    archived text, comments that vanished are put back. Stats count only new losses (kept, gone, mine)."""
    ts = ts or now_ts()
    old = {c["id"]: c for c in old_items or [] if isinstance(c, dict) and "id" in c}
    st: Counter = Counter()
    out: list[dict] = []
    seen: set = set()
    for c in new_items:
        seen.add(c.get("id"))
        o = old.get(c.get("id"))
        why = degraded(o, c) if o else None
        if why and o is not None:
            if not o.get("_site"):
                st["kept"] += 1
                if uid is not None and (o.get("author") or {}).get("id") == uid:
                    st["mine"] += 1
            out.append(keep(o, why, ts))
        else:
            out.append(c)
    appended = False
    for cid, o in old.items():
        if cid in seen or comment_stub(o):
            continue
        if not o.get("_site"):
            st["gone"] += 1
            if uid is not None and (o.get("author") or {}).get("id") == uid:
                st["mine"] += 1
        out.append(keep(o, "gone", ts))
        appended = True
    if appended:
        out.sort(key=lambda c: (c.get("date") or 0, c.get("id") or 0))
    return out, st


def site_summary(stats: dict | None) -> str:
    """'1 пост удалён, 3 комментария' — what disappeared from DTF in a sync (kept in the archive)."""
    if not stats:
        return ""
    parts = []
    np_ = sum(v for k, v in stats.items() if k.startswith("posts_"))
    nc = sum(v for k, v in stats.items() if k.startswith("comments_"))
    nctx = stats.get("context", 0)
    if np_:
        parts.append(count_label(np_, *POSTS))
    if nc:
        parts.append(count_label(nc, *COMMENTS))
    if nctx:
        parts.append(count_label(nctx, "чужой комментарий", "чужих комментария", "чужих комментариев") + " в ветках")
    return ", ".join(parts)
