from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timezone
import json
import time
import os
import uuid

from mangaplus import MangaPlus
from mangaplus.constants import Language, Viewer, Quality, Ranking, TitleType

APP_VERSION = 237
CACHE_TTL = 600
CHAPTER_CACHE = {}

CACHE = {
    "updates": {}
}

LANG_MAP = {
    "en": "ENGLISH",
    "id": "INDONESIAN",
    "es": "SPANISH",
    "fr": "FRENCH",
    "pt": "PORTUGUESE_BR",
    "pt-br": "PORTUGUESE_BR",
    "ru": "RUSSIAN",
    "th": "THAI",
    "vi": "VIETNAMESE",
    "de": "GERMAN",
}


def get_lang(code):
    code = (code or "en").lower()
    name = LANG_MAP.get(code, "ENGLISH")
    return getattr(Language, name, Language.ENGLISH)


def to_iso(ts):
    if not ts:
        return None

    try:
        ts = int(ts)
        if ts > 10000000000:
            ts = ts / 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except Exception:
        return None


def normalize_updates(raw):
    items = []
    seen = set()

    def val(data, keys, default=None):
        if not isinstance(data, dict):
            return default

        for key in keys:
            if key in data and data[key] not in [None, "", [], {}]:
                return data[key]

        return default

    def title_info(obj):
        if not isinstance(obj, dict):
            return None

        title_id = val(obj, ["titleId", "titleID"])
        name = val(obj, ["name", "titleName", "englishName"])
        author = val(obj, ["author", "authorName"])
        cover = val(obj, [
            "portraitImageUrl",
            "titleImageUrl",
            "thumbnailUrl",
            "landscapeImageUrl",
            "backgroundImageUrl",
        ])

        if title_id or name or cover:
            return {
                "title_id": title_id,
                "title": name,
                "author": author,
                "cover": cover,
            }

        return None

    def chapter_info(obj, ctx_title):
        if not isinstance(obj, dict):
            return None

        chapter_id = val(obj, ["chapterId", "chapterID"])
        if not chapter_id:
            return None

        chapter_name = val(obj, [
            "name",
            "chapterName",
            "title",
            "chapterTitle",
        ])

        subtitle = val(obj, [
            "subTitle",
            "subtitle",
            "chapterSubTitle",
            "chapterSubtitle",
        ])

        thumbnail = val(obj, [
            "thumbnailUrl",
            "thumbnailURL",
            "chapterImageUrl",
        ])

        start_ts = val(obj, [
            "startTimeStamp",
            "startTimestamp",
            "releaseStartTimeStamp",
            "updateTimeStamp",
        ])

        end_ts = val(obj, [
            "endTimeStamp",
            "endTimestamp",
        ])

        t = ctx_title or {}

        return {
            "title_id": t.get("title_id"),
            "title": t.get("title"),
            "author": t.get("author"),
            "cover": t.get("cover") or thumbnail,
            "chapter_id": chapter_id,
            "chapter": chapter_name,
            "subtitle": subtitle,
            "thumbnail": thumbnail or t.get("cover"),
            "published_at": to_iso(start_ts),
            "expired_at": to_iso(end_ts),
            "title_url": f"https://mangaplus.shueisha.co.jp/titles/{t.get('title_id')}" if t.get("title_id") else None,
            "chapter_url": f"https://mangaplus.shueisha.co.jp/viewer/{chapter_id}",
        }

    def scan(node, ctx_title=None):
        if isinstance(node, dict):
            local_title = ctx_title

            own_title = title_info(node)
            if own_title and own_title.get("title_id"):
                local_title = own_title

            for key in [
                "title",
                "titleView",
                "titleDetail",
                "titleDetailView",
                "titleInfo",
                "titleData",
                "mangaTitle",
            ]:
                child = node.get(key)
                if isinstance(child, dict):
                    child_title = title_info(child)
                    if child_title:
                        local_title = child_title

            chapter = chapter_info(node, local_title)

            if chapter:
                unique_key = f"{chapter.get('title_id')}:{chapter.get('chapter_id')}"
                if unique_key not in seen:
                    seen.add(unique_key)
                    items.append(chapter)

            for value in node.values():
                scan(value, local_title)

        elif isinstance(node, list):
            for item in node:
                scan(item, ctx_title)

    scan(raw)

    items = [item for item in items if item.get("chapter_id")]

    items.sort(
        key=lambda x: (
            0 if x.get("title") else 1,
            str(x.get("title") or ""),
            str(x.get("chapter_id") or ""),
        )
    )

    return items


def fetch_updates(lang_code="en", force=False):
    lang_code = (lang_code or "en").lower()
    now = time.time()

    cached = CACHE["updates"].get(lang_code)

    if not force and cached and now - cached["time"] < CACHE_TTL:
        return cached["items"]

    selected_lang = get_lang(lang_code)

    client = MangaPlus(
        lang=selected_lang,
        clang=[selected_lang],
        viewer=Viewer.VERTICAL,
    )

    client.APP_VERSION = APP_VERSION
    client.register(device_id=str(uuid.uuid4()))

    raw = client.getHome()
    items = normalize_updates(raw)

    CACHE["updates"][lang_code] = {
        "time": now,
        "items": items,
    }

    return items



def get_quality(code):
    code = (code or "super_high").lower().replace("-", "_")

    mapping = {
        "super_high": "SUPER_HIGH",
        "high": "HIGH",
        "low": "LOW",
    }

    name = mapping.get(code, "SUPER_HIGH")
    return getattr(Quality, name, Quality.SUPER_HIGH)


def extract_chapter_pages(raw):
    pages = []
    title = None
    chapter = None

    def val(data, keys, default=None):
        if not isinstance(data, dict):
            return default
        for key in keys:
            if key in data and data[key] not in [None, "", [], {}]:
                return data[key]
        return default

    def scan_meta(node):
        nonlocal title, chapter

        if isinstance(node, dict):
            if title is None:
                title = val(node, ["titleName", "title", "name"])
            if chapter is None:
                chapter = val(node, ["chapterName", "chapterTitle", "subtitle", "subTitle"])

            for v in node.values():
                scan_meta(v)

        elif isinstance(node, list):
            for item in node:
                scan_meta(item)

    def scan_pages(node):
        if isinstance(node, dict):
            manga_page = node.get("mangaPage") or node.get("manga_page")

            if isinstance(manga_page, dict):
                image_url = val(manga_page, ["imageUrl", "imageURL", "url"])
                width = val(manga_page, ["width"])
                height = val(manga_page, ["height"])
                encryption_key = val(manga_page, ["encryptionKey", "encryption_key"])

                if image_url:
                    pages.append({
                        "index": len(pages) + 1,
                        "image_url": image_url,
                        "width": width,
                        "height": height,
                        "is_encrypted": bool(encryption_key),
                    })

            else:
                image_url = val(node, ["imageUrl", "imageURL", "url"])

                if image_url and str(image_url).startswith("http"):
                    pages.append({
                        "index": len(pages) + 1,
                        "image_url": image_url,
                        "width": val(node, ["width"]),
                        "height": val(node, ["height"]),
                        "is_encrypted": bool(val(node, ["encryptionKey", "encryption_key"])),
                    })

            for v in node.values():
                scan_pages(v)

        elif isinstance(node, list):
            for item in node:
                scan_pages(item)

    scan_meta(raw)
    scan_pages(raw)

    # Hapus duplikat URL
    clean = []
    seen = set()

    for page in pages:
        url = page.get("image_url")
        if url and url not in seen:
            seen.add(url)
            page["index"] = len(clean) + 1
            clean.append(page)

    return {
        "title": title,
        "chapter": chapter,
        "pages": clean,
        "page_count": len(clean),
        "encrypted_pages": len([x for x in clean if x.get("is_encrypted")]),
    }


def fetch_chapter(chapter_id, lang_code="id", quality_code="super_high", force=False):
    cache_key = f"{lang_code}:{chapter_id}:{quality_code}"
    now = time.time()

    cached = CHAPTER_CACHE.get(cache_key)
    if not force and cached and now - cached["time"] < CACHE_TTL:
        return cached["data"]

    selected_lang = get_lang(lang_code)

    client = MangaPlus(
        lang=selected_lang,
        clang=[selected_lang],
        viewer=Viewer.VERTICAL
    )

    client.APP_VERSION = 237

    import uuid
    client.register(device_id=str(uuid.uuid4()))

    raw = client.getMangaData(
        chapter_id=int(chapter_id),
        split=True,
        quality=get_quality(quality_code)
    )

    parsed = extract_chapter_pages(raw)

    data = {
        "ok": True,
        "source": "MANGA Plus by SHUEISHA",
        "lang": lang_code,
        "chapter_id": int(chapter_id),
        "quality": quality_code,
        "title": parsed.get("title"),
        "chapter": parsed.get("chapter"),
        "page_count": parsed.get("page_count"),
        "encrypted_pages": parsed.get("encrypted_pages"),
        "pages": parsed.get("pages"),
    }

    CHAPTER_CACHE[cache_key] = {
        "time": now,
        "data": data,
    }

    return data


# =========================
# EXTRA NORMALIZED ENDPOINTS
# =========================

PUBLIC_LANGUAGES = [
    {"code": "en", "api_code": "eng", "name": "English"},
    {"code": "id", "api_code": "ind", "name": "Indonesian"},
    {"code": "es", "api_code": "esp", "name": "Spanish"},
    {"code": "fr", "api_code": "fra", "name": "French"},
    {"code": "pt-br", "api_code": "ptb", "name": "Portuguese BR"},
    {"code": "ru", "api_code": "rus", "name": "Russian"},
    {"code": "th", "api_code": "tha", "name": "Thai"},
    {"code": "vi", "api_code": "vie", "name": "Vietnamese"},
    {"code": "de", "api_code": "deu", "name": "German"},
]

TITLE_TYPE_MAP = {
    "serializing": TitleType.SERIALIZING,
    "ongoing": TitleType.SERIALIZING,
    "completed": TitleType.COMPLETED,
    "complete": TitleType.COMPLETED,
    "one-shot": TitleType.ONE_SHOT,
    "oneshot": TitleType.ONE_SHOT,
    "one_shot": TitleType.ONE_SHOT,
}

RANKING_MAP = {
    "hottest": Ranking.HOTTEST,
    "hot": Ranking.HOTTEST,
    "trending": Ranking.TRENDING,
    "trend": Ranking.TRENDING,
    "completed": Ranking.COMPLETED,
    "complete": Ranking.COMPLETED,
}

def make_client(lang_code="id"):
    selected_lang = get_lang(lang_code)
    client = MangaPlus(
        lang=selected_lang,
        clang=[selected_lang],
        viewer=Viewer.VERTICAL
    )
    client.APP_VERSION = APP_VERSION
    client.register(device_id=str(uuid.uuid4()))
    return client

def pick(data, keys, default=None):
    if not isinstance(data, dict):
        return default
    for key in keys:
        if key in data and data[key] not in [None, "", [], {}]:
            return data[key]
    return default

def iter_dicts(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from iter_dicts(value)
    elif isinstance(node, list):
        for item in node:
            yield from iter_dicts(item)

def normalize_title_items(raw):
    items = []
    seen = set()

    for obj in iter_dicts(raw):
        title_id = pick(obj, ["titleId", "titleID"])
        name = pick(obj, ["name", "titleName", "englishName"])
        author = pick(obj, ["author", "authorName"])
        description = pick(obj, ["description", "overview", "synopsis"])
        cover = pick(obj, [
            "portraitImageUrl",
            "titleImageUrl",
            "thumbnailUrl",
            "landscapeImageUrl",
            "backgroundImageUrl",
        ])
        banner = pick(obj, [
            "backgroundImageUrl",
            "landscapeImageUrl",
            "bannerImageUrl",
        ])

        if not title_id:
            continue

        key = str(title_id)
        if key in seen:
            continue

        seen.add(key)
        items.append({
            "title_id": title_id,
            "title": name,
            "author": author,
            "description": description,
            "cover": cover,
            "thumbnail": cover,
            "banner": banner or cover,
            "title_url": f"https://mangaplus.shueisha.co.jp/titles/{title_id}",
            "detail_api": f"/api/detail/{title_id}",
        })

    return items

def normalize_creators(items):
    creators = {}
    for item in items:
        author = item.get("author")
        if not author:
            continue

        if author not in creators:
            creators[author] = {
                "name": author,
                "total_titles": 0,
                "titles": []
            }

        creators[author]["total_titles"] += 1
        creators[author]["titles"].append({
            "title_id": item.get("title_id"),
            "title": item.get("title"),
            "cover": item.get("cover"),
            "detail_api": item.get("detail_api"),
        })

    return sorted(creators.values(), key=lambda x: x["name"].lower())

def paginate_items(items, page=1, limit=30):
    page = max(1, int(page))
    limit = max(1, min(int(limit), 100))
    total = len(items)
    start = (page - 1) * limit
    end = start + limit

    return {
        "page": page,
        "limit": limit,
        "total": total,
        "total_pages": (total + limit - 1) // limit,
        "count": len(items[start:end]),
        "items": items[start:end],
    }

def fetch_home_data(lang_code="id", force=False):
    cache_key = f"home:{lang_code}"
    now = time.time()

    cached = CHAPTER_CACHE.get(cache_key)
    if not force and cached and now - cached["time"] < CACHE_TTL:
        return cached["data"]

    client = make_client(lang_code)
    raw = client.getHome()

    data = {
        "ok": True,
        "source": "MANGA Plus by SHUEISHA",
        "lang": lang_code,
        "raw_keys": list(raw.keys()) if isinstance(raw, dict) else [],
        "featured": normalize_title_items(raw),
        "updates": normalize_updates(raw),
        "raw": raw,
    }

    CHAPTER_CACHE[cache_key] = {"time": now, "data": data}
    return data

def fetch_all_titles_data(lang_code="id", title_type="serializing", force=False):
    title_type = (title_type or "serializing").lower()
    enum_type = TITLE_TYPE_MAP.get(title_type, TitleType.SERIALIZING)

    cache_key = f"titles:{lang_code}:{title_type}"
    now = time.time()

    cached = CHAPTER_CACHE.get(cache_key)
    if not force and cached and now - cached["time"] < CACHE_TTL:
        return cached["data"]

    client = make_client(lang_code)
    raw = client.getAllTitlesV3(title_type=enum_type)
    items = normalize_title_items(raw)

    data = {
        "ok": True,
        "source": "MANGA Plus by SHUEISHA",
        "lang": lang_code,
        "type": title_type,
        "items": items,
        "raw": raw,
    }

    CHAPTER_CACHE[cache_key] = {"time": now, "data": data}
    return data

def fetch_ranking_data(lang_code="id", ranking_type="hottest", force=False):
    ranking_type = (ranking_type or "hottest").lower()
    enum_type = RANKING_MAP.get(ranking_type, Ranking.HOTTEST)

    cache_key = f"ranking:{lang_code}:{ranking_type}"
    now = time.time()

    cached = CHAPTER_CACHE.get(cache_key)
    if not force and cached and now - cached["time"] < CACHE_TTL:
        return cached["data"]

    client = make_client(lang_code)
    raw = client.getRankingV2(ranking=enum_type)
    items = normalize_title_items(raw)

    data = {
        "ok": True,
        "source": "MANGA Plus by SHUEISHA",
        "lang": lang_code,
        "type": ranking_type,
        "items": items,
        "raw": raw,
    }

    CHAPTER_CACHE[cache_key] = {"time": now, "data": data}
    return data

def fetch_search_data(lang_code="id", q="", force=False):
    q = (q or "").strip().lower()

    cache_key = f"search-base:{lang_code}"
    now = time.time()

    cached = CHAPTER_CACHE.get(cache_key)
    if not force and cached and now - cached["time"] < CACHE_TTL:
        all_items = cached["items"]
    else:
        client = make_client(lang_code)
        raw = client.getSearchTitles()
        all_items = normalize_title_items(raw)

        # fallback kalau search endpoint kosong
        if not all_items:
            all_items = []
            for t in ["serializing", "completed", "one-shot"]:
                all_items.extend(fetch_all_titles_data(lang_code, t, force=True)["items"])

        # dedupe
        clean = []
        seen = set()
        for item in all_items:
            key = str(item.get("title_id"))
            if key not in seen:
                seen.add(key)
                clean.append(item)
        all_items = clean

        CHAPTER_CACHE[cache_key] = {"time": now, "items": all_items}

    if q:
        all_items = [
            item for item in all_items
            if q in str(item.get("title") or "").lower()
            or q in str(item.get("author") or "").lower()
            or q in str(item.get("description") or "").lower()
        ]

    return all_items

def build_schedule_from_updates(updates):
    groups = {}

    for item in updates:
        published = item.get("published_at")
        key = "unknown"

        if published:
            key = published[:10]

        if key not in groups:
            groups[key] = []

        groups[key].append(item)

    result = []
    for date_key, items in groups.items():
        result.append({
            "date": date_key,
            "count": len(items),
            "items": items,
        })

    return result

def fetch_settings_data(lang_code="id", force=False):
    cache_key = f"settings:{lang_code}"
    now = time.time()

    cached = CHAPTER_CACHE.get(cache_key)
    if not force and cached and now - cached["time"] < CACHE_TTL:
        return cached["data"]

    client = make_client(lang_code)
    raw = client.getSettings()

    data = {
        "ok": True,
        "source": "MANGA Plus by SHUEISHA",
        "lang": lang_code,
        "languages": PUBLIC_LANGUAGES,
        "raw": raw,
    }

    CHAPTER_CACHE[cache_key] = {"time": now, "data": data}
    return data


# =========================
# DETAIL MANGA ENDPOINT
# =========================

def normalize_title_detail_full(raw, title_id):
    detail = {
        "title_id": int(title_id),
        "title": None,
        "author": None,
        "description": None,
        "cover": None,
        "thumbnail": None,
        "banner": None,
        "chapters": [],
    }

    seen_chapters = set()

    def val(data, keys, default=None):
        if not isinstance(data, dict):
            return default
        for key in keys:
            if key in data and data[key] not in [None, "", [], {}]:
                return data[key]
        return default

    def scan(node):
        if isinstance(node, dict):
            maybe_title_id = val(node, ["titleId", "titleID"])
            maybe_title = val(node, ["name", "titleName", "englishName"])
            maybe_author = val(node, ["author", "authorName"])
            maybe_desc = val(node, ["description", "overview", "synopsis"])

            maybe_cover = val(node, [
                "portraitImageUrl",
                "titleImageUrl",
                "thumbnailUrl",
                "landscapeImageUrl",
                "backgroundImageUrl",
            ])

            maybe_banner = val(node, [
                "backgroundImageUrl",
                "landscapeImageUrl",
                "bannerImageUrl",
            ])

            if maybe_title_id or maybe_title or maybe_cover:
                if maybe_title and not detail["title"]:
                    detail["title"] = maybe_title

                if maybe_author and not detail["author"]:
                    detail["author"] = maybe_author

                if maybe_desc and not detail["description"]:
                    detail["description"] = maybe_desc

                if maybe_cover and not detail["cover"]:
                    detail["cover"] = maybe_cover
                    detail["thumbnail"] = maybe_cover

                if maybe_banner and not detail["banner"]:
                    detail["banner"] = maybe_banner

            chapter_id = val(node, ["chapterId", "chapterID"])

            if chapter_id and str(chapter_id) not in seen_chapters:
                seen_chapters.add(str(chapter_id))

                chapter_name = val(node, [
                    "name",
                    "chapterName",
                    "title",
                    "chapterTitle",
                ])

                subtitle = val(node, [
                    "subTitle",
                    "subtitle",
                    "chapterSubTitle",
                    "chapterSubtitle",
                ])

                thumbnail = val(node, [
                    "thumbnailUrl",
                    "thumbnailURL",
                    "chapterImageUrl",
                ])

                start_ts = val(node, [
                    "startTimeStamp",
                    "startTimestamp",
                    "releaseStartTimeStamp",
                    "updateTimeStamp",
                ])

                end_ts = val(node, [
                    "endTimeStamp",
                    "endTimestamp",
                ])

                detail["chapters"].append({
                    "chapter_id": chapter_id,
                    "chapter": chapter_name,
                    "subtitle": subtitle,
                    "thumbnail": thumbnail or detail.get("cover"),
                    "published_at": to_iso(start_ts),
                    "expired_at": to_iso(end_ts),
                    "reader_api": f"/api/chapter/{chapter_id}",
                    "reader_url": f"https://mangaplus.shueisha.co.jp/viewer/{chapter_id}",
                })

            for value in node.values():
                scan(value)

        elif isinstance(node, list):
            for item in node:
                scan(item)

    scan(raw)

    detail["chapters"].sort(
        key=lambda x: int(x.get("chapter_id") or 0),
        reverse=True
    )

    if not detail["banner"]:
        detail["banner"] = detail["cover"]

    return detail


def fetch_title_detail_full(title_id, lang_code="id", force=False):
    cache_key = f"full-detail:{lang_code}:{title_id}"
    now = time.time()

    cached = CHAPTER_CACHE.get(cache_key)
    if not force and cached and now - cached["time"] < CACHE_TTL:
        return cached["data"]

    selected_lang = get_lang(lang_code)

    client = MangaPlus(
        lang=selected_lang,
        clang=[selected_lang],
        viewer=Viewer.VERTICAL
    )

    client.APP_VERSION = APP_VERSION
    client.register(device_id=str(uuid.uuid4()))

    raw = client.getTitleDetail(title_id=int(title_id))
    detail = normalize_title_detail_full(raw, title_id)

    data = {
        "ok": True,
        "source": "MANGA Plus by SHUEISHA",
        "lang": lang_code,
        "detail": detail,
    }

    CHAPTER_CACHE[cache_key] = {
        "time": now,
        "data": data,
    }

    return data

class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        print("%s - %s" % (self.address_string(), format % args))

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")

        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = parse_qs(parsed.query)

        lang = qs.get("lang", ["en"])[0]

        try:
            limit = int(qs.get("limit", ["30"])[0])
        except Exception:
            limit = 30

        try:
            page = int(qs.get("page", ["1"])[0])
        except Exception:
            page = 1

        limit = max(1, min(limit, 100))
        page = max(1, page)

        force = qs.get("force", ["0"])[0].lower() in ["1", "true", "yes"]

        try:
            if path == "/":
                return self.send_json({
                    "ok": True,
                    "message": "MangaPlus API aktif",
                    "source": "MANGA Plus by SHUEISHA",
                    "endpoints": {
                        "health": "/health",
                        "all_endpoints": "/api/endpoints",
                        "updates": "/api/updates?lang=id&limit=20",
                        "updates_page": "/api/updates?lang=id&page=1&limit=20",
                        "search": "/api/updates?lang=id&q=bug",
                        "title": "/api/title/100294?lang=id",
                        "cache_clear": "/api/cache/clear",
                    }
                })

            if path == "/health":
                return self.send_json({
                    "ok": True,
                    "status": "healthy",
                    "service": "MangaPlus Updates API",
                    "app_version": APP_VERSION,
                })


            if path == "/api/endpoints":
                return self.send_json({
                    "ok": True,
                    "base_url": "https://mangaplus-api.onrender.com",
                    "endpoints": {
                        "home": "/api/home?lang=id",
                        "updates": "/api/updates?lang=id&page=1&limit=20",
                        "featured": "/api/featured?lang=id",
                        "ranking": "/api/ranking?lang=id&type=hottest",
                        "ranking_trending": "/api/ranking?lang=id&type=trending",
                        "ranking_completed": "/api/ranking?lang=id&type=completed",
                        "titles_serializing": "/api/titles?lang=id&type=serializing&page=1&limit=30",
                        "titles_completed": "/api/titles?lang=id&type=completed&page=1&limit=30",
                        "titles_one_shot": "/api/titles?lang=id&type=one-shot&page=1&limit=30",
                        "search": "/api/search?lang=id&q=one",
                        "creators": "/api/creators?lang=id",
                        "languages": "/api/languages",
                        "settings": "/api/settings?lang=id",
                        "schedule": "/api/schedule?lang=id",
                        "detail": "/api/detail/100294?lang=id",
                        "chapter": "/api/chapter/1026318?lang=id&quality=super_high",
                        "raw_home": "/api/raw/home?lang=id",
                        "raw_settings": "/api/raw/settings?lang=id",
                    }
                })

            if path == "/api/languages":
                return self.send_json({
                    "ok": True,
                    "count": len(PUBLIC_LANGUAGES),
                    "languages": PUBLIC_LANGUAGES,
                })

            if path == "/api/settings":
                data = fetch_settings_data(lang_code=lang, force=force)
                raw = qs.get("raw", ["0"])[0].lower() in ["1", "true", "yes"]
                if not raw:
                    data = {k: v for k, v in data.items() if k != "raw"}
                return self.send_json(data)

            if path == "/api/home":
                data = fetch_home_data(lang_code=lang, force=force)
                raw = qs.get("raw", ["0"])[0].lower() in ["1", "true", "yes"]
                if not raw:
                    data = {k: v for k, v in data.items() if k != "raw"}
                return self.send_json(data)

            if path in ["/api/featured", "/api/unggulan"]:
                data = fetch_home_data(lang_code=lang, force=force)
                items = data.get("featured", [])

                q = qs.get("q", [""])[0].strip().lower()
                if q:
                    items = [
                        item for item in items
                        if q in str(item.get("title") or "").lower()
                        or q in str(item.get("author") or "").lower()
                    ]

                page_data = paginate_items(items, page=page, limit=limit)
                return self.send_json({
                    "ok": True,
                    "source": "MANGA Plus by SHUEISHA",
                    "lang": lang,
                    "section": "featured",
                    **page_data,
                })

            if path == "/api/ranking":
                ranking_type = qs.get("type", ["hottest"])[0]
                data = fetch_ranking_data(lang_code=lang, ranking_type=ranking_type, force=force)
                items = data.get("items", [])

                q = qs.get("q", [""])[0].strip().lower()
                if q:
                    items = [
                        item for item in items
                        if q in str(item.get("title") or "").lower()
                        or q in str(item.get("author") or "").lower()
                    ]

                page_data = paginate_items(items, page=page, limit=limit)
                return self.send_json({
                    "ok": True,
                    "source": "MANGA Plus by SHUEISHA",
                    "lang": lang,
                    "type": ranking_type,
                    **page_data,
                })

            if path in ["/api/titles", "/api/manga", "/api/daftar-manga"]:
                title_type = qs.get("type", ["serializing"])[0]
                data = fetch_all_titles_data(lang_code=lang, title_type=title_type, force=force)
                items = data.get("items", [])

                q = qs.get("q", [""])[0].strip().lower()
                if q:
                    items = [
                        item for item in items
                        if q in str(item.get("title") or "").lower()
                        or q in str(item.get("author") or "").lower()
                        or q in str(item.get("description") or "").lower()
                    ]

                page_data = paginate_items(items, page=page, limit=limit)
                return self.send_json({
                    "ok": True,
                    "source": "MANGA Plus by SHUEISHA",
                    "lang": lang,
                    "type": title_type,
                    **page_data,
                })

            if path == "/api/search":
                q = qs.get("q", [""])[0]
                items = fetch_search_data(lang_code=lang, q=q, force=force)
                page_data = paginate_items(items, page=page, limit=limit)

                return self.send_json({
                    "ok": True,
                    "source": "MANGA Plus by SHUEISHA",
                    "lang": lang,
                    "q": q,
                    **page_data,
                })

            if path in ["/api/creators", "/api/kreator"]:
                # Ambil kreator dari semua daftar title
                all_items = []
                for t in ["serializing", "completed", "one-shot"]:
                    all_items.extend(fetch_all_titles_data(lang_code=lang, title_type=t, force=force)["items"])

                # Dedupe title
                clean = []
                seen = set()
                for item in all_items:
                    key = str(item.get("title_id"))
                    if key not in seen:
                        seen.add(key)
                        clean.append(item)

                creators = normalize_creators(clean)

                q = qs.get("q", [""])[0].strip().lower()
                if q:
                    creators = [
                        c for c in creators
                        if q in c["name"].lower()
                    ]

                page_data = paginate_items(creators, page=page, limit=limit)
                return self.send_json({
                    "ok": True,
                    "source": "MANGA Plus by SHUEISHA",
                    "lang": lang,
                    **page_data,
                })

            if path in ["/api/schedule", "/api/jadwal"]:
                updates = fetch_updates(lang_code=lang, force=force)
                schedule = build_schedule_from_updates(updates)

                return self.send_json({
                    "ok": True,
                    "source": "MANGA Plus by SHUEISHA",
                    "lang": lang,
                    "note": "Schedule dibuat dari data update yang tersedia. Jika published_at null, item masuk ke grup unknown.",
                    "count": len(schedule),
                    "schedule": schedule,
                })

            if path == "/api/raw/home":
                return self.send_json(fetch_home_data(lang_code=lang, force=force).get("raw"))

            if path == "/api/raw/settings":
                return self.send_json(fetch_settings_data(lang_code=lang, force=force).get("raw"))

            if path == "/api/raw/search":
                client = make_client(lang)
                return self.send_json(client.getSearchTitles())

            if path == "/api/raw/titles":
                title_type = qs.get("type", ["serializing"])[0]
                enum_type = TITLE_TYPE_MAP.get(title_type, TitleType.SERIALIZING)
                client = make_client(lang)
                return self.send_json(client.getAllTitlesV3(title_type=enum_type))

            if path == "/api/raw/ranking":
                ranking_type = qs.get("type", ["hottest"])[0]
                enum_type = RANKING_MAP.get(ranking_type, Ranking.HOTTEST)
                client = make_client(lang)
                return self.send_json(client.getRankingV2(ranking=enum_type))

            if path.startswith("/api/raw/detail/"):
                title_id = path.replace("/api/raw/detail/", "").strip()
                if not title_id.isdigit():
                    return self.send_json({"ok": False, "message": "title_id tidak valid"}, status=400)
                client = make_client(lang)
                return self.send_json(client.getTitleDetail(title_id=int(title_id)))

            if path.startswith("/api/raw/chapter/"):
                chapter_id = path.replace("/api/raw/chapter/", "").strip()
                quality = qs.get("quality", ["super_high"])[0]
                if not chapter_id.isdigit():
                    return self.send_json({"ok": False, "message": "chapter_id tidak valid"}, status=400)
                client = make_client(lang)
                return self.send_json(client.getMangaData(
                    chapter_id=int(chapter_id),
                    split=True,
                    quality=get_quality(quality)
                ))

            if path == "/api/cache/clear":
                CACHE["updates"] = {}
                CHAPTER_CACHE.clear()
                return self.send_json({
                    "ok": True,
                    "message": "Cache berhasil dibersihkan",
                })



            if path.startswith("/api/detail/"):
                title_id = path.replace("/api/detail/", "").strip()

                if not title_id.isdigit():
                    return self.send_json({
                        "ok": False,
                        "message": "title_id tidak valid"
                    }, status=400)

                data = fetch_title_detail_full(
                    title_id=title_id,
                    lang_code=lang,
                    force=force
                )

                return self.send_json(data)

            if path.startswith("/api/chapter/"):
                chapter_id = path.replace("/api/chapter/", "").strip()
                quality = qs.get("quality", ["super_high"])[0]

                if not chapter_id.isdigit():
                    return self.send_json({
                        "ok": False,
                        "message": "chapter_id tidak valid"
                    }, status=400)

                data = fetch_chapter(
                    chapter_id=chapter_id,
                    lang_code=lang,
                    quality_code=quality,
                    force=force
                )

                return self.send_json(data)

            if path.startswith("/api/title/"):
                title_id = path.replace("/api/title/", "").strip()
                items = fetch_updates(lang_code=lang, force=force)

                title_items = [
                    item for item in items
                    if str(item.get("title_id")) == str(title_id)
                ]

                if not title_items:
                    return self.send_json({
                        "ok": False,
                        "message": "Title tidak ditemukan",
                        "title_id": title_id,
                    }, status=404)

                first = title_items[0]

                return self.send_json({
                    "ok": True,
                    "source": "MANGA Plus by SHUEISHA",
                    "lang": lang,
                    "title_id": first.get("title_id"),
                    "title": first.get("title"),
                    "author": first.get("author"),
                    "cover": first.get("cover"),
                    "title_url": first.get("title_url"),
                    "chapters": title_items,
                })

            if path == "/api/updates":
                items = fetch_updates(lang_code=lang, force=force)

                q = qs.get("q", [""])[0].strip().lower()

                if q:
                    items = [
                        item for item in items
                        if q in str(item.get("title") or "").lower()
                        or q in str(item.get("author") or "").lower()
                        or q in str(item.get("chapter") or "").lower()
                        or q in str(item.get("subtitle") or "").lower()
                    ]

                total = len(items)
                start = (page - 1) * limit
                end = start + limit
                paged_items = items[start:end]

                return self.send_json({
                    "ok": True,
                    "source": "MANGA Plus by SHUEISHA",
                    "lang": lang,
                    "page": page,
                    "limit": limit,
                    "total": total,
                    "total_pages": (total + limit - 1) // limit,
                    "count": len(paged_items),
                    "items": paged_items,
                })

            return self.send_json({
                "ok": False,
                "message": "Endpoint tidak ditemukan",
                "path": path,
            }, status=404)

        except Exception as e:
            return self.send_json({
                "ok": False,
                "error": str(e),
            }, status=500)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Server jalan di http://0.0.0.0:{port}")
    server.serve_forever()
