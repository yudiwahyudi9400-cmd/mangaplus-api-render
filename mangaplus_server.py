from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timezone
import json
import time
import os
import uuid

from mangaplus import MangaPlus
from mangaplus.constants import Language, Viewer, Quality

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

            if path == "/api/cache/clear":
                CACHE["updates"] = {}
                CHAPTER_CACHE.clear()
                return self.send_json({
                    "ok": True,
                    "message": "Cache berhasil dibersihkan",
                })


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
