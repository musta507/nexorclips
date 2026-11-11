"""Lectura de vistas, autor y likes de los clips mediante Apify (TikTok, Instagram y YouTube)."""
import json
import logging
import os
import re
from datetime import datetime

import requests

log = logging.getLogger("clipprofit.views")

APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "")

# Actores de Apify que se usan para cada red (se pueden cambiar desde Railway sin tocar código)
ACTORS = {
    "tiktok": os.environ.get("APIFY_ACTOR_TIKTOK", "clockworks~tiktok-scraper"),
    "instagram": os.environ.get("APIFY_ACTOR_INSTAGRAM", "apify~instagram-scraper"),
    "youtube": os.environ.get("APIFY_ACTOR_YOUTUBE", "streamers~youtube-scraper"),
    "facebook": os.environ.get("APIFY_ACTOR_FACEBOOK", "apify~facebook-posts-scraper"),
}

SHORT_TIKTOK = re.compile(r"^https?://(?:vm|vt)\.tiktok\.com/|^https?://(?:www\.)?tiktok\.com/t/"
                          r"|^https?://fb\.watch/|^https?://(?:www\.|m\.)?facebook\.com/share/", re.I)


# ---------------------------------------------------------------- enlaces
def embed_url(platform, url):
    """Enlace para ver el vídeo dentro de la web (reproductor oficial de cada red)."""
    vid = video_id(url)
    if not vid:
        return ""
    if platform == "tiktok":
        return f"https://www.tiktok.com/embed/v2/{vid}"
    if platform == "instagram":
        return f"https://www.instagram.com/reel/{vid}/embed/"
    if platform == "youtube":
        return f"https://www.youtube-nocookie.com/embed/{vid}"
    if platform == "facebook":
        from urllib.parse import quote
        return f"https://www.facebook.com/plugins/video.php?href={quote(url, safe='')}&show_text=false"
    return ""


def detect_platform(url):
    u = (url or "").lower()
    if "tiktok.com" in u:
        return "tiktok"
    if "instagram.com" in u:
        return "instagram"
    if "youtube.com" in u or "youtu.be" in u:
        return "youtube"
    if "facebook.com" in u or "fb.watch" in u:
        return "facebook"
    return None


def video_id(url):
    """Saca el identificador del vídeo a partir del enlace."""
    url = url or ""
    patterns = [
        r"tiktok\.com/.*?/(?:video|photo)/(\d+)",
        r"instagram\.com/(?:[^/]+/)?(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)",
        r"youtube\.com/shorts/([A-Za-z0-9_-]{6,})",
        r"facebook\.com/(?:reel|reels)/(\d+)",
        r"facebook\.com/.*?/videos/(?:[^/]+/)?(\d+)",
        r"facebook\.com/watch/?\?v=(\d+)",
        r"fb\.watch/([A-Za-z0-9_-]+)",
        r"[?&]v=([A-Za-z0-9_-]{6,})",
        r"youtu\.be/([A-Za-z0-9_-]{6,})",
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None


def tiktok_handle_from_url(url):
    m = re.search(r"tiktok\.com/@([A-Za-z0-9._-]+)/", url or "")
    return clean_handle(m.group(1)) if m else ""


def clean_handle(h):
    h = (h or "").strip().lower()
    h = re.sub(r"^https?://(www\.|m\.)?(tiktok|instagram|youtube|facebook)\.com/", "", h)
    h = h.lstrip("@").strip("/ ")
    return re.sub(r"[^a-z0-9._-]", "", h)[:60]


def normalize_url(url, resolve=True):
    """Limpia el enlace: quita espacios, añade https y resuelve los enlaces cortos de TikTok."""
    url = (url or "").strip().split()[0] if (url or "").strip() else ""
    if url and not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    if resolve and SHORT_TIKTOK.search(url):
        try:
            r = requests.get(url, allow_redirects=True, timeout=8,
                             headers={"User-Agent": "Mozilla/5.0 (compatible; ClipProfit/1.0)"})
            if video_id(r.url):
                url = r.url
        except requests.RequestException as exc:
            log.warning("No se pudo resolver el enlace corto %s: %s", url, exc)
    # Para TikTok guardamos una versión limpia sin parámetros de seguimiento
    if detect_platform(url) == "tiktok" and video_id(url):
        handle = tiktok_handle_from_url(url)
        if handle:
            url = f"https://www.tiktok.com/@{handle}/video/{video_id(url)}"
    return url[:500]


# ---------------------------------------------------------------- Apify
def _build_input(platform, urls):
    if platform == "tiktok":
        return {"postURLs": urls, "resultsPerPage": 1, "shouldDownloadVideos": False,
                "shouldDownloadCovers": False, "shouldDownloadSubtitles": False}
    if platform == "instagram":
        return {"directUrls": urls, "resultsType": "posts", "resultsLimit": 1, "addParentData": False}
    if platform == "facebook":
        return {"startUrls": [{"url": u} for u in urls], "resultsLimit": 1}
    return {"startUrls": [{"url": u} for u in urls], "maxResults": 1, "maxResultsShorts": 1}


def _to_int(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip().replace(",", "").replace(" ", "")
    try:
        return int(float(s))
    except ValueError:
        return None


def _first(item, keys):
    for k in keys:
        cur = item
        for part in k.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
        if cur not in (None, ""):
            return cur
    return None


VIEW_KEYS = {
    "tiktok": ["playCount", "plays", "viewCount", "stats.playCount"],
    "instagram": ["videoPlayCount", "videoViewCount", "playCount", "viewCount"],
    "youtube": ["viewCount", "views"],
    "facebook": ["viewsCount", "videoViewCount", "playCount", "views", "video_view_count", "playsCount"],
}
LIKE_KEYS = {
    "tiktok": ["diggCount", "likes", "stats.diggCount"],
    "instagram": ["likesCount", "likes"],
    "youtube": ["likes", "likeCount"],
    "facebook": ["likes", "likesCount", "reactionsCount", "reactions"],
}
COMMENT_KEYS = {
    "tiktok": ["commentCount", "comments", "stats.commentCount"],
    "instagram": ["commentsCount", "comments"],
    "youtube": ["commentsCount", "commentCount", "comments"],
    "facebook": ["comments", "commentsCount"],
}
SHARE_KEYS = {
    "tiktok": ["shareCount", "shares", "stats.shareCount"],
    "instagram": ["sharesCount", "reshareCount"],
    "youtube": ["sharesCount"],
    "facebook": ["shares", "sharesCount"],
}
AUTHOR_KEYS = {
    "tiktok": ["authorMeta.name", "author.uniqueId", "authorMeta.uniqueId", "author"],
    "instagram": ["ownerUsername", "owner.username"],
    "youtube": ["channelUsername", "channelUrl", "channelName"],
    "facebook": ["user.profileUrl", "pageName", "user.name", "ownerUsername", "author.name"],
}


VIDEO_URL_KEYS = {        # enlace para descargar el vídeo (para guardar una copia y revisarlo)
    "tiktok": ["mediaUrls", "videoMeta.downloadAddr"],
    "instagram": ["videoUrl", "video_url"],
}
THUMB_KEYS = {            # portada del vídeo (enlace de la red, puede caducar: se renueva en cada lectura)
    "tiktok": ["videoMeta.coverUrl", "videoMeta.originalCoverUrl", "covers.default", "cover"],
    "instagram": ["displayUrl", "thumbnailUrl", "images.0"],
    "youtube": ["thumbnailUrl", "thumbnail"],
}
AVATAR_KEYS = {           # foto de perfil de la cuenta
    "tiktok": ["authorMeta.avatar", "author.avatarThumb", "authorMeta.originalAvatarUrl"],
    "instagram": ["ownerProfilePicUrl", "owner.profilePicUrl", "profilePicUrl"],
    "youtube": ["channelAvatarUrl", "channelThumbnail", "authorThumbnail"],
}
CAPTION_KEYS = {          # texto del vídeo
    "tiktok": ["text", "desc"],
    "instagram": ["caption"],
    "youtube": ["title", "text"],
}
DATE_KEYS = {
    "tiktok": ["createTimeISO", "createTime", "create_time"],
    "instagram": ["timestamp", "takenAtTimestamp", "taken_at_timestamp"],
    "youtube": ["date", "uploadDate", "publishedAt"],
    "facebook": ["time", "timestamp", "date", "creation_time"],
}


def _to_date(v):
    """Fecha de publicación (UTC, sin zona) a partir de un número de segundos o un texto ISO."""
    from datetime import datetime, timezone
    if v is None or isinstance(v, bool):
        return None
    try:
        if isinstance(v, (int, float)) or (isinstance(v, str) and v.strip().isdigit()):
            n = float(v)
            n = n / 1000 if n > 1e12 else n
            return datetime.fromtimestamp(n, tz=timezone.utc).replace(tzinfo=None) if n > 1e9 else None
        d = datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
        return d.astimezone(timezone.utc).replace(tzinfo=None) if d.tzinfo else d
    except (ValueError, OverflowError, OSError):
        return None


def _ids_from_item(item):
    ids = set()
    for k in ("id", "shortCode", "shortcode", "videoId"):
        if item.get(k):
            ids.add(str(item[k]))
    for k in ("webVideoUrl", "url", "inputUrl", "postUrl", "submittedVideoUrl"):
        if item.get(k):
            vid = video_id(str(item[k]))
            if vid:
                ids.add(vid)
    return ids


def parse_item(platform, item):
    views = _to_int(_first(item, VIEW_KEYS[platform]))
    likes = _to_int(_first(item, LIKE_KEYS[platform]))
    comments = _to_int(_first(item, COMMENT_KEYS[platform]))
    shares = _to_int(_first(item, SHARE_KEYS[platform]))
    author = _first(item, AUTHOR_KEYS[platform])
    author = clean_handle(author) if isinstance(author, str) else ""
    posted = _to_date(_first(item, DATE_KEYS[platform]))
    video = _first(item, VIDEO_URL_KEYS.get(platform, []))
    if isinstance(video, list):
        video = video[0] if video else None
    thumb = _first(item, THUMB_KEYS.get(platform, []))
    if isinstance(thumb, list):
        thumb = thumb[0] if thumb else None
    caption = _first(item, CAPTION_KEYS.get(platform, []))
    avatar = _first(item, AVATAR_KEYS.get(platform, []))
    return {"views": views, "likes": likes, "comments": comments, "shares": shares, "author": author,
            "posted_at": posted, "video_url": video if isinstance(video, str) and video.startswith("https://") else "",
            "thumb": thumb if isinstance(thumb, str) and thumb.startswith("https://") else "",
            "caption": " ".join(caption.split())[:300] if isinstance(caption, str) else "",
            "avatar": avatar if isinstance(avatar, str) and avatar.startswith("https://") else ""}


APIFY_HOOK = None       # la web lo pone para apuntar cada llamada (qué, cuántos enlaces, cuántos resultados)


def _apify(actor, body, kind, platform, n, timeout=300):
    resp = requests.post(f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items",
                         params={"token": APIFY_TOKEN, "timeout": timeout - 20}, json=body, timeout=timeout)
    items = None
    if resp.status_code < 400:
        try:
            items = resp.json() or []
        except ValueError:
            items = []
    if APIFY_HOOK:
        try:
            APIFY_HOOK(kind, platform, n, len(items) if isinstance(items, list) else 0, resp.status_code)
        except Exception:  # noqa: BLE001
            pass
    if resp.status_code >= 400:
        raise RuntimeError(f"Apify respondió {resp.status_code}: {resp.text[:200]}")
    return items if isinstance(items, list) else []


def fetch_items(platform, urls):
    """Llama a Apify y devuelve los datos tal cual llegan (lista de diccionarios)."""
    if not APIFY_TOKEN:
        raise RuntimeError("Falta APIFY_TOKEN en las variables de entorno.")
    if not urls:
        return []
    return _apify(ACTORS[platform], _build_input(platform, urls), "vistas", platform, len(urls))


def info_by_id(platform, items):
    """{video_id: {"views", "likes", "author"}} a partir de lo que devuelve Apify."""
    result = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        info = parse_item(platform, item)
        if info["views"] is None:
            continue
        for vid in _ids_from_item(item):
            prev = result.get(vid)
            if not prev or info["views"] > prev["views"]:
                result[vid] = info
    return result


def fetch_download_urls(platform, urls):
    """{video_id: enlace de descarga}. Solo TikTok necesita una llamada aparte (con la descarga activada)."""
    if not APIFY_TOKEN or not urls:
        return {}
    body = _build_input(platform, urls)
    if platform == "tiktok":
        body["shouldDownloadVideos"] = True
    out = {}
    for item in _apify(ACTORS[platform], body, "descarga", platform, len(urls)):
        if isinstance(item, dict):
            url = parse_item(platform, item).get("video_url")
            if platform == "tiktok":            # solo vale la copia que guarda Apify; el enlace de TikTok da 403
                media = item.get("mediaUrls") or []
                url = next((m for m in media if isinstance(m, str) and m.startswith("https://")), "")
            if url:
                for vid in _ids_from_item(item):
                    out[vid] = url
    return out


def download_video(url):
    """Descarga un vídeo. Los que guarda Apify se piden con el token (si no, pueden dar 403)."""
    headers = {"User-Agent": "Mozilla/5.0"}
    if "api.apify.com" in url and APIFY_TOKEN:
        headers["Authorization"] = f"Bearer {APIFY_TOKEN}"
    elif "tiktok" in url:
        headers["Referer"] = "https://www.tiktok.com/"
    return requests.get(url, timeout=90, headers=headers)


def fetch_views(platform, urls):
    """Devuelve {video_id: {"views", "likes", "author"}} para enlaces de la misma red."""
    return info_by_id(platform, fetch_items(platform, urls))


def probe(url):
    """Prueba un solo enlace y devuelve lo que entiende la web (para el botón de admin)."""
    url = normalize_url(url)
    platform, vid = detect_platform(url), video_id(url)
    out = {"url": url, "platform": platform, "video_id": vid, "items": 0, "views": None, "likes": None,
           "author": "", "keys": [], "sample": "", "error": "", "actor": ACTORS.get(platform, "")}
    if not platform or not vid:
        out["error"] = "La web no reconoce este enlace como un vídeo de TikTok, Instagram o YouTube."
        return out
    try:
        items = fetch_items(platform, [url])
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
        return out
    out["items"] = len(items)
    if items and isinstance(items[0], dict):
        out["keys"] = sorted(items[0].keys())
        out["sample"] = json.dumps(items[0], ensure_ascii=False, indent=2)[:3000]
    found = info_by_id(platform, items).get(vid)
    if found:
        out.update(views=found["views"], likes=found["likes"], author=found["author"])
    else:
        out["error"] = ("Apify respondió, pero la web no encontró las vistas de este vídeo. "
                        "Pásale a Claude el resultado de abajo para ajustarlo.") if items else \
                       "Apify no devolvió ningún dato para este enlace (¿vídeo privado o borrado?)."
    return out


# ---------------------------------------------------------------- bio del perfil (verificar que la cuenta es suya)
BIO_KEYS = {
    "tiktok": ["authorMeta.signature", "author.signature", "signature", "bio"],
    "instagram": ["biography", "bio"],
    "youtube": ["channelDescription", "aboutChannelInfo.channelDescription", "description"],
    "facebook": ["intro", "about", "info", "description"],
}


def fetch_bio(platform, handle):
    """Lee el texto de la bio del perfil. Devuelve el texto, o None si no se pudo leer."""
    if not APIFY_TOKEN:
        raise RuntimeError("Falta APIFY_TOKEN en las variables de entorno.")
    if platform == "tiktok":
        actor, body = ACTORS["tiktok"], {"profiles": [handle], "resultsPerPage": 1, "shouldDownloadVideos": False,
                                         "shouldDownloadCovers": False, "shouldDownloadSubtitles": False}
    elif platform == "instagram":
        actor, body = ACTORS["instagram"], {"directUrls": [f"https://www.instagram.com/{handle}/"],
                                            "resultsType": "details", "resultsLimit": 1}
    elif platform == "facebook":
        actor, body = (os.environ.get("APIFY_ACTOR_FACEBOOK_PAGE", "apify~facebook-pages-scraper"),
                       {"startUrls": [{"url": f"https://www.facebook.com/{handle}"}]})
    else:
        actor, body = ACTORS["youtube"], {"startUrls": [{"url": f"https://www.youtube.com/@{handle}"}],
                                          "maxResults": 1, "maxResultsShorts": 0}
    for item in _apify(actor, body, "bio", platform, 1, timeout=120):
        bio = _first(item, BIO_KEYS[platform]) if isinstance(item, dict) else None
        if isinstance(bio, str):
            return bio
    return None


# ---------------------------------------------------------------- perfiles: vídeos nuevos de una cuenta
def _profile_input(platform, handles, per):
    if platform == "tiktok":
        return {"profiles": handles, "resultsPerPage": per, "shouldDownloadVideos": False,
                "shouldDownloadCovers": False, "shouldDownloadSubtitles": False}
    if platform == "instagram":
        return {"directUrls": [f"https://www.instagram.com/{h}/" for h in handles], "resultsType": "posts",
                "resultsLimit": per, "addParentData": False}
    return {"startUrls": [{"url": f"https://www.youtube.com/@{h}/shorts"} for h in handles], "maxResults": 0,
            "maxResultsShorts": per}


PROFILE_URL_KEYS = {"tiktok": ["webVideoUrl", "url"], "instagram": ["url", "inputUrl"], "youtube": ["url"]}


def fetch_profile_videos(platform, handles, per=12):
    """Los últimos vídeos publicados por esas cuentas, con sus cifras: [{url, vid, author, views, ..., posted_at}]."""
    if platform not in ("tiktok", "instagram", "youtube"):
        return []
    if not APIFY_TOKEN:
        raise RuntimeError("Falta APIFY_TOKEN en las variables de entorno.")
    if not handles:
        return []
    data = _apify(ACTORS[platform], _profile_input(platform, handles, per), "perfiles", platform, len(handles))
    out = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        url = _first(item, PROFILE_URL_KEYS[platform])
        if not isinstance(url, str) or not url.startswith("http"):
            continue
        vid = video_id(url)
        if not vid:
            continue
        info = parse_item(platform, item)
        info.update({"url": url, "vid": vid})
        out.append(info)
    return out


# ---------------------------------------------------------------- yt-dlp (gratis): perfiles de TikTok/YouTube y descargas
YTDLP_PLATFORMS = ("tiktok", "youtube")


def ytdlp_available():
    try:
        import yt_dlp  # noqa: F401
        return True
    except ImportError:
        return False


def _ydl(opts):
    import yt_dlp
    base = {"quiet": True, "no_warnings": True, "skip_download": True, "socket_timeout": 25, "retries": 2,
            "extractor_retries": 2, "noplaylist": False, "ignoreerrors": False}
    base.update(opts)
    return yt_dlp.YoutubeDL(base)


def tiktok_id_time(vid):
    """Los ID de TikTok llevan dentro el momento de publicación (los 32 bits altos son segundos Unix)."""
    try:
        ts = int(vid) >> 32
    except (TypeError, ValueError):
        return None
    if 1_450_000_000 < ts < 4_000_000_000:
        return datetime.utcfromtimestamp(ts)
    return None


def _ts(e):
    if e.get("timestamp"):
        try:
            return datetime.utcfromtimestamp(int(e["timestamp"]))
        except (TypeError, ValueError, OSError):
            return None
    d = e.get("upload_date")
    if isinstance(d, str) and len(d) == 8:
        try:
            return datetime.strptime(d, "%Y%m%d")
        except ValueError:
            return None
    return None


def ytdlp_profile_videos(platform, handle, per=12, known=()):
    """Últimos vídeos de una cuenta con yt-dlp: [{url, vid, author, posted_at, views: None}].
    Las cifras no se toman de aquí (las lee Apify). `known`: IDs que ya tenemos (no hace falta su fecha)."""
    if platform not in YTDLP_PLATFORMS:
        raise RuntimeError("yt-dlp solo se usa para TikTok y YouTube")
    h = clean_handle(handle)
    url = f"https://www.tiktok.com/@{h}" if platform == "tiktok" else f"https://www.youtube.com/@{h}/shorts"
    out = []
    with _ydl({"extract_flat": "in_playlist", "playlistend": per}) as y:
        info = y.extract_info(url, download=False) or {}
        entries = [e for e in (info.get("entries") or []) if isinstance(e, dict) and e.get("id")][:per]
        for e in entries:
            vid = str(e["id"])
            if platform == "tiktok":
                link = f"https://www.tiktok.com/@{h}/video/{vid}"
                posted = _ts(e) or tiktok_id_time(vid)
            else:
                link = f"https://www.youtube.com/shorts/{vid}"
                posted = _ts(e)
                if not posted and vid not in known:     # vídeo nuevo: hace falta su fecha
                    full = y.extract_info(link, download=False) or {}
                    posted = _ts(full)
                    if not posted:
                        raise RuntimeError(f"yt-dlp no dio la fecha del vídeo {vid}")
            out.append({"url": link, "vid": vid, "author": h, "posted_at": posted, "views": None})
    return out


def ytdlp_download(url, max_mb=300):
    """Descarga el vídeo con yt-dlp y devuelve sus bytes."""
    import glob
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        with _ydl({"skip_download": False, "outtmpl": f"{d}/v.%(ext)s", "max_filesize": max_mb * 1024 * 1024,
                   "format": "best[ext=mp4][height<=1080]/best[ext=mp4]/best"}) as y:
            y.download([url])
        files = glob.glob(f"{d}/v.*")
        if not files:
            raise RuntimeError("yt-dlp no descargó nada")
        with open(files[0], "rb") as f:
            return f.read()
