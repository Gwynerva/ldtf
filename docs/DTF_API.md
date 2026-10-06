# DTF API: как этот инструмент получает данные

Справка для людей и ИИ-агентов, которые будут поддерживать `dtf_backup`. Всё ниже проверено вживую
(сентябрь 2026). Быстрая проверка, что так и осталось, — `python -m dtf_backup check-api` (или страница
«Диагностика API» в приложении). Живые тесты: `DTF_LIVE=1 python -m unittest tests.test_live_api`.

## 1. Откуда брать API

- dtf.ru — Vue SPA на платформе **Osnova** (та же, что у vc.ru). Клиент ходит в `https://api.dtf.ru/<версия>/<эндпоинт>`.
  У разных эндпоинтов разные версии (`v2.5`, `v2.7`, `v2.10`…); старые `v1.x` и `X-Device-Token` больше не работают.
- **Как найти эндпоинты заново**, если что-то поменялось:
  1. Открыть любую страницу dtf.ru и найти главный бандл `https://dtf.ru/assets/index-<hash>.js`
     (в `<script src>` или в сетевых запросах).
  2. Скачать его и грепать `transport.get(` / `transport.post(` вместе с `apiVersion:"v2.X"`.
     Пример: `transport.get("comments",{...,subsiteId:t},{auth:...,apiVersion:"v2.5"})`.
  3. Параметры пагинации видны рядом: `lastId`, `lastSortingValue`, `cursor`.
  4. Карта рендеров блоков редактора — объект вида `{text:…,header:…,list:…,media:…}` в компоненте рендера блоков
     (грепать `osnovaEmbed:`).
- Ответ API почти всегда: `{"message": "", "result": ...}`.

## 2. Эндпоинты, которые использует инструмент (все работают анонимно)

| Назначение | Запрос | Модуль |
|---|---|---|
| Профиль | `GET v2.7/subsite?uri=<ник>&markdown=false` или `?id=<id>` | `api.Dtf.subsite` |
| Посты пользователя | `GET v2.10/timeline?markdown=false&sorting=new&subsitesIds=<id>[&cursor=…]` | `api.Dtf.timeline_page` |
| Пост целиком | `GET v2.10/content?id=<postId>&markdown=false` | `api.Dtf.content` |
| Всё дерево комментариев поста | `GET v2.10/comments?sorting=date&contentId=<postId>` | `api.Dtf.post_comments` |
| Корневая ветка вокруг комментария | `GET v2.10/comments?commentId=<id>` | `api.Dtf.comment_branch` |
| Лента комментариев пользователя | `GET v2.5/comments?sorting=new&subsiteId=<id>[&lastId=…&lastSortingValue=…]` | `api.Dtf.user_comments_page` |
| Каталоги реакций и значков | `GET v2.9/assets` | `api.Dtf.assets` |

Подробности и подводные камни:

- **timeline**: 12 постов на страницу; следующая страница — параметр `cursor` из ответа (`result.cursor`).
  Элементы — `result.items[].data` (полный пост с `blocks`). В новом DTF все посты автора лежат в его блоге
  (`subsiteId` = id автора), отдельных «постов в сообществах» нет.
- **content** даёт то же, что элемент ленты, плюс `media`, `categories`, `customCover`, `keywords`.
  Для удалённого или скрытого поста — 404; тогда инструмент сохраняет копию из ленты (`_source: "timeline"`), но
  только если полной версии в архиве ещё нет.
- **comments?contentId** — **только без `firstLoad=true`**: с ним приходят лишь 2 уровня. Без него приходит всё дерево
  (даже 2000+ комментариев за раз; `len(items)` бывает больше `counters.comments`, так как учитываются удалённые).
- **comments?commentId** отдаёт всю корневую ветку (корень, все потомки), в которой лежит комментарий.
- **comments?subsiteId** — комментарии, написанные пользователем, 30 на страницу, от новых к старым.
  Пагинация: `lastId` + `lastSortingValue` из ответа (или id и date последнего элемента).
  **Трюк:** `lastId=999999999&lastSortingValue=<unix time>` начинает ленту с произвольной даты. Так первичная выгрузка
  идёт параллельно по месячным срезам (`sync._make_slices`). Комментарии, удалённые модератором, в ленте не приходят,
  но видны в ветках (с текстом «Комментарий удалён модератором»).
- **assets**: `reactions[] = {id, type (free|plus), staticUuid, animatedUuid}`, `badges[]`. Названий и полярности
  у реакций нет; ▲/▼ задаёт пользователь (`archive/reactions.config.json`, общий для всех архивов).

## 3. Структуры данных

**Комментарий** (важные поля): `id, date (unix), author{id,name,nickname,uri,avatar}, replyTo (0 для корня), level,
threadId` (хеш корневой ветки), `entry{id,title,subsiteId,subsiteName}`, `text, media[], likes{counterLikes},
reactions{counters[{id,count}]}, replyCount, isRemoved, isRemovedByModerator, isEdited, lastModificationDate`.

- `likes.counterLikes` — «рейтинг» DTF: **любая реакция считается +1**, дизлайков в новом DTF нет.
- `text` — **plain text** (буквальные `<` и `>` возможны, иногда `&gt;`) плюс теги
  `<mention id="…" nickname="…">Имя</mention>`. Строки с `>` в начале — цитаты. Разбор: `normalize.comment_text`.
- `media[]`: `{type: image|movie|video|link, data}`; `movie` — загруженное видео или gif (mp4),
  `video` — внешнее (`external_service {name, id}` + `thumbnail`), `link` — карточка ссылки.

**Пост**: `id, date, dateModified, title, url, subsiteId, author, subsite, counters{comments,favorites,reposts…},
reactions, repostId, repostData{type,data{original_id,title,blocks,author…}}, blocks[]`.

**Блоки редактора** (`blocks[] = {type, data, cover, hidden, anchor}`):
- `hidden: true` — **спойлер**; `cover: true` — блок показывается в ленте; `anchor` — id для оглавлений (`#anchor`).
- Типы из бандла сайта: `text, header, list, delimiter, quote, media, link, video, tweet, telegram, osnovaEmbed, audio,
  rawhtml, special_button, telegram_button, quiz, code, person, yamusic, spotify, game, tiktok, instagram, incut, number`.
- Текст блоков — санитизированный **HTML** (`p, a, b, i, em, br`…). Внешние ссылки обёрнуты:
  `https://api.dtf.ru/v2.8/redirect?to=<url-encoded>&postId=…`, разворачивает `normalize.unwrap_url`.
- Медиа-объект: `{type: "image", data: {uuid, width, height, size, type (jpg|png|gif|webp…), color, isVideo, has_audio,
  duration}}`. В `uuid` иногда лежит URL (`https://leonardo.osnova.io/ico/<домен>` — иконка ссылки).
- Рендер и запасной вариант для неизвестных типов — `blocks.py` (`FULL`, `PARTIAL`, `GENERIC_TYPES`).

**Удалённое и замороженное** (проверено 2026-09-28; распознаёт `guard.py`):
- **Удалённый аккаунт:** `subsite` отвечает 200: `name` «Аккаунт удален», `isRemovedByUserRequest: true`, аватар-заглушка,
  `nickname: null`, `uri: ""`. **Замороженный:** «Аккаунт заморожен», `isFrozen: true`. Несуществующий id или ник — 404.
  Счётчики профиля (`counters.entries/comments`) у всех равны 1 и для сверки бесполезны.
- **Посты** такого аккаунта остаются в ленте: `title` «Статья удалена», один блок «Этот материал был удалён по просьбе
  автора.», флаг поста `isRemovedByUserRequest`, `counters.comments: 0`, `dateModified` прежний.
  Живой автор может «стереть» пост правкой — заменить заголовок и текст на «статья удалена».
- **Комментарии** удалённых и замороженных аккаунтов приходят с текстом «Комментарий недоступен» (медиа иногда остаются).
  Другие заглушки:
  - «Комментарий удалён модератором» (`isRemoved` + `isRemovedByModerator`);
  - «Комментарий удалён автором поста» (`isRemoved`);
  - «Комментарий недоступен» без флагов — автор скрыл или удалил комментарий, аккаунт жив.

  Флаг `isRemovedByModerator` бывает и у комментариев с обычным текстом (восстановленных), поэтому заглушку определяет
  текст, а флаги объясняют причину.

## 4. Медиа: CDN `leonardo.osnova.io`

| URL | Что отдаёт |
|---|---|
| `/<uuid>/` | оригинал картинки; для gif и видео — **302 на `/-/format/mp4/`** (mp4 и есть каноничный файл) |
| `/<uuid>/-/format/mp4/` | видео или gif как mp4 (поддерживает Range) |
| `/<uuid>/-/format/raw/` | исходный файл без конвертации; нужен для **анимированных реакций** (animated WebP) |
| `/<uuid>/-/scale_crop/72x72/` | маленькая квадратная JPEG-копия (~2 КБ) — аватарки авторов |
| `/ico/<домен>` | иконка сайта для карточек ссылок |

uuid почти всегда версии 5 (детерминированный), так что повторная загрузка того же файла обычно получает тот же uuid.
Хранилище инструмента адресуется по содержимому (`archive/media/<sha[:2]>/<sha256>.<ext>`, общее для всех архивов),
дедупликация: uuid → Range-проба (первые и последние 64 КБ) → sha256. Модуль: `media.py`.

## 5. Скорость и ограничения

- **Новое TLS-соединение на каждый запрос → DTF начинает рвать TCP-соединения** (примерно после 300 запросов).
  С keep-alive (`http.HttpClient`: одно соединение на поток и хост) ограничений не видно: 1 поток ≈ 2,5 запроса/с,
  4 потока ≈ 9,5/с, 8 потоков ≈ 17/с без ошибок.
- На лёгких эндпоинтах (`comments?commentId`) при ~18 запросах/с приходит **HTTP 429**. `AdaptiveLimiter`
  стартует с 10 запросов/с, на 429 снижает темп ×0,7 и медленно поднимает его обратно; на сетевых ошибках делает
  экспоненциальную паузу, после серии ошибок подряд sync останавливается («предохранитель»).
- Авторизация скорость **не повышает** (замерено: 0,43 с на запрос против 0,36 с анонимно — анонимные ответы
  частично кешируются nginx).

## 6. Авторизация (не используется)

- Заголовок `JWTAuthorization: Bearer <access token>`. Access token живёт **300 секунд**.
- Refresh token лежит в браузере в `localStorage["auth-refresh-token"] = {"token", "expTimestamp"}` (~60 дней).
  Обновление: `POST https://api.dtf.ru/v3.4/auth/refresh {token}` → новый AT **и новый RT**. RT **одноразовый**:
  если использовать токен браузера из скрипта, браузер разлогинится при следующем обновлении.
- Отдельная сессия для скрипта: войти в приватном окне, забрать `auth-refresh-token`, закрыть окно, не выходя из аккаунта.
- Даёт: черновики (`v2.10/new/posts/drafts`), историю правок (`content/<id>/history[/<ver>]`), приватную статистику
  постов (`v2.12/posts/<id>/stats`). В архив это сейчас не входит.
- Устарело: токены «Инструменты разработчика» (`X-Device-Token`), `v1.x`, вебхуки, `ws-sio.dtf.ru` (socket.io 2.x).

## 7. Ссылки

- Пост: `https://dtf.ru/<postId>` → 301 на канонический URL; комментарий: `https://dtf.ru/<postId>?comment=<id>`.
- Профиль: `https://dtf.ru/<ник>` или `https://dtf.ru/id<id>`; `https://dtf.ru/u/<id>-slug` — тоже профиль (не пост!).

## 8. Где что в коде

| Модуль | Роль |
|---|---|
| `api.py` | эндпоинты DTF (раздел 2) |
| `http.py` | keep-alive клиент, адаптивный лимитер, ретраи |
| `sync.py` | стадии: profile → posts → comments (срезы по месяцам) → threads (контекст) → media |
| `context.py` | деревья комментариев, «предки + ответы» |
| `media.py` | сбор uuid из JSON, загрузка, дедупликация, варианты CDN |
| `normalize.py` | HTML блоков, текст комментариев (mention, цитаты), разворот ссылок |
| `blocks.py` | рендер блоков редактора и запасной вариант для неизвестных |
| `reactions.py` | каталог реакций, ▲/▼ |
| `viewdb.py`, `export.py`, `render.py` | view.sqlite для приложения, `data/` и `md/` для агентов |
| `web/` | локальное приложение (сервер, страницы, задания) |
| `checkapi.py` | живые контрактные проверки API (этот документ в виде тестов) |

Если сломалось: запустить `check-api`, у упавшей проверки есть подсказка «смотреть: …»; сверить с бандлом сайта
(раздел 1) и поправить соответствующий модуль. Сырые ответы в `archive/<ник>/raw/` не зависят от кода рендера:
после исправления достаточно `render`.
