"""Pruebas automáticas de NexorClips. Ejecutar con:  python -m pytest -q"""
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
        if verify:                                   # en la mayoría de pruebas, cuentas ya verificadas por el admin
            with A.app.app_context():
                u = A.User.query.filter_by(email=email).one()
                for p in A.PLATFORMS:
                    for h in u.handles(p):
                        u.set_verified(p, h)
                A.db.session.commit()
    return c


WALLET = "TQn9Y2khEsLJW1ChVWFMSMeRDow5KcbLSE"


def post(c, path, **data):
    data.setdefault("_csrf", "t")
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
    assert "vincula tu cuenta de TikTok" in flashes(r)[0][1]
    post(c, "/cuenta", section="redes", handles_tiktok="@Ana")
    assert "no está entre tus cuentas" in flashes(post(c, f"/campana/{cid}", url=tt("otra", 1)))[0][1]
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
    set_views(monkeypatch, {str(7300000000000000001): 200_000})     # 20 $
    with A.app.app_context():
        A.refresh_views()
    post(a, "/cuenta", section="cobro", payout_method=A.USDT_METHOD, payout_details=WALLET)
    assert "mínimo" in flashes(post(a, "/ganancias/retirar"))[0][1]
    set_views(monkeypatch, {str(7300000000000000001): 450_000})     # 45 $
    with A.app.app_context():
        A.refresh_views()
    assert flashes(post(a, "/ganancias/retirar"))[0][0] == "ok"
    assert "en curso" in flashes(post(a, "/ganancias/retirar"))[0][1]
    with A.app.app_context():
        p = A.Payout.query.one()
        assert p.amount == 45.0
        pid = p.id
    post(admin, "/admin/clippers", payout_id=pid, action="pagar", note="tx-1")
    post(admin, "/admin/clippers", payout_id=pid, action="rechazar")          # ya pagado: no cambia
    with A.app.app_context():
        u = A.User.query.filter_by(email="a@test.com").one()
        m = A.user_money(u)
        assert get(A.Payout, pid).status == "pagado" and m["available"] == 0 and m["paid"] == 45.0


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
        assert camp.cover_mime == "image/jpeg" and camp.cover_data[:2] == b"\xff\xd8" and camp.report_token


def test_all_pages_render():
    cid = make_campaign()
    admin = client_for("admin@test.com")
    a = client_for("a@test.com", tiktok="ana")
    post(a, f"/campana/{cid}", url=tt("ana", 1))
    for path in ["/", "/?cat=musica", f"/campana/{cid}", "/enviar", "/ganancias", "/ganancias?tab=pendiente",
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
    assert "Funciona" in html and "1,234" in html and "@ana" in html


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


def test_old_clips_are_not_refreshed(monkeypatch):
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
    assert 'class="dot"' in home                                                    # aviso de no leído
    page = a.get("/soporte").get_data(as_text=True)
    assert "En 7 días hábiles &lt;b&gt;ok&lt;/b&gt;" in page                        # sin HTML inyectado
    assert 'class="dot"' not in a.get("/").get_data(as_text=True)
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
    assert "Empezar a clipear" in html and "Lanzar una campaña" in html
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
    assert 'class="count"' in admin.get("/admin").get_data(as_text=True)
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
def test_only_usdt_payouts():
    c = client_for("u@test.com")
    r = post(c, "/cuenta", section="cobro", payout_method="PayPal", payout_details="u@paypal.com")
    assert "solo está disponible el cobro en cripto" in r.get_data(as_text=True)
    assert "red TRC20" in post(c, "/cuenta", section="cobro", payout_method=A.USDT_METHOD, payout_details="0xabc").get_data(as_text=True)
    with A.app.app_context():
        assert not A.User.query.filter_by(email="u@test.com").one().payout_details
    post(c, "/cuenta", section="cobro", payout_method=A.USDT_METHOD, payout_details=WALLET)
    with A.app.app_context():
        u = A.User.query.filter_by(email="u@test.com").one()
        assert u.payout_details == WALLET and u.payout_method == A.USDT_METHOD
    assert 'value="Cripto (USDT · TRC20)" selected' in client_for("nuevo@test.com").get("/cuenta").get_data(as_text=True)


def test_account_verification_by_admin():
    cid = make_campaign()
    admin = client_for("admin@test.com")
    a = client_for("a@test.com", verify=False, tiktok="ana")
    html = a.get("/cuenta").get_data(as_text=True)
    assert "Pendiente de verificar" in html and "hablar con soporte" in html and "bio" not in html
    assert "quiero verificar mis cuentas: @ana (TikTok)" in a.get("/soporte?verificar=1").get_data(as_text=True)
    r = post(a, f"/campana/{cid}", url=tt("ana", 1))
    assert "aún no está verificada" in r.get_data(as_text=True)
    with A.app.app_context():
        sid = A.Submission.query.one().id
        uid = A.User.query.filter_by(email="a@test.com").one().id
    r = post(admin, f"/admin/envio/{sid}/aprobar")
    assert "no puedes aprobar" in r.get_data(as_text=True).lower()
    with A.app.app_context():
        assert get(A.Submission, sid).status == "pendiente"
    post(admin, f"/admin/clipper/{uid}/cuenta", platform="tiktok", handle="ana", accion="verificar")
    assert "ya está verificada" in a.get("/soporte").get_data(as_text=True)
    post(admin, f"/admin/envio/{sid}/aprobar")
    with A.app.app_context():
        assert get(A.Submission, sid).status == "aprobado"
    # rechazar una cuenta la quita y avisa al clipper
    post(admin, f"/admin/clipper/{uid}/cuenta", platform="tiktok", handle="ana", accion="rechazar")
    with A.app.app_context():
        u = get(A.User, uid)
        assert u.handles("tiktok") == [] and not u.verified_set
    # al quitar una cuenta y volver a añadirla, hay que verificarla otra vez
    post(a, "/cuenta", section="redes", handles_tiktok="ana")
    with A.app.app_context():
        assert not get(A.User, uid).is_verified("tiktok", "ana")
    assert A.app.test_client().post(f"/admin/clipper/{uid}/cuenta").status_code in (302, 400)


def test_landing_next_step():
    c = A.app.test_client()
    assert "Siguiente" in c.get("/").get_data(as_text=True)
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
    assert "Siguiente" in c.get("/").get_data(as_text=True)
