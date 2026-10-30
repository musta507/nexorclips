"""Lectura de vistas, autor y likes de los clips mediante Apify (TikTok, Instagram y YouTube)."""
import json
import logging
import os
import re

import requests

log = logging.getLogger("clipprofit.views")

APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "")

# Actores de Apify que se usan para cada red (se pueden cambiar desde Railway sin tocar código)
ACTORS = {
    "tiktok": os.environ.get("APIFY_ACTOR_TIKTOK", "clockworks~tiktok-scraper"),
    "instagram": os.environ.get("APIFY_ACTOR_INSTAGRAM", "apify~instagram-scraper"),
    "youtube": os.environ.get("APIFY_ACTOR_YOUTUBE", "streamers~youtube-scraper"),
}

SHORT_TIKTOK = re.compile(r"^https?://(?:vm|vt)\.tiktok\.com/|^https?://(?:www\.)?tiktok\.com/t/", re.I)


# ---------------------------------------------------------------- enlaces
def detect_platform(url):
    u = (url or "").lower()
    if "tiktok.com" in u:
        return "tiktok"
    if "instagram.com" in u:
        return "instagram"
    if "youtube.com" in u or "youtu.be" in u:
        return "youtube"
    return None


def video_id(url):
    """Saca el identificador del vídeo a partir del enlace."""
    url = url or ""
    patterns = [
        r"tiktok\.com/.*?/(?:video|photo)/(\d+)",
        r"instagram\.com/(?:[^/]+/)?(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)",
        r"youtube\.com/shorts/([A-Za-z0-9_-]{6,})",
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
    h = re.sub(r"^https?://(www\.)?(tiktok|instagram|youtube)\.com/", "", h)
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
}
LIKE_KEYS = {
    "tiktok": ["diggCount", "likes", "stats.diggCount"],
    "instagram": ["likesCount", "likes"],
    "youtube": ["likes", "likeCount"],
}
COMMENT_KEYS = {
    "tiktok": ["commentCount", "comments", "stats.commentCount"],
    "instagram": ["commentsCount", "comments"],
    "youtube": ["commentsCount", "commentCount", "comments"],
}
SHARE_KEYS = {
    "tiktok": ["shareCount", "shares", "stats.shareCount"],
    "instagram": ["sharesCount", "reshareCount"],
    "youtube": ["sharesCount"],
}
AUTHOR_KEYS = {
    "tiktok": ["authorMeta.name", "author.uniqueId", "authorMeta.uniqueId", "author"],
    "instagram": ["ownerUsername", "owner.username"],
    "youtube": ["channelUsername", "channelUrl", "channelName"],
}


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
    return {"views": views, "likes": likes, "comments": comments, "shares": shares, "author": author}


def fetch_items(platform, urls):
    """Llama a Apify y devuelve los datos tal cual llegan (lista de diccionarios)."""
    if not APIFY_TOKEN:
        raise RuntimeError("Falta APIFY_TOKEN en las variables de entorno.")
    if not urls:
        return []
    actor = ACTORS[platform]
    endpoint = f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"
    resp = requests.post(endpoint, params={"token": APIFY_TOKEN, "timeout": 280},
                         json=_build_input(platform, urls), timeout=300)
    if resp.status_code >= 400:
        raise RuntimeError(f"Apify respondió {resp.status_code}: {resp.text[:200]}")
    data = resp.json() or []
    return data if isinstance(data, list) else []


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
    else:
        actor, body = ACTORS["youtube"], {"startUrls": [{"url": f"https://www.youtube.com/@{handle}"}],
                                          "maxResults": 1, "maxResultsShorts": 0}
    resp = requests.post(f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items",
                         params={"token": APIFY_TOKEN, "timeout": 110}, json=body, timeout=120)
    if resp.status_code >= 400:
        raise RuntimeError(f"Apify respondió {resp.status_code}: {resp.text[:200]}")
    data = resp.json() or []
    for item in data if isinstance(data, list) else []:
        bio = _first(item, BIO_KEYS[platform]) if isinstance(item, dict) else None
        if isinstance(bio, str):
            return bio
    return None
