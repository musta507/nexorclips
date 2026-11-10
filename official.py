"""APIs oficiales de TikTok, Instagram y YouTube (cambio 87).

Cuando el clipper conecta su cuenta (OAuth), la web lee sus vídeos, sus cifras y los datos de su audiencia
(país, edad y sexo) directamente de cada red, sin Apify. Si no hay permisos o algo falla, se sigue usando Apify.

Todo lo que se devuelve de un vídeo tiene la misma forma que lo que da Apify (views.py → parse_item):
{url, vid, author, views, likes, comments, shares, posted_at, thumb, caption, avatar}.
Las funciones no tocan la base de datos: app.py guarda los tokens (cifrados) y los resultados.
"""
import os
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests

TIMEOUT = 20

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
TIKTOK_CLIENT_KEY = os.environ.get("TIKTOK_CLIENT_KEY", "")
TIKTOK_CLIENT_SECRET = os.environ.get("TIKTOK_CLIENT_SECRET", "")
INSTAGRAM_APP_ID = os.environ.get("INSTAGRAM_APP_ID", "")
INSTAGRAM_APP_SECRET = os.environ.get("INSTAGRAM_APP_SECRET", "")

IG_GRAPH = "https://graph.instagram.com/v21.0"

# Qué pide cada red (lo que sale en la pantalla de permisos)
SCOPES = {
    "youtube": ["https://www.googleapis.com/auth/youtube.readonly", "https://www.googleapis.com/auth/yt-analytics.readonly"],
    "tiktok": ["user.info.basic", "user.info.profile", "user.info.stats", "video.list"],
    "instagram": ["instagram_business_basic", "instagram_business_manage_insights"],
}
LABELS = {"youtube": "YouTube", "tiktok": "TikTok", "instagram": "Instagram"}
ENV_NEEDED = {"youtube": ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"],
              "tiktok": ["TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET"],
              "instagram": ["INSTAGRAM_APP_ID", "INSTAGRAM_APP_SECRET"]}


class ApiError(Exception):
    """Fallo de una API oficial. `expired` = hay que volver a conectar la cuenta."""
    def __init__(self, msg, expired=False):
        super().__init__(msg)
        self.expired = expired


def configured(platform):
    return {"youtube": bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET),
            "tiktok": bool(TIKTOK_CLIENT_KEY and TIKTOK_CLIENT_SECRET),
            "instagram": bool(INSTAGRAM_APP_ID and INSTAGRAM_APP_SECRET)}.get(platform, False)


def _ts(v):
    if v in (None, ""):
        return None
    try:
        if isinstance(v, (int, float)) or str(v).isdigit():
            return datetime.fromtimestamp(int(v), tz=timezone.utc).replace(tzinfo=None)
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00").replace("+0000", "+00:00"))
        return d.astimezone(timezone.utc).replace(tzinfo=None) if d.tzinfo else d
    except (ValueError, OverflowError, OSError):
        return None


def _int(v):
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _check(resp, what):
    if resp.status_code in (401, 403):
        raise ApiError(f"{what}: permiso caducado o retirado ({resp.status_code})", expired=True)
    if resp.status_code >= 400:
        raise ApiError(f"{what}: {resp.status_code} {resp.text[:200]}")
    try:
        return resp.json()
    except ValueError as exc:
        raise ApiError(f"{what}: respuesta no válida") from exc


# ---------------------------------------------------------------- 1) conectar la cuenta (OAuth)
def auth_url(platform, state, redirect_uri):
    """Adónde mandar al clipper para que dé permiso."""
    if platform == "youtube":
        return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
            "client_id": GOOGLE_CLIENT_ID, "redirect_uri": redirect_uri, "response_type": "code",
            "scope": " ".join(SCOPES["youtube"]), "access_type": "offline", "prompt": "consent",
            "include_granted_scopes": "true", "state": state})
    if platform == "tiktok":
        return "https://www.tiktok.com/v2/auth/authorize/?" + urlencode({
            "client_key": TIKTOK_CLIENT_KEY, "redirect_uri": redirect_uri, "response_type": "code",
            "scope": ",".join(SCOPES["tiktok"]), "state": state})
    if platform == "instagram":
        return "https://www.instagram.com/oauth/authorize?" + urlencode({
            "client_id": INSTAGRAM_APP_ID, "redirect_uri": redirect_uri, "response_type": "code",
            "scope": ",".join(SCOPES["instagram"]), "state": state, "enable_fb_login": "0", "force_authentication": "1"})
    raise ApiError("Red no soportada")


def exchange_code(platform, code, redirect_uri):
    """Cambia el código de la vuelta por los tokens. Devuelve {access, refresh, expires_in, refresh_expires_in}."""
    if platform == "youtube":
        d = _check(requests.post("https://oauth2.googleapis.com/token", data={
            "code": code, "client_id": GOOGLE_CLIENT_ID, "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": redirect_uri, "grant_type": "authorization_code"}, timeout=TIMEOUT), "Google")
        return {"access": d.get("access_token", ""), "refresh": d.get("refresh_token", ""),
                "expires_in": d.get("expires_in"), "scope": d.get("scope", "")}
    if platform == "tiktok":
        d = _check(requests.post("https://open.tiktokapis.com/v2/oauth/token/", data={
            "client_key": TIKTOK_CLIENT_KEY, "client_secret": TIKTOK_CLIENT_SECRET, "code": code,
            "grant_type": "authorization_code", "redirect_uri": redirect_uri},
            headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=TIMEOUT), "TikTok")
        if d.get("error"):
            raise ApiError(f"TikTok: {d.get('error_description') or d['error']}")
        return {"access": d.get("access_token", ""), "refresh": d.get("refresh_token", ""), "expires_in": d.get("expires_in"),
                "refresh_expires_in": d.get("refresh_expires_in"), "scope": d.get("scope", ""), "open_id": d.get("open_id", "")}
    if platform == "instagram":
        d = _check(requests.post("https://api.instagram.com/oauth/access_token", data={
            "client_id": INSTAGRAM_APP_ID, "client_secret": INSTAGRAM_APP_SECRET, "grant_type": "authorization_code",
            "redirect_uri": redirect_uri, "code": code}, timeout=TIMEOUT), "Instagram")
        short = d.get("access_token") or ((d.get("data") or [{}])[0].get("access_token"))
        long_ = _check(requests.get("https://graph.instagram.com/access_token", params={
            "grant_type": "ig_exchange_token", "client_secret": INSTAGRAM_APP_SECRET, "access_token": short},
            timeout=TIMEOUT), "Instagram")
        # Instagram no tiene «refresh token»: el token largo (60 días) se renueva a sí mismo
        return {"access": long_.get("access_token", ""), "refresh": "", "expires_in": long_.get("expires_in"), "scope": ""}
    raise ApiError("Red no soportada")


def refresh_token(platform, refresh, access=""):
    """Renueva el token antes de que caduque. Devuelve lo mismo que exchange_code."""
    if platform == "youtube":
        d = _check(requests.post("https://oauth2.googleapis.com/token", data={
            "client_id": GOOGLE_CLIENT_ID, "client_secret": GOOGLE_CLIENT_SECRET, "refresh_token": refresh,
            "grant_type": "refresh_token"}, timeout=TIMEOUT), "Google")
        return {"access": d.get("access_token", ""), "refresh": refresh, "expires_in": d.get("expires_in")}
    if platform == "tiktok":
        d = _check(requests.post("https://open.tiktokapis.com/v2/oauth/token/", data={
            "client_key": TIKTOK_CLIENT_KEY, "client_secret": TIKTOK_CLIENT_SECRET, "grant_type": "refresh_token",
            "refresh_token": refresh}, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=TIMEOUT), "TikTok")
        if d.get("error"):
            raise ApiError(f"TikTok: {d.get('error_description') or d['error']}", expired=True)
        return {"access": d.get("access_token", ""), "refresh": d.get("refresh_token", refresh),
                "expires_in": d.get("expires_in"), "refresh_expires_in": d.get("refresh_expires_in")}
    if platform == "instagram":
        d = _check(requests.get("https://graph.instagram.com/refresh_access_token", params={
            "grant_type": "ig_refresh_token", "access_token": access}, timeout=TIMEOUT), "Instagram")
        return {"access": d.get("access_token", access), "refresh": "", "expires_in": d.get("expires_in")}
    raise ApiError("Red no soportada")


# ---------------------------------------------------------------- 2) quién es (para comprobar que la cuenta es la suya)
def profile(platform, access):
    """{handle, name, avatar, id}: la cuenta que ha dado permiso."""
    if platform == "youtube":
        d = _check(requests.get("https://www.googleapis.com/youtube/v3/channels", params={"part": "snippet", "mine": "true"},
                                headers={"Authorization": f"Bearer {access}"}, timeout=TIMEOUT), "YouTube")
        it = (d.get("items") or [{}])[0]
        sn = it.get("snippet") or {}
        return {"id": it.get("id", ""), "handle": (sn.get("customUrl") or "").lstrip("@"), "name": sn.get("title", ""),
                "avatar": ((sn.get("thumbnails") or {}).get("default") or {}).get("url", "")}
    if platform == "tiktok":
        d = _check(requests.get("https://open.tiktokapis.com/v2/user/info/",
                                params={"fields": "open_id,username,display_name,avatar_url,follower_count"},
                                headers={"Authorization": f"Bearer {access}"}, timeout=TIMEOUT), "TikTok")
        u = (d.get("data") or {}).get("user") or {}
        return {"id": u.get("open_id", ""), "handle": u.get("username", ""), "name": u.get("display_name", ""),
                "avatar": u.get("avatar_url", ""), "followers": u.get("follower_count")}
    if platform == "instagram":
        d = _check(requests.get(f"{IG_GRAPH}/me", params={"fields": "user_id,username,name,profile_picture_url,followers_count",
                                                          "access_token": access}, timeout=TIMEOUT), "Instagram")
        return {"id": str(d.get("user_id") or d.get("id") or ""), "handle": d.get("username", ""), "name": d.get("name", ""),
                "avatar": d.get("profile_picture_url", ""), "followers": d.get("followers_count")}
    raise ApiError("Red no soportada")


# ---------------------------------------------------------------- 3) sus vídeos con cifras (sustituye a Apify)
def videos(platform, access, handle="", limit=30):
    """Últimos vídeos de la cuenta con vistas, me gusta, comentarios, compartidos, portada y texto."""
    if platform == "youtube":
        return _yt_videos(access, handle, limit)
    if platform == "tiktok":
        return _tt_videos(access, handle, limit)
    if platform == "instagram":
        return _ig_videos(access, handle, limit)
    raise ApiError("Red no soportada")


def _yt_videos(access, handle, limit):
    h = {"Authorization": f"Bearer {access}"}
    ch = _check(requests.get("https://www.googleapis.com/youtube/v3/channels",
                             params={"part": "contentDetails,snippet", "mine": "true"}, headers=h, timeout=TIMEOUT), "YouTube")
    items = ch.get("items") or []
    if not items:
        return []
    uploads = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    avatar = (((items[0].get("snippet") or {}).get("thumbnails") or {}).get("default") or {}).get("url", "")
    pl = _check(requests.get("https://www.googleapis.com/youtube/v3/playlistItems",
                             params={"part": "contentDetails", "playlistId": uploads, "maxResults": min(limit, 50)},
                             headers=h, timeout=TIMEOUT), "YouTube")
    ids = [x["contentDetails"]["videoId"] for x in pl.get("items") or [] if x.get("contentDetails")]
    if not ids:
        return []
    vs = _check(requests.get("https://www.googleapis.com/youtube/v3/videos",
                             params={"part": "snippet,statistics", "id": ",".join(ids)}, headers=h, timeout=TIMEOUT), "YouTube")
    out = []
    for v in vs.get("items") or []:
        st, sn = v.get("statistics") or {}, v.get("snippet") or {}
        thumbs = sn.get("thumbnails") or {}
        out.append({"url": f"https://www.youtube.com/shorts/{v['id']}", "vid": v["id"], "author": handle,
                    "views": _int(st.get("viewCount")), "likes": _int(st.get("likeCount")), "comments": _int(st.get("commentCount")),
                    "shares": None, "posted_at": _ts(sn.get("publishedAt")), "caption": sn.get("title", "")[:300],
                    "thumb": (thumbs.get("medium") or thumbs.get("default") or {}).get("url", ""), "avatar": avatar, "video_url": ""})
    return out


def _tt_videos(access, handle, limit):
    d = _check(requests.post("https://open.tiktokapis.com/v2/video/list/",
                             params={"fields": "id,title,video_description,cover_image_url,share_url,view_count,like_count,"
                                               "comment_count,share_count,create_time"},
                             json={"max_count": min(limit, 20)},
                             headers={"Authorization": f"Bearer {access}", "Content-Type": "application/json"},
                             timeout=TIMEOUT), "TikTok")
    if (d.get("error") or {}).get("code") not in (None, "ok"):
        raise ApiError(f"TikTok: {d['error'].get('message', d['error'].get('code'))}")
    out = []
    for v in (d.get("data") or {}).get("videos") or []:
        vid = str(v.get("id", ""))
        out.append({"url": v.get("share_url") or f"https://www.tiktok.com/@{handle}/video/{vid}", "vid": vid, "author": handle,
                    "views": _int(v.get("view_count")), "likes": _int(v.get("like_count")),
                    "comments": _int(v.get("comment_count")), "shares": _int(v.get("share_count")),
                    "posted_at": _ts(v.get("create_time")), "caption": (v.get("video_description") or v.get("title") or "")[:300],
                    "thumb": v.get("cover_image_url", ""), "avatar": "", "video_url": ""})
    return out


def _ig_videos(access, handle, limit):
    d = _check(requests.get(f"{IG_GRAPH}/me/media", params={
        "fields": "id,caption,media_type,media_product_type,permalink,thumbnail_url,media_url,timestamp,like_count,comments_count",
        "limit": min(limit, 50), "access_token": access}, timeout=TIMEOUT), "Instagram")
    out = []
    for m in d.get("data") or []:
        if m.get("media_type") != "VIDEO":
            continue
        views = shares = None
        try:                                     # vistas y compartidos vienen de las estadísticas de cada vídeo
            ins = _check(requests.get(f"{IG_GRAPH}/{m['id']}/insights", params={"metric": "views,shares",
                                                                                 "access_token": access}, timeout=TIMEOUT), "Instagram")
            vals = {x.get("name"): (x.get("values") or [{}])[0].get("value") for x in ins.get("data") or []}
            views, shares = _int(vals.get("views")), _int(vals.get("shares"))
        except ApiError as exc:
            if exc.expired:
                raise
        code = (m.get("permalink") or "").rstrip("/").split("/")[-1]
        out.append({"url": m.get("permalink", ""), "vid": code or m["id"], "author": handle, "views": views,
                    "likes": _int(m.get("like_count")), "comments": _int(m.get("comments_count")), "shares": shares,
                    "posted_at": _ts(m.get("timestamp")), "caption": (m.get("caption") or "")[:300],
                    "thumb": m.get("thumbnail_url", ""), "avatar": "", "video_url": m.get("media_url", "")})
    return out


# ---------------------------------------------------------------- 4) audiencia: país, edad y sexo
def audience(platform, access, video_ids=()):
    """Datos de audiencia que permite cada red.
    Devuelve {"account": {country, age, gender}, "videos": {vid: {country, age, gender}}} con porcentajes (0-100).
    - YouTube: por vídeo (País, edad y sexo) con YouTube Analytics.
    - Instagram: de la cuenta entera (Meta no da la audiencia de cada reel).
    - TikTok: la API normal no da audiencia; hace falta TikTok for Business (se añade cuando haya permisos)."""
    if platform == "youtube":
        return {"account": _yt_audience(access, None), "videos": {v: _yt_audience(access, v) for v in list(video_ids)[:20]}}
    if platform == "instagram":
        return {"account": _ig_audience(access), "videos": {}}
    return {"account": {}, "videos": {}}


def _pct(rows):
    tot = sum(v for _k, v in rows) or 0
    return {k: round(v * 100 / tot, 1) for k, v in sorted(rows, key=lambda kv: -kv[1])} if tot else {}


def _yt_audience(access, vid):
    h = {"Authorization": f"Bearer {access}"}
    base = {"ids": "channel==MINE", "startDate": "2020-01-01", "endDate": datetime.utcnow().strftime("%Y-%m-%d")}
    if vid:
        base["filters"] = f"video=={vid}"
    out = {}
    try:
        c = _check(requests.get("https://youtubeanalytics.googleapis.com/v2/reports",
                                params={**base, "dimensions": "country", "metrics": "views", "sort": "-views", "maxResults": 10},
                                headers=h, timeout=TIMEOUT), "YouTube Analytics")
        out["country"] = _pct([(r[0], r[1]) for r in c.get("rows") or []])
        g = _check(requests.get("https://youtubeanalytics.googleapis.com/v2/reports",
                                params={**base, "dimensions": "ageGroup,gender", "metrics": "viewerPercentage"},
                                headers=h, timeout=TIMEOUT), "YouTube Analytics")
        age, gender = {}, {}
        for a, s, pct in g.get("rows") or []:
            age[a.replace("age", "")] = age.get(a.replace("age", ""), 0) + pct
            gender[s] = gender.get(s, 0) + pct
        out["age"] = {k: round(v, 1) for k, v in sorted(age.items())}
        out["gender"] = {k: round(v, 1) for k, v in gender.items()}
    except ApiError as exc:
        if exc.expired:
            raise
    return out


def _ig_audience(access):
    out = {}
    for key, breakdown in (("country", "country"), ("age", "age"), ("gender", "gender")):
        try:
            d = _check(requests.get(f"{IG_GRAPH}/me/insights", params={
                "metric": "follower_demographics", "period": "lifetime", "metric_type": "total_value",
                "breakdown": breakdown, "access_token": access}, timeout=TIMEOUT), "Instagram")
            res = (((d.get("data") or [{}])[0].get("total_value") or {}).get("breakdowns") or [{}])[0].get("results") or []
            out[key] = _pct([((r.get("dimension_values") or ["?"])[0], r.get("value") or 0) for r in res])
        except ApiError as exc:
            if exc.expired:
                raise
    return out
