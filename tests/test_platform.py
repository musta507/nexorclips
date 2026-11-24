import re
"""Pruebas automáticas de ClipProfit Hispano. Ejecutar con:  python -m pytest -q"""
import io
import os
import sys
import tempfile
from datetime import datetime, timedelta

DB = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ.setdefault("SEARCH_ENGINE", "apify")
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


def client_for(email, verify=True, onboarded=True, **handles):
    c = A.app.test_client()
    with c.session_transaction() as s:
        s["_csrf"] = "t"
    c.post("/login/dev", data={"email": email, "_csrf": "t"})
    if onboarded:                                    # casi todas las pruebas empiezan con el perfil ya hecho
        with A.app.app_context():
            u = A.User.query.filter_by(email=email).first()
            if u and not u.onboarded_at:
                u.onboarded_at = A.datetime.utcnow(); A.db.session.commit()
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


def test_no_se_pueden_corregir_vistas_a_mano():
    """Mustapha no quiere corregir vistas a mano en ninguna parte: ni botón, ni permiso de mod, ni ruta."""
    cid = make_campaign(rate=100, budget=1000)
    admin = client_for("admin@test.com")
    a = client_for("a@test.com", tiktok="ana")
    post(a, f"/campana/{cid}", url=tt("ana", 1))
    approve_all(admin)
    with A.app.app_context():
        sid = A.Submission.query.one().id
        before = get(A.Submission, sid).views
    assert admin.post(f"/admin/envio/{sid}/visitas", data={"_csrf": "t", "views": "2000000"}).status_code == 404
    with A.app.app_context():
        assert get(A.Submission, sid).views == before
    assert "vistas" not in A.MOD_PERMS
    assert "Corregir vistas" not in admin.get(f"/admin/campana/{cid}/revisar?tab=aprobados").get_data(as_text=True)


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
    assert "Solicitar entrar" in page and "Tus clips se suben solos" not in page and 'name="handle"' not in page
    assert "Aún no estás en ninguna campaña" in a.get("/enviar").get_data(as_text=True)
    # 1) pide entrar: sin cuenta
    html = post(a, f"/campana/{cid}/unirme").get_data(as_text=True)
    assert "Solicitud enviada" in html and "Tus clips se suben solos" not in html
    with A.app.app_context():
        mb = A.CampaignMember.query.one()
        assert mb.status == "pendiente" and not A.CampaignAccount.query.first()
        mid = mb.id
    assert "Solicitud enviada" in a.get("/campanas").get_data(as_text=True)
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
    assert "Solicitud aprobada" in page and 'name="handle"' in page and "Tus clips se suben solos" not in page
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
    assert "Tus clips se suben solos" in a.get(f"/campana/{cid}").get_data(as_text=True)
    assert "Clip enviado" in post(a, f"/campana/{cid}", url=tt("ana", 2)).get_data(as_text=True)
    assert "no es una de tus cuentas aceptadas" in post(a, f"/campana/{cid}", url=tt("otra", 3)).get_data(as_text=True)
    with A.app.app_context():
        sid = A.Submission.query.one().id
        owner = A.Submission.query.one().user_id
    prof = admin.get(f"/admin/clipper/{owner}").get_data(as_text=True)   # en el perfil del clipper no se revisan clips
    assert f"/admin/envio/{sid}/aprobar" not in prof and f"/admin/envio/{sid}/rechazar" not in prof
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
    monkeypatch.setattr(A, "PHONE_ALERTS", True)            # (apagados en la web; aquí se prueba que el código sigue bien)
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
    assert "1,20 $" in html and "por 1.000 vistas" in html and "Mín. 6,00 $ por clip" in html


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
    assert r.status_code == 302 and "/bienvenida" in c2.get("/ganancias").headers["Location"]   # primero, su perfil
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
    assert "10,00 $" in page and "100,00 $" in page and "sin máximo" in page and "hasta" not in page
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
    monkeypatch.setattr(A, "REFRESH_AT", "")
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
    monkeypatch.setattr(A, "creators_on", lambda: True)
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
    monkeypatch.setattr(A, "creators_on", lambda: True)
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
    monkeypatch.setattr(A, "creators_on", lambda: True)
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
    monkeypatch.setattr(A, "creators_on", lambda: True)
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


def test_onboarding_one_step_required_fields():
    a = client_for("a@test.com", verify=False, onboarded=False)
    page = a.get("/bienvenida").get_data(as_text=True)
    assert "Saltar" not in page and "Tu usuario" in page
    r = post(a, "/bienvenida", name="Ana García", username="a b", country="MX")
    assert "de 3 a 24" in r.get_data(as_text=True)
    r = post(a, "/bienvenida", name="Ana García", username="anag")
    assert "Elige tu país" in r.get_data(as_text=True)
    post(a, "/bienvenida", name="Ana García", username="@AnaG", country="MX")
    with A.app.app_context():
        u = A.User.query.filter_by(email="a@test.com").one()
        assert (u.name, u.username, u.country, u.languages) == ("Ana García", "anag", "MX", "es")
        assert u.onboarded_at
    home = a.get("/").get_data(as_text=True)
    assert "Primeros pasos" not in home and "Agrega tu cuenta" not in home
    b = client_for("b@test.com", verify=False)
    r = post(b, "/bienvenida", name="Otra Ana", username="ANAG", country="ES")
    assert "ya está cogido" in r.get_data(as_text=True)


def test_ranking_masks_names():
    cid = make_campaign()
    admin = client_for("admin@test.com")
    a = client_for("a@test.com", tiktok="ana")
    with A.app.app_context():
        A.User.query.filter_by(email="a@test.com").one().avatar_url = "/foto/1"; A.db.session.commit()
    post(a, "/bienvenida", name="Ana García López", username="anagl", country="ES")
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
    monkeypatch.setattr(A, "CLIPPER_PUSH", True)            # (apagado para los clippers; aquí se prueba que el código sigue bien)
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
    page = admin.get("/admin/avisos").get_data(as_text=True)
    assert "a@test.com" in page and "Vista previa" in page
    post(admin, "/admin/avisos", uids=str(u.id), title="Hola {nombre}", body="Hola {nombre}, hay campaña", go="clips")
    with A.app.app_context():
        m = A.Message.query.order_by(A.Message.id.desc()).first()
        assert "hay campaña" in m.body and m.user_id == u.id and m.from_admin


def test_push_subscribe_saves_device(monkeypatch):
    monkeypatch.setattr(A, "CLIPPER_PUSH", True)
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
    assert admin.get("/admin").status_code == 200
    page = admin.get(f"/admin/campana/{cid}").get_data(as_text=True)
    assert "Ubicación" in page and "España" in page
    with A.app.app_context():
        assert A.Post.query.filter_by(demo=True).count() >= 4 and A.Call.query.filter_by(demo=True).count() == 2
        assert A.AudienceStat.query.filter_by(demo=True).count() > 5 and get(A.Campaign, cid).materials
        ch = A.Channel.query.filter_by(campaign_id=cid, name="Anuncios").one().id
    assert "¡Bienvenidos a la campaña!" in admin.get(f"/campana/{cid}?canal={ch}&hub=1").get_data(as_text=True)
    post(admin, "/admin/ajustes/demo", accion="quitar")
    with A.app.app_context():
        assert A.Submission.query.count() == 0 and A.User.query.count() == 1
        assert not A.Post.query.filter_by(demo=True).count() and not A.Call.query.count() and not A.AudienceStat.query.count()
        assert not get(A.Campaign, cid).materials
        assert get(A.Campaign, cid).spent == 0


def test_demo_and_notify_cards_visible_in_production(monkeypatch):
    monkeypatch.setattr(A, "ALLOW_WIPE", False)
    page = client_for("admin@test.com").get("/admin/ajustes").get_data(as_text=True)
    assert "Cargar datos de demostración" in page and "Avisos a los clippers" not in page


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


def test_home_feature_campaign():
    """Campaña destacada: automático, una campaña o nada (el modo «anuncio» ya no existe)."""
    cid = make_campaign(song="Campaña Uno")
    admin = client_for("admin@test.com")
    a = client_for("a@test.com")
    assert post(admin, "/admin/destacado", mode="announce", title="X").status_code == 400
    assert "Un anuncio" not in admin.get("/admin/destacado").get_data(as_text=True)
    post(admin, "/admin/destacado", mode="campaign", campaign_id=str(cid))
    home = a.get("/campanas").get_data(as_text=True)
    assert "home-ann" not in home and "featured fcar-slide" in home and "Plazas abiertas" in home
    assert "Rellenar formulario" in home and "solicitar=1" in home          # el botón lleva directo a solicitar
    post(admin, "/admin/destacado", mode="none")
    assert "featured fcar-slide" not in a.get("/campanas").get_data(as_text=True)


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
    assert "Ver vídeo de estadísticas" in admin.get(f"/admin/campana/{cid}/revisar").get_data(as_text=True)
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
    assert mod.post(f"/admin/envio/{other}/visitas", data={"_csrf": "t", "views": "999999"}).status_code == 404
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
        # 3:00 en Madrid, justo después de la lectura diaria
        A.set_setting("reminders_sent", "")
        assert not A.reminders_due(datetime(2026, 11, 6, 1, 30))[0]   # 01:30 UTC = 02:30 Madrid (invierno)
        due, day = A.reminders_due(datetime(2026, 11, 6, 2, 10))      # 03:10 Madrid
        assert due
        A.set_setting("reminders_sent", day)
        assert not A.reminders_due(datetime(2026, 11, 6, 9, 0))[0]   # ya mandado hoy
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
    a = client_for("otro@test.com", onboarded=False)
    with A.app.app_context():
        A.User.query.filter_by(email="otro@test.com").one().avatar_url = "/foto/1"; A.db.session.commit()
    r = post(a, "/bienvenida", name="Otro", username="otro", country="OT")
    assert "Escribe en qué país vives" in r.get_data(as_text=True)
    post(a, "/bienvenida", name="Otro", username="otro", country="OT", country_other="  Alemania ")
    with A.app.app_context():
        u = A.User.query.filter_by(email="otro@test.com").one()
        assert (u.country, u.country_other, u.country_name) == ("OT", "Alemania", "Alemania")
    post(a, "/bienvenida", name="Otro", username="otro", country="ES", country_other="Alemania")
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


def test_clipper_deletes_clip_and_it_is_not_rediscovered(monkeypatch):
    cid = make_campaign()
    a = client_for("a@test.com", tiktok="ana")
    post(a, f"/campana/{cid}", url=tt("ana", 1))
    with A.app.app_context():
        s = A.Submission.query.one(); sid, key = s.id, s.video_key
    assert "Eliminar clip" in a.get("/mis-clips").get_data(as_text=True)
    assert post(client_for("b@test.com"), f"/mis-clips/{sid}/eliminar").status_code == 404
    post(a, f"/mis-clips/{sid}/eliminar")
    with A.app.app_context():
        assert A.Submission.query.count() == 0 and A.IgnoredClip.query.filter_by(video_key=key).one()
    post(a, f"/campana/{cid}", url=tt("ana", 1))                         # a mano sí se puede volver a añadir
    with A.app.app_context():
        assert A.Submission.query.count() == 1 and A.IgnoredClip.query.count() == 0
        s = A.Submission.query.one(); s.status = "aprobado"; A.db.session.commit(); sid = s.id
    post(a, f"/mis-clips/{sid}/eliminar")                                # aprobado: no se borra
    with A.app.app_context():
        assert A.Submission.query.count() == 1


def test_last_seen_shown_to_admin():
    cid = make_campaign()
    admin = client_for("admin@test.com")
    a = client_for("a@test.com", tiktok="ana")
    client_for("b@test.com", tiktok="beto")
    a.get("/")                                           # Ana acaba de entrar
    with A.app.app_context():
        b = A.User.query.filter_by(email="b@test.com").first()
        b.last_seen_at = datetime.utcnow() - timedelta(days=9)   # Beto lleva 9 días sin entrar
        A.db.session.commit()
        assert A.seen(None) == "Nunca" and A.seen_level(None) == "off"
        assert A.seen(datetime.utcnow()) == "En línea" and A.seen_level(datetime.utcnow()) == "on"
        assert A.seen(datetime.utcnow() - timedelta(hours=3)) == "hace 3 h"
    html = admin.get("/admin/clippers?orden=actividad").get_data(as_text=True)
    assert "Última vez" in html and "En línea" in html and "hace 9 d" in html
    assert html.index("a@test.com") < html.index("b@test.com")   # el más reciente, primero
    assert "llevan más de 7 días sin entrar" in html
    post(a, f"/campana/{cid}", url=tt("ana", 1))
    assert "Última vez" in admin.get(f"/admin/campana/{cid}/clippers").get_data(as_text=True)


def test_my_demo_is_separate_from_platform_demo(monkeypatch):
    cid = make_campaign(budget=500)
    admin = client_for("admin@test.com")
    a = client_for("a@test.com", tiktok="ana")
    post(a, f"/campana/{cid}", url=tt("ana", 1))          # un clip real
    post(admin, "/admin/ajustes/demo", accion="cargar")    # demo de la plataforma
    with A.app.app_context():
        spent_before = get(A.Campaign, cid).spent
        platform_demo = A.User.query.filter(A.User.email.like(f"%{A.DEMO_DOMAIN}")).count()
    post(admin, "/admin/ajustes/mi-demo", accion="cargar")
    with A.app.app_context():
        me = A.User.query.filter_by(email="admin@test.com").first()
        mine = A.Submission.query.filter_by(user_id=me.id, my_demo=True).all()
        assert mine and all(A.is_fake(s) for s in mine)
        assert A.ViewSnapshot.query.filter(A.ViewSnapshot.submission_id.in_([s.id for s in mine])).count()
        assert A.Notice.query.filter_by(user_id=me.id, my_demo=True).count() == 4
    assert admin.get("/mis-clips").status_code == 200 and admin.get("/ganancias").status_code == 200
    assert "Quitar mi demo" in admin.get("/admin/ajustes").get_data(as_text=True)
    post(admin, "/admin/ajustes/mi-demo", accion="cargar")             # no se duplica
    with A.app.app_context():
        assert A.Submission.query.filter_by(user_id=me.id, my_demo=True).count() == len(mine)
    post(admin, "/admin/ajustes/mi-demo", accion="quitar")
    with A.app.app_context():
        assert A.Submission.query.filter_by(my_demo=True).count() == 0
        assert A.CampaignAccount.query.filter_by(my_demo=True).count() == 0
        assert A.CampaignMember.query.filter_by(my_demo=True).count() == 0
        assert A.Notice.query.filter_by(my_demo=True).count() == 0
        assert abs(get(A.Campaign, cid).spent - spent_before) < 0.01          # el presupuesto vuelve a como estaba
        assert A.User.query.filter(A.User.email.like(f"%{A.DEMO_DOMAIN}")).count() == platform_demo   # la otra demo sigue
        assert A.Submission.query.filter_by(user_id=A.User.query.filter_by(email="a@test.com").first().id).count() == 1


def test_daily_scan_at_3_madrid(monkeypatch):
    from datetime import datetime
    monkeypatch.setattr(A.viewcounter, "APIFY_TOKEN", "x")
    monkeypatch.setattr(A, "REFRESH_AT", "03:00")
    with A.app.app_context():
        A.set_setting("refresh_day", "")
        assert not A._refresh_due(datetime(2026, 11, 6, 1, 0))     # 02:00 Madrid
        assert A._refresh_due(datetime(2026, 11, 6, 2, 5))         # 03:05 Madrid
        A.set_setting("refresh_day", "2026-11-06")
        assert not A._refresh_due(datetime(2026, 11, 6, 15, 0))    # ya se hizo hoy
        # invierno: 03:00 Madrid = 02:00 UTC; verano: 01:00 UTC
        assert A.next_scan_utc(datetime(2026, 11, 6, 12, 0)) == datetime(2026, 11, 7, 2, 0)
        assert A.next_scan_utc(datetime(2026, 7, 6, 0, 30)) == datetime(2026, 7, 6, 1, 0)


def test_channels_undo_restore_calls_and_auto_channels():
    from datetime import datetime, timedelta
    cid = make_campaign()
    admin = client_for("admin@test.com")
    a = client_for("a@test.com", tiktok="ana")
    _auto_accept()
    admin.get(f"/admin/campana/{cid}/canales")
    with A.app.app_context():
        chans = {c.name: c for c in A.Channel.query.filter_by(campaign_id=cid)}
        assert chans["Cuentas TOP"].kind == "top" and chans["Llamadas"].kind == "calls" and chans["Vídeos virales"].kind == "winners"
        ids = {k: v.id for k, v in chans.items()}
        sec = A.ChannelSection.query.filter_by(campaign_id=cid, title="Comunidad").one().id
    # borrar un canal: desaparece, sale «Deshacer» y vuelve
    page = post(admin, f"/admin/campana/{cid}/canales", act="canal_borrar", channel=str(ids["Anuncios"])).get_data(as_text=True)
    assert "Deshacer" in page and "Canal «Anuncios» borrado" in page
    assert ">Anuncios<" not in a.get(f"/campana/{cid}").get_data(as_text=True)
    post(admin, f"/admin/campana/{cid}/canales", act="deshacer", what="canal", id=str(ids["Anuncios"]))
    with A.app.app_context():
        assert get(A.Channel, ids["Anuncios"]).deleted_at is None
    # borrar una sección y restaurar todo
    post(admin, f"/admin/campana/{cid}/canales", act="seccion_borrar", section=str(sec))
    assert "Restaurar todas las secciones" in admin.get(f"/admin/campana/{cid}/canales").get_data(as_text=True)
    post(admin, f"/admin/campana/{cid}/canales", act="restaurar_todo")
    with A.app.app_context():
        assert get(A.ChannelSection, sec).deleted_at is None
    # llamada: hora de España → UTC, aviso 30 min antes una sola vez
    ch = ids["Llamadas"]
    post(admin, f"/admin/campana/{cid}/canales?canal={ch}", act="llamada_nueva", canal=str(ch), title="Directo de dudas",
         date="2026-11-13", time="19:00", minutes="60", link="https://meet.google.com/abc")
    with A.app.app_context():
        call = A.Call.query.one()
        assert call.starts_at == datetime(2026, 11, 13, 18, 0)          # invierno: España = UTC+1
        call.starts_at = datetime.utcnow() + timedelta(minutes=28)
        A.db.session.commit()
        assert A.call_reminders() >= 1 and A.call_reminders() == 0
        ana = A.User.query.filter_by(email="a@test.com").one()
        assert A.Notice.query.filter_by(user_id=ana.id, kind="llamada").count() == 1
    page = a.get(f"/campana/{cid}?canal={ch}").get_data(as_text=True)
    assert "Directo de dudas" in page and "hora de España" in page and "meet.google.com/abc" in page
    for name in ("Cuentas TOP", "Vídeos virales"):
        assert a.get(f"/campana/{cid}?canal={ids[name]}").status_code == 200


def test_invite_link_single_use_and_google_form():
    cid = make_campaign()
    admin = client_for("admin@test.com")
    post(admin, f"/admin/campana/{cid}/editar", apply_form_url="https://forms.gle/abc")
    with A.app.app_context():
        get(A.Campaign, cid).apply_form_url = "https://forms.gle/abc"
        A.db.session.commit()
    b = client_for("b@test.com", verify=False)
    page = b.get(f"/campana/{cid}").get_data(as_text=True)
    assert "forms.gle/abc" in page and "Rellenar el formulario" in page
    post(admin, f"/admin/campana/{cid}/invitaciones", act="crear", n="2", note="Ana")
    with A.app.app_context():
        toks = [i.token for i in A.Invite.query.filter_by(campaign_id=cid)]
    assert len(toks) == 2
    assert "Enlaces de invitación" in admin.get(f"/admin/campana/{cid}/solicitudes").get_data(as_text=True)
    b.get(f"/invitacion/{toks[0]}")
    with A.app.app_context():
        ub = A.User.query.filter_by(email="b@test.com").one()
        assert A.CampaignMember.query.filter_by(user_id=ub.id, campaign_id=cid).one().status == "aceptada"
        assert A.Invite.query.filter_by(token=toks[0]).one().used_by == ub.id
    c = client_for("c@test.com", verify=False)
    assert "ya se ha usado" in c.get(f"/invitacion/{toks[0]}", follow_redirects=True).get_data(as_text=True)
    with A.app.app_context():
        uc = A.User.query.filter_by(email="c@test.com").one()
        assert not A.CampaignMember.query.filter_by(user_id=uc.id, campaign_id=cid).first()


def test_auto_featured_new_and_reopened():
    from datetime import datetime, timedelta
    old = make_campaign(song="Vieja")
    new = make_campaign(song="Nueva")
    with A.app.app_context():
        get(A.Campaign, old).created_at = datetime.utcnow() - timedelta(days=40)
        get(A.Campaign, new).created_at = datetime.utcnow() - timedelta(days=1)
        A.db.session.commit()
        cards = [(c, A.campaign_stats(c)) for c in A.active_campaigns()]
        assert [(c.song, why) for c, _s, why in A.auto_featured(cards)] == [("Nueva", "nueva"), ("Vieja", "")]   # todas las abiertas, las nuevas primero
        get(A.Campaign, old).spots_opened_at = datetime.utcnow()
        A.db.session.commit()
        cards = [(c, A.campaign_stats(c)) for c in A.active_campaigns()]
        assert [(c.song, why) for c, _s, why in A.auto_featured(cards)] == [("Vieja", "plazas"), ("Nueva", "nueva")]
    page = client_for("a@test.com").get("/campanas").get_data(as_text=True)
    assert "data-carousel" in page and "Plazas abiertas" in page


def test_distribution_plan_auto_detect_and_hidden_from_clippers(monkeypatch):
    from datetime import datetime, timedelta
    admin = client_for("admin@test.com")
    post(admin, "/admin/distribucion", client="Marca X", name="Plan diario",
         accounts="tiktok @marca.uno\nhttps://www.instagram.com/marca.dos/\nnada")
    with A.app.app_context():
        camp = A.Campaign.query.filter_by(status=A.DIST_STATUS).one()
        did = camp.id
        accs = A.CampaignAccount.query.filter_by(campaign_id=did).all()
        assert sorted((a.platform, a.handle) for a in accs) == [("instagram", "marca.dos"), ("tiktok", "marca.uno")]
        for a in accs:
            a.decided_at = datetime.utcnow() - timedelta(days=2)
        A.db.session.commit()
    monkeypatch.setattr(A, "AUTO_PLATFORMS", ("tiktok", "instagram", "youtube"))
    vid = {"url": "https://www.tiktok.com/@marca.uno/video/7300000000000000001", "vid": "7300000000000000001",
           "author": "marca.uno", "views": 5000, "likes": 100, "posted_at": datetime.utcnow() - timedelta(hours=3)}
    monkeypatch.setattr(A.viewcounter, "fetch_profile_videos", lambda p, hs, per=12: [vid] if p == "tiktok" else [])
    with A.app.app_context():
        A.discover_clips()
        s = A.Submission.query.filter_by(campaign_id=did).one()
        assert s.status == "aprobado" and float(s.earned or 0) == 0 and s.views == 5000
    page = admin.get(f"/admin/distribucion/{did}").get_data(as_text=True)
    assert "@marca.uno" in page and "Marca X" in page
    assert "Marca X" in admin.get("/admin/distribucion").get_data(as_text=True)
    assert "Plan diario" not in admin.get("/admin/campanas").get_data(as_text=True)
    a = client_for("a@test.com")
    assert a.get(f"/campana/{did}").status_code == 404
    assert "Plan diario" not in a.get("/campanas").get_data(as_text=True)
    with A.app.app_context():
        tok = get(A.Campaign, did).report_token
    rep = a.get(f"/informe/{tok}").get_data(as_text=True) if tok else ""
    assert "Plan de distribución" in rep


def test_official_api_connect_verify_read_and_delete(monkeypatch):
    import base64, hashlib, hmac, json as _j
    from datetime import datetime, timedelta
    cid = make_campaign()
    admin = client_for("admin@test.com")
    a = client_for("a@test.com", verify=False)
    post(a, f"/campana/{cid}/unirme")
    with A.app.app_context():
        m = A.CampaignMember.query.one(); m.status = "aceptada"
        uid = m.user_id
        A.db.session.add(A.CampaignAccount(user_id=uid, campaign_id=cid, platform="tiktok", handle="ana", status="pendiente"))
        A.set_setting("api_mode", "on")
        A.db.session.commit()
    monkeypatch.setattr(A.official, "TIKTOK_CLIENT_KEY", "k"); monkeypatch.setattr(A.official, "TIKTOK_CLIENT_SECRET", "s")
    monkeypatch.setattr(A.official, "INSTAGRAM_APP_SECRET", "igsecret")
    assert "Conectar @ana" in a.get(f"/campana/{cid}").get_data(as_text=True)
    r = a.get("/conectar/tiktok")
    assert r.status_code == 302 and "tiktok.com/v2/auth/authorize" in r.headers["Location"]
    with a.session_transaction() as s:
        state = s["api_state"]["s"]
    monkeypatch.setattr(A.official, "exchange_code", lambda p, c, ru: {"access": "AT-secreto", "refresh": "RT", "expires_in": 86400})
    monkeypatch.setattr(A.official, "profile", lambda p, t: {"id": "oid1", "handle": "ana", "avatar": "https://x/a.jpg"})
    a.get(f"/conectar/tiktok/vuelta?code=c&state={state}")
    with A.app.app_context():
        tok = A.ApiToken.query.one()
        assert tok.handle == "ana" and "AT-secreto" not in tok.access_enc and A.dec(tok.access_enc) == "AT-secreto"
        acc = A.CampaignAccount.query.filter_by(handle="ana").one()
        assert acc.status == "aceptada" and acc.bio_verified
        acc.decided_at = datetime.utcnow() - timedelta(days=1)
        A.db.session.commit()
    vid = {"url": "https://www.tiktok.com/@ana/video/7300000000000000009", "vid": "7300000000000000009", "author": "ana",
           "views": 1234, "likes": 10, "comments": 1, "shares": 0, "posted_at": datetime.utcnow() - timedelta(hours=2),
           "caption": "hola", "thumb": "https://x/t.jpg", "avatar": ""}
    monkeypatch.setattr(A.official, "videos", lambda p, t, h, limit=30: [vid])
    def no_apify(*a_, **k):
        raise AssertionError("no debería llamar a Apify")
    monkeypatch.setattr(A.viewcounter, "fetch_profile_videos", no_apify)
    with A.app.app_context():
        A.discover_clips()
        s = A.Submission.query.one()
        assert s.views == 1234 and s.caption == "hola" and s.id in A._api_fresh
        A.db.session.add(A.AudienceStat(platform="tiktok", handle="ana", submission_id=s.id,
                                        data=_j.dumps({"country": {"ES": 70, "MX": 30}, "gender": {"female": 60, "male": 40}})))
        A.db.session.commit()
        aud = A.audience_for([s])
        assert aud["country"]["ES"] == 70 and aud["clips"] == 1
    # Meta pide borrar los datos de una cuenta de Instagram
    with A.app.app_context():
        A.db.session.add(A.ApiToken(user_id=uid, platform="instagram", ext_id="ig9", handle="ana.ig"))
        A.db.session.commit()
    payload = base64.urlsafe_b64encode(_j.dumps({"user_id": "ig9"}).encode()).decode().rstrip("=")
    sig = base64.urlsafe_b64encode(hmac.new(b"igsecret", payload.encode(), hashlib.sha256).digest()).decode().rstrip("=")
    r = a.post("/meta/borrar-datos", data={"signed_request": f"{sig}.{payload}"})
    assert r.status_code == 200 and r.get_json()["confirmation_code"]
    assert a.post("/meta/borrar-datos", data={"signed_request": f"malo.{payload}"}).status_code == 400
    with A.app.app_context():
        assert not A.ApiToken.query.filter_by(platform="instagram").first()
    assert "Cómo borrar tus datos" in a.get("/eliminar-datos").get_data(as_text=True)


def test_api_refresh_by_ids_and_solo_mode(monkeypatch):
    from datetime import datetime, timedelta
    cid = make_campaign()
    a = client_for("a@test.com", tiktok="ana")
    with A.app.app_context():
        uid = A.User.query.filter_by(email="a@test.com").one().id
        A.db.session.add(A.ApiToken(user_id=uid, platform="tiktok", ext_id="o1", handle="ana", access_enc=A.enc("AT"),
                                    expires_at=datetime.utcnow() + timedelta(days=1)))
        old = A.Submission(user_id=uid, campaign_id=cid, url="https://www.tiktok.com/@ana/video/7300000000000000111",
                           platform="tiktok", video_key="tiktok:7300000000000000111", author="ana", views=10)
        other = A.Submission(user_id=uid, campaign_id=cid, url="https://www.tiktok.com/@luis/video/7300000000000000222",
                             platform="tiktok", video_key="tiktok:7300000000000000222", author="luis", views=5)
        A.db.session.add_all([old, other])
        A.set_setting("api_mode", "on")
        A.db.session.commit()
        monkeypatch.setattr(A.official, "stats_by_ids", lambda p, t, ids, h="": {"7300000000000000111": {"views": 999, "likes": 9}})
        rest = A.api_refresh("tiktok", [old, other])
        assert rest == [other] and old.views == 999
        A.set_setting("api_mode", "solo")
        A.db.session.commit()
        assert A.api_required()


def test_ytdlp_finds_clips_apify_only_reads_and_check_changes_nothing(monkeypatch):
    cid = make_campaign(song="Bali")
    client_for("a@test.com", tiktok="clipsdepablo")
    _auto_accept()
    now = datetime.utcnow()
    with A.app.app_context():
        acc = A.CampaignAccount.query.one()
        acc.decided_at = now - timedelta(days=2)
        A.set_setting("search_engine", "ytdlp")
        A.db.session.commit()
    vids = [{"url": "https://www.tiktok.com/@clipsdepablo/video/7500000000000000011", "vid": "7500000000000000011",
             "author": "clipsdepablo", "posted_at": now - timedelta(hours=3), "views": None}]
    monkeypatch.setattr(A.viewcounter, "ytdlp_available", lambda: True)
    monkeypatch.setattr(A.viewcounter, "ytdlp_profile_videos", lambda p, h, per=12, known=(): list(vids))

    def no_apify(*a, **k):
        raise AssertionError("no debe buscar en Apify")
    monkeypatch.setattr(A.viewcounter, "fetch_profile_videos", no_apify)
    with A.app.app_context():
        added, errors = A.discover_clips()
        assert added == 1 and not errors
        assert A.Submission.query.one().campaign_id == cid
        # comprobación: un vídeo de antes de la última búsqueda que falta → aviso, sin añadirlo
        A.set_setting("last_refresh_at", now.isoformat())
        vids.append({"url": "https://www.tiktok.com/@clipsdepablo/video/7500000000000000012", "vid": "7500000000000000012",
                     "author": "clipsdepablo", "posted_at": now - timedelta(hours=1), "views": None})
        vids.append({"url": "https://www.tiktok.com/@clipsdepablo/video/7500000000000000013", "vid": "7500000000000000013",
                     "author": "clipsdepablo", "posted_at": now + timedelta(minutes=5), "views": None})   # después: normal
        res = A.ytdlp_check()
        assert [i[0] for i in res["issues"]] == ["falta"] and A.Submission.query.count() == 1
    # si yt-dlp falla, esa cuenta va a Apify
    def boom(*a, **k):
        raise RuntimeError("bloqueado")
    monkeypatch.setattr(A.viewcounter, "ytdlp_profile_videos", boom)
    monkeypatch.setattr(A.viewcounter, "fetch_profile_videos", lambda p, hs, per=12: [])
    with A.app.app_context():
        A.discover_clips()
        assert A.json.loads(A.get_setting("ytdlp_stats"))["fail"] == 1
        A.apify_log("vistas", "tiktok", 3, 3, 200)
        assert A.apify_summary()["by"]["vistas"]["items"] == 3



# ---------------------------------------------------------------- auditoría de noviembre
def _analytics_reference(camp, max_days=180):
    """Versión anterior (clip × día), como referencia: estadísticas de una campaña: totales, por red y la evolución día a día de cada métrica.
    Los días sin lectura arrastran la última conocida. Si un clip no tiene historial de me gusta / comentarios /
    compartidos (se empezaron a guardar después), se estiman en proporción a sus vistas de ese momento."""
    subs = [s for s in camp.submissions if s.status == "aprobado"]
    totals = {m: sum(getattr(s, m) or 0 for s in subs) for m in A.METRICS}
    spent = A.cents(camp.spent)
    by_platform = [{"key": p, "name": A.PLATFORMS[p],
                    "views": sum(s.views or 0 for s in subs if s.platform == p),
                    "posts": sum(1 for s in subs if s.platform == p)} for p in camp.platform_list]
    out = {"totals": totals, "spent": spent, "posts": len(subs),
           "ecpm": spent / totals["views"] * 1000 if totals["views"] else 0,
           "cpm": (camp.rate or 0) / 1000,
           "engagement": (totals["likes"] + totals["comments"] + totals["shares"]) / totals["views"] * 100 if totals["views"] else 0,
           "by_platform": by_platform, "series": None}
    if not subs:
        return out
    snaps = {}
    for sn in (A.ViewSnapshot.query.filter(A.ViewSnapshot.submission_id.in_([s.id for s in subs]))
               .order_by(A.ViewSnapshot.taken_at).all()):
        snaps.setdefault(sn.submission_id, []).append(sn)
    today = datetime.utcnow().date()
    start = max(min(s.created_at.date() for s in subs), today - timedelta(days=max_days - 1))
    days = [start + timedelta(days=i) for i in range((today - start).days + 1)]
    cum = {m: [0] * len(days) for m in A.METRICS}
    for s in subs:
        cur_views = s.views or 0
        ratio = {m: (getattr(s, m) or 0) / cur_views if cur_views else 0 for m in A.METRICS[1:]}
        points = [(s.created_at, {m: 0 for m in A.METRICS})]
        for sn in snaps.get(s.id, []):
            vals = {"views": sn.views or 0}
            for m in A.METRICS[1:]:
                v = getattr(sn, m)
                vals[m] = v if v is not None else round(vals["views"] * ratio[m])
            points.append((sn.taken_at, vals))
        points.append((datetime.utcnow(), {m: getattr(s, m) or 0 for m in A.METRICS}))   # la lectura de ahora
        j, last = 0, points[0][1]
        for i, d in enumerate(days):
            end = datetime.combine(d, datetime.max.time())
            while j < len(points) and points[j][0] <= end:
                last = points[j][1]
                j += 1
            for m in A.METRICS:
                cum[m][i] += last[m]
    daily = {m: [max(0, cum[m][i] - (cum[m][i - 1] if i else 0)) for i in range(len(days))] for m in A.METRICS}
    out["series"] = {"labels": [d.isoformat() for d in days], "cumulative": cum, "daily": daily}
    return out


def test_campaign_analytics_matches_previous_version():
    cid = make_campaign(budget=5000)
    admin = client_for("admin@test.com")
    post(admin, "/admin/ajustes/demo", accion="cargar")
    with A.app.app_context():
        c = get(A.Campaign, cid)
        new, ref = A.campaign_analytics(c), _analytics_reference(c)
        assert new["posts"] == ref["posts"] > 5 and new["totals"] == ref["totals"] and new["by_platform"] == ref["by_platform"]
        assert new["series"]["labels"] == ref["series"]["labels"]
        assert new["series"]["cumulative"] == ref["series"]["cumulative"]
        assert new["series"]["daily"] == ref["series"]["daily"]
        new7, ref7 = A.campaign_analytics(c, max_days=7), _analytics_reference(c, max_days=7)
        assert new7["series"] == ref7["series"]


def test_campaign_stats_and_review_counts_come_from_database():
    cid = make_campaign(budget=5000)
    admin = client_for("admin@test.com")
    post(admin, "/admin/ajustes/demo", accion="cargar")
    with A.app.app_context():
        c = get(A.Campaign, cid)
        st = A.campaign_stats(c)
        ok = [s for s in c.submissions if s.status == "aprobado"]
        assert st["clips"] == len(ok) and st["views"] == sum(s.views or 0 for s in ok)
        assert st["pending"] == sum(1 for s in c.submissions if s.status == "pendiente")
        n_ok = A.Submission.query.filter_by(status="aprobado").count()
        n_no = A.Submission.query.filter_by(status="rechazado").count()
        assert A.views_week()["views"] >= 0
    page = admin.get("/admin/clips").get_data(as_text=True)            # en «Por revisar» también salen los números reales
    assert f'Aprobados <b class="tnum">{n_ok}</b>' in page and f'Rechazados <b class="tnum">{n_no}</b>' in page


def test_long_lists_are_paged():
    make_campaign(budget=5000)
    admin = client_for("admin@test.com")
    post(admin, "/admin/ajustes/demo", accion="cargar")
    with A.app.app_context():
        A.LIST_PAGE, keep = 5, A.LIST_PAGE
    try:
        page = admin.get("/admin/clippers").get_data(as_text=True)
        assert "Ver más clippers" in page and page.count("/admin/clipper/") < admin.get("/admin/clippers?p=9").get_data(as_text=True).count("/admin/clipper/")
        assert "Ver más clippers" not in admin.get("/admin/clippers?p=9").get_data(as_text=True)
    finally:
        A.LIST_PAGE = keep


def test_bunny_copies_one_video_at_a_time(monkeypatch):
    monkeypatch.setattr(A, "BUNNY_LIBRARY_ID", "1")
    monkeypatch.setattr(A, "BUNNY_API_KEY", "k")
    monkeypatch.setattr(A, "search_engine", lambda: "ytdlp")
    monkeypatch.setattr(A, "poll_bunny_copies", lambda: None)
    holding, peak, order = [], [0], []

    class Sub:
        def __init__(self, i):
            self.id, self.platform, self.url, self.media_url, self.video_tries = i, "tiktok", f"u{i}", "", 0
            self.video_guid = self.video_status = self.video_error = ""
            self.campaign = type("C", (), {"song": "X"})()
            self.video_key = f"tiktok:{i}"
    subs = [Sub(1), Sub(2), Sub(3)]
    monkeypatch.setattr(A, "copy_candidates", lambda ids=None: list(subs))

    def download(url):
        holding.append(url)
        peak[0] = max(peak[0], len(holding))
        order.append("baja " + url)
        if url == "u2":
            holding.remove(url)
            raise RuntimeError("bloqueado")
        return b"x" * 20000
    monkeypatch.setattr(A.viewcounter, "ytdlp_download", download)
    monkeypatch.setattr(A, "bunny_call", lambda m, path, **k: (True, 200, {"guid": "g"}))

    class Up:
        status_code = 200

    def put(url, data=None, **k):
        order.append("sube")
        if holding:
            holding.pop()
        return Up()
    monkeypatch.setattr(A.requests, "put", put)
    monkeypatch.setattr(A.viewcounter, "fetch_download_urls", lambda p, urls: {})
    with A.app.app_context():
        done, rep = A.copy_videos_now()
    assert done == 2 and peak[0] == 1                       # nunca dos vídeos descargados a la vez
    assert order[:4] == ["baja u1", "sube", "baja u2", "baja u3"]
    assert subs[1].video_status == "error" and "Apify" in subs[1].video_error


def test_daily_refresh_is_marked_done_only_when_it_finishes(monkeypatch):
    calls, sent = [], []
    monkeypatch.setattr(A, "read_audiences", lambda: None)
    monkeypatch.setattr(A, "check_creator_balances", lambda: None)
    monkeypatch.setattr(A, "notify_phone", lambda *a, **k: sent.append(a[0]))

    def boom():
        calls.append(1)
        raise RuntimeError("se cortó")
    monkeypatch.setattr(A, "refresh_views", boom)
    t0 = datetime.utcnow()
    with A.app.app_context():
        day = A._local().strftime("%Y-%m-%d")
        assert A.run_daily_refresh(t0) is False and A.get_setting("refresh_day") != day       # falló: el día sigue pendiente
        assert A.run_daily_refresh(t0 + timedelta(minutes=10)) is False and len(calls) == 1   # aún no toca reintentar
        assert A.run_daily_refresh(t0 + timedelta(minutes=50)) is False and len(calls) == 2
        assert not sent
        assert A.run_daily_refresh(t0 + timedelta(minutes=100)) is False and len(calls) == 3
        assert sent and "no ha terminado" in sent[0]                                            # tercer fallo: aviso
        assert A.run_daily_refresh(t0 + timedelta(minutes=200)) is False and len(calls) == 3   # no insiste más
        assert any("lectura diaria" in a[1].lower() for a in A.tech_alerts())
        A.set_setting("refresh_run", "")
        monkeypatch.setattr(A, "refresh_views", lambda: "ok")
        assert A.run_daily_refresh(t0) is True and A.get_setting("refresh_day") == day
        assert A.run_daily_refresh(t0 + timedelta(hours=2)) is False                            # ya hecha hoy


def _clipper_reference(subs, days=30):
    """Versión anterior (clip × día), como referencia. Panel de Mis clips: totales, evolución día a día de cada métrica y vistas por red de los clips del periodo."""
    today = datetime.utcnow().date()
    if days:
        start = today - timedelta(days=days - 1)
    else:
        start = max(min((s.created_at.date() for s in subs), default=today), today - timedelta(days=364))
    subs = [s for s in subs if s.created_at.date() >= start]
    live = [s for s in subs if s.status != "rechazado"]
    approved = [s for s in subs if s.status == "aprobado"]
    totals = {m: sum(getattr(s, m) or 0 for s in live) for m in A.METRICS}
    totals.update({"earned": A.cents(sum(s.earned or 0 for s in approved)), "sent": len(subs), "approved": len(approved)})
    by_platform = sorted(({"key": p, "name": A.PLATFORMS[p], "posts": sum(1 for s in subs if s.platform == p),
                           "views": sum(s.views or 0 for s in live if s.platform == p)} for p in A.PLATFORMS
                          if any(s.platform == p for s in subs)), key=lambda r: -r["views"])
    dates = [start + timedelta(days=i) for i in range((today - start).days + 1)]
    cum = {m: [0] * len(dates) for m in A.CLIP_METRICS}
    idx = {d: i for i, d in enumerate(dates)}
    snaps = {}
    if live:
        for sn in (A.ViewSnapshot.query.filter(A.ViewSnapshot.submission_id.in_([s.id for s in live]))
                   .order_by(A.ViewSnapshot.taken_at).all()):
            snaps.setdefault(sn.submission_id, []).append(sn)
    now = datetime.utcnow()
    for s in live:                                      # vistas, me gusta…: la última lectura de cada día (se arrastra)
        cur_views = s.views or 0
        ratio = {m: (getattr(s, m) or 0) / cur_views if cur_views else 0 for m in A.METRICS[1:]}
        points = [(s.created_at, {m: 0 for m in A.METRICS})]
        for sn in snaps.get(s.id, []):
            vals = {"views": sn.views or 0}
            for m in A.METRICS[1:]:
                v = getattr(sn, m)
                vals[m] = v if v is not None else round(vals["views"] * ratio[m])
            points.append((sn.taken_at, vals))
        points.append((now, {m: getattr(s, m) or 0 for m in A.METRICS}))
        j, last = 0, points[0][1]
        for i, d in enumerate(dates):
            end = datetime.combine(d, datetime.max.time())
            while j < len(points) and points[j][0] <= end:
                last = points[j][1]
                j += 1
            for m in A.METRICS:
                cum[m][i] += last[m]
    def add_from(day, metric, amount):                  # suma desde ese día en adelante
        i = idx.get(max(day, start))
        if i is None:
            return
        for k in range(i, len(dates)):
            cum[metric][k] += amount
    for s in subs:
        add_from(s.created_at.date(), "sent", 1)
    for s in approved:
        when = (s.reviewed_at or s.created_at).date()
        add_from(when, "approved", 1)
        add_from(when, "earned", float(s.earned or 0))
    cum["earned"] = [round(v, 2) for v in cum["earned"]]
    daily = {m: [max(0, round(cum[m][i] - (cum[m][i - 1] if i else 0), 2)) for i in range(len(dates))] for m in A.CLIP_METRICS}
    return {"totals": totals, "by_platform": by_platform,
            "series": {"labels": [d.isoformat() for d in dates], "cumulative": cum, "daily": daily}}


def test_clipper_analytics_matches_previous_version():
    make_campaign(budget=5000)
    post(client_for("admin@test.com"), "/admin/ajustes/demo", accion="cargar")
    with A.app.app_context():
        u = A.User.query.join(A.Submission).filter(A.User.is_admin.is_(False)).first()
        subs = list(u.submissions)
        assert len(subs) > 2
        for days in (7, 30, 0):
            assert A.clipper_analytics(subs, days) == _clipper_reference(subs, days)


# ---------------------------------------------------------------- escala (miles de usuarios)
def test_old_clips_are_read_every_few_days_and_dormant_accounts_weekly():
    now = datetime.utcnow()
    S = type("S", (), {})
    new, old = S(), S()
    new.posted_at, new.created_at, new.checked_at = now - timedelta(days=3), now - timedelta(days=3), now - timedelta(hours=20)
    old.posted_at, old.created_at, old.checked_at = now - timedelta(days=40), now - timedelta(days=40), now - timedelta(hours=20)
    assert A.read_due(new, now) and not A.read_due(old, now)
    old.checked_at = now - timedelta(days=3)
    assert A.read_due(old, now)
    old.checked_at = None
    assert A.read_due(old, now)                               # nunca leído: siempre toca
    acc = S()
    acc.id, acc.decided_at, acc.created_at, acc.last_post_at = 7, now - timedelta(days=5), now - timedelta(days=5), None
    assert A.scan_today(acc, now)                             # recién aceptada: a diario
    acc.decided_at = now - timedelta(days=60)
    assert sum(A.scan_today(acc, now + timedelta(days=d)) for d in range(14)) == 2      # parada: un día a la semana
    acc.last_post_at = now - timedelta(days=2)
    assert A.scan_today(acc, now)                             # ha publicado hace poco: a diario


def test_apify_daily_cap_defers_reads_and_ytdlp_figures_are_used(monkeypatch):
    cid = make_campaign(song="Bali")
    client_for("a@test.com", tiktok="clipsdepablo")
    _auto_accept()
    now = datetime.utcnow()
    with A.app.app_context():
        acc = A.CampaignAccount.query.one()
        acc.decided_at = now - timedelta(days=2)
        A.set_setting("search_engine", "ytdlp")
        A.db.session.commit()
    vids = [{"url": f"https://www.tiktok.com/@clipsdepablo/video/750000000000000001{i}", "vid": f"750000000000000001{i}",
             "author": "clipsdepablo", "posted_at": now - timedelta(hours=3), "views": None, "yt_views": 4000 + i,
             "likes": 10, "comments": 1, "shares": 0} for i in range(3)]
    monkeypatch.setattr(A.viewcounter, "ytdlp_available", lambda: True)
    monkeypatch.setattr(A.viewcounter, "ytdlp_profile_videos", lambda p, h, per=12, known=(): [dict(v) for v in vids])
    asked = []
    monkeypatch.setattr(A.viewcounter, "fetch_views", lambda p, urls: asked.extend(urls) or {})
    monkeypatch.setattr(A.viewcounter, "fetch_profile_videos", lambda *a, **k: [])
    with A.app.app_context():
        A.refresh_views()
        subs = A.Submission.query.order_by(A.Submission.id).all()
        assert [s.views for s in subs] == [4000, 4001, 4002] and not asked       # cifras de yt-dlp: Apify no se usa
        # sin las cifras de yt-dlp, se piden a Apify… hasta el tope diario
        A.set_setting("ytdlp_stats", "off")
        A.set_setting("apify_daily_max", "2")
        A.db.session.commit()
        A.refresh_views()
        assert len(asked) == 2
        assert A.json.loads(A.get_setting("apify_deferred"))["n"] == 1
        assert any("Tope diario de Apify" in a[1] for a in A.tech_alerts())


def test_profile_menu_balance_matches_full_calculation():
    make_campaign(budget=5000)
    admin = client_for("admin@test.com")
    post(admin, "/admin/ajustes/demo", accion="cargar")
    with A.app.app_context():
        for c in A.Campaign.query.all():
            c.status = "finalizada"
        A.db.session.commit()
        users = A.User.query.join(A.Submission).filter(A.User.is_admin.is_(False)).distinct().limit(6).all()
        for u in users:
            with A.app.test_request_context("/"):
                A.login_user(u)
                A.g.pop("_memo", None)                         # (el contexto de la prueba se comparte entre vueltas)
                assert A.my_available() == A.user_money(u)["available"]
        assert any(A.user_money(u)["available"] > 0 for u in users)


def test_clippers_have_no_phone_notifications():
    a = client_for("a@test.com")
    page = a.get("/cuenta").get_data(as_text=True)
    assert "Avisos en este dispositivo" not in page and 'id="notificaciones"' not in page
    assert a.post("/push/suscribir", data={"_csrf": "t", "endpoint": "https://fcm.googleapis.com/abc", "p256dh": "k", "auth": "a"}).status_code == 404
    assert not A.push_ready()


def test_phone_alerts_are_off_for_everyone(monkeypatch):
    monkeypatch.setattr(A, "NTFY_TOPIC", "tema")
    sent = []
    monkeypatch.setattr(A, "_spawn", lambda fn, *args: sent.append(args))
    with A.app.app_context():
        A.notify_phone("Hola", "mensaje")
    assert not sent and not A.push_ready()


def test_home_shows_global_rank_even_without_earnings():
    make_campaign(budget=5000)
    admin = client_for("admin@test.com")
    post(admin, "/admin/ajustes/demo", accion="cargar")
    nuevo = client_for("nuevo@test.com")
    page = nuevo.get("/").get_data(as_text=True)
    assert "Clasificación" in page and "Ver completa" in page and "Tu puesto" in page
    with A.app.app_context():
        u = A.User.query.filter_by(email="nuevo@test.com").one()
        r = A.my_rank(u)
        ranked = len(A.rank_table())
        assert r["pos"] > ranked > 3 and r["earned"] == 0 and r["goal"] and r["need"] > 0     # sale su puesto aunque no haya ganado nada
        first = A.db.session.get(A.User, A.rank_table()[0][0])
        assert A.my_rank(first)["pos"] == 1
        A.snapshot_ranks()
        u = A.User.query.filter_by(email="nuevo@test.com").one()
        assert u.rank_pos == r["pos"]
        u.rank_prev = u.rank_pos + 25                       # ayer estaba 25 puestos más abajo
        A.db.session.commit()
        r2 = A.my_rank(u)
        assert r2["up"] == 25 and r2["fire"]
    page = nuevo.get("/").get_data(as_text=True)
    assert "▲ 25" in page and "hm-fire" in page and f"#{r['pos']}" in page


def test_week_table_is_filled_with_older_clips_when_the_week_is_short():
    make_campaign(budget=5000)
    post(client_for("admin@test.com"), "/admin/ajustes/demo", accion="cargar")
    with A.app.app_context():
        old = datetime.utcnow() - timedelta(days=20)
        ids = [s.id for s in A.Submission.query.filter_by(status="aprobado").order_by(A.Submission.id).all()]
        A.Submission.query.filter(A.Submission.id.in_(ids[2:])).update({"created_at": old}, synchronize_session=False)
        A.db.session.commit()
        rows = A.clips_of_week(8)
        assert len(rows) == 8 and len({s.id for s in rows}) == 8
        week = [s for s in rows if s.created_at > datetime.utcnow() - timedelta(days=7)]
        assert rows[:len(week)] == week                       # primero los de esta semana, luego el relleno


def test_my_clips_has_no_audience_panel():
    make_campaign(budget=5000)
    post(client_for("admin@test.com"), "/admin/ajustes/demo", accion="cargar")
    with A.app.app_context():
        email = A.User.query.join(A.Submission).filter(A.User.is_admin.is_(False)).first().email
    page = client_for(email).get("/mis-clips").get_data(as_text=True)
    assert "Mis clips" in page and "Ubicación" not in page and "aud-" not in page


def test_admin_menu_groups_and_search():
    """El menú del panel va por grupos, «Revisar clips» tiene enlace propio y el buscador encuentra clippers y campañas."""
    make_campaign(budget=5000)
    admin = client_for("admin@test.com")
    post(admin, "/admin/ajustes/demo", accion="cargar")
    page = admin.get("/admin").get_data(as_text=True)
    for txt in ("Comunidad", "Sistema", 'action="/admin/buscar"'):
        assert txt in page
    assert '<span class="adm-gt">Por hacer</span>' not in page     # lo de los mods (revisar, solicitudes) no va en el menú
    with A.app.app_context():
        u = A.User.query.filter(A.User.is_admin.is_(False)).first()
        uid, name = u.id, u.name
        acc = A.CampaignAccount.query.first()
        handle, acc_uid = acc.handle, acc.user_id
    r = admin.get("/admin/buscar?q=" + name.split()[0])
    assert r.status_code in (200, 302)
    if r.status_code == 200:
        assert name in r.get_data(as_text=True)
    r = admin.get("/admin/buscar?q=@" + handle)                  # una cuenta exacta: directo a la ficha de su dueño
    assert r.status_code == 302 and r.headers["Location"].endswith(f"/admin/clipper/{acc_uid}")
    assert "Sin resultados" in admin.get("/admin/buscar?q=zzzzqqqq").get_data(as_text=True)
    assert admin.get("/admin/buscar").status_code == 200
    with A.app.app_context():
        email = A.db.session.get(A.User, uid).email
    assert client_for(email).get("/admin/buscar?q=a").status_code == 403


def test_tech_panel_tabs_and_api_guide():
    """El Panel técnico va por pestañas y la de APIs trae la guía con las direcciones que piden las redes."""
    page = client_for("admin@test.com").get("/admin/ajustes").get_data(as_text=True)
    for tab in ("estado", "lectura", "videos", "apis", "pagos", "datos"):
        assert f'data-tab="{tab}"' in page and f'id="{tab}"' in page
    for txt in ("/conectar/tiktok/vuelta", "/conectar/instagram/vuelta", "/conectar/youtube/vuelta", "/meta/borrar-datos",
                "/privacidad", "/eliminar-datos", "user.info.stats", "instagram_business_manage_insights", "yt-analytics.readonly"):
        assert txt in page
    assert "Avisos al móvil sin activar" not in page             # los avisos al móvil están apagados: no se reclama su clave


def test_domain_verification_for_api_reviews():
    """Los códigos de verificación de Google, Meta y TikTok se guardan en el panel y se sirven donde las redes los buscan."""
    admin = client_for("admin@test.com")
    assert admin.get("/tiktokAbC12345.txt").status_code == 404
    assert "google-site-verification" not in admin.get("/privacidad").get_data(as_text=True)
    post(admin, "/admin/ajustes/verificacion", google='<meta name="google-site-verification" content="g00gleC0de_123-xyz" />',
         meta="metacode123456", google_file="google1a2b3c4d5e6f7a8b.html",
         tiktok_file="tiktokAbC12345.txt", tiktok_body="tiktok-developers-site-verification=AbC12345")
    home = A.app.test_client().get("/").get_data(as_text=True)
    assert '<meta name="google-site-verification" content="g00gleC0de_123-xyz">' in home
    assert '<meta name="facebook-domain-verification" content="metacode123456">' in home
    r = A.app.test_client().get("/tiktokAbC12345.txt")
    assert r.status_code == 200 and r.get_data(as_text=True) == "tiktok-developers-site-verification=AbC12345"
    assert A.app.test_client().get("/tiktokOtro9999.txt").status_code == 404
    r = A.app.test_client().get("/google1a2b3c4d5e6f7a8b.html")
    assert r.status_code == 200 and r.get_data(as_text=True) == "google-site-verification: google1a2b3c4d5e6f7a8b.html"
    post(admin, "/admin/ajustes/verificacion", google='"><script>alert(1)</script>', meta="", tiktok_file="../../etc/passwd", tiktok_body="x")
    home = A.app.test_client().get("/").get_data(as_text=True)                # lo que no es un código se descarta entero
    assert "google-site-verification" not in home and "<script>alert(1)" not in home
    assert A.app.test_client().get("/tiktokAbC12345.txt").status_code == 404


def test_legal_pages_cover_connected_networks():
    """Lo que revisan TikTok, Meta y Google: portada pública con enlaces legales y política con el apartado de sus datos."""
    c = A.app.test_client()
    home = c.get("/").get_data(as_text=True)
    assert 'href="/terminos"' in home and 'href="/privacidad"' in home
    priv = c.get("/privacidad").get_data(as_text=True)
    for txt in ("Datos de TikTok, Instagram, YouTube y Facebook", "youtube.com/t/terms", "policies.google.com/privacy",
                "api-services-user-data-policy", "myaccount.google.com/permissions", "/eliminar-datos"):
        assert txt in priv
    assert "youtube.com/t/terms" in c.get("/terminos").get_data(as_text=True)
    assert c.get("/eliminar-datos").status_code == 200


def test_support_chat_popup():
    """El botón flotante abre una ventanita con el chat; la conversación se carga sola y se puede abrir a pantalla completa."""
    make_campaign(budget=5000)
    c = client_for("clipper@test.com")
    page = c.get("/campanas").get_data(as_text=True)
    assert 'id="chat-pop"' in page and 'data-src="/soporte/mini"' in page and 'data-cp-full' in page and 'href="/soporte"' in page
    post(c, "/soporte", body="Hola desde la ventanita")
    mini = c.get("/soporte/mini").get_data(as_text=True)
    assert "Hola desde la ventanita" in mini and "data-chat" in mini and "<html" not in mini
    assert 'id="chat-pop"' not in c.get("/soporte").get_data(as_text=True)      # en la página del chat no hace falta
    assert A.app.test_client().get("/soporte/mini").status_code in (302, 401)


def test_featured_open_badge_is_plain():
    """«Plazas abiertas» del destacado: sin emoji y con «Nueva» aparte."""
    make_campaign(budget=5000)
    page = client_for("clipper@test.com").get("/campanas").get_data(as_text=True)
    assert "🔓" not in page
    if "Plazas abiertas" in page:
        assert 'class="eyebrow open"><i aria-hidden="true"></i>Plazas abiertas' in page


def test_featured_carousel_markup():
    """El carrusel del destacado trae lo que necesita la transición: pista, diapositivas, puntos y flechas."""
    for i in range(3):
        make_campaign(budget=5000 + i)
    page = client_for("clipper@test.com").get("/campanas").get_data(as_text=True)
    if page.count("fcar-slide") > 1:
        assert "data-carousel" in page and "data-car-track" in page and "data-car-next" in page and "data-car-dot" in page


def test_account_rows_widgets():
    """Planes de distribución y preguntas de la campaña: filas cómodas (red + @), mandando el mismo texto de siempre."""
    admin = client_for("admin@test.com")
    page = admin.get("/admin/distribucion").get_data(as_text=True)
    assert 'data-rows="acc"' in page and "una por línea" not in page
    assert A.parse_dist_accounts("tiktok @cuenta.uno\ninstagram @otra_cuenta\nyoutube @canal.tres") == [
        ("tiktok", "cuenta.uno"), ("instagram", "otra_cuenta"), ("youtube", "canal.tres")]
    cid = make_campaign(budget=5000)
    form = admin.get(f"/admin/campana/{cid}/editar").get_data(as_text=True)
    assert 'data-rows="txt"' in form and 'name="join_questions"' in form


def test_legal_pages_complete():
    """Aviso legal, cookies y privacidad: enlazados en el pie y con los datos del titular que se ponen en el panel."""
    c = A.app.test_client()
    home = c.get("/").get_data(as_text=True)
    for href in ('href="/terminos"', 'href="/privacidad"', 'href="/cookies"', 'href="/aviso-legal"'):
        assert href in home
    assert "remember_token" in c.get("/cookies").get_data(as_text=True)
    assert "LSSI" in c.get("/aviso-legal").get_data(as_text=True)
    admin = client_for("admin@test.com")
    assert "Faltan los datos del aviso legal" in admin.get("/admin/ajustes").get_data(as_text=True)
    post(admin, "/admin/ajustes/legal", owner="Felipe Prueba", nif="12345678Z", address="Calle Mayor 1, Madrid", email="legal@ejemplo.com", registry="")
    notice = c.get("/aviso-legal").get_data(as_text=True)
    assert "Felipe Prueba" in notice and "12345678Z" in notice and "Calle Mayor 1, Madrid" in notice
    assert "Felipe Prueba" in c.get("/privacidad").get_data(as_text=True)
    assert "Faltan los datos del aviso legal" not in admin.get("/admin/ajustes").get_data(as_text=True)


def test_admin_profile_menu_shows_balance():
    """El admin también puede clipear: su menú de perfil enseña su saldo, como el de cualquier clipper."""
    make_campaign(budget=5000)
    page = client_for("admin@test.com").get("/campanas").get_data(as_text=True)
    assert "Saldo disponible" in page


def test_admin_summary_stats_chart():
    """Resumen del panel: la gráfica general (vistas por día, dinero, clips, clippers) en lugar de «Para hacer»."""
    admin = client_for("admin@test.com")
    cid = make_campaign()
    now = datetime.utcnow()
    today = now.date()
    with A.app.app_context():
        u = A.User(email="v@test.com", name="V")
        A.db.session.add(u); A.db.session.commit()
        s = A.Submission(user_id=u.id, campaign_id=cid, platform="tiktok", url=tt("v", 1), video_key="tiktok:7300000000000000001", status="aprobado", views=900, earned=1.5,
                         created_at=now - timedelta(days=40), reviewed_at=now - timedelta(days=1))
        A.db.session.add(s); A.db.session.commit()
        for days_ago, v in ((40, 100), (3, 300), (3, 350), (2, 340), (0, 900)):   # una lectura más baja no resta
            A.db.session.add(A.ViewSnapshot(submission_id=s.id, views=v, taken_at=now - timedelta(days=days_ago)))
        A.db.session.commit()
        start = today - timedelta(days=29)
        out = A.views_daily(start, 30)
        assert len(out) == 30 and out[-4] == 250 and out[-3] == 0 and out[-1] == 550 and sum(out) == 800
        an = A.admin_analytics(30)
        assert an["totals"]["views"] == 800 and an["totals"]["spent"] == 1.5
    html = admin.get("/admin").get_data(as_text=True)
    assert "Dinero repartido" in html and "Actividad reciente" in html and "Clip aprobado" in html
    assert "Para hacer" not in html


def test_admin_campaigns_filter_in_page():
    """Campañas del panel: llegan todas y la barra filtra en la página (la pastilla se desliza); ?estado marca la pestaña."""
    make_campaign(budget=5000)
    admin = client_for("admin@test.com")
    page = admin.get("/admin/campanas").get_data(as_text=True)
    assert 'data-row-filter="#camp-rows"' in page and 'class="seg"' in page and 'data-f="activa"' in page
    page = admin.get("/admin/campanas?estado=finalizada").get_data(as_text=True)
    assert 'class="f-out"' in page and 'class="on" data-tab data-f="finalizada"' in page


def test_resumen_periodos():
    """El Resumen del panel se puede ver por 7 días, 30 días o desde siempre."""
    c = client_for("admin@test.com")
    marcado = {"": "Últimos 30 días", "?p=7": "Últimos 7 días", "?p=todo": "Desde siempre", "?p=raro": "Últimos 30 días"}
    for p, t in marcado.items():
        r = c.get("/admin" + p)
        assert r.status_code == 200 and re.search(r'<a class="on" href="[^"]*">' + t + "</a>", r.get_data(as_text=True))


def test_resumen_graficas_para_probar():
    """La tarjeta de gráficas del Resumen trae sus tres pestañas y sus datos se calculan sin fallar."""
    c = client_for("admin@test.com")
    for p in ("", "?p=7", "?p=todo"):
        page = c.get("/admin" + p).get_data(as_text=True)
        for k in ("vm", "cpm", "top"):
            assert f'data-ov-pane="{k}"' in page
        assert 'data-ov-pane="clips"' not in page


def test_lectura_de_vistas_guarda_la_foto_de_la_cuenta():
    """Al leer las vistas de un clip, si Apify trae la foto de perfil, se guarda en la cuenta (sale en «Cuentas»)."""
    cid = make_campaign()
    a = client_for("a@test.com", tiktok="ana")
    post(a, f"/campana/{cid}", url=tt("ana", 1))
    with A.app.app_context():
        s = A.Submission.query.filter_by(campaign_id=cid).first()
        A.apply_reading(s, {"views": 10, "author": "ana", "avatar": "https://p16.tiktokcdn.com/ana.jpg"}, datetime.utcnow())
        A.db.session.commit()
        acc = A.CampaignAccount.query.filter_by(campaign_id=cid, handle="ana").first()
        assert acc and acc.avatar_url == "https://p16.tiktokcdn.com/ana.jpg"


def test_portada_de_tiktok_fresca(monkeypatch):
    """Las portadas de TikTok caducan y no se dejan enlazar: la web las pide frescas y las sirve ella misma."""
    cid = make_campaign()
    a = client_for("a@test.com", tiktok="ana")
    post(a, f"/campana/{cid}", url=tt("ana", 1))
    with A.app.app_context():
        s = A.Submission.query.filter_by(campaign_id=cid).first()
        sid = s.id
        s.thumb_url = "https://vieja.example/portada.jpg"
        acc = A.CampaignAccount.query.filter_by(campaign_id=cid).first()
        acc.avatar_url = "https://p16.tiktokcdn.com/ana.jpg"
        aid = acc.id
        A.db.session.commit()
    got = []
    monkeypatch.setattr(A, "tiktok_cover", lambda url: "https://p16.tiktokcdn.com/fresca.jpg")
    monkeypatch.setattr(A, "fetch_image", lambda url: got.append(url) or (b"JPG", "image/jpeg"))
    r = a.get(f"/clip/{sid}/portada")
    assert r.status_code == 200 and r.data == b"JPG" and got[-1] == "https://p16.tiktokcdn.com/fresca.jpg"
    assert a.get(f"/cuenta-red/{aid}/foto").data == b"JPG" and got[-1] == "https://p16.tiktokcdn.com/ana.jpg"
    monkeypatch.setattr(A, "fetch_image", lambda url: None)
    assert a.get(f"/clip/{sid}/portada").status_code == 404


def test_anadir_mod_por_usuario():
    """Al añadir un mod se elige el usuario por su nombre (de la lista), no hace falta su correo."""
    cid = make_campaign()
    admin = client_for("admin@test.com")
    client_for("mod@test.com")
    with A.app.app_context():
        u = A.User.query.filter_by(email="mod@test.com").one()
        name, uid = u.name, u.id
    page = admin.get("/admin/mods").get_data(as_text=True)
    assert 'list="mod-users"' in page and f'data-uid="{uid}"' in page and "Correo del usuario" not in page
    post(admin, "/admin/mods", act="anadir", user=name, uid=str(uid), campaigns=[str(cid)], perms=["soporte"])
    with A.app.app_context():
        assert A.CampaignMod.query.filter_by(user_id=uid, campaign_id=cid).count() == 1



def test_portada_y_foto_se_guardan_al_leer(monkeypatch):
    """Cuando Apify trae portada y foto, se guardan en la web (luego los enlaces de las redes caducan)."""
    import io
    from PIL import Image
    buf = io.BytesIO(); Image.new("RGB", (400, 600), "red").save(buf, "PNG")
    monkeypatch.setattr(A, "fetch_image", lambda url: (buf.getvalue(), "image/png"))
    cid = make_campaign()
    a = client_for("a@test.com", tiktok="ana")
    post(a, f"/campana/{cid}", url=tt("ana", 1))
    with A.app.app_context():
        s = A.Submission.query.filter_by(campaign_id=cid).first()
        A.apply_reading(s, {"views": 5, "author": "ana", "thumb": "https://cdn/x.jpg", "avatar": "https://cdn/a.jpg"}, datetime.utcnow())
        A.db.session.commit()
        sid = s.id
        acc = A.CampaignAccount.query.filter_by(campaign_id=cid, handle="ana").first()
        assert s.thumb_ok and s.thumb_data[:2] == b"\xff\xd8" and acc.avatar_ok
        aid = acc.id
    monkeypatch.setattr(A, "fetch_image", lambda url: None)        # aunque el enlace ya no valga, se ve lo guardado
    assert a.get(f"/clip/{sid}/portada").status_code == 200
    assert a.get(f"/cuenta-red/{aid}/foto").status_code == 200


def test_facebook_paginas_api_oficial(monkeypatch):
    """Facebook (páginas): conectar guarda el token de la página y la lectura saca reels con sus reproducciones."""
    import official

    class R:
        def __init__(self, d, code=200):
            self.d, self.status_code, self.text = d, code, ""
        def json(self):
            return self.d

    def fake_get(url, params=None, timeout=None, **kw):
        if url.endswith("/oauth/access_token"):
            return R({"access_token": "LARGO" if (params or {}).get("grant_type") else "CORTO"})
        if url.endswith("/me/accounts"):
            return R({"data": [{"id": "55", "name": "Mi página", "access_token": "PAGINA"}]})
        if url.endswith("/me") and "picture" in (params or {}).get("fields", ""):
            return R({"id": "55", "name": "Mi página", "username": "mipagina", "picture": {"data": {"url": "https://fb/p.jpg"}}})
        if url.endswith("/me/video_reels"):
            return R({"data": [{"id": "900", "description": "Clip", "created_time": "2026-11-01T10:00:00+0000",
                                "picture": "https://fb/t.jpg", "likes": {"summary": {"total_count": 7}},
                                "comments": {"summary": {"total_count": 2}}}]})
        if url.endswith("/me/videos"):
            return R({"data": []})
        if url.endswith("/900"):
            return R({"id": "900", "description": "Clip", "picture": "https://fb/t.jpg", "likes": {"summary": {"total_count": 9}}})
        if url.endswith("/900/video_insights"):
            return R({"data": [{"name": "fb_reels_total_plays", "values": [{"value": 1234}]}]})
        return R({}, 404)

    monkeypatch.setattr(official.requests, "get", fake_get)
    monkeypatch.setattr(official, "FACEBOOK_APP_ID", "id"); monkeypatch.setattr(official, "FACEBOOK_APP_SECRET", "s")
    assert official.configured("facebook") and "facebook.com" in official.auth_url("facebook", "st", "https://x/vuelta")
    tk = official.exchange_code("facebook", "code", "https://x/vuelta")
    assert tk["access"] == "PAGINA" and tk["page_id"] == "55"
    prof = official.profile("facebook", tk["access"])
    assert prof["handle"] == "mipagina" and prof["avatar"] == "https://fb/p.jpg"
    vids = official.videos("facebook", tk["access"], "mipagina")
    assert vids[0]["views"] == 1234 and vids[0]["likes"] == 7 and vids[0]["url"] == "https://www.facebook.com/reel/900"
    st = official.stats_by_ids("facebook", tk["access"], ["900"])["900"]
    assert st["views"] == 1234 and st["likes"] == 9


def test_add_account_by_web_adds_it_verified(monkeypatch):
    """«Añadir cuenta → Por la web»: al volver de la red, la cuenta entra en la campaña ya verificada."""
    cid = make_campaign()
    a = client_for("a@test.com", verify=False)
    post(a, f"/campana/{cid}/unirme")
    with A.app.app_context():
        A.CampaignMember.query.one().status = "aceptada"
        A.set_setting("api_mode", "on")
        A.db.session.commit()
    page = a.get(f"/campana/{cid}").get_data(as_text=True)
    assert 'data-acc-go="web"' in page and 'data-acc-go="codigo"' in page
    monkeypatch.setattr(A.official, "TIKTOK_CLIENT_KEY", "k"); monkeypatch.setattr(A.official, "TIKTOK_CLIENT_SECRET", "s")
    r = a.get(f"/conectar/tiktok?cid={cid}&next=/campana/{cid}")
    assert r.status_code == 302
    with a.session_transaction() as s:
        state = s["api_state"]["s"]
    monkeypatch.setattr(A.official, "exchange_code", lambda p, c, ru: {"access": "AT", "refresh": "RT", "expires_in": 86400})
    monkeypatch.setattr(A.official, "profile", lambda p, t: {"id": "oid9", "handle": "nueva_ana", "avatar": ""})
    a.get(f"/conectar/tiktok/vuelta?code=c&state={state}")
    with A.app.app_context():
        acc = A.CampaignAccount.query.filter_by(campaign_id=cid, handle="nueva_ana").one()
        assert acc.status == "aceptada" and acc.bio_verified


def test_add_account_by_web_without_keys_goes_to_the_network():
    cid = make_campaign()
    a = client_for("a@test.com", verify=False)
    r = a.get(f"/conectar/instagram?cid={cid}")
    assert r.status_code == 302 and r.headers["Location"].startswith("https://www.instagram.com/oauth/authorize")
    with a.session_transaction() as s:
        state = s["api_state"]["s"]
    r = a.get(f"/conectar/instagram/vuelta?code=c&state={state}", follow_redirects=True)
    assert "Muy pronto" in r.get_data(as_text=True)


def test_ytdlp_brings_cover_and_account_photo(monkeypatch):
    """Cuando yt-dlp da las vistas (y no se pide nada a Apify), también deja guardadas la portada y la foto de la cuenta."""
    import io
    from PIL import Image
    make_campaign(song="Bali")
    client_for("a@test.com", tiktok="clipsdepablo")
    _auto_accept()
    now = datetime.utcnow()
    with A.app.app_context():
        A.CampaignAccount.query.one().decided_at = now - timedelta(days=2)
        A.set_setting("search_engine", "ytdlp")
        A.db.session.commit()
    vids = [{"url": "https://www.tiktok.com/@clipsdepablo/video/7500000000000000021", "vid": "7500000000000000021",
             "author": "clipsdepablo", "posted_at": now - timedelta(hours=3), "views": None, "yt_views": 500,
             "thumb": "https://cdn.test/portada.jpg", "avatar": "https://cdn.test/foto.jpg"}]
    monkeypatch.setattr(A.viewcounter, "ytdlp_available", lambda: True)
    monkeypatch.setattr(A.viewcounter, "ytdlp_profile_videos", lambda p, h, per=12, known=(): [dict(v) for v in vids])
    buf = io.BytesIO(); Image.new("RGB", (300, 400), "red").save(buf, "JPEG")
    monkeypatch.setattr(A, "fetch_image", lambda url: (buf.getvalue(), "image/jpeg"))
    with A.app.app_context():
        added, errors = A.discover_clips()
        assert added == 1 and not errors
        sub, acc = A.Submission.query.one(), A.CampaignAccount.query.one()
        assert sub.views == 500 and sub.thumb_ok and sub.thumb_data
        assert acc.avatar_ok and acc.avatar_data


def test_ytdlp_thumb_picks_the_still_cover():
    V = A.viewcounter
    e = {"thumbnails": [{"id": "dynamicCover", "url": "https://x/anim.webp"}, {"id": "cover", "url": "https://x/fija.jpg"}]}
    assert V._ytdlp_thumb("tiktok", "1", e) == "https://x/fija.jpg"
    assert V._ytdlp_thumb("youtube", "abc", {}) == "https://i.ytimg.com/vi/abc/hqdefault.jpg"
    assert V._ytdlp_thumb("tiktok", "1", {}) == ""
    assert V._ytdlp_avatar({"thumbnails": [{"id": "avatar_uncropped", "url": "https://x/a.jpg"}]}, []) == "https://x/a.jpg"
    assert V._ytdlp_avatar({}, [{"author_avatar": "https://x/b.jpg"}]) == "https://x/b.jpg"


def test_delete_all_real_test_users_keeps_demo_and_admins():
    cid = make_campaign()
    admin = client_for("admin@test.com")
    a = client_for("a@test.com", tiktok="ana")
    client_for("b@test.com")
    with A.app.app_context():
        A.db.session.add(A.User(email="demo1" + A.DEMO_DOMAIN, name="Demo Uno"))
        A.db.session.commit()
    page = admin.get("/admin/ajustes").get_data(as_text=True)
    assert "Usuarios reales de prueba" in page and "a@test.com" in page
    post(admin, "/admin/ajustes/borrar-usuarios", confirm="no")
    with A.app.app_context():
        assert A.User.query.filter_by(email="a@test.com").first()
    post(admin, "/admin/ajustes/borrar-usuarios", confirm="eliminar")
    with A.app.app_context():
        emails = {u.email for u in A.User.query.all()}
        assert emails == {"admin@test.com", "demo1" + A.DEMO_DOMAIN}
        assert not A.CampaignAccount.query.count() and not A.CampaignMember.query.count()
    c = client_for("c@test.com")                       # un clipper no puede usarlo
    post(c, "/admin/ajustes/borrar-usuarios", confirm="ELIMINAR")
    with A.app.app_context():
        assert A.User.query.filter_by(email="c@test.com").first()


def test_ranking_periods_count_views_gained_in_the_period():
    """«Esta semana» ordena por las vistas conseguidas en los últimos 7 días, no por los clips enviados esa semana."""
    from datetime import datetime, timedelta
    cid = make_campaign()
    admin = client_for("admin@test.com")
    a, b = client_for("a@test.com", tiktok="ana"), client_for("b@test.com", tiktok="beto")
    post(a, f"/campana/{cid}", url=tt("ana", 1))
    post(b, f"/campana/{cid}", url=tt("beto", 2))
    with A.app.app_context():
        sa = A.Submission.query.filter_by(author="ana").one()
        sb = A.Submission.query.filter_by(author="beto").one()
        ids = (sa.id, sb.id)
    for sid in ids:
        post(admin, f"/admin/envio/{sid}/aprobar")
    with A.app.app_context():
        old = datetime.utcnow() - timedelta(days=20)
        sa, sb = A.db.session.get(A.Submission, ids[0]), A.db.session.get(A.Submission, ids[1])
        sa.created_at = sb.created_at = old                       # los dos clips son de hace 20 días
        sa.views, sb.views = 5000, 9000
        A.db.session.add(A.ViewSnapshot(submission_id=sa.id, views=1000, taken_at=datetime.utcnow() - timedelta(days=10)))   # Ana: +4.000 esta semana
        A.db.session.add(A.ViewSnapshot(submission_id=sb.id, views=8900, taken_at=datetime.utcnow() - timedelta(days=10)))   # Beto: +100
        A.db.session.commit()
        A._ttl.clear() if hasattr(A, "_ttl") else None
        week = A.ranking("semana", limit=49)
        assert [(r["user"].email, r["views"], r["earned"]) for r in week] == [("a@test.com", 4000, None), ("b@test.com", 100, None)]
    page = a.get("/ranking?p=semana").get_data(as_text=True)
    assert "vistas esta semana" in page
    assert 'class="on" href="/ranking?p=global"' in a.get("/ranking").get_data(as_text=True)


def test_review_counts_without_loading_clips_match_the_full_queue():
    """El recuento ligero de «por revisar» (el del menú y la campanita) da lo mismo que clasificar los clips enteros."""
    cid = make_campaign(min_views=1000)                    # con mínimo: unos clips están «contando» y otros «por revisar»
    a, b = client_for("a@test.com", tiktok="ana"), client_for("b@test.com", tiktok="beto")
    for i in range(1, 4):
        post(a, f"/campana/{cid}", url=tt("ana", i))
        post(b, f"/campana/{cid}", url=tt("beto", 10 + i))
    with A.app.test_request_context():
        subs = A.Submission.query.order_by(A.Submission.id).all()
        for i, s in enumerate(subs):
            s.views = [0, 50, 5_000, 80_000, 2_000_000, 900][i]
            s.stats_status = "enviado" if i % 2 else ""
        A.db.session.commit()
    with A.app.test_request_context():
        full = sorted(s.campaign_id for s in A.review_queue()[0])
        assert sorted(A.review_campaign_ids()) == full and 0 < len(full) < 6
        assert A.pending_by_campaign().get(cid, {}).get("clips", 0) == len(full)


def test_campaign_clippers_list_is_paged_and_searchable(monkeypatch):
    cid = make_campaign()
    admin = client_for("admin@test.com")
    with A.app.app_context():
        for i in range(5):
            u = A.User(email=f"c{i}@test.com", name=f"Clipper {i}")
            A.db.session.add(u); A.db.session.flush()
            A.db.session.add(A.CampaignMember(user_id=u.id, campaign_id=cid, status="aceptada"))
            A.db.session.add(A.CampaignAccount(user_id=u.id, campaign_id=cid, platform="tiktok", handle=f"cuenta{i}", status="aceptada"))
        A.db.session.commit()
    monkeypatch.setattr(A, "LIST_PAGE", 1)                      # tandas de 2
    page = admin.get(f"/admin/campana/{cid}/clippers").get_data(as_text=True)
    filas = 'data-label="Ganado"'
    assert page.count(filas) == 2 and "Se ven 2 de 5" in page and "Ver más" in page
    assert admin.get(f"/admin/campana/{cid}/clippers?p=3").get_data(as_text=True).count(filas) == 5
    found = admin.get(f"/admin/campana/{cid}/clippers?q=cuenta4").get_data(as_text=True)
    assert "Clipper 4" in found and "Clipper 1" not in found


def test_cover_does_not_wait_when_outside_downloads_are_busy(monkeypatch):
    """Si ya hay dos descargas de imágenes de fuera en marcha, la web no se queda esperando: responde 404 al momento."""
    cid = make_campaign()
    a = client_for("a@test.com", tiktok="ana")
    post(a, f"/campana/{cid}", url=tt("ana", 1))
    with A.app.app_context():
        s = A.Submission.query.one(); s.thumb_url = "https://cdn.test/x.jpg"; A.db.session.commit(); sid = s.id
    calls = []
    monkeypatch.setattr(A, "fetch_image", lambda u: calls.append(u) or None)
    monkeypatch.setattr(A, "tiktok_cover", lambda u: "")
    assert A._outside.acquire(blocking=False) and A._outside.acquire(blocking=False)
    try:
        assert a.get(f"/clip/{sid}/portada").status_code == 404 and not calls
    finally:
        A._outside.release(); A._outside.release()
    assert a.get(f"/clip/{sid}/portada").status_code == 404 and calls


def test_password_recovery_by_email(monkeypatch):
    """Con el correo configurado: se pide el enlace, llega por correo, vale una sola vez y no delata si el correo existe."""
    sent = []
    monkeypatch.setattr(A, "SMTP_HOST", "smtp.test"); monkeypatch.setattr(A, "MAIL_FROM", "hola@test.com")
    monkeypatch.setattr(A, "send_mail", lambda to, subject, body: sent.append((to, body)) or True)
    c = A.app.test_client()
    from werkzeug.security import generate_password_hash
    with A.app.app_context():
        A.db.session.add(A.User(email="ana@test.com", name="Ana", password_hash=generate_password_hash("clave-vieja-1")))
        A.db.session.commit()
    monkeypatch.setattr(A, "_login_fails", {})
    c = A.app.test_client()
    with c.session_transaction() as se:
        se["_csrf"] = "t"
    assert "He olvidado mi contraseña" in c.get("/login").get_data(as_text=True)
    r1 = post(c, "/recuperar", email="nadie@test.com").get_data(as_text=True)
    r2 = post(c, "/recuperar", email="ana@test.com").get_data(as_text=True)
    assert "te hemos enviado un enlace" in r1 and "te hemos enviado un enlace" in r2 and len(sent) == 1
    link = re.search(r"https?://\S+/recuperar/\S+", sent[0][1]).group(0)
    path = "/" + link.split("/", 3)[3]
    assert "Pon una contraseña nueva" in c.get(path).get_data(as_text=True)
    assert "al menos 8" in post(c, path, password="corta").get_data(as_text=True)
    assert "Contraseña cambiada" in post(c, path, password="clave-nueva-2").get_data(as_text=True)
    assert "ha caducado o ya se usó" in c.get(path, follow_redirects=True).get_data(as_text=True)     # un solo uso
    assert "incorrectos" in post(c, "/login/correo", email="ana@test.com", password="clave-vieja-1").get_data(as_text=True)
    c2 = A.app.test_client()
    with c2.session_transaction() as se:
        se["_csrf"] = "t"
    ok = c2.post("/login/correo", data={"_csrf": "t", "email": "ana@test.com", "password": "clave-nueva-2"})
    assert ok.status_code == 302 and "/login" not in ok.headers["Location"]


def test_password_recovery_hidden_without_mail():
    c = A.app.test_client()
    assert "He olvidado mi contraseña" not in c.get("/login").get_data(as_text=True)
    assert c.get("/recuperar").status_code == 404


def test_slow_pages_are_noted_for_the_tech_panel(monkeypatch):
    monkeypatch.setattr(A, "_slow", [])
    A.note_slow("admin_home", 2300); A.note_slow("admin_home", 1800); A.note_slow("my_clips", 1600)
    assert A.slow_pages()[0] == ("admin_home", 2, 2300)
    with A.app.test_request_context():
        assert any("Páginas lentas" in t for _lvl, t, _b in A.tech_alerts())


def test_landing_wall_shows_only_saved_covers_of_approved_clips():
    cid = make_campaign()
    a = client_for("a@test.com", tiktok="ana")
    post(a, f"/campana/{cid}", url=tt("ana", 1))
    post(a, f"/campana/{cid}", url=tt("ana", 2))
    with A.app.app_context():
        s1, s2 = A.Submission.query.order_by(A.Submission.id).all()
        s1.status, s1.thumb_data, s1.thumb_ok, s1.views = "aprobado", b"\xff\xd8jpg", True, 1234
        s2.thumb_data, s2.thumb_ok = b"\xff\xd8jpg", True              # pendiente: no se enseña
        A.db.session.commit(); i1, i2 = s1.id, s2.id
    anon = A.app.test_client()
    page = anon.get("/").get_data(as_text=True)
    assert f"/portada-clip/{i1}" in page and f"/portada-clip/{i2}" not in page and "lp-wall" in page
    r = anon.get(f"/portada-clip/{i1}")
    assert r.status_code == 200 and "public" in r.headers["Cache-Control"]
    assert anon.get(f"/portada-clip/{i2}").status_code == 404


def test_landing_images_section_removed():
    """Mustapha quitó «Imágenes de la portada» del panel: ni sale ni la portada usa esas imágenes."""
    admin = client_for("admin@test.com")
    assert "Imágenes de la portada" not in admin.get("/admin/destacado").get_data(as_text=True)
    with A.app.app_context():
        A.db.session.add(A.LandingTile(url="https://www.tiktok.com/@ana/video/1", status="ok", views=5)); A.db.session.commit()
        tid = A.LandingTile.query.one().id
    A.cache_clear() if hasattr(A, "cache_clear") else None
    assert f"/portada-inicio/{tid}" not in A.app.test_client().get("/").get_data(as_text=True)


def test_landing_lead_from_clipprofit_es():
    c = A.app.test_client()
    o = {"Origin": "https://clipprofit.es"}
    pre = c.open("/api/creador-interesado", method="OPTIONS", headers=o)
    assert pre.headers.get("Access-Control-Allow-Origin") == "https://clipprofit.es"
    r = c.post("/api/creador-interesado", headers=o, json={"name": "Lucía", "email": "LU@x.com", "brand": "Lucía TV",
                                                          "budget": "$2.000 – $10.000", "nicho": "Finanzas"})
    assert r.status_code == 200 and r.get_json()["ok"]
    assert c.post("/api/creador-interesado", headers=o, json={"name": "X", "email": "mal", "brand": "Y"}).status_code == 400
    assert c.post("/api/creador-interesado", headers={"Origin": "https://otra.com"}, json={"name": "a"}).status_code == 403
    assert c.post("/api/creador-interesado", headers=o, json={"name": "Bot", "email": "b@b.com", "brand": "b",
                                                          "website": "spam"}).get_json()["ok"]
    with A.app.app_context():
        leads = A.CampaignLead.query.all()
        assert len(leads) == 1 and leads[0].email == "lu@x.com" and "nicho: Finanzas" in leads[0].message
    admin = client_for("admin@test.com")
    page = admin.get("/admin").get_data(as_text=True)
    assert "Creadores interesados" in page


def test_book_call_from_landing():
    c = A.app.test_client()
    page = c.get("/reservar")
    assert page.status_code == 200 and "Reserva una llamada" in page.get_data(as_text=True)
    csp = page.headers["Content-Security-Policy"]
    assert ("frame-ancestors 'self' https://clipprofit.es https://cool-pavlova-5ecc27.netlify.app https://www.clipprofit.es" in csp
            and "X-Frame-Options" not in page.headers and "Set-Cookie" not in page.headers)
    other = c.get("/login")
    assert other.headers["X-Frame-Options"] == "DENY" and "frame-ancestors 'none'" in other.headers["Content-Security-Policy"]
    with A.app.app_context():
        slots = A.book_slots()
    assert slots
    at = slots[0].strftime("%Y-%m-%dT%H:%M")
    r = c.post("/reservar", data={"at": at, "name": "Lucía", "phone": "+34 600 11 22 33", "email": "lu@x.com"})
    body = r.get_data(as_text=True)
    assert "Gracias por tu reserva" in body and "por WhatsApp" in body
    r = c.post("/reservar", data={"at": at, "name": "Otro", "phone": "+34 600 11 22 44", "email": "o@x.com"})
    assert "ya no está libre" in r.get_data(as_text=True)                 # la misma hora no se reserva dos veces
    r = c.post("/reservar", data={"at": slots[1].strftime("%Y-%m-%dT%H:%M"), "name": "Otro", "phone": "12", "email": "o@x.com"})
    assert "WhatsApp" in r.get_data(as_text=True) and r.status_code == 400
    with A.app.app_context():
        lead = A.CampaignLead.query.one()
        assert (lead.phone, lead.meet_at) == ("+34600112233", slots[0])
        assert slots[0] not in A.book_slots()
    admin = client_for("admin@test.com")
    page = admin.get("/admin/solicitudes").get_data(as_text=True)
    assert "Horario de llamadas" in page and "Crear campaña" not in page and "Marcar" not in page
    with A.app.app_context():
        lid = A.CampaignLead.query.one().id
    r = admin.get(f"/admin/solicitudes/{lid}/whatsapp")
    assert r.headers["Location"].startswith("https://wa.me/34600112233?text=")
    with A.app.app_context():
        assert A.db.session.get(A.CampaignLead, lid).status == "contactada"
    post(admin, "/admin/solicitudes/horario", days=["5"], start="11:00", end="12:00", slot="30", ahead="0", horizon="14")
    with A.app.app_context():
        assert A.book_cfg()["days"] == [5] and all(A._local(s).weekday() == 5 for s in A.book_slots())


def test_book_call_keeps_landing_answers():
    c = A.app.test_client()
    with A.app.app_context():
        at = A.book_slots()[0].strftime("%Y-%m-%dT%H:%M")
    r = c.post("/reservar", data={"at": at, "name": "Pablo", "phone": "+52 55 1234 5678", "email": "p@x.com",
                                  "budget": "$500 – $2.000", "x_Nicho": "Finanzas", "x_Seguidores": "200K"})
    assert "Gracias por tu reserva" in r.get_data(as_text=True)
    with A.app.app_context():
        lead = A.CampaignLead.query.one()
        assert lead.budget == "$500 – $2.000" and "Nicho: Finanzas" in lead.message and "Seguidores: 200K" in lead.message



def test_delete_lead_frees_slot():
    c = A.app.test_client()
    with A.app.app_context():
        at = A.book_slots()[0]
    c.post("/reservar", data={"at": at.strftime("%Y-%m-%dT%H:%M"), "name": "Ana", "phone": "+34600000001", "email": "a@x.com"})
    with A.app.app_context():
        lid = A.CampaignLead.query.one().id
        assert at not in A.book_slots()
    post(client_for("admin@test.com"), "/admin/solicitudes", lead_id=str(lid), status="borrar")
    with A.app.app_context():
        assert A.CampaignLead.query.count() == 0 and at in A.book_slots()


def test_railway_address_goes_to_own_domain(monkeypatch):
    c = A.app.test_client()
    assert c.get("/login", base_url="https://web-production-x.up.railway.app").status_code == 200   # sin BASE_URL: nada cambia
    monkeypatch.setattr(A, "BASE_URL", "https://app.clipprofit.es")
    monkeypatch.setattr(A, "CANON_HOST", "app.clipprofit.es")
    r = c.get("/admin/solicitudes?x=1", base_url="https://web-production-x.up.railway.app")
    assert r.status_code == 301 and r.headers["Location"] == "https://app.clipprofit.es/admin/solicitudes?x=1"
    assert c.get("/salud", base_url="https://web-production-x.up.railway.app").status_code != 301
    assert c.get("/login", base_url="https://app.clipprofit.es").status_code == 200


def test_error_codes(monkeypatch):
    A.app.config["PROPAGATE_EXCEPTIONS"] = False
    def _boom(*a, **k):
        raise RuntimeError("fallo de prueba")
    monkeypatch.setitem(A.app.view_functions, "terms", _boom)
    try:
        r = A.app.test_client().get("/terminos")
        body = r.get_data(as_text=True)
        assert r.status_code == 500 and "Código del error" in body
        with A.app.app_context():
            e = A.ErrorLog.query.one()
            assert e.code in body and "fallo de prueba" in e.what and "Traceback" in e.detail
        admin = client_for("admin@test.com")
        page = admin.get(f"/admin/errores?codigo={e.code.lower()}").get_data(as_text=True)
        assert "fallo de prueba" in page
        c = A.app.test_client()
        r = c.get("/auth/google/callback?code=x&state=otro", follow_redirects=True)
        assert "Código" in r.get_data(as_text=True)
    finally:
        A.app.config["PROPAGATE_EXCEPTIONS"] = None


def test_google_login_signed_state_without_cookie(monkeypatch):
    monkeypatch.setattr(A, "GOOGLE_CLIENT_ID", "id")
    monkeypatch.setattr(A, "BASE_URL", "https://app.clipprofit.es")
    c = A.app.test_client()
    r = c.get("/login/google", base_url="https://web-production-x.up.railway.app")
    assert r.status_code == 302 and r.headers["Location"].startswith("https://app.clipprofit.es/login/google")
    r = c.get("/login/google?next=/ranking", base_url="https://app.clipprofit.es")
    state = r.headers["Location"].split("state=")[1].split("&")[0]
    calls = []
    def fake_post(*a, **k):
        calls.append(1)
        return type("R", (), {"json": lambda self: {"access_token": "t"} if len(calls) == 1 else {"error": "invalid_grant"}})()
    monkeypatch.setattr(A.requests, "post", fake_post)
    monkeypatch.setattr(A.requests, "get", lambda *a, **k: type("R", (), {"json": lambda self: {
        "email": "nuevo@g.com", "email_verified": True, "sub": "123", "name": "Nuevo"}})() if len(calls) == 1 else
        type("R", (), {"json": lambda self: {}})())
    other = A.app.test_client()                             # otra ventana, sin la cookie del principio (móvil, app instalada)
    r = other.get(f"/auth/google/callback?code=abc&state={state}", base_url="https://app.clipprofit.es")
    assert r.status_code == 302 and "/login" not in r.headers["Location"]
    third = A.app.test_client()                             # el navegador repite la misma vuelta
    r = third.get(f"/auth/google/callback?code=abc&state={state}", base_url="https://app.clipprofit.es")
    assert r.status_code == 302 and "/login" not in r.headers["Location"]
    r = A.app.test_client().get("/auth/google/callback?code=abc&state=falso", base_url="https://app.clipprofit.es", follow_redirects=True)
    assert "Código" in r.get_data(as_text=True)


def test_google_callback_repeated_by_browser_is_not_an_error():
    c = client_for("ya@dentro.com")
    r = c.get("/auth/google/callback?code=x&state=usado")
    assert r.status_code == 302 and "/login" not in r.headers["Location"]
    with A.app.app_context():
        assert A.ErrorLog.query.count() == 0


def test_tech_notes():
    admin = client_for("admin@test.com")
    post(admin, "/admin/notas", act="nueva", text="Llamar a Felipe por el dominio")
    page = admin.get("/admin/ajustes").get_data(as_text=True)
    assert "Llamar a Felipe por el dominio" in page and 'data-tab="notas"' in page
    with A.app.app_context():
        nid = A.TechNote.query.one().id
    post(admin, "/admin/notas", act="hecha", nid=str(nid))
    with A.app.app_context():
        assert A.db.session.get(A.TechNote, nid).done
    post(admin, "/admin/notas", act="borrar", nid=str(nid))
    with A.app.app_context():
        assert A.TechNote.query.count() == 0


def test_every_error_is_noted_and_explained():
    c = client_for("ana@test.com", onboarded=False)
    post(c, "/bienvenida", name="Ana", username="a b", country="ES")             # aviso a un usuario
    c.get("/no-existe-esta-pagina")                                               # página que no existe
    c.get("/wp-login.php")                                                        # robots: no se apunta
    r = A.app.test_client().post("/api/error-navegador", json={"msg": "x is not a function", "page": "https://app.clipprofit.es/mis-clips",
                                                                "src": "app.js", "line": 10})
    assert r.status_code == 204
    with A.app.app_context():
        h = A._ErrorsToPanel(); import logging
        h.emit(logging.LogRecord("clipprofit", logging.ERROR, __file__, 1, "Apify no respondió", None, None))
        rows = {e.kind: e for e in A.ErrorLog.query.all()}
    assert set(rows) == {"aviso", "pagina", "navegador", "tarea"}
    assert "ana@test.com" in rows["aviso"].explain and "de 3 a 24" in rows["aviso"].explain
    assert "no existe" in rows["pagina"].explain and "wp-login" not in rows["pagina"].path
    assert rows["navegador"].path == "/mis-clips" and "x is not a function" in rows["navegador"].explain
    assert "Tarea automática" in rows["tarea"].explain
    post(c, "/bienvenida", name="Ana", username="a b", country="ES")             # el mismo aviso otra vez: suma, no repite
    with A.app.app_context():
        assert A.ErrorLog.query.filter_by(kind="aviso").one().times == 2
    page = client_for("admin@test.com").get("/admin/errores?tipo=aviso").get_data(as_text=True)
    assert "le salió este aviso" in page and "×2" in page


def test_mail_by_resend(monkeypatch):
    monkeypatch.setattr(A, "RESEND_API_KEY", "re_x")
    monkeypatch.setattr(A, "MAIL_FROM", "no-responder@clipprofit.es")
    sent = []
    monkeypatch.setattr(A.requests, "post", lambda url, **k: sent.append((url, k["json"])) or type("R", (), {"status_code": 200, "text": ""})())
    monkeypatch.setattr(A.threading, "Thread", lambda target, daemon=True: type("T", (), {"start": lambda self: target()})())
    assert A.mail_ready() and A.send_mail("ana@x.com", "Hola", "Texto")
    assert sent[0][0] == "https://api.resend.com/emails" and sent[0][1]["to"] == ["ana@x.com"]


def test_google_only_when_password_login_off(monkeypatch):
    monkeypatch.setattr(A, "PASSWORD_LOGIN", False)
    c = A.app.test_client()
    page = c.get("/login").get_data(as_text=True)
    assert "Continuar con Google" in page
    with c.session_transaction() as s:
        s["_csrf"] = "t"
    r = c.post("/registro", data={"_csrf": "t", "name": "X", "email": "x@x.com", "password": "12345678"}, follow_redirects=True)
    assert "solo se puede entrar con Google" in r.get_data(as_text=True)
    with A.app.app_context():
        assert A.User.query.filter_by(email="x@x.com").count() == 0



def test_new_account_must_finish_profile_first():
    c = client_for("nueva@test.com", onboarded=False)
    for path in ("/", "/ranking", "/mis-clips", "/cuenta"):
        r = c.get(path)
        assert r.status_code == 302 and "/bienvenida" in r.headers["Location"], path
    assert c.get("/bienvenida").status_code == 200 and c.get("/terminos").status_code == 200
    post(c, "/bienvenida", name="Nueva", username="nueva", country="ES")
    assert c.get("/ranking").status_code == 200


def test_book_call_tracks_plan_and_button():
    c = A.app.test_client()
    page = c.get("/reservar?plan=Pro&boton=Precios").get_data(as_text=True)
    assert 'name="plan" value="Pro"' in page and 'name="boton" value="Precios"' in page
    with A.app.app_context():
        at = A.book_slots()[0].strftime("%Y-%m-%dT%H:%M")
    c.post("/reservar", data={"at": at, "name": "Lu", "phone": "+34600000002", "email": "lu@x.com", "plan": "Pro", "boton": "Precios"})
    with A.app.app_context():
        l = A.CampaignLead.query.one()
        assert (l.plan, l.button) == ("Pro", "Precios")
    page = client_for("admin@test.com").get("/admin/solicitudes").get_data(as_text=True)
    assert "Reservas por plan" in page and "Plan: <b>Pro</b>" in page


def test_connect_whop_with_button(monkeypatch):
    monkeypatch.setattr(A, "WHOP_CLIENT_ID", "app_x")
    monkeypatch.setattr(A, "BASE_URL", "https://app.clipprofit.es")
    monkeypatch.setattr(A, "whop_enabled", lambda: True)
    c = client_for("juan@test.com")
    assert "Conectar con Whop" in c.get("/cuenta").get_data(as_text=True)
    r = c.get("/cobros/whop/conectar")
    loc = r.headers["Location"]
    assert loc.startswith("https://api.whop.com/oauth/authorize?") and "code_challenge_method=S256" in loc
    assert "redirect_uri=https%3A%2F%2Fapp.clipprofit.es%2Fauth%2Fwhop%2Fcallback" in loc
    state = loc.split("state=")[1].split("&")[0]
    sent = {}
    def fake_post(url, json=None, **k):
        sent.update(json or {})
        return type("R", (), {"json": lambda self: {"access_token": "t"}})()
    monkeypatch.setattr(A.requests, "post", fake_post)
    monkeypatch.setattr(A.requests, "get", lambda *a, **k: type("R", (), {"json": lambda self: {"sub": "user_abc", "preferred_username": "juanclips"}})())
    from urllib.parse import unquote
    r = c.get(f"/auth/whop/callback?code=xyz&state={unquote(state)}")
    assert r.status_code == 302 and sent["code_verifier"] and sent["grant_type"] == "authorization_code"
    with A.app.app_context():
        u = A.User.query.filter_by(email="juan@test.com").one()
        assert (u.whop_user_id, u.whop_username, u.payout_method) == ("user_abc", "juanclips", A.WHOP_METHOD)
    r = c.get("/auth/whop/callback?code=xyz&state=falso", follow_redirects=True)
    assert "Código" in r.get_data(as_text=True)


def test_creators_always_pay_from_their_whop_without_switch():
    assert A.creators_on()
    page = client_for("admin@test.com").get("/admin/ajustes").get_data(as_text=True)
    assert "De dónde sale el dinero de los retiros" not in page and "Volver al Whop principal" not in page


def test_creator_paid_through_installed_app_without_key(monkeypatch):
    monkeypatch.setattr(A, "creators_on", lambda: True)
    monkeypatch.setattr(A, "WHOP_APP_API_KEY", "app-key")
    monkeypatch.setattr(A, "WHOP_APP_ID", "app_123")
    admin = client_for("admin@test.com")
    page = admin.get("/admin/creadores/nuevo").get_data(as_text=True)
    assert "https://whop.com/apps/app_123/install" in page and "Cómo conectar el Whop del creador" in page and "WHOP_KEY_" not in page
    post(admin, "/admin/creadores/nuevo", name="Pablo", company_id="biz_abc12345", key_var="")
    with A.app.app_context():
        c = A.Creator.query.filter_by(name="Pablo").one()
        assert A.wallet(c.id) == ("app-key", "biz_abc12345", "Pablo") and A.wallet_ready(c.id)


def test_dark_mode_is_off_for_now():
    """Mustapha lo quitó (24 nov): sin DARK_MODE no sale el interruptor ni se pone oscuro aunque la cuenta lo tenga."""
    c = client_for("sinoscuro@example.com")
    c.post("/cuenta/tema", data={"_csrf": "t", "theme": "dark"}, headers={"X-Requested-With": "fetch"})
    page = c.get("/cuenta").get_data(as_text=True)
    assert 'data-theme="dark"' not in page and "data-theme-toggle" not in page and "Modo oscuro" not in page


def test_dark_mode_is_chosen_by_each_user(monkeypatch):
    """Modo oscuro (si se enciende con DARK_MODE): claro por defecto; cada usuario lo elige y se guarda en su cuenta."""
    monkeypatch.setattr(A, "DARK_MODE", True)
    c = client_for("oscuro@example.com")
    page = c.get("/cuenta").get_data(as_text=True)
    assert 'data-theme="dark"' not in page and "data-theme-toggle" in page
    assert c.post("/cuenta/tema", data={"_csrf": "t", "theme": "dark"}, headers={"X-Requested-With": "fetch"}).status_code == 204
    assert 'data-theme="dark"' in c.get("/cuenta").get_data(as_text=True)
    other = client_for("claro@example.com")
    assert 'data-theme="dark"' not in other.get("/cuenta").get_data(as_text=True)
    c.post("/cuenta/tema", data={"_csrf": "t", "theme": ""})
    assert 'data-theme="dark"' not in c.get("/cuenta").get_data(as_text=True)
    assert c.post("/cuenta/tema", data={"theme": "dark"}).status_code == 400          # sin código de seguridad no vale


def test_campaign_chooses_which_creator_pays(monkeypatch):
    """«Paga desde el Whop de» vuelve al formulario; en la web real no se activa una campaña sin creador."""
    admin = client_for("admin@test.com")
    with A.app.app_context():
        cr = A.Creator(name="Pablo", company_id="biz_pablo"); A.db.session.add(cr); A.db.session.commit(); crid = cr.id
    assert "Paga desde el Whop de" in admin.get("/admin/campana/nueva").get_data(as_text=True)
    monkeypatch.setattr(A, "REQUIRE_CREATOR", True)
    base = dict(song="Sin dueño", artist="Pablo", rate="100", budget="500", pf_tiktok="on", status="activa")
    r = post(admin, "/admin/campana/nueva", creator_id="", **base)
    assert "Elige qué creador paga" in r.get_data(as_text=True)
    with A.app.app_context():
        assert A.Campaign.query.count() == 0
    post(admin, "/admin/campana/nueva", creator_id=str(crid), **base)
    with A.app.app_context():
        camp = A.Campaign.query.one()
        assert camp.creator_id == crid and camp.status == "activa"
        camp.creator_id, camp.status = None, "pausada"; A.db.session.commit(); cid = camp.id
        assert any("sin creador" not in t for _, t, _ in A.tech_alerts())
    r = post(admin, f"/admin/campana/{cid}/estado", accion="reanudar")
    assert "Elige qué creador paga" in r.get_data(as_text=True)
    with A.app.app_context():
        assert A.db.session.get(A.Campaign, cid).status == "pausada"
        A.db.session.get(A.Campaign, cid).status = "activa"; A.db.session.commit()
        assert any("sin creador" in t for _, t, _ in A.tech_alerts())
