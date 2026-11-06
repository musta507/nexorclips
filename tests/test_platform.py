"""Pruebas automáticas de ClipProfit Hispano. Ejecutar con:  python -m pytest -q"""
import io
import os
import sys
import tempfile
from datetime import datetime, timedelta

DB = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ.update(DATABASE_URL=os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{DB}", ADMIN_EMAIL="admin@test.com", APIFY_TOKEN="test",
                  CRON_SECRET="cron-key", DEV_LOGIN="1", GOOGLE_CLIENT_ID="cid", GOOGLE_CLIENT_SECRET="sec",
                  SUPPORT_EMAIL="soporte@test.com")
os.environ.pop("RAILWAY_ENVIRONMENT", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import app as A  # noqa: E402
import views as V  # noqa: E402


# ---------------------------------------------------------------- utilidades
@pytest.fixture(autouse=True)
def clean_db():
    with A.app.app_context():
        A.db.drop_all()
        A.ensure_schema()
    AUTO_ACCEPT.clear()
    yield


def client_for(email, verify=True, **handles):
    c = A.app.test_client()
    with c.session_transaction() as s:
        s["_csrf"] = "t"
    c.post("/login/dev", data={"email": email, "_csrf": "t"})
    with c.session_transaction() as s:      # el login renueva el código; fijamos uno conocido para las pruebas
        s["_csrf"] = "t"
    if handles:
        data = {"_csrf": "t", "section": "redes"}
        data.update({f"handles_{k}": v for k, v in handles.items()})
        c.post("/cuenta", data=data)
    if verify:                                       # en la mayoría de pruebas, el clipper ya está aceptado en las campañas
        AUTO_ACCEPT.add(email)
    return c


AUTO_ACCEPT = set()


def _auto_accept():
    """Acepta en todas las campañas las cuentas de los clippers de prueba marcados (simula al admin)."""
    with A.app.app_context():
        for u in A.User.query.filter(A.User.email.in_(AUTO_ACCEPT)).all():
            for camp in A.Campaign.query.all():
                for p in A.PLATFORMS:
                    for h in u.handles(p):
                        if not A.CampaignAccount.query.filter_by(campaign_id=camp.id, platform=p, handle=h).first():
                            A.db.session.add(A.CampaignAccount(user_id=u.id, campaign_id=camp.id, platform=p,
                                                               handle=h, status="aceptada", bio_verified=True))
                if not A.CampaignMember.query.filter_by(user_id=u.id, campaign_id=camp.id).first():
                    A.db.session.add(A.CampaignMember(user_id=u.id, campaign_id=camp.id, status="aceptada"))
        A.db.session.commit()


WALLET = "TQn9Y2khEsLJW1ChVWFMSMeRDow5KcbLSE"


def post(c, path, **data):
    data.setdefault("_csrf", "t")
    if "url" in data and AUTO_ACCEPT:
        _auto_accept()
    return c.post(path, data=data, follow_redirects=True)


def flashes(resp):
    import re
    return re.findall(r'class="flash (\w+)"[^>]*>([^<]+)', resp.get_data(as_text=True))


def make_campaign(**kw):
    defaults = dict(song="Canción", artist="Artista", rate=200, budget=300, min_views=0, max_per_video=None,
                    platforms="tiktok,instagram,youtube", status="activa", spent=0.0, report_token="tok-" + os.urandom(4).hex())
    defaults.update(kw)
    with A.app.app_context():
        c = A.Campaign(**defaults)
        A.db.session.add(c)
        A.db.session.commit()
        return c.id


def tt(handle, n):
    return f"https://www.tiktok.com/@{handle}/video/{7300000000000000000 + n}"


def get(model, pk):
    return A.db.session.get(model, pk)


# ---------------------------------------------------------------- acceso y seguridad
def test_anon_sees_login_and_google_redirect():
    c = A.app.test_client()
    assert "Empezar a clipear" in c.get("/").get_data(as_text=True)
    assert "Continuar con Google" in c.get("/login").get_data(as_text=True)
    r = c.get("/login/google")
    assert r.status_code == 302 and "accounts.google.com" in r.location


def test_google_callback_creates_admin(monkeypatch):
    c = A.app.test_client()
    r = c.get("/login/google")
    import re
    state = re.search(r"state=([^&]+)", r.location).group(1)

    class R:
        def __init__(self, d): self.d = d
        def json(self): return self.d
    monkeypatch.setattr(A.requests, "post", lambda *a, **k: R({"access_token": "x"}))
    monkeypatch.setattr(A.requests, "get", lambda *a, **k: R({"sub": "g1", "email": "Admin@test.com",
                                                              "email_verified": True, "name": "Admin"}))
    assert c.get(f"/auth/google/callback?code=1&state={state}").status_code == 302
    assert c.get("/admin").status_code == 200
    assert c.get("/auth/google/callback?code=1&state=otro").status_code == 302


def test_csrf_and_security_headers():
    c = client_for("a@test.com")
    assert c.post("/cuenta", data={"section": "cobro", "payout_details": "x"}).status_code == 400
    assert c.post("/cuenta", data={"section": "cobro", "payout_details": "x", "_csrf": "falso"}).status_code == 400
    fresh = A.app.test_client()                       # sesión sin token y formulario sin token
    assert fresh.post("/login/dev", data={"email": "x@test.com"}).status_code == 400
    r = c.get("/")
    for h in ("Content-Security-Policy", "X-Frame-Options", "X-Content-Type-Options", "Referrer-Policy"):
        assert h in r.headers


def test_custom_error_pages_and_health():
    c = A.app.test_client()
    r = c.get("/no-existe")
    assert r.status_code == 404 and "No encontramos esta página" in r.get_data(as_text=True)
    assert c.get("/salud").get_json() == {"ok": True}


def test_non_admin_blocked_from_admin():
    c = client_for("a@test.com")
    assert c.get("/admin").status_code == 403


# ---------------------------------------------------------------- envío de clips y antifraude
def test_submission_rules():
    cid = make_campaign(platforms="tiktok,instagram")
    c = client_for("a@test.com")
    r = post(c, f"/campana/{cid}", url=tt("ana", 1))
    assert "verifica tu cuenta" in flashes(r)[0][1]          # aceptado en la campaña, pero sin cuenta añadida
    post(c, "/cuenta", section="redes", handles_tiktok="@Ana")
    assert "no es una de tus cuentas aceptadas" in flashes(post(c, f"/campana/{cid}", url=tt("otra", 1)))[0][1]
    assert flashes(post(c, f"/campana/{cid}", url=tt("ana", 1) + "?is_from_webapp=1"))[0][0] == "ok"
    assert "ya se envió" in flashes(post(c, f"/campana/{cid}", url=tt("ana", 1)))[0][1]
    assert "no es válido" in flashes(post(c, f"/campana/{cid}", url="https://www.tiktok.com/@ana"))[0][1]
    assert "no acepta vídeos de YouTube" in flashes(post(c, f"/campana/{cid}", url="https://youtube.com/shorts/abcdefghijk"))[0][1]
    with A.app.app_context():
        s = A.Submission.query.one()
        assert s.url == tt("ana", 1) and s.author == "ana"


def test_handles_are_unique_per_clipper():
    a = client_for("a@test.com", tiktok="ana")
    b = client_for("b@test.com")
    r = post(b, "/cuenta", section="redes", handles_tiktok="ana")
    assert "ya está vinculada a otro clipper" in flashes(r)[0][1]
    cid = make_campaign()
    post(b, "/cuenta", section="redes", handles_tiktok="bea")
    r = post(b, f"/campana/{cid}", url=tt("ana", 5))
    assert "vinculada a otro clipper" in flashes(r)[0][1]
    assert a  # el primero conserva su cuenta


def test_daily_limit(monkeypatch):
    monkeypatch.setattr(A, "MAX_CLIPS_DAY", 2)
    cid = make_campaign()
    c = client_for("a@test.com", tiktok="ana")
    post(c, f"/campana/{cid}", url=tt("ana", 1))
    post(c, f"/campana/{cid}", url=tt("ana", 2))
    assert "límite" in flashes(post(c, f"/campana/{cid}", url=tt("ana", 3)))[0][1]


def test_refresh_flags_foreign_author_and_spike(monkeypatch):
    cid = make_campaign()
    c = client_for("a@test.com", instagram="ana")
    client_for("b@test.com", instagram="bea")
    post(c, f"/campana/{cid}", url="https://www.instagram.com/reel/AAA111/")
    post(c, f"/campana/{cid}", url="https://www.instagram.com/reel/BBB222/")
    data = {"AAA111": {"views": 1000, "likes": 100, "author": "bea"},
            "BBB222": {"views": 1000, "likes": 100, "author": "ana"}}
    monkeypatch.setattr(V, "fetch_views", lambda p, urls: data if p == "instagram" else {})
    with A.app.app_context():
        A.refresh_views()
        subs = {s.video_key: s for s in A.Submission.query.all()}
        assert "cuenta_de_otro" in subs["instagram:AAA111"].flag_list
        assert not subs["instagram:BBB222"].flag_list
    data["BBB222"] = {"views": 900000, "likes": 50, "author": "ana"}
    with A.app.app_context():
        A.refresh_views()
        s = A.Submission.query.filter_by(video_key="instagram:BBB222").one()
        assert {"subida_rapida", "pocos_likes"} <= set(s.flag_list)
        assert A.ViewSnapshot.query.filter_by(submission_id=s.id).count() == 2


def test_ban_blocks_everything():
    cid = make_campaign()
    admin = client_for("admin@test.com")
    c = client_for("a@test.com", tiktok="ana")
    with A.app.app_context():
        uid = A.User.query.filter_by(email="a@test.com").one().id
    post(admin, f"/admin/clipper/{uid}/bloquear", reason="Bots")
    r = c.get("/")
    assert r.status_code == 403 and "suspendida" in r.get_data(as_text=True)
    assert c.post(f"/campana/{cid}", data={"_csrf": "t", "url": tt("ana", 1)}).status_code == 403


# ---------------------------------------------------------------- dinero
def approve_all(admin):
    with A.app.app_context():
        ids = [s.id for s in A.Submission.query.filter_by(status="pendiente").order_by(A.Submission.id)]
    for i in ids:
        post(admin, f"/admin/envio/{i}/aprobar")


def set_views(monkeypatch, mapping):
    monkeypatch.setattr(V, "fetch_views", lambda p, urls: {
        V.video_id(u): {"views": mapping[V.video_id(u)], "likes": None, "author": ""}
        for u in urls if V.video_id(u) in mapping})


def test_ledger_budget_order_and_no_decrease(monkeypatch):
    cid = make_campaign(rate=200, budget=300, min_views=5000, max_per_video=150)
    admin = client_for("admin@test.com")
    a = client_for("a@test.com", tiktok="ana")
    b = client_for("b@test.com", tiktok="bea")
    post(a, f"/campana/{cid}", url=tt("ana", 1))
    post(b, f"/campana/{cid}", url=tt("bea", 2))
    approve_all(admin)
    k1, k2 = str(7300000000000000001), str(7300000000000000002)
    set_views(monkeypatch, {k1: 500_000, k2: 4_000})          # 100 $ y por debajo del mínimo
    with A.app.app_context():
        A.refresh_views()
        s1, s2 = A.Submission.query.order_by(A.Submission.id).all()
        assert (s1.earned, s2.earned) == (100.0, 0.0)
    set_views(monkeypatch, {k1: 2_000_000, k2: 2_000_000})    # tope 150 cada uno, pero solo quedan 200
    with A.app.app_context():
        A.refresh_views()
        s1, s2 = A.Submission.query.order_by(A.Submission.id).all()
        camp = get(A.Campaign, cid)
        assert s1.earned == 150.0 and s2.earned == 150.0 and camp.spent == 300.0
        assert camp.status == "finalizada" and camp.exhausted
    set_views(monkeypatch, {k1: 10, k2: 10})                  # bajada de vistas: el dinero no baja
    with A.app.app_context():
        A.refresh_views()
        assert [s.earned for s in A.Submission.query.order_by(A.Submission.id)] == [150.0, 150.0]


def test_budget_never_exceeded_with_many_clips(monkeypatch):
    cid = make_campaign(rate=1000, budget=100)
    admin = client_for("admin@test.com")
    mapping = {}
    for i in range(6):
        c = client_for(f"c{i}@test.com", tiktok=f"user{i}")
        post(c, f"/campana/{cid}", url=tt(f"user{i}", i))
        mapping[str(7300000000000000000 + i)] = 33_333
    approve_all(admin)
    set_views(monkeypatch, mapping)
    with A.app.app_context():
        A.refresh_views()
        total = sum(s.earned for s in A.Submission.query.all())
        assert abs(total - 100.0) < 0.001 and get(A.Campaign, cid).spent == pytest.approx(100.0)


def test_reject_after_approval_claws_back(monkeypatch):
    cid = make_campaign(rate=100, budget=1000)
    admin = client_for("admin@test.com")
    a = client_for("a@test.com", tiktok="ana")
    post(a, f"/campana/{cid}", url=tt("ana", 1))
    approve_all(admin)
    set_views(monkeypatch, {str(7300000000000000001): 1_000_000})
    with A.app.app_context():
        A.refresh_views()
        sid = A.Submission.query.one().id
    post(admin, f"/admin/envio/{sid}/rechazar", reason="Bots")
    with A.app.app_context():
        s = get(A.Submission, sid)
        assert s.earned == 0 and s.campaign.spent == 0 and s.status == "rechazado"
        assert A.AuditLog.query.filter_by(action="clip_rechazado").count() == 1


def test_manual_view_correction_can_lower():
    cid = make_campaign(rate=100, budget=1000)
    admin = client_for("admin@test.com")
    a = client_for("a@test.com", tiktok="ana")
    post(a, f"/campana/{cid}", url=tt("ana", 1))
    approve_all(admin)
    with A.app.app_context():
        sid = A.Submission.query.one().id
    post(admin, f"/admin/envio/{sid}/visitas", views="2000000")
    post(admin, f"/admin/envio/{sid}/visitas", views="500000")
    with A.app.app_context():
        s = get(A.Submission, sid)
        assert s.earned == 50.0 and s.campaign.spent == 50.0


def test_withdrawals(monkeypatch):
    cid = make_campaign(rate=100, budget=1000)
    admin = client_for("admin@test.com")
    a = client_for("a@test.com", tiktok="ana")
    post(a, f"/campana/{cid}", url=tt("ana", 1))
    approve_all(admin)
    set_views(monkeypatch, {str(7300000000000000001): 450_000})     # 45 $
    with A.app.app_context():
        A.refresh_views()
    assert "conecta tu usuario de Whop" in flashes(post(a, "/ganancias/retirar"))[0][1]
    calls = _whop_fake(monkeypatch)
    post(a, "/cuenta", section="cobro", payout_method=A.WHOP_METHOD, whop_username="ana")
    assert "cuando la campaña termina" in flashes(post(a, "/ganancias/retirar"))[0][1]   # campaña abierta: aún no
    with A.app.app_context():
        u = A.User.query.filter_by(email="a@test.com").one()
        assert A.user_money(u)["live"] == 45.0 and A.user_money(u)["available"] == 0
    assert not [x for x in calls if "/transfers" in x[1]]
    post(admin, f"/admin/campana/{cid}/cerrar")
    assert "ha terminado" in a.get("/soporte").get_data(as_text=True)     # aviso por el chat
    assert flashes(post(a, "/ganancias/retirar"))[0][0] == "ok"
    with A.app.app_context():
        p = A.Payout.query.one()
        u = A.User.query.filter_by(email="a@test.com").one()
        assert p.amount == 45.0 and p.status == "pagado" and A.user_money(u)["available"] == 0
    assert "Aún no tienes saldo" in flashes(post(a, "/ganancias/retirar"))[0][1]


# ---------------------------------------------------------------- informes, CSV y admin
def test_report_page_is_public_and_private_data_free(monkeypatch):
    cid = make_campaign(report_token="secreto123")
    admin = client_for("admin@test.com")
    a = client_for("a@test.com", tiktok="ana")
    post(a, f"/campana/{cid}", url=tt("ana", 1))
    approve_all(admin)
    r = A.app.test_client().get("/informe/secreto123")
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and "@ana" in html and "a@test.com" not in html
    assert r.headers.get("X-Robots-Tag") == "noindex, nofollow"
    assert A.app.test_client().get("/informe/malo").status_code == 404


def test_csv_exports_and_activity():
    cid = make_campaign()
    admin = client_for("admin@test.com")
    a = client_for("a@test.com", tiktok="ana")
    post(a, f"/campana/{cid}", url=tt("ana", 1))
    approve_all(admin)
    r = admin.get(f"/admin/campana/{cid}/clips.csv")
    assert r.status_code == 200 and "a@test.com" in r.get_data(as_text=True)
    assert admin.get("/admin/retiros.csv").status_code == 200
    assert A.csv_safe("=HYPERLINK(1)") == "'=HYPERLINK(1)" and A.csv_safe("Ana") == "Ana"
    assert "Clip aprobado" in admin.get("/admin/actividad").get_data(as_text=True)


def test_cover_upload_validation():
    admin = client_for("admin@test.com")
    base = {"_csrf": "t", "song": "S", "artist": "A", "rate": "100", "budget": "500", "pf_tiktok": "on", "status": "activa"}
    bad = dict(base, cover_file=(io.BytesIO(b"no soy una imagen"), "x.png", "image/png"))
    r = admin.post("/admin/campana/nueva", data=bad, content_type="multipart/form-data", follow_redirects=True)
    assert "imagen válida" in r.get_data(as_text=True)
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (40, 40), (255, 90, 54)).save(buf, "PNG")
    buf.seek(0)
    good = dict(base, cover_file=(buf, "x.png", "image/png"))
    admin.post("/admin/campana/nueva", data=good, content_type="multipart/form-data")
    with A.app.app_context():
        camp = A.Campaign.query.one()
        assert camp.cover_mime == "image/webp" and camp.cover_data[8:12] == b"WEBP" and camp.report_token


def test_all_pages_render():
    cid = make_campaign()
    admin = client_for("admin@test.com")
    a = client_for("a@test.com", tiktok="ana")
    post(a, f"/campana/{cid}", url=tt("ana", 1))
    for path in ["/", "/?cat=musica", f"/campana/{cid}", "/enviar", "/ganancias", "/mis-clips?tab=revisar",
                 "/ganancias?tab=retiros", "/cuenta", "/terminos", "/privacidad"]:
        assert a.get(path).status_code == 200, path
    for path in ["/admin", f"/admin/campana/{cid}", f"/admin/campana/{cid}?estado=avisos", f"/admin/campana/{cid}/editar",
                 "/admin/campana/nueva", "/admin/clippers", "/admin/clippers?q=ana", "/admin/actividad", "/admin/probar-apify"]:
        assert admin.get(path).status_code == 200, path


def test_probe(monkeypatch):
    admin = client_for("admin@test.com")
    monkeypatch.setattr(V, "fetch_items", lambda p, urls: [{"id": "7300000000000000001", "playCount": 1234.0,
                                                             "diggCount": 50, "authorMeta": {"name": "Ana"}}])
    html = post(admin, "/admin/probar-apify", url=tt("ana", 1)).get_data(as_text=True)
    assert "Funciona" in html and "1.234" in html and "@ana" in html


def test_cron_requires_key():
    c = A.app.test_client()
    assert c.get("/cron/actualizar").status_code == 403
    assert c.get("/cron/actualizar?key=mala").status_code == 403


# ---------------------------------------------------------------- lectura de enlaces y datos
def test_url_helpers():
    assert V.normalize_url("  www.tiktok.com/@Ana/video/123?is_from_webapp=1 ", resolve=False) == \
        "https://www.tiktok.com/@ana/video/123"
    assert V.video_id("https://youtu.be/abcdefghijk") == "abcdefghijk"
    assert V.video_id("https://www.instagram.com/reel/C1x_Y-2/?igsh=xx") == "C1x_Y-2"
    assert V.clean_handle("https://www.instagram.com/Ana.Clips/") == "ana.clips"
    info = V.info_by_id("tiktok", [{"id": "1", "playCount": "1234.0", "authorMeta": {"name": "@Ana"}}])
    assert info["1"]["views"] == 1234 and info["1"]["author"] == "ana"


def test_clips_counted_until_campaign_ends(monkeypatch):
    cid = make_campaign()
    a = client_for("a@test.com", tiktok="ana")
    post(a, f"/campana/{cid}", url=tt("ana", 1))
    with A.app.app_context():
        A.Submission.query.one().created_at = datetime.utcnow() - timedelta(days=60)
        A.db.session.commit()
    asked = []
    monkeypatch.setattr(V, "fetch_views", lambda p, urls: (asked.extend(urls), {})[1])
    with A.app.app_context():
        A.refresh_views()
    assert asked == [tt("ana", 1)]


def test_old_clips_are_not_refreshed(monkeypatch):
    monkeypatch.setattr(A, "REFRESH_DAYS", 14)          # si se pone un límite de días en Railway
    cid = make_campaign()
    a = client_for("a@test.com", tiktok="ana")
    post(a, f"/campana/{cid}", url=tt("ana", 1))
    post(a, f"/campana/{cid}", url=tt("ana", 2))
    with A.app.app_context():
        old = A.Submission.query.order_by(A.Submission.id).first()
        old.created_at = datetime.utcnow() - timedelta(days=30)
        A.db.session.commit()
    asked = []
    monkeypatch.setattr(V, "fetch_views", lambda p, urls: (asked.extend(urls), {})[1])
    with A.app.app_context():
        A.refresh_views()
    assert asked == [tt("ana", 2)]


# ---------------------------------------------------------------- chat de soporte
def test_support_chat_flow():
    admin = client_for("admin@test.com")
    a = client_for("a@test.com")
    b = client_for("b@test.com")
    assert flashes(post(a, "/soporte", body="   "))[0][0] == "error"
    post(a, "/soporte", body="Hola, ¿cuándo pagáis?")
    with A.app.app_context():
        uid = A.User.query.filter_by(email="a@test.com").one().id
    inbox = admin.get("/admin/mensajes").get_data(as_text=True)
    assert "cuándo pagáis" in inbox and 'class="count"' in inbox
    r = admin.post(f"/admin/mensajes/{uid}", data={"_csrf": "t", "body": "En 7 días hábiles <b>ok</b>"},
                   headers={"X-Requested-With": "fetch"})
    assert r.get_json()["ok"] and r.get_json()["message"]["mine"]
    assert 'class="count"' not in admin.get("/admin/mensajes").get_data(as_text=True)   # leído al abrirlo
    home = a.get("/").get_data(as_text=True)
    assert 'class="msg-notice"' not in home and "1 mensaje del equipo" in home and "<title>(1)" in home   # en la campanita
    n = a.get("/avisos").get_json()
    assert n["unread"] == 1 and n["url"] == "/soporte" and "7 días" in n["body"]
    page = a.get("/soporte").get_data(as_text=True)
    assert "En 7 días hábiles &lt;b&gt;ok&lt;/b&gt;" in page                        # sin HTML inyectado
    home = a.get("/").get_data(as_text=True)
    assert "mensaje del equipo" not in home and "<title>(" not in home
    assert a.get("/avisos").get_json() == {"unread": 0, "bell": 0}
    data = a.get("/soporte/mensajes?after=0").get_json()["messages"]
    assert [m["mine"] for m in data] == [True, False]
    assert "cuándo pagáis" not in b.get("/soporte").get_data(as_text=True)          # conversaciones privadas
    assert b.get(f"/admin/mensajes/{uid}").status_code == 403


def test_chat_limits_and_banned_can_write(monkeypatch):
    monkeypatch.setattr(A, "MAX_MESSAGES_HOUR", 2)
    admin = client_for("admin@test.com")
    a = client_for("a@test.com")
    with A.app.app_context():
        uid = A.User.query.filter_by(email="a@test.com").one().id
    post(admin, f"/admin/clipper/{uid}/bloquear", reason="Pruebas")
    assert a.get("/soporte").status_code == 200
    post(a, "/soporte", body="1")
    post(a, "/soporte", body="2")
    assert "muchos mensajes" in flashes(post(a, "/soporte", body="3"))[0][1]
    assert "x" * 2001 and flashes(post(admin, f"/admin/mensajes/{uid}", body="x" * 2001))[0][0] == "error"


# ---------------------------------------------------------------- portada y solicitudes
def test_landing_and_campaign_request():
    c = A.app.test_client()
    html = c.get("/").get_data(as_text=True)
    assert "Empezar a clipear" in html and "Gana dinero haciendo" in html
    assert "Continuar con Google" in c.get("/login").get_data(as_text=True)
    with c.session_transaction() as s:
        s["_csrf"] = "t"
    r = post(c, "/lanzar-campana", name="Luna", email="mal", brand="Luna Varela")
    assert "correo válido" in r.get_data(as_text=True)
    post(c, "/lanzar-campana", name="Bot", email="b@b.com", brand="Spam", website="http://spam")   # trampa
    r = post(c, "/lanzar-campana", name="Luna", email="Luna@Sello.com", brand="Luna Varela", category="musica",
             budget="$2.000 – $10.000", link="https://open.spotify.com/track/x", message="Lanzamiento en noviembre")
    assert "Solicitud recibida" in r.get_data(as_text=True)
    with A.app.app_context():
        assert A.CampaignLead.query.count() == 1
        lead = A.CampaignLead.query.one()
        assert lead.email == "luna@sello.com" and lead.status == "nueva"
        lid = lead.id
    admin = client_for("admin@test.com")
    assert "Luna Varela" in admin.get("/admin/solicitudes").get_data(as_text=True)
    assert 'class="adm-n"' in admin.get("/admin").get_data(as_text=True)
    form = admin.get(f"/admin/campana/nueva?solicitud={lid}").get_data(as_text=True)
    assert 'value="Luna Varela"' in form and "open.spotify.com" in form
    post(admin, f"/admin/campana/nueva?solicitud={lid}", song="Noche", artist="Luna Varela", rate="200", budget="2000",
         pf_tiktok="on", status="activa", category="musica")
    with A.app.app_context():
        assert get(A.CampaignLead, lid).status == "convertida" and A.Campaign.query.count() == 1
    post(admin, "/admin/solicitudes", lead_id=lid, status="contactada")
    with A.app.app_context():
        assert get(A.CampaignLead, lid).status == "contactada"
    assert A.app.test_client().get("/admin/solicitudes").status_code == 302


def test_campaign_request_rate_limit():
    c = A.app.test_client()
    with c.session_transaction() as s:
        s["_csrf"] = "t"
    for i in range(5):
        post(c, "/lanzar-campana", name="X", email=f"x{i}@x.com", brand="Marca")
    assert "varias solicitudes" in post(c, "/lanzar-campana", name="X", email="y@x.com", brand="Marca").get_data(as_text=True)


# ---------------------------------------------------------------- cobro en USDT y verificación de cuentas
def test_whop_is_only_payout():
    c = client_for("u@test.com")
    for method in ("PayPal", A.USDT_METHOD):
        r = post(c, "/cuenta", section="cobro", payout_method=method, payout_details=WALLET)
        assert "solo por Whop" in r.get_data(as_text=True)
    with A.app.app_context():
        assert not A.User.query.filter_by(email="u@test.com").one().payout_details
    html = client_for("nuevo@test.com").get("/cuenta").get_data(as_text=True)
    assert "Whop" in html and "USDT" not in html and "PayPal" not in html


def test_join_campaign_and_admin_accepts(monkeypatch):
    cid = make_campaign()
    admin = client_for("admin@test.com")
    a = client_for("a@test.com", verify=False)
    page = a.get(f"/campana/{cid}").get_data(as_text=True)
    assert "Solicitar entrar" in page and "Enlace de tu clip" not in page and 'name="handle"' not in page
    assert "Aún no estás aceptado" in a.get("/enviar").get_data(as_text=True)
    # 1) pide entrar: sin cuenta
    html = post(a, f"/campana/{cid}/unirme").get_data(as_text=True)
    assert "Solicitud enviada" in html and "Enlace de tu clip" not in html
    with A.app.app_context():
        mb = A.CampaignMember.query.one()
        assert mb.status == "pendiente" and not A.CampaignAccount.query.first()
        mid = mb.id
    assert "Pendiente" in a.get("/campanas").get_data(as_text=True)
    assert "en revisión" in post(a, f"/campana/{cid}", url=tt("ana", 1)).get_data(as_text=True)
    assert "no se puede añadir" not in post(a, f"/campana/{cid}/cuentas", platform="tiktok", handle="ana").get_data(as_text=True)
    with A.app.app_context():
        assert not A.CampaignAccount.query.first()                       # aún no aceptado: no puede añadir cuenta
    assert "1 solicitud" in admin.get("/admin").get_data(as_text=True)                    # aviso en el resumen (por campaña)
    assert "Aceptar" in admin.get("/admin/solicitudes-clippers").get_data(as_text=True)
    # 2) el admin acepta
    post(admin, f"/admin/solicitud/{mid}/aceptar")
    assert "Te hemos aceptado" in a.get("/soporte").get_data(as_text=True)
    page = a.get(f"/campana/{cid}").get_data(as_text=True)
    assert "Solicitud aprobada" in page and 'name="handle"' in page and "Enlace de tu clip" not in page
    # 3) añade su cuenta y la verifica con la bio (automático)
    post(a, f"/campana/{cid}/cuentas", platform="tiktok", handle="@Ana")
    with A.app.app_context():
        acc = A.CampaignAccount.query.one()
        assert (acc.handle, acc.status) == ("ana", "pendiente")
        code, aid = acc.user.bio_code(), acc.id
    assert "verifica tu cuenta" in post(a, f"/campana/{cid}", url=tt("ana", 1)).get_data(as_text=True)
    b = client_for("b@test.com")
    with A.app.app_context():
        A.db.session.add(A.CampaignMember(user_id=A.User.query.filter_by(email="b@test.com").one().id, campaign_id=cid, status="aceptada"))
        A.db.session.commit()
    assert "ya está vinculada a otro clipper" in post(b, f"/campana/{cid}/cuentas", platform="tiktok", handle="ana").get_data(as_text=True)
    monkeypatch.setattr(V, "fetch_bio", lambda p, h: f"clips {code}")
    with A.app.test_request_context():
        A.check_bio(aid)
    with A.app.app_context():
        assert get(A.CampaignAccount, aid).status == "aceptada"
    with A.app.app_context():
        assert A.Message.query.filter_by(user_id=get(A.CampaignAccount, aid).user_id).count() == 1   # solo el aviso de aceptado
    assert "Enlace de tu clip" in a.get(f"/campana/{cid}").get_data(as_text=True)
    assert "Clip enviado" in post(a, f"/campana/{cid}", url=tt("ana", 2)).get_data(as_text=True)
    assert "no es una de tus cuentas aceptadas" in post(a, f"/campana/{cid}", url=tt("otra", 3)).get_data(as_text=True)
    with A.app.app_context():
        sid = A.Submission.query.one().id
    post(admin, f"/admin/envio/{sid}/aprobar")
    with A.app.app_context():
        assert get(A.Submission, sid).status == "aprobado"
    # en otra campaña hay que pedir entrar de nuevo
    cid2 = make_campaign(song="Otra")
    assert "solicita entrar" in post(a, f"/campana/{cid2}", url=tt("ana", 4)).get_data(as_text=True)
    assert A.app.test_client().post(f"/admin/solicitud/{mid}/aceptar").status_code in (302, 400)


def test_rejected_member():
    cid = make_campaign()
    admin = client_for("admin@test.com")
    a = client_for("r@test.com", verify=False)
    post(a, f"/campana/{cid}/unirme")
    with A.app.app_context():
        mid = A.CampaignMember.query.one().id
    post(admin, f"/admin/solicitud/{mid}/rechazar")
    page = a.get(f"/campana/{cid}").get_data(as_text=True)
    assert "Solicitud no aceptada" in page and "Solicitar entrar" not in page


def test_landing_next_step():
    c = A.app.test_client()
    assert "Empezar a clipear" in c.get("/").get_data(as_text=True)
    assert c.get("/empezar?rol=clipper").headers["Location"].endswith("/login")
    assert c.get("/empezar?rol=campana").headers["Location"].endswith("/lanzar-campana")
    assert c.get("/empezar").headers["Location"].endswith("/")


def test_logout_really_logs_out():
    c = client_for("x@test.com")
    with A.app.app_context():
        pass
    c.post("/login/dev", data={"email": "x@test.com", "_csrf": "t"})
    assert c.get("/ganancias").status_code == 200
    r = c.get("/salir")
    assert r.status_code == 302
    assert any("remember_token=;" in h for h in r.headers.getlist("Set-Cookie"))
    assert c.get("/ganancias").status_code == 302          # ya no entra
    assert "Empezar a clipear" in c.get("/").get_data(as_text=True)


def test_phone_notifications(monkeypatch):
    sent = []
    monkeypatch.setattr(A, "NTFY_TOPIC", "prueba-topic")
    monkeypatch.setattr(A.requests, "post", lambda url, json=None, timeout=None: sent.append((url, json)))

    class NowThread:
        def __init__(self, target, daemon=None): self.target = target
        def start(self): self.target()
    monkeypatch.setattr(A.threading, "Thread", NowThread)
    cid = make_campaign(song="Neón")
    a = client_for("a@test.com", verify=False)
    post(a, "/soporte", body="Hola, ¿cuándo pagáis?")
    post(a, f"/campana/{cid}/unirme", platform="tiktok", handle="ana")
    admin = client_for("admin@test.com")
    with A.app.app_context():
        uid = A.User.query.filter_by(email="a@test.com").one().id
    post(admin, f"/admin/mensajes/{uid}", body="Respuesta del admin")      # lo que escribe el admin no avisa
    assert [j["topic"] for _, j in sent] == ["prueba-topic", "prueba-topic"]
    assert "cuándo pagáis" in sent[0][1]["message"] and sent[0][1]["click"].endswith(f"/admin/mensajes/{uid}")
    assert "quiere unirse" in sent[1][1]["title"] and "Neón" in sent[1][1]["message"]
    monkeypatch.setattr(A, "NTFY_TOPIC", "")
    post(a, "/soporte", body="otro")
    assert len(sent) == 2                                                   # sin tema configurado, no se envía nada


class _R:
    def __init__(self, code, data):
        self.status_code, self._d, self.ok, self.text = code, data, code < 400, ""

    def json(self):
        return self._d


def _whop_fake(monkeypatch, transfer_status="succeeded", fail=False):
    monkeypatch.setattr(A, "WHOP_API_KEY", "k")
    monkeypatch.setattr(A, "WHOP_COMPANY_ID", "biz_1")
    calls = []

    def fake(method, url, **kw):
        calls.append((method, url, kw.get("json"), kw.get("headers", {})))
        if "/users/" in url:
            name = url.rsplit("/", 1)[-1]
            return _R(200, {"id": "user_" + name, "username": name}) if name != "nadie" else _R(404, {"error": "not found"})
        if method == "GET" and "/transfers/" in url:
            return _R(200, {"id": "xfer_1", "status": "succeeded"})
        if fail:
            return _R(400, {"error": {"message": "Saldo insuficiente"}})
        return _R(201, {"id": "xfer_1", "status": transfer_status, "amount": kw["json"]["amount"]})
    monkeypatch.setattr(A.requests, "request", fake)
    monkeypatch.setattr(A, "notify_phone", lambda *a, **k: None)
    return calls


def _clipper_with_balance(monkeypatch, email="w@test.com", views=450_000):
    cid = make_campaign(rate=100, budget=1000)
    admin = client_for("admin@test.com")
    c = client_for(email, tiktok="wh")
    post(c, f"/campana/{cid}", url=tt("wh", 1))
    approve_all(admin)
    set_views(monkeypatch, {str(7300000000000000001): views})
    with A.app.app_context():
        A.refresh_views()
    post(admin, f"/admin/campana/{cid}/cerrar")       # lo ganado se puede retirar al cerrar la campaña
    return c, admin


def test_whop_connect_and_instant_withdraw(monkeypatch):
    c, admin = _clipper_with_balance(monkeypatch)
    # sin claves de Whop: no se puede conectar
    r = post(c, "/cuenta", section="cobro", payout_method=A.WHOP_METHOD, whop_username="ana")
    assert "aún no está activado" in r.get_data(as_text=True)
    calls = _whop_fake(monkeypatch)
    assert "No encontramos" in post(c, "/cuenta", section="cobro", payout_method=A.WHOP_METHOD,
                                    whop_username="@nadie").get_data(as_text=True)
    r = post(c, "/cuenta", section="cobro", payout_method=A.WHOP_METHOD, whop_username="https://whop.com/@ana/")
    assert "Whop conectado: @ana" in r.get_data(as_text=True)
    assert "Activo" in c.get("/cuenta").get_data(as_text=True)
    r = post(c, "/ganancias/retirar")
    assert "Te hemos enviado" in r.get_data(as_text=True)
    with A.app.app_context():
        p = A.Payout.query.one()
        assert p.status == "pagado" and p.ext_id == "xfer_1" and p.amount == 45.0
        u = A.User.query.filter_by(email="w@test.com").one()
        assert A.user_money(u)["available"] == 0
    body, headers = calls[-1][2], calls[-1][3]
    assert body["destination_id"] == "user_ana" and body["origin_id"] == "biz_1" and body["amount"] == 45.0 and body["type"] == "balance"
    assert headers["Idempotency-Key"] == body["idempotence_key"]


def test_whop_processing_then_synced(monkeypatch):
    c, admin = _clipper_with_balance(monkeypatch)
    _whop_fake(monkeypatch, transfer_status="processing")
    post(c, "/cuenta", section="cobro", payout_method=A.WHOP_METHOD, whop_username="ana")
    assert "Llegará en unos minutos" in post(c, "/ganancias/retirar").get_data(as_text=True)
    c.get("/ganancias?tab=retiros")                                          # consulta a Whop y se actualiza
    with A.app.app_context():
        assert A.Payout.query.one().status == "pagado"


def test_whop_failure_returns_money(monkeypatch):
    c, admin = _clipper_with_balance(monkeypatch)
    _whop_fake(monkeypatch, fail=True)
    post(c, "/cuenta", section="cobro", payout_method=A.WHOP_METHOD, whop_username="ana")
    assert "sigue en tu saldo" in post(c, "/ganancias/retirar").get_data(as_text=True)
    with A.app.app_context():
        p = A.Payout.query.one()
        u = A.User.query.filter_by(email="w@test.com").one()
        assert p.status == "rechazado" and "Saldo insuficiente" in p.error and A.user_money(u)["available"] == 45.0
    assert "Saldo insuficiente" in admin.get("/admin/retiros").get_data(as_text=True)
    _whop_fake(monkeypatch)                                                   # ya hay saldo: el clipper vuelve a intentarlo
    assert "Te hemos enviado" in post(c, "/ganancias/retirar").get_data(as_text=True)


def test_old_stuck_payout_is_released(monkeypatch):
    c, admin = _clipper_with_balance(monkeypatch)
    with A.app.app_context():
        u = A.User.query.filter_by(email="w@test.com").one()
        A.db.session.add(A.Payout(user_id=u.id, amount=45.0, method=A.WHOP_METHOD, status="solicitado", note=A.OLD_WHOP_FAIL_NOTE))
        A.db.session.commit()
    c.get("/ganancias")
    with A.app.app_context():
        u = A.User.query.filter_by(email="w@test.com").one()
        assert A.Payout.query.one().status == "rechazado" and A.user_money(u)["available"] == 45.0


def test_whop_big_amount_waits_for_admin(monkeypatch):
    c, admin = _clipper_with_balance(monkeypatch)
    calls = _whop_fake(monkeypatch)
    monkeypatch.setattr(A, "WHOP_AUTO_MAX", 10.0)
    post(c, "/cuenta", section="cobro", payout_method=A.WHOP_METHOD, whop_username="ana")
    assert "cantidad alta" in post(c, "/ganancias/retirar").get_data(as_text=True)
    assert not [x for x in calls if "/transfers" in x[1]]
    with A.app.app_context():
        assert A.Payout.query.one().status == "solicitado"


def test_campaign_form_per_thousand_and_min_pay():
    """El admin pone el pago por cada 1.000 vistas y el mínimo por clip en dinero."""
    admin = client_for("admin@test.com")
    r = post(admin, "/admin/campana/nueva", song="Directo", artist="Rafa", rate_k="1.20", budget="3000",
             min_pay="6", max_per_video="150", pf_tiktok="on", status="activa")
    with A.app.app_context():
        camp = A.Campaign.query.filter_by(song="Directo").one()
        assert camp.rate == 1200 and camp.min_views == 5000 and A.min_pay(camp) == 6.0
    html = client_for("c@test.com").get("/campanas").get_data(as_text=True)
    assert "1,20 $" in html and "POR 1.000 VISTAS" in html and "Mín. 6,00 $ por clip" in html


def test_admin_wipe_all_test_data(monkeypatch):
    c, admin = _clipper_with_balance(monkeypatch)
    post(c, "/soporte", body="hola")
    r = post(admin, "/admin/borrar-datos", confirm="no")
    assert "escribe BORRAR" in r.get_data(as_text=True)
    with A.app.app_context():
        assert A.Campaign.query.count() == 1
    assert client_for("w2@test.com").post("/admin/borrar-datos", data={"confirm": "BORRAR", "_csrf": "t"}).status_code in (302, 403, 404)
    r = post(admin, "/admin/borrar-datos", confirm="BORRAR")
    assert "se han borrado" in r.get_data(as_text=True)
    with A.app.app_context():
        assert A.Campaign.query.count() == 0 and A.Submission.query.count() == 0 and A.Message.query.count() == 0
        assert [u.email for u in A.User.query.all()] == ["admin@test.com"]


def test_email_register_and_login():
    c = A.app.test_client()
    with c.session_transaction() as s:
        s["_csrf"] = "t"
    r = c.post("/registro", data={"_csrf": "t", "name": "Lu", "email": "lu@test.com", "password": "corta"}, follow_redirects=True)
    assert "8 caracteres" in r.get_data(as_text=True)
    r = c.post("/registro", data={"_csrf": "t", "name": "Lu", "email": "admin@test.com", "password": "12345678"}, follow_redirects=True)
    assert "se entra con Google" in r.get_data(as_text=True)
    r = c.post("/registro", data={"_csrf": "t", "name": "Lu", "email": "Lu@Test.com", "password": "12345678"}, follow_redirects=True)
    assert "Bienvenido, Lu" in r.get_data(as_text=True)
    c.get("/salir")
    c2 = A.app.test_client()
    with c2.session_transaction() as s:
        s["_csrf"] = "t"
    r = c2.post("/login/correo", data={"_csrf": "t", "email": "lu@test.com", "password": "mala1234"}, follow_redirects=True)
    assert "incorrectos" in r.get_data(as_text=True)
    with c2.session_transaction() as s:
        s["_csrf"] = "t"
    r = c2.post("/login/correo", data={"_csrf": "t", "email": "lu@test.com", "password": "12345678"})
    assert r.status_code == 302 and c2.get("/ganancias").status_code == 200
    with A.app.app_context():                     # si luego entra con Google ese correo, la contraseña deja de valer
        with A.app.test_request_context():
            A.sign_in("lu@test.com", "Lu", sub="g-123")
        u = A.User.query.filter_by(email="lu@test.com").one()
        assert u.google_sub == "g-123" and u.password_hash == "" and not u.is_admin


def test_join_questions_answers_and_clipper_page():
    admin = client_for("admin@test.com")
    post(admin, "/admin/campana/nueva", song="Preguntas", artist="Rafa", rate_k="1", budget="100", pf_tiktok="on",
         pf_instagram="on", status="activa", join_questions="¿Cuánto llevas clipeando?\n\n¿Clips al día?")
    with A.app.app_context():
        camp = A.Campaign.query.filter_by(song="Preguntas").one()
        cid = camp.id
        assert camp.question_list == ["¿Cuánto llevas clipeando?", "¿Clips al día?"] and camp.platform_list == ["tiktok", "instagram"]
    c = client_for("q@test.com", verify=False)
    assert "¿Clips al día?" in c.get(f"/campana/{cid}").get_data(as_text=True)
    assert "todas las preguntas" in post(c, f"/campana/{cid}/unirme", q_0="2 años").get_data(as_text=True)
    post(c, f"/campana/{cid}/unirme", q_0="2 años", q_1="5")
    assert "Ya enviaste" in post(c, f"/campana/{cid}/unirme", q_0="x", q_1="y").get_data(as_text=True)
    with A.app.app_context():
        mb = A.CampaignMember.query.filter_by(campaign_id=cid).one()
        assert mb.answer_list == [("¿Cuánto llevas clipeando?", "2 años"), ("¿Clips al día?", "5")]
        uid, mid = mb.user_id, mb.id
    assert "2 años" in admin.get("/admin/solicitudes-clippers").get_data(as_text=True)
    post(admin, f"/admin/solicitud/{mid}/aceptar")
    post(c, f"/campana/{cid}/cuentas", platform="tiktok", handle="qq")
    html = admin.get(f"/admin/clipper/{uid}").get_data(as_text=True)
    assert "2 años" in html and "@qq" in html and "Preguntas" in html and "Sin verificar" in html
    assert "2 años" in admin.get(f"/admin/mensajes/{uid}").get_data(as_text=True)
    assert c.get(f"/admin/clipper/{uid}").status_code == 403


def test_chat_photo():
    from PIL import Image
    img = io.BytesIO()
    Image.new("RGB", (40, 30), (200, 10, 10)).save(img, "PNG")
    img.seek(0)
    c = client_for("f@test.com", verify=False)
    r = c.post("/soporte", data={"_csrf": "t", "body": "", "image": (img, "cap.png", "image/png")},
               content_type="multipart/form-data", headers={"X-Requested-With": "fetch"})
    d = r.get_json()
    assert d["ok"] and d["message"]["image"]
    assert c.get(d["message"]["image"]).mimetype == "image/jpeg"
    assert client_for("otro@test.com").get(d["message"]["image"]).status_code == 404
    assert client_for("admin@test.com").get(d["message"]["image"]).status_code == 200
    r = c.post("/soporte", data={"_csrf": "t", "body": "", "image": (io.BytesIO(b"nada"), "x.png", "image/png")},
               content_type="multipart/form-data", headers={"X-Requested-With": "fetch"})
    assert r.status_code == 400 and "no se puede abrir" in r.get_json()["error"]


def test_bio_code_verification(monkeypatch):
    cid = make_campaign()
    a = client_for("bio@test.com", verify=False)
    with A.app.app_context():
        uid = A.User.query.filter_by(email="bio@test.com").one().id
        A.db.session.add(A.CampaignMember(user_id=uid, campaign_id=cid, status="aceptada")); A.db.session.commit()
    post(a, f"/campana/{cid}/cuentas", platform="tiktok", handle="biouser")
    with A.app.app_context():
        code = A.User.query.filter_by(email="bio@test.com").one().verify_code
        aid = A.CampaignAccount.query.one().id
    assert code.startswith("CP") and code in a.get(f"/campana/{cid}").get_data(as_text=True)
    monkeypatch.setattr(V, "fetch_bio", lambda p, h: "Clips diarios 🔥")
    with A.app.test_request_context():
        A.check_bio(aid)
    with A.app.app_context():
        acc = get(A.CampaignAccount, aid)
        assert not acc.bio_verified and acc.status == "pendiente" and "Todavía no vemos" in acc.bio_status
    monkeypatch.setattr(V, "fetch_bio", lambda p, h: f"Clips diarios {code.lower()}")
    with A.app.test_request_context():
        A.check_bio(aid)
    with A.app.app_context():
        acc = get(A.CampaignAccount, aid)
        assert acc.bio_verified and acc.status == "aceptada"          # verificada = ya puede subir clips, sin pasar por el admin
        assert not A.Message.query.filter_by(user_id=uid).first()      # nada por el chat
    cid2 = make_campaign(song="Otra")
    with A.app.app_context():
        A.db.session.add(A.CampaignMember(user_id=uid, campaign_id=cid2, status="aceptada")); A.db.session.commit()
    r = post(a, f"/campana/{cid2}/cuentas", platform="tiktok", handle="biouser")   # cada cuenta, en una sola campaña
    assert "solo puede estar en una campaña" in r.get_data(as_text=True)
    with A.app.app_context():
        get(A.Campaign, cid).status = "finalizada"; A.db.session.commit()
    post(a, f"/campana/{cid2}/cuentas", platform="tiktok", handle="biouser")   # terminada la otra, ya sale verificada
    with A.app.app_context():
        assert A.CampaignAccount.query.filter_by(campaign_id=cid2).one().status == "aceptada"


def test_old_accounts_become_members():
    cid = make_campaign()
    with A.app.app_context():
        A.CampaignMember.query.delete()
        u = A.User(email="old@test.com", name="Old"); A.db.session.add(u); A.db.session.flush()
        A.db.session.add(A.CampaignAccount(user_id=u.id, campaign_id=cid, platform="tiktok", handle="oldie", status="aceptada"))
        A.db.session.commit()
        A.migrate_members()
        mb = A.CampaignMember.query.filter_by(user_id=u.id).one()
        assert mb.status == "aceptada" and A.CampaignAccount.query.filter_by(handle="oldie").one().bio_verified


def test_reject_needs_reason():
    cid = make_campaign()
    admin = client_for("admin@test.com")
    a = client_for("a@test.com", tiktok="ana")
    post(a, f"/campana/{cid}", url=tt("ana", 1))
    with A.app.app_context():
        sid = A.Submission.query.one().id
    assert "motivo" in post(admin, f"/admin/envio/{sid}/rechazar", reason=" ").get_data(as_text=True)
    post(admin, f"/admin/envio/{sid}/rechazar", reason="Sin subtítulos")
    with A.app.app_context():
        s = get(A.Submission, sid)
        assert s.status == "rechazado" and s.reject_reason == "Sin subtítulos"


def test_whop_company_detected_from_key(monkeypatch):
    monkeypatch.setattr(A, "WHOP_API_KEY", "k")
    monkeypatch.setattr(A, "WHOP_COMPANY_ID", "")
    monkeypatch.setattr(A, "_whop_company", {"id": "", "tried": 0.0})
    monkeypatch.setattr(A.requests, "request", lambda m, url, **kw: _R(200, {"id": "biz_auto"}) if url.endswith("/accounts/me") else _R(404, {}))
    with A.app.app_context():
        assert A.whop_enabled() and A.whop_company_id() == "biz_auto"


def test_withdraw_chosen_amount(monkeypatch):
    c, admin = _clipper_with_balance(monkeypatch)      # 45 $ disponibles
    calls = _whop_fake(monkeypatch)
    post(c, "/cuenta", section="cobro", payout_method=A.WHOP_METHOD, whop_username="ana")
    assert 'id="wd-amount"' in c.get("/ganancias").get_data(as_text=True)
    assert "Solo tienes" in flashes(post(c, "/ganancias/retirar", amount="100"))[0][1]
    assert flashes(post(c, "/ganancias/retirar", amount="0"))[0][0] == "error"
    assert flashes(post(c, "/ganancias/retirar", amount="10,50 $"))[0][0] == "ok"
    with A.app.app_context():
        p = A.Payout.query.one()
        u = A.User.query.filter_by(email="w@test.com").one()
        assert p.amount == 10.5 and p.status == "pagado" and A.user_money(u)["available"] == 34.5
    assert [x for x in calls if "/transfers" in x[1]][-1][2]["amount"] == 10.5


def test_cover_small_sizes():
    from PIL import Image
    admin = client_for("admin@test.com")
    img = io.BytesIO(); Image.new("RGB", (900, 900), (10, 200, 90)).save(img, "PNG"); img.seek(0)
    data = {"_csrf": "t", "song": "S", "artist": "A", "rate": "100", "budget": "500", "pf_tiktok": "on", "status": "activa",
            "cover_file": (img, "x.png", "image/png")}
    admin.post("/admin/campana/nueva", data=data, content_type="multipart/form-data")
    with A.app.app_context():
        c = A.Campaign.query.filter_by(song="S").one(); cid, v = c.id, c.cover_version
    r = admin.get(f"/portada/{cid}?v={v}&t=160")
    assert r.mimetype == "image/webp" and len(r.data) < 3000
    assert 'width="62"' in client_for("c@test.com").get("/campanas").get_data(as_text=True)


def test_rate_per_platform(monkeypatch):
    admin = client_for("admin@test.com")
    post(admin, "/admin/campana/nueva", song="Redes", artist="Rafa", budget="1000", min_pay="2", status="activa",
         pf_tiktok="on", pf_instagram="on", rate_k_tiktok="1", rate_k_instagram="1,50")
    with A.app.app_context():
        c = A.Campaign.query.filter_by(song="Redes").one()
        cid = c.id
        assert c.rate == 1500 and c.rate_for("tiktok") == 1000 and c.rate_for("instagram") == 1500 and c.rates_vary
    assert "¿Cuánto tiempo llevas haciendo clips?" in admin.get("/admin/campana/nueva").get_data(as_text=True)   # preguntas predeterminadas
    html = client_for("v@test.com").get("/campanas").get_data(as_text=True)
    assert "HASTA" not in html and "1,50 $" in html   # por fuera, el pago más alto sin «hasta»
    # el mismo número de vistas paga distinto según la red; el mínimo (2 $) es en dinero
    with A.app.app_context():
        camp = A.db.session.get(A.Campaign, cid)
        t = A.Submission(platform="tiktok", views=1500); i = A.Submission(platform="instagram", views=1500)
        assert A.raw_earning(t, camp) == 0.0            # 1,50 $ < mínimo 2 $
        assert A.raw_earning(i, camp) == 2.25          # 1.500 vistas × 1,50 $/1.000
        t.views = 2000
        assert A.raw_earning(t, camp) == 2.0
    page = client_for("v@test.com").get(f"/campana/{cid}").get_data(as_text=True)
    assert "1,00 $" in page and "1,50 $" in page and "Instagram" in page


def test_min_max_per_platform():
    admin = client_for("admin@test.com")
    post(admin, "/admin/campana/nueva", song="MinMax", artist="Pablo", budget="5000", status="activa",
         pf_tiktok="on", pf_instagram="on",
         rate_k_tiktok="1", min_k_tiktok="10", max_k_tiktok="100",
         rate_k_instagram="1,50", min_k_instagram="5", max_k_instagram="")
    with A.app.app_context():
        c = A.Campaign.query.filter_by(song="MinMax").one()
        cid = c.id
        assert (c.min_for("tiktok"), c.max_for("tiktok")) == (10.0, 100.0)
        assert (c.min_for("instagram"), c.max_for("instagram")) == (5.0, None)
        assert c.min_for("youtube") == c.min_money       # red no marcada: valores generales
        t = A.Submission(platform="tiktok", views=9_000)       # 9 $ < mínimo 10 $
        assert A.raw_earning(t, c) == 0.0
        t.views = 500_000                                       # 500 $ → tope 100 $
        assert A.raw_earning(t, c) == 100.0
        i = A.Submission(platform="instagram", views=4_000)     # 6 $ ≥ mínimo 5 $
        assert A.raw_earning(i, c) == 6.0
        i.views = 1_000_000                                     # sin tope en Instagram
        assert A.raw_earning(i, c) == 1500.0
        assert c.min_views_for("tiktok") == 10_000
    page = client_for("v@test.com").get(f"/campana/{cid}").get_data(as_text=True)
    assert "10,00 $" in page and "100,00 $" in page and "Sin máximo" in page and "hasta" not in page
    home = client_for("v@test.com").get("/campanas").get_data(as_text=True)
    assert "Mín. hasta 10,00 $" in home and "Sin máximo por clip" in home
    # un mínimo mayor que el máximo no se puede guardar
    r = post(admin, f"/admin/campana/{cid}/editar", song="MinMax", artist="Pablo", budget="5000", status="activa",
             pf_tiktok="on", rate_k_tiktok="1", min_k_tiktok="200", max_k_tiktok="100")
    assert "no puede ser mayor que el máximo" in r.get_data(as_text=True)


def test_admin_sections_render():
    admin = client_for("admin@test.com")
    cid = make_campaign()
    for url in ["/admin", "/admin/campanas", "/admin/campanas?estado=activa", "/admin/solicitudes-clippers",
                "/admin/solicitudes-clippers?estado=aceptada", "/admin/clips", f"/admin/clips?c={cid}&avisos=1",
                "/admin/retiros", "/admin/ajustes", "/admin/clippers", "/admin/mensajes", "/admin/actividad",
                "/admin/solicitudes", f"/admin/campana/{cid}", "/admin/campana/nueva"]:
        r = admin.get(url)
        assert r.status_code == 200, url
        assert 'class="adm-side"' in r.get_data(as_text=True), url          # menú lateral en todas
    assert 'class="adm-side"' not in client_for("c@test.com").get("/").get_data(as_text=True)



def test_admin_wipe_keeps_campaigns(monkeypatch):
    c, admin = _clipper_with_balance(monkeypatch)
    post(c, "/soporte", body="hola")
    r = post(admin, "/admin/borrar-datos", confirm="BORRAR", mode="keep_campaigns")
    assert "Las campañas se quedan" in r.get_data(as_text=True)
    with A.app.app_context():
        camps = A.Campaign.query.all()
        assert len(camps) == 1 and camps[0].spent == 0
        assert A.Submission.query.count() == 0 and A.Payout.query.count() == 0 and A.Message.query.count() == 0
        assert A.CampaignMember.query.count() == 0 and A.CampaignAccount.query.count() == 0
        assert [u.email for u in A.User.query.all()] == ["admin@test.com"]


def test_admin_removed_from_env_loses_access(monkeypatch):
    c, admin = _clipper_with_balance(monkeypatch)
    assert admin.get("/admin").status_code == 200
    monkeypatch.setattr(A, "ADMIN_EMAILS", set())
    assert admin.get("/admin").status_code in (302, 403, 404)
    with A.app.app_context():
        assert not A.User.query.filter_by(email="admin@test.com").first().is_admin


def test_auto_refresh_every_6_hours(monkeypatch):
    from datetime import datetime, timedelta
    monkeypatch.setattr(A.viewcounter, "APIFY_TOKEN", "x")
    monkeypatch.setattr(A, "REFRESH_HOURS", 6)
    with A.app.app_context():
        A.set_setting("last_refresh_at", "")
        assert A._refresh_due()                     # nunca se ha hecho
        now = datetime.utcnow()
        A.set_setting("last_refresh_at", (now - timedelta(hours=5)).isoformat())
        assert not A._refresh_due(now)
        A.set_setting("last_refresh_at", (now - timedelta(hours=6, minutes=1)).isoformat())
        assert A._refresh_due(now)
    monkeypatch.setattr(A.viewcounter, "APIFY_TOKEN", "")
    with A.app.app_context():
        assert not A._refresh_due()                 # sin Apify no hace nada


def test_campaign_full_blocks_new_requests(monkeypatch):
    c, admin = _clipper_with_balance(monkeypatch)
    with A.app.app_context():
        camp = A.Campaign.query.first()
        camp.full, camp.status, cid = True, "activa", camp.id
        A.db.session.commit()
    new = client_for("nuevo-full@test.com")
    html = new.get(f"/campana/{cid}").get_data(as_text=True)
    assert "Plazas completas" in html and 'data-open="join-dialog"' not in html
    r = post(new, f"/campana/{cid}/unirme")
    assert "plazas de esta campaña están completas" in r.get_data(as_text=True)
    with A.app.app_context():
        assert A.CampaignMember.query.filter_by(campaign_id=cid).join(A.User).filter(A.User.email == "nuevo-full@test.com").count() == 0


def test_whop_app_test_box(monkeypatch):
    monkeypatch.setattr(A, "CREATORS_ENABLED", True)
    c, admin = _clipper_with_balance(monkeypatch)
    monkeypatch.setattr(A, "WHOP_APP_API_KEY", "apik_test")
    calls = []

    def fake(method, path, **kw):
        calls.append((method, path, kw.get("headers", {}).get("Authorization"), kw.get("json")))
        if path.startswith("/users/"):
            return True, 200, {"id": "user_123", "username": "pepe"}
        return True, 200, {"id": "ctt_1", "status": "completed"}
    monkeypatch.setattr(A, "whop_call", fake)
    html = post(admin, "/admin/ajustes/probar-app-whop", company="biz_abc", username="@pepe", amount="0,5").get_data(as_text=True)
    assert "Funciona" in html
    assert calls[1][1] == "/transfers" and calls[1][2] == "Bearer apik_test"
    assert calls[1][3]["origin_id"] == "biz_abc" and calls[1][3]["amount"] == 0.5
    bad = post(admin, "/admin/ajustes/probar-app-whop", company="biz_abc", username="pepe", amount="50").get_data(as_text=True)
    assert "entre 0,01 y 5" in bad
    assert c.post("/admin/ajustes/probar-app-whop", data={"_csrf": "t", "company": "biz_abc", "username": "x", "amount": "1"}).status_code in (302, 403, 404)


def test_full_campaigns_go_last_on_home(monkeypatch):
    c, admin = _clipper_with_balance(monkeypatch)
    with A.app.app_context():
        a = A.Campaign(song="Primera Llena", artist="X", rate=1000, budget=100, status="activa", full=True)
        b = A.Campaign(song="Segunda Abierta", artist="Y", rate=1000, budget=100, status="activa")
        A.db.session.add_all([a, b]); A.db.session.commit()
    html = client_for("mira-orden@test.com").get("/campanas").get_data(as_text=True)
    grid = html[html.index("Campañas para ti"):]
    assert grid.index("Segunda Abierta") < grid.index("Primera Llena")


def test_admin_delete_clipper_account(monkeypatch):
    c, admin = _clipper_with_balance(monkeypatch)
    post(c, "/soporte", body="hola")
    with A.app.app_context():
        u = A.User.query.filter(A.User.is_admin.is_(False)).first()
        uid, camp = u.id, A.Campaign.query.first()
        cid, earned = camp.id, sum(s.earned or 0 for s in u.submissions)
        spent_before = camp.spent
    r = post(admin, f"/admin/clipper/{uid}/eliminar", confirm="no")
    assert "escribe ELIMINAR" in r.get_data(as_text=True)
    r = post(admin, f"/admin/clipper/{uid}/eliminar", confirm="ELIMINAR")
    assert "se ha eliminado" in r.get_data(as_text=True)
    with A.app.app_context():
        assert A.db.session.get(A.User, uid) is None
        assert A.Submission.query.filter_by(user_id=uid).count() == 0 and A.Message.query.filter_by(user_id=uid).count() == 0
        assert abs(A.db.session.get(A.Campaign, cid).spent - max(0, spent_before - earned)) < 0.01
        admin_id = A.User.query.filter_by(is_admin=True).first().id
    r = post(admin, f"/admin/clipper/{admin_id}/eliminar", confirm="ELIMINAR")
    assert "administrador" in r.get_data(as_text=True)


def test_admin_delete_activity_entry(monkeypatch):
    c, admin = _clipper_with_balance(monkeypatch)
    with A.app.app_context():
        e = A.AuditLog(action="clipper_eliminado", detail="borrame@test.com"); A.db.session.add(e); A.db.session.commit(); lid = e.id
    assert "borrame@test.com" in admin.get("/admin/actividad").get_data(as_text=True)
    assert c.post(f"/admin/actividad/{lid}/borrar", data={"_csrf": "t"}).status_code in (302, 403, 404)
    r = post(admin, f"/admin/actividad/{lid}/borrar")
    assert "Registro borrado" in r.get_data(as_text=True) and "borrame@test.com" not in r.get_data(as_text=True)


def test_waitlist_notifies_when_places_open(monkeypatch):
    c, admin = _clipper_with_balance(monkeypatch)
    with A.app.app_context():
        camp = A.Campaign(song="Lista Espera", artist="Z", rate=1000, budget=100, status="activa", full=True)
        A.db.session.add(camp); A.db.session.commit(); cid = camp.id
    w = client_for("espera@test.com")
    assert "Avísame cuando haya plazas" in w.get(f"/campana/{cid}").get_data(as_text=True)
    r = post(w, f"/campana/{cid}/avisame")
    assert "Te avisaremos" in r.get_data(as_text=True)
    with A.app.app_context():
        assert A.CampaignWaitlist.query.filter_by(campaign_id=cid).count() == 1
    # el admin quita «plazas completas» guardando la campaña
    with A.app.app_context():
        camp = A.db.session.get(A.Campaign, cid)
        camp.full = True
        with A.app.test_request_context(method="POST", data={"status": "activa"}):
            from flask_login import login_user
            login_user(A.User.query.filter_by(is_admin=True).first())
            assert A.notify_waitlist(camp) == 1
        A.db.session.commit()
        uid = A.User.query.filter_by(email="espera@test.com").first().id
        msgs = A.Message.query.filter_by(user_id=uid).all()
        assert any("Hay plazas" in m.body for m in msgs)
        assert A.CampaignWaitlist.query.filter_by(campaign_id=cid).count() == 0


def test_campaign_analytics_series(monkeypatch):
    from datetime import datetime, timedelta
    c, admin = _clipper_with_balance(monkeypatch)
    with A.app.app_context():
        camp = A.Campaign.query.first()
        s = next(x for x in camp.submissions if x.status == "aprobado")
        base = datetime.utcnow() - timedelta(days=3)
        s.created_at = base
        A.ViewSnapshot.query.filter_by(submission_id=s.id).delete()
        A.db.session.add_all([
            A.ViewSnapshot(submission_id=s.id, views=1000, taken_at=base + timedelta(days=1)),            # sin likes: se estiman
            A.ViewSnapshot(submission_id=s.id, views=3000, likes=300, comments=30, shares=3, taken_at=base + timedelta(days=2)),
        ])
        s.views, s.likes, s.comments, s.shares = 4000, 400, 40, 4
        A.db.session.commit()
        an = A.campaign_analytics(camp)
        ser = an["series"]
        assert len(ser["labels"]) >= 4
        cum = ser["cumulative"]["views"]
        assert cum == sorted(cum) and cum[-1] == an["totals"]["views"]          # acumulado nunca baja y acaba en el total
        assert sum(ser["daily"]["views"]) == cum[-1]
        assert an["posts"] >= 1 and an["cpm"] == camp.rate / 1000
    html = client_for("cualquiera@test.com").get(f"/informe/{camp.report_token}" if camp.report_token else "/").get_data(as_text=True)
    page = admin.get(f"/admin/campana/{camp.id}").get_data(as_text=True)
    assert "data-chart-data" in page and "eCPM" in page


def test_backup_and_wipe_blocked_in_production(monkeypatch):
    import json as _json
    c, admin = _clipper_with_balance(monkeypatch)
    r = admin.get("/admin/copia-de-seguridad.json")
    data = _json.loads(r.get_data(as_text=True))
    assert r.status_code == 200 and "campaign" in data["tables"] and "user" in data["tables"]
    assert all("cover_data" not in row for row in data["tables"]["campaign"])
    assert c.get("/admin/copia-de-seguridad.json").status_code in (302, 403, 404)
    monkeypatch.setattr(A, "ALLOW_WIPE", False)
    r = post(admin, "/admin/borrar-datos", confirm="BORRAR", mode="all")
    assert "desactivado" in r.get_data(as_text=True)
    with A.app.app_context():
        assert A.Campaign.query.count() == 1


def test_parse_spanish_numbers():
    assert A.parse_float("5.000") == 5000 and A.parse_float("5.000,50") == 5000.5
    assert A.parse_float("1,5") == 1.5 and A.parse_float("1.5") == 1.5 and A.parse_float("0.150") == 0.15
    assert A.parse_float("1.500.000") == 1_500_000 and A.parse_float("") is None and A.parse_float("abc", 0) == 0


def _two_creator_setup(monkeypatch):
    """Un clipper con dinero en dos campañas cerradas: una del creador Pablo (su Whop) y otra de la cuenta principal."""
    c, admin = _clipper_with_balance(monkeypatch)
    monkeypatch.setenv("WHOP_KEY_PABLO", "apik_pablo")
    with A.app.app_context():
        cr = A.Creator(name="Pablo", key_var="WHOP_KEY_PABLO", company_id="biz_pablo")
        A.db.session.add(cr); A.db.session.commit()
        u = A.User.query.filter(A.User.is_admin.is_(False)).first()
        camp1 = A.Campaign.query.first()
        camp2 = A.Campaign(song="Otra", artist="P", rate=1_000_000, budget=1000, spent=40, status="finalizada", creator_id=cr.id)
        A.db.session.add(camp2); A.db.session.flush()
        A.db.session.add(A.Submission(user_id=u.id, campaign_id=camp2.id, url="https://www.tiktok.com/@x/video/99", platform="tiktok",
                                      video_key="tiktok:99", views=40, earned=40, status="aprobado"))
        A.db.session.commit()
        before = A.user_money(u)
        return c, admin, cr.id, before


def test_withdraw_splits_by_creator_wallet(monkeypatch):
    monkeypatch.setattr(A, "CREATORS_ENABLED", True)
    c, admin, crid, before = _two_creator_setup(monkeypatch)
    assert before["by_creator"].get(crid) == 40
    calls = _whop_fake(monkeypatch)
    post(c, "/cuenta", section="cobro", payout_method=A.WHOP_METHOD, whop_username="ana")
    r = post(c, "/ganancias/retirar")
    assert "Te hemos enviado" in r.get_data(as_text=True)
    with A.app.app_context():
        ps = A.Payout.query.order_by(A.Payout.id).all()
        assert {p.creator_id for p in ps} == {crid, None}
        assert abs(sum(p.amount for p in ps) - before["available"]) < 0.01
    transfers = [x for x in calls if x[0] == "POST" and x[1].endswith("/transfers")]
    assert {t[2].get("origin_id") for t in transfers} == {"biz_pablo", "biz_1"}
    keys = {t[2].get("origin_id"): t[3].get("Authorization") for t in transfers}
    assert keys["biz_pablo"] == "Bearer apik_pablo" and keys["biz_1"] == "Bearer k"     # cada pago con la clave de su Whop


def test_creator_without_key_keeps_money_reserved(monkeypatch):
    monkeypatch.setattr(A, "CREATORS_ENABLED", True)
    c, admin, crid, before = _two_creator_setup(monkeypatch)
    monkeypatch.delenv("WHOP_KEY_PABLO")
    _whop_fake(monkeypatch)
    post(c, "/cuenta", section="cobro", payout_method=A.WHOP_METHOD, whop_username="ana")
    r = post(c, "/ganancias/retirar").get_data(as_text=True)
    assert "quedan reservados" in r
    with A.app.app_context():
        p = A.Payout.query.filter_by(creator_id=crid).first()
        assert p.status == "solicitado" and "Esperando" in p.note


def test_creators_page_and_roles(monkeypatch):
    monkeypatch.setattr(A, "CREATORS_ENABLED", True)
    c, admin, crid, before = _two_creator_setup(monkeypatch)
    html = admin.get("/admin/creadores").get_data(as_text=True)
    assert "Pablo" in html and "Conectado" in html
    r = post(admin, "/admin/creadores/nuevo", name="Mala", key_var="otra cosa")
    assert "WHOP_KEY_" in r.get_data(as_text=True)
    monkeypatch.setattr(A, "REVIEWER_EMAILS", {"rev@test.com"})
    rev = client_for("rev@test.com")
    with A.app.app_context():
        u = A.User.query.filter_by(email="rev@test.com").first(); u.is_admin, u.admin_role = True, "reviewer"; A.db.session.commit()
    assert rev.get("/admin/clips").status_code == 200
    assert rev.get("/admin/retiros").status_code == 403 and rev.get("/admin/creadores").status_code == 403
    assert "Retiros" not in rev.get("/admin").get_data(as_text=True).split("adm-nav")[1].split("</nav>")[0]


# ---------------------------------------------------------------- fase 2: revisión y campañas
def _refresh_with(monkeypatch, data):
    monkeypatch.setattr(V, "fetch_views", lambda p, urls: data if p == "tiktok" else {})
    with A.app.test_request_context():
        A.refresh_views()


def _vid(n):
    return str(7300000000000000000 + n)


def test_clip_counts_views_until_minimum_then_goes_to_review(monkeypatch):
    cid = make_campaign(min_tiktok=1.0)          # 200 por 1M → mínimo $1 = 5.000 vistas
    admin = client_for("admin@test.com")
    a = client_for("a@test.com", tiktok="ana")
    r = post(a, f"/campana/{cid}", url=tt("ana", 1))
    assert "contando vistas" in r.get_data(as_text=True).lower()
    _refresh_with(monkeypatch, {_vid(1): {"views": 1000}})
    page = admin.get("/admin/clips").get_data(as_text=True)
    assert "Todo revisado" in page
    assert "@ana" in admin.get("/admin/clips?tab=contando").get_data(as_text=True)
    _refresh_with(monkeypatch, {_vid(1): {"views": 6000}})
    page = admin.get("/admin/clips").get_data(as_text=True)
    assert 'data-card' in page and "tiktok.com/embed/v2/" + _vid(1) in page and "Detección de bots" in page


def test_video_posted_before_joining_is_flagged(monkeypatch):
    cid = make_campaign()
    a = client_for("a@test.com", tiktok="ana")
    post(a, f"/campana/{cid}", url=tt("ana", 1))
    _refresh_with(monkeypatch, {_vid(1): {"views": 100, "posted_at": datetime.utcnow() - timedelta(days=30)}})
    with A.app.app_context():
        s = A.Submission.query.one()
        assert s.status == "pendiente" and "publicado_antes" in s.flag_list


def test_reject_with_fixed_reason_and_appeal_once():
    cid = make_campaign()
    admin = client_for("admin@test.com")
    a = client_for("a@test.com", tiktok="ana")
    post(a, f"/campana/{cid}", url=tt("ana", 1))
    with A.app.app_context():
        sid = A.Submission.query.one().id
    post(admin, f"/admin/envio/{sid}/rechazar", code="menciones", reason="falta @marca")
    with A.app.app_context():
        assert get(A.Submission, sid).reject_reason == "Faltan las menciones o hashtags obligatorios: falta @marca"
    post(a, f"/clip/{sid}/apelar", text="Sí que puse la mención, mira el minuto 0:03")
    assert "ya no se puede" in post(a, f"/clip/{sid}/apelar", text="otra vez lo mismo, por favor").get_data(as_text=True)
    assert "minuto 0:03" in admin.get("/admin/clips?tab=apelaciones").get_data(as_text=True)
    post(admin, f"/admin/envio/{sid}/apelacion_aceptar", approve="1")
    with A.app.app_context():
        s = get(A.Submission, sid)
        assert s.status == "aprobado" and s.appeal_status == "aceptada"
        assert A.Message.query.filter_by(user_id=s.user_id).count() >= 1


def test_bulk_review():
    cid = make_campaign()
    admin = client_for("admin@test.com")
    a = client_for("a@test.com", tiktok="ana")
    for n in (1, 2, 3):
        post(a, f"/campana/{cid}", url=tt("ana", n))
    with A.app.app_context():
        ids = [s.id for s in A.Submission.query.all()]
    admin.post("/admin/clips/lote", data={"_csrf": "t", "ids": ids[:2], "accion": "aprobar"})
    admin.post("/admin/clips/lote", data={"_csrf": "t", "ids": ids[2:], "accion": "rechazar", "code": "calidad"})
    with A.app.app_context():
        st = sorted(s.status for s in A.Submission.query.all())
        assert st == ["aprobado", "aprobado", "rechazado"]


def test_duplicate_pause_and_add_budget():
    cid = make_campaign(budget=100, spent=90)
    admin = client_for("admin@test.com")
    post(admin, f"/admin/campana/{cid}/duplicar")
    with A.app.app_context():
        copy = A.Campaign.query.filter(A.Campaign.id != cid).one()
        assert copy.status == "pausada" and copy.spent == 0 and copy.song.endswith("(copia)")
    post(admin, f"/admin/campana/{cid}/estado", accion="pausar")
    post(admin, f"/admin/campana/{cid}/estado", accion="presupuesto", amount="1.000")
    with A.app.app_context():
        c = get(A.Campaign, cid)
        assert c.status == "pausada" and c.budget == 1100


def test_campaign_places_fill_up_and_toggle():
    cid = make_campaign(max_members=1)
    a = client_for("a@test.com", tiktok="ana", verify=False)
    post(a, f"/campana/{cid}/unirme")
    admin = client_for("admin@test.com")
    with A.app.app_context():
        mid = A.CampaignMember.query.one().id
    post(admin, f"/admin/solicitud/{mid}/aceptar")
    with A.app.app_context():
        assert get(A.Campaign, cid).full
    post(admin, f"/admin/campana/{cid}/estado", accion="plazas")
    with A.app.app_context():
        assert not get(A.Campaign, cid).full


def test_campaign_announcements_and_ideas_one_by_one():
    cid = make_campaign()
    admin = client_for("admin@test.com")
    a = client_for("a@test.com", tiktok="ana")
    post(a, f"/campana/{cid}", url=tt("ana", 1))          # le acepta en la campaña
    post(admin, f"/admin/campana/{cid}/contenido", accion="anuncio", text="Nuevo material en el Drive")
    post(admin, f"/admin/campana/{cid}/contenido", accion="idea", text="Top 3 frases")
    post(admin, f"/admin/campana/{cid}/contenido", accion="idea", text="Reacción")
    post(admin, f"/admin/campana/{cid}/contenido", accion="borrar_idea", i="0")
    with A.app.app_context():
        c = get(A.Campaign, cid)
        assert c.idea_list == ["Reacción"] and c.announcement_list[0][1] == "Nuevo material en el Drive"
        ana = A.User.query.filter_by(email="a@test.com").one()
        assert A.Notice.query.filter_by(kind="anuncio", user_id=ana.id).count() == 1

def test_wizard_saves_campaign():
    admin = client_for("admin@test.com")
    post(admin, "/admin/campana/nueva", wizard="1", song="Nueva", artist="Pablo", pf_tiktok="on", rate_k_tiktok="1,5",
         min_k_tiktok="", max_k_tiktok="", budget="5.000", max_members="20", status="activa")
    with A.app.app_context():
        c = A.Campaign.query.one()
        assert (c.kind, c.max_members, c.rate, c.budget) == ("clipping", 20, 1500, 5000)

def test_publish_date_parsed_from_apify():
    assert V.parse_item("tiktok", {"playCount": 5, "createTime": 1700000000})["posted_at"].year == 2023
    assert V.parse_item("instagram", {"videoPlayCount": 5, "timestamp": "2024-05-01T10:00:00.000Z"})["posted_at"].month == 5


# ---------------------------------------------------------------- fase 3-6: clipper, avisos, formación, informe
def test_my_clips_tabs_and_stats(monkeypatch):
    cid = make_campaign(min_tiktok=1.0)
    a = client_for("a@test.com", tiktok="ana")
    post(a, f"/campana/{cid}", url=tt("ana", 1))
    page = a.get("/mis-clips?tab=contando").get_data(as_text=True)
    assert "Contando vistas" in page and "Faltan" in page
    assert "Nada por aquí" in a.get("/mis-clips?tab=aprobado").get_data(as_text=True)


def test_onboarding_two_steps():
    a = client_for("a@test.com", verify=False)
    post(a, "/bienvenida?paso=1", name="Ana García", tiktok="@anag")
    post(a, "/bienvenida?paso=2", country="MX", languages=["es", "en"])           # el paso 2 ya termina
    with A.app.app_context():
        u = A.User.query.filter_by(email="a@test.com").one()
        assert (u.name, u.country, u.languages) == ("Ana García", "MX", "es")
        assert u.onboarded_at and "anag" in u.handles("tiktok")
    home = a.get("/").get_data(as_text=True)
    assert "Primeros pasos" not in home and "Agrega tu cuenta" not in home


def test_ranking_masks_names():
    cid = make_campaign()
    admin = client_for("admin@test.com")
    a = client_for("a@test.com", tiktok="ana")
    post(a, "/bienvenida?paso=1", name="Ana García López")
    post(a, f"/campana/{cid}", url=tt("ana", 1))
    with A.app.app_context():
        sid = A.Submission.query.one().id
    post(admin, f"/admin/envio/{sid}/aprobar")
    page = client_for("b@test.com").get("/ranking").get_data(as_text=True)
    assert "Ana G." in page and "García López" not in page


def test_web_push_encryption_roundtrip():
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    import push as P
    ua = ec.generate_private_key(ec.SECP256R1())
    ua_pub = P._raw_public(ua)
    auth = os.urandom(16)
    body = P.encrypt(b"hola", P.b64u(ua_pub), P.b64u(auth))
    salt, idlen = body[:16], body[20]
    as_pub, cipher = body[21:21 + idlen], body[21 + idlen:]
    peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_pub)
    secret = ua.exchange(ec.ECDH(), peer)
    ikm = P._hkdf(auth, secret, b"WebPush: info\x00" + ua_pub + as_pub, 32)
    cek = P._hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = P._hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    assert AESGCM(cek).decrypt(nonce, cipher, None) == b"hola\x02"
    priv, pub = P.new_vapid_keys()
    assert P.vapid_public(priv) == pub
    assert P.vapid_header("https://fcm.googleapis.com/x", priv, "mailto:a@b.c").startswith("vapid t=")


def test_notifications_respect_preferences(monkeypatch):
    sent = []
    monkeypatch.setattr(A, "VAPID_PRIVATE_KEY", "x")
    monkeypatch.setattr(A, "VAPID_PUBLIC_KEY", "y")
    monkeypatch.setattr(A, "_spawn", lambda fn, *args: sent.append(args))
    cid = make_campaign()
    admin = client_for("admin@test.com")
    a = client_for("a@test.com", tiktok="ana")
    post(a, "/notificaciones", **{"clip_aprobado.push": "on"})
    post(a, f"/campana/{cid}", url=tt("ana", 1))
    with A.app.app_context():
        sid = A.Submission.query.one().id
        u = A.User.query.filter_by(email="a@test.com").one()
        assert A.wants(u, "clip_aprobado", "push") and not A.wants(u, "clip_rechazado", "push")
    post(admin, f"/admin/envio/{sid}/aprobar")
    assert sent and sent[0][1] == "clip_aprobado"
    post(admin, "/admin/avisos", audience="todos", title="Hola {nombre}", body="Hola {nombre}, hay campaña", chat="on")
    with A.app.app_context():
        assert "hay campaña" in A.Message.query.order_by(A.Message.id.desc()).first().body


def test_push_subscribe_saves_device():
    a = client_for("a@test.com")
    r = a.post("/push/suscribir", data={"_csrf": "t", "endpoint": "https://fcm.googleapis.com/abc", "p256dh": "k", "auth": "a"})
    assert r.status_code == 200
    with A.app.app_context():
        assert A.PushSub.query.count() == 1


def test_courses_lessons_and_progress():
    admin = client_for("admin@test.com")
    post(admin, "/admin/formacion", title="Curso de clips")
    with A.app.app_context():
        c = A.Course.query.one()
        cid, chid = c.id, c.chapters[0].id
    post(admin, f"/admin/formacion/{cid}", act="leccion", chapter=str(chid), title="Ganchos")
    with A.app.app_context():
        lid = A.Lesson.query.one().id
    post(admin, f"/admin/formacion/leccion/{lid}", title="Ganchos", body="Empieza fuerte", video_url="https://youtu.be/abcdefghijk", published="on")
    a = client_for("a@test.com")
    assert "Muy pronto" in a.get("/formacion").get_data(as_text=True)
    post(admin, f"/admin/formacion/{cid}", act="guardar", title="Curso de clips", published="on")
    assert "Curso de clips" in a.get("/formacion").get_data(as_text=True)
    page = a.get(f"/formacion/leccion/{lid}").get_data(as_text=True)
    assert "youtube-nocookie.com/embed/abcdefghijk" in page and "Empieza fuerte" in page
    post(a, f"/formacion/leccion/{lid}/progreso", done="1")                     # un vídeo no se marca a mano
    assert "100%" not in a.get("/formacion").get_data(as_text=True)
    with A.app.app_context():
        A.LessonProgress.query.update({"updated_at": datetime.utcnow() - timedelta(minutes=2)}); A.db.session.commit()
    post(a, f"/formacion/leccion/{lid}/progreso", ranges="0-60", duration="60")  # lo ha visto entero
    html = a.get("/formacion").get_data(as_text=True)
    assert "100%" in html and "1 capítulo · 1 lección" in html
    assert "i.ytimg.com/vi/abcdefghijk" in a.get(f"/formacion/leccion/{lid}").get_data(as_text=True)   # miniatura


def test_course_opens_where_you_left_off():
    admin = client_for("admin@test.com")
    post(admin, "/admin/formacion", title="Curso")
    with A.app.app_context():
        c = A.Course.query.one(); c.published = True
        ch = c.chapters[0]
        ls = [A.Lesson(chapter_id=ch.id, title=f"L{i}", position=i, duration=120) for i in range(3)]
        A.db.session.add_all(ls); A.db.session.commit()
        cid, ids = c.id, [x.id for x in ls]
    a = client_for("a@test.com")
    assert a.get(f"/formacion/{cid}").headers["Location"].endswith(f"/formacion/leccion/{ids[0]}")
    post(a, f"/formacion/leccion/{ids[0]}/progreso", done="1")
    assert a.get(f"/formacion/{cid}").headers["Location"].endswith(f"/formacion/leccion/{ids[1]}")
    post(a, f"/formacion/leccion/{ids[2]}/progreso", seconds="40")              # empezó la 3.ª sin acabarla
    assert a.get(f"/formacion/{cid}").headers["Location"].endswith(f"/formacion/leccion/{ids[2]}")
    html = a.get("/formacion").get_data(as_text=True)
    assert "33%" in html and "6 min" in html and "ClipProfit" in html            # portada generada sin imagen


def test_course_cover_upload():
    from PIL import Image
    admin = client_for("admin@test.com")
    post(admin, "/admin/formacion", title="Con portada")
    with A.app.app_context():
        cid = A.Course.query.one().id
    buf = io.BytesIO(); Image.new("RGB", (160, 100), "red").save(buf, "PNG"); buf.seek(0)
    admin.post(f"/admin/formacion/{cid}", data={"_csrf": "t", "act": "guardar", "title": "Con portada",
                                               "cover_file": (buf, "p.png")}, content_type="multipart/form-data")
    with A.app.app_context():
        assert A.db.session.get(A.Course, cid).cover_version == 1
    r = admin.get(f"/formacion/{cid}/portada?v=1")
    assert r.status_code == 200 and r.mimetype == "image/webp"


def test_remove_campaign_account():
    cid = make_campaign()
    admin = client_for("admin@test.com")
    a = client_for("acc@test.com", tiktok="cuenta1")
    post(a, f"/campana/{cid}", url=tt("cuenta1", 1))
    with A.app.app_context():
        acc = A.CampaignAccount.query.filter_by(handle="cuenta1").one(); aid = acc.id
    r = post(a, f"/campana/{cid}/cuenta/{aid}/quitar")
    assert "en marcha" in r.get_data(as_text=True)                     # tiene un clip contando: no se deja
    with A.app.app_context():
        assert A.db.session.get(A.CampaignAccount, aid)
    assert post(client_for("x@test.com"), f"/campana/{cid}/cuenta/{aid}/quitar").status_code == 404
    post(admin, f"/admin/cuenta-campana/{aid}/quitar")                  # el equipo sí puede
    with A.app.app_context():
        assert A.db.session.get(A.CampaignAccount, aid) is None


def test_bunny_upload_signature(monkeypatch):
    monkeypatch.setattr(A, "BUNNY_LIBRARY_ID", "123")
    monkeypatch.setattr(A, "BUNNY_API_KEY", "key")
    monkeypatch.setattr(A, "bunny_call", lambda m, p, **k: (True, 200, {"guid": "g-1"}))
    admin = client_for("admin@test.com")
    post(admin, "/admin/formacion", title="C")
    with A.app.app_context():
        ch = A.Chapter.query.one()
        A.db.session.add(A.Lesson(chapter_id=ch.id, title="L"))
        A.db.session.commit()
        lid = A.Lesson.query.one().id
    d = admin.post(f"/admin/formacion/leccion/{lid}/video", data={"_csrf": "t"}).get_json()
    import hashlib
    assert d["guid"] == "g-1" and d["signature"] == hashlib.sha256(f"123key{d['expire']}g-1".encode()).hexdigest()
    assert "key" not in str({k: v for k, v in d.items() if k != "signature"})


def test_report_has_best_clips_and_print():
    cid = make_campaign()
    admin = client_for("admin@test.com")
    a = client_for("a@test.com", tiktok="ana")
    post(a, f"/campana/{cid}", url=tt("ana", 1))
    with A.app.app_context():
        s = A.Submission.query.one()
        s.views = 5000
        A.db.session.commit()
        sid, tok = s.id, get(A.Campaign, cid).report_token
    post(admin, f"/admin/envio/{sid}/aprobar")
    page = A.app.test_client().get(f"/informe/{tok}").get_data(as_text=True)
    assert "Lo mejor de la campaña" in page and "data-print" in page and "tiktok.com/embed/v2/" in page


def test_campaign_hub_channels_for_members():
    cid = make_campaign(rules="Menciona a @marca", ideas="Reacción", announcements="12/11 | Nuevo material")
    outsider = client_for("b@test.com", verify=False)
    page = outsider.get(f"/campana/{cid}").get_data(as_text=True)
    assert 'class="hub"' not in page and "Solicitar entrar" in page
    a = client_for("a@test.com", tiktok="ana")
    _auto_accept()
    page = a.get(f"/campana/{cid}").get_data(as_text=True)
    assert 'class="hub"' in page and "Anuncios" in page and "Cuentas TOP" in page
    with A.app.app_context():
        chans = {c.name: c.id for c in A.Channel.query.filter_by(campaign_id=cid)}
    assert "Nuevo material" in a.get(f"/campana/{cid}?canal={chans['Anuncios']}").get_data(as_text=True)
    assert "Reacción" in a.get(f"/campana/{cid}?canal={chans['Ideas virales']}").get_data(as_text=True)
    assert "Menciona a @marca" in a.get(f"/campana/{cid}?canal={chans['Normas']}").get_data(as_text=True)


def test_admin_channels_posts_likes_comments_and_whatsapp():
    cid = make_campaign()
    admin = client_for("admin@test.com")
    a = client_for("a@test.com", tiktok="ana")
    _auto_accept()
    admin.get(f"/admin/campana/{cid}/canales")
    with A.app.app_context():
        sec = A.ChannelSection.query.filter_by(campaign_id=cid).order_by(A.ChannelSection.position).first().id
    post(admin, f"/admin/campana/{cid}/canales", act="canal_nuevo", section=str(sec), name="Cuentas que molan", emoji="🔥")
    with A.app.app_context():
        ch = A.Channel.query.filter_by(name="Cuentas que molan").one().id
    post(admin, f"/admin/campana/{cid}/canales?canal={ch}", act="publicar", canal=str(ch), body="Mirad https://www.tiktok.com/@pablo", notify="on")
    post(admin, f"/admin/campana/{cid}/canales", act="whatsapp", whatsapp_url="https://chat.whatsapp.com/AbC123xyz")
    page = a.get(f"/campana/{cid}?canal={ch}").get_data(as_text=True)
    assert 'href="https://www.tiktok.com/@pablo"' in page and "chat.whatsapp.com/AbC123xyz" in page
    with A.app.app_context():
        pid = A.Post.query.one().id
        ana = A.User.query.filter_by(email="a@test.com").one()
        assert A.Notice.query.filter_by(user_id=ana.id, kind="anuncio").count() == 1
    post(a, f"/campana/{cid}/publicacion/{pid}/megusta")
    post(a, f"/campana/{cid}/publicacion/{pid}/comentar", body="¡Gracias!")
    with A.app.app_context():
        assert A.PostLike.query.count() == 1 and A.PostComment.query.one().body == "¡Gracias!"
    outsider = client_for("b@test.com", verify=False)
    assert outsider.post(f"/campana/{cid}/publicacion/{pid}/megusta", data={"_csrf": "t"}).status_code == 404


def test_campaign_admin_tabs_and_menu():
    cid = make_campaign()
    admin = client_for("admin@test.com")
    a = client_for("a@test.com", tiktok="ana", verify=False)
    post(a, f"/campana/{cid}/unirme")
    for path in ["", "/revisar", "/solicitudes", "/clippers", "/canales", "/editar"]:
        r = admin.get(f"/admin/campana/{cid}{path}")
        assert r.status_code == 200 and "cn-tabs" in r.get_data(as_text=True), path
    menu = admin.get("/admin").get_data(as_text=True)
    assert ">Clips por revisar<" not in menu and "/admin/campana/%d/solicitudes" % cid in menu

def test_demo_data_loads_and_removes_cleanly():
    cid = make_campaign(budget=5000, min_tiktok=0.5, join_questions="¿Cuánto llevas?")
    admin = client_for("admin@test.com")
    msg = flashes(post(admin, "/admin/ajustes/demo", accion="cargar"))
    assert msg and "Demo cargada" in msg[0][1]
    with A.app.app_context():
        c = get(A.Campaign, cid)
        assert c.status == "activa" and 0 < c.spent <= 5000 * .56
        assert A.ViewSnapshot.query.count() > 50 and A.Submission.query.filter_by(status="aprobado").count() > 5
    assert "Ranking" in admin.get("/ranking").get_data(as_text=True)
    assert admin.get("/admin").status_code == 200 and admin.get(f"/admin/campana/{cid}").status_code == 200
    post(admin, "/admin/ajustes/demo", accion="quitar")
    with A.app.app_context():
        assert A.Submission.query.count() == 0 and A.User.query.count() == 1
        assert get(A.Campaign, cid).spent == 0


def test_demo_and_notify_cards_visible_in_production(monkeypatch):
    monkeypatch.setattr(A, "ALLOW_WIPE", False)
    page = client_for("admin@test.com").get("/admin/ajustes").get_data(as_text=True)
    assert "Cargar datos de demostración" in page and "Avisos a los clippers" in page


def test_tech_panel_only_for_tech_email(monkeypatch):
    monkeypatch.setattr(A, "TECH_EMAILS", {"yo@test.com"})
    monkeypatch.setattr(A, "ADMIN_EMAILS", {"admin@test.com", "yo@test.com"})
    felipe = client_for("admin@test.com")
    yo = client_for("yo@test.com")
    assert felipe.get("/admin/ajustes").status_code == 403
    assert felipe.get("/admin/actividad").status_code == 403
    assert "Panel técnico" not in felipe.get("/admin").get_data(as_text=True)
    assert "Avisos técnicos" in yo.get("/admin/ajustes").get_data(as_text=True)
    assert felipe.get("/admin/creadores").status_code == 403


def test_facebook_is_off_by_default_but_works_when_enabled(monkeypatch):
    admin = client_for("admin@test.com")
    page = admin.get("/admin/campana/nueva").get_data(as_text=True)
    assert 'name="pf_facebook"' in page and 'name="pf_facebook" checked' not in page
    cid = make_campaign(platforms="tiktok,facebook")
    a = client_for("a@test.com", facebook="mipagina")
    r = post(a, f"/campana/{cid}", url="https://www.facebook.com/reel/1234567890123")
    assert "Clip enviado" in r.get_data(as_text=True)
    with A.app.app_context():
        s = A.Submission.query.one()
        assert s.platform == "facebook" and s.video_key == "facebook:1234567890123"
    assert V.parse_item("facebook", {"viewsCount": 900, "likes": 12, "time": "2026-11-01T10:00:00Z"})["views"] == 900


def test_home_feature_announcement_and_campaign():
    cid = make_campaign(song="Campaña Uno")
    admin = client_for("admin@test.com")
    a = client_for("a@test.com")
    post(admin, "/admin/destacado", mode="announce", title="Nueva forma de cobrar", body="Ya puedes cobrar al momento.",
         btn_text="Ver más", btn_url="/ganancias", notify="on")
    home = a.get("/campanas").get_data(as_text=True)
    assert "Nueva forma de cobrar" in home and 'class="featured home-ann"' in home
    with A.app.app_context():
        assert A.Notice.query.filter_by(kind="destacado").count() >= 1
    post(admin, "/admin/destacado", mode="campaign", campaign_id=str(cid))
    home = a.get("/campanas").get_data(as_text=True)
    assert "home-ann" not in home and "Campaña destacada" in home
    post(admin, "/admin/destacado", mode="none")
    assert "Campaña destacada" not in a.get("/campanas").get_data(as_text=True)


def test_review_cards_filters_and_video_copy(monkeypatch):
    cid = make_campaign()
    admin = client_for("admin@test.com")
    a = client_for("a@test.com", tiktok="ana", instagram="ana")
    post(a, f"/campana/{cid}", url=tt("ana", 1))
    post(a, f"/campana/{cid}", url="https://www.instagram.com/reel/ABCdef123/")
    with A.app.app_context():
        s1, s2 = A.Submission.query.order_by(A.Submission.id).all()
        s1.review_since = datetime.utcnow() - timedelta(days=3)
        s2.media_url = "https://cdn.example.com/v.mp4"
        A.db.session.commit()
        sid2 = s2.id
    page = admin.get("/admin/clips?f=atrasados").get_data(as_text=True)
    assert "atrasado" in page and page.count("data-card ") + page.count("data-card\n") >= 0 and page.count('class="cr-card"') == 1
    assert admin.get("/admin/clips?f=instagram").get_data(as_text=True).count('class="cr-card"') == 1
    assert admin.get("/admin/clips?q=canci%C3%B3n").get_data(as_text=True).count('class="cr-card"') == 2
    # Copia del vídeo en Bunny
    monkeypatch.setattr(A, "BUNNY_LIBRARY_ID", "1")
    monkeypatch.setattr(A, "BUNNY_API_KEY", "k")
    calls = []
    def fake_bunny(m, path, **k):
        calls.append((m, path))
        return (True, 200, {"guid": "g-9"}) if m == "POST" else (True, 200, {"status": 4})
    monkeypatch.setattr(A, "bunny_call", fake_bunny)
    monkeypatch.setattr(V, "fetch_download_urls", lambda p, urls: {_vid(1): "https://cdn.example.com/t.mp4"})
    class R:
        status_code = 200
        content = b"x" * 20000
    monkeypatch.setattr(A.requests, "get", lambda *a_, **k: R())
    monkeypatch.setattr(A.requests, "put", lambda *a_, **k: type("P", (), {"status_code": 200})())
    with A.app.app_context():
        assert A.copy_review_videos() == 2
        for s_ in A.Submission.query.all():          # la prueba de estadísticas ya está subida
            s_.stats_status = "enviado"
        A.db.session.commit()
        s2 = get(A.Submission, sid2)
        assert s2.video_guid == "g-9" and s2.video_status == "listo"
    assert "iframe.mediadelivery.net/embed/1/g-9" in admin.get("/admin/clips").get_data(as_text=True)




def test_whatsapp_general_group_on_home():
    admin = client_for("admin@test.com")
    a = client_for("a@test.com", tiktok="ana")
    post(admin, "/admin/destacado", act="whatsapp", whatsapp_url="https://evil.com/x")
    home = a.get("/").get_data(as_text=True)
    assert "evil.com" not in home and "grupo-whatsapp" not in home
    post(admin, "/admin/destacado", act="whatsapp", whatsapp_url="https://chat.whatsapp.com/Grupo123?mode=ems_copy_t")
    page = a.get("/").get_data(as_text=True)
    assert "/grupo-whatsapp" in page and "Únete al grupo de WhatsApp" in page
    r = a.get("/grupo-whatsapp")
    assert r.status_code == 302 and r.headers["Location"].startswith("https://chat.whatsapp.com/Grupo123")
    with A.app.app_context():
        assert A.User.query.filter_by(email="a@test.com").one().wa_joined_at is not None
    post(admin, "/admin/destacado", act="whatsapp", whatsapp_url="")
    assert "grupo-whatsapp" not in a.get("/").get_data(as_text=True)


def test_hub_sections_only_for_members_with_rules_preview():
    cid = make_campaign(rules="Menciona @artista en cada vídeo")
    client_for("admin@test.com")
    out = client_for("fuera@test.com", verify=False)
    page = out.get(f"/campana/{cid}").get_data(as_text=True)
    assert "Menciona @artista" in page and "hub-side" not in page and 'tab=recursos' not in page
    assert "hub-ch" not in out.get(f"/campana/{cid}?tab=recursos").get_data(as_text=True)
    a = client_for("a@test.com", tiktok="ana")
    _auto_accept()
    assert "hub-ch" in a.get(f"/campana/{cid}").get_data(as_text=True)


def test_earnings_grouped_by_campaign_and_review_lives_in_my_clips():
    cid = make_campaign(song="Neón")
    a = client_for("a@test.com", tiktok="ana")
    post(a, f"/campana/{cid}", url=tt("ana", 1))
    post(a, f"/campana/{cid}", url=tt("ana", 2))
    with A.app.app_context():
        for s_ in A.Submission.query.all():
            s_.status, s_.views, s_.earned = "aprobado", 10000, 2.0
        A.db.session.commit()
    page = a.get("/ganancias").get_data(as_text=True)
    assert "Por campaña" in page and "Neón" in page and "4,00" in page
    r = a.get("/ganancias?tab=pendiente")
    assert r.status_code == 302 and "/mis-clips" in r.headers["Location"]
    assert "Visualizaciones por plataforma" in a.get("/mis-clips").get_data(as_text=True)


def test_stats_video_required_blocks_approval_and_expires(monkeypatch):
    monkeypatch.setattr(A, "BUNNY_LIBRARY_ID", "lib")
    monkeypatch.setattr(A, "BUNNY_API_KEY", "key")
    monkeypatch.setattr(A, "bunny_call", lambda m, p, **k: (True, 200, {"guid": "st-1"}))
    cid = make_campaign(stats_note="60 % España")
    admin = client_for("admin@test.com")
    a = client_for("a@test.com", tiktok="ana")
    post(a, f"/campana/{cid}", url=tt("ana", 1))
    with A.app.app_context():
        s_ = A.Submission.query.one()
        s_.views, s_.review_since = 50000, datetime.utcnow()
        A.db.session.commit()
        sid = s_.id
        A.auto_review(A.db.session.get(A.Submission, sid))
        A.db.session.commit()
        assert A.db.session.get(A.Submission, sid).stats_status == "pedido"
        assert A.clip_stage(A.db.session.get(A.Submission, sid)) == "estadisticas"
    page = a.get("/mis-clips?tab=estadisticas").get_data(as_text=True)
    assert "60 % España" in page and "Subir vídeo" in page
    assert "falta que el clipper verifique" in flashes(post(admin, f"/admin/envio/{sid}/aprobar"))[0][1]
    d = a.post(f"/mis-clips/{sid}/estadisticas", data={"_csrf": "t"}).get_json()
    assert d["ok"] and d["guid"] == "st-1"
    a.post(f"/mis-clips/{sid}/estadisticas/listo", data={"_csrf": "t"})
    with A.app.app_context():
        assert A.clip_stage(A.db.session.get(A.Submission, sid)) == "revisar"
    assert "Verificación de vistas que subió" in admin.get(f"/admin/campana/{cid}/revisar").get_data(as_text=True)
    post(admin, f"/admin/envio/{sid}/aprobar")
    with A.app.app_context():
        assert A.db.session.get(A.Submission, sid).status == "aprobado"
    # otro clip que no sube el vídeo a tiempo se rechaza solo
    post(a, f"/campana/{cid}", url=tt("ana", 2))
    with A.app.app_context():
        s2 = A.Submission.query.filter(A.Submission.id != sid).one()
        s2.views, s2.review_since, s2.stats_status = 50000, datetime.utcnow() - timedelta(hours=80), "pedido"
        s2.stats_asked_at = datetime.utcnow() - timedelta(hours=80)
        A.db.session.commit()
        assert A.auto_review(s2) == "rechazado"
        assert s2.reject_code == "sin_estadisticas"
    other = client_for("b@test.com", tiktok="bea")
    assert other.post(f"/mis-clips/{sid}/estadisticas", data={"_csrf": "t"}).status_code == 404


def test_campaign_mods_permissions():
    cid = make_campaign()
    cid2 = make_campaign(song="Otra")
    admin = client_for("admin@test.com")
    mod = client_for("mod@test.com", tiktok="modo")
    a = client_for("a@test.com", tiktok="ana")
    post(a, f"/campana/{cid}", url=tt("ana", 1))
    post(mod, f"/campana/{cid}", url=tt("modo", 2))
    assert mod.get(f"/admin/campana/{cid}/revisar").status_code == 403
    post(admin, f"/admin/campana/{cid}/mods", act="poner", email="mod@test.com")
    with A.app.app_context():
        assert A.CampaignMod.query.count() == 1
        mine = A.Submission.query.join(A.User).filter(A.User.email == "mod@test.com").one().id
        other = A.Submission.query.join(A.User).filter(A.User.email == "a@test.com").one().id
    page = mod.get(f"/admin/campana/{cid}/revisar").get_data(as_text=True)
    assert mod.get(f"/admin/campana/{cid}/revisar").status_code == 200 and "Ajustes" not in page.split("cn-tabs")[1][:600]
    assert mod.get(f"/admin/campana/{cid2}/revisar").status_code == 403
    assert mod.get(f"/admin/campana/{cid}").status_code == 403            # resumen con dinero: no
    assert mod.get(f"/admin/campana/{cid}/editar").status_code == 403
    assert mod.get("/admin/retiros").status_code == 403
    assert "propios clips" in flashes(post(mod, f"/admin/envio/{mine}/aprobar"))[0][1]
    assert mod.post(f"/admin/envio/{other}/visitas", data={"_csrf": "t", "views": "999999"}).status_code == 403
    post(mod, f"/admin/envio/{other}/aprobar")
    with A.app.app_context():
        assert A.db.session.get(A.Submission, other).status == "aprobado"
        assert A.db.session.get(A.Submission, mine).status == "pendiente"
    # canales: publica, pero no cambia secciones ni el grupo
    mod.get(f"/admin/campana/{cid}/canales")
    with A.app.app_context():
        ch = A.Channel.query.filter_by(campaign_id=cid, kind="posts").first().id
    post(mod, f"/admin/campana/{cid}/canales?canal={ch}", act="publicar", canal=str(ch), body="Hola desde el mod")
    assert mod.post(f"/admin/campana/{cid}/canales", data={"_csrf": "t", "act": "whatsapp", "whatsapp_url": ""}).status_code == 403
    _auto_accept()
    hub = a.get(f"/campana/{cid}?canal={ch}").get_data(as_text=True)
    assert "Hola desde el mod" in hub and "MOD" in hub
    post(admin, f"/admin/campana/{cid}/mods", act="quitar", email="mod@test.com")
    assert mod.get(f"/admin/campana/{cid}/revisar").status_code == 403


def test_bell_groups_by_day_with_campaign_cover():
    cid = make_campaign(song="Neón", artist="Luna", budget=5000, rate=200)
    a = client_for("a@test.com", tiktok="ana")
    with A.app.app_context():
        u = A.User.query.filter_by(email="a@test.com").one()
        A.db.session.add(A.Notice(user_id=u.id, kind="campana_nueva", title="Nueva campaña de Luna",
                                  body=A.campaign_hook(A.db.session.get(A.Campaign, cid)), url=f"/campana/{cid}", campaign_id=cid))
        A.db.session.add(A.Notice(user_id=u.id, kind="retiro", title="Retiro pagado", body="x", url="/ganancias",
                                  created_at=datetime.utcnow() - timedelta(days=1)))
        A.db.session.commit()
        hook = A.campaign_hook(A.db.session.get(A.Campaign, cid))
    assert "Gana tu parte de 5.000" in hook and "500.000 vistas" in hook
    page = a.get("/").get_data(as_text=True)
    assert page.index(">Hoy<") < page.index(">Ayer<") and "Nueva campaña de Luna" in page


def test_home_dashboard_steps_week_clips_and_ranking():
    cid = make_campaign(song="Neón")
    admin = client_for("admin@test.com")
    post(admin, "/admin/destacado", act="whatsapp", whatsapp_url="https://chat.whatsapp.com/G1")
    a = client_for("a@test.com", tiktok="ana", verify=False)
    page = a.get("/").get_data(as_text=True)
    assert "Empieza a ganar en 3 pasos" in page and "Ver formación" in page and "Aplica a una campaña" in page and "para repartir en campañas activas" in page
    with A.app.app_context():
        c = A.Course(title="Cómo clipear desde cero", published=True)
        A.db.session.add(c); A.db.session.flush()
        ch = A.Chapter(course_id=c.id, title="Inicio"); A.db.session.add(ch); A.db.session.flush()
        A.db.session.add(A.Lesson(chapter_id=ch.id, title="L1", body="x"))
        A.db.session.commit()
        A.rename_beginner_course()
        assert A.Course.query.one().title == "Guía para principiantes"
    page = a.get("/").get_data(as_text=True)
    assert "en 3 pasos" in page and "Mira la guía para principiantes" in page and "Ir a formación" in page
    post(admin, "/admin/destacado", act="whatsapp", whatsapp_url="")
    assert "Próximamente" in a.get("/").get_data(as_text=True)
    b = client_for("b@test.com", tiktok="bea")
    post(b, f"/campana/{cid}", url=tt("bea", 1))
    with A.app.app_context():
        s_ = A.Submission.query.one()
        s_.status, s_.views, s_.earned = "aprobado", 50000, 10.0
        A.db.session.commit()
    page = a.get("/").get_data(as_text=True)
    assert "Clips de la semana" in page and "10,00" in page
    assert "Tu puesto" in page
    assert "Neón" in a.get("/campanas").get_data(as_text=True)


def test_stats_redo_request_and_fresh_deadline(monkeypatch):
    monkeypatch.setattr(A, "BUNNY_LIBRARY_ID", "lib")
    monkeypatch.setattr(A, "BUNNY_API_KEY", "key")
    cid = make_campaign()
    admin = client_for("admin@test.com")
    a = client_for("a@test.com", tiktok="ana")
    post(a, f"/campana/{cid}", url=tt("ana", 1))
    with A.app.app_context():
        s_ = A.Submission.query.one()
        s_.views, s_.review_since = 50000, datetime.utcnow() - timedelta(days=10)   # ya estaba en revisión antes del cambio
        A.db.session.commit()
        sid = s_.id
        assert A.auto_review(s_) == ""                 # no se rechaza de golpe: el plazo empieza ahora
        assert s_.stats_status == "pedido" and s_.stats_asked_at
        s_.stats_status, s_.stats_guid = "enviado", "g"
        A.db.session.commit()
    post(admin, f"/admin/envio/{sid}/pedir_estadisticas", reason="No se ven los países")
    with A.app.app_context():
        s_ = A.db.session.get(A.Submission, sid)
        assert s_.stats_status == "pedido" and s_.stats_reason == "No se ven los países" and s_.status == "pendiente"
    assert "No se ven los países" in a.get("/mis-clips?tab=estadisticas").get_data(as_text=True)


def test_campaign_support_chat_with_mods():
    cid = make_campaign(song="Neón")
    admin = client_for("admin@test.com")
    a = client_for("a@test.com", tiktok="ana")
    mod = client_for("mod@test.com", tiktok="modo")
    out = client_for("fuera@test.com", verify=False)
    _auto_accept()
    post(admin, f"/admin/campana/{cid}/mods", act="poner", email="mod@test.com")
    assert "Soporte" in a.get(f"/campana/{cid}").get_data(as_text=True)
    post(a, f"/campana/{cid}/soporte", body="¿Vale el audio original?")
    assert out.post(f"/campana/{cid}/soporte", data={"_csrf": "t", "body": "hola"}).status_code == 403
    with A.app.app_context():
        uid = A.User.query.filter_by(email="a@test.com").one().id
        mod_id = A.User.query.filter_by(email="mod@test.com").one().id
        assert A.Notice.query.filter_by(user_id=mod_id, kind="mensaje").count() == 1
        assert A.pending_by_campaign()[cid]["support"] == 1
    page = mod.get(f"/admin/campana/{cid}/soporte?u={uid}").get_data(as_text=True)
    assert "¿Vale el audio original?" in page
    post(mod, f"/admin/campana/{cid}/soporte", u=str(uid), body="Sí, usa el original")
    with A.app.app_context():
        assert A.pending_by_campaign().get(cid, {}).get("support", 0) == 0
        assert A.Notice.query.filter_by(user_id=uid, kind="mensaje").count() == 1
    hub = a.get(f"/campana/{cid}?soporte=1").get_data(as_text=True)
    assert "Sí, usa el original" in hub and "MOD" in hub
    assert admin.get(f"/admin/campana/{cid}/soporte").status_code == 200


def test_admin_sees_campaign_as_outsider_unless_hub():
    cid = make_campaign()
    admin = client_for("admin@test.com")
    page = admin.get(f"/campana/{cid}").get_data(as_text=True)
    assert "hub-ch" not in page and "Ver el hub" in page
    assert "hub-ch" in admin.get(f"/campana/{cid}?hub=1").get_data(as_text=True)


def test_discover_clips_from_profiles_and_daily_reminder(monkeypatch):
    cid = make_campaign(song="Bali")
    a = client_for("a@test.com", tiktok="clipsdepablo")
    _auto_accept()
    with A.app.app_context():
        acc = A.CampaignAccount.query.one()
        acc.decided_at = datetime.utcnow() - timedelta(days=2)
        A.db.session.commit()
    now = datetime.utcnow()
    items = [
        {"url": "https://www.tiktok.com/@clipsdepablo/video/7500000000000000001", "vid": "7500000000000000001",
         "author": "clipsdepablo", "views": 1200, "likes": 30, "comments": 2, "shares": 1, "posted_at": now - timedelta(hours=3), "video_url": ""},
        {"url": "https://www.tiktok.com/@clipsdepablo/video/7500000000000000002", "vid": "7500000000000000002",
         "author": "clipsdepablo", "views": 999, "likes": 1, "comments": 0, "shares": 0, "posted_at": now - timedelta(days=10), "video_url": ""},
    ]
    monkeypatch.setattr(A.viewcounter, "APIFY_TOKEN", "x")
    monkeypatch.setattr(A.viewcounter, "fetch_profile_videos", lambda p, handles, per=12: items if p == "tiktok" else [])
    with A.app.app_context():
        added, errors = A.discover_clips()
        assert added == 1 and not errors                       # el antiguo (de antes de entrar) no cuenta
        s_ = A.Submission.query.one()
        assert s_.source == "auto" and s_.views == 1200 and s_.campaign_id == cid
        assert A.discover_clips()[0] == 0                       # no se duplica
        assert A.inactivity_reminders() == 0                    # publicó hace 3 h: sin aviso
        acc = A.CampaignAccount.query.one()
        acc.last_post_at = datetime.utcnow() - timedelta(hours=30)
        A.db.session.commit()
        assert A.inactivity_reminders() == 1
        acc.last_scan_at = None                                 # sin poder mirar el perfil, no se avisa
        A.db.session.commit()
        assert A.inactivity_reminders() == 0
        uid = A.User.query.filter_by(email="a@test.com").one().id
        assert A.Notice.query.filter_by(user_id=uid, kind="inactividad").count() == 1
        # 1:00 en Madrid
        due, _ = A.reminders_due(datetime(2026, 11, 6, 0, 30))   # 00:30 UTC = 01:30 Madrid (invierno)
        assert due
        assert not A.reminders_due(datetime(2026, 11, 6, 9, 0))[0]
    assert "detectado solo" in a.get("/mis-clips").get_data(as_text=True)


def test_account_only_in_one_campaign():
    c1 = make_campaign(song="Uno")
    c2 = make_campaign(song="Dos")
    a = client_for("a@test.com", verify=False)
    with A.app.app_context():
        u = A.User.query.filter_by(email="a@test.com").one()
        for c in (c1, c2):
            A.db.session.add(A.CampaignMember(user_id=u.id, campaign_id=c, status="aceptada"))
        A.db.session.add(A.CampaignAccount(user_id=u.id, campaign_id=c1, platform="tiktok", handle="micuenta", status="aceptada"))
        A.db.session.commit()
    r = post(a, f"/campana/{c2}/cuentas", platform="tiktok", handle="micuenta")
    assert "solo puede estar en una campaña" in r.get_data(as_text=True)


def test_mods_page_and_permissions():
    cid = make_campaign(song="Neón")
    admin = client_for("admin@test.com")
    reviewer = client_for("rev@test.com")
    mod = client_for("mod@test.com", tiktok="modo")
    with A.app.app_context():
        r = A.User.query.filter_by(email="rev@test.com").one(); r.is_admin, r.admin_role = True, "reviewer"; A.db.session.commit()
    assert reviewer.get("/admin/mods").status_code == 403          # solo el admin principal
    post(admin, "/admin/mods", act="anadir", email="mod@test.com", campaigns=[str(cid)], perms=["soporte", "canales"])
    with A.app.app_context():
        row = A.CampaignMod.query.one()
        assert row.perm_set == {"soporte", "canales"}
        mid = row.id
    assert mod.get(f"/admin/campana/{cid}/revisar").status_code == 403
    assert mod.get(f"/admin/campana/{cid}/soporte").status_code == 200
    page = admin.get("/admin/mods").get_data(as_text=True)
    assert "mod@test.com" in page and f"2 de {len(A.MOD_PERMS)} permisos" in page
    post(admin, "/admin/mods", act="permisos", mid=str(mid), perms=list(A.MOD_PERMS))
    assert mod.get(f"/admin/campana/{cid}/revisar").status_code == 200
    post(admin, "/admin/mods", act="quitar", mid=str(mid))
    assert mod.get(f"/admin/campana/{cid}/soporte").status_code == 403
    clip_page = admin.get("/admin/clipper/%d" % 1)
    assert clip_page.status_code in (200, 404)


def test_whop_daily_cap_waits_for_admin(monkeypatch):
    """Por encima del tope diario de pagos automáticos, el retiro espera a un admin."""
    c, admin = _clipper_with_balance(monkeypatch)
    calls = _whop_fake(monkeypatch)
    monkeypatch.setattr(A, "WHOP_AUTO_DAILY", 40.0)
    post(c, "/cuenta", section="cobro", payout_method=A.WHOP_METHOD, whop_username="ana")
    post(c, "/ganancias/retirar")
    assert not [x for x in calls if "/transfers" in x[1]]
    with A.app.app_context():
        assert A.Payout.query.one().status == "solicitado"


def test_default_whop_cap_is_set():
    assert A.WHOP_AUTO_MAX > 0 and A.WHOP_AUTO_DAILY > 0      # nunca pagos automáticos sin tope por defecto


def test_bio_check_state_endpoint():
    cid = make_campaign()
    c = client_for("bio@test.com")
    with A.app.app_context():
        u = A.User.query.filter_by(email="bio@test.com").one()
        acc = A.CampaignAccount(user_id=u.id, campaign_id=cid, platform="tiktok", handle="biocheck",
                                status="pendiente", bio_status="Comprobando…", bio_checked_at=datetime.utcnow())
        A.db.session.add(acc); A.db.session.commit(); aid = acc.id
    assert c.get(f"/campana/{cid}/cuenta/{aid}/estado").get_json() == {"checking": True, "verified": False}
    with A.app.app_context():
        acc = A.db.session.get(A.CampaignAccount, aid)
        acc.bio_status, acc.bio_verified = "Verificada", True; A.db.session.commit()
    assert c.get(f"/campana/{cid}/cuenta/{aid}/estado").get_json() == {"checking": False, "verified": True}
    assert client_for("otro@test.com").get(f"/campana/{cid}/cuenta/{aid}/estado").status_code == 404


def test_mod_extra_permissions():
    """Los mods antiguos conservan los 6 de siempre; los permisos nuevos van desmarcados y se activan uno a uno."""
    cid = make_campaign(song="Permisos", budget=1000)
    admin = client_for("admin@test.com")
    mod = client_for("mod2@test.com", tiktok="modo2")
    with A.app.app_context():
        u = A.User.query.filter_by(email="mod2@test.com").one()
        row = A.CampaignMod(campaign_id=cid, user_id=u.id, perms="")      # mod de antes del cambio
        A.db.session.add(row); A.db.session.commit(); mid = row.id
        assert row.perm_set == set(A.MOD_DEFAULT)
    for path in ("", "/clippers", "/editar", "/clips.csv"):
        assert mod.get(f"/admin/campana/{cid}{path}").status_code == 403
    assert post(mod, f"/admin/campana/{cid}/estado", accion="pausar").status_code == 403
    post(admin, "/admin/mods", act="permisos", mid=str(mid), perms=list(A.MOD_DEFAULT) + ["estadisticas", "clippers", "editar", "csv"])
    page = mod.get(f"/admin/campana/{cid}").get_data(as_text=True)
    assert "Añadir presupuesto" not in page and "Informe para el creador" not in page
    assert mod.get(f"/admin/campana/{cid}/clippers").status_code == 200
    assert mod.get(f"/admin/campana/{cid}/clips.csv").status_code == 200
    form = mod.get(f"/admin/campana/{cid}/editar").get_data(as_text=True)
    assert "Solo lectura" in form and "Cerrar campaña" not in form
    # edita las normas e intenta subir el presupuesto: las normas cambian, el dinero no
    post(mod, f"/admin/campana/{cid}/editar", song="Permisos", artist="Rafa", rate_k="9", budget="99999",
         pf_tiktok="on", status="finalizada", rules="Normas nuevas", wizard="1")
    with A.app.app_context():
        camp = A.db.session.get(A.Campaign, cid)
        assert camp.budget == 1000 and camp.status == "activa" and "Normas nuevas" in (camp.rules or "")
    assert post(mod, f"/admin/campana/{cid}/estado", accion="presupuesto", amount="50").status_code == 403
    post(admin, "/admin/mods", act="permisos", mid=str(mid), perms=list(A.MOD_DEFAULT) + ["estado"])
    post(mod, f"/admin/campana/{cid}/estado", accion="pausar")
    with A.app.app_context():
        assert A.db.session.get(A.Campaign, cid).status == "pausada"


def test_lesson_counts_only_real_watching():
    """Saltar al final no cuenta, y no se puede «ver» más rápido de lo que pasa el tiempo."""
    admin = client_for("admin@test.com")
    post(admin, "/admin/formacion", title="Curso")
    with A.app.app_context():
        c = A.Course.query.one(); c.published = True
        ls = A.Lesson(chapter_id=c.chapters[0].id, title="Vídeo", video_url="https://youtu.be/abcdefghijk", duration=600)
        A.db.session.add(ls); A.db.session.commit(); lid = ls.id
    a = client_for("a@test.com")
    url = f"/formacion/leccion/{lid}/progreso"
    d = a.post(url, data={"_csrf": "t", "ranges": "540-600"}, headers={"X-Requested-With": "fetch"}).get_json()
    assert d == {"ok": True, "done": False, "pct": 10}
    d = a.post(url, data={"_csrf": "t", "ranges": "0-540"}, headers={"X-Requested-With": "fetch"}).get_json()
    assert d["done"] is False and d["pct"] == 10                                   # 9 minutos «vistos» en un segundo: no
    with A.app.app_context():
        row = A.LessonProgress.query.one(); row.updated_at = datetime.utcnow() - timedelta(minutes=10); A.db.session.commit()
    d = a.post(url, data={"_csrf": "t", "ranges": "0-540"}, headers={"X-Requested-With": "fetch"}).get_json()
    assert d["done"] is True and d["pct"] == 100
    with A.app.app_context():
        assert A.parse_ranges("0-10,5-20,30-40,999-2000", 600) == [(0, 20), (30, 40)]


def test_onboarding_other_country():
    a = client_for("otro@test.com")
    r = post(a, "/bienvenida?paso=2", country="OT")
    assert "Escribe en qué país vives" in r.get_data(as_text=True)
    post(a, "/bienvenida?paso=2", country="OT", country_other="  Alemania ")
    with A.app.app_context():
        u = A.User.query.filter_by(email="otro@test.com").one()
        assert (u.country, u.country_other, u.country_name) == ("OT", "Alemania", "Alemania")
    post(a, "/bienvenida?paso=2", country="ES", country_other="Alemania")
    with A.app.app_context():
        u = A.User.query.filter_by(email="otro@test.com").one()
        assert (u.country_other, u.country_name) == ("", "España")


def test_my_clips_panel_filters_and_series():
    cid = make_campaign()
    a = client_for("a@test.com", tiktok="ana")
    post(a, f"/campana/{cid}", url=tt("ana", 1))
    post(a, f"/campana/{cid}", url=tt("ana", 2))
    with A.app.app_context():
        u = A.User.query.filter_by(email="a@test.com").one()
        subs = A.Submission.query.all()
        subs[0].views, subs[0].status, subs[0].earned, subs[0].reviewed_at = 9000, "aprobado", 4.5, datetime.utcnow()
        A.db.session.commit()
        an = A.clipper_analytics(list(u.submissions), 7)
        assert an["totals"]["sent"] == 2 and an["totals"]["approved"] == 1 and an["totals"]["earned"] == 4.5
        assert len(an["series"]["labels"]) == 7 and an["series"]["cumulative"]["sent"][-1] == 2
        assert an["series"]["cumulative"]["views"][-1] == 9000 and an["by_platform"][0]["key"] == "tiktok"
    page = a.get("/mis-clips?dias=7&orden=vistas").get_data(as_text=True)
    assert "Visualizaciones por plataforma" in page and "Cobrando" in page and "data-chart-smooth" in page
    sid = subs_id = None
    with A.app.app_context():
        sid = A.Submission.query.filter_by(status="aprobado").one().id
    assert f"#{sid}" in a.get(f"/mis-clips?q=%23{sid}").get_data(as_text=True)
    assert "Nada por aquí" in a.get("/mis-clips?q=noexiste").get_data(as_text=True)
    assert "No hay datos que ver" in a.get("/mis-clips?red=youtube").get_data(as_text=True)
