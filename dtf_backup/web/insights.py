"""Donations of an archive: what DTF readers donated to the owner's posts (with or without a comment), who donated,
what the owner's own comments got and what the owner sent. Read from view.sqlite (viewdb: posts.donations,
comments.donation / donated); amounts are rubles as DTF reports them."""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

from ..normalize import comment_text
from ..state import unpack
from ..util import count_label, num, plural, rub, short, ts_date, ts_human
from .icons import icon
from .ui import badge, empty_state, fold, page_head, sec_head, stat

if TYPE_CHECKING:
    from .viewer import ArchiveView

E = html.escape
DONATIONS = ("донат", "доната", "донатов")
TOP_DONORS = 12


def donations_total(v: "ArchiveView") -> int:
    """What the owner's posts got: DTF's own sum per post, or the donation comments under a post DTF shows 0 for
    (older posts). The page and the home stats use the same number."""
    db = v.db()
    try:
        rows = db.execute("SELECT p.donations, COALESCE((SELECT SUM(c.donation) FROM comments c WHERE c.entry_id=p.id "
                          "AND c.own_post=1 AND c.donation>0), 0) FROM posts p").fetchall()
    finally:
        db.close()
    return sum(max(a or 0, b or 0) for a, b in rows)


def page_donations(v: "ArchiveView") -> tuple[str, str]:
    db = v.db()
    posts = db.execute("SELECT id, title, date, donations, stats_at FROM posts").fetchall()
    by_post: dict[int, tuple[int, int]] = {r[0]: (r[1], r[2]) for r in db.execute(
        "SELECT entry_id, SUM(donation), COUNT(*) FROM comments WHERE own_post=1 AND donation>0 GROUP BY entry_id")}
    donors = db.execute("SELECT author, SUM(donation) AS s, COUNT(*) AS n, MAX(date) AS last FROM comments "
                        "WHERE own_post=1 AND donation>0 GROUP BY author ORDER BY s DESC").fetchall()
    feed = db.execute("SELECT id, entry_id, author, date, donation, data FROM comments WHERE own_post=1 AND donation>0 "
                      "ORDER BY date DESC").fetchall()
    got = db.execute("SELECT id, entry_id, date, donated, data FROM comments WHERE mine=1 AND donated>0 "
                     "ORDER BY date DESC").fetchall()
    sent = db.execute("SELECT id, entry_id, date, donation, data FROM comments WHERE mine=1 AND donation>0 "
                      "ORDER BY date DESC").fetchall()
    titles = {r[0]: r[1] or "" for r in db.execute("SELECT id, title FROM entries")}
    db.close()
    stats_at = max((r["stats_at"] or 0 for r in posts), default=0)

    rows = []
    total = with_comment = 0
    for p in posts:
        dtf = p["donations"] or 0
        csum, cn = by_post.get(p["id"], (0, 0))
        amount = max(dtf, csum)
        if not amount:
            continue
        total += amount
        with_comment += csum
        rows.append((amount, p, csum, cn, dtf))
    rows.sort(key=lambda t: (-t[0], -t[1]["date"]))
    sub = ("Суммы постов — данные DTF" + (f" на {ts_human(stats_at)}" if stats_at else "") +
           "; донаты с комментариями — на время, когда LDTF скачал ветки обсуждений.")
    head = page_head("Донаты", sub)
    if not rows and not got and not sent:
        return "Донаты", head + empty_state("volunteer_activism", "Донатов нет",
                                            "Ни постам, ни комментариям этого архива пока никто не донатил.")
    tiles = [stat(rub(total), "получили посты")]
    if with_comment:
        tiles.append(stat(rub(with_comment), f"с комментарием · {count_label(len(feed), *DONATIONS)}"))
    if donors:
        tiles.append(stat(num(len(donors)), plural(len(donors), "донатер", "донатера", "донатеров")))
    if got:
        tiles.append(stat(rub(sum(r["donated"] for r in got)), "получили комментарии автора"))
    if sent:
        tiles.append(stat(rub(sum(r["donation"] for r in sent)), "отправил автор"))
    body = [head, f'<section class="card don-sum"><div class="stats">{"".join(tiles)}</div></section>']

    if rows:
        items = []
        for amount, p, csum, cn, dtf in rows:
            parts = [ts_date(p["date"])]
            if cn:
                parts.append(f"с комментарием {rub(csum)} · {count_label(cn, *DONATIONS)}")
            if dtf > csum and csum:
                parts.append(f"без комментария или удалены {rub(dtf - csum)}")
            elif dtf > csum:
                parts.append("без комментариев")
            if not dtf and csum:
                parts.append("сумма по комментариям: DTF не показывает её у старых постов")
            items.append(f'<div class="li don-li"><span class="don-amt">{rub(amount)}</span><div class="li-body">'
                         f'<a class="li-link li-t" href="{E(v.links.post(p["id"]))}">{E(p["title"] or "Без заголовка")}</a>'
                         f'<span class="li-s">{" · ".join(E(x) for x in parts)}</span></div></div>')
        body.append(sec_head("Посты", n=len(rows)) + f'<div class="card list">{"".join(items)}</div>')

    if donors:
        def donor(r: dict) -> str:
            last = f" · последний {ts_date(r['last'])}" if r["n"] > 1 else f" · {ts_date(r['last'])}"
            return (f'<div class="li don-li">{v.cv.avatar_html(r["author"])}<div class="li-body">'
                    f'<span class="li-t">{E(v.cv.author_name(r["author"]))}</span>'
                    f'<span class="li-s">{count_label(r["n"], *DONATIONS)}{last}</span></div>'
                    f'<span class="don-amt">{rub(r["s"])}</span></div>')
        top = "".join(donor(r) for r in donors[:TOP_DONORS])
        rest = "".join(donor(r) for r in donors[TOP_DONORS:])
        more = fold(f"Ещё {len(donors) - TOP_DONORS}", f'<div class="card list">{rest}</div>', "fold don-more") if rest else ""
        body.append(sec_head("Кто донатил", n=len(donors)) + f'<div class="card list">{top}</div>{more}')

    def comment_row(r: dict, amount_badge: str, mine: bool = False) -> str:
        c = unpack(r["data"])
        text = comment_text(c.get("text")).text.strip()
        who = "" if mine else f'<span class="ca">{v.cv.avatar_html(r["author"])}{E(v.cv.author_name(r["author"]))}</span>'
        post = titles.get(r["entry_id"]) or "Пост"
        return (f'<a class="don-c" href="{E(v.links.go(r["id"]))}"><div class="c-h">{who}{amount_badge}'
                f'<span class="cd">{ts_human(r["date"])}</span></div>'
                f'<div class="don-t{"" if text else " muted"}">{E(short(text, 280)) if text else "Без текста — только донат"}</div>'
                f'<div class="don-p">{icon("article")}<span>{E(post)}</span></div></a>')

    if feed:
        body.append(sec_head("Донаты с комментариями", n=len(feed)) + '<div class="card don-feed">' +
                    "".join(comment_row(r, badge(f"Донат {rub(r['donation'])}", "volunteer_activism", "don")) for r in feed)
                    + "</div>")
    if got:
        body.append(sec_head("Донаты комментариям автора", n=len(got)) + '<div class="card don-feed">' +
                    "".join(comment_row(r, badge(f"+{rub(r['donated'])}", "volunteer_activism", "don"), mine=True)
                            for r in got) + "</div>")
    if sent:
        body.append(sec_head("Донаты, которые отправил автор", n=len(sent)) + '<div class="card don-feed">' +
                    "".join(comment_row(r, badge(f"Донат {rub(r['donation'])}", "volunteer_activism", "don"), mine=True)
                            for r in sent) + "</div>")
    return "Донаты", "".join(body)
