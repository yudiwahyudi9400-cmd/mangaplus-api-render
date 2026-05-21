from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timezone
import json
import time
import os

from mangaplus import MangaPlus
from mangaplus.constants import Language, Viewer

CACHE_TTL = 600
CACHE = {
    "time": 0,
    "items": []
}

def get_lang(code):
    code = (code or "en").lower()

    mapping = {
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

    name = mapping.get(code, "ENGLISH")
    return getattr(Language, name, Language.ENGLISH)

def get_val(data, keys, default=None):
    if not isinstance(data, dict):
        return default

    for key in keys:
        if key in data and data[key] not in [None, ""]:
            return data[key]

    return default

def walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from walk(item)

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
            "backgroundImageUrl"
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
            "chapterTitle"
        ])

        subtitle = val(obj, [
            "subTitle",
            "subtitle",
            "chapterSubTitle",
            "chapterSubtitle"
        ])

        thumbnail = val(obj, [
            "thumbnailUrl",
            "thumbnailURL",
            "chapterImageUrl"
        ])

        start_ts = val(obj, [
            "startTimeStamp",
            "startTimestamp",
            "releaseStartTimeStamp",
            "updateTimeStamp"
        ])

        end_ts = val(obj, [
            "endTimeStamp",
            "endTimestamp"
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

            # Kalau object ini sendiri adalah data title
            own_title = title_info(node)
            if own_title and own_title.get("title_id"):
                local_title = own_title

            # Kalau title ada sebagai child/sibling di object yang sama
            for key in [
                "title",
                "titleView",
                "titleDetail",
                "titleDetailView",
                "titleInfo",
                "titleData",
                "mangaTitle"
            ]:
                child = node.get(key)
                if isinstance(child, dict):
                    child_title = title_info(child)
                    if child_title:
                        local_title = child_title

            ch = chapter_info(node, local_title)
            if ch:
                key = f"{ch.get('title_id')}:{ch.get('chapter_id')}"
                if key not in seen:
                    seen.add(key)
                    items.append(ch)

            for value in node.values():
                scan(value, local_title)

        elif isinstance(node, list):
            for item in node:
                scan(item, ctx_title)

    scan(raw)

    # Prioritaskan yang punya title, biar data kosong tidak muncul di atas
    items.sort(
        key=lambda x: (
            0 if x.get("title") else 1,
            x.get("published_at") or "",
            str(x.get("chapter_id") or "")
        ),
        reverse=False
    )

    return items

def fetch_updates(lang_code="en", force=False):
    now = time.time()

    if not force and CACHE["items"] and now - CACHE["time"] < CACHE_TTL:
        return CACHE["items"]

    selected_lang = get_lang(lang_code)

    client = MangaPlus(
        lang=selected_lang,
        clang=[selected_lang],
        viewer=Viewer.VERTICAL
    )

    # Versi yang berhasil dari test kamu
    client.APP_VERSION = 237

    # Wajib register device dulu
    import uuid
    device_id = str(uuid.uuid4())
    client.register(device_id=device_id)

    # getUpdates/home_v4 error, pakai home_v6
    raw = client.getHome()

    items = normalize_updates(raw)

    CACHE["time"] = now
    CACHE["items"] = items

    return items

class Handler(BaseHTTPRequestHandler):
    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")

        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        lang = qs.get("lang", ["en"])[0]
        limit = int(qs.get("limit", ["30"])[0])
        force = qs.get("force", ["0"])[0] in ["1", "true", "yes"]

        try:
            if path == "/":
                return self.send_json({
                    "ok": True,
                    "message": "MangaPlus API aktif",
                    "endpoints": [
                        "/api/updates?lang=en&limit=30",
                        "/api/updates?lang=id&limit=30"
                    ]
                })

            if path == "/health":
                return self.send_json({
                    "ok": True,
                    "status": "healthy",
                    "service": "MangaPlus Updates API"
                })

            if path == "/api/cache/clear":
                CACHE["time"] = 0
                CACHE["items"] = []
                return self.send_json({
                    "ok": True,
                    "message": "Cache berhasil dibersihkan"
                })

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
                        "title_id": title_id
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
                    "chapters": title_items
                })

            if path == "/api/updates":
                items = fetch_updates(lang_code=lang, force=force)

                q = qs.get("q", [""])[0].strip().lower()
                page = int(qs.get("page", ["1"])[0])

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
                "message": "Endpoint tidak ditemukan"
            }, status=404)

        except Exception as e:
            return self.send_json({
                "ok": False,
                "error": str(e)
            }, status=500)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Server jalan di http://0.0.0.0:{port}")
    server.serve_forever()

