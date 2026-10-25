"""NexorClips — plataforma de clipping: campañas, clips, vistas y pagos."""
import csv
import io
import json
import logging
import math
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta
from functools import wraps
from urllib.parse import urlencode

import requests
from flask import (Flask, Response, abort, flash, redirect, render_template,
                   g, request, session, url_for)
from flask_login import (LoginManager, UserMixin, current_user, login_required,
                         login_user, logout_user)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect as sa_inspect, text
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

import views as viewcounter

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("nexorclips")

# ---------------------------------------------------------------- configuración
PRODUCTION = bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("PRODUCTION") == "1")

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

secret = os.environ.get("SECRET_KEY", "")
if not secret:
    if PRODUCTION:
        raise RuntimeError("Falta la variable SECRET_KEY en Railway. Añádela y vuelve a desplegar.")
    secret = "solo-para-pruebas-en-local"
app.config.update(
    SECRET_KEY=secret,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=PRODUCTION,
    REMEMBER_COOKIE_SECURE=PRODUCTION,
    REMEMBER_COOKIE_HTTPONLY=True,
    REMEMBER_COOKIE_DURATION=timedelta(days=30),
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    MAX_CONTENT_LENGTH=8 * 1024 * 1024,
    SEND_FILE_MAX_AGE_DEFAULT=31536000,          # css, js, fuentes e iconos: el móvil los guarda un año
    COMPRESS_MIMETYPES=["text/html", "text/css", "application/javascript", "text/javascript", "application/json",
                        "image/svg+xml", "application/manifest+json"],
    COMPRESS_MIN_SIZE=600,
)
from flask_compress import Compress
Compress(app)                                     # envía las páginas comprimidas: cargan mucho antes con datos móviles
db_url = (os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_PRIVATE_URL")
          or os.environ.get("DATABASE_PUBLIC_URL") or "").strip().strip('"').strip("'")
if not db_url and os.environ.get("PGHOST"):      # también vale si Railway nos da las piezas sueltas
    from urllib.parse import quote
    db_url = "postgresql://{}:{}@{}:{}/{}".format(quote(os.environ.get("PGUSER", "postgres")), quote(os.environ.get("PGPASSWORD", "")),
                                                   os.environ["PGHOST"], os.environ.get("PGPORT", "5432"), os.environ.get("PGDATABASE", "railway"))
db_url = db_url or "sqlite:///plataforma.db"
if PRODUCTION and not db_url.startswith(("postgres://", "postgresql")):
    seen = sorted(k for k in os.environ if "DATABASE" in k or k.startswith("PG"))
    raise RuntimeError("Falta la base de datos. En Railway: añade PostgreSQL y en la web la variable "
                       "DATABASE_URL=${{Postgres.DATABASE_URL}}. Variables de base de datos que veo: "
                       + (", ".join(f"{k}={'(vacía)' if not os.environ.get(k) else os.environ[k][:11] + '…'}" for k in seen) or "ninguna"))
for prefix in ("postgres://", "postgresql://"):   # Railway da "postgresql://"; usamos el conector psycopg2
    if db_url.startswith(prefix):
        db_url = "postgresql+psycopg2://" + db_url[len(prefix):]
app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True, "pool_recycle": 280}

APP_NAME = os.environ.get("APP_NAME", "ClipProfit Hispano")
SUPPORT_EMAIL = os.environ.get("SUPPORT_EMAIL", "")
CURRENCY = os.environ.get("CURRENCY", "$")
CURRENCY_CODE = os.environ.get("CURRENCY_CODE", "USD")
REFRESH_DAYS = int(os.environ.get("REFRESH_DAYS", "14"))       # dejar de actualizar clips más antiguos
MIN_WITHDRAW = float(os.environ.get("MIN_WITHDRAW", "30"))
MAX_CLIPS_DAY = int(os.environ.get("MAX_CLIPS_DAY", "30"))     # límite de clips por clipper y día
PAYOUT_DAYS = int(os.environ.get("PAYOUT_DAYS", "7"))          # plazo de pago que se promete al clipper
ADMIN_EMAILS = {e.strip().lower() for e in os.environ.get("ADMIN_EMAIL", "").split(",") if e.strip()}
CRON_SECRET = os.environ.get("CRON_SECRET", "")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()           # avisos al móvil del admin con la app ntfy
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
WHOP_API_KEY = os.environ.get("WHOP_API_KEY", "").strip()
WHOP_COMPANY_ID = os.environ.get("WHOP_COMPANY_ID", "").strip()
WHOP_API = os.environ.get("WHOP_API_URL", "https://api.whop.com/api/v1").rstrip("/")
WHOP_AUTO_MAX = float(os.environ.get("WHOP_AUTO_MAX", "500"))   # por encima, el retiro espera a que lo apruebe un admin
WHOP_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{2,40}$")
BASE_URL = os.environ.get("BASE_URL", "").strip().strip('"').rstrip("/")
if BASE_URL and not BASE_URL.startswith("http"):
    BASE_URL = "https://" + BASE_URL
DEV_LOGIN = os.environ.get("DEV_LOGIN") == "1" and not PRODUCTION   # nunca disponible en producción

PLATFORMS = {"tiktok": "TikTok", "instagram": "Instagram", "youtube": "YouTube Shorts"}
CATEGORIES = {"musica": "Música", "streamer": "Streamers", "marca": "Marcas", "infoproducto": "Infoproductos", "otro": "Otros"}
CATEGORY_ONE = {"musica": "Música", "streamer": "Streamer", "marca": "Marca", "infoproducto": "Infoproducto", "otro": "Otro"}
USDT_METHOD = "Cripto (USDT · TRC20)"
WHOP_METHOD = "Whop"
PAYOUT_METHODS = [WHOP_METHOD, USDT_METHOD, "PayPal", "Transferencia bancaria"]
PAYOUT_AVAILABLE = {WHOP_METHOD, USDT_METHOD, "Cripto (USDT)"}   # "Cripto (USDT)" es el nombre antiguo
PAYOUT_OPEN = ("solicitado", "procesando")                        # retiros que aún no han terminado
PAYOUT_ONLY_USDT_MSG = "De momento solo está disponible el cobro por Whop o en cripto (USDT, red TRC20). Elige una de esas opciones para guardar tu cuenta."
TRC20_RE = re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$")
FLAG_LABELS = {
    "cuenta_no_declarada": "Cuenta no vinculada",
    "cuenta_de_otro": "Cuenta de otro clipper",
    "subida_rapida": "Subida de vistas sospechosa",
    "pocos_likes": "Muy pocos likes para sus vistas",
}
MAX_HANDLES = 5
CENT = 0.005

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = None


# ---------------------------------------------------------------- modelos
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    name = db.Column(db.String(120), nullable=False)
    avatar_url = db.Column(db.String(500), default="")
    google_sub = db.Column(db.String(64), unique=True, nullable=True)
    is_admin = db.Column(db.Boolean, default=False)
    is_banned = db.Column(db.Boolean, default=False)
    ban_reason = db.Column(db.String(255), default="")
    payout_method = db.Column(db.String(60), default="")
    payout_details = db.Column(db.String(255), default="")
    whop_user_id = db.Column(db.String(40), default="")
    whop_username = db.Column(db.String(80), default="")
    tiktok_handles = db.Column(db.String(400), default="")
    instagram_handles = db.Column(db.String(400), default="")
    youtube_handles = db.Column(db.String(400), default="")
    verified_handles = db.Column(db.String(1200), default="")   # sin uso: la verificación ahora es por campaña
    verify_code = db.Column(db.String(12), default="")           # sin uso
    accepted_terms_at = db.Column(db.DateTime, nullable=True)
    last_seen_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def initial(self):
        return (self.name or self.email or "?")[0].upper()

    @property
    def payout_ready(self):
        """True si tiene una forma de cobro activa y completa."""
        if self.payout_method == WHOP_METHOD:
            return bool(self.whop_user_id) and whop_enabled()
        return self.payout_method in PAYOUT_AVAILABLE and bool(self.payout_details)

    @property
    def payout_label(self):
        if self.payout_method == WHOP_METHOD and self.whop_user_id:
            return f"Whop · @{self.whop_username}"
        if self.payout_details:
            d = self.payout_details
            return f"USDT · {d[:6]}…{d[-4:]}" if len(d) > 12 else f"USDT · {d}"
        return ""

    def handles(self, platform):
        raw = getattr(self, f"{platform}_handles", "") or ""
        return [h for h in raw.split(",") if h]

    @property
    def has_any_handle(self):
        return any(self.handles(p) for p in PLATFORMS)



class Campaign(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    song = db.Column(db.String(200), nullable=False)
    artist = db.Column(db.String(200), nullable=False)
    cover_url = db.Column(db.String(500), default="")
    cover_data = db.deferred(db.Column(db.LargeBinary, nullable=True))
    cover_mime = db.Column(db.String(40), default="")
    cover_version = db.Column(db.Integer, default=0)       # 0 = sin imagen subida
    sound_url = db.Column(db.String(500), default="")
    rules = db.Column(db.Text, default="")
    rate = db.Column(db.Float, nullable=False)              # pago por cada 1.000.000 de vistas
    budget = db.Column(db.Float, nullable=False)
    spent = db.Column(db.Float, default=0.0)                # dinero ya asignado a clips (libro de cuentas)
    min_views = db.Column(db.Integer, default=0)            # mínimo para cobrar un vídeo
    max_per_video = db.Column(db.Float, nullable=True)      # tope por vídeo (vacío = sin tope)
    platforms = db.Column(db.String(100), default="tiktok,instagram,youtube")
    status = db.Column(db.String(20), default="activa")     # activa / pausada / finalizada
    featured = db.Column(db.Boolean, default=False)
    category = db.Column(db.String(20), default="musica")
    report_token = db.Column(db.String(40), unique=True, nullable=True)
    ended_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    submissions = db.relationship("Submission", backref="campaign", lazy=True)

    @property
    def platform_list(self):
        return [p for p in (self.platforms or "").split(",") if p]

    @property
    def remaining(self):
        return max(round((self.budget or 0) - (self.spent or 0), 2), 0.0)

    @property
    def exhausted(self):
        return (self.spent or 0) >= (self.budget or 0) - CENT


class Submission(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey("campaign.id"), nullable=False, index=True)
    url = db.Column(db.String(500), nullable=False)
    platform = db.Column(db.String(20), nullable=False)
    video_key = db.Column(db.String(120), unique=True, nullable=False)  # evita repetir vídeos
    views = db.Column(db.Integer, default=0)
    likes = db.Column(db.Integer, nullable=True)
    author = db.Column(db.String(80), default="")
    earned = db.Column(db.Float, default=0.0)              # dinero asignado a este clip; nunca baja solo
    flags = db.Column(db.String(200), default="")
    status = db.Column(db.String(20), default="pendiente")  # pendiente / aprobado / rechazado
    reject_reason = db.Column(db.String(255), default="")
    views_updated_at = db.Column(db.DateTime, nullable=True)
    checked_at = db.Column(db.DateTime, nullable=True)
    views_error = db.Column(db.String(255), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    user = db.relationship("User", backref="submissions")

    @property
    def flag_list(self):
        return [f for f in (self.flags or "").split(",") if f]

    def add_flag(self, f):
        if f not in self.flag_list:
            self.flags = ",".join(self.flag_list + [f])

    def drop_flag(self, f):
        self.flags = ",".join(x for x in self.flag_list if x != f)


class ViewSnapshot(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    submission_id = db.Column(db.Integer, db.ForeignKey("submission.id"), nullable=False, index=True)
    views = db.Column(db.Integer, nullable=False)
    taken_at = db.Column(db.DateTime, default=datetime.utcnow)


class Payout(db.Model):
    """Retiros: el clipper los solicita y el admin los marca como pagados."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    amount = db.Column(db.Float, nullable=False)
    method = db.Column(db.String(60), default="")
    details = db.Column(db.String(255), default="")
    status = db.Column(db.String(20), default="solicitado")  # solicitado / procesando / pagado / rechazado
    note = db.Column(db.String(255), default="")
    ext_id = db.Column(db.String(80), default="")    # ID de la transferencia en Whop
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    paid_at = db.Column(db.DateTime, nullable=True)
    user = db.relationship("User", backref="payouts")


class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    admin_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    action = db.Column(db.String(60), nullable=False)
    detail = db.Column(db.String(500), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    admin = db.relationship("User")


class Message(db.Model):
    """Chat de soporte: cada clipper tiene una conversación privada con el equipo (admins)."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)   # dueño de la conversación
    from_admin = db.Column(db.Boolean, default=False)
    sender_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    body = db.Column(db.Text, nullable=False)
    read_by_admin = db.Column(db.Boolean, default=False)
    read_by_user = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    user = db.relationship("User", foreign_keys=[user_id])
    sender = db.relationship("User", foreign_keys=[sender_id])


class CampaignAccount(db.Model):
    """Cuenta con la que un clipper se une a una campaña. Solo puede subir vídeos cuando el equipo la acepta."""
    __table_args__ = (db.UniqueConstraint("campaign_id", "platform", "handle", name="uq_campaign_account"),)
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey("campaign.id"), nullable=False, index=True)
    platform = db.Column(db.String(20), nullable=False)
    handle = db.Column(db.String(100), nullable=False)
    status = db.Column(db.String(20), default="pendiente", index=True)    # pendiente · aceptada · rechazada
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    decided_at = db.Column(db.DateTime, nullable=True)
    user = db.relationship("User")
    campaign = db.relationship("Campaign")


JOIN_STATUSES = {"pendiente": "Pendiente de verificar", "aceptada": "Aceptada", "rechazada": "Rechazada"}
MAX_JOIN_ACCOUNTS = 5


def campaign_accounts(user_id, campaign_id, status=None, platform=None):
    q = CampaignAccount.query.filter_by(user_id=user_id, campaign_id=campaign_id)
    if status:
        q = q.filter_by(status=status)
    if platform:
        q = q.filter_by(platform=platform)
    return q.order_by(CampaignAccount.created_at).all()


class CampaignLead(db.Model):
    """Solicitudes de artistas y marcas que quieren lanzar una campaña (desde la portada)."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(255), nullable=False)
    brand = db.Column(db.String(200), nullable=False)          # artista, marca o creador
    category = db.Column(db.String(20), default="musica")
    budget = db.Column(db.String(40), default="")
    link = db.Column(db.String(500), default="")
    message = db.Column(db.Text, default="")
    status = db.Column(db.String(20), default="nueva")         # nueva / contactada / convertida / descartada
    ip = db.Column(db.String(64), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


class Setting(db.Model):
    key = db.Column(db.String(60), primary_key=True)
    value = db.Column(db.String(500), default="")


@login_manager.user_loader
def load_user(uid):
    try:
        return db.session.get(User, int(uid))
    except (TypeError, ValueError):
        return None


def ensure_schema():
    """Crea las tablas y añade columnas nuevas a tablas existentes (migración sencilla y segura)."""
    db.create_all()
    insp = sa_inspect(db.engine)
    dialect = db.engine.dialect
    for table in db.metadata.sorted_tables:
        existing = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in existing:
                continue
            ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col.type.compile(dialect=dialect)}'
            default = col.default.arg if col.default is not None and not callable(col.default.arg) else None
            if isinstance(default, bool):
                ddl += " DEFAULT " + ("TRUE" if default else "FALSE")
            elif isinstance(default, (int, float)):
                ddl += f" DEFAULT {default}"
            elif isinstance(default, str):
                ddl += " DEFAULT '" + default.replace("'", "''") + "'"
            with db.engine.begin() as conn:
                conn.execute(text(ddl))
            log.info("Columna añadida: %s.%s", table.name, col.name)


with app.app_context():
    ensure_schema()


# ---------------------------------------------------------------- utilidades generales
def audit(action, detail=""):
    db.session.add(AuditLog(admin_id=current_user.id if current_user.is_authenticated else None,
                            action=action, detail=str(detail)[:500]))


def set_setting(key, value):
    db.session.merge(Setting(key=key, value=value))
    db.session.commit()


def get_setting(key, default=""):
    row = db.session.get(Setting, key)
    return row.value if row else default


def parse_float(value, default=None):
    try:
        v = float(str(value).replace(",", ".").strip())
        return v if v == v and abs(v) < 1e12 else default   # descarta NaN y cifras absurdas
    except (TypeError, ValueError):
        return default


def csv_safe(v):
    """Evita que Excel o Google Sheets ejecuten como fórmula un texto que empiece por = + - @."""
    v = "" if v is None else str(v)
    return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


def cents(v):
    return round(float(v or 0) + 1e-9, 2)


# ---------------------------------------------------------------- dinero (libro de cuentas)
def raw_earning(sub, camp):
    """Lo que valdría el clip por sus vistas, con mínimo y tope por clip aplicados."""
    if (sub.views or 0) < (camp.min_views or 0):
        return 0.0
    amount = (sub.views or 0) / 1_000_000 * camp.rate
    if camp.max_per_video:
        amount = min(amount, camp.max_per_video)
    return cents(amount)


def finalize_if_exhausted(camp):
    if camp.exhausted and camp.status == "activa":
        camp.status, camp.ended_at = "finalizada", datetime.utcnow()
        log.info("Campaña %s agotada y finalizada", camp.id)


def accrue(sub, allow_decrease=False):
    """Asigna al clip el dinero que le corresponde, sin pasarse del presupuesto de la campaña.

    Las ganancias de un clip solo suben automáticamente. Solo una corrección manual del admin
    (allow_decrease=True) puede bajarlas.
    """
    if sub.status != "aprobado":
        return 0.0
    camp = sub.campaign
    target = raw_earning(sub, camp)
    current = sub.earned or 0.0
    delta = cents(target - current)
    if delta < 0 and not allow_decrease:
        return 0.0
    if delta > 0:
        delta = min(delta, camp.remaining)
    if abs(delta) < CENT:
        return 0.0
    sub.earned = cents(current + delta)
    camp.spent = cents((camp.spent or 0) + delta)
    finalize_if_exhausted(camp)
    return delta


def clawback(sub):
    """Quita al clip todo lo asignado (cuando el admin lo rechaza tras aprobarlo)."""
    amount = sub.earned or 0.0
    if amount:
        sub.campaign.spent = cents(max((sub.campaign.spent or 0) - amount, 0))
        sub.earned = 0.0
    return amount


def campaign_stats(camp):
    approved = [s for s in camp.submissions if s.status == "aprobado"]
    pending = [s for s in camp.submissions if s.status == "pendiente"]
    spent = cents(camp.spent)
    pct = min(100, round(spent / camp.budget * 100)) if camp.budget else 100
    return {
        "earnings": {s.id: cents(s.earned) for s in approved},
        "estimates": {s.id: min(raw_earning(s, camp), camp.remaining) for s in pending},
        "spent": spent,
        "remaining": camp.remaining,
        "pct": pct,
        "left_pct": 0 if camp.exhausted else max(0, 100 - pct),
        "views": sum(s.views or 0 for s in approved),
        "clips": len(approved),
        "pending": len(pending),
        "flagged": sum(1 for s in pending if s.flag_list),
        "exhausted": camp.exhausted,
        "effective_cpm": cents(spent / (sum(s.views or 0 for s in approved) or 1) * 1_000_000) if approved else 0,
    }


def user_money(user):
    """Disponible = ganado en clips aprobados − retirado. Pendiente = estimación de clips en revisión."""
    earned_rows, pending_rows, stats = [], [], {}
    for s in user.submissions:
        if s.status == "aprobado":
            earned_rows.append((s, cents(s.earned)))
        elif s.status == "pendiente":
            st = stats.setdefault(s.campaign_id, campaign_stats(s.campaign))
            pending_rows.append((s, st["estimates"].get(s.id, 0)))
    earned = cents(sum(a for _, a in earned_rows))
    withdrawn = cents(sum(p.amount for p in user.payouts if p.status in PAYOUT_OPEN + ("pagado",)))
    paid = cents(sum(p.amount for p in user.payouts if p.status == "pagado"))
    balance = cents(earned - withdrawn)
    return {
        "earned": earned,
        "pending": cents(sum(a for _, a in pending_rows)),
        "available": max(balance, 0.0),
        "debt": max(-balance, 0.0),
        "paid": paid,
        "open_payout": next((p for p in user.payouts if p.status in PAYOUT_OPEN), None),
        "earned_rows": sorted(earned_rows, key=lambda r: r[0].created_at, reverse=True),
        "pending_rows": sorted(pending_rows, key=lambda r: r[0].created_at, reverse=True),
    }


# ---------------------------------------------------------------- antifraude
def handle_owner(platform, handle, exclude_user_id=None):
    """Devuelve el usuario que tiene vinculada esa cuenta (o None)."""
    if not handle:
        return None
    col = getattr(User, f"{platform}_handles")
    for u in User.query.filter(col.ilike(f"%{handle}%")).all():
        if u.id != exclude_user_id and handle in u.handles(platform):
            return u
    return None


def check_author(sub):
    """Marca el clip si el autor del vídeo no es una cuenta del clipper."""
    sub.drop_flag("cuenta_no_declarada")
    sub.drop_flag("cuenta_de_otro")
    if not sub.author:
        return
    if sub.author in sub.user.handles(sub.platform):
        return
    owner = handle_owner(sub.platform, sub.author, exclude_user_id=sub.user_id)
    sub.add_flag("cuenta_de_otro" if owner else "cuenta_no_declarada")


def profile_url(platform, handle):
    return {"tiktok": f"https://www.tiktok.com/@{handle}", "instagram": f"https://www.instagram.com/{handle}/",
            "youtube": f"https://www.youtube.com/@{handle}"}[platform]


def account_verified(sub):
    """¿El clip viene de una cuenta aceptada por el equipo en esta campaña?"""
    ok = {a.handle for a in campaign_accounts(sub.user_id, sub.campaign_id, "aceptada", sub.platform)}
    if sub.author:
        return sub.author in ok
    return bool(ok)


def check_growth(sub, new_views, likes):
    last = (ViewSnapshot.query.filter_by(submission_id=sub.id)
            .order_by(ViewSnapshot.taken_at.desc()).first())
    if last:
        hours = (datetime.utcnow() - last.taken_at).total_seconds() / 3600
        gained = new_views - last.views
        if hours <= 24 and gained >= 100_000 and gained >= 4 * max(last.views, 1):
            sub.add_flag("subida_rapida")
    if likes is not None and new_views >= 20_000 and likes / new_views < 0.003:
        sub.add_flag("pocos_likes")
    else:
        sub.drop_flag("pocos_likes")


# ---------------------------------------------------------------- actualizar vistas
_refresh_lock = threading.Lock()


def apply_reading(sub, info, now):
    """Guarda una lectura de Apify en el clip: vistas, likes, autor, historial y avisos."""
    views = info["views"] if isinstance(info, dict) else int(info)
    likes = info.get("likes") if isinstance(info, dict) else None
    author = info.get("author") if isinstance(info, dict) else ""
    sub.checked_at = now
    sub.views_error = ""
    if author:
        sub.author = author
    if likes is not None:
        sub.likes = likes
    check_author(sub)
    if views > (sub.views or 0):
        check_growth(sub, views, likes)
        sub.views = views
        sub.views_updated_at = now
        db.session.add(ViewSnapshot(submission_id=sub.id, views=views, taken_at=now))
    elif likes is not None:
        check_growth(sub, sub.views or 0, likes)


def refresh_views():
    """Pide a Apify las vistas de los clips recientes de campañas activas y reparte el dinero."""
    if not _refresh_lock.acquire(blocking=False):
        return "Ya hay una actualización en marcha."
    try:
        since = datetime.utcnow() - timedelta(days=REFRESH_DAYS)
        subs = (Submission.query.join(Campaign)
                .filter(Campaign.status == "activa", Submission.status != "rechazado",
                        Submission.created_at >= since)
                .order_by(Submission.created_at).all())
        subs = [s for s in subs if not s.campaign.exhausted]
        read, errors = 0, []
        for platform in PLATFORMS:
            group = [s for s in subs if s.platform == platform]
            for i in range(0, len(group), 50):
                chunk = group[i:i + 50]
                try:
                    found = viewcounter.fetch_views(platform, [s.url for s in chunk])
                except Exception as exc:  # noqa: BLE001
                    log.exception("Error de Apify (%s)", platform)
                    errors.append(f"{PLATFORMS[platform]}: {exc}")
                    for s in chunk:
                        s.views_error = "Error al consultar Apify"
                    db.session.commit()
                    continue
                now = datetime.utcnow()
                for s in chunk:
                    info = found.get(s.video_key.split(":", 1)[1])
                    if info is None:
                        s.checked_at = now
                        s.views_error = "No se pudieron leer las vistas (¿vídeo borrado o privado?)"
                    else:
                        apply_reading(s, info, now)
                        read += 1
                db.session.commit()
        # Reparto del dinero por orden de envío, hasta agotar cada presupuesto
        assigned = 0.0
        for s in sorted((s for s in subs if s.status == "aprobado"), key=lambda s: s.created_at):
            assigned += accrue(s)
        db.session.commit()
        msg = f"Vistas leídas en {read} de {len(subs)} clips · {money(assigned)} repartidos."
        if errors:
            msg += " Errores: " + " | ".join(errors)
        log.info(msg)
        set_setting("last_refresh", datetime.utcnow().strftime("%d/%m/%Y %H:%M") + " UTC · " + msg)
        return msg
    except Exception:  # noqa: BLE001
        db.session.rollback()
        log.exception("Fallo actualizando vistas")
        set_setting("last_refresh", datetime.utcnow().strftime("%d/%m/%Y %H:%M") + " UTC · Error inesperado, revisa los logs de Railway.")
        raise
    finally:
        _refresh_lock.release()


def refresh_in_background():
    def run():
        with app.app_context():
            try:
                refresh_views()
            except Exception:  # noqa: BLE001
                pass
    threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------- seguridad
def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(16)
    return session["_csrf"]


@app.before_request
def _start_timer():
    g._t0 = time.perf_counter()


@app.before_request
def before():
    if request.method == "POST":
        sent, expected = request.form.get("_csrf", ""), session.get("_csrf", "")
        if not sent or not expected or not secrets.compare_digest(sent, expected):
            abort(400, description="csrf")
    if current_user.is_authenticated:
        now = datetime.utcnow()
        if not current_user.last_seen_at or (now - current_user.last_seen_at).total_seconds() > 600:
            current_user.last_seen_at = now
            db.session.commit()
        if current_user.is_banned and request.endpoint not in ("logout", "static", "terms", "privacy", "health",
                                                               "support", "support_poll", "notices"):
            return render_template("banned.html"), 403
    return None


CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "font-src 'self'; img-src 'self' data: https:; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    resp.headers.setdefault("Content-Security-Policy", CSP)
    if getattr(g, "_t0", None):     # cuánto tarda el servidor en preparar la página (sin contar internet)
        resp.headers["Server-Timing"] = f"app;dur={(time.perf_counter() - g._t0) * 1000:.0f}"
    if resp.mimetype == "text/html":
        # Chrome/Android: prepara la página siguiente en cuanto el dedo o el ratón van hacia un enlace
        resp.headers.setdefault("Speculation-Rules", '"/reglas-precarga.json"')
    if PRODUCTION:
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return resp


# Páginas que NO se deben precargar: cierran sesión, marcan mensajes como leídos o hacen acciones.
NO_PRELOAD = ["/salir", "/auth/*", "/login*", "/empezar*", "/cron/*", "/soporte*", "/admin/mensajes*",
              "/avisos", "/static/*", "/admin/*csv*"]


@app.route("/reglas-precarga.json")
def speculation_rules():
    rules = {"prerender": [{
        "where": {"and": [{"href_matches": "/*"}, {"not": {"href_matches": NO_PRELOAD}},
                          {"not": {"selector_matches": "[data-no-preload], [target=_blank], [download]"}}]},
        "eagerness": "moderate"}]}
    resp = app.response_class(json.dumps(rules), mimetype="application/speculationrules+json")
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


ERROR_TEXT = {
    400: ("La página ha caducado", "Vuelve atrás, recarga la página e inténtalo otra vez."),
    403: ("No tienes acceso", "Esta sección no está disponible para tu cuenta."),
    404: ("No encontramos esta página", "Puede que el enlace esté mal o que la campaña ya no exista."),
    413: ("El archivo es demasiado grande", "Sube una imagen de menos de 8 MB."),
    500: ("Algo ha fallado", "Ya estamos avisados. Inténtalo de nuevo en unos minutos."),
}


@app.errorhandler(HTTPException)
def http_error(e):
    title, body = ERROR_TEXT.get(e.code, (e.name, e.description or ""))
    return render_template("error.html", code=e.code, title=title, body=body), e.code


@app.errorhandler(Exception)
def unhandled_error(e):
    db.session.rollback()
    log.exception("Error no controlado")
    title, body = ERROR_TEXT[500]
    return render_template("error.html", code=500, title=title, body=body), 500


def admin_required(f):
    @wraps(f)
    @login_required
    def wrapper(*a, **kw):
        if not current_user.is_admin:
            abort(403)
        return f(*a, **kw)
    return wrapper


# ---------------------------------------------------------------- plantillas
def cover_src(camp):
    if camp.cover_version:
        return url_for("cover", cid=camp.id, v=camp.cover_version)
    return camp.cover_url or ""


def split_logo(name):
    """'NexorClips' -> ('NEXOR', 'CLIPS'): la parte final 'clips' va en otro color."""
    flat = name.replace(" ", "")
    if len(flat) > 5 and flat.lower().endswith("clips"):
        return flat[:-5].upper(), "CLIPS"
    return name.upper(), ""


LOGO_MAIN, LOGO_SUB = split_logo(APP_NAME)


_static_versions = {}


def static_url(filename):
    """Dirección del archivo con su versión: si cambia, el móvil descarga el nuevo; si no, usa el guardado."""
    if filename not in _static_versions:
        try:
            _static_versions[filename] = int(os.path.getmtime(os.path.join(app.static_folder, filename)))
        except OSError:
            _static_versions[filename] = 0
    return url_for("static", filename=filename, v=_static_versions[filename])


@app.context_processor
def inject_globals():
    return {"csrf_token": csrf_token, "static_url": static_url, "APP_NAME": APP_NAME, "CUR": CURRENCY, "CUR_CODE": CURRENCY_CODE,
            "PLATFORMS": PLATFORMS, "cover_src": cover_src, "min_pay": min_pay, "MIN_WITHDRAW": MIN_WITHDRAW,
            "PAYOUT_METHODS": PAYOUT_METHODS, "PAYOUT_AVAILABLE": PAYOUT_AVAILABLE, "USDT_METHOD": USDT_METHOD, "WHOP_METHOD": WHOP_METHOD, "WHOP_AUTO_MAX": WHOP_AUTO_MAX, "whop_enabled": whop_enabled, "account_verified": account_verified, "profile_url": profile_url, "CATEGORIES": CATEGORIES, "CATEGORY_ONE": CATEGORY_ONE,
            "LOGO_MAIN": LOGO_MAIN, "LOGO_SUB": LOGO_SUB, "SUPPORT_EMAIL": SUPPORT_EMAIL,
            "FLAG_LABELS": FLAG_LABELS, "PAYOUT_DAYS": PAYOUT_DAYS, "YEAR": datetime.utcnow().year,
            "REFRESH_DAYS": REFRESH_DAYS, "unread_messages": unread_messages, "notice_data": notice_data, "new_leads": new_leads}


@app.template_filter("rate_k")
def rate_k_filter(rate_per_million):
    """Tarifa guardada por 1M de vistas → texto por cada 1.000 vistas ($1.00)."""
    return money((rate_per_million or 0) / 1000)


def min_pay(camp):
    """Lo mínimo que cobra un clip: las vistas mínimas al precio de la campaña."""
    return round((camp.min_views or 0) / 1_000_000 * (camp.rate or 0), 2)


@app.template_filter("money")
def money(v, decimals=2):
    return f"{CURRENCY}{(v or 0):,.{decimals}f}"


@app.template_filter("amount")
def amount(v):
    return f"{(v or 0):,.2f}"


@app.template_filter("num")
def num(v):
    return f"{int(v or 0):,}"


@app.template_filter("short")
def short(v):
    v = int(v or 0)
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}M".replace(".0M", "M")
    if v >= 1_000:
        return f"{v / 1_000:.1f}K".replace(".0K", "K")
    return str(v)


@app.template_filter("ago")
def ago(dt):
    if not dt:
        return ""
    secs = max((datetime.utcnow() - dt).total_seconds(), 0)
    if secs < 3600:
        return f"hace {max(1, int(secs // 60))} min"
    if secs < 86400:
        return f"hace {int(secs // 3600)} h"
    return f"hace {int(secs // 86400)} d"


@app.template_filter("checked")
def checked(sub):
    if not sub.checked_at:
        return "Pendiente de primera lectura"
    return "Actualizado " + ago(sub.checked_at)


@app.template_filter("date")
def date(dt):
    return dt.strftime("%d/%m/%Y") if dt else ""


@app.template_filter("datetime")
def datetime_f(dt):
    return dt.strftime("%d/%m/%Y %H:%M") if dt else ""


def active_campaigns():
    """Campañas activas: la destacada primero, después la música y luego el resto, de más nueva a más antigua."""
    camps = Campaign.query.filter_by(status="activa").order_by(Campaign.created_at.desc()).all()
    return sorted(camps, key=lambda c: (not c.featured, c.category != "musica"))


# ---------------------------------------------------------------- acceso con Google
def redirect_uri():
    if BASE_URL:
        uri = BASE_URL + url_for("google_callback")
    else:
        uri = url_for("google_callback", _external=True, _scheme="https" if PRODUCTION else None)
    if PRODUCTION and uri.startswith("http://"):          # Google solo acepta https en producción
        uri = "https://" + uri[len("http://"):]
    return uri


def sign_in(email, name, avatar="", sub=None):
    email = email.lower()
    user = (User.query.filter_by(google_sub=sub).first() if sub else None) or User.query.filter_by(email=email).first()
    if not user:
        user = User(email=email, name=(name or email.split("@")[0])[:120], accepted_terms_at=datetime.utcnow())
        db.session.add(user)
        log.info("Nuevo usuario: %s", email)
    user.google_sub = sub or user.google_sub
    user.avatar_url = (avatar or user.avatar_url or "")[:500]
    if not user.accepted_terms_at:
        user.accepted_terms_at = datetime.utcnow()
    if email in ADMIN_EMAILS:
        user.is_admin = True
    db.session.commit()
    session.pop("_csrf", None)            # código de seguridad nuevo en cada inicio de sesión
    session.permanent = True
    login_user(user, remember=True)
    return user


def safe_next(nxt):
    return nxt if nxt and nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt else url_for("index")


@app.route("/login")
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    return render_template("login.html", google_ready=bool(GOOGLE_CLIENT_ID), dev_login=DEV_LOGIN)


@app.route("/login/google")
def login_google():
    if not GOOGLE_CLIENT_ID:
        flash("El acceso con Google aún no está configurado.", "error")
        return redirect(url_for("login"))
    state = secrets.token_urlsafe(24)
    session["oauth_state"] = state
    session["after_login"] = request.args.get("next", "")
    params = {"client_id": GOOGLE_CLIENT_ID, "redirect_uri": redirect_uri(), "response_type": "code",
              "scope": "openid email profile", "state": state, "prompt": "select_account"}
    return redirect("https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params))


@app.route("/auth/google/callback")
def google_callback():
    if request.args.get("error"):
        flash("Has cancelado el inicio de sesión con Google.", "error")
        return redirect(url_for("login"))
    if not request.args.get("code") or request.args.get("state") != session.pop("oauth_state", None):
        flash("No se pudo iniciar sesión. Inténtalo otra vez.", "error")
        return redirect(url_for("login"))
    try:
        token = requests.post("https://oauth2.googleapis.com/token", timeout=15, data={
            "code": request.args["code"], "client_id": GOOGLE_CLIENT_ID, "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": redirect_uri(), "grant_type": "authorization_code"}).json()
        info = requests.get("https://openidconnect.googleapis.com/v1/userinfo", timeout=15,
                            headers={"Authorization": f"Bearer {token.get('access_token', '')}"}).json()
    except (requests.RequestException, ValueError):
        log.exception("Fallo en el login con Google")
        flash("Google no respondió. Inténtalo otra vez.", "error")
        return redirect(url_for("login"))
    if not info.get("email") or not info.get("email_verified"):
        flash("Tu cuenta de Google no tiene un correo verificado.", "error")
        return redirect(url_for("login"))
    sign_in(info["email"], info.get("name", ""), info.get("picture", ""), info.get("sub"))
    return redirect(safe_next(session.pop("after_login", "")))


@app.route("/login/dev", methods=["POST"])
def login_dev():
    if not DEV_LOGIN:
        abort(404)
    email = request.form.get("email", "").strip()
    if "@" not in email:
        abort(400)
    sign_in(email, email.split("@")[0])
    return redirect(url_for("index"))


@app.route("/salir")
def logout():
    session.clear()
    logout_user()        # después de vaciar la sesión: así también borra la cookie de «recordarme»
    resp = redirect(url_for("index"))
    resp.delete_cookie(app.config.get("REMEMBER_COOKIE_NAME", "remember_token"))
    return resp


# ---------------------------------------------------------------- páginas públicas
@app.route("/terminos")
def terms():
    return render_template("legal_terms.html")


@app.route("/privacidad")
def privacy():
    return render_template("legal_privacy.html")


@app.route("/informe/<token>")
def report(token):
    camp = Campaign.query.filter_by(report_token=token).first() or abort(404)
    st = campaign_stats(camp)
    approved = sorted((s for s in camp.submissions if s.status == "aprobado"), key=lambda s: -(s.views or 0))
    by_platform = {p: {"clips": 0, "views": 0} for p in PLATFORMS}
    for s in approved:
        by_platform[s.platform]["clips"] += 1
        by_platform[s.platform]["views"] += s.views or 0
    resp = Response(render_template("report.html", camp=camp, st=st, top=approved[:20], by_platform=by_platform,
                                    creators=len({s.user_id for s in approved})))
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


@app.route("/salud")
def health():
    db.session.execute(text("SELECT 1"))
    return {"ok": True}


@app.route("/portada/<int:cid>")
def cover(cid):
    camp = db.session.get(Campaign, cid)
    if not camp or not camp.cover_version:
        abort(404)
    resp = Response(camp.cover_data, mimetype=camp.cover_mime or "image/jpeg")
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


# ---------------------------------------------------------------- clipper
@app.route("/")
def index():
    if not current_user.is_authenticated:
        return landing()
    all_cards = [(c, campaign_stats(c)) for c in active_campaigns()]
    featured = all_cards[0] if all_cards else None
    cat = request.args.get("cat", "")
    cards = [x for x in all_cards if not cat or x[0].category == cat]
    counts = {k: sum(1 for c, _ in all_cards if c.category == k) for k in CATEGORIES}
    my_join = {}
    for a in CampaignAccount.query.filter_by(user_id=current_user.id).all():
        if my_join.get(a.campaign_id) != "aceptada":
            my_join[a.campaign_id] = a.status
    return render_template("home.html", cards=cards, featured=featured, cat=cat, counts=counts,
                           total=len(all_cards), new_user=not current_user.submissions, my_join=my_join)


def submit_clip(camp, raw_url):
    """Valida y guarda un clip. Devuelve (ok, mensaje)."""
    if camp.status != "activa" or camp.exhausted:
        return False, "Esta campaña ya no acepta clips."
    today = datetime.utcnow() - timedelta(hours=24)
    if Submission.query.filter(Submission.user_id == current_user.id, Submission.created_at >= today).count() >= MAX_CLIPS_DAY:
        return False, f"Has llegado al límite de {MAX_CLIPS_DAY} clips en 24 horas. Vuelve a intentarlo mañana."
    url = viewcounter.normalize_url(raw_url)
    platform, vid = viewcounter.detect_platform(url), viewcounter.video_id(url)
    if not platform or not vid:
        return False, "Ese enlace no es válido. Pega el enlace directo al vídeo, no al perfil."
    if platform not in camp.platform_list:
        return False, f"Esta campaña no acepta vídeos de {PLATFORMS[platform]}."
    mine = [a.handle for a in campaign_accounts(current_user.id, camp.id, "aceptada", platform)]
    if not mine:
        if campaign_accounts(current_user.id, camp.id, "pendiente"):
            return False, "Tu cuenta aún está pendiente de verificar en esta campaña. Te escribiremos por el chat."
        return False, f"Para subir vídeos de {PLATFORMS[platform]} aquí, primero únete a la campaña con esa cuenta."
    author = viewcounter.tiktok_handle_from_url(url) if platform == "tiktok" else ""
    if author and author not in mine:
        if handle_owner(platform, author, exclude_user_id=current_user.id):
            return False, f"Ese vídeo es de @{author}, una cuenta vinculada a otro clipper."
        return False, f"Ese vídeo es de @{author}, que no es una de tus cuentas aceptadas en esta campaña."
    if Submission.query.filter_by(video_key=f"{platform}:{vid}").first():
        return False, "Ese vídeo ya se envió antes."
    db.session.add(Submission(user_id=current_user.id, campaign_id=camp.id, url=url, platform=platform,
                              video_key=f"{platform}:{vid}", author=author))
    db.session.commit()
    return True, "¡Clip enviado! Lo revisaremos y empezará a sumar vistas."


@app.route("/campana/<int:cid>", methods=["GET", "POST"])
@login_required
def campaign(cid):
    camp = db.session.get(Campaign, cid) or abort(404)
    if camp.status == "pausada" and not current_user.is_admin:
        abort(404)
    if request.method == "POST":
        ok, msg = submit_clip(camp, request.form.get("url", ""))
        flash(msg, "ok" if ok else "error")
        return redirect(url_for("campaign", cid=cid))
    st = campaign_stats(camp)
    mine = sorted([s for s in camp.submissions if s.user_id == current_user.id],
                  key=lambda s: s.created_at, reverse=True)
    accounts = campaign_accounts(current_user.id, camp.id)
    accepted = [a for a in accounts if a.status == "aceptada"]
    return render_template("campaign.html", camp=camp, st=st, mine=mine, accounts=accounts, accepted=accepted,
                           join_statuses=JOIN_STATUSES)


@app.route("/campana/<int:cid>/unirme", methods=["POST"])
@login_required
def join_campaign(cid):
    camp = db.session.get(Campaign, cid) or abort(404)
    back = redirect(url_for("campaign", cid=cid))
    if camp.status != "activa" or camp.exhausted:
        flash("Esta campaña ya no acepta clippers nuevos.", "error")
        return back
    platform = request.form.get("platform", "")
    handle = viewcounter.clean_handle(request.form.get("handle", ""))
    if platform not in camp.platform_list:
        flash("Elige una red de las que acepta la campaña.", "error")
        return back
    if not handle:
        flash("Escribe el usuario de la cuenta con la que vas a publicar.", "error")
        return back
    owner = handle_owner(platform, handle, exclude_user_id=current_user.id)
    if owner:
        flash(f"La cuenta @{handle} ya está vinculada a otro clipper. Si es tuya, escríbenos por el chat.", "error")
        return back
    existing = CampaignAccount.query.filter_by(campaign_id=cid, platform=platform, handle=handle).first()
    if existing:
        flash("Ya has añadido esa cuenta a esta campaña." if existing.user_id == current_user.id
              else f"La cuenta @{handle} ya está en esta campaña con otro clipper.", "error")
        return back
    if len(campaign_accounts(current_user.id, cid)) >= MAX_JOIN_ACCOUNTS:
        flash(f"Puedes añadir como máximo {MAX_JOIN_ACCOUNTS} cuentas por campaña.", "error")
        return back
    linked = current_user.handles(platform)
    if handle not in linked:
        if len(linked) >= MAX_HANDLES:
            flash(f"Ya tienes {MAX_HANDLES} cuentas de {PLATFORMS[platform]} vinculadas. Quita alguna en «Mi cuenta».", "error")
            return back
        setattr(current_user, f"{platform}_handles", ",".join(linked + [handle]))
    db.session.add(CampaignAccount(user_id=current_user.id, campaign_id=cid, platform=platform, handle=handle))
    db.session.add(Message(user_id=current_user.id, from_admin=False, sender_id=current_user.id,
                           body=f"He solicitado unirme a «{camp.song}» con @{handle} ({PLATFORMS[platform]}).",
                           read_by_admin=False, read_by_user=True))
    db.session.commit()
    notify_phone(f"🙋 {current_user.name} quiere unirse", f"«{camp.song}» con @{handle} ({PLATFORMS[platform]}). "
                 "Escríbele para verificar la cuenta.", url_for("admin_chat", uid=current_user.id), tags="raising_hand")
    flash("¡Solicitud enviada! Nuestro equipo te escribirá por el chat de soporte para verificar tu cuenta. "
          "Cuando te aceptemos, podrás subir vídeos en esta campaña.", "ok")
    return back


@app.route("/enviar", methods=["GET", "POST"])
@login_required
def submit():
    joined = {a.campaign_id for a in CampaignAccount.query.filter_by(user_id=current_user.id, status="aceptada")}
    camps = [c for c in active_campaigns() if c.id in joined]
    if request.method == "POST":
        camp = db.session.get(Campaign, request.form.get("campaign_id", type=int) or 0)
        if not camp:
            flash("Elige una campaña.", "error")
        else:
            ok, msg = submit_clip(camp, request.form.get("url", ""))
            flash(msg, "ok" if ok else "error")
        return redirect(url_for("submit", c=request.form.get("campaign_id", "")))
    subs = sorted(current_user.submissions, key=lambda s: s.created_at, reverse=True)
    return render_template("submit.html", camps=camps, subs=subs, selected=request.args.get("c", type=int))


@app.route("/ganancias")
@login_required
def earnings():
    tab = request.args.get("tab", "disponible")
    if tab not in ("disponible", "pendiente", "retiros"):
        tab = "disponible"
    whop_sync([p for p in current_user.payouts if p.status == "procesando"])
    m = user_money(current_user)
    payouts = sorted(current_user.payouts, key=lambda p: p.created_at, reverse=True)
    return render_template("earnings.html", m=m, tab=tab, payouts=payouts)


_withdraw_lock = threading.Lock()


@app.route("/ganancias/retirar", methods=["POST"])
@login_required
def withdraw():
    if not current_user.payout_ready:
        flash("Primero agrega tu cuenta de cobro.", "error")
        return redirect(url_for("account", _anchor="cobro"))
    with _withdraw_lock:
        m = user_money(current_user)
        if m["open_payout"] or Payout.query.filter(Payout.user_id == current_user.id,
                                                   Payout.status.in_(PAYOUT_OPEN)).first():
            flash("Ya tienes un retiro en curso. Podrás pedir otro cuando termine.", "error")
            return redirect(url_for("earnings", tab="retiros"))
        if m["available"] < MIN_WITHDRAW:
            flash(f"El retiro mínimo es {money(MIN_WITHDRAW)}.", "error")
            return redirect(url_for("earnings", tab="retiros"))
        amount = m["available"]
        is_whop = current_user.payout_method == WHOP_METHOD
        auto = is_whop and amount <= WHOP_AUTO_MAX
        payout = Payout(user_id=current_user.id, amount=amount, method=current_user.payout_method,
                        status="procesando" if auto else "solicitado",
                        details=(f"@{current_user.whop_username} ({current_user.whop_user_id})" if is_whop
                                 else current_user.payout_details))
        db.session.add(payout)
        db.session.commit()
    log.info("Retiro: user=%s amount=%s method=%s auto=%s", current_user.id, amount, payout.method, auto)
    if auto:
        result = whop_pay(payout)
        db.session.commit()
        if result == "pagado":
            notify_phone(f"✅ Pagado por Whop: {money(amount)}", f"{current_user.name} · @{current_user.whop_username}",
                         url_for("admin_clippers"), tags="white_check_mark")
            flash(f"¡Listo! Te hemos enviado {money(amount)} a tu cuenta de Whop (@{current_user.whop_username}). "
                  "Ya lo tienes en tu saldo de Whop.", "ok")
        elif result == "procesando":
            flash(f"Estamos enviando {money(amount)} a tu cuenta de Whop. Llegará en unos minutos.", "ok")
        else:
            flash("No hemos podido enviar el pago automático ahora mismo. Tu retiro queda solicitado "
                  "y lo revisamos enseguida; no tienes que hacer nada.", "info")
        return redirect(url_for("earnings", tab="retiros"))
    notify_phone(f"💸 Retiro de {money(amount)}", f"{current_user.name} quiere cobrar en {payout.method}.",
                 url_for("admin_clippers"), tags="money_with_wings")
    if is_whop:
        flash(f"Retiro de {money(amount)} solicitado. Por ser una cantidad alta lo revisamos antes de enviarlo a tu Whop.", "ok")
    else:
        flash(f"Retiro de {money(amount)} solicitado. Lo pagaremos en un plazo de {PAYOUT_DAYS} días hábiles.", "ok")
    return redirect(url_for("earnings", tab="retiros"))


@app.route("/cuenta", methods=["GET", "POST"])
@login_required
def account():
    if request.method == "POST":
        section = request.form.get("section")
        if section == "cobro_usar":
            method = request.form.get("payout_method", "")
            if method == WHOP_METHOD and current_user.whop_user_id and whop_enabled():
                current_user.payout_method = WHOP_METHOD
            elif method == USDT_METHOD and current_user.payout_details:
                current_user.payout_method = USDT_METHOD
            else:
                flash("Primero conecta esa forma de cobro.", "error")
                return redirect(url_for("account", _anchor="cobro"))
            db.session.commit()
            flash(f"Ahora cobrarás por {current_user.payout_label}.", "ok")
            return redirect(url_for("account", _anchor="cobro"))
        if section == "cobro" and request.form.get("payout_method") == WHOP_METHOD:
            username = (request.form.get("whop_username") or "").strip().lstrip("@")
            if "whop.com/" in username:
                username = username.rstrip("/").rsplit("/", 1)[-1].lstrip("@")
            if not whop_enabled():
                flash("El cobro por Whop aún no está activado. De momento puedes cobrar en USDT.", "info")
            elif not WHOP_USERNAME_RE.match(username):
                flash("Escribe tu nombre de usuario de Whop (sin espacios), por ejemplo @tunombre.", "error")
            else:
                uid, uname = whop_find_user(username)
                if not uid:
                    flash(f"No encontramos el usuario @{username} en Whop. Revisa que esté bien escrito: "
                          "lo ves en tu perfil de whop.com.", "error")
                else:
                    current_user.whop_user_id, current_user.whop_username = uid, uname[:80]
                    current_user.payout_method = WHOP_METHOD
                    db.session.commit()
                    flash(f"Whop conectado: @{uname}. Cuando retires, el dinero llegará al momento a tu saldo de Whop.", "ok")
            return redirect(url_for("account", _anchor="cobro"))
        if section == "cobro":
            method = request.form.get("payout_method", "")
            details = request.form.get("payout_details", "").strip()[:255]
            if method not in PAYOUT_AVAILABLE:
                flash(PAYOUT_ONLY_USDT_MSG, "info")
                return redirect(url_for("account", _anchor="cobro"))
            if not TRC20_RE.match(details):
                flash("Esa dirección no parece de la red TRC20. Debe empezar por «T» y tener 34 caracteres. "
                      "Cópiala tal cual desde tu wallet o exchange, eligiendo la red TRC20.", "error")
                return redirect(url_for("account", _anchor="cobro"))
            current_user.payout_method = USDT_METHOD
            current_user.payout_details = details
            db.session.commit()
            flash("Wallet guardada. Te enviaremos los retiros en USDT por la red TRC20.", "ok")
        elif section == "redes":
            for p in PLATFORMS:
                raw = request.form.get(f"handles_{p}", "")
                handles = []
                for h in raw.replace("\n", ",").replace(" ", ",").split(","):
                    h = viewcounter.clean_handle(h)
                    if h and h not in handles:
                        handles.append(h)
                if len(handles) > MAX_HANDLES:
                    flash(f"Puedes vincular como máximo {MAX_HANDLES} cuentas por red.", "error")
                    return redirect(url_for("account"))
                for h in handles:
                    owner = handle_owner(p, h, exclude_user_id=current_user.id)
                    if owner:
                        flash(f"La cuenta @{h} de {PLATFORMS[p]} ya está vinculada a otro clipper. "
                              f"Si es tuya, escríbenos{(' a ' + SUPPORT_EMAIL) if SUPPORT_EMAIL else ''}.", "error")
                        return redirect(url_for("account"))
                setattr(current_user, f"{p}_handles", ",".join(handles))
            db.session.commit()
            flash("Cuentas guardadas.", "ok")
        return redirect(request.args.get("next") and safe_next(request.args.get("next")) or url_for("account"))
    return render_template("account.html")


# ---------------------------------------------------------------- panel de admin
@app.route("/admin")
@admin_required
def admin_home():
    camps = Campaign.query.order_by(Campaign.created_at.desc()).all()
    cards = [(c, campaign_stats(c)) for c in camps]
    pending = Submission.query.filter_by(status="pendiente").order_by(Submission.created_at).all()
    open_payouts = Payout.query.filter(Payout.status.in_(PAYOUT_OPEN)).all()
    kpis = {
        "budget_live": cents(sum(c.budget for c in camps if c.status == "activa")),
        "spent": cents(sum(c.spent or 0 for c in camps)),
        "paid": cents(db.session.query(db.func.coalesce(db.func.sum(Payout.amount), 0)).filter(Payout.status == "pagado").scalar()),
        "open_payouts": cents(sum(p.amount for p in open_payouts)),
        "open_payouts_n": len(open_payouts),
        "clippers": db.session.query(db.func.count(db.distinct(Submission.user_id))).scalar() or 0,
        "views": db.session.query(db.func.coalesce(db.func.sum(Submission.views), 0)).filter(Submission.status == "aprobado").scalar() or 0,
        "pending": len(pending),
        "flagged": sum(1 for s in pending if s.flag_list),
    }
    joins = CampaignAccount.query.filter_by(status="pendiente").order_by(CampaignAccount.created_at).all()
    return render_template("admin/home.html", cards=cards, pending=pending, kpis=kpis, joins=joins,
                           last_refresh=get_setting("last_refresh", "Nunca"),
                           has_token=bool(viewcounter.APIFY_TOKEN))


def process_cover(file):
    """Valida la imagen y la vuelve a guardar limpia (sin datos ocultos) y a un tamaño razonable."""
    from PIL import Image, UnidentifiedImageError
    try:
        img = Image.open(file.stream)
        img.verify()
        file.stream.seek(0)
        img = Image.open(file.stream)
        img = img.convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError):
        return None
    img.thumbnail((1600, 1600))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85, optimize=True, progressive=True)
    return out.getvalue()


def _campaign_from_form(camp):
    camp.song = request.form.get("song", "").strip()[:200]
    camp.artist = request.form.get("artist", "").strip()[:200]
    cover_url = request.form.get("cover_url", "").strip()[:500]
    camp.cover_url = cover_url if cover_url.startswith("https://") else ""
    sound_url = request.form.get("sound_url", "").strip()[:500]
    camp.sound_url = sound_url if sound_url.startswith(("https://", "http://")) else ""
    camp.rules = request.form.get("rules", "").strip()[:5000]
    if request.form.get("rate_k") is not None:          # el formulario pide el pago por cada 1.000 vistas
        camp.rate = round((parse_float(request.form.get("rate_k"), 0) or 0) * 1000, 4)
    else:
        camp.rate = parse_float(request.form.get("rate"), 0) or 0
    camp.budget = parse_float(request.form.get("budget"), 0) or 0
    if request.form.get("min_pay") is not None:         # y el pago mínimo por clip en dinero
        mp = parse_float(request.form.get("min_pay"), 0) or 0
        camp.min_views = int(math.ceil(mp * 1_000_000 / camp.rate)) if mp > 0 and camp.rate > 0 else 0
    else:
        camp.min_views = max(int(parse_float(request.form.get("min_views"), 0) or 0), 0)
    max_pv = parse_float(request.form.get("max_per_video"))
    camp.max_per_video = max_pv if max_pv and max_pv > 0 else None
    camp.platforms = ",".join(p for p in PLATFORMS if request.form.get(f"pf_{p}")) or "tiktok"
    status = request.form.get("status", "activa")
    camp.status = status if status in ("activa", "pausada", "finalizada") else "activa"
    camp.featured = bool(request.form.get("featured"))
    cat = request.form.get("category", "musica")
    camp.category = cat if cat in CATEGORIES else "otro"
    file = request.files.get("cover_file")
    if file and file.filename:
        data = process_cover(file)
        if not data:
            return "La portada tiene que ser una imagen válida (JPG, PNG o WEBP)."
        camp.cover_data, camp.cover_mime = data, "image/jpeg"
        camp.cover_version = (camp.cover_version or 0) + 1
    if request.form.get("remove_cover"):
        camp.cover_data, camp.cover_version = None, 0
    if not (camp.song and camp.artist and camp.rate > 0 and camp.budget > 0):
        return "Faltan datos: título, creador, pago por 1M de vistas y presupuesto."
    if camp.budget < (camp.spent or 0):
        return f"El presupuesto no puede ser menor que lo ya repartido ({money(camp.spent)})."
    return None


@app.route("/admin/campana/nueva", methods=["GET", "POST"])
@app.route("/admin/campana/<int:cid>/editar", methods=["GET", "POST"])
@admin_required
def admin_campaign_form(cid=None):
    camp = db.session.get(Campaign, cid) if cid else Campaign(platforms="tiktok,instagram,youtube", status="activa",
                                                              category="musica", spent=0.0)
    lead = db.session.get(CampaignLead, request.args.get("solicitud", type=int) or 0) if not cid else None
    if lead and request.method == "GET":       # crear campaña a partir de una solicitud
        camp.song, camp.artist, camp.category = "", lead.brand, lead.category
        camp.sound_url = lead.link if lead.link.startswith(("https://", "http://")) else ""
    if cid and not camp:
        abort(404)
    if request.method == "POST":
        error = _campaign_from_form(camp)
        if not error:
            if camp.status == "activa" and camp.exhausted and cid:
                return_msg = "Guardada, pero sigue sin presupuesto libre: súbelo para reactivarla."
                camp.status = "finalizada"
            else:
                return_msg = "Campaña guardada."
                if camp.status != "finalizada":
                    camp.ended_at = None
            if not camp.report_token:
                camp.report_token = secrets.token_urlsafe(18)
            if not cid:
                db.session.add(camp)
            db.session.flush()
            if camp.featured:
                Campaign.query.filter(Campaign.id != camp.id).update({"featured": False})
            from_lead = db.session.get(CampaignLead, request.args.get("solicitud", type=int) or 0) if not cid else None
            if from_lead:
                from_lead.status = "convertida"
            audit("campaña_creada" if not cid else "campaña_editada",
                  f"#{camp.id} {camp.song} · {money(camp.rate, 0)}/1M · presupuesto {money(camp.budget, 0)} · {camp.status}")
            db.session.commit()
            flash(return_msg, "ok")
            return redirect(url_for("admin_campaign", cid=camp.id))
        db.session.rollback()
        flash(error, "error")
        if cid:
            camp = db.session.get(Campaign, cid)
    return render_template("admin/campaign_form.html", camp=camp, editing=bool(cid))


@app.route("/admin/campana/<int:cid>")
@admin_required
def admin_campaign(cid):
    camp = db.session.get(Campaign, cid) or abort(404)
    if not camp.report_token:
        camp.report_token = secrets.token_urlsafe(18)
        db.session.commit()
    st = campaign_stats(camp)
    filt = request.args.get("estado", "")
    subs = sorted(camp.submissions, key=lambda s: (s.status != "pendiente", -len(s.flag_list), -(s.views or 0)))
    if filt == "avisos":
        subs = [s for s in subs if s.flag_list]
    elif filt:
        subs = [s for s in subs if s.status == filt]
    report_url = (BASE_URL or request.host_url.rstrip("/")) + url_for("report", token=camp.report_token)
    return render_template("admin/campaign.html", camp=camp, st=st, subs=subs, filt=filt, report_url=report_url)


@app.route("/admin/campana/<int:cid>/clips.csv")
@admin_required
def admin_campaign_csv(cid):
    camp = db.session.get(Campaign, cid) or abort(404)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["fecha", "clipper", "email", "red", "cuenta", "enlace", "vistas", "likes", "estado", "ganado", "avisos"])
    for s in sorted(camp.submissions, key=lambda s: s.created_at):
        w.writerow([csv_safe(x) for x in [s.created_at.strftime("%Y-%m-%d %H:%M"), s.user.name, s.user.email, PLATFORMS[s.platform],
                    s.author, s.url, s.views or 0, s.likes if s.likes is not None else "", s.status,
                    f"{cents(s.earned):.2f}", " / ".join(FLAG_LABELS.get(f, f) for f in s.flag_list)]])
    return Response("﻿" + out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=campana-{camp.id}-clips.csv"})


@app.route("/admin/envio/<int:sid>/<accion>", methods=["POST"])
@admin_required
def admin_review(sid, accion):
    sub = db.session.get(Submission, sid) or abort(404)
    camp = sub.campaign
    if accion == "aprobar":
        if not account_verified(sub):
            who = f"@{sub.author}" if sub.author else f"de {PLATFORMS[sub.platform]}"
            flash(f"No puedes aprobar este clip: la cuenta {who} de {sub.user.name} aún no está verificada. "
                  "Verifícala desde su chat.", "error")
            return redirect(request.referrer or url_for("admin_home"))
        sub.status, sub.reject_reason = "aprobado", ""
        added = accrue(sub)
        audit("clip_aprobado", f"#{sub.id} de {sub.user.email} en «{camp.song}» · {money(added)} asignados")
        if camp.exhausted and not sub.earned:
            flash("Aprobado, pero la campaña ya no tiene presupuesto: este clip no suma dinero.", "error")
    elif accion == "rechazar":
        was = sub.status
        removed = clawback(sub) if was == "aprobado" else 0
        sub.status = "rechazado"
        sub.reject_reason = request.form.get("reason", "").strip()[:255] or "No cumple las reglas de la campaña"
        audit("clip_rechazado", f"#{sub.id} de {sub.user.email} · motivo: {sub.reject_reason}"
                                + (f" · {money(removed)} retirados" if removed else ""))
        if removed:
            flash(f"Clip rechazado. Se han retirado {money(removed)} de sus ganancias.", "ok")
    elif accion == "visitas":
        new_views = max(int(parse_float(request.form.get("views"), sub.views) or 0), 0)
        old = sub.views or 0
        sub.views, sub.views_updated_at = new_views, datetime.utcnow()
        db.session.add(ViewSnapshot(submission_id=sub.id, views=new_views))
        delta = accrue(sub, allow_decrease=True)
        audit("vistas_corregidas", f"#{sub.id}: {old} → {new_views}" + (f" · {money(delta)}" if delta else ""))
    elif accion == "quitar_avisos":
        sub.flags = ""
        audit("avisos_quitados", f"#{sub.id}")
    else:
        abort(400)
    db.session.commit()
    return redirect(request.referrer or url_for("admin_home"))


@app.route("/admin/actualizar-visitas", methods=["POST"])
@admin_required
def admin_refresh():
    refresh_in_background()
    flash("Actualizando vistas en segundo plano. Recarga en unos minutos.", "ok")
    return redirect(url_for("admin_home"))


@app.route("/admin/probar-apify", methods=["GET", "POST"])
@admin_required
def admin_probe():
    result = None
    if request.method == "POST":
        result = viewcounter.probe(request.form.get("url", ""))
    return render_template("admin/probe.html", result=result, has_token=bool(viewcounter.APIFY_TOKEN))


def whop_call(method, path, **kw):
    """Llama a la API de Whop y devuelve (ok, código, datos)."""
    headers = {"Authorization": f"Bearer {WHOP_API_KEY}", "Content-Type": "application/json"}
    headers.update(kw.pop("headers", {}))
    try:
        r = requests.request(method, f"{WHOP_API}{path}", headers=headers, timeout=30, **kw)
    except requests.RequestException as e:
        return False, 0, {"error": f"No se pudo conectar con Whop: {e}"}
    try:
        data = r.json()
    except ValueError:
        data = {"respuesta": r.text[:2000]}
    return r.ok, r.status_code, data


def whop_enabled():
    return bool(WHOP_API_KEY and WHOP_COMPANY_ID)


def whop_find_user(username):
    """Busca un usuario de Whop por su nombre de usuario. Devuelve (id, username) o (None, None)."""
    ok, code, data = whop_call("GET", f"/users/{username}")
    if ok and isinstance(data, dict) and str(data.get("id", "")).startswith("user_"):
        return data["id"], (data.get("username") or username)
    log.warning("Whop: usuario %s no encontrado (%s) %s", username, code, str(data)[:300])
    return None, None


def _whop_apply(payout, ok, code, data):
    """Actualiza el retiro según la respuesta de Whop. Devuelve 'pagado', 'procesando' o 'fallido'."""
    data = data if isinstance(data, dict) else {}
    status = str(data.get("status") or "").lower()
    if data.get("id"):
        payout.ext_id = str(data["id"])[:80]
    if ok and status not in ("failed", "processing", "pending", "created"):
        payout.status, payout.paid_at = "pagado", datetime.utcnow()
        payout.note = f"Enviado a tu Whop · {payout.ext_id}"[:255]
        return "pagado"
    if ok and status != "failed":
        payout.status, payout.note = "procesando", "Enviando a tu cuenta de Whop"
        return "procesando"
    err = data.get("error")
    reason = data.get("failure_reason") or data.get("message") or (err.get("message") if isinstance(err, dict) else err)
    payout.status, payout.note = "solicitado", "Pago automático pendiente de revisar"
    log.error("Whop: fallo al pagar el retiro #%s (%s) %s", payout.id, code, str(data)[:500])
    notify_phone(f"⚠️ Falló un pago por Whop ({money(payout.amount)})",
                 f"{payout.user.name}: {str(reason or code)[:150]}", url_for("admin_clippers"), tags="warning")
    return "fallido"


def whop_pay(payout):
    """Manda el retiro al saldo de Whop del clipper. Es seguro repetirlo: la clave evita pagar dos veces."""
    key = f"{APP_NAME.lower().replace(' ', '')}-payout-{payout.id}"
    body = {"amount": round(payout.amount, 2), "currency": "usd", "origin_id": WHOP_COMPANY_ID,
            "destination_id": payout.user.whop_user_id, "notes": f"Retiro #{payout.id} · {APP_NAME}"[:50],
            "idempotence_key": key, "metadata": {"payout_id": str(payout.id)}}
    ok, code, data = whop_call("POST", "/transfers", json=body, headers={"Idempotency-Key": key})
    if not ok and code == 400 and "type" in str(data).lower():
        ok, code, data = whop_call("POST", "/transfers", json={**body, "type": "balance"}, headers={"Idempotency-Key": key})
    result = _whop_apply(payout, ok, code, data)
    log.info("Whop: retiro #%s → %s (%s)", payout.id, result, code)
    return result


def whop_sync(payouts):
    """Consulta en Whop los retiros que seguían «procesando»."""
    changed = False
    for p in payouts:
        if p.status != "procesando" or not p.ext_id or not whop_enabled():
            continue
        ok, code, data = whop_call("GET", f"/transfers/{p.ext_id}")
        if ok:
            _whop_apply(p, ok, code, data)
            changed = True
    if changed:
        db.session.commit()


@app.route("/admin/clippers", methods=["GET", "POST"])
@admin_required
def admin_clippers():
    if request.method == "POST":
        payout = db.session.get(Payout, request.form.get("payout_id", type=int) or 0) or abort(404)
        if payout.status != "solicitado":
            flash("Ese retiro ya estaba gestionado.", "error")
            return redirect(url_for("admin_clippers"))
        note = request.form.get("note", "").strip()[:255]
        if request.form.get("action") == "whop":
            if payout.method != WHOP_METHOD or not payout.user.whop_user_id or not whop_enabled():
                flash("Este retiro no se puede pagar por Whop.", "error")
                return redirect(url_for("admin_clippers"))
            payout.status = "procesando"
            db.session.commit()
            result = whop_pay(payout)
            audit("retiro_whop", f"#{payout.id} {money(payout.amount)} a {payout.user.email} · {result} · {payout.ext_id}")
            db.session.commit()
            flash({"pagado": f"Enviado {money(payout.amount)} a @{payout.user.whop_username} por Whop.",
                   "procesando": "Whop está procesando el pago. Se actualizará solo."}.get(
                result, "Whop no ha aceptado el pago. Mira el aviso en tu móvil o los registros."),
                "ok" if result != "fallido" else "error")
            return redirect(url_for("admin_clippers"))
        if request.form.get("action") == "pagar":
            payout.status, payout.paid_at, payout.note = "pagado", datetime.utcnow(), note
            audit("retiro_pagado", f"#{payout.id} {money(payout.amount)} a {payout.user.email} · {payout.method} · {note}")
            flash(f"Retiro de {money(payout.amount)} marcado como pagado.", "ok")
        elif request.form.get("action") == "rechazar":
            payout.status, payout.note = "rechazado", note or "Rechazado"
            audit("retiro_rechazado", f"#{payout.id} {money(payout.amount)} de {payout.user.email} · {payout.note}")
            flash("Retiro rechazado. El dinero vuelve a su saldo disponible.", "ok")
        db.session.commit()
        return redirect(url_for("admin_clippers"))
    whop_sync(Payout.query.filter_by(status="procesando").all())
    open_requests = Payout.query.filter(Payout.status.in_(PAYOUT_OPEN)).order_by(Payout.created_at).all()
    q = request.args.get("q", "").strip().lower()
    users = User.query.order_by(User.created_at.desc()).all()
    if q:
        users = [u for u in users if q in u.email.lower() or q in u.name.lower()
                 or any(q in h for p in PLATFORMS for h in u.handles(p))]
    rows = [(u, user_money(u), len(u.submissions)) for u in users]
    rows.sort(key=lambda r: -r[1]["earned"])
    return render_template("admin/clippers.html", rows=rows, open_requests=open_requests, q=q)


@app.route("/admin/clipper/<int:uid>/<accion>", methods=["POST"])
@admin_required
def admin_ban(uid, accion):
    user = db.session.get(User, uid) or abort(404)
    if user.is_admin:
        flash("No puedes bloquear a un administrador.", "error")
    elif accion == "bloquear":
        user.is_banned, user.ban_reason = True, request.form.get("reason", "").strip()[:255] or "Incumplimiento de los términos"
        audit("clipper_bloqueado", f"{user.email} · {user.ban_reason}")
        flash(f"{user.name} ha sido bloqueado.", "ok")
    elif accion == "desbloquear":
        user.is_banned, user.ban_reason = False, ""
        audit("clipper_desbloqueado", user.email)
        flash(f"{user.name} vuelve a tener acceso.", "ok")
    db.session.commit()
    return redirect(request.referrer or url_for("admin_clippers"))


@app.route("/admin/retiros.csv")
@admin_required
def admin_payouts_csv():
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["id", "fecha", "clipper", "email", "metodo", "datos", "importe", "estado", "pagado", "nota"])
    for p in Payout.query.order_by(Payout.created_at.desc()).all():
        w.writerow([csv_safe(x) for x in [p.id, p.created_at.strftime("%Y-%m-%d %H:%M"), p.user.name, p.user.email, p.method,
                    p.details, f"{p.amount:.2f}", p.status, p.paid_at.strftime("%Y-%m-%d %H:%M") if p.paid_at else "", p.note]])
    return Response("﻿" + out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=retiros.csv"})


@app.route("/admin/actividad")
@admin_required
def admin_activity():
    logs = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(300).all()
    return render_template("admin/activity.html", logs=logs)


# ---------------------------------------------------------------- chat de soporte
MAX_MESSAGE_LEN = 2000
MAX_MESSAGES_HOUR = int(os.environ.get("MAX_MESSAGES_HOUR", "30"))


def unread_messages():
    """Mensajes sin leer para el usuario actual (para el aviso del menú)."""
    if not current_user.is_authenticated:
        return 0
    if current_user.is_admin:
        return Message.query.filter_by(from_admin=False, read_by_admin=False).count()
    return Message.query.filter_by(user_id=current_user.id, from_admin=True, read_by_user=False).count()


def notify_phone(title, message, path="", tags="speech_balloon"):
    """Manda un aviso al móvil del admin (app ntfy). Va en segundo plano: nunca frena ni rompe la web."""
    if not NTFY_TOPIC:
        return
    base = BASE_URL or (request.host_url.rstrip("/") if request else "")
    payload = {"topic": NTFY_TOPIC, "title": title[:120], "message": message[:400], "tags": [tags]}
    if path and base:
        payload["click"] = base + path

    def send():
        try:
            requests.post(NTFY_SERVER, json=payload, timeout=8)
        except Exception as e:                              # sin conexión o ntfy caído: solo se anota
            log.warning("No se pudo enviar el aviso al móvil: %s", e)
    threading.Thread(target=send, daemon=True).start()


def notice_data():
    """Aviso de mensajes nuevos para el usuario actual: cuántos hay, de quién y el último texto."""
    n = unread_messages()
    if not n:
        return {"unread": 0}
    if current_user.is_admin:
        last = (Message.query.filter_by(from_admin=False, read_by_admin=False).order_by(Message.id.desc()).first())
        who, url = last.user.name, url_for("admin_chat", uid=last.user_id)
        title = f"{n} mensaje{'s' if n > 1 else ''} nuevo{'s' if n > 1 else ''}" if n > 1 else f"Mensaje de {who}"
    else:
        last = (Message.query.filter_by(user_id=current_user.id, from_admin=True, read_by_user=False)
                .order_by(Message.id.desc()).first())
        who, url = f"Equipo {APP_NAME}", url_for("support")
        title = f"Equipo {APP_NAME} te ha escrito" if n == 1 else f"Tienes {n} mensajes del equipo {APP_NAME}"
    body = last.body if len(last.body) <= 140 else last.body[:137].rstrip() + "…"
    return {"unread": n, "id": last.id, "title": title, "who": who, "body": body, "url": url,
            "ago": ago(last.created_at)}


@app.route("/avisos")
@login_required
def notices():
    return notice_data()


def message_json(m, viewer_is_admin):
    mine = m.from_admin == viewer_is_admin
    who = f"Equipo {APP_NAME}" if m.from_admin else m.user.name
    return {"id": m.id, "body": m.body, "mine": mine, "who": who if not mine else "Tú",
            "from_admin": m.from_admin, "ago": ago(m.created_at), "at": m.created_at.strftime("%d/%m/%Y %H:%M")}


def wants_json():
    return request.headers.get("X-Requested-With") == "fetch"


def post_message(owner_id, from_admin):
    body = (request.form.get("body") or "").strip()
    if not body:
        return None, "Escribe un mensaje."
    if len(body) > MAX_MESSAGE_LEN:
        return None, f"El mensaje es demasiado largo (máximo {MAX_MESSAGE_LEN} caracteres)."
    if not from_admin:
        hour_ago = datetime.utcnow() - timedelta(hours=1)
        sent = Message.query.filter(Message.user_id == owner_id, Message.from_admin.is_(False),
                                    Message.created_at >= hour_ago).count()
        if sent >= MAX_MESSAGES_HOUR:
            return None, "Has enviado muchos mensajes seguidos. Espera un poco y vuelve a escribir."
    m = Message(user_id=owner_id, from_admin=from_admin, sender_id=current_user.id, body=body,
                read_by_admin=from_admin, read_by_user=not from_admin)
    db.session.add(m)
    db.session.commit()
    if not from_admin:
        notify_phone(f"💬 {current_user.name}", body, url_for("admin_chat", uid=owner_id))
    return m, None


def conversation(owner_id, after=0):
    q = Message.query.filter(Message.user_id == owner_id)
    if after:
        q = q.filter(Message.id > after)
    return q.order_by(Message.id).all()


def mark_read(owner_id, by_admin):
    if by_admin:
        Message.query.filter_by(user_id=owner_id, from_admin=False, read_by_admin=False).update({"read_by_admin": True})
    else:
        Message.query.filter_by(user_id=owner_id, from_admin=True, read_by_user=False).update({"read_by_user": True})
    db.session.commit()


@app.route("/soporte", methods=["GET", "POST"])
@login_required
def support():
    if request.method == "POST":
        m, error = post_message(current_user.id, from_admin=False)
        if wants_json():
            return ({"ok": True, "message": message_json(m, False)} if m else ({"ok": False, "error": error}, 400))
        if error:
            flash(error, "error")
        return redirect(url_for("support"))
    msgs = conversation(current_user.id)
    mark_read(current_user.id, by_admin=False)
    prefill = ""
    return render_template("support.html", msgs=msgs, prefill=prefill)


@app.route("/soporte/mensajes")
@login_required
def support_poll():
    msgs = conversation(current_user.id, request.args.get("after", type=int) or 0)
    mark_read(current_user.id, by_admin=False)
    return {"messages": [message_json(m, False) for m in msgs]}


@app.route("/admin/mensajes")
@admin_required
def admin_inbox():
    last_ids = (db.session.query(Message.user_id, db.func.max(Message.id))
                .group_by(Message.user_id).all())
    unread = dict(db.session.query(Message.user_id, db.func.count(Message.id))
                  .filter(Message.from_admin.is_(False), Message.read_by_admin.is_(False))
                  .group_by(Message.user_id).all())
    threads = []
    for uid, mid in last_ids:
        last = db.session.get(Message, mid)
        threads.append({"user": last.user, "last": last, "unread": unread.get(uid, 0)})
    threads.sort(key=lambda t: (t["unread"] == 0, -t["last"].id))
    return render_template("admin/inbox.html", threads=threads)


@app.route("/admin/mensajes/<int:uid>", methods=["GET", "POST"])
@admin_required
def admin_chat(uid):
    user = db.session.get(User, uid) or abort(404)
    if request.method == "POST":
        m, error = post_message(user.id, from_admin=True)
        if m:
            mark_read(user.id, by_admin=True)       # si respondes, lo anterior queda leído
        if wants_json():
            return ({"ok": True, "message": message_json(m, True)} if m else ({"ok": False, "error": error}, 400))
        if error:
            flash(error, "error")
        return redirect(url_for("admin_chat", uid=uid))
    msgs = conversation(user.id)
    mark_read(user.id, by_admin=True)
    accounts = CampaignAccount.query.filter_by(user_id=user.id).order_by(CampaignAccount.created_at.desc()).all()
    return render_template("admin/chat.html", user=user, msgs=msgs, m=user_money(user), accounts=accounts,
                           join_statuses=JOIN_STATUSES)


@app.route("/admin/mensajes/<int:uid>/nuevos")
@admin_required
def admin_chat_poll(uid):
    db.session.get(User, uid) or abort(404)
    msgs = conversation(uid, request.args.get("after", type=int) or 0)
    mark_read(uid, by_admin=True)
    return {"messages": [message_json(m, True) for m in msgs]}


@app.route("/admin/cuenta-campana/<int:aid>/<accion>", methods=["POST"])
@admin_required
def admin_campaign_account(aid, accion):
    acc = db.session.get(CampaignAccount, aid) or abort(404)
    if accion not in ("aceptar", "rechazar"):
        abort(400)
    label = f"@{acc.handle} ({PLATFORMS.get(acc.platform, acc.platform)})"
    acc.status = "aceptada" if accion == "aceptar" else "rechazada"
    acc.decided_at = datetime.utcnow()
    if accion == "aceptar":
        note = f"¡Te hemos aceptado en «{acc.campaign.song}» con {label}! Ya puedes subir tus vídeos desde la página de la campaña."
    else:
        note = (f"No hemos podido aceptar {label} en «{acc.campaign.song}». "
                "Si crees que es un error, respóndenos por aquí.")
    db.session.add(Message(user_id=acc.user_id, from_admin=True, sender_id=current_user.id, body=note,
                           read_by_admin=True, read_by_user=False))
    audit("clipper_" + ("aceptado" if accion == "aceptar" else "rechazado"),
          f"{acc.user.email} · {label} · «{acc.campaign.song}»")
    db.session.commit()
    flash(f"{acc.user.name} {'aceptado' if accion == 'aceptar' else 'rechazado'} en «{acc.campaign.song}».", "ok")
    return redirect(request.referrer or url_for("admin_home"))


# ---------------------------------------------------------------- portada y solicitudes de campaña
BUDGETS = ["Menos de $500", "$500 – $2.000", "$2.000 – $10.000", "Más de $10.000", "Aún no lo sé"]
LEAD_STATUSES = {"nueva": "Nueva", "contactada": "Contactada", "convertida": "Campaña creada", "descartada": "Descartada"}


def landing_context():
    camps = active_campaigns()
    active = User.query.filter(User.last_seen_at >= datetime.utcnow() - timedelta(days=7)).count()
    if active >= 50:
        live_text = f"En vivo · {active:,} clippers activos".replace(",", ".")
    elif camps:
        live_text = f"En vivo · {len(camps)} {'campaña activa' if len(camps) == 1 else 'campañas activas'}"
    else:
        live_text = "Nuevas campañas cada semana"
    stats = {   # solo datos reales; la plantilla oculta los que estén a cero
        "views": db.session.query(db.func.coalesce(db.func.sum(Submission.views), 0)).filter(Submission.status == "aprobado").scalar() or 0,
        "paid": cents(db.session.query(db.func.coalesce(db.func.sum(Payout.amount), 0)).filter(Payout.status == "pagado").scalar()),
        "campaigns": len(camps),
    }
    return {"live_text": live_text, "showcase": [(c, campaign_stats(c)) for c in camps[:6]], "stats": stats}


def landing():
    return render_template("landing.html", **landing_context())


@app.route("/empezar")
def start():
    rol = request.args.get("rol")
    if rol == "campana":
        return redirect(url_for("launch"))
    if rol == "clipper":
        return redirect(url_for("index") if current_user.is_authenticated else url_for("login"))
    return redirect(url_for("index"))


@app.route("/lanzar-campana", methods=["GET", "POST"])
def launch():
    form = {}
    if request.method == "POST":
        form = {k: (request.form.get(k) or "").strip() for k in ("name", "email", "brand", "category", "budget", "link", "message")}
        if request.form.get("website"):                       # campo trampa: solo lo rellenan los bots
            return redirect(url_for("launch_thanks"))
        ip = (request.remote_addr or "")[:64]          # ProxyFix ya la toma del proxy de Railway
        recent = CampaignLead.query.filter(CampaignLead.ip == ip,
                                           CampaignLead.created_at >= datetime.utcnow() - timedelta(hours=1)).count()
        error = None
        if not form["name"] or not form["brand"]:
            error = "Escribe tu nombre y el del artista o marca."
        elif "@" not in form["email"] or "." not in form["email"].split("@")[-1]:
            error = "Escribe un correo válido para poder responderte."
        elif recent >= 5:
            error = "Has enviado varias solicitudes seguidas. Inténtalo más tarde."
        if error:
            flash(error, "error")
        else:
            db.session.add(CampaignLead(
                name=form["name"][:120], email=form["email"][:255].lower(), brand=form["brand"][:200],
                category=form["category"] if form["category"] in CATEGORIES else "otro",
                budget=form["budget"] if form["budget"] in BUDGETS else "", link=form["link"][:500],
                message=form["message"][:3000], ip=ip))
            db.session.commit()
            log.info("Nueva solicitud de campaña: %s", form["brand"])
            notify_phone(f"🎵 Nueva campaña: {form['brand']}", f"{form['name']} · {form['email']} · {form['budget'] or 'sin presupuesto'}",
                         url_for("admin_leads"), tags="moneybag")
            return redirect(url_for("launch_thanks"))
    return render_template("launch.html", form=form, budgets=BUDGETS)


@app.route("/lanzar-campana/gracias")
def launch_thanks():
    return render_template("launch_thanks.html")


@app.route("/admin/solicitudes", methods=["GET", "POST"])
@admin_required
def admin_leads():
    if request.method == "POST":
        lead = db.session.get(CampaignLead, request.form.get("lead_id", type=int) or 0) or abort(404)
        status = request.form.get("status", "")
        if status in LEAD_STATUSES:
            lead.status = status
            audit("solicitud_actualizada", f"#{lead.id} {lead.brand} → {LEAD_STATUSES[status]}")
            db.session.commit()
        return redirect(url_for("admin_leads"))
    leads = CampaignLead.query.order_by(CampaignLead.created_at.desc()).all()
    leads.sort(key=lambda l: l.status != "nueva")
    return render_template("admin/leads.html", leads=leads, statuses=LEAD_STATUSES)


def new_leads():
    if not current_user.is_authenticated or not current_user.is_admin:
        return 0
    return CampaignLead.query.filter_by(status="nueva").count()


# ---------------------------------------------------------------- tarea programada
@app.route("/cron/actualizar")
def cron_refresh():
    if not CRON_SECRET or not secrets.compare_digest(request.args.get("key", ""), CRON_SECRET):
        abort(403)
    whop_sync(Payout.query.filter_by(status="procesando").all())
    refresh_in_background()
    return "ok"


if __name__ == "__main__":
    app.run(debug=not PRODUCTION, port=int(os.environ.get("PORT", 5000)))
