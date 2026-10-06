"""Samples of every DTF editor block for the "Блоки DTF" page: how LDTF shows each type (blocks.TYPE_TITLES).

The shapes follow what DTF sends (raw/posts/*.json.gz); the pictures are small drawings shipped with the app
(assets/samples/), so the page works without the internet. When an archive has a real block of a type, the page shows
that one instead: the samples matter for the types no archive has yet.
"""

from __future__ import annotations

from typing import Any

from .normalize import MediaResolver

U1, U2, U3 = (f"00000000-0000-4000-8000-0000000d00{i:02d}" for i in (1, 2, 3))
UA = "00000000-0000-4000-8000-0000000d00a1"
DEMO_MEDIA = {U1: ("sample-1.svg", 1200, 675), U2: ("sample-2.svg", 800, 800), U3: ("sample-3.svg", 900, 1200)}


def demo_resolver() -> MediaResolver:
    """The sample pictures resolve to the app's own files (/assets/samples/...)."""
    return MediaResolver({k: {"path": f"assets/samples/{name}", "mime": "image/svg+xml"} for k, (name, _, _) in
                          DEMO_MEDIA.items()})


def img(u: str) -> dict:
    _, w, h = DEMO_MEDIA[u]
    return {"type": "image", "data": {"uuid": u, "width": w, "height": h, "type": "svg"}}


SAMPLES: dict[str, dict[str, Any]] = {
    "text": {"text": "<p>Обычный абзац: <b>жирный</b>, <i>курсив</i>, <s>зачёркнутый</s> и "
                     "<a href=\"https://dtf.ru/\">ссылка</a>. Длинные строки переносятся по ширине страницы.</p>"},
    "header": {"text": "Заголовок раздела", "style": "h2"},
    "list": {"type": "UL", "items": ["Первый пункт списка", "Второй, с <b>выделением</b>", "Третий пункт"]},
    "quote": {"text": "<p>Цитата: слова другого человека, отделённые от текста поста.</p>",
              "subline1": "Имя автора", "subline2": "кто он", "type": "default"},
    "incut": {"text": "<p>Врезка — заметка на полях: её ставят, чтобы выделить важное.</p>"},
    "delimiter": {"type": "default"},
    "media": {"items": [{"title": "Подпись к первой картинке", "image": img(U1)}, {"title": "", "image": img(U2)},
                        {"title": "Вертикальная картинка", "image": img(U3)}], "title": "Галерея из трёх картинок"},
    "video": {"video": {"type": "video", "data": {"external_service": {"name": "youtube", "id": "aqz-KE-bpKQ"},
                                                  "thumbnail": img(U1)}}, "title": "Видео с YouTube"},
    "audio": {"audio": {"type": "audio", "data": {"uuid": UA, "filename": "melody.mp3",
                                                  "audio_info": {"duration": 95, "format": "mp3"}}},
              "image": img(U2), "title": "Пример трека"},
    "link": {"link": {"type": "link", "data": {"url": "https://example.com/article", "hostname": "example.com",
                                               "title": "Заголовок страницы по ссылке",
                                               "description": "Описание, которое DTF подтягивает со страницы.",
                                               "image": img(U1)}}},
    "osnovaEmbed": {"osnovaEmbed": {"type": "osnovaEmbed", "data": {
        "original_id": 1, "url": "https://dtf.ru/1", "title": "Встроенный пост DTF",
        "description": "Карточка другого поста с сайта: открывается в архиве, если он там есть.",
        "image": img(U2), "subsite": {"name": "Блог автора"}}}},
    "code": {"lang": "python", "text": "def hello(name):\n    return f\"Привет, {name}!\"\n\nprint(hello(\"DTF\"))"},
    "quiz": {"title": "Какой жанр вам ближе?", "items": {"a1": "Ролевые игры", "a2": "Стратегии", "a3": "Шутеры"},
             "hash": "sample"},
    "person": {"title": "Имя Фамилия", "description": "Короткое описание человека, о котором пост.", "image": img(U2)},
    "telegram": {"telegram": {"type": "telegram", "data": {"tg_data": {
        "url": "https://t.me/example/1", "datetime": 1790000000, "author": {"name": "Канал в Telegram"},
        "text": "<p><b>Пост из Telegram</b></p><p>DTF сохраняет текст поста канала, автора и ссылку на оригинал.</p>",
        "photos": [], "videos": []}}}},
    "special_button": {"text": "Перейти на сайт", "url": "https://example.com/"},
    "telegram_button": {"text": "Подписаться в Telegram", "url": "https://t.me/example"},
    "rawhtml": {"raw": "<iframe src=\"https://example.com/embed\" width=\"560\" height=\"315\"></iframe>"},
    "tweet": {"tweet": {"type": "tweet", "data": {"name": "Автор твита", "text": "Текст твита: LDTF показывает его "
                                                  "как текст со ссылкой на оригинал.",
                                                  "url": "https://x.com/example/status/1"}}},
    "instagram": {"instagram": {"type": "instagram", "data": {"title": "Пост в Instagram",
                                                              "url": "https://www.instagram.com/p/example/"}}},
    "tiktok": {"tiktok": {"type": "tiktok", "data": {"title": "Видео в TikTok",
                                                     "url": "https://www.tiktok.com/@example/video/1"}}},
    "yamusic": {"yamusic": {"type": "yamusic", "data": {"title": "Альбом в Яндекс Музыке",
                                                        "url": "https://music.yandex.ru/album/1"}}},
    "spotify": {"spotify": {"type": "spotify", "data": {"title": "Плейлист в Spotify",
                                                        "url": "https://open.spotify.com/playlist/example"}}},
    "game": {"game": {"title": "Название игры", "description": "Карточка игры из каталога DTF.",
                      "url": "https://dtf.ru/games/example", "image": img(U3)}},
    "number": {"text": "42", "title": "миллиона игроков за первую неделю"},
    "embed": {"embed": {"title": "Встраивание старого формата", "url": "https://example.com/embed"}},
    "movie": {"movie": {"title": "Фильм в старом формате", "url": "https://example.com/movie", "image": img(U1)}},
}


def sample_block(t: str) -> dict:
    return {"type": t, "cover": False, "hidden": False, "anchor": "", "data": SAMPLES.get(t, {})}
