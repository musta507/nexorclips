"""ClipProfit Hispano — plataforma de clipping: campañas, clips, vistas y pagos."""
import base64
import csv
import hashlib
import io
import json
import logging
import math
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from functools import wraps
from urllib.parse import quote, urlencode, urlparse

import requests
from flask import (Flask, Response, abort, flash, has_request_context, redirect, render_template,
                   g, request, session, url_for)
from flask_login import (LoginManager, UserMixin, current_user, login_required,
                         login_user, logout_user)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect as sa_inspect, text
from sqlalchemy.orm import joinedload, selectinload
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

import push as webpush
import views as viewcounter

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("clipprofit")

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
                       "DATABASE_URL=${{Postgres.DATABASE_URL}}. Variables de base de datos que veo (solo el nombre, "
                       "nunca el valor): " + (", ".join(f"{k}{' (vacía)' if not os.environ.get(k) else ''}" for k in seen) or "ninguna"))
for prefix in ("postgres://", "postgresql://"):   # Railway da "postgresql://"; usamos el conector psycopg2
    if db_url.startswith(prefix):
        db_url = "postgresql+psycopg2://" + db_url[len(prefix):]
app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True, "pool_recycle": 280}

APP_NAME = os.environ.get("APP_NAME", "ClipProfit Hispano")
SUPPORT_EMAIL = os.environ.get("SUPPORT_EMAIL", "")
CURRENCY = os.environ.get("CURRENCY", "$")
CURRENCY_CODE = os.environ.get("CURRENCY_CODE", "USD")
REFRESH_DAYS = int(os.environ.get("REFRESH_DAYS", "0"))        # 0 = se cuentan hasta que acaba la campaña
MIN_WITHDRAW = float(os.environ.get("MIN_WITHDRAW", "1"))           # Whop no envía pagos de menos de 1 $
MAX_CLIPS_DAY = int(os.environ.get("MAX_CLIPS_DAY", "0"))      # 0 = sin límite de clips al día
PAYOUT_DAYS = int(os.environ.get("PAYOUT_DAYS", "7"))          # plazo de pago que se promete al clipper
ADMIN_EMAILS = {e.strip().lower() for e in os.environ.get("ADMIN_EMAIL", "").split(",") if e.strip()}          # dueños: todo
REVIEWER_EMAILS = {e.strip().lower() for e in os.environ.get("REVIEWER_EMAILS", "").split(",") if e.strip()}   # revisan clips y solicitudes
SUPPORT_EMAILS = {e.strip().lower() for e in os.environ.get("SUPPORT_EMAILS", "").split(",") if e.strip()}     # atienden mensajes
ADMIN_ROLES = {"owner": "Dueño", "reviewer": "Revisor", "support": "Soporte"}
# Panel técnico (claves, errores, Apify, Whop, copias…): solo estos correos. Vacío = cualquier dueño (hasta configurarlo)
TECH_EMAILS = {e.strip().lower() for e in os.environ.get("TECH_EMAILS", "").split(",") if e.strip()}
CREATORS_ENABLED = False          # pagos desde el Whop de cada creador: en pausa hasta hablarlo con el cliente
CRON_SECRET = os.environ.get("CRON_SECRET", "")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()           # avisos al móvil del admin con la app ntfy
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
WHOP_API_KEY = os.environ.get("WHOP_API_KEY", "").strip()
# Avisos a los clippers: al navegador/móvil (Web Push) y por WhatsApp (API oficial de Meta, con plantilla aprobada)
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
VAPID_PUBLIC_KEY = webpush.vapid_public(VAPID_PRIVATE_KEY) if VAPID_PRIVATE_KEY else ""
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN", "").strip()
WHATSAPP_PHONE_ID = os.environ.get("WHATSAPP_PHONE_ID", "").strip()
WHATSAPP_TEMPLATE = os.environ.get("WHATSAPP_TEMPLATE", "aviso_general").strip()
WHATSAPP_LANG = os.environ.get("WHATSAPP_LANG", "es").strip()
# Formación: los vídeos se suben a Bunny Stream (la clave solo vive en Railway; el navegador sube con una firma temporal)
BUNNY_LIBRARY_ID = os.environ.get("BUNNY_LIBRARY_ID", "").strip()
BUNNY_API_KEY = os.environ.get("BUNNY_API_KEY", "").strip()
BUNNY_TOKEN_KEY = os.environ.get("BUNNY_TOKEN_KEY", "").strip()     # opcional: solo se ven los vídeos desde la web
BUNNY_CDN_HOST = os.environ.get("BUNNY_CDN_HOST", "").strip().removeprefix("https://").strip("/")   # vz-xxxx.b-cdn.net: miniaturas
WHOP_COMPANY_ID = os.environ.get("WHOP_COMPANY_ID", "").strip()
WHOP_APP_API_KEY = os.environ.get("WHOP_APP_API_KEY", "").strip()   # prueba de la opción 2: la app de Whop que instalan los creadores
WHOP_API = os.environ.get("WHOP_API_URL", "https://api.whop.com/api/v1").rstrip("/")
WHOP_AUTO_MAX = float(os.environ.get("WHOP_AUTO_MAX", "50"))    # retiros por encima de esto los aprueba un admin (0 = sin tope)
WHOP_AUTO_DAILY = float(os.environ.get("WHOP_AUTO_DAILY", "300"))  # máximo pagado solo en 24 h entre todos; lo que pase espera a un admin (0 = sin tope)
WHOP_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{2,40}$")
BASE_URL = os.environ.get("BASE_URL", "").strip().strip('"').rstrip("/")
if BASE_URL and not BASE_URL.startswith("http"):
    BASE_URL = "https://" + BASE_URL
DEV_LOGIN = os.environ.get("DEV_LOGIN") == "1" and not PRODUCTION   # nunca disponible en producción
# «Empezar de cero» borra datos reales: en producción está apagado salvo que se active a propósito en Railway
ALLOW_WIPE = os.environ.get("ALLOW_WIPE") == "1" or not PRODUCTION

PLATFORMS = {"tiktok": "TikTok", "instagram": "Instagram", "youtube": "YouTube Shorts", "facebook": "Facebook Reels"}
CATEGORIES = {"streamer": "Creadores"}          # todas las campañas son de clipping de creadores
CATEGORY_ONE = {"streamer": "Creador"}
USDT_METHOD = "Cripto (USDT · TRC20)"
WHOP_METHOD = "Whop"
PAYOUT_METHODS = [WHOP_METHOD]                                    # Whop es la única forma de cobro
PAYOUT_AVAILABLE = {WHOP_METHOD}
PAYOUT_OPEN = ("solicitado", "procesando")                        # retiros que aún no han terminado
PAYOUT_ONLY_WHOP_MSG = "Los pagos se hacen solo por Whop. Conecta tu usuario de Whop para cobrar."
FLAG_LABELS = {
    "cuenta_no_declarada": "Cuenta no vinculada",
    "cuenta_de_otro": "Cuenta de otro clipper",
    "subida_rapida": "Subida de vistas sospechosa",
    "pocos_likes": "Muy pocos likes para sus vistas",
    "publicado_antes": "Publicado antes de unirse a la campaña",
}
# Motivos de rechazo fijos (el clipper ve el texto; se puede añadir un mensaje)
REJECT_REASONS = {
    "reglas": "No cumple las reglas de la campaña",
    "menciones": "Faltan las menciones o hashtags obligatorios",
    "audio": "No usa el material de la campaña",
    "calidad": "Calidad baja (imagen, audio o edición)",
    "duplicado": "Contenido repetido o reutilizado",
    "vistas": "Vistas no orgánicas o sospechosas",
    "antiguo": "Publicado antes de unirte a la campaña",
    "cuenta": "La cuenta no está verificada en esta campaña",
    "otro": "Otro motivo",
}
SYSTEM_REASONS = {"caducado": "No llegó al pago mínimo a tiempo",
                  "sin_estadisticas": "No verificaste las vistas a tiempo"}
STATS_HOURS = 72                    # plazo para subir el vídeo de estadísticas desde que el clip llega al mínimo
STATS_MAX_MB = 100
CAMPAIGN_KINDS = {"clipping": "Clipping"}
COUNTRIES = {
    "ES": "España", "MX": "México", "AR": "Argentina", "CO": "Colombia", "CL": "Chile", "PE": "Perú",
    "VE": "Venezuela", "EC": "Ecuador", "GT": "Guatemala", "CU": "Cuba", "BO": "Bolivia", "DO": "República Dominicana",
    "HN": "Honduras", "PY": "Paraguay", "SV": "El Salvador", "NI": "Nicaragua", "CR": "Costa Rica", "PA": "Panamá",
    "UY": "Uruguay", "PR": "Puerto Rico", "US": "Estados Unidos", "GQ": "Guinea Ecuatorial", "OT": "Otro país",
}
LANGUAGES = {"es": "Español", "en": "Inglés", "pt": "Portugués", "ca": "Catalán", "fr": "Francés", "it": "Italiano"}
MAX_HANDLES = 5
MAX_QUESTIONS = 10
DEFAULT_QUESTIONS = "\n".join([                 # preguntas que salen ya escritas al crear una campaña
    "¿Cuánto tiempo llevas haciendo clips?",
    "Pásanos el enlace de un clip tuyo que haya funcionado bien",
    "¿Cuántos clips puedes publicar al día?",
    "¿En qué redes vas a publicar (TikTok, Instagram, YouTube)?",
])
CENT = 0.005

db = SQLAlchemy(app)
# Dinero: se guarda como número decimal exacto (NUMERIC), nunca como coma flotante. Python lo recibe como float
# y cents() redondea cada operación al céntimo; así lo guardado siempre cuadra.
Money = db.Numeric(14, 2, asdecimal=False)
Rate = db.Numeric(14, 4, asdecimal=False)
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
    password_hash = db.Column(db.String(255), default="")      # solo si se registró con correo y contraseña
    is_admin = db.Column(db.Boolean, default=False)
    admin_role = db.Column(db.String(20), default="")          # owner · reviewer · support (sale de las variables de Railway)
    is_banned = db.Column(db.Boolean, default=False)
    ban_reason = db.Column(db.String(255), default="")
    payout_method = db.Column(db.String(60), default="")
    payout_details = db.Column(db.String(255), default="")
    whop_user_id = db.Column(db.String(40), default="")
    whop_username = db.Column(db.String(80), default="")
    tiktok_handles = db.Column(db.String(400), default="")
    instagram_handles = db.Column(db.String(400), default="")
    youtube_handles = db.Column(db.String(400), default="")
    facebook_handles = db.Column(db.String(400), default="")
    verified_handles = db.Column(db.String(1200), default="")   # sin uso (de una versión antigua); se puede borrar más adelante
    verify_code = db.Column(db.String(12), default="")           # código que el clipper pone en su bio para verificar sus cuentas
    accepted_terms_at = db.Column(db.DateTime, nullable=True)
    trusted = db.Column(db.Boolean, default=False)                # de confianza: sus clips se pueden aprobar solos
    country = db.Column(db.String(2), default="")
    country_other = db.Column(db.String(60), default="")       # si elige «Otro país», el que escribe

    @property
    def country_name(self):
        if self.country == "OT":
            return self.country_other or "Otro país"
        return COUNTRIES.get(self.country or "", "")
    languages = db.Column(db.String(60), default="")
    whatsapp = db.Column(db.String(30), default="")
    onboarded_at = db.Column(db.DateTime, nullable=True)
    wa_joined_at = db.Column(db.DateTime, nullable=True)        # pulsó «Unirme» al grupo general de WhatsApp
    notify_off = db.Column(db.String(400), default="")             # avisos apagados: «evento.canal» separados por comas
    avatar_data = db.deferred(db.Column(db.LargeBinary, nullable=True))
    avatar_ver = db.Column(db.Integer, default=0)
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

    def bio_code(self):
        """Código que el clipper pone en su bio para demostrar que la cuenta es suya."""
        if not self.verify_code:
            self.verify_code = "CP" + str(secrets.randbelow(900000) + 100000)
            db.session.commit()
        return self.verify_code

    def handles(self, platform):
        raw = getattr(self, f"{platform}_handles", "") or ""
        return [h for h in raw.split(",") if h]

    @property
    def has_any_handle(self):
        return any(self.handles(p) for p in PLATFORMS)



class Creator(db.Model):
    """Creador que paga sus campañas desde SU Whop. La clave nunca se guarda aquí:
    va en una variable de Railway (p. ej. WHOP_KEY_PABLO) y aquí solo se guarda el NOMBRE de esa variable."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    contact = db.Column(db.String(255), default="")          # correo, WhatsApp… para avisarle
    key_var = db.Column(db.String(60), default="")           # nombre de la variable de Railway con su clave de Whop
    company_id = db.Column(db.String(40), default="")        # biz_ de su empresa (si se deja vacío, se detecta con la clave)
    notes = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    campaigns = db.relationship("Campaign", backref="creator", lazy=True)


class Campaign(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    creator_id = db.Column(db.Integer, db.ForeignKey("creator.id"), nullable=True, index=True)   # quién paga (vacío = cuenta principal)
    song = db.Column(db.String(200), nullable=False)
    artist = db.Column(db.String(200), nullable=False)
    cover_url = db.Column(db.String(500), default="")
    cover_data = db.deferred(db.Column(db.LargeBinary, nullable=True))
    cover_mime = db.Column(db.String(40), default="")
    cover_version = db.Column(db.Integer, default=0)       # 0 = sin imagen subida
    cover_tint = db.Column(db.String(200), default="")     # color de la tarjeta destacada, sacado de la portada
    cover_lqip = db.Column(db.Text, default="")            # versión diminuta de la portada (se ve mientras carga la buena)
    cover_meta_v = db.Column(db.Integer, default=0)        # de qué versión de la portada son el color y la miniatura
    sound_url = db.Column(db.String(500), default="")
    materials = db.Column(db.Text, default="")               # materiales de referencia: «Nombre | enlace» por línea
    rules = db.Column(db.Text, default="")                 # reglas: qué publicar
    rules_required = db.Column(db.Text, default="")        # reglas: obligatorio
    rules_forbidden = db.Column(db.Text, default="")       # reglas: prohibido
    rate = db.Column(Rate, nullable=False)              # pago por cada 1.000.000 de vistas (el más alto de las redes)
    rate_tiktok = db.Column(Rate, nullable=True)        # pago de cada red (vacío = el general)
    rate_instagram = db.Column(Rate, nullable=True)
    rate_youtube = db.Column(Rate, nullable=True)
    rate_facebook = db.Column(Rate, nullable=True)
    min_tiktok = db.Column(Money, nullable=True)         # pago mínimo por clip de cada red en dinero (vacío = el general, 0 = sin mínimo)
    min_instagram = db.Column(Money, nullable=True)
    min_youtube = db.Column(Money, nullable=True)
    min_facebook = db.Column(Money, nullable=True)
    max_tiktok = db.Column(Money, nullable=True)         # pago máximo por clip de cada red (vacío = el general, 0 = sin tope)
    max_instagram = db.Column(Money, nullable=True)
    max_youtube = db.Column(Money, nullable=True)
    max_facebook = db.Column(Money, nullable=True)
    budget = db.Column(Money, nullable=False)
    spent = db.Column(Money, default=0.0)                # dinero ya asignado a clips (libro de cuentas)
    min_views = db.Column(db.Integer, default=0)            # mínimo para cobrar un vídeo
    max_per_video = db.Column(Money, nullable=True)      # tope por vídeo (vacío = sin tope)
    platforms = db.Column(db.String(100), default="tiktok,instagram,youtube")
    status = db.Column(db.String(20), default="activa")     # activa / pausada / finalizada
    featured = db.Column(db.Boolean, default=False)
    full = db.Column(db.Boolean, default=False)              # plazas completas: no se aceptan solicitudes nuevas
    category = db.Column(db.String(20), default="streamer")
    join_questions = db.Column(db.Text, default="")         # preguntas para unirse, una por línea
    kind = db.Column(db.String(20), default="clipping")      # siempre clipping
    bonus_per_clip = db.Column(Money, nullable=True)        # dinero fijo extra por clip aprobado que llega al mínimo
    starts_at = db.Column(db.DateTime, nullable=True)        # antes de esta fecha no se aceptan clips
    ends_on = db.Column(db.DateTime, nullable=True)          # al llegar, la campaña se cierra sola
    max_members = db.Column(db.Integer, default=0)           # plazas (0 = sin límite); al llenarse pasa a «plazas completas»
    countries = db.Column(db.String(200), default="")        # países permitidos (vacío = todos)
    new_accounts_only = db.Column(db.Boolean, default=False) # hay que usar una cuenta nueva, solo para esta campaña
    earn_days = db.Column(db.Integer, default=0)             # días que un clip suma vistas desde que se envía (0 = hasta el final)
    expire_days = db.Column(db.Integer, default=0)           # días para llegar al mínimo; si no, se rechaza solo (0 = nunca)
    review_at_min = db.Column(db.Boolean, default=True)      # solo se revisa un clip cuando llega al pago mínimo
    auto_approve_trusted = db.Column(db.Boolean, default=False)
    auto_reject_old = db.Column(db.Boolean, default=False)   # rechaza solo los vídeos publicados antes de unirse
    auto_reject_unverified = db.Column(db.Boolean, default=False)
    low_alert_sent = db.Column(db.Boolean, default=False)    # ya se avisó de que queda poco presupuesto
    announcements = db.Column(db.Text, default="")           # anuncios para los clippers («fecha | texto» por línea)
    ideas = db.Column(db.Text, default="")                   # ideas de clips, una por línea
    discord_url = db.Column(db.String(300), default="")      # antiguo (ya no se usa)
    whatsapp_url = db.Column(db.String(300), default="")     # enlace al grupo de WhatsApp de la campaña
    stats_required = db.Column(db.Boolean, default=False)     # al llegar al mínimo, el clipper sube un vídeo de sus estadísticas
    stats_note = db.Column(db.String(300), default="")        # qué países se piden (lo lee el clipper antes de grabar)
    channels_ready = db.Column(db.Boolean, default=False)    # ya tiene sus canales (se crean la primera vez)
    report_token = db.Column(db.String(40), unique=True, nullable=True)
    ended_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    submissions = db.relationship("Submission", backref="campaign", lazy=True)

    def rate_for(self, platform):
        """Pago por 1M de vistas de una red concreta."""
        return getattr(self, f"rate_{platform}", None) or self.rate or 0

    @property
    def platform_rates(self):
        """[(red, pago por 1M)] de las redes que acepta la campaña."""
        return [(p, self.rate_for(p)) for p in self.platform_list]

    @property
    def rates_vary(self):
        return len({round(r, 4) for _, r in self.platform_rates}) > 1

    @property
    def min_money(self):
        """Pago mínimo por clip en dinero (lo que un clip tiene que valer para empezar a cobrar)."""
        return round((self.min_views or 0) / 1_000_000 * (self.rate or 0), 2)   # en céntimos, como se escribió

    def min_for(self, platform):
        """Pago mínimo por clip en una red (0 = sin mínimo)."""
        v = getattr(self, f"min_{platform}", None)
        return self.min_money if v is None else round(v, 2)

    def max_for(self, platform):
        """Pago máximo por clip en una red (None = sin tope)."""
        v = getattr(self, f"max_{platform}", None)
        v = self.max_per_video if v is None else v
        return v if v and v > 0 else None

    def min_views_for(self, platform):
        r, m = self.rate_for(platform), self.min_for(platform)
        return int(math.ceil(m * 1_000_000 / r)) if r and m else 0

    @property
    def pay_rows(self):
        """[(red, pago por 1M, mínimo, máximo)] de las redes que acepta la campaña."""
        return [(p, self.rate_for(p), self.min_for(p), self.max_for(p)) for p in self.platform_list]

    @property
    def min_range(self):
        """(el mínimo más bajo, el más alto) de todas las redes — para las tarjetas de fuera."""
        mins = [m for _, _, m, _ in self.pay_rows] or [0]
        return min(mins), max(mins)

    @property
    def top_max(self):
        """El máximo por clip más alto de las redes, o None si alguna no tiene tope."""
        maxs = [m for _, _, _, m in self.pay_rows]
        return None if (not maxs or None in maxs) else max(maxs)

    @property
    def maxs_vary(self):
        return len({m for _, _, _, m in self.pay_rows}) > 1

    @property
    def material_list(self):
        """[(nombre, enlace, tipo)] — tipo sirve para el icono: tiktok, instagram, youtube, drive o link."""
        out = []
        lines = (self.materials or "").splitlines() or ([self.sound_url] if self.sound_url else [])
        for line in lines:
            name, _, url = line.rpartition("|") if "|" in line else ("", "", line)
            name, url = name.strip(), url.strip()
            if not url.startswith(("https://", "http://")):
                continue
            host = urlparse(url).netloc.lower()
            kind = ("tiktok" if "tiktok." in host else "instagram" if "instagram." in host
                    else "youtube" if ("youtube." in host or "youtu.be" in host)
                    else "facebook" if ("facebook." in host or "fb.watch" in host)
                    else "drive" if host.endswith(("drive.google.com", "docs.google.com")) else "link")
            if not name:
                name = {"tiktok": "TikTok", "instagram": "Instagram", "youtube": "YouTube", "facebook": "Facebook",
                        "drive": "Google Drive"}.get(kind) or host.removeprefix("www.") or "Enlace"
            out.append((name[:80], url, kind))
        return out[:12]

    @property
    def rules_text(self):
        """Todas las reglas en un solo texto (las campañas antiguas tenían 3 apartados separados)."""
        parts = [(self.rules or "").strip()]
        if (self.rules_required or "").strip():
            parts.append("Obligatorio:\n" + self.rules_required.strip())
        if (self.rules_forbidden or "").strip():
            parts.append("Prohibido:\n" + self.rules_forbidden.strip())
        return "\n\n".join(p for p in parts if p)

    @property
    def question_list(self):
        return [q.strip() for q in (self.join_questions or "").splitlines() if q.strip()][:MAX_QUESTIONS]

    @property
    def platform_list(self):
        return [p for p in (self.platforms or "").split(",") if p]

    @property
    def remaining(self):
        return max(round((self.budget or 0) - (self.spent or 0), 2), 0.0)

    @property
    def exhausted(self):
        return (self.spent or 0) >= (self.budget or 0) - CENT

    @property
    def country_list(self):
        return [c for c in (self.countries or "").split(",") if c in COUNTRIES]

    @property
    def not_started(self):
        return bool(self.starts_at and self.starts_at > datetime.utcnow())

    @property
    def low_budget(self):
        return bool(self.budget) and not self.exhausted and self.remaining <= self.budget * 0.2

    @property
    def members_accepted(self):
        return CampaignMember.query.filter_by(campaign_id=self.id, status="aceptada").count() if self.id else 0

    @property
    def announcement_list(self):
        out = []
        for line in (self.announcements or "").splitlines():
            d, sep, t = line.partition("|")
            if sep and t.strip():
                out.append((d.strip(), t.strip()))
            elif line.strip():
                out.append(("", line.strip()))
        return out

    @property
    def idea_list(self):
        return [i.strip() for i in (self.ideas or "").splitlines() if i.strip()][:30]


class Submission(db.Model):
    __table_args__ = (db.Index("ix_submission_status", "status"),                       # casi todo filtra por estado
                      db.Index("ix_submission_campaign_status", "campaign_id", "status"))
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey("campaign.id"), nullable=False, index=True)
    url = db.Column(db.String(500), nullable=False)
    platform = db.Column(db.String(20), nullable=False)
    video_key = db.Column(db.String(120), unique=True, nullable=False)  # evita repetir vídeos
    views = db.Column(db.Integer, default=0)
    likes = db.Column(db.Integer, nullable=True)
    comments = db.Column(db.Integer, nullable=True)
    shares = db.Column(db.Integer, nullable=True)
    author = db.Column(db.String(80), default="")
    earned = db.Column(Money, default=0.0)              # dinero asignado a este clip; nunca baja solo
    flags = db.Column(db.String(200), default="")
    status = db.Column(db.String(20), default="pendiente")  # pendiente / aprobado / rechazado
    reject_reason = db.Column(db.String(255), default="")
    reject_code = db.Column(db.String(20), default="")
    reviewed_at = db.Column(db.DateTime, nullable=True)
    posted_at = db.Column(db.DateTime, nullable=True)       # cuándo se publicó el vídeo (lo dice Apify)
    appeal_text = db.Column(db.String(1000), default="")     # el clipper puede apelar un rechazo una sola vez
    appeal_status = db.Column(db.String(20), default="")     # "" · pendiente · aceptada · rechazada
    appeal_at = db.Column(db.DateTime, nullable=True)
    review_since = db.Column(db.DateTime, nullable=True)    # cuándo llegó al mínimo y pasó a «Por revisar»
    stats_status = db.Column(db.String(12), default="")      # vídeo de estadísticas: "" · pedido · subiendo · enviado
    stats_guid = db.Column(db.String(64), default="")        # el vídeo en Bunny
    stats_at = db.Column(db.DateTime, nullable=True)        # cuándo lo envió
    stats_asked_at = db.Column(db.DateTime, nullable=True)  # cuándo se le pidió (desde aquí cuenta el plazo)
    stats_reason = db.Column(db.String(300), default="")    # si se le pide otra: qué corregir
    source = db.Column(db.String(10), default="")          # "auto" = la web lo encontró sola en el perfil
    video_guid = db.Column(db.String(64), default="")        # copia del vídeo en Bunny para revisarlo
    video_status = db.Column(db.String(20), default="")      # "" · subiendo · listo · error · borrado
    video_tries = db.Column(db.Integer, default=0)
    media_url = db.Column(db.String(1000), default="")       # enlace de descarga que da Apify (caduca)
    thumb_url = db.Column(db.String(1000), default="")       # portada que da Apify (se renueva en cada lectura)
    caption = db.Column(db.String(300), default="")          # texto del vídeo
    views_updated_at = db.Column(db.DateTime, nullable=True)
    checked_at = db.Column(db.DateTime, nullable=True)
    views_error = db.Column(db.String(255), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    my_demo = db.Column(db.Boolean, default=False)          # demo personal del admin (se quita aparte)
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
    likes = db.Column(db.Integer, nullable=True)
    comments = db.Column(db.Integer, nullable=True)
    shares = db.Column(db.Integer, nullable=True)
    taken_at = db.Column(db.DateTime, default=datetime.utcnow)


class Payout(db.Model):
    """Retiros: el clipper los solicita y el admin los marca como pagados."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    creator_id = db.Column(db.Integer, db.ForeignKey("creator.id"), nullable=True, index=True)  # de qué Whop sale (vacío = principal)
    amount = db.Column(Money, nullable=False)
    method = db.Column(db.String(60), default="")
    details = db.Column(db.String(255), default="")
    status = db.Column(db.String(20), default="solicitado")  # solicitado / procesando / pagado / rechazado
    note = db.Column(db.String(255), default="")
    ext_id = db.Column(db.String(80), default="")    # ID de la transferencia en Whop
    error = db.Column(db.String(400), default="")    # última respuesta de Whop si falló (solo la ve el admin)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    paid_at = db.Column(db.DateTime, nullable=True)
    user = db.relationship("User", backref="payouts")
    creator = db.relationship("Creator")


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
    image_data = db.deferred(db.Column(db.LargeBinary, nullable=True))     # foto adjunta (captura, etc.)
    image_mime = db.Column(db.String(40), default="")
    read_by_admin = db.Column(db.Boolean, default=False)
    read_by_user = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    user = db.relationship("User", foreign_keys=[user_id])
    sender = db.relationship("User", foreign_keys=[sender_id])


class IgnoredClip(db.Model):
    """Vídeo que el clipper borró de Mis clips: la detección automática no lo vuelve a añadir."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    video_key = db.Column(db.String(120), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class CampMessage(db.Model):
    """Soporte de una campaña: conversación privada de un clipper con el equipo y los mods de esa campaña."""
    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey("campaign.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)    # el clipper (dueño del hilo)
    sender_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    from_staff = db.Column(db.Boolean, default=False)
    body = db.Column(db.Text, nullable=False)
    read_by_user = db.Column(db.Boolean, default=False)
    read_by_staff = db.Column(db.Boolean, default=False)
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
    answers = db.Column(db.Text, default="")                              # respuestas al formulario (JSON)
    bio_verified = db.Column(db.Boolean, default=False)                   # el código estaba en su bio
    bio_status = db.Column(db.String(160), default="")                    # resultado de la última comprobación
    bio_checked_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    decided_at = db.Column(db.DateTime, nullable=True)
    my_demo = db.Column(db.Boolean, default=False)          # demo personal del admin (se quita aparte)
    last_post_at = db.Column(db.DateTime, nullable=True)       # último vídeo que vimos en su perfil
    last_scan_at = db.Column(db.DateTime, nullable=True)       # última vez que miramos su perfil
    user = db.relationship("User")
    campaign = db.relationship("Campaign")

    @property
    def answer_list(self):
        try:
            return [(str(q), str(a)) for q, a in json.loads(self.answers or "[]")]
        except (ValueError, TypeError):
            return []


class CampaignMember(db.Model):
    """Solicitud de un clipper para entrar en una campaña: responde a las preguntas y el equipo la acepta o rechaza.
    Una vez aceptado, el clipper añade la cuenta (normalmente creada solo para esa campaña) y la verifica con su bio."""
    __table_args__ = (db.UniqueConstraint("user_id", "campaign_id", name="uq_campaign_member"),)
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey("campaign.id"), nullable=False, index=True)
    status = db.Column(db.String(20), default="pendiente", index=True)    # pendiente · aceptada · rechazada
    answers = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    decided_at = db.Column(db.DateTime, nullable=True)
    my_demo = db.Column(db.Boolean, default=False)          # demo personal del admin (se quita aparte)
    user = db.relationship("User")
    campaign = db.relationship("Campaign")

    @property
    def answer_list(self):
        try:
            return [(str(q), str(a)) for q, a in json.loads(self.answers or "[]")]
        except (ValueError, TypeError):
            return []


class CampaignWaitlist(db.Model):
    """Clippers que quieren que les avisemos cuando una campaña con plazas completas vuelva a abrir."""
    __table_args__ = (db.UniqueConstraint("user_id", "campaign_id", name="uq_campaign_waitlist"),)
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey("campaign.id"), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


def notify_waitlist(camp, sender_id=None):
    """La campaña vuelve a tener plazas: aviso por el chat a todos los que lo pidieron y se vacía la lista."""
    rows = CampaignWaitlist.query.filter_by(campaign_id=camp.id).all()
    for w in rows:
        db.session.add(Message(user_id=w.user_id, from_admin=True, sender_id=sender_id, read_by_admin=True, read_by_user=False,
                               body=f"¡Hay plazas en «{camp.song}»! Ya puedes solicitar entrar en la campaña."))
        notify_user(w.user_id, "plazas", f"Hay plazas en {camp.song}", "Entra ya y pide unirte antes de que se llenen.",
                    f"/campana/{camp.id}", camp_id=camp.id)
        db.session.delete(w)
    return len(rows)


def campaign_member(user_id, campaign_id):
    return CampaignMember.query.filter_by(user_id=user_id, campaign_id=campaign_id).first()


JOIN_STATUSES = {"pendiente": "Sin verificar", "aceptada": "Verificada", "rechazada": "Rechazada"}        # cuentas
MEMBER_STATUSES = {"pendiente": "En revisión", "aceptada": "Aceptado", "rechazada": "Rechazado"}          # solicitudes
MAX_JOIN_ACCOUNTS = 5


def campaign_accounts(user_id, campaign_id, status=None, platform=None):
    q = CampaignAccount.query.filter_by(user_id=user_id, campaign_id=campaign_id)
    if status:
        q = q.filter_by(status=status)
    if platform:
        q = q.filter_by(platform=platform)
    return q.order_by(CampaignAccount.created_at).all()


class CampaignLead(db.Model):
    """Solicitudes de creadores que quieren lanzar una campaña (desde la portada)."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(255), nullable=False)
    brand = db.Column(db.String(200), nullable=False)          # nombre del creador o canal
    category = db.Column(db.String(20), default="streamer")
    budget = db.Column(db.String(40), default="")
    link = db.Column(db.String(500), default="")
    message = db.Column(db.Text, default="")
    status = db.Column(db.String(20), default="nueva")         # nueva / contactada / convertida / descartada
    ip = db.Column(db.String(64), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


class Notice(db.Model):
    """Aviso de la campanita de un clipper."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    kind = db.Column(db.String(30), default="")
    title = db.Column(db.String(120), default="")
    body = db.Column(db.String(400), default="")
    url = db.Column(db.String(300), default="")
    campaign_id = db.Column(db.Integer, nullable=True)    # para enseñar la portada de la campaña (sin clave ajena: se borra aparte)
    read = db.Column(db.Boolean, default=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    my_demo = db.Column(db.Boolean, default=False)          # demo personal del admin (se quita aparte)


class ChannelSection(db.Model):
    """Grupo de canales dentro de una campaña (p. ej. «Empieza aquí»)."""
    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey("campaign.id"), nullable=False, index=True)
    title = db.Column(db.String(80), nullable=False)
    position = db.Column(db.Integer, default=0)
    channels = db.relationship("Channel", backref="section", lazy=True, order_by="Channel.position",
                               cascade="all, delete-orphan")


class Channel(db.Model):
    """Canal de una campaña. kind: posts (publicaciones) · rules (normas de la campaña) · resources (materiales)
    · winners (los clips con más vistas, automático)."""
    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey("campaign.id"), nullable=False, index=True)
    section_id = db.Column(db.Integer, db.ForeignKey("channel_section.id"), nullable=False, index=True)
    name = db.Column(db.String(60), nullable=False)
    emoji = db.Column(db.String(8), default="")
    kind = db.Column(db.String(12), default="posts")
    position = db.Column(db.Integer, default=0)
    posts = db.relationship("Post", backref="channel", lazy=True, order_by="Post.created_at.desc()",
                            cascade="all, delete-orphan")


class Post(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    channel_id = db.Column(db.Integer, db.ForeignKey("channel.id"), nullable=False, index=True)
    author_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    body = db.Column(db.Text, default="")
    image_data = db.deferred(db.Column(db.LargeBinary, nullable=True))
    image_mime = db.Column(db.String(30), default="")
    pinned = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    edited_at = db.Column(db.DateTime, nullable=True)
    author = db.relationship("User")
    likes = db.relationship("PostLike", backref="post", lazy=True, cascade="all, delete-orphan")
    comments = db.relationship("PostComment", backref="post", lazy=True, order_by="PostComment.created_at",
                               cascade="all, delete-orphan")


class PostLike(db.Model):
    __table_args__ = (db.UniqueConstraint("post_id", "user_id", name="uq_post_like"),)
    id = db.Column(db.Integer, primary_key=True)
    post_id = db.Column(db.Integer, db.ForeignKey("post.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)


class PostComment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    post_id = db.Column(db.Integer, db.ForeignKey("post.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    body = db.Column(db.String(1000), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user = db.relationship("User")


class CampaignMod(db.Model):
    """Moderador de una campaña: publica en sus canales, modera comentarios, revisa clips y acepta solicitudes.
    No toca dinero ni ajustes, y no revisa sus propios clips."""
    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey("campaign.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    perms = db.Column(db.String(300), default="")             # permisos separados por comas ("" = los de siempre: MOD_DEFAULT)
    user = db.relationship("User")
    campaign = db.relationship("Campaign")
    __table_args__ = (db.UniqueConstraint("campaign_id", "user_id"),)

    @property
    def perm_set(self):
        return set(MOD_DEFAULT) if not self.perms else {p_ for p_ in self.perms.split(",") if p_ in MOD_PERMS}


def is_mod(user, cid):
    if not getattr(user, "is_authenticated", False) or not cid:
        return False
    return bool(CampaignMod.query.filter_by(campaign_id=cid, user_id=user.id).first())


def campaign_mod_ids(cid):
    return {m.user_id for m in CampaignMod.query.filter_by(campaign_id=cid)}


def mod_campaigns(user):
    if not getattr(user, "is_authenticated", False) or getattr(user, "is_admin", False):
        return []
    return [m.campaign for m in CampaignMod.query.filter_by(user_id=user.id).all() if m.campaign]


# Todo lo que se puede hacer en una campaña. Los 6 primeros vienen marcados al añadir un mod; el resto, desmarcados.
MOD_PERMS = {"revisar": "Revisar clips", "verificar": "Pedir otra verificación de vistas", "solicitudes": "Solicitudes y cuentas",
             "canales": "Publicar en canales", "comentarios": "Moderar comentarios", "soporte": "Soporte de la campaña",
             "estadisticas": "Ver estadísticas", "clippers": "Ver clippers y lo que gana cada uno",
             "gestionar_canales": "Gestionar canales y grupo de WhatsApp", "informe": "Informe del creador",
             "csv": "Descargar CSV de clips", "editar": "Editar la campaña (normas, materiales, plazas…)",
             "vistas": "Corregir vistas a mano", "dinero": "Presupuesto y tarifas",
             "estado": "Pausar, abrir/cerrar plazas y finalizar"}
MOD_DEFAULT = ("revisar", "verificar", "solicitudes", "canales", "comentarios", "soporte")
MOD_RISKY = {"vistas": "cambia lo que se paga", "dinero": "toca el dinero de la campaña", "estado": "puede cerrar la campaña"}
ENDPOINT_PERM = {"admin_campaign_review": "revisar", "admin_review": ("revisar", "verificar", "vistas"), "admin_review_bulk": "revisar",
                 "admin_campaign_requests": "solicitudes", "admin_campaign_member": "solicitudes",
                 "admin_campaign_account": "solicitudes", "admin_campaign_channels": ("canales", "gestionar_canales"),
                 "admin_campaign_support": "soporte", "admin_campaign": ("estadisticas", "informe"),
                 "admin_campaign_clippers": "clippers", "admin_campaign_csv": "csv", "admin_campaign_form": "editar",
                 "admin_campaign_state": ("estado", "dinero"), "admin_campaign_close": "estado"}


def mod_perms(user, cid):
    if not getattr(user, "is_authenticated", False) or not cid:
        return set()
    m = CampaignMod.query.filter_by(campaign_id=cid, user_id=user.id).first()
    return m.perm_set if m else set()


def mod_can(user, cid, perm):
    if isinstance(perm, (tuple, set, list)):
        return bool(set(perm) & mod_perms(user, cid))
    return perm in mod_perms(user, cid)


def perm_ok(cid, perm, admin_ep=None):
    """Para pantallas compartidas: el admin según su rol; el mod según sus permisos en esa campaña."""
    if getattr(current_user, "is_admin", False):
        return admin_can(admin_ep or request.endpoint)
    return mod_can(current_user, cid, perm)


def mod_home(cid, user=None):
    """Primera pantalla del panel de la campaña a la que el mod tiene acceso."""
    ps = mod_perms(user or current_user, cid)
    for perm, ep in (("revisar", "admin_campaign_review"), ("solicitudes", "admin_campaign_requests"),
                     ("soporte", "admin_campaign_support"), ("canales", "admin_campaign_channels"),
                     ("gestionar_canales", "admin_campaign_channels"), ("estadisticas", "admin_campaign"),
                     ("informe", "admin_campaign"), ("clippers", "admin_campaign_clippers"), ("editar", "admin_campaign_form")):
        if perm in ps:
            return url_for(ep, cid=cid)
    return url_for("campaign", cid=cid)


MOD_ENDPOINTS = set(ENDPOINT_PERM)
MOD_CHANNEL_ACTS = {"publicar", "post_editar", "post_borrar", "post_fijar"}


DEFAULT_CHANNELS = [
    ("Empieza aquí", [("📜", "Normas", "rules")]),
    ("Comunidad", [("📣", "Anuncios", "posts")]),
    ("Contenido y herramientas", [("🏆", "Cuentas TOP", "posts"), ("💡", "Ideas virales", "posts"),
                                  ("📁", "Recursos", "resources"), ("🎬", "Clips ganadores", "winners")]),
]


def ensure_channels(camp):
    """La primera vez crea los canales de la campaña y pasa a ellos los anuncios y las ideas que ya hubiera."""
    if camp.channels_ready:
        return
    if ChannelSection.query.filter_by(campaign_id=camp.id).first():
        camp.channels_ready = True
        db.session.commit()
        return
    made = {}
    for i, (title, chans) in enumerate(DEFAULT_CHANNELS):
        sec = ChannelSection(campaign_id=camp.id, title=title, position=i)
        db.session.add(sec)
        db.session.flush()
        for j, (emo, name, kind) in enumerate(chans):
            ch = Channel(campaign_id=camp.id, section_id=sec.id, name=name, emoji=emo, kind=kind, position=j)
            db.session.add(ch)
            db.session.flush()
            made[name] = ch
    for d, t in camp.announcement_list:                  # anuncios antiguos → publicaciones en «Anuncios»
        when = None
        for fmt in ("%d/%m/%Y", "%d/%m"):
            try:
                when = datetime.strptime(d, fmt)
                if fmt == "%d/%m":
                    when = when.replace(year=datetime.utcnow().year)
                break
            except ValueError:
                pass
        db.session.add(Post(channel_id=made["Anuncios"].id, body=t, created_at=when or camp.created_at))
    if camp.idea_list:                                   # ideas antiguas → una publicación en «Ideas virales»
        db.session.add(Post(channel_id=made["Ideas virales"].id, body="\n".join("• " + x for x in camp.idea_list),
                            created_at=camp.created_at))
    camp.channels_ready = True
    db.session.commit()


def campaign_channels(camp):
    ensure_channels(camp)
    return (ChannelSection.query.filter_by(campaign_id=camp.id).order_by(ChannelSection.position, ChannelSection.id)
            .options(selectinload(ChannelSection.channels)).all())


def is_campaign_member(user, camp):
    if getattr(user, "is_admin", False):
        return True
    m = campaign_member(user.id, camp.id)
    return bool(m and m.status == "aceptada") or bool(campaign_accounts(user.id, camp.id, "aceptada"))


_URL_RE = re.compile(r"(https?://[^\s<]+)")


def linkify(text_):
    """Texto de una publicación con los enlaces clicables (todo lo demás se escapa)."""
    from markupsafe import Markup, escape
    out, last = [], 0
    for m in _URL_RE.finditer(text_ or ""):
        url = m.group(1).rstrip(".,);:!?»\"'")
        out.append(escape((text_ or "")[last:m.start()]))
        out.append(Markup('<a class="link" href="{0}" target="_blank" rel="noopener nofollow">{0}</a>').format(url))
        last = m.start() + len(url)
    out.append(escape((text_ or "")[last:]))
    return Markup("").join(out)


class PushSub(db.Model):
    """Un navegador o móvil del clipper que aceptó recibir avisos."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    endpoint = db.Column(db.String(800), unique=True, nullable=False)
    p256dh = db.Column(db.String(200), nullable=False)
    auth = db.Column(db.String(60), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Course(db.Model):
    """Curso de formación: capítulos con lecciones (vídeo, texto y archivos)."""
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(160), nullable=False)
    description = db.Column(db.Text, default="")
    position = db.Column(db.Integer, default=0)
    published = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    cover_data = db.deferred(db.Column(db.LargeBinary, nullable=True))   # portada de la tarjeta del curso
    cover_version = db.Column(db.Integer, default=0)                     # 0 = sin portada (se dibuja una con el nombre)
    chapters = db.relationship("Chapter", backref="course", lazy=True, order_by="Chapter.position",
                               cascade="all, delete-orphan")

    @property
    def lessons(self):
        return [ls for ch in self.chapters for ls in ch.lessons]

    @property
    def visible_lessons(self):
        return [ls for ls in self.lessons if ls.published]


class Chapter(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    course_id = db.Column(db.Integer, db.ForeignKey("course.id"), nullable=False, index=True)
    title = db.Column(db.String(160), nullable=False)
    position = db.Column(db.Integer, default=0)
    lessons = db.relationship("Lesson", backref="chapter", lazy=True, order_by="Lesson.position",
                              cascade="all, delete-orphan")


class Lesson(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    chapter_id = db.Column(db.Integer, db.ForeignKey("chapter.id"), nullable=False, index=True)
    title = db.Column(db.String(160), nullable=False)
    body = db.Column(db.Text, default="")                    # texto de la lección
    video_guid = db.Column(db.String(64), default="")        # vídeo subido a Bunny Stream
    video_url = db.Column(db.String(500), default="")        # o un enlace de YouTube / Vimeo
    video_status = db.Column(db.String(20), default="")      # subiendo · procesando · listo
    duration = db.Column(db.Integer, default=0)              # segundos
    file_name = db.Column(db.String(160), default="")
    file_mime = db.Column(db.String(80), default="")
    file_data = db.deferred(db.Column(db.LargeBinary, nullable=True))
    position = db.Column(db.Integer, default=0)
    published = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    progress = db.relationship("LessonProgress", backref="lesson", lazy=True, cascade="all, delete-orphan")


class LessonProgress(db.Model):
    __table_args__ = (db.UniqueConstraint("user_id", "lesson_id", name="uq_lesson_progress"),)
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    lesson_id = db.Column(db.Integer, db.ForeignKey("lesson.id"), nullable=False, index=True)
    seconds = db.Column(db.Integer, default=0)               # hasta dónde lo vio (para seguir)
    watched = db.Column(db.Text, default="")                 # tramos vistos de verdad, en segundos: "0-120,300-410"
    completed_at = db.Column(db.DateTime, nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)


class Setting(db.Model):
    key = db.Column(db.String(60), primary_key=True)
    value = db.Column(db.Text, default="")


@login_manager.user_loader
def load_user(uid):
    try:
        user = db.session.get(User, int(uid))
    except (TypeError, ValueError):
        return None
    if user and user.is_admin:
        role = role_for(user.email)
        if not role:
            user.is_admin, user.admin_role = False, ""     # su correo ya no está en ninguna lista: deja de ser admin al momento
            db.session.commit()
        elif role != user.admin_role:
            user.admin_role = role                          # le han cambiado de lista en Railway
            db.session.commit()
    return user


def role_for(email):
    """Rol de admin según las variables de Railway: ADMIN_EMAIL (dueño), REVIEWER_EMAILS (revisor), SUPPORT_EMAILS (soporte)."""
    email = (email or "").lower()
    if email in ADMIN_EMAILS:
        return "owner"
    if email in REVIEWER_EMAILS:
        return "reviewer"
    if email in SUPPORT_EMAILS:
        return "support"
    return ""


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
        have = {ix["name"] for ix in insp.get_indexes(table.name)}
        for ix in table.indexes:                       # índices nuevos en tablas que ya existían
            if ix.name and ix.name not in have:
                ix.create(bind=db.engine, checkfirst=True)
                log.info("Índice añadido: %s", ix.name)


MONEY_COLUMNS = {
    "campaign": {"budget": 2, "spent": 2, "max_per_video": 2, "min_tiktok": 2, "min_instagram": 2, "min_youtube": 2,
                 "min_facebook": 2, "max_tiktok": 2, "max_instagram": 2, "max_youtube": 2, "max_facebook": 2,
                 "rate": 4, "rate_tiktok": 4, "rate_instagram": 4, "rate_youtube": 4, "rate_facebook": 4},
    "submission": {"earned": 2},
    "payout": {"amount": 2},
}


def migrate_money_columns():
    """Las columnas de dinero eran coma flotante: en PostgreSQL se pasan a NUMERIC exacto, redondeando al céntimo (una sola vez)."""
    if db.engine.dialect.name != "postgresql":
        return
    with db.engine.begin() as conn:
        for table, cols in MONEY_COLUMNS.items():
            for col, scale in cols.items():
                kind = conn.execute(text("SELECT data_type FROM information_schema.columns WHERE table_name = :t AND column_name = :c"),
                                    {"t": table, "c": col}).scalar()
                if kind in ("double precision", "real"):
                    conn.execute(text(f'ALTER TABLE "{table}" ALTER COLUMN "{col}" TYPE NUMERIC(14,{scale}) '
                                      f'USING round("{col}"::numeric, {scale})'))
                    log.info("Dinero exacto: %s.%s pasa a NUMERIC", table, col)
        kind = conn.execute(text("SELECT data_type FROM information_schema.columns WHERE table_name = 'setting' AND column_name = 'value'")).scalar()
        if kind == "character varying":                 # los ajustes pueden ser largos (anuncio de inicio, su imagen)
            conn.execute(text('ALTER TABLE "setting" ALTER COLUMN "value" TYPE TEXT'))


def migrate_members():
    """Antes, la solicitud iba unida a la cuenta. Crea la solicitud de quien ya tenía cuentas en una campaña (solo una vez)."""
    if CampaignMember.query.first() or not CampaignAccount.query.first():
        return
    seen = {}
    for a in CampaignAccount.query.order_by(CampaignAccount.created_at).all():
        key = (a.user_id, a.campaign_id)
        m = seen.get(key)
        if not m:
            m = seen[key] = CampaignMember(user_id=a.user_id, campaign_id=a.campaign_id, status=a.status,
                                          answers=a.answers or "", created_at=a.created_at, decided_at=a.decided_at)
            db.session.add(m)
        elif a.status == "aceptada" or (a.status == "pendiente" and m.status == "rechazada"):
            m.status = a.status
        if a.status == "aceptada" and not a.bio_verified:
            a.bio_verified, a.bio_status = True, "Verificada"     # ya la había aceptado el equipo a mano
    db.session.commit()
    log.info("Solicitudes creadas a partir de cuentas antiguas: %s", len(seen))


with app.app_context():
    ensure_schema()
    migrate_money_columns()
    migrate_members()


def rename_beginner_course():
    """Una sola vez: el curso de cómo clipear pasa a llamarse «Guía para principiantes»."""
    try:
        if get_setting("beginner_renamed") or beginner_course():
            return
        c = (Course.query.filter(db.func.lower(Course.title).like("%clip%"))
             .order_by(Course.published.desc(), Course.position, Course.id).first())
        if c:
            log.info("Curso «%s» renombrado a «Guía para principiantes»", c.title)
            c.title = "Guía para principiantes"
        set_setting("beginner_renamed", "1")
        db.session.commit()
    except Exception:  # noqa: BLE001
        db.session.rollback()
        log.exception("No se pudo renombrar el curso para principiantes")


# ---------------------------------------------------------------- utilidades generales
def audit(action, detail=""):
    db.session.add(AuditLog(admin_id=current_user.id if getattr(current_user, "is_authenticated", False) else None,
                            action=action, detail=str(detail)[:500]))


def set_setting(key, value):
    db.session.merge(Setting(key=key, value=value))
    db.session.commit()


def get_setting(key, default=""):
    row = db.session.get(Setting, key)
    return row.value if row else default


_THOUSANDS_DOT = re.compile(r"^-?[1-9]\d{0,2}(\.\d{3})+$")


def parse_dt(value):
    """Fecha y hora de un <input type=datetime-local> (se guarda tal cual, como UTC). Vacío = None."""
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value[:16], fmt)
        except ValueError:
            pass
    return None


def parse_float(value, default=None):
    """Lee números como los escribe alguien de España o Latinoamérica:
    5000 · 5.000 · 5.000,50 · 1,5 · 1.5 · 1.500.000 · 5000 $  →  el número correcto.
    Con punto y coma a la vez, el último es el decimal. Un punto seguido de grupos de 3 cifras es de miles."""
    if value is None:
        return default
    t = str(value).strip().replace(" ", "").replace("\u00a0", "").replace("$", "").replace("€", "")
    if not t:
        return default
    if "," in t and "." in t:
        dec = "," if t.rfind(",") > t.rfind(".") else "."
        t = t.replace("." if dec == "," else ",", "").replace(dec, ".")
    elif "," in t:
        t = t.replace(",", ".") if t.count(",") == 1 else t.replace(",", "")
    elif _THOUSANDS_DOT.match(t):
        t = t.replace(".", "")
    try:
        v = float(t)
        return v if v == v and abs(v) < 1e12 else default   # descarta NaN y cifras absurdas
    except (TypeError, ValueError):
        return default


def csv_safe(v):
    """Evita que Excel o Google Sheets ejecuten como fórmula un texto que empiece por = + - @."""
    v = "" if v is None else str(v)
    return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


def lock_row(model, row_id):
    """Bloquea la fila hasta el final de la transacción (SELECT … FOR UPDATE) y la relee de la base de datos.
    Así dos procesos o servidores no pueden tocar a la vez el mismo saldo o presupuesto. En SQLite no hace falta."""
    if db.engine.dialect.name != "postgresql":
        return db.session.get(model, row_id)
    return db.session.query(model).filter_by(id=row_id).with_for_update().populate_existing().one()


class GlobalLock:
    """Candado compartido entre procesos (pg_advisory_lock) para tareas que no deben correr dos veces a la vez."""
    def __init__(self, key):
        self.key, self.conn, self.local = key, None, threading.Lock()

    def acquire(self):
        if not self.local.acquire(blocking=False):
            return False
        if db.engine.dialect.name == "postgresql":
            self.conn = db.engine.connect()
            if not self.conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": self.key}).scalar():
                self.conn.close()
                self.conn = None
                self.local.release()
                return False
        return True

    def release(self):
        if self.conn is not None:
            try:
                self.conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": self.key})
            finally:
                self.conn.close()
                self.conn = None
        self.local.release()


def cents(v):
    return round(float(v or 0) + 1e-9, 2)


# ---------------------------------------------------------------- dinero (libro de cuentas)
def raw_earning(sub, camp):
    """Lo que valdría el clip por sus vistas, según el pago de su red, con mínimo y tope por clip aplicados."""
    amount = (sub.views or 0) / 1_000_000 * camp.rate_for(sub.platform)
    low = camp.min_for(sub.platform)
    if low and amount + 1e-9 < low:
        return 0.0
    top = camp.max_for(sub.platform)
    if top:
        amount = min(amount, top)
    return cents(amount)


def reached_min(sub, camp=None):
    """¿El clip ya vale el pago mínimo de su red?"""
    camp = camp or sub.campaign
    low = camp.min_for(sub.platform)
    return not low or (sub.views or 0) / 1_000_000 * camp.rate_for(sub.platform) + 1e-9 >= low


def stats_on(camp):
    """La prueba de estadísticas es obligatoria en todas las campañas (sin Bunny no se puede subir, así que no se pide)."""
    return bunny_ready()


def needs_stats(sub):
    """El clip llegó al mínimo, la campaña pide estadísticas y el clipper aún no ha enviado el vídeo."""
    return (sub.status == "pendiente" and stats_on(sub.campaign) and sub.stats_status != "enviado"
            and reached_min(sub, sub.campaign))


def stats_due(sub):
    start = sub.stats_asked_at or datetime.utcnow()
    return start + timedelta(hours=STATS_HOURS)


def clip_stage(sub):
    """En qué punto está un clip: contando (aún no llega al mínimo), estadisticas (falta el vídeo), revisar, aprobado o rechazado."""
    if sub.status != "pendiente":
        return sub.status
    camp = sub.campaign
    if not reached_min(sub, camp):
        return "contando"
    return "estadisticas" if needs_stats(sub) else "revisar"


STAGE_LABELS = {"contando": "Contando vistas", "estadisticas": "Falta verificar vistas", "revisar": "En revisión",
                "aprobado": "Aprobado", "rechazado": "Rechazado"}


def earn_window_closed(sub, now=None):
    """(Opción quitada: los clips suman vistas hasta que termina la campaña.)"""
    return False


def join_date(user_id, camp):
    m = campaign_member(user_id, camp.id)
    return m.created_at if m else camp.created_at


def reject_text(code, message=""):
    label = REJECT_REASONS.get(code) or SYSTEM_REASONS.get(code) or ""
    message = (message or "").strip()
    return (f"{label}: {message}" if label and message and code != "otro" else message or label)[:255]


def approve_sub(sub, by="admin"):
    sub.status, sub.reject_reason, sub.reject_code, sub.reviewed_at = "aprobado", "", "", datetime.utcnow()
    added = accrue(sub)
    audit("clip_aprobado" if by == "admin" else "clip_aprobado_auto",
          f"#{sub.id} de {sub.user.email} en «{sub.campaign.song}» · {money(added)} asignados")
    notify_user(sub.user_id, "clip_aprobado", f"Tu clip de {sub.campaign.song} está aprobado",
                f"Lleva {num(sub.views or 0)} vistas y {money(sub.earned or 0)} ganados. Sigue sumando mientras crezca.",
                "/mis-clips?tab=aprobado", camp_id=sub.campaign_id)
    return added


def reject_sub(sub, code, message="", by="admin"):
    removed = clawback(sub) if sub.status == "aprobado" else 0
    sub.status, sub.reject_code, sub.reviewed_at = "rechazado", code, datetime.utcnow()
    sub.reject_reason = reject_text(code, message)
    audit("clip_rechazado" if by == "admin" else "clip_rechazado_auto",
          f"#{sub.id} de {sub.user.email} · motivo: {sub.reject_reason}" + (f" · {money(removed)} retirados" if removed else ""))
    notify_user(sub.user_id, "clip_rechazado", f"Clip rechazado en {sub.campaign.song}",
                f"{sub.reject_reason}", "/mis-clips?tab=rechazado", camp_id=sub.campaign_id)
    return removed


def auto_review(sub, now=None):
    """Marca con un aviso los vídeos publicados antes de unirse a la campaña (decide el admin) y gestiona el vídeo
    de estadísticas: lo pide al llegar al mínimo y rechaza el clip si no llega a tiempo. Devuelve "" o "rechazado"."""
    if sub.status != "pendiente":
        return ""
    now = now or datetime.utcnow()
    if needs_stats(sub):
        if not sub.stats_status or not sub.stats_asked_at:
            sub.stats_status, sub.stats_asked_at = "pedido", now
            notify_user(sub.user_id, "estadisticas", f"Verifica las vistas de tu clip · {sub.campaign.song}",
                        f"Tu clip llegó al mínimo. Tienes {STATS_HOURS} h para subir la grabación de sus estadísticas en «Verificar vistas».",
                        "/mis-clips?tab=estadisticas", camp_id=sub.campaign_id)
        elif now > stats_due(sub) + timedelta(hours=1 if sub.stats_status == "subiendo" else 0):
            reject_sub(sub, "sin_estadisticas", by="auto")
            return "rechazado"
    if sub.posted_at and sub.posted_at < join_date(sub.user_id, sub.campaign) - timedelta(hours=1):
        sub.add_flag("publicado_antes")
    return ""


def finalize_if_exhausted(camp):
    if camp.exhausted and camp.status == "activa":
        close_campaign(camp)
        log.info("Campaña %s agotada y finalizada", camp.id)


def close_campaign(camp, sender_id=None):
    """Cierra la campaña y avisa por el chat a cada clipper de lo que ya puede retirar."""
    camp.status, camp.ended_at = "finalizada", datetime.utcnow()
    totals = {}
    for s in camp.submissions:
        if s.status == "aprobado" and (s.earned or 0) > 0:
            totals[s.user_id] = totals.get(s.user_id, 0) + s.earned
    for uid, amount in totals.items():
        db.session.add(Message(user_id=uid, from_admin=True, sender_id=sender_id, read_by_admin=True, read_by_user=False,
                               body=f"La campaña «{camp.song}» ha terminado. Ya puedes retirar lo que ganaste en ella: "
                                    f"{money(amount)}. Ve a Ganancias y pulsa «Retirar»."))


def accrue(sub, allow_decrease=False):
    """Asigna al clip el dinero que le corresponde, sin pasarse del presupuesto de la campaña.

    Las ganancias de un clip solo suben automáticamente. Solo una corrección manual del admin
    (allow_decrease=True) puede bajarlas.
    """
    if sub.status != "aprobado":
        return 0.0
    camp = lock_row(Campaign, sub.campaign_id)     # nadie más toca este presupuesto hasta guardar
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
        camp = lock_row(Campaign, sub.campaign_id)
        camp.spent = cents(max((camp.spent or 0) - amount, 0))
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


METRICS = ("views", "likes", "comments", "shares")


def campaign_analytics(camp, max_days=180):
    """Estadísticas de una campaña para el panel: totales, por red y la evolución día a día de cada métrica.
    Los días sin lectura arrastran la última conocida. Si un clip no tiene historial de me gusta / comentarios /
    compartidos (se empezaron a guardar después), se estiman en proporción a sus vistas de ese momento."""
    subs = [s for s in camp.submissions if s.status == "aprobado"]
    totals = {m: sum(getattr(s, m) or 0 for s in subs) for m in METRICS}
    spent = cents(camp.spent)
    by_platform = [{"key": p, "name": PLATFORMS[p],
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
    for sn in (ViewSnapshot.query.filter(ViewSnapshot.submission_id.in_([s.id for s in subs]))
               .order_by(ViewSnapshot.taken_at).all()):
        snaps.setdefault(sn.submission_id, []).append(sn)
    today = datetime.utcnow().date()
    start = max(min(s.created_at.date() for s in subs), today - timedelta(days=max_days - 1))
    days = [start + timedelta(days=i) for i in range((today - start).days + 1)]
    cum = {m: [0] * len(days) for m in METRICS}
    for s in subs:
        cur_views = s.views or 0
        ratio = {m: (getattr(s, m) or 0) / cur_views if cur_views else 0 for m in METRICS[1:]}
        points = [(s.created_at, {m: 0 for m in METRICS})]
        for sn in snaps.get(s.id, []):
            vals = {"views": sn.views or 0}
            for m in METRICS[1:]:
                v = getattr(sn, m)
                vals[m] = v if v is not None else round(vals["views"] * ratio[m])
            points.append((sn.taken_at, vals))
        points.append((datetime.utcnow(), {m: getattr(s, m) or 0 for m in METRICS}))   # la lectura de ahora
        j, last = 0, points[0][1]
        for i, d in enumerate(days):
            end = datetime.combine(d, datetime.max.time())
            while j < len(points) and points[j][0] <= end:
                last = points[j][1]
                j += 1
            for m in METRICS:
                cum[m][i] += last[m]
    daily = {m: [max(0, cum[m][i] - (cum[m][i - 1] if i else 0)) for i in range(len(days))] for m in METRICS}
    out["series"] = {"labels": [d.isoformat() for d in days], "cumulative": cum, "daily": daily}
    return out


CLIP_RANGES = {7: "Últimos 7 días", 30: "Últimos 30 días", 90: "Últimos 90 días", 0: "Todo"}
CLIP_METRICS = ("views", "likes", "comments", "shares", "earned", "sent", "approved")


def clipper_analytics(subs, days=30):
    """Panel de Mis clips: totales, evolución día a día de cada métrica y vistas por red de los clips del periodo."""
    today = datetime.utcnow().date()
    if days:
        start = today - timedelta(days=days - 1)
    else:
        start = max(min((s.created_at.date() for s in subs), default=today), today - timedelta(days=364))
    subs = [s for s in subs if s.created_at.date() >= start]
    live = [s for s in subs if s.status != "rechazado"]
    approved = [s for s in subs if s.status == "aprobado"]
    totals = {m: sum(getattr(s, m) or 0 for s in live) for m in METRICS}
    totals.update({"earned": cents(sum(s.earned or 0 for s in approved)), "sent": len(subs), "approved": len(approved)})
    by_platform = sorted(({"key": p, "name": PLATFORMS[p], "posts": sum(1 for s in subs if s.platform == p),
                           "views": sum(s.views or 0 for s in live if s.platform == p)} for p in PLATFORMS
                          if any(s.platform == p for s in subs)), key=lambda r: -r["views"])
    dates = [start + timedelta(days=i) for i in range((today - start).days + 1)]
    cum = {m: [0] * len(dates) for m in CLIP_METRICS}
    idx = {d: i for i, d in enumerate(dates)}
    snaps = {}
    if live:
        for sn in (ViewSnapshot.query.filter(ViewSnapshot.submission_id.in_([s.id for s in live]))
                   .order_by(ViewSnapshot.taken_at).all()):
            snaps.setdefault(sn.submission_id, []).append(sn)
    now = datetime.utcnow()
    for s in live:                                      # vistas, me gusta…: la última lectura de cada día (se arrastra)
        cur_views = s.views or 0
        ratio = {m: (getattr(s, m) or 0) / cur_views if cur_views else 0 for m in METRICS[1:]}
        points = [(s.created_at, {m: 0 for m in METRICS})]
        for sn in snaps.get(s.id, []):
            vals = {"views": sn.views or 0}
            for m in METRICS[1:]:
                v = getattr(sn, m)
                vals[m] = v if v is not None else round(vals["views"] * ratio[m])
            points.append((sn.taken_at, vals))
        points.append((now, {m: getattr(s, m) or 0 for m in METRICS}))
        j, last = 0, points[0][1]
        for i, d in enumerate(dates):
            end = datetime.combine(d, datetime.max.time())
            while j < len(points) and points[j][0] <= end:
                last = points[j][1]
                j += 1
            for m in METRICS:
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
    daily = {m: [max(0, round(cum[m][i] - (cum[m][i - 1] if i else 0), 2)) for i in range(len(dates))] for m in CLIP_METRICS}
    return {"totals": totals, "by_platform": by_platform,
            "series": {"labels": [d.isoformat() for d in dates], "cumulative": cum, "daily": daily}}


def user_money(user):
    """Disponible = ganado en clips aprobados de campañas ya cerradas − retirado.
    En curso = ganado en campañas que siguen abiertas (se libera al cerrarse). Pendiente = clips en revisión."""
    earned_rows, live_rows, pending_rows, stats = [], [], [], {}
    for s in user.submissions:
        if s.status == "aprobado":
            (earned_rows if s.campaign.status == "finalizada" else live_rows).append((s, cents(s.earned)))
        elif s.status == "pendiente":
            st = stats.setdefault(s.campaign_id, campaign_stats(s.campaign))
            pending_rows.append((s, st["estimates"].get(s.id, 0)))
    closed = cents(sum(a for _, a in earned_rows))
    live = cents(sum(a for _, a in live_rows))
    earned = cents(closed + live)
    withdrawn = cents(sum(p.amount for p in user.payouts if p.status in PAYOUT_OPEN + ("pagado",)))
    paid = cents(sum(p.amount for p in user.payouts if p.status == "pagado"))
    balance = cents(closed - withdrawn)
    # Saldo por creador: cada creador paga desde su Whop lo que se ganó en SUS campañas
    by_creator = {}
    for s_, a in earned_rows:
        k = s_.campaign.creator_id if CREATORS_ENABLED else None
        by_creator[k] = by_creator.get(k, 0) + a
    for p in user.payouts:
        if p.status in PAYOUT_OPEN + ("pagado",):
            k = p.creator_id if CREATORS_ENABLED else None
            by_creator[k] = by_creator.get(k, 0) - p.amount
    by_creator = {k: cents(v) for k, v in by_creator.items() if cents(v) >= CENT}
    return {
        "by_creator": by_creator,
        "earned": earned,
        "live": live,
        "pending": cents(sum(a for _, a in pending_rows)),
        "available": max(balance, 0.0),
        "debt": max(-balance, 0.0),
        "paid": paid,
        "open_payout": next((p for p in user.payouts if p.status in PAYOUT_OPEN), None),
        "earned_rows": sorted(earned_rows, key=lambda r: r[0].created_at, reverse=True),
        "live_rows": sorted(live_rows, key=lambda r: r[0].created_at, reverse=True),
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
            "youtube": f"https://www.youtube.com/@{handle}", "facebook": f"https://www.facebook.com/{handle}"}[platform]


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
_refresh_lock = GlobalLock(731902)       # un solo recuento de vistas a la vez, aunque haya varios procesos


DEMO_DOMAIN = "@demo.clipprofit.app"     # los clippers de demostración tienen este correo (no existe)


def is_demo(user):
    return bool(user and (user.email or "").endswith(DEMO_DOMAIN))


def is_fake(obj):
    """Clip o cuenta inventados: de un clipper de demostración o de la demo personal del admin. Nunca se leen en Apify."""
    return bool(getattr(obj, "my_demo", False)) or is_demo(getattr(obj, "user", None))


def apply_reading(sub, info, now):
    """Guarda una lectura de Apify en el clip: vistas, likes, autor, historial y avisos."""
    views = info["views"] if isinstance(info, dict) else int(info)
    likes = info.get("likes") if isinstance(info, dict) else None
    author = info.get("author") if isinstance(info, dict) else ""
    comments = info.get("comments") if isinstance(info, dict) else None
    shares = info.get("shares") if isinstance(info, dict) else None
    posted = info.get("posted_at") if isinstance(info, dict) else None
    if isinstance(info, dict) and info.get("video_url"):
        sub.media_url = str(info["video_url"])[:1000]
    if isinstance(info, dict) and info.get("thumb"):
        sub.thumb_url = str(info["thumb"])[:1000]
    if isinstance(info, dict) and info.get("caption") and not sub.caption:
        sub.caption = str(info["caption"])[:300]
    sub.checked_at = now
    sub.views_error = ""
    if posted and not sub.posted_at:
        sub.posted_at = posted
    if author:
        sub.author = author
    engaged = any(v is not None and v != getattr(sub, k) for k, v in (("likes", likes), ("comments", comments), ("shares", shares)))
    if likes is not None:
        sub.likes = likes
    if comments is not None:
        sub.comments = comments
    if shares is not None:
        sub.shares = shares
    check_author(sub)
    if views > (sub.views or 0):
        check_growth(sub, views, likes)
        sub.views = views
        sub.views_updated_at = now
    elif likes is not None:
        check_growth(sub, sub.views or 0, likes)
    if views > 0 and (sub.views_updated_at == now or engaged):     # historial para las gráficas
        db.session.add(ViewSnapshot(submission_id=sub.id, views=sub.views or 0, likes=sub.likes,
                                    comments=sub.comments, shares=sub.shares, taken_at=now))


def refresh_views():
    """Pide a Apify las vistas de los clips recientes de campañas activas y reparte el dinero."""
    if not _refresh_lock.acquire():
        return "Ya hay una actualización en marcha."
    try:
        try:                                      # primero, los clips nuevos que haya en los perfiles de los clippers
            discover_clips()
        except Exception:  # noqa: BLE001
            db.session.rollback()
            log.exception("Fallo buscando clips nuevos en los perfiles")
        q = (Submission.query.join(Campaign)
             .filter(Campaign.status == "activa", Submission.status != "rechazado"))
        if REFRESH_DAYS > 0:
            q = q.filter(Submission.created_at >= datetime.utcnow() - timedelta(days=REFRESH_DAYS))
        subs = q.order_by(Submission.created_at).all()
        apply_schedule()
        now0 = datetime.utcnow()
        subs = [s for s in subs if not s.campaign.exhausted and not earn_window_closed(s, now0) and not is_fake(s)]
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
        auto = {"aprobado": 0, "rechazado": 0}
        pend = (Submission.query.join(Campaign)
                .filter(Campaign.status == "activa", Submission.status == "pendiente").all())
        for s in pend:                                   # reglas automáticas (también caduca lo que no llegó al mínimo)
            if not s.review_since and reached_min(s):
                s.review_since = datetime.utcnow()
            if is_fake(s):
                continue
            r = auto_review(s)
            if r:
                auto[r] += 1
        for s in sorted((s for s in subs if s.status == "aprobado"), key=lambda s: s.created_at):
            assigned += accrue(s)
        db.session.commit()
        budget_alerts()
        try:
            copy_review_videos()
        except Exception:  # noqa: BLE001
            log.exception("Fallo copiando vídeos a Bunny")
        msg = f"Vistas leídas en {read} de {len(subs)} clips · {money(assigned)} repartidos."
        if auto["aprobado"] or auto["rechazado"]:
            msg += f" Automático: {auto['aprobado']} aprobados, {auto['rechazado']} rechazados."
        if errors:
            msg += " Errores: " + " | ".join(errors)
        log.info(msg)
        set_setting("last_refresh", datetime.utcnow().strftime("%d/%m/%Y %H:%M") + " UTC · " + msg)
        set_setting("last_refresh_at", datetime.utcnow().isoformat())   # el reloj interno cuenta desde aquí
        return msg
    except Exception:  # noqa: BLE001
        db.session.rollback()
        log.exception("Fallo actualizando vistas")
        set_setting("last_refresh", datetime.utcnow().strftime("%d/%m/%Y %H:%M") + " UTC · Error inesperado, revisa los logs de Railway.")
        raise
    finally:
        _refresh_lock.release()


def apply_schedule():
    """Cierra las campañas cuya fecha de fin ya pasó."""
    now = datetime.utcnow()
    closed = 0
    for c in []:                                       # (opción de fecha de fin quitada)
        close_campaign(c)
        audit("campaña_cerrada_fecha", f"#{c.id} {c.song}")
        closed += 1
    if closed:
        db.session.commit()
    return closed


def budget_alerts():
    """Aviso al móvil cuando a una campaña le queda el 20 % del presupuesto (una vez)."""
    for c in Campaign.query.filter_by(status="activa", low_alert_sent=False).all():
        if c.low_budget:
            c.low_alert_sent = True
            db.session.commit()
            notify_phone(f"💸 A «{c.song}» le queda poco presupuesto",
                         f"Quedan {money(c.remaining)} de {money(c.budget)}. Añade más si quieres que siga.",
                         url_for("admin_campaign", cid=c.id) if has_request_context() else "", tags="money_with_wings")


VIDEO_KEEP_DAYS = int(os.environ.get("VIDEO_KEEP_DAYS", "7"))   # días que se guarda la copia tras revisar el clip


def copy_review_videos(limit=30):
    """Copia a Bunny el vídeo de los clips que esperan revisión, para verlos en el panel aunque la red falle.
    Y borra las copias de los clips revisados hace más de VIDEO_KEEP_DAYS días."""
    if not bunny_ready():
        return 0
    now = datetime.utcnow()
    old = (Submission.query.filter(Submission.video_guid != "", Submission.status != "pendiente",
                                   Submission.reviewed_at.isnot(None),
                                   Submission.reviewed_at < now - timedelta(days=VIDEO_KEEP_DAYS)).limit(200).all())
    for s_ in old:
        try:
            bunny_call("DELETE", f"/videos/{s_.video_guid}")
        except requests.RequestException:
            continue
        s_.video_guid, s_.video_status = "", "borrado"
    old_stats = (Submission.query.join(Campaign).filter(Submission.stats_guid != "", Campaign.status == "finalizada",
                                                       Campaign.ended_at.isnot(None),
                                                       Campaign.ended_at < now - timedelta(days=90)).limit(200).all())
    for s_ in old_stats:                        # vídeos de estadísticas: se borran a los 90 días de cerrar la campaña
        try:
            bunny_call("DELETE", f"/videos/{s_.stats_guid}")
        except requests.RequestException:
            continue
        s_.stats_guid = ""
    db.session.commit()
    todo = (Submission.query.filter(Submission.status == "pendiente", Submission.review_since.isnot(None),
                                    Submission.video_guid == "", Submission.video_tries < 3,
                                    Submission.platform.in_(("tiktok", "instagram")))
            .order_by(Submission.review_since).limit(limit).all())
    todo = [s_ for s_ in todo if not is_fake(s_)]
    tiktok = [s_ for s_ in todo if s_.platform == "tiktok" and not s_.media_url]
    if tiktok:
        try:
            links = viewcounter.fetch_download_urls("tiktok", [s_.url for s_ in tiktok])
        except Exception as e:  # noqa: BLE001
            log.warning("Apify no dio los vídeos de TikTok: %s", e)
            links = {}
        for s_ in tiktok:
            s_.media_url = (links.get(s_.video_key.split(":", 1)[1]) or "")[:1000]
    done = 0
    for s_ in todo:
        s_.video_tries = (s_.video_tries or 0) + 1
        if not s_.media_url:
            continue
        try:
            r = requests.get(s_.media_url, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code >= 400 or len(r.content) < 10_000 or len(r.content) > 300 * 1024 * 1024:
                raise RuntimeError(f"descarga {r.status_code} · {len(r.content)} bytes")
            ok, code, data = bunny_call("POST", "/videos", json={"title": f"Clip {s_.id} · {s_.campaign.song}"[:200]})
            if not ok or not data.get("guid"):
                raise RuntimeError(f"Bunny {code}")
            guid = data["guid"]
            up = requests.put(f"https://video.bunnycdn.com/library/{BUNNY_LIBRARY_ID}/videos/{guid}", data=r.content,
                              headers={"AccessKey": BUNNY_API_KEY, "Content-Type": "application/octet-stream"}, timeout=180)
            if up.status_code >= 300:
                raise RuntimeError(f"subida {up.status_code}")
            s_.video_guid, s_.video_status = guid, "subiendo"
            done += 1
        except Exception as e:  # noqa: BLE001
            log.warning("No se pudo copiar el vídeo del clip %s: %s", s_.id, e)
            s_.video_status = "error"
        db.session.commit()
    for s_ in Submission.query.filter(Submission.video_status == "subiendo").limit(100).all():
        try:
            ok, _, data = bunny_call("GET", f"/videos/{s_.video_guid}")
            if ok:
                s_.video_status = {4: "listo", 5: "error", 6: "error"}.get(data.get("status"), "subiendo")
        except requests.RequestException:
            pass
    db.session.commit()
    return done


def clip_player(sub):
    """Reproductor del clip en la revisión: la copia propia (Bunny) si ya está lista; si no, el de la red."""
    if sub.video_guid and sub.video_status == "listo" and BUNNY_LIBRARY_ID:
        return f"https://iframe.mediadelivery.net/embed/{BUNNY_LIBRARY_ID}/{sub.video_guid}?autoplay=false&preload=true&responsive=true", True
    return viewcounter.embed_url(sub.platform, sub.url), False


def refresh_in_background():
    def run():
        with app.app_context():
            try:
                refresh_views()
            except Exception:  # noqa: BLE001
                pass
    threading.Thread(target=run, daemon=True).start()


AUTO_PLATFORMS = ("tiktok", "instagram", "youtube")
SCAN_CHUNK = 20                         # cuentas por llamada a Apify
APP_TZ = os.environ.get("APP_TZ", "Europe/Madrid")
REMIND_HOUR = int(os.environ.get("REMIND_HOUR", "3"))       # hora (de España) del aviso «no has publicado en 24 h»


def discover_clips(now=None):
    """Mira el perfil de cada cuenta aceptada en una campaña activa y añade solos los vídeos nuevos.
    Cada cuenta es de una sola campaña, así que todo lo que publica después de entrar es un clip de esa campaña."""
    now = now or datetime.utcnow()
    accs = (CampaignAccount.query.join(Campaign)
            .filter(CampaignAccount.status == "aceptada", Campaign.status == "activa",
                    CampaignAccount.platform.in_(AUTO_PLATFORMS)).all())
    accs = [a for a in accs if not a.campaign.exhausted and a.platform in a.campaign.platform_list and not is_fake(a)]
    added, errors = 0, []
    by_platform = {}
    for a in accs:
        by_platform.setdefault(a.platform, []).append(a)
    for platform, group in by_platform.items():
        for i in range(0, len(group), SCAN_CHUNK):
            chunk = group[i:i + SCAN_CHUNK]
            try:
                items = viewcounter.fetch_profile_videos(platform, [a.handle for a in chunk])
            except Exception as e:  # noqa: BLE001
                errors.append(f"{PLATFORMS[platform]}: {e}")
                log.warning("No se pudieron leer perfiles de %s: %s", platform, e)
                continue
            by_handle = {viewcounter.clean_handle(a.handle): a for a in chunk}
            for a in chunk:
                a.last_scan_at = now
            for it in items:
                acc = by_handle.get(viewcounter.clean_handle(it.get("author") or ""))
                if not acc and len(chunk) == 1:
                    acc = chunk[0]
                if not acc:
                    continue
                posted = it.get("posted_at")
                if posted and (not acc.last_post_at or posted > acc.last_post_at):
                    acc.last_post_at = posted
                since = (acc.decided_at or acc.created_at or now) - timedelta(hours=1)
                if not posted or posted < since:
                    continue                                  # solo cuentan los vídeos publicados después de entrar
                key = f"{platform}:{it['vid']}"
                if Submission.query.filter_by(video_key=key).first() or IgnoredClip.query.filter_by(video_key=key).first():
                    continue                                  # ya está, o el clipper lo borró a propósito
                camp = acc.campaign
                sub = Submission(user_id=acc.user_id, campaign_id=camp.id, url=viewcounter.normalize_url(it["url"], resolve=False),
                                 platform=platform, video_key=key, author=acc.handle, source="auto", posted_at=posted,
                                 review_since=None if camp.min_for(platform) else now)
                db.session.add(sub)
                db.session.flush()
                if it.get("views") is not None:
                    apply_reading(sub, it, now)
                notify_user(acc.user_id, "clip_nuevo", f"Clip detectado en {camp.song}",
                            f"Hemos añadido solo tu nuevo vídeo de @{acc.handle}. Ya está contando vistas.",
                            "/mis-clips", camp_id=camp.id)
                added += 1
            db.session.commit()
    if added:
        log.info("Clips detectados solos: %s", added)
    return added, errors


def inactivity_reminders(now=None):
    """Aviso diario: cuentas de campañas activas que no han publicado nada en las últimas 24 h."""
    now = now or datetime.utcnow()
    accs = (CampaignAccount.query.join(Campaign)
            .filter(CampaignAccount.status == "aceptada", Campaign.status == "activa").all())
    n = 0
    for a in accs:
        camp = a.campaign
        if camp.exhausted or is_fake(a) or (a.user and a.user.is_banned):
            continue
        if (a.decided_at or a.created_at or now) > now - timedelta(hours=12):
            continue                                          # recién aceptada: aún no toca
        if a.platform not in AUTO_PLATFORMS or not a.last_scan_at or a.last_scan_at < now - timedelta(hours=26):
            continue                                          # si no hemos podido mirar su perfil, no avisamos a ciegas
        if a.last_post_at and a.last_post_at > now - timedelta(hours=24):
            continue
        st = campaign_stats(camp)
        notify_user(a.user_id, "inactividad", f"@{a.handle} no ha subido ningún clip en 24 h",
                    f"Quedan {money(st['remaining'], 0)} por repartir en {camp.song}. Sube un clip para no quedarte fuera.",
                    f"/campana/{camp.id}", camp_id=camp.id)
        n += 1
    db.session.commit()
    return n


def _local(now=None):
    return (now or datetime.utcnow()).replace(tzinfo=timezone.utc).astimezone(ZoneInfo(APP_TZ))


def reminders_due(now=None):
    """¿Ya pasó la hora del aviso diario (justo después de la lectura de las 3:00 en España) y aún no se ha mandado hoy?"""
    local = _local(now)
    return local.hour >= REMIND_HOUR and get_setting("reminders_sent", "") != local.strftime("%Y-%m-%d"), local.strftime("%Y-%m-%d")


REFRESH_HOURS = parse_float(os.environ.get("REFRESH_HOURS"), 6) or 0   # solo si REFRESH_AT está vacío: cada cuántas horas
REFRESH_AT = (os.environ.get("REFRESH_AT", "03:00") or "").strip()      # hora fija (España) de la lectura diaria: clips nuevos + vistas


def _refresh_minute():
    try:
        h, m = REFRESH_AT.split(":")
        return int(h) * 60 + int(m)
    except ValueError:
        return 180


def next_scan_utc(now=None):
    """La próxima lectura diaria, en UTC (la web la enseña en la hora de cada clipper). Tiene en cuenta el cambio de hora."""
    if not REFRESH_AT:
        return None
    local = _local(now)
    mins = _refresh_minute()
    day = local.date() if local.hour * 60 + local.minute < mins else local.date() + timedelta(days=1)
    target = datetime(day.year, day.month, day.day, mins // 60, mins % 60, tzinfo=ZoneInfo(APP_TZ))
    return target.astimezone(timezone.utc).replace(tzinfo=None)


def _refresh_due(now=None):
    """¿Toca leer los perfiles y las vistas? Una vez al día a la hora fija (REFRESH_AT, en España),
    o cada REFRESH_HOURS horas si REFRESH_AT está vacío."""
    if not viewcounter.APIFY_TOKEN:
        return False
    if REFRESH_AT:
        local = _local(now)
        return (local.hour * 60 + local.minute >= _refresh_minute()
                and get_setting("refresh_day", "") != local.strftime("%Y-%m-%d"))
    if REFRESH_HOURS <= 0:
        return False
    now = now or datetime.utcnow()
    try:
        last = datetime.fromisoformat(get_setting("last_refresh_at", ""))
    except ValueError:
        return True
    return now - last >= timedelta(hours=REFRESH_HOURS)


def check_creator_balances():
    """Una vez al día: si a algún creador no le llega el saldo de su Whop para lo que deben sus campañas, aviso al móvil."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if get_setting("creators_checked", "") == today:
        return
    set_setting("creators_checked", today)
    for r in (creators_overview() if CREATORS_ENABLED else []):
        if r["missing"]:
            notify_phone(f"⚠️ A {r['c'].name} le falta saldo en Whop",
                         f"Debe {money(r['owed'])} a clippers y tiene {money(r['balance'])}. Faltan {money(r['missing'])}.", tags="warning")


def _auto_refresh_loop():
    """Reloj interno: cada 10 minutos mira si toca la lectura diaria (o la de cada REFRESH_HOURS horas)."""
    time.sleep(90)                          # deja arrancar la web primero
    while True:
        try:
            with app.app_context():
                fresh = False
                if _refresh_due():
                    set_setting("last_refresh_at", datetime.utcnow().isoformat())
                    set_setting("refresh_day", _local().strftime("%Y-%m-%d"))
                    refresh_views()                       # primero busca los clips nuevos en los perfiles, luego las vistas
                    check_creator_balances()
                    fresh = True
                due, day = reminders_due()
                if due:                                   # justo después de la lectura: aviso a quien no publicó en 24 h
                    set_setting("reminders_sent", day)
                    db.session.commit()
                    if not fresh and not REFRESH_AT:
                        discover_clips()
                    inactivity_reminders()
        except Exception:  # noqa: BLE001
            log.exception("Fallo en la actualización automática de vistas")
        time.sleep(600)


if PRODUCTION and (REFRESH_AT or REFRESH_HOURS > 0) and not os.environ.get("PYTEST_CURRENT_TEST"):
    threading.Thread(target=_auto_refresh_loop, daemon=True, name="auto-refresh").start()


# ---------------------------------------------------------------- seguridad
def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(16)
    return session["_csrf"]


@app.before_request
def _start_timer():
    g._t0 = time.perf_counter()


SEEN_EVERY = 120      # cada cuánto se guarda la última visita de un usuario (segundos)
ONLINE_FOR = 300      # si entró hace menos de esto, sale «En línea»


@app.before_request
def before():
    if request.method == "POST":
        sent, expected = request.form.get("_csrf", ""), session.get("_csrf", "")
        if not sent or not expected or not secrets.compare_digest(sent, expected):
            abort(400, description="csrf")
    if current_user.is_authenticated:
        now = datetime.utcnow()
        if not current_user.last_seen_at or (now - current_user.last_seen_at).total_seconds() > SEEN_EVERY:
            current_user.last_seen_at = now
            db.session.commit()
        if current_user.is_banned and request.endpoint not in ("logout", "static", "terms", "privacy", "health",
                                                               "support", "support_poll", "notices"):
            return render_template("banned.html"), 403
    return None


CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "font-src 'self'; img-src 'self' data: https:; connect-src 'self' https://video.bunnycdn.com; "
       "frame-src https://www.tiktok.com https://www.instagram.com https://www.youtube-nocookie.com "
       "https://iframe.mediadelivery.net https://player.vimeo.com https://www.facebook.com; "
       "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    resp.headers.setdefault("Content-Security-Policy", CSP)
    # Con Turbo, un formulario que responde con una página (sin redirigir) tiene que llevar un código 4xx para que se pinte
    if (request.method == "POST" and resp.status_code == 200 and request.headers.get("X-Turbo-Request-Id")
            and resp.mimetype == "text/html"):
        resp.status_code = 422
    if getattr(g, "_t0", None):     # cuánto tarda el servidor en preparar la página (sin contar internet)
        resp.headers["Server-Timing"] = f"app;dur={(time.perf_counter() - g._t0) * 1000:.0f}"
    if PRODUCTION:
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
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


# Qué puede abrir cada rol. El dueño, todo. Lo que no está en la lista es solo para el dueño (dinero, campañas, ajustes…).
ROLE_ENDPOINTS = {
    "reviewer": {"admin_home", "admin_clips", "admin_requests", "admin_review", "admin_review_bulk", "admin_campaign_member",
                 "admin_campaign_review", "admin_campaign_requests", "admin_campaign_clippers",
                 "admin_campaign_account", "admin_campaign", "admin_campaigns", "admin_clipper"},
    "support": {"admin_home", "admin_inbox", "admin_chat", "admin_chat_poll", "admin_clipper"},
}


TECH_ENDPOINTS = {"admin_settings", "admin_probe", "admin_refresh", "admin_whop_app_test", "admin_vapid_keys", "admin_backup",
                  "admin_demo", "admin_my_demo", "admin_wipe", "admin_activity", "admin_activity_delete", "admin_creators", "admin_creator_form"}


def is_tech(user=None):
    user = user or current_user
    if not getattr(user, "is_admin", False) or (user.admin_role or "owner") != "owner":
        return False
    return (user.email or "").lower() in TECH_EMAILS if TECH_EMAILS else True


def admin_can(endpoint, user=None):
    user = user or current_user
    if not getattr(user, "is_admin", False):
        return False
    if endpoint in ("admin_creators", "admin_creator_form") and not CREATORS_ENABLED:
        return False
    if endpoint in TECH_ENDPOINTS:
        return is_tech(user)
    role = user.admin_role or "owner"
    return role == "owner" or endpoint in ROLE_ENDPOINTS.get(role, set())


def admin_required(f):
    @wraps(f)
    @login_required
    def wrapper(*a, **kw):
        if not current_user.is_admin or not admin_can(request.endpoint):
            abort(403)
        return f(*a, **kw)
    return wrapper


def staff_required(campaign_of):
    """Admin con permiso, o mod de la campaña que toca (campaign_of saca el id de campaña de los argumentos)."""
    def deco(f):
        @wraps(f)
        @login_required
        def wrapper(*a, **kw):
            if current_user.is_admin:
                if not admin_can(request.endpoint):
                    abort(403)
                return f(*a, **kw)
            cid = campaign_of(kw)
            if request.endpoint in MOD_ENDPOINTS and cid and mod_can(current_user, cid, ENDPOINT_PERM.get(request.endpoint, "")):
                g.mod_cid = cid
                return f(*a, **kw)
            abort(403)
        return wrapper
    return deco


def staff_can(endpoint, cid):
    """Para las plantillas: ¿puede el usuario entrar en esta pantalla de la campaña?"""
    if getattr(current_user, "is_admin", False):
        return admin_can(endpoint)
    return endpoint in MOD_ENDPOINTS and mod_can(current_user, cid, ENDPOINT_PERM.get(endpoint, ""))


def acting_as_mod():
    return not getattr(current_user, "is_admin", False)


def _sub_camp(kw):
    s_ = db.session.get(Submission, kw.get("sid") or 0)
    return s_.campaign_id if s_ else None


def _member_camp(kw):
    m_ = db.session.get(CampaignMember, kw.get("mid") or 0)
    return m_.campaign_id if m_ else None


def _account_camp(kw):
    a_ = db.session.get(CampaignAccount, kw.get("aid") or 0)
    return a_.campaign_id if a_ else None


def _bulk_camp(kw):
    """Revisión en bloque: vale si todos los clips marcados son de campañas que modera."""
    ids = [int(x) for x in request.form.getlist("ids") if x.isdigit()][:200]
    cids = {c for (c,) in db.session.query(Submission.campaign_id).filter(Submission.id.in_(ids or [0]))}
    return cids.pop() if len(cids) == 1 else None


# ---------------------------------------------------------------- plantillas
def _cover_meta(data):
    """Color de fondo para la tarjeta destacada y una miniatura difuminada (en texto) a partir de la portada."""
    import base64
    from PIL import Image
    img = Image.open(io.BytesIO(data)).convert("RGB")
    small = img.resize((24, 24))
    r = g = b = n = 0.0
    for pr, pg, pb in small.getdata():
        w = 1 + (max(pr, pg, pb) - min(pr, pg, pb)) / 40      # más peso a los colores vivos que a los grises
        r += pr * w; g += pg * w; b += pb * w; n += w
    r, g, b = r / n, g / n, b / n
    k = 0.42 * 255 / max(r, g, b, 1)
    deep = f"rgb({round(r * k)},{round(g * k)},{round(b * k)})"
    mid = f"rgb({round(r * .62)},{round(g * .62)},{round(b * .62)})"
    tint = f"radial-gradient(120% 140% at 0% 0%, {mid} 0%, {deep} 45%, #0b0b0c 100%)"
    tiny = img.copy()
    tiny.thumbnail((20, 20))
    out = io.BytesIO()
    tiny.save(out, "WEBP", quality=45)
    return tint, "data:image/webp;base64," + base64.b64encode(out.getvalue()).decode()


def cover_meta(camp):
    """(color de la tarjeta, miniatura difuminada). Se calcula una vez por portada y se guarda."""
    if not camp.cover_version:
        return "", ""
    if camp.cover_meta_v != camp.cover_version or not camp.cover_lqip:
        try:
            camp.cover_tint, camp.cover_lqip = _cover_meta(camp.cover_data)
            camp.cover_meta_v = camp.cover_version
            db.session.commit()
        except Exception:  # noqa: BLE001
            log.exception("No se pudo preparar la miniatura de la portada %s", camp.id)
            db.session.rollback()
            return "", ""
    return camp.cover_tint or "", camp.cover_lqip or ""


def cover_src(camp, size=None):
    if camp.cover_version:
        return url_for("cover", cid=camp.id, v=camp.cover_version, t=size if size in COVER_SIZES else None)
    return camp.cover_url or ""


def split_logo(name):
    """Separa el nombre para el logo antiguo (ya no se usa en las plantillas)."""
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
    return {"csrf_token": csrf_token, "admin_can": admin_can, "is_tech": is_tech, "bell_data": bell_data, "wallet_ready": wallet_ready, "ADMIN_ROLES": ADMIN_ROLES, "static_url": static_url, "APP_NAME": APP_NAME, "CUR": CURRENCY, "CUR_CODE": CURRENCY_CODE,
            "PLATFORMS": PLATFORMS, "cover_src": cover_src, "cover_meta": cover_meta, "min_pay": min_pay, "MIN_WITHDRAW": MIN_WITHDRAW,
            "PAYOUT_METHODS": PAYOUT_METHODS, "PAYOUT_AVAILABLE": PAYOUT_AVAILABLE, "USDT_METHOD": USDT_METHOD, "WHOP_METHOD": WHOP_METHOD, "WHOP_AUTO_MAX": WHOP_AUTO_MAX, "whop_enabled": whop_enabled, "account_verified": account_verified, "profile_url": profile_url, "CATEGORIES": CATEGORIES, "CATEGORY_ONE": CATEGORY_ONE,
            "LOGO_MAIN": LOGO_MAIN, "LOGO_SUB": LOGO_SUB, "SUPPORT_EMAIL": SUPPORT_EMAIL,
            "FLAG_LABELS": FLAG_LABELS, "PAYOUT_DAYS": PAYOUT_DAYS, "YEAR": datetime.utcnow().year, "now_utc": datetime.utcnow(),
            "REFRESH_DAYS": REFRESH_DAYS, "REFRESH_HOURS": REFRESH_HOURS, "REFRESH_AT": REFRESH_AT, "next_scan_utc": next_scan_utc, "admin_counts": admin_counts, "unread_messages": unread_messages, "notice_data": notice_data, "new_leads": new_leads,
            "REJECT_REASONS": REJECT_REASONS, "COUNTRIES": COUNTRIES, "LANGUAGES": LANGUAGES, "CAMPAIGN_KINDS": CAMPAIGN_KINDS,
            "clip_stage": clip_stage, "STAGE_LABELS": STAGE_LABELS, "stats_embed": stats_embed, "stats_due": stats_due, "staff_can": staff_can, "perm_ok": perm_ok, "clip_deletable": clip_deletable, "clip_thumb": clip_thumb, "is_mod": is_mod, "mod_can": mod_can, "mod_home": mod_home,
            "mod_campaigns": mod_campaigns, "acting_as_mod": acting_as_mod,
            "STATS_HOURS": STATS_HOURS, "bunny_ready": bunny_ready, "reached_min": reached_min,
            "raw_earning_of": lambda s_: raw_earning(s_, s_.campaign),
            "VAPID_ON": bool(VAPID_PUBLIC_KEY), "WHATSAPP_ON": whatsapp_ready(),
            "WHATSAPP_TEMPLATE": WHATSAPP_TEMPLATE}


app.add_template_filter(lambda t: linkify(t), "linkify")


@app.template_filter("groupby_keep")
def groupby_keep(rows, key):
    """Como groupby pero sin reordenar: los grupos salen en el orden en que aparecen."""
    out = []
    for r in rows:
        k = r.get(key) or ""
        if out and out[-1][0] == k:
            out[-1][1].append(r)
        else:
            out.append((k, [r]))
    return out


@app.template_filter("rate_k")
def rate_k_filter(rate_per_million):
    """Tarifa guardada por 1M de vistas → texto por cada 1.000 vistas ($1.00)."""
    return money((rate_per_million or 0) / 1000)


def min_pay(camp):
    """Lo mínimo que cobra un clip: las vistas mínimas al precio de la campaña."""
    return round((camp.min_views or 0) / 1_000_000 * (camp.rate or 0), 2)


def _es(v, decimals):
    """Número en formato español: 5.000,00"""
    return f"{(v or 0):,.{decimals}f}".replace(",", "\x00").replace(".", ",").replace("\x00", ".")


@app.template_filter("money")
def money(v, decimals=2):
    return f"{_es(v, decimals)} {CURRENCY}"


@app.template_filter("earn")
def earn(v, decimals=2):
    """Dinero ganado: verde si hay algo, gris si es 0. Igual en toda la web."""
    from markupsafe import Markup
    cls = "m-earn" if float(v or 0) > 0 else "m-zero"
    return Markup('<span class="{}">{}</span>').format(cls, money(v, decimals))


@app.template_filter("iso")
def iso_f(dt):
    """Fecha en UTC para que el navegador la pase a la hora del país de quien mira."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else ""


@app.template_filter("amount")
def amount(v):
    return _es(v, 2)


@app.template_filter("num")
def num(v):
    return f"{int(v or 0):,}".replace(",", ".")


@app.template_filter("short")
def short(v):
    v = int(v or 0)
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}M".replace(".0M", "M").replace(".", ",")
    if v >= 1_000:
        return f"{v / 1_000:.1f}K".replace(".0K", "K").replace(".", ",")
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


@app.template_filter("seen")
def seen(dt):
    """Última vez que alguien entró en la web: «En línea», «hace 3 h», «Nunca»."""
    if not dt:
        return "Nunca"
    if (datetime.utcnow() - dt).total_seconds() < ONLINE_FOR:
        return "En línea"
    return ago(dt)


@app.template_filter("seen_level")
def seen_level(dt):
    """Color del punto: verde en línea, gris claro hoy, ámbar esta semana, rojo más de 7 días o nunca."""
    if not dt:
        return "off"
    secs = (datetime.utcnow() - dt).total_seconds()
    return "on" if secs < ONLINE_FOR else "day" if secs < 86400 else "week" if secs < 7 * 86400 else "off"


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
    camps = (Campaign.query.filter_by(status="activa").options(selectinload(Campaign.submissions))
             .order_by(Campaign.created_at.desc()).all())     # los clips de todas las campañas en una sola consulta
    return sorted(camps, key=lambda c: not c.featured)


# ---------------------------------------------------------------- acceso con Google
def redirect_uri():
    if BASE_URL:
        uri = BASE_URL + url_for("google_callback")
    else:
        uri = url_for("google_callback", _external=True, _scheme="https" if PRODUCTION else None)
    if PRODUCTION and uri.startswith("http://"):          # Google solo acepta https en producción
        uri = "https://" + uri[len("http://"):]
    return uri


def sign_in(email, name, avatar="", sub=None, password_user=None):
    email = email.lower()
    user = password_user or (User.query.filter_by(google_sub=sub).first() if sub else None) or User.query.filter_by(email=email).first()
    if not user:
        user = User(email=email, name=(name or email.split("@")[0])[:120], accepted_terms_at=datetime.utcnow())
        db.session.add(user)
        log.info("Nuevo usuario: %s", email)
    if sub and not user.google_sub and user.password_hash:
        user.password_hash = ""      # Google confirma que el correo es suyo: la contraseña puesta por otro deja de valer
    user.google_sub = sub or user.google_sub
    if not user.avatar_ver:                  # si subió su propia foto, no la cambiamos por la de Google
        user.avatar_url = (avatar or user.avatar_url or "")[:500]
    if not user.accepted_terms_at:
        user.accepted_terms_at = datetime.utcnow()
    if role_for(email) and password_user is None:   # el admin solo se activa entrando con Google, nunca con contraseña
        user.is_admin, user.admin_role = True, role_for(email)
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
    mode = "registro" if request.args.get("modo") == "registro" else "entrar"
    return render_template("login.html", google_ready=bool(GOOGLE_CLIENT_ID), dev_login=DEV_LOGIN, mode=mode,
                           form_email=session.pop("form_email", ""), form_name=session.pop("form_name", ""))


EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
_login_fails = {}


def too_many_fails(key):
    now = time.time()
    tries = [t for t in _login_fails.get(key, []) if now - t < 900]
    _login_fails[key] = tries
    return len(tries) >= 10


@app.route("/login/correo", methods=["POST"])
def login_email():
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    ip = request.remote_addr or ""
    if too_many_fails(ip):
        flash("Demasiados intentos. Espera unos minutos y vuelve a probar.", "error")
        return redirect(url_for("login"))
    user = User.query.filter_by(email=email).first()
    if not user or not user.password_hash or not check_password_hash(user.password_hash, password):
        _login_fails.setdefault(ip, []).append(time.time())
        session["form_email"] = email[:255]
        if user and not user.password_hash:
            flash("Esa cuenta se creó con Google. Pulsa «Continuar con Google».", "error")
        else:
            flash("Correo o contraseña incorrectos.", "error")
        return redirect(url_for("login"))
    sign_in(user.email, user.name, password_user=user)
    return redirect(url_for("index"))


@app.route("/registro", methods=["POST"])
def register():
    name = (request.form.get("name") or "").strip()[:120]
    email = (request.form.get("email") or "").strip().lower()[:255]
    password = request.form.get("password") or ""
    back = redirect(url_for("login", modo="registro"))
    session["form_email"], session["form_name"] = email, name
    if too_many_fails("reg:" + (request.remote_addr or "")):
        flash("Has creado varias cuentas seguidas. Inténtalo más tarde.", "error")
        return back
    if not name:
        flash("Escribe tu nombre.", "error")
        return back
    if not EMAIL_RE.match(email):
        flash("Escribe un correo válido.", "error")
        return back
    if len(password) < 8:
        flash("La contraseña tiene que tener al menos 8 caracteres.", "error")
        return back
    if role_for(email):
        flash("Con este correo se entra con Google.", "error")
        return back
    if User.query.filter_by(email=email).first():
        flash("Ya hay una cuenta con ese correo. Entra con ella.", "error")
        return redirect(url_for("login"))
    user = User(email=email, name=name, password_hash=generate_password_hash(password), accepted_terms_at=datetime.utcnow())
    db.session.add(user)
    db.session.commit()
    _login_fails.setdefault("reg:" + (request.remote_addr or ""), []).append(time.time())
    session.pop("form_email", None)
    session.pop("form_name", None)
    log.info("Nuevo usuario con correo: %s", email)
    sign_in(email, name, password_user=user)
    flash(f"¡Bienvenido, {name}! Completa tu perfil en 3 pasos.", "ok")
    return redirect(url_for("onboarding"))


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
    u = sign_in(info["email"], info.get("name", ""), info.get("picture", ""), info.get("sub"))
    if u and not u.is_admin and not u.onboarded_at and not u.submissions:
        return redirect(url_for("onboarding"))
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
    best = [(s, viewcounter.embed_url(s.platform, s.url)) for s in approved[:3] if (s.views or 0) > 0]
    resp = Response(render_template("report.html", camp=camp, st=st, top=approved[:20], by_platform=by_platform, best=best,
                                    creators=len({s.user_id for s in approved}), an=campaign_analytics(camp)))
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
    size = request.args.get("t", type=int)
    if size in COVER_SIZES:
        key = (cid, camp.cover_version, size)
        data = _cover_cache.get(key)
        if data is None:
            data = resize_cover(camp.cover_data, size)
            if len(_cover_cache) > 300:
                _cover_cache.clear()
            _cover_cache[key] = data
        mime = "image/webp"
    else:
        data, mime = camp.cover_data, camp.cover_mime or "image/jpeg"
    resp = Response(data, mimetype=mime)
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


COVER_SIZES = (160, 720)          # miniatura de las tarjetas y portada grande (el doble de lo que se ve, para pantallas nítidas)
_cover_cache = {}


def resize_cover(data, size):
    """Versión pequeña de la portada en WebP, para que el móvil no descargue la imagen entera."""
    from PIL import Image
    img = Image.open(io.BytesIO(data)).convert("RGB")
    img.thumbnail((size, size))
    out = io.BytesIO()
    img.save(out, "WEBP", quality=80, method=4)
    return out.getvalue()


# ---------------------------------------------------------------- clipper
@app.route("/")
def index():
    if not current_user.is_authenticated:
        return landing()
    return render_template("home.html", **home_data(current_user))


@app.route("/campanas")
@login_required
def campaigns_page():
    all_cards = [(c, campaign_stats(c)) for c in active_campaigns()]
    hf = home_feature()
    if hf["mode"] == "campaign":
        featured = next((x for x in all_cards if x[0].id == hf["campaign_id"]), None)
    elif hf["mode"] in ("announce", "none"):
        featured = None
    else:
        featured = all_cards[0] if all_cards else None
    cat = request.args.get("cat", "")
    cards = [x for x in all_cards if not cat or x[0].category == cat]
    counts = {k: sum(1 for c, _ in all_cards if c.category == k) for k in CATEGORIES}
    my_join = {m.campaign_id: m.status for m in CampaignMember.query.filter_by(user_id=current_user.id).all()}
    cards.sort(key=lambda x: bool(x[0].full and x[0].id not in my_join))   # plazas completas (si no estás dentro) al final
    apply_schedule()
    return render_template("campaigns.html", cards=cards, featured=featured, cat=cat, counts=counts,
                           total=len(all_cards), new_user=not current_user.submissions, my_join=my_join,
                           announce=hf if hf["mode"] == "announce" else None)


BEGINNER_DAYS = 14                 # pasado este tiempo, el banner deja de enseñar la guía para principiantes


def beginner_course():
    """El curso marcado como «Guía para principiantes» (el primero publicado con ese nombre)."""
    return (Course.query.filter(Course.published.is_(True), db.func.lower(Course.title).like("%principiante%"))
            .order_by(Course.position, Course.id).first())


def home_data(u):
    """Todo lo del Inicio: resumen, banner de formación, paso a paso, clips y ranking de la semana."""
    subs = list(u.submissions)
    stage = {}
    for s_ in subs:
        k = clip_stage(s_)
        stage[k] = stage.get(k, 0) + 1
    bits = []
    if stage.get("estadisticas"):
        n = stage["estadisticas"]
        bits.append(f"{n} {'clip' if n == 1 else 'clips'} por verificar")
    if stage.get("contando"):
        n = stage["contando"]
        bits.append(f"{n} {'clip' if n == 1 else 'clips'} contando vistas")
    if stage.get("revisar"):
        n = stage["revisar"]
        bits.append(f"{n} en revisión")
    summary = ("Tienes " + ", ".join(bits[:-1]) + (" y " if len(bits) > 1 else "") + bits[-1] + ".") if bits else (
        "Elige una campaña, publica tus clips y cobra por cada vista." if not subs else "Todo al día. Sigue publicando para sumar vistas.")
    # formación: guía para principiantes → siguiente curso → lo que funciona
    guide = beginner_course()
    gp = course_progress(guide, u.id) if guide else None
    # banner de formación: cuánto hay para aprender y cuánto llevas
    pub = [c for c in Course.query.filter_by(published=True).all() if c.visible_lessons]
    prs = [course_progress(c, u.id) for c in pub]
    les_total = sum(p["total"] for p in prs)
    les_done = sum(p["done"] for p in prs)
    learn = {"courses": len(pub), "lessons": les_total, "done": les_done,
             "pct": round(les_done / les_total * 100) if les_total else 0,
             "duration": dur_label(sum(ls.duration or 0 for c in pub for ls in c.visible_lessons))}
    seen_ids = set().union(*[p["done_ids"] for p in prs]) if prs else set()
    all_les = [ls for c in sorted(pub, key=lambda c: (c.position, c.id)) for ls in c.visible_lessons]
    pick = [ls for ls in all_les if ls.id not in seen_ids][:3] or all_les[:3]     # las siguientes que le tocan
    learn["thumbs"] = [{"title": ls.title, "img": lesson_thumb(ls),
                        "dur": f"{ls.duration // 60}:{ls.duration % 60:02d}" if ls.duration else ""} for ls in pick]
    learn_url = url_for("courses")              # el banner lleva a Formación en general, sin marcar ningún curso
    # paso a paso
    wa = get_setting("whatsapp_general", "")
    steps = [{"title": "Únete al grupo de WhatsApp", "text": "Avisos, plazas nuevas y comunidad de clippers.",
              "done": bool(wa and u.wa_joined_at), "btn": "Unirme", "url": url_for("join_whatsapp"), "ext": True,
              "soon": not wa}]                               # sin enlace aún: sale como «Próximamente» y no cuenta
    if guide and gp["total"]:
        steps.append({"title": "Mira la guía para principiantes", "text": "Lo básico para que te aprueben los clips.",
                      "done": gp["finished"], "btn": "Empezar", "url": url_for("courses", destacar=guide.id)})
    else:                                                    # aún no hay guía publicada: lleva a Formación
        any_done = any(course_progress(c, u.id)["finished"] for c in Course.query.filter_by(published=True).all())
        steps.append({"title": "Mira la guía para principiantes", "text": "Lo básico para que te aprueben los clips.",
                      "done": any_done, "btn": "Ver formación", "url": url_for("courses")})
    steps.append({"title": "Aplica a una campaña", "text": "Elige una y pide entrar.",
                  "done": bool(CampaignMember.query.filter_by(user_id=u.id).first()), "btn": "Ver campañas",
                  "url": url_for("campaigns_page")})
    show_steps = not all(st["done"] or st.get("soon") for st in steps)
    # ranking de la semana: mi puesto y cuánto falta para el de arriba
    full = ranking("semana", 1000)
    me = next((r for r in full if r["user"].id == u.id), None)
    above = full[me["pos"] - 2] if me and me["pos"] > 1 else None
    rank = {"top": full[:5], "me": me, "total": len(full), "above": above,
            "pct": (round(me["pos"] / len(full) * 100) if me else None),
            "bar": (min(100, round(me["views"] / above["views"] * 100)) if me and above and above["views"] else 100)}
    # tu resumen rápido
    week_ago = datetime.utcnow() - timedelta(days=7)
    money_ = user_money(u)
    quick = {"available": money_["available"], "live": money_["live"],
             "views_week": sum(s_.views or 0 for s_ in subs if s_.created_at >= week_ago and s_.status != "rechazado"),
             "counting": stage.get("contando", 0) + stage.get("revisar", 0), "stats": stage.get("estadisticas", 0)}
    # banner de bienvenida: portadas de campañas activas (primero las que tienen plazas libres)
    live = [c for c in active_campaigns() if not c.exhausted]
    live.sort(key=lambda c: (bool(c.full), not c.cover_version))
    open_n = sum(1 for c in live if not c.full)
    if not bits and open_n:
        summary = (f"{open_n} {'campaña activa' if open_n == 1 else 'campañas activas'} con plazas libres. "
                   "Elige una y empieza a ganar.")
    actives = active_campaigns()
    pot = cents(sum(max(c.remaining or 0, 0) for c in actives))   # lo que queda por repartir (con o sin plazas)
    hero_stats = {"active": len(actives), "open": sum(1 for c in actives if not c.full and not c.exhausted),
                  "best": max([max([c.rate or 0] + [r for _, r in c.platform_rates]) for c in actives] or [0])}
    return {"quick": quick, "hero_camps": live[:3], "pot": pot, "hero_stats": hero_stats,
            "summary": summary, "learn_url": learn_url, "learn": learn, "steps": steps, "show_steps": show_steps,
            "steps_done": sum(1 for st in steps if st["done"]), "wa_general": wa, "week_clips": clips_of_week(6),
            "rank": rank, "masked": mask_name}


@app.route("/grupo-whatsapp")
@login_required
def join_whatsapp():
    """Abre el grupo general y lo apunta como paso hecho."""
    url = get_setting("whatsapp_general", "")
    if not url:
        return redirect(url_for("index"))
    if not current_user.wa_joined_at:
        current_user.wa_joined_at = datetime.utcnow()
        db.session.commit()
    return redirect(url)


def submit_clip(camp, raw_url):
    """Valida y guarda un clip. Devuelve (ok, mensaje)."""
    if camp.status != "activa" or camp.exhausted:
        return False, "Esta campaña ya no acepta clips."
    if False and camp.not_started:                     # (opción de fecha de inicio quitada)
        return False, f"La campaña empieza el {camp.starts_at.strftime('%d/%m/%Y a las %H:%M')} (hora UTC). Podrás subir clips desde entonces."
    today = datetime.utcnow() - timedelta(hours=24)
    if MAX_CLIPS_DAY > 0 and Submission.query.filter(Submission.user_id == current_user.id, Submission.created_at >= today).count() >= MAX_CLIPS_DAY:
        return False, f"Has llegado al límite de {MAX_CLIPS_DAY} clips en 24 horas. Vuelve a intentarlo mañana."
    url = viewcounter.normalize_url(raw_url)
    platform, vid = viewcounter.detect_platform(url), viewcounter.video_id(url)
    if not platform or not vid:
        return False, "Ese enlace no es válido. Pega el enlace directo al vídeo, no al perfil."
    if platform not in camp.platform_list:
        return False, f"Esta campaña no acepta vídeos de {PLATFORMS[platform]}."
    mine = [a.handle for a in campaign_accounts(current_user.id, camp.id, "aceptada", platform)]
    if not mine:
        member = campaign_member(current_user.id, camp.id)
        if not member:
            return False, "Primero solicita entrar en esta campaña."
        if member.status == "pendiente":
            return False, "Tu solicitud para esta campaña aún está en revisión."
        if member.status == "rechazada":
            return False, "No has sido aceptado en esta campaña."
        return False, f"Añade y verifica tu cuenta de {PLATFORMS[platform]} en esta campaña para subir clips desde ella."
    author = viewcounter.tiktok_handle_from_url(url) if platform == "tiktok" else ""
    if author and author not in mine:
        if handle_owner(platform, author, exclude_user_id=current_user.id):
            return False, f"Ese vídeo es de @{author}, una cuenta vinculada a otro clipper."
        return False, f"Ese vídeo es de @{author}, que no es una de tus cuentas aceptadas en esta campaña."
    if Submission.query.filter_by(video_key=f"{platform}:{vid}").first():
        return False, "Ese vídeo ya se envió antes."
    IgnoredClip.query.filter_by(video_key=f"{platform}:{vid}").delete()   # lo había borrado y lo vuelve a añadir a mano
    db.session.add(Submission(user_id=current_user.id, campaign_id=camp.id, url=url, platform=platform,
                              video_key=f"{platform}:{vid}", author=author,
                              review_since=None if camp.min_for(platform) else datetime.utcnow()))
    db.session.commit()
    if any(camp.min_for(p) for p in camp.platform_list):
        return True, "¡Clip enviado! Ya está contando vistas. Lo revisaremos cuando llegue al pago mínimo."
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
    waiting = bool(CampaignWaitlist.query.filter_by(user_id=current_user.id, campaign_id=camp.id).first())
    winners = sorted((s for s in camp.submissions if s.status == "aprobado" and (s.views or 0) > 0),
                     key=lambda s: -(s.views or 0))[:10]
    ctab = request.args.get("tab", "resumen")
    real_member = (not current_user.is_admin and is_campaign_member(current_user, camp)) or is_mod(current_user, camp.id)
    staff_view = current_user.is_admin and request.args.get("hub") == "1"   # el equipo ve la campaña como un clipper de fuera…
    hub = real_member or staff_view                                         # …salvo que pida ver el hub
    sections, chan, posts, liked = [], None, [], set()
    support = None
    if real_member and not current_user.is_admin:
        sup_q = CampMessage.query.filter_by(campaign_id=cid, user_id=current_user.id)
        support = {"unread": sup_q.filter_by(from_staff=True, read_by_user=False).count(), "open": request.args.get("soporte") == "1",
                   "msgs": []}
        if support["open"]:
            support["msgs"] = sup_q.order_by(CampMessage.created_at).all()
            if support["unread"]:
                sup_q.filter_by(from_staff=True, read_by_user=False).update({"read_by_user": True})
                db.session.commit()
                support["unread"] = 0
    if hub:
        sections = campaign_channels(camp)
        chid = request.args.get("canal", type=int)
        if chid:
            chan = next((c for sec in sections for c in sec.channels if c.id == chid), None)
            if not chan:
                return redirect(url_for("campaign", cid=cid))
            if chan.kind in ("posts", "rules", "resources"):
                posts = (Post.query.filter_by(channel_id=chan.id).options(selectinload(Post.likes), selectinload(Post.comments))
                         .order_by(Post.pinned.desc(), Post.created_at.desc()).limit(50).all())
                liked = {lk.post_id for lk in PostLike.query.filter(PostLike.user_id == current_user.id,
                                                                    PostLike.post_id.in_([p_.id for p_ in posts] or [0]))}
    return render_template("campaign.html", camp=camp, st=st, mine=mine, accounts=accounts, accepted=accepted,
                           member=campaign_member(current_user.id, camp.id), join_statuses=JOIN_STATUSES, waiting=waiting,
                           winners=winners, ctab=ctab, hub=hub, sections=sections, chan=chan, posts=posts, liked=liked,
                           mod_ids=campaign_mod_ids(cid) if hub else set(), i_mod=hub and is_mod(current_user, cid),
                           mod_del=hub and mod_can(current_user, cid, "comentarios"),
                           hubq=1 if staff_view else None, staff_view=staff_view, support=support,
                           mod_ids_all=campaign_mod_ids(cid) if support and support["open"] else set())


def _member_post(cid, pid):
    camp = db.session.get(Campaign, cid) or abort(404)
    post_ = db.session.get(Post, pid)
    if not post_ or post_.channel.campaign_id != cid or not is_campaign_member(current_user, camp):
        abort(404)
    return camp, post_


@app.route("/campana/<int:cid>/publicacion/<int:pid>/megusta", methods=["POST"])
@login_required
def post_like(cid, pid):
    camp, post_ = _member_post(cid, pid)
    row = PostLike.query.filter_by(post_id=pid, user_id=current_user.id).first()
    if row:
        db.session.delete(row)
    else:
        db.session.add(PostLike(post_id=pid, user_id=current_user.id))
    db.session.commit()
    if wants_json():
        return {"liked": not row, "count": PostLike.query.filter_by(post_id=pid).count()}
    return redirect(url_for("campaign", cid=cid, canal=post_.channel_id, _anchor=f"p{pid}"))


@app.route("/campana/<int:cid>/publicacion/<int:pid>/comentar", methods=["POST"])
@login_required
def post_comment(cid, pid):
    camp, post_ = _member_post(cid, pid)
    body = " ".join((request.form.get("body") or "").split())[:1000]
    if body:
        recent = PostComment.query.filter(PostComment.user_id == current_user.id,
                                          PostComment.created_at >= datetime.utcnow() - timedelta(minutes=10)).count()
        if recent >= 20 and not current_user.is_admin:
            flash("Has escrito muchos comentarios seguidos. Espera un poco.", "error")
        else:
            db.session.add(PostComment(post_id=pid, user_id=current_user.id, body=body))
            db.session.commit()
    return redirect(url_for("campaign", cid=cid, canal=post_.channel_id, _anchor=f"p{pid}"))


@app.route("/comentario/<int:coid>/borrar", methods=["POST"])
@login_required
def delete_comment(coid):
    co = db.session.get(PostComment, coid) or abort(404)
    ch = co.post.channel
    if co.user_id != current_user.id and not current_user.is_admin and not mod_can(current_user, ch.campaign_id, "comentarios"):
        abort(403)
    db.session.delete(co)
    db.session.commit()
    nxt = request.form.get("next", "")
    return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("campaign", cid=ch.campaign_id, canal=ch.id))


@app.route("/publicacion/<int:pid>/imagen")
@login_required
def post_image(pid):
    p_ = db.session.get(Post, pid)
    if not p_ or not p_.image_mime or not is_campaign_member(current_user, db.session.get(Campaign, p_.channel.campaign_id)):
        abort(404)
    resp = Response(p_.image_data, mimetype=p_.image_mime)
    resp.headers["Cache-Control"] = "private, max-age=31536000, immutable"
    return resp


@app.route("/campana/<int:cid>/avisame", methods=["POST"])
@login_required
def campaign_waitlist(cid):
    """Apuntarse (o desapuntarse) para que te avisen cuando haya plazas."""
    camp = db.session.get(Campaign, cid) or abort(404)
    row = CampaignWaitlist.query.filter_by(user_id=current_user.id, campaign_id=cid).first()
    if row:
        db.session.delete(row)
        flash("Ya no te avisaremos de esta campaña.", "info")
    elif camp.full and not campaign_member(current_user.id, cid):
        db.session.add(CampaignWaitlist(user_id=current_user.id, campaign_id=cid))
        flash("¡Hecho! Te avisaremos por mensaje cuando haya plazas.", "ok")
    db.session.commit()
    return redirect(url_for("campaign", cid=cid))


@app.route("/campana/<int:cid>/unirme", methods=["POST"])
@login_required
def join_campaign(cid):
    """El clipper pide entrar: solo responde a las preguntas. La cuenta la añade cuando le aceptan."""
    camp = db.session.get(Campaign, cid) or abort(404)
    back = redirect(url_for("campaign", cid=cid))
    if camp.status != "activa" or camp.exhausted:
        flash("Esta campaña ya no acepta clippers nuevos.", "error")
        return back
    if campaign_member(current_user.id, cid):
        flash("Ya enviaste tu solicitud para esta campaña.", "info")
        return back
    if camp.full or (camp.max_members and camp.members_accepted >= camp.max_members):
        flash("Las plazas de esta campaña están completas. Vuelve a mirar más tarde.", "error")
        return back
    answers = []
    for i, q in enumerate(camp.question_list):
        a = (request.form.get(f"q_{i}") or "").strip()[:1500]
        if not a:
            flash("Responde a todas las preguntas para enviar tu solicitud.", "error")
            return back
        answers.append([q, a])
    db.session.add(CampaignMember(user_id=current_user.id, campaign_id=cid,
                                  answers=json.dumps(answers, ensure_ascii=False) if answers else ""))
    db.session.commit()
    notify_phone(f"🙋 {current_user.name} quiere unirse", f"«{camp.song}». Revisa su solicitud.",
                 url_for("admin_home", _anchor="unirse"), tags="raising_hand")
    flash("¡Solicitud enviada! Te avisaremos cuando el equipo la revise.", "ok")
    return back


@app.route("/campana/<int:cid>/cuentas", methods=["POST"])
@login_required
def add_campaign_account(cid):
    """Ya aceptado en la campaña, el clipper añade la cuenta con la que publicará y luego la verifica con su bio."""
    camp = db.session.get(Campaign, cid) or abort(404)
    back = redirect(url_for("campaign", cid=cid))
    member = campaign_member(current_user.id, cid)
    if not member or member.status != "aceptada":
        flash("Podrás añadir tu cuenta cuando te acepten en la campaña.", "error")
        return back
    platform = request.form.get("platform", "")
    handle = viewcounter.clean_handle(request.form.get("handle", ""))
    if platform not in camp.platform_list:
        flash("Elige una red de las que acepta la campaña.", "error")
        return back
    if not handle:
        flash("Escribe el usuario de la cuenta con la que vas a publicar.", "error")
        return back
    if handle_owner(platform, handle, exclude_user_id=current_user.id):
        flash(f"La cuenta @{handle} ya está vinculada a otro clipper.", "error")
        return back
    if False and camp.new_accounts_only:               # (opción quitada)
        used = (CampaignAccount.query.filter(CampaignAccount.platform == platform, CampaignAccount.handle == handle,
                                             CampaignAccount.campaign_id != cid).first()
                or Submission.query.filter(Submission.platform == platform, Submission.author == handle,
                                           Submission.campaign_id != cid).first())
        if used:
            flash(f"Esta campaña pide una cuenta nueva, solo para ella. @{handle} ya se usó en otra campaña.", "error")
            return back
    other = (CampaignAccount.query.filter(CampaignAccount.platform == platform, CampaignAccount.handle == handle,
                                          CampaignAccount.campaign_id != cid, CampaignAccount.status != "rechazada")
             .join(Campaign).filter(Campaign.status != "finalizada").first())
    if other:                                          # cada cuenta es de una sola campaña: todo lo que publica es de ella
        flash(f"La cuenta @{handle} ya está en la campaña «{other.campaign.song}». Cada cuenta solo puede estar en una campaña.", "error")
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
    verified = CampaignAccount.query.filter_by(user_id=current_user.id, platform=platform, handle=handle, bio_verified=True).first()
    db.session.add(CampaignAccount(user_id=current_user.id, campaign_id=cid, platform=platform, handle=handle,
                                   status="aceptada" if verified else "pendiente", decided_at=datetime.utcnow() if verified else None,
                                   bio_verified=bool(verified), bio_status="Verificada" if verified else ""))
    db.session.commit()
    if verified:
        flash(f"Cuenta @{handle} añadida. Ya estaba verificada: puedes subir clips desde ella.", "ok")
    else:
        flash(f"Cuenta @{handle} añadida. Pon el código {current_user.bio_code()} en su bio y pulsa «Comprobar».", "ok")
    return back


BIO_WAIT = 90          # segundos entre comprobaciones de la misma cuenta


def check_bio(aid):
    """Lee la bio con Apify y marca la cuenta como verificada si tiene el código. Va en segundo plano."""
    acc = db.session.get(CampaignAccount, aid)
    if not acc:
        return
    code = acc.user.bio_code()
    try:
        bio = viewcounter.fetch_bio(acc.platform, acc.handle)
    except Exception as e:  # noqa: BLE001
        log.warning("No se pudo leer la bio de @%s: %s", acc.handle, e)
        acc.bio_status = "No hemos podido leer tu perfil ahora. Inténtalo en unos minutos."
        db.session.commit()
        return
    if bio is None:
        acc.bio_status = f"No encontramos el perfil @{acc.handle}. Revisa que el usuario esté bien y que el perfil sea público."
    elif code.lower() in bio.lower():
        now = datetime.utcnow()
        others = CampaignAccount.query.filter_by(user_id=acc.user_id, platform=acc.platform, handle=acc.handle).all()
        for a in others:              # la misma cuenta queda verificada en todas sus campañas
            a.bio_verified, a.bio_status = True, "Verificada"
            if a.status == "pendiente":
                a.status, a.decided_at = "aceptada", now
    else:
        acc.bio_status = f"Todavía no vemos el código {code} en la bio de @{acc.handle}. Guárdalo en tu perfil y vuelve a comprobar."
    db.session.commit()


@app.route("/campana/<int:cid>/cuenta/<int:aid>/comprobar", methods=["POST"])
@login_required
def verify_bio(cid, aid):
    acc = db.session.get(CampaignAccount, aid)
    if not acc or acc.user_id != current_user.id or acc.campaign_id != cid:
        abort(404)
    if acc.bio_verified:
        flash("Esa cuenta ya está verificada.", "ok")
        return redirect(url_for("campaign", cid=cid))
    if acc.bio_checked_at and (datetime.utcnow() - acc.bio_checked_at).total_seconds() < BIO_WAIT and acc.bio_status == "Comprobando…":
        flash("Ya lo estamos comprobando. Recarga la página en un momento.", "info")
        return redirect(url_for("campaign", cid=cid))
    acc.bio_checked_at, acc.bio_status = datetime.utcnow(), "Comprobando…"
    db.session.commit()
    if not viewcounter.APIFY_TOKEN:
        acc.bio_status = "La comprobación automática no está disponible ahora mismo. Inténtalo de nuevo en un rato."
        db.session.commit()
        return redirect(url_for("campaign", cid=cid))

    def run(app_obj=app, aid=aid):
        with app_obj.test_request_context(base_url=BASE_URL or "http://localhost"):
            try:
                check_bio(aid)
            except Exception:  # noqa: BLE001
                log.exception("Fallo comprobando la bio")
                db.session.rollback()
    threading.Thread(target=run, daemon=True).start()
    return redirect(url_for("campaign", cid=cid))


def account_open_clips(acc):
    """Clips de esa cuenta en la campaña que aún no están cerrados (contando, verificando o en revisión)."""
    q = Submission.query.filter(Submission.user_id == acc.user_id, Submission.campaign_id == acc.campaign_id,
                                Submission.platform == acc.platform, Submission.status == "pendiente")
    return q.filter(db.or_(Submission.author == acc.handle, Submission.author == "", Submission.author.is_(None))).count()


def remove_campaign_account(acc, by_staff=False):
    """Quita la cuenta de la campaña. Devuelve un error si no se puede (tiene clips en marcha)."""
    n = account_open_clips(acc)
    if n and not by_staff:
        return (f"@{acc.handle} tiene {n} {'clip' if n == 1 else 'clips'} en marcha en esta campaña. "
                "Espera a que se aprueben o se rechacen, o escribe a soporte.")
    label = f"@{acc.handle} ({PLATFORMS.get(acc.platform, acc.platform)})"
    audit("cuenta_quitada", f"{acc.user.email} · {label} · «{acc.campaign.song}»" + (" · por el equipo" if by_staff else ""))
    db.session.delete(acc)
    db.session.commit()
    return None


@app.route("/campana/<int:cid>/cuenta/<int:aid>/quitar", methods=["POST"])
@login_required
def remove_account(cid, aid):
    acc = db.session.get(CampaignAccount, aid)
    if not acc or acc.user_id != current_user.id or acc.campaign_id != cid:
        abort(404)
    handle = acc.handle
    err = remove_campaign_account(acc)
    flash(err or f"Has quitado @{handle} de esta campaña. Ya no se buscarán clips nuevos en esa cuenta.", "error" if err else "ok")
    return redirect(url_for("campaign", cid=cid))


@app.route("/campana/<int:cid>/cuenta/<int:aid>/estado")
@login_required
def bio_state(cid, aid):
    """Para que la página sepa sola cuándo ha terminado la comprobación de la bio (sin recargar a mano)."""
    acc = db.session.get(CampaignAccount, aid)
    if not acc or acc.user_id != current_user.id or acc.campaign_id != cid:
        abort(404)
    stuck = acc.bio_checked_at and (datetime.utcnow() - acc.bio_checked_at).total_seconds() > 300
    return {"checking": acc.bio_status == "Comprobando…" and not stuck, "verified": bool(acc.bio_verified)}


CLIP_TABS = {"todos": "Todos", "contando": "Contando vistas", "estadisticas": "Verificar vistas", "revisar": "En revisión", "aprobado": "Aprobados", "rechazado": "Rechazados"}


def clipper_stats(subs):
    approved = [s for s in subs if s.status == "aprobado"]
    return {"views": sum(s.views or 0 for s in subs if s.status != "rechazado"),
            "earned": cents(sum(s.earned or 0 for s in approved)),
            "sent": len(subs), "approved": len(approved),
            "rate": round(len(approved) / len([s for s in subs if s.status in ("aprobado", "rechazado")]) * 100)
            if any(s.status in ("aprobado", "rechazado") for s in subs) else None}


@app.route("/enviar", methods=["GET", "POST"])
@app.route("/mis-clips", methods=["GET", "POST"])
@login_required
def my_clips():
    joined = {a.campaign_id for a in CampaignAccount.query.filter_by(user_id=current_user.id, status="aceptada")}
    joined |= {m.campaign_id for m in CampaignMember.query.filter_by(user_id=current_user.id, status="aceptada")}
    camps = [c for c in active_campaigns() if c.id in joined]
    if request.method == "POST":
        camp = db.session.get(Campaign, request.form.get("campaign_id", type=int) or 0)
        if not camp:
            flash("Elige una campaña.", "error")
        else:
            ok, msg = submit_clip(camp, request.form.get("url", ""))
            flash(msg, "ok" if ok else "error")
        return redirect(url_for("my_clips", c=request.form.get("campaign_id", "")))
    allsubs = sorted(current_user.submissions, key=lambda s: s.created_at, reverse=True)
    tab = request.args.get("tab", "todos")
    tab = tab if tab in CLIP_TABS else "todos"
    fc = request.args.get("campana", type=int)
    fp = request.args.get("red", "")
    fp = fp if fp in PLATFORMS else ""
    dias = request.args.get("dias", 30, type=int)
    dias = dias if dias in CLIP_RANGES else 30
    q = " ".join((request.args.get("q") or "").split())[:80]
    orden = request.args.get("orden", "recientes")
    orden = orden if orden in ("recientes", "vistas", "ganado") else "recientes"
    scoped = [s for s in allsubs if (not fc or s.campaign_id == fc) and (not fp or s.platform == fp)]
    an = clipper_analytics(scoped, dias)
    ql = q.lower().lstrip("#@")
    subs = [s for s in scoped if (tab == "todos" or clip_stage(s) == tab)
            and (not ql or ql in f"{s.id} {s.url} {s.author or ''} {s.campaign.song} {s.campaign.artist}".lower())]
    if orden == "vistas":
        subs.sort(key=lambda s: -(s.views or 0))
    elif orden == "ganado":
        subs.sort(key=lambda s: -float(s.earned or 0))
    counts = {t: sum(1 for s in scoped if clip_stage(s) == t) for t in CLIP_TABS if t != "todos"}
    counts["todos"] = len(scoped)
    my_camps = sorted({s.campaign for s in allsubs}, key=lambda c: c.song.lower())
    stats_sent = sorted((s for s in allsubs if s.stats_status == "enviado"), key=lambda s: s.stats_at or s.created_at,
                        reverse=True)[:30]
    show_stats = bool(counts.get("estadisticas") or stats_sent or bunny_ready())
    if tab == "estadisticas" and not show_stats:
        return redirect(url_for("my_clips"))
    return render_template("submit.html", camps=camps, subs=subs, selected=request.args.get("c", type=int),
                           tab=tab, counts=counts, stats=clipper_stats(allsubs), my_camps=my_camps, fc=fc, fp=fp,
                           stats_sent=stats_sent, show_stats=show_stats, now=datetime.utcnow(), an=an, dias=dias,
                           q=q, orden=orden, ranges=CLIP_RANGES)


def clip_deletable(s):
    """El clipper puede borrar un clip suyo si no ha cobrado nada por él (no aprobado) ni tiene una apelación en curso."""
    return s.status != "aprobado" and s.appeal_status != "pendiente"


@app.route("/mis-clips/<int:sid>/eliminar", methods=["POST"])
@login_required
def delete_clip(sid):
    s = db.session.get(Submission, sid)
    if not s or s.user_id != current_user.id:
        abort(404)
    if not clip_deletable(s):
        flash("Este clip ya está aprobado o tiene una apelación en curso: no se puede borrar. Escribe a soporte si hay un problema.", "error")
        return redirect(request.referrer or url_for("my_clips"))
    if not IgnoredClip.query.filter_by(video_key=s.video_key).first():
        db.session.add(IgnoredClip(user_id=current_user.id, video_key=s.video_key))
    ViewSnapshot.query.filter_by(submission_id=s.id).delete(synchronize_session=False)
    audit("clip_borrado_por_clipper", f"#{s.id} · {s.url} · «{s.campaign.song}»")
    db.session.delete(s)
    db.session.commit()
    flash("Clip eliminado. No volverá a aparecer solo; si te equivocaste, añádelo a mano con su enlace.", "ok")
    return redirect(request.referrer or url_for("my_clips"))


# ---------------------------------------------------------------- ranking
RANK_PERIODS = {"semana": ("Esta semana", 7), "mes": ("Este mes", 30), "global": ("Siempre", 0)}


def mask_name(name):
    """«Ana García López» → «Ana G.» — en el ranking nadie ve el nombre completo de los demás."""
    parts = (name or "Clipper").split()
    return parts[0][:16] + (f" {parts[1][0].upper()}." if len(parts) > 1 and parts[1] else "")


def ranking(period="semana", limit=50):
    days = RANK_PERIODS.get(period, RANK_PERIODS["semana"])[1]
    q = (db.session.query(Submission.user_id, db.func.sum(Submission.views), db.func.sum(Submission.earned),
                          db.func.count(Submission.id))
         .join(User, User.id == Submission.user_id)
         .filter(Submission.status == "aprobado", User.is_banned.is_(False), User.is_admin.is_(False)))
    if days:
        q = q.filter(Submission.created_at >= datetime.utcnow() - timedelta(days=days))
    rows = q.group_by(Submission.user_id).order_by(db.func.sum(Submission.views).desc()).limit(limit).all()
    users = {u.id: u for u in User.query.filter(User.id.in_([r[0] for r in rows])).all()} if rows else {}
    return [{"pos": i + 1, "user": users[r[0]], "name": mask_name(users[r[0]].name), "views": int(r[1] or 0),
             "earned": cents(r[2]), "clips": r[3]} for i, r in enumerate(rows) if r[0] in users]


@app.route("/ranking")
@login_required
def ranking_page():
    period = request.args.get("p", "semana")
    period = period if period in RANK_PERIODS else "semana"
    rows = ranking(period)
    me = next((r for r in rows if r["user"].id == current_user.id), None)
    return render_template("ranking.html", rows=rows, period=period, me=me, periods=RANK_PERIODS)


def first_steps(user):
    """Los 4 primeros pasos del clipper, para la tarjeta de bienvenida."""
    members = CampaignMember.query.filter_by(user_id=user.id).all()
    steps = [
        ("Completa tu perfil", bool(user.onboarded_at), url_for("onboarding")),
        ("Únete a una campaña", any(m.status == "aceptada" for m in members), url_for("campaigns_page")),
        ("Sube tu primer clip", bool(user.submissions), url_for("my_clips")),
        ("Conecta Whop para cobrar", bool(user.whop_user_id), url_for("account", _anchor="cobro")),
    ]
    return {"steps": steps, "done": sum(1 for s in steps if s[1])}


def clips_of_week(limit=3):
    return (Submission.query.join(Campaign).join(User, User.id == Submission.user_id)
            .filter(Submission.status == "aprobado", Submission.created_at >= datetime.utcnow() - timedelta(days=7),
                    User.is_banned.is_(False), User.is_admin.is_(False), Submission.views > 0)   # igual que el ranking
            .order_by(Submission.views.desc()).limit(limit).all())


# ---------------------------------------------------------------- bienvenida (3 pasos)
@app.route("/bienvenida", methods=["GET", "POST"])
@login_required
def onboarding():
    step = request.args.get("paso", 1, type=int)
    step = step if step in (1, 2) else 1
    u = current_user
    if request.method == "POST":
        if request.form.get("saltar"):
            u.onboarded_at = u.onboarded_at or datetime.utcnow()
            db.session.commit()
            return redirect(url_for("index"))
        if step == 1:
            name = (request.form.get("name") or "").strip()[:120]
            if len(name) < 2:
                flash("Escribe tu nombre.", "error")
                return redirect(url_for("onboarding", paso=1))
            u.name = name
            file = request.files.get("photo")
            if file and file.filename:
                data = process_avatar(file)
                if not data:
                    flash("La foto tiene que ser una imagen (JPG, PNG o WEBP).", "error")
                    return redirect(url_for("onboarding", paso=1))
                u.avatar_data, u.avatar_ver = data, (u.avatar_ver or 0) + 1
                u.avatar_url = url_for("avatar", uid=u.id, v=u.avatar_ver)
            tt_ = viewcounter.clean_handle(request.form.get("tiktok", ""))
            if tt_ and tt_ not in u.handles("tiktok"):
                if handle_owner("tiktok", tt_, exclude_user_id=u.id):
                    flash(f"La cuenta @{tt_} ya está vinculada a otro clipper.", "error")
                    return redirect(url_for("onboarding", paso=1))
                if len(u.handles("tiktok")) < MAX_HANDLES:
                    u.tiktok_handles = ",".join(u.handles("tiktok") + [tt_])
        elif step == 2:
            c = request.form.get("country", "")
            other = " ".join((request.form.get("country_other") or "").split())[:60]
            if c not in COUNTRIES:
                flash("Elige tu país.", "error")
                return redirect(url_for("onboarding", paso=2))
            if c == "OT" and len(other) < 2:
                flash("Escribe en qué país vives.", "error")
                return redirect(url_for("onboarding", paso=2))
            u.country, u.country_other = c, (other if c == "OT" else "")
            u.languages = "es"                       # todos publican en español: ya no se pregunta
            u.onboarded_at = datetime.utcnow()       # el cobro (Whop) se conecta luego en «Mi cuenta»
            db.session.commit()
            flash("¡Perfil listo! Elige una campaña y pide unirte.", "ok")
            return redirect(url_for("index"))
        db.session.commit()
        return redirect(url_for("onboarding", paso=step + 1))
    return render_template("onboarding.html", step=step)


def process_avatar(file):
    """Foto de perfil: cuadrada, 256 px, JPEG limpio (sin datos de la cámara)."""
    from PIL import Image, ImageOps, UnidentifiedImageError
    try:
        img = Image.open(file.stream)
        img.verify()
        file.stream.seek(0)
        img = ImageOps.exif_transpose(Image.open(file.stream)).convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError):
        return None
    img = ImageOps.fit(img, (256, 256))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85, optimize=True)
    return out.getvalue()


@app.route("/foto/<int:uid>")
@login_required
def avatar(uid):
    u = db.session.get(User, uid)
    if not u or not u.avatar_data:
        abort(404)
    resp = Response(u.avatar_data, mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "private, max-age=31536000, immutable"
    return resp


@app.route("/ganancias")
@login_required
def earnings():
    tab = request.args.get("tab", "campanas")
    if tab == "pendiente":                      # los clips en revisión viven en «Mis clips»
        return redirect(url_for("my_clips", tab="revisar"))
    if tab != "retiros":
        tab = "campanas"
    whop_sync([p for p in current_user.payouts if p.status == "procesando"])
    release_stuck_payouts(Payout.query.filter_by(user_id=current_user.id))
    m = user_money(current_user)
    by_camp = {}                                # lo ganado, agrupado por campaña (no clip a clip)
    for s_, a in m["earned_rows"] + m["live_rows"]:
        row = by_camp.setdefault(s_.campaign_id, {"camp": s_.campaign, "clips": 0, "views": 0, "amount": 0.0, "last": s_.created_at})
        row["clips"] += 1
        row["views"] += s_.views or 0
        row["amount"] += a
        row["last"] = max(row["last"], s_.created_at)
    camp_rows = sorted(by_camp.values(), key=lambda r: (r["camp"].status == "finalizada", -r["amount"]))
    payouts = sorted(current_user.payouts, key=lambda p: p.created_at, reverse=True)
    return render_template("earnings.html", m=m, tab=tab, payouts=payouts, camp_rows=camp_rows)


_withdraw_lock = threading.Lock()


@app.route("/ganancias/retirar", methods=["POST"])
@login_required
def withdraw():
    if not current_user.payout_ready:
        flash("Primero conecta tu usuario de Whop en «Mi cuenta».", "error")
        return redirect(url_for("account", _anchor="cobro"))
    with _withdraw_lock:
        lock_row(User, current_user.id)          # dos retiros a la vez del mismo clipper: el segundo espera y ve el primero
        m = user_money(current_user)
        if m["open_payout"] or Payout.query.filter(Payout.user_id == current_user.id,
                                                   Payout.status.in_(PAYOUT_OPEN)).first():
            flash("Ya tienes un retiro en curso. Podrás pedir otro cuando termine.", "error")
            return redirect(url_for("earnings", tab="retiros"))
        if m["available"] < CENT:
            flash("Aún no tienes saldo para retirar. Lo que ganas en una campaña se puede retirar cuando la campaña termina.", "error")
            return redirect(url_for("earnings"))
        if m["available"] < MIN_WITHDRAW:
            flash(f"El retiro mínimo es {money(MIN_WITHDRAW)}.", "error")
            return redirect(url_for("earnings", tab="retiros"))
        asked = (request.form.get("amount") or "").strip()
        amount = cents(parse_float(asked, 0) or 0) if asked else m["available"]
        if amount < 0.01:
            flash("Escribe cuánto quieres retirar.", "error")
            return redirect(url_for("earnings"))
        if amount > m["available"] + 1e-9:
            flash(f"Solo tienes {money(m['available'])} disponibles para retirar.", "error")
            return redirect(url_for("earnings"))
        if amount < MIN_WITHDRAW:
            flash(f"El retiro mínimo es {money(MIN_WITHDRAW)}.", "error")
            return redirect(url_for("earnings"))
        parts, left = [], amount               # se reparte entre los Whop de los creadores, empezando por el que más debe
        for cid, avail in sorted(m["by_creator"].items(), key=lambda kv: -kv[1]):
            take = cents(min(avail, left))
            if take >= max(MIN_WITHDRAW, CENT):
                parts.append((cid, take))
                left = cents(left - take)
            if left < CENT:
                break
        if not parts:
            flash(f"Cada pago por Whop tiene que ser de al menos {money(MIN_WITHDRAW)}. Prueba con una cantidad mayor.", "error")
            return redirect(url_for("earnings"))
        amount = cents(sum(t for _, t in parts))
        created = []
        auto_today = auto_paid_last_day()
        for cid, take in parts:
            auto = (wallet_ready(cid) and (WHOP_AUTO_MAX <= 0 or take <= WHOP_AUTO_MAX)
                    and (WHOP_AUTO_DAILY <= 0 or auto_today + take <= WHOP_AUTO_DAILY))
            if auto:
                auto_today += take
            p = Payout(user_id=current_user.id, creator_id=cid, amount=take, method=current_user.payout_method,
                       status="procesando" if auto else "solicitado",
                       note="" if auto or wallet_ready(cid) else "Esperando a que se conecte el Whop del creador",
                       details=f"@{current_user.whop_username} ({current_user.whop_user_id})")
            db.session.add(p)
            created.append((p, auto))
        db.session.commit()
    log.info("Retiro: user=%s amount=%s partes=%s", current_user.id, amount, len(created))
    results = []
    for p, auto in created:
        if auto:
            results.append(whop_pay(p))
            db.session.commit()
        else:
            results.append("espera")
    paid = cents(sum(p.amount for (p, _), r in zip(created, results) if r == "pagado"))
    waiting = cents(sum(p.amount for (p, _), r in zip(created, results) if r in ("procesando", "espera")))
    failed = cents(sum(p.amount for (p, _), r in zip(created, results) if r == "fallido"))
    if paid:
        notify_phone(f"✅ Pagado por Whop: {money(paid)}", f"{current_user.name} · @{current_user.whop_username}",
                     url_for("admin_payouts"), tags="white_check_mark")
    if any(r == "espera" for r in results):
        notify_phone(f"💸 Retiro de {money(waiting)} esperando", f"{current_user.name}: revisa Retiros en el panel.",
                     url_for("admin_payouts"), tags="money_with_wings")
    msgs = []
    if paid:
        msgs.append(f"¡Listo! Te hemos enviado {money(paid)} a tu cuenta de Whop (@{current_user.whop_username}).")
    processing = cents(sum(p.amount for (p, _), r in zip(created, results) if r == "procesando"))
    held = [p for (p, _), r in zip(created, results) if r == "espera"]
    if processing:
        msgs.append(f"Estamos enviando {money(processing)} a tu cuenta de Whop. Llegará en unos minutos.")
    if any(wallet_ready(p.creator_id) for p in held):
        msgs.append(f"Retiro de {money(sum(p.amount for p in held if wallet_ready(p.creator_id)))} solicitado. "
                    "Por ser una cantidad alta lo revisamos antes de enviarlo a tu Whop.")
    if any(not wallet_ready(p.creator_id) for p in held):
        msgs.append(f"{money(sum(p.amount for p in held if not wallet_ready(p.creator_id)))} quedan reservados para ti: "
                    "te los enviaremos en cuanto el creador de esa campaña conecte su Whop.")
    if failed:
        msgs.append(f"Whop no ha podido enviar {money(failed)}. No se te ha descontado: sigue en tu saldo.")
    flash(" ".join(msgs), "error" if failed and not paid and not waiting else "ok")
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
            flash(PAYOUT_ONLY_WHOP_MSG, "info")
            return redirect(url_for("account", _anchor="cobro"))
        if section == "redes":
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
def _per_request(key, fn):
    """Calcula algo una sola vez por petición (la campanita y el menú del panel piden lo mismo varias veces)."""
    if not has_request_context():
        return fn()
    memo = g.setdefault("_memo", {})
    if key not in memo:
        memo[key] = fn()
    return memo[key]


def review_queue(cid=None):
    """Clips pendientes repartidos en «por revisar» y «contando vistas»."""
    if cid is None:
        return _per_request("review_queue", lambda: _review_queue(None))
    return _review_queue(cid)


def _review_queue(cid):
    q = (Submission.query.filter_by(status="pendiente")
         .options(joinedload(Submission.user), joinedload(Submission.campaign)).order_by(Submission.created_at))
    if cid:
        q = q.filter(Submission.campaign_id == cid)
    rev, cnt = [], []
    for s_ in q.all():
        (rev if clip_stage(s_) == "revisar" else cnt).append(s_)
    return rev, cnt


def pending_by_campaign():
    """{campaña: {"clips": por revisar + apelaciones, "requests": solicitudes, "accounts": cuentas sin verificar}}."""
    return _per_request("pending_by_campaign", _pending_by_campaign)


def _pending_by_campaign():
    out = {}
    rev, _ = review_queue()
    for s_ in rev:
        out.setdefault(s_.campaign_id, {"clips": 0, "requests": 0, "accounts": 0})["clips"] += 1
    for cid, n in (db.session.query(Submission.campaign_id, db.func.count(Submission.id))
                   .filter(Submission.appeal_status == "pendiente").group_by(Submission.campaign_id)):
        out.setdefault(cid, {"clips": 0, "requests": 0, "accounts": 0})["clips"] += n
    for cid, n in (db.session.query(CampaignMember.campaign_id, db.func.count(CampaignMember.id))
                   .filter(CampaignMember.status == "pendiente").group_by(CampaignMember.campaign_id)):
        out.setdefault(cid, {"clips": 0, "requests": 0, "accounts": 0})["requests"] = n
    for cid, n in (db.session.query(CampaignAccount.campaign_id, db.func.count(CampaignAccount.id))
                   .filter(CampaignAccount.status == "pendiente").group_by(CampaignAccount.campaign_id)):
        out.setdefault(cid, {"clips": 0, "requests": 0, "accounts": 0})["accounts"] = n
    for cid, n in (db.session.query(CampMessage.campaign_id, db.func.count(db.distinct(CampMessage.user_id)))
                   .filter(CampMessage.from_staff.is_(False), CampMessage.read_by_staff.is_(False))
                   .group_by(CampMessage.campaign_id)):
        out.setdefault(cid, {"clips": 0, "requests": 0, "accounts": 0})["support"] = n
    return out


def review_queue_count():
    return len(review_queue()[0]) + Submission.query.filter_by(appeal_status="pendiente").count()


def admin_counts():
    """Números del menú lateral del panel: lo que está esperando a que hagas algo."""
    return _per_request("admin_counts", _admin_counts)


def _admin_counts():
    req, clips = CampaignMember.query.filter_by(status="pendiente").count(), review_queue_count()
    accs = CampaignAccount.query.filter_by(status="pendiente").count()
    sup = (db.session.query(db.func.count(db.distinct(CampMessage.user_id)))
           .filter(CampMessage.from_staff.is_(False), CampMessage.read_by_staff.is_(False)).scalar() or 0)
    return {
        "requests": req,
        "clips": clips,
        "accounts": accs,
        "payouts": Payout.query.filter(Payout.status.in_(PAYOUT_OPEN)).count(),
        "messages": Message.query.filter_by(from_admin=False, read_by_admin=False).count(),
        "leads": CampaignLead.query.filter_by(status="nueva").count(),
        "creators_alert": 0,
        "camps": req + clips + accs + sup,
        "tech": tech_alert_count(),
    }


def daily_series(rows, days=30):
    """[(fecha, valor)] → lista de (día, total) de los últimos `days` días, con ceros donde no hubo nada."""
    today = datetime.utcnow().date()
    totals = {today - timedelta(days=i): 0.0 for i in range(days)}
    for when, value in rows:
        d = when.date() if when else None
        if d in totals:
            totals[d] += value or 0
    return [(d, totals[d]) for d in sorted(totals)]


def views_at(subs, snaps, when):
    """Vistas que tenían los clips en un momento dado (la última lectura anterior; 0 si aún no existían)."""
    total = 0
    for s_ in subs:
        if s_.created_at > when:
            continue
        last = 0
        for t, v in snaps.get(s_.id, ()):
            if t <= when:
                last = v
            else:
                break
        total += last
    return total


def views_week():
    """Vistas conseguidas en los últimos 7 días y el cambio frente a los 7 anteriores."""
    subs = Submission.query.filter(Submission.status == "aprobado").all()
    if not subs:
        return {"views": 0, "change": None}
    now = datetime.utcnow()
    snaps = {}
    for sn in (db.session.query(ViewSnapshot.submission_id, ViewSnapshot.taken_at, ViewSnapshot.views)
               .filter(ViewSnapshot.submission_id.in_([x.id for x in subs])).order_by(ViewSnapshot.taken_at)):
        snaps.setdefault(sn[0], []).append((sn[1], sn[2] or 0))
    a, b, c = (views_at(subs, snaps, now - timedelta(days=14)), views_at(subs, snaps, now - timedelta(days=7)),
               sum(x.views or 0 for x in subs))
    this, prev = max(0, c - b), max(0, b - a)
    return {"views": this, "change": round((this - prev) / prev * 100) if prev else None}


def admin_kpis(camps):
    open_payouts = Payout.query.filter(Payout.status.in_(PAYOUT_OPEN)).all()
    return {
        "budget_live": cents(sum(c.budget for c in camps if c.status == "activa")),
        "spent": cents(sum(c.spent or 0 for c in camps)),
        "paid": cents(db.session.query(db.func.coalesce(db.func.sum(Payout.amount), 0)).filter(Payout.status == "pagado").scalar()),
        "open_payouts": cents(sum(p.amount for p in open_payouts)),
        "open_payouts_n": len(open_payouts),
        "week": views_week(),
        "views": db.session.query(db.func.coalesce(db.func.sum(Submission.views), 0)).filter(Submission.status == "aprobado").scalar() or 0,
        "live_camps": sum(1 for c in camps if c.status == "activa"),
    }


@app.route("/admin")
@admin_required
def admin_home():
    camps = Campaign.query.options(selectinload(Campaign.submissions)).order_by(Campaign.created_at.desc()).all()
    cards = [(c, campaign_stats(c)) for c in camps]
    since = datetime.utcnow() - timedelta(days=31)
    clips_day = daily_series([(s.created_at, 1) for s in Submission.query.filter(Submission.created_at >= since)
                              .with_entities(Submission.created_at)])
    paid_day = daily_series(Payout.query.filter(Payout.status == "pagado", Payout.paid_at >= since)
                            .with_entities(Payout.paid_at, Payout.amount).all())
    users_day = daily_series([(u.created_at, 1) for u in User.query.filter(User.created_at >= since).with_entities(User.created_at)])
    nc = admin_counts()
    alerts = []
    names = {c.id: c.song for c in camps}
    for cid_, pd in sorted(pending_by_campaign().items(), key=lambda kv: -(kv[1]["clips"] + kv[1]["requests"])):
        if pd["clips"]:
            alerts.append(("film", f"«{names.get(cid_, '')}»: {pd['clips']} {'clip' if pd['clips'] == 1 else 'clips'} por revisar",
                           url_for("admin_campaign_review", cid=cid_)))
        n = pd["requests"] + pd["accounts"]
        if n:
            alerts.append(("inbox", f"«{names.get(cid_, '')}»: {n} {'solicitud' if n == 1 else 'solicitudes'}",
                           url_for("admin_campaign_requests", cid=cid_)))
        if pd.get("support"):
            alerts.append(("chat", f"«{names.get(cid_, '')}»: {pd['support']} {'consulta' if pd['support'] == 1 else 'consultas'} en soporte",
                           url_for("admin_campaign_support", cid=cid_)))
    if nc["payouts"]:
        alerts.append(("cash", f"{nc['payouts']} {'retiro' if nc['payouts'] == 1 else 'retiros'} en curso", url_for("admin_payouts")))
    failed = Payout.query.filter(Payout.status == "rechazado", Payout.error != "",
                                 Payout.created_at >= datetime.utcnow() - timedelta(days=7)).count()
    if failed:
        alerts.append(("cash", f"{failed} {'pago' if failed == 1 else 'pagos'} que Whop no pudo enviar esta semana", url_for("admin_payouts")))
    for c, st in cards:
        if c.status == "activa" and st["left_pct"] <= 20:
            alerts.append(("flag", f"«{c.song}» está a punto de agotarse: queda el {st['left_pct']}%", url_for("admin_campaign", cid=c.id)))
    if nc["messages"]:
        alerts.append(("chat", f"{nc['messages']} {'mensaje' if nc['messages'] == 1 else 'mensajes'} sin leer", url_for("admin_inbox")))
    if not viewcounter.APIFY_TOKEN:
        alerts.append(("gear", "Falta APIFY_TOKEN: las vistas no se están contando", url_for("admin_settings")))
    live = [(c, st) for c, st in cards if c.status == "activa"]
    return render_template("admin/overview.html", kpis=admin_kpis(camps), alerts=alerts, live=live[:6],
                           clips_day=clips_day, paid_day=paid_day, users_day=users_day)


@app.route("/admin/campanas")
@admin_required
def admin_campaigns():
    camps = Campaign.query.options(selectinload(Campaign.submissions)).order_by(Campaign.created_at.desc()).all()
    filt = request.args.get("estado", "")
    if filt in ("activa", "pausada", "finalizada"):
        camps = [c for c in camps if c.status == filt]
    return render_template("admin/campaigns.html", cards=[(c, campaign_stats(c)) for c in camps], filt=filt,
                           pend=pending_by_campaign())


@app.route("/admin/solicitudes-clippers")
@admin_required
def admin_requests():
    return requests_page()


def requests_page(cid=None):
    estado = request.args.get("estado", "pendiente")
    estado = estado if estado in ("pendiente", "aceptada", "rechazada") else "pendiente"
    q = (CampaignMember.query.filter_by(status=estado)
         .options(joinedload(CampaignMember.user), joinedload(CampaignMember.campaign)))
    camp, accounts = None, []
    if cid:
        camp = db.session.get(Campaign, cid) or abort(404)
        q = q.filter(CampaignMember.campaign_id == cid)
        accounts = (CampaignAccount.query.filter_by(campaign_id=cid, status="pendiente")
                    .options(joinedload(CampaignAccount.user)).order_by(CampaignAccount.created_at).all())
    joins = q.order_by(CampaignMember.created_at.desc() if estado != "pendiente" else CampaignMember.created_at).limit(300).all()
    return render_template("admin/requests.html", joins=joins, estado=estado, camp=camp, accounts=accounts,
                           pend=pending_by_campaign().get(cid, {}) if cid else {}, sec="solicitudes")


@app.route("/admin/campana/<int:cid>/solicitudes")
@staff_required(lambda kw: kw.get("cid"))
def admin_campaign_requests(cid):
    return requests_page(cid)


@app.route("/admin/campana/<int:cid>/revisar")
@staff_required(lambda kw: kw.get("cid"))
def admin_campaign_review(cid):
    db.session.get(Campaign, cid) or abort(404)
    return clips_page(cid)


@app.route("/admin/campana/<int:cid>/clippers")
@staff_required(lambda kw: kw.get("cid"))
def admin_campaign_clippers(cid):
    """Los clippers aceptados en la campaña con lo que llevan en ella."""
    camp = db.session.get(Campaign, cid) or abort(404)
    members = (CampaignMember.query.filter_by(campaign_id=cid, status="aceptada")
               .options(joinedload(CampaignMember.user)).all())
    stats = {}
    for s_ in Submission.query.filter_by(campaign_id=cid).all():
        d = stats.setdefault(s_.user_id, {"clips": 0, "approved": 0, "views": 0, "earned": 0.0, "last": None})
        d["clips"] += 1
        if s_.status == "aprobado":
            d["approved"] += 1
            d["views"] += s_.views or 0
            d["earned"] = cents(d["earned"] + (s_.earned or 0))
        d["last"] = max(d["last"] or s_.created_at, s_.created_at)
    rows = sorted(((m, stats.get(m.user_id, {"clips": 0, "approved": 0, "views": 0, "earned": 0.0, "last": None}),
                    campaign_accounts(m.user_id, cid)) for m in members), key=lambda r: -r[1]["views"])
    mods = CampaignMod.query.filter_by(campaign_id=cid).options(joinedload(CampaignMod.user)).all()
    return render_template("admin/camp_clippers.html", camp=camp, rows=rows, pend=pending_by_campaign().get(cid, {}),
                           sec="clippers", mods=mods, mod_ids={m_.user_id for m_ in mods})


@app.route("/admin/mods", methods=["GET", "POST"])
@admin_required
def admin_mods():
    """Todos los mods en un sitio: en qué campañas están, qué pueden hacer y cuánto trabajan."""
    if request.method == "POST":
        act = request.form.get("act", "")
        chosen = set(request.form.getlist("perms"))
        perms = [p_ for p_ in MOD_PERMS if p_ in chosen]
        perm_str = ",".join(perms)
        if act == "anadir":
            email = (request.form.get("email") or "").strip().lower()
            u = User.query.filter(db.func.lower(User.email) == email).first() if email else None
            cids = [int(x) for x in request.form.getlist("campaigns") if x.isdigit()]
            if not u:
                flash("No hay ningún usuario con ese correo. Tiene que haberse registrado antes.", "error")
            elif u.is_admin:
                flash(f"{u.name} ya es del equipo: puede hacerlo todo.", "info")
            elif u.is_banned:
                flash("No puedes hacer mod a un usuario bloqueado.", "error")
            elif not cids:
                flash("Elige al menos una campaña.", "error")
            elif not perms:
                flash("Marca al menos un permiso.", "error")
            else:
                n = 0
                for camp in Campaign.query.filter(Campaign.id.in_(cids)).all():
                    row = CampaignMod.query.filter_by(campaign_id=camp.id, user_id=u.id).first()
                    if row:
                        row.perms = perm_str
                        continue
                    db.session.add(CampaignMod(campaign_id=camp.id, user_id=u.id, perms=perm_str))
                    notify_user(u.id, "mod", f"Ahora eres mod de {camp.song}",
                                "Te han dado permisos de moderador en esta campaña. Entra desde la campaña o desde tu menú.",
                                f"/campana/{camp.id}", camp_id=camp.id)
                    n += 1
                audit("mod_añadido", f"{u.email} · {len(cids)} campañas · {perm_str or 'todos'}")
                flash(f"{u.name} es mod en {n} {'campaña nueva' if n == 1 else 'campañas nuevas'}.", "ok")
        elif act == "permisos":
            row = db.session.get(CampaignMod, request.form.get("mid", type=int) or 0) or abort(404)
            if not perms:
                flash("Marca al menos un permiso (o quita al mod de esa campaña).", "error")
            else:
                row.perms = perm_str
                audit("mod_permisos", f"{row.user.email} · «{row.campaign.song}» · {perm_str or 'todos'}")
                flash(f"Permisos de {row.user.name} en «{row.campaign.song}» guardados.", "ok")
        elif act == "quitar":
            row = db.session.get(CampaignMod, request.form.get("mid", type=int) or 0) or abort(404)
            audit("mod_quitado", f"{row.user.email} · «{row.campaign.song}»")
            flash(f"{row.user.name} ya no es mod de «{row.campaign.song}».", "ok")
            db.session.delete(row)
        elif act == "quitar_todo":
            uid = request.form.get("uid", type=int) or 0
            rows = CampaignMod.query.filter_by(user_id=uid).all()
            for row in rows:
                db.session.delete(row)
            u = db.session.get(User, uid)
            audit("mod_quitado", f"{u.email if u else uid} · todas las campañas")
            flash("Ya no es mod de ninguna campaña.", "ok")
        else:
            abort(400)
        db.session.commit()
        return redirect(url_for("admin_mods"))
    rows = CampaignMod.query.options(joinedload(CampaignMod.user), joinedload(CampaignMod.campaign)).all()
    week = datetime.utcnow() - timedelta(days=7)
    mods = {}
    for r in rows:
        if not r.user or not r.campaign:
            continue
        mods.setdefault(r.user_id, {"user": r.user, "rows": []})["rows"].append(r)
    for uid, m in mods.items():
        acts = dict(db.session.query(AuditLog.action, db.func.count(AuditLog.id))
                    .filter(AuditLog.admin_id == uid, AuditLog.created_at >= week).group_by(AuditLog.action).all())
        m["activity"] = {
            "clips": acts.get("clip_aprobado", 0) + acts.get("clip_rechazado", 0),
            "requests": acts.get("clipper_aceptado", 0) + acts.get("clipper_rechazado", 0),
            "support": CampMessage.query.filter(CampMessage.sender_id == uid, CampMessage.from_staff.is_(True),
                                                CampMessage.created_at >= week).count(),
            "posts": Post.query.filter(Post.author_id == uid, Post.created_at >= week).count(),
        }
    camps = Campaign.query.filter(Campaign.status != "finalizada").order_by(Campaign.created_at.desc()).all()
    return render_template("admin/mods.html", mods=sorted(mods.values(), key=lambda m: m["user"].name.lower()),
                           camps=camps, perms=MOD_PERMS, defaults=MOD_DEFAULT, risky=MOD_RISKY)


@app.route("/campana/<int:cid>/soporte", methods=["POST"])
@login_required
def campaign_support_send(cid):
    """El clipper escribe al equipo de la campaña."""
    camp = db.session.get(Campaign, cid) or abort(404)
    if current_user.is_admin or not is_campaign_member(current_user, camp):
        abort(403)
    body = (request.form.get("body") or "").strip()[:2000]
    if body:
        recent = CampMessage.query.filter(CampMessage.user_id == current_user.id,
                                          CampMessage.created_at >= datetime.utcnow() - timedelta(minutes=10)).count()
        if recent >= 20:
            flash("Has escrito muchos mensajes seguidos. Espera un poco.", "error")
        else:
            db.session.add(CampMessage(campaign_id=cid, user_id=current_user.id, sender_id=current_user.id, body=body,
                                       read_by_user=True))
            for m_ in CampaignMod.query.filter_by(campaign_id=cid).all():
                if m_.user_id != current_user.id and "soporte" in m_.perm_set:
                    notify_user(m_.user_id, "mensaje", f"Soporte de {camp.song}: {current_user.name}", body[:200],
                                url_for("admin_campaign_support", cid=cid, u=current_user.id), camp_id=cid)
            notify_phone(f"Soporte {camp.song}", f"{current_user.name}: {body[:200]}",
                         url_for("admin_campaign_support", cid=cid, u=current_user.id))
            db.session.commit()
    return redirect(url_for("campaign", cid=cid, soporte=1, _anchor="fin"))


@app.route("/admin/campana/<int:cid>/soporte", methods=["GET", "POST"])
@staff_required(lambda kw: kw.get("cid"))
def admin_campaign_support(cid):
    """Bandeja de soporte de la campaña: la ven y responden los admins y los mods de esa campaña."""
    camp = db.session.get(Campaign, cid) or abort(404)
    uid = request.values.get("u", type=int)
    if request.method == "POST":
        body = (request.form.get("body") or "").strip()[:2000]
        u = db.session.get(User, uid or 0)
        if not u or not CampMessage.query.filter_by(campaign_id=cid, user_id=u.id).first():
            abort(404)
        if body:
            db.session.add(CampMessage(campaign_id=cid, user_id=u.id, sender_id=current_user.id, from_staff=True, body=body,
                                       read_by_staff=True))
            CampMessage.query.filter_by(campaign_id=cid, user_id=u.id, from_staff=False).update({"read_by_staff": True})
            notify_user(u.id, "mensaje", f"Respuesta del equipo · {camp.song}", body[:200],
                        url_for("campaign", cid=cid, soporte=1), camp_id=cid)
            db.session.commit()
        return redirect(url_for("admin_campaign_support", cid=cid, u=u.id, _anchor="fin"))
    rows = CampMessage.query.filter_by(campaign_id=cid).order_by(CampMessage.created_at.desc()).all()
    threads, seen = [], set()
    for m_ in rows:
        if m_.user_id in seen:
            continue
        seen.add(m_.user_id)
        unread = sum(1 for x in rows if x.user_id == m_.user_id and not x.from_staff and not x.read_by_staff)
        threads.append({"user": m_.user, "last": m_, "unread": unread})
    msgs, who = [], None
    if uid:
        who = db.session.get(User, uid)
        msgs = sorted((m_ for m_ in rows if m_.user_id == uid), key=lambda m_: m_.created_at)
        if any(not m_.from_staff and not m_.read_by_staff for m_ in msgs):
            CampMessage.query.filter_by(campaign_id=cid, user_id=uid, from_staff=False).update({"read_by_staff": True})
            db.session.commit()
    return render_template("admin/camp_support.html", camp=camp, threads=threads, msgs=msgs, who=who,
                           mod_ids=campaign_mod_ids(cid), pend=pending_by_campaign().get(cid, {}), sec="soporte")


@app.route("/admin/campana/<int:cid>/mods", methods=["POST"])
@admin_required
def admin_campaign_mods(cid):
    """Añadir o quitar moderadores de una campaña (por clipper de la lista o por correo)."""
    camp = db.session.get(Campaign, cid) or abort(404)
    act = request.form.get("act", "")
    uid = request.form.get("uid", type=int)
    email = (request.form.get("email") or "").strip().lower()
    u = db.session.get(User, uid) if uid else (User.query.filter(db.func.lower(User.email) == email).first() if email else None)
    if not u:
        flash("No hay ningún usuario con ese correo. Tiene que haberse registrado antes.", "error")
        return redirect(url_for("admin_mods"))
    row = CampaignMod.query.filter_by(campaign_id=cid, user_id=u.id).first()
    if act == "quitar":
        if row:
            db.session.delete(row)
            audit("mod_quitado", f"{u.email} · «{camp.song}»")
            flash(f"{u.name} ya no es mod de «{camp.song}».", "ok")
    else:
        if u.is_admin:
            flash(f"{u.name} ya es del equipo: puede hacerlo todo.", "info")
        elif u.is_banned:
            flash("No puedes hacer mod a un usuario bloqueado.", "error")
        elif not row:
            db.session.add(CampaignMod(campaign_id=cid, user_id=u.id))
            notify_user(u.id, "mod", f"Ahora eres mod de {camp.song}",
                        "Puedes publicar en los canales, moderar comentarios, revisar clips y aceptar solicitudes.",
                        url_for("admin_campaign_review", cid=cid), camp_id=cid)
            audit("mod_añadido", f"{u.email} · «{camp.song}»")
            flash(f"{u.name} es ahora mod de «{camp.song}».", "ok")
    db.session.commit()
    return redirect(url_for("admin_mods"))


REVIEW_SLA_HOURS = int(os.environ.get("REVIEW_SLA_HOURS", "48"))   # a partir de aquí un clip sale como «atrasado»
REVIEW_FILTERS = {"todos": ("Todos", "grid"), "atrasados": ("Atrasados", "clock"), "avisos": ("Con avisos", "flag"),
                  "tiktok": ("TikTok", "tiktok"), "instagram": ("Instagram", "instagram"),
                  "youtube": ("YouTube", "youtube"), "facebook": ("Facebook", "facebook")}


def waiting_hours(sub):
    since = sub.appeal_at if sub.appeal_status == "pendiente" else (sub.review_since or sub.created_at)
    return (datetime.utcnow() - since).total_seconds() / 3600


def clip_series(subs, days=30):
    """Vistas, me gusta, comentarios y compartidos de cada clip, día a día (acumulado), para la gráfica de la revisión."""
    if not subs:
        return {}
    snaps = {}
    for sn in (ViewSnapshot.query.filter(ViewSnapshot.submission_id.in_([x.id for x in subs]))
               .order_by(ViewSnapshot.taken_at).all()):
        snaps.setdefault(sn.submission_id, []).append(sn)
    today = datetime.utcnow().date()
    out = {}
    for s_ in subs:
        start = max(s_.created_at.date(), today - timedelta(days=days - 1))
        n = (today - start).days + 1
        cur = {"views": s_.views or 0, "likes": s_.likes or 0, "comments": s_.comments or 0, "shares": s_.shares or 0}
        ser = {m: [0] * n for m in cur}
        pts = [(sn.taken_at.date(), sn) for sn in snaps.get(s_.id, [])]
        j, last = 0, {m: 0 for m in cur}
        for i in range(n):
            d = start + timedelta(days=i)
            while j < len(pts) and pts[j][0] <= d:
                sn = pts[j][1]
                last = {"views": sn.views or 0, "likes": sn.likes or 0, "comments": sn.comments or 0, "shares": sn.shares or 0}
                j += 1
            for m in cur:
                ser[m][i] = last[m] if i < n - 1 else cur[m]
        out[s_.id] = {"labels": [(start + timedelta(days=i)).isoformat() for i in range(n)], **ser}
    return out


def clip_growth(subs, hours=24):
    """Lo que ha subido cada clip en las últimas 24 horas (vistas, me gusta, comentarios y compartidos)."""
    out = {s_.id: {"views": 0, "likes": 0, "comments": 0, "shares": 0} for s_ in subs}
    if not subs:
        return out
    since = datetime.utcnow() - timedelta(hours=hours)
    before = {}
    for sn in (ViewSnapshot.query.filter(ViewSnapshot.submission_id.in_([x.id for x in subs]), ViewSnapshot.taken_at <= since)
               .order_by(ViewSnapshot.taken_at).all()):
        before[sn.submission_id] = sn                     # la última lectura de hace más de 24 h
    for s_ in subs:
        old = before.get(s_.id)
        if s_.created_at > since and not old:            # clip de hoy: todo lo que tiene es de las últimas 24 h
            base = {"views": 0, "likes": 0, "comments": 0, "shares": 0}
        elif old:
            base = {"views": old.views or 0, "likes": old.likes or 0, "comments": old.comments or 0, "shares": old.shares or 0}
        else:
            continue
        for m in base:
            out[s_.id][m] = max(0, (getattr(s_, m) or 0) - base[m])
    return out


def bot_check(sub):
    """Texto de la línea «Detección de bots» de cada clip, a partir de sus avisos y su interacción."""
    v = sub.views or 0
    if "subida_rapida" in sub.flag_list:
        return "alerta", "Subida de vistas muy rápida en pocas horas: revisa que no sean vistas compradas."
    if "pocos_likes" in sub.flag_list:
        return "alerta", "Los me gusta no han ido acordes con las visualizaciones: posible actividad de bots."
    if "cuenta_de_otro" in sub.flag_list:
        return "alerta", "El vídeo es de una cuenta vinculada a otro clipper."
    if v >= 5000 and sub.comments is not None and sub.comments == 0:
        return "aviso", "Ningún comentario con muchas vistas: no confirmado como bots, pero conviene mirarlo."
    if v and sub.likes is not None:
        return "ok", f"Interacción normal ({(sub.likes + (sub.comments or 0) + (sub.shares or 0)) / v * 100:.1f} %): sin señales de bots."
    return "ok", "Aún sin datos suficientes para analizarlo."


@app.route("/admin/clips")
@admin_required
def admin_clips():
    return clips_page()


def clips_page(camp_id=None):
    cid = camp_id or request.args.get("c", type=int)
    tab = request.args.get("tab", "revisar")
    tab = tab if tab in ("revisar", "contando", "apelaciones", "aprobados", "rechazados") else "revisar"
    f = request.args.get("f", "todos")
    f = f if f in REVIEW_FILTERS else "todos"
    q = (request.args.get("q") or "").strip().lower().lstrip("@")
    rev, cnt = review_queue(cid)
    if acting_as_mod():                               # un mod no ve (ni revisa) sus propios clips
        rev = [s_ for s_ in rev if s_.user_id != current_user.id]
        cnt = [s_ for s_ in cnt if s_.user_id != current_user.id]
    aq = (Submission.query.filter_by(appeal_status="pendiente")
          .options(joinedload(Submission.user), joinedload(Submission.campaign)).order_by(Submission.appeal_at))
    if cid:
        aq = aq.filter(Submission.campaign_id == cid)
    appeals = [s_ for s_ in aq.all() if not (acting_as_mod() and s_.user_id == current_user.id)]
    done = {"aprobados": [], "rechazados": []}
    if tab in done or cid:
        dq = (Submission.query.filter(Submission.status.in_(("aprobado", "rechazado")))
              .options(joinedload(Submission.user), joinedload(Submission.campaign)))
        if cid:
            dq = dq.filter(Submission.campaign_id == cid)
        for s_ in dq.order_by(Submission.id.desc()).limit(500):
            if acting_as_mod() and s_.user_id == current_user.id:
                continue
            done["aprobados" if s_.status == "aprobado" else "rechazados"].append(s_)
    rows = {"revisar": rev, "contando": cnt, "apelaciones": appeals, **done}[tab]
    if tab not in done:
        rows = sorted(rows, key=lambda s_: -waiting_hours(s_))        # lo que más espera, primero
    counts = {k: 0 for k in REVIEW_FILTERS}
    for s_ in rows:
        counts["todos"] += 1
        if tab != "contando" and waiting_hours(s_) >= REVIEW_SLA_HOURS:
            counts["atrasados"] += 1
        if s_.flag_list:
            counts["avisos"] += 1
        counts[s_.platform] = counts.get(s_.platform, 0) + 1
    if f == "atrasados":
        rows = [s_ for s_ in rows if waiting_hours(s_) >= REVIEW_SLA_HOURS]
    elif f == "avisos" or request.args.get("avisos"):
        rows = [s_ for s_ in rows if s_.flag_list]
    elif f in PLATFORMS:
        rows = [s_ for s_ in rows if s_.platform == f]
    if q:
        rows = [s_ for s_ in rows if q in (s_.user.name or "").lower() or q in (s_.author or "").lower()
                or q in (s_.user.email or "").lower() or q in (s_.campaign.song or "").lower()]
    page = max(request.args.get("p", 1, type=int), 1)
    per = 12
    total = len(rows)
    shown = rows[: page * per]
    camps = Campaign.query.order_by(Campaign.created_at.desc()).all() if not acting_as_mod() else []
    camp = db.session.get(Campaign, cid) if cid else None
    return render_template("admin/clips.html", rows=shown, total=total, more=total > len(shown), page=page, tab=tab, f=f,
                           q=request.args.get("q", ""), camps=camps, cid=cid, camp=camp, counts=counts,
                           n={"revisar": len(rev), "contando": len(cnt), "apelaciones": len(appeals),
                              "aprobados": len(done["aprobados"]), "rechazados": len(done["rechazados"])},
                           growth=clip_growth(shown), players={s_.id: clip_player(s_) for s_ in shown},
                           bots={s_.id: bot_check(s_) for s_ in shown}, hours={s_.id: waiting_hours(s_) for s_ in shown},
                           sla=REVIEW_SLA_HOURS, filters=REVIEW_FILTERS, in_camp=bool(camp_id),
                           pend=pending_by_campaign().get(cid, {}) if camp_id else {}, sec="revisar")


@app.route("/admin/retiros")
@admin_required
def admin_payouts():
    whop_sync(Payout.query.filter_by(status="procesando").all())
    release_stuck_payouts()
    open_requests = (Payout.query.filter(Payout.status.in_(PAYOUT_OPEN)).options(joinedload(Payout.user))
                     .order_by(Payout.created_at).all())
    failed = (Payout.query.filter(Payout.status == "rechazado", Payout.error != "").options(joinedload(Payout.user))
              .order_by(Payout.created_at.desc()).limit(10).all())
    history = (Payout.query.filter(Payout.status == "pagado").options(joinedload(Payout.user))
               .order_by(Payout.paid_at.desc()).limit(100).all())
    return render_template("admin/payouts.html", open_requests=open_requests, failed=failed, history=history)


@app.route("/admin/ajustes")
@admin_required
def admin_settings(app_test=None):
    whop = {"key": bool(WHOP_API_KEY), "company": whop_company_id() if WHOP_API_KEY else "",
            "error": _whop_company.get("error", ""), "from_env": bool(WHOP_COMPANY_ID)}
    return render_template("admin/settings.html", whop=whop, last_refresh=get_setting("last_refresh", "Nunca"), allow_wipe=ALLOW_WIPE,
                           alerts=tech_alerts(), creators_on=CREATORS_ENABLED, tech_set=bool(TECH_EMAILS),
                           has_token=bool(viewcounter.APIFY_TOKEN), has_app_key=bool(WHOP_APP_API_KEY), app_test=app_test,
                           demo_n=User.query.filter(User.email.like(f"%{DEMO_DOMAIN}")).count(),
                           my_demo_n=Submission.query.filter_by(user_id=current_user.id, my_demo=True).count())


DEMO_NAMES = ["Lucía Fernández", "Mateo Rodríguez", "Valentina Gómez", "Hugo Martín", "Camila Torres", "Leo Sánchez",
              "Sofía Ramírez", "Daniel Ruiz", "Martina López", "Pablo Navarro", "Isabella Castro", "Álvaro Moreno",
              "Emma Herrera", "Diego Vargas", "Julia Ortega", "Thiago Medina", "Paula Romero", "Bruno Silva",
              "Carla Molina", "Adrián Rojas", "Lola Delgado", "Gael Morales", "Noa Jiménez", "Iker Peña", "Elena Ríos"]
DEMO_COUNTRIES = ["ES", "ES", "ES", "MX", "MX", "AR", "CO", "CL", "PE", "ES", "MX", "ES", "AR", "CO", "ES",
                  "AR", "ES", "UY", "ES", "CL", "ES", "MX", "ES", "ES", "VE"]
DEMO_ANSWERS = [   # (palabra de la pregunta, respuestas posibles): cada respuesta encaja con su pregunta
    ("tiempo", ["Llevo un año haciendo clips de streamers", "Unos 6 meses", "Empecé este verano, pero subo todos los días"]),
    ("enlace", ["https://www.tiktok.com/@miscuts/video/7301234567890", "Mi mejor clip tiene 1,2M: https://www.tiktok.com/@clipsdiarios/video/7309876543210"]),
    ("día", ["3 al día", "Entre 2 y 4", "5 o más si hay directo largo"]),
    ("redes", ["TikTok e Instagram", "TikTok, Instagram y YouTube Shorts", "Sobre todo TikTok"]),
]


def demo_answers(questions, r):
    out = []
    for q in questions:
        opts = next((a for k, a in DEMO_ANSWERS if k in q.lower()), ["Sí, sin problema"])
        out.append([q, r.choice(opts)])
    return json.dumps(out, ensure_ascii=False)


def _demo_avatar(name, seed):
    """Foto de perfil inventada: degradado de color con la inicial (sin fotos de personas reales)."""
    from PIL import Image, ImageDraw
    import colorsys
    import random as rnd
    r = rnd.Random(seed)
    h = r.random()
    c1 = tuple(int(x * 255) for x in colorsys.hsv_to_rgb(h, .55, .95))
    c2 = tuple(int(x * 255) for x in colorsys.hsv_to_rgb((h + .12) % 1, .75, .65))
    img = Image.new("RGB", (256, 256))
    d = ImageDraw.Draw(img)
    for y in range(256):
        t = y / 255
        d.line([(0, y), (256, y)], fill=tuple(int(c1[i] * (1 - t) + c2[i] * t) for i in range(3)))
    try:
        from PIL import ImageFont
        font = ImageFont.load_default(size=120)
    except (TypeError, OSError):
        font = None
    d.text((128, 128), name[:1].upper(), fill="white", anchor="mm", font=font)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85)
    return out.getvalue()


def load_demo(days=30, seed=7):
    """Llena la web con clippers y estadísticas de demostración de los últimos días. Se quita con remove_demo()."""
    import random as rnd
    r = rnd.Random(seed)
    now = datetime.utcnow()
    camps = [c for c in Campaign.query.order_by(Campaign.id).all() if c.status != "finalizada" and not c.exhausted]
    if not camps:
        return "No hay campañas activas con presupuesto: crea alguna antes."
    users = []
    for i, name in enumerate(DEMO_NAMES):
        handle = viewcounter.clean_handle(name.split()[0] + "." + name.split()[1][:4] + str(r.randint(1, 99)))
        handle = handle.encode("ascii", "ignore").decode() or f"clipper{i}"
        u = User(email=f"demo{i + 1}{DEMO_DOMAIN}", name=name, country=DEMO_COUNTRIES[i % len(DEMO_COUNTRIES)],
                 languages="es", created_at=now - timedelta(days=days + r.randint(1, 15), hours=r.randint(0, 23)),
                 onboarded_at=now - timedelta(days=days), accepted_terms_at=now - timedelta(days=days),
                 last_seen_at=now - timedelta(hours=r.randint(1, 70)), tiktok_handles=handle,
                 instagram_handles=handle if r.random() < .6 else "", youtube_handles=handle if r.random() < .35 else "",
                 trusted=r.random() < .2)
        db.session.add(u)
        db.session.flush()
        u.avatar_data, u.avatar_ver = _demo_avatar(name, i), 1
        u.avatar_url = f"/foto/{u.id}?v=1"
        users.append((u, handle, 0.4 + r.random() * 2.2))        # cada uno con su «talento»
    db.session.flush()
    plan = []                                                  # (sub, vistas finales)
    n = 0
    for u, handle, skill in users:
        mine = r.sample(camps, k=min(len(camps), r.randint(1, 3)))
        for c in mine:
            joined = now - timedelta(days=days - r.randint(0, 6), hours=r.randint(0, 23))
            db.session.add(CampaignMember(user_id=u.id, campaign_id=c.id, status="aceptada", created_at=joined,
                                          decided_at=joined + timedelta(hours=r.randint(1, 20)),
                                          answers=demo_answers(c.question_list, r)))
            plats = [p for p in c.platform_list if u.handles(p)] or c.platform_list[:1]
            for p in plats:
                db.session.add(CampaignAccount(user_id=u.id, campaign_id=c.id, platform=p, handle=handle, status="aceptada",
                                               bio_verified=True, bio_status="Verificada", created_at=joined, decided_at=joined))
            for _ in range(r.randint(2, 7)):
                created = joined + timedelta(hours=r.randint(6, max(7, int((now - joined).total_seconds() // 3600) - 2)))
                if created > now - timedelta(hours=3):
                    created = now - timedelta(hours=r.randint(3, 40))
                p = r.choice(plats)
                n += 1
                vid = str(7400000000000000000 + seed * 100000 + n) if p == "tiktok" else f"DEMO{seed}x{n}"
                url = {"tiktok": f"https://www.tiktok.com/@{handle}/video/{vid}",
                       "instagram": f"https://www.instagram.com/reel/{vid}/",
                       "youtube": f"https://www.youtube.com/shorts/{vid}"}[p]
                age = (now - created).total_seconds() / 86400
                viral = r.random() < .08
                final = int(r.lognormvariate(9.6, 1.1) * skill * (8 if viral else 1) * min(1, .35 + age / 6))
                minv = c.min_views_for(p)
                if minv and final < minv and age >= 2 and r.random() < .75:       # la mayoría de los antiguos llegan al mínimo
                    final = int(minv * r.uniform(1.1, 5))
                s_ = Submission(user_id=u.id, campaign_id=c.id, url=url, platform=p, video_key=f"{p}:{vid}", author=handle,
                                created_at=created, posted_at=created - timedelta(minutes=r.randint(5, 300)), views=final)
                db.session.add(s_)
                plan.append((s_, final, created))
    db.session.flush()
    caps = {c.id: (c.spent or 0) + c.remaining * .55 for c in camps}   # no gastar más del 55 % de lo que queda
    # Historial día a día (curva que sube rápido al principio y se va frenando)
    for s_, _, created in plan:
        final = s_.views or 0
        age_h = max(1, (now - created).total_seconds() / 3600)
        speed = r.uniform(18, 60)
        t = created
        while True:
            t += timedelta(hours=r.choice((6, 12, 24)))
            if t >= now:
                break
            h = (t - created).total_seconds() / 3600
            frac = (1 - math.exp(-h / speed)) / (1 - math.exp(-age_h / speed))
            v = int(final * min(1, frac))
            db.session.add(ViewSnapshot(submission_id=s_.id, views=v, likes=int(v * .062), comments=int(v * .004),
                                        shares=int(v * .006), taken_at=t))
        s_.likes, s_.comments, s_.shares = int(final * r.uniform(.04, .09)), int(final * r.uniform(.002, .006)), int(final * r.uniform(.003, .01))
        s_.views_updated_at = s_.checked_at = now - timedelta(hours=r.randint(1, 5))
    db.session.flush()
    # Estados: los que llegaron al mínimo y llevan tiempo, casi todos aprobados; algunos rechazados; el resto, en curso
    reasons = ["reglas", "menciones", "audio", "calidad", "duplicado"]
    for s_, _, created in sorted(plan, key=lambda x: x[2]):
        c = s_.campaign
        old = (now - created).days >= 2
        if not reached_min(s_, c):
            if old and r.random() < .25:
                s_.status, s_.reject_code = "rechazado", "caducado"
                s_.reject_reason = reject_text("caducado", "No llegó al pago mínimo a tiempo.")
            continue
        if old and r.random() < .1:
            code = r.choice(reasons)
            s_.status, s_.reject_code, s_.reject_reason = "rechazado", code, reject_text(code)
            s_.reviewed_at = created + timedelta(hours=r.randint(5, 30))
            if r.random() < .3:
                s_.appeal_text, s_.appeal_status = "Creo que sí cumple, puse la mención en la descripción.", "pendiente"
                s_.appeal_at = s_.reviewed_at + timedelta(hours=3)
        elif (old or r.random() < .4) and (c.spent or 0) + raw_earning(s_, c) <= caps[c.id]:
            s_.status, s_.reviewed_at = "aprobado", created + timedelta(hours=r.randint(4, 30))
            accrue(s_)
    # Unos cuantos que quieren entrar en una campaña (para la bandeja de solicitudes)
    for u, _, _ in r.sample(users, k=min(4, len(users))):
        free = [c for c in camps if not campaign_member(u.id, c.id)]
        if free:
            c = r.choice(free)
            db.session.add(CampaignMember(user_id=u.id, campaign_id=c.id, status="pendiente",
                                          created_at=now - timedelta(hours=r.randint(2, 30)),
                                          answers=demo_answers(c.question_list, r)))
            db.session.flush()
    # Algún mensaje de soporte y progreso en la formación
    for u, _, _ in r.sample(users, k=min(3, len(users))):
        db.session.add(Message(user_id=u.id, from_admin=False, sender_id=u.id, read_by_admin=False, read_by_user=True,
                               body=r.choice(["¿Cuándo se actualizan las vistas de mi último clip?",
                                              "Hola, ¿puedo subir el mismo clip a Instagram?",
                                              "¿Cuánto tarda en llegar el retiro a Whop?"]),
                               created_at=now - timedelta(hours=r.randint(1, 20))))
    lessons = [ls for c in Course.query.filter_by(published=True).all() for ls in c.visible_lessons]
    for u, _, _ in users:
        for ls in lessons[:r.randint(0, len(lessons))]:
            db.session.add(LessonProgress(user_id=u.id, lesson_id=ls.id, completed_at=now - timedelta(days=r.randint(0, days))))
    db.session.commit()
    total = len(plan)
    return f"Demo cargada: {len(users)} clippers y {total} clips de los últimos {days} días."


def remove_demo():
    demo = User.query.filter(User.email.like(f"%{DEMO_DOMAIN}")).all()
    for u in demo:
        delete_user_account(u)
    db.session.commit()
    return len(demo)


MY_DEMO_NOTICES = [("clip", "Clip aprobado", "Tu clip ya cuenta para el pago."),
                   ("clip", "¡Tu clip va muy bien!", "Ha pasado las 100.000 vistas."),
                   ("campana", "Te han aceptado en la campaña", "Ya puedes subir clips."),
                   ("clip", "Clip rechazado", "No cumplía las reglas de la campaña.")]


def load_my_demo(user, days=21, seed=11):
    """Demo personal: llena la cuenta del admin como si fuera un clipper (campañas, cuentas, clips, vistas y avisos).
    Va marcada aparte (my_demo) y no se mezcla con los clippers de demostración de la plataforma."""
    import random as rnd
    r = rnd.Random(seed + user.id)
    now = datetime.utcnow()
    camps = [c for c in Campaign.query.order_by(Campaign.id).all() if c.status != "finalizada" and not c.exhausted]
    if not camps:
        return "No hay campañas activas con presupuesto: crea alguna antes."
    base = (user.name or "admin").split()[0]
    handle = (viewcounter.clean_handle(base + ".demo").encode("ascii", "ignore").decode() or "admin.demo") + str(user.id)
    plan, n = [], 0
    for c in r.sample(camps, k=min(len(camps), 3)):
        joined = now - timedelta(days=days - r.randint(0, 4), hours=r.randint(0, 23))
        if not campaign_member(user.id, c.id):
            db.session.add(CampaignMember(user_id=user.id, campaign_id=c.id, status="aceptada", created_at=joined,
                                          decided_at=joined + timedelta(hours=r.randint(1, 12)), my_demo=True,
                                          answers=demo_answers(c.question_list, r)))
        plats = c.platform_list[:2] or ["tiktok"]
        for p in plats:
            if not CampaignAccount.query.filter_by(campaign_id=c.id, platform=p, handle=handle).first():
                db.session.add(CampaignAccount(user_id=user.id, campaign_id=c.id, platform=p, handle=handle, status="aceptada",
                                               bio_verified=True, bio_status="Verificada", created_at=joined,
                                               decided_at=joined, my_demo=True))
        for _ in range(r.randint(5, 9)):
            created = joined + timedelta(hours=r.randint(6, max(7, int((now - joined).total_seconds() // 3600) - 2)))
            p = r.choice(plats)
            n += 1
            vid = str(7500000000000000000 + user.id * 100000 + n) if p == "tiktok" else f"MYDEMO{user.id}x{n}"
            url = {"tiktok": f"https://www.tiktok.com/@{handle}/video/{vid}",
                   "instagram": f"https://www.instagram.com/reel/{vid}/",
                   "youtube": f"https://www.youtube.com/shorts/{vid}",
                   "facebook": f"https://www.facebook.com/reel/{vid}"}.get(p, f"https://www.tiktok.com/@{handle}/video/{vid}")
            age = (now - created).total_seconds() / 86400
            final = int(r.lognormvariate(9.8, 1.1) * (6 if r.random() < .12 else 1) * min(1, .35 + age / 6))
            minv = c.min_views_for(p)
            if minv and final < minv and age >= 2 and r.random() < .7:
                final = int(minv * r.uniform(1.1, 5))
            s_ = Submission(user_id=user.id, campaign_id=c.id, url=url, platform=p, video_key=f"{p}:mydemo{user.id}x{n}",
                            author=handle, created_at=created, posted_at=created - timedelta(minutes=r.randint(5, 300)),
                            views=final, my_demo=True)
            db.session.add(s_)
            plan.append((s_, created))
    db.session.flush()
    for s_, created in plan:                                   # historial de vistas (sube rápido y se frena)
        final, age_h, speed, t = s_.views or 0, max(1, (now - created).total_seconds() / 3600), r.uniform(18, 60), created
        while True:
            t += timedelta(hours=r.choice((6, 12, 24)))
            if t >= now:
                break
            frac = (1 - math.exp(-(t - created).total_seconds() / 3600 / speed)) / (1 - math.exp(-age_h / speed))
            v = int(final * min(1, frac))
            db.session.add(ViewSnapshot(submission_id=s_.id, views=v, likes=int(v * .062), comments=int(v * .004),
                                        shares=int(v * .006), taken_at=t))
        s_.likes, s_.comments, s_.shares = int(final * r.uniform(.04, .09)), int(final * r.uniform(.002, .006)), int(final * r.uniform(.003, .01))
        s_.views_updated_at = s_.checked_at = now - timedelta(hours=r.randint(1, 5))
    caps = {c.id: (c.spent or 0) + c.remaining * .25 for c in camps}    # como mucho, el 25 % de lo que queda en cada campaña
    for s_, created in sorted(plan, key=lambda x: x[1]):
        c, old = s_.campaign, (now - created).days >= 2
        if not reached_min(s_, c):
            if old and r.random() < .3:
                s_.status, s_.reject_code = "rechazado", "caducado"
                s_.reject_reason = reject_text("caducado", "No llegó al pago mínimo a tiempo.")
            continue
        if old and r.random() < .12:
            s_.status, s_.reject_code, s_.reject_reason = "rechazado", "reglas", reject_text("reglas")
            s_.reviewed_at = created + timedelta(hours=r.randint(5, 30))
        elif (old or r.random() < .4) and (c.spent or 0) + raw_earning(s_, c) <= caps[c.id]:
            s_.status, s_.reviewed_at = "aprobado", created + timedelta(hours=r.randint(4, 30))
            accrue(s_)
    for i, (kind, title, body) in enumerate(MY_DEMO_NOTICES):
        db.session.add(Notice(user_id=user.id, kind=kind, title=title, body=body, url="/mis-clips", read=i > 1,
                              created_at=now - timedelta(hours=3 + i * 9), my_demo=True))
    db.session.commit()
    return f"Tu demo como clipper está cargada: {len(plan)} clips en {len({s_.campaign_id for s_, _ in plan})} campañas."


def remove_my_demo(user):
    """Quita solo la demo personal del admin. Lo que ganaron sus clips vuelve al presupuesto de cada campaña."""
    subs = Submission.query.filter_by(user_id=user.id, my_demo=True).all()
    for s_ in subs:
        if s_.earned and s_.campaign:
            camp = lock_row(Campaign, s_.campaign_id)
            camp.spent = max(0.0, cents((camp.spent or 0) - s_.earned))
    ids = [s_.id for s_ in subs]
    if ids:
        ViewSnapshot.query.filter(ViewSnapshot.submission_id.in_(ids)).delete(synchronize_session=False)
        Submission.query.filter(Submission.id.in_(ids)).delete(synchronize_session=False)
    for model in (CampaignAccount, CampaignMember, Notice):
        model.query.filter_by(user_id=user.id, my_demo=True).delete(synchronize_session=False)
    db.session.commit()
    return len(ids)


@app.route("/admin/ajustes/mi-demo", methods=["POST"])
@admin_required
def admin_my_demo():
    if request.form.get("accion") == "quitar":
        n = remove_my_demo(current_user)
        audit("mi_demo_quitada", f"{n} clips")
        db.session.commit()
        flash(f"Tu demo como clipper se ha quitado ({n} clips). La demo de la plataforma y lo real siguen igual.", "ok")
    elif Submission.query.filter_by(user_id=current_user.id, my_demo=True).first():
        flash("Ya tienes tu demo cargada. Quítala primero si quieres volver a cargarla.", "error")
    else:
        msg = load_my_demo(current_user)
        audit("mi_demo_cargada", msg)
        db.session.commit()
        flash(msg, "ok")
    return redirect(url_for("admin_settings"))


@app.route("/admin/ajustes/demo", methods=["POST"])
@admin_required
def admin_demo():
    if request.form.get("accion") == "quitar":
        n = remove_demo()
        audit("demo_quitada", f"{n} clippers")
        db.session.commit()
        flash(f"Datos de demostración quitados ({n} clippers). Lo real sigue igual.", "ok")
    else:
        if User.query.filter(User.email.like(f"%{DEMO_DOMAIN}")).first():
            flash("Ya hay datos de demostración. Quítalos primero si quieres volver a cargarlos.", "error")
        else:
            msg = load_demo()
            audit("demo_cargada", msg)
            db.session.commit()
            flash(msg, "ok")
    return redirect(url_for("admin_settings"))


def owed_total():
    """Lo que la plataforma aún tiene que pagar a los clippers por Whop (ganado en clips aprobados − ya enviado)."""
    earned = (db.session.query(db.func.coalesce(db.func.sum(Submission.earned), 0))
              .filter(Submission.status == "aprobado").scalar() or 0)
    sent = (db.session.query(db.func.coalesce(db.func.sum(Payout.amount), 0))
            .filter(Payout.status.in_(("pagado", "procesando"))).scalar() or 0)
    return cents(float(earned) - float(sent))


def tech_alerts():
    """Avisos técnicos para el panel técnico: [(nivel, título, texto)]. Nivel: error · aviso."""
    out = []
    if not viewcounter.APIFY_TOKEN:
        out.append(("error", "Falta la clave de Apify", "Sin APIFY_TOKEN no se leen las vistas de los clips."))
    if not WHOP_API_KEY:
        out.append(("error", "Falta la clave de Whop", "Sin WHOP_API_KEY los clippers no pueden cobrar."))
    elif whop_company_id():
        bal, err = whop_balance(None)
        owed = owed_total()
        if bal is not None and bal + CENT < owed:
            out.append(("error", "Pendiente de saldo en Whop",
                        f"Tu Whop tiene {money(bal)} y se deben {money(owed)} a los clippers. Faltan {money(cents(owed - bal))}: "
                        "los retiros esperarán hasta que recargues."))
        elif bal is None and err:
            out.append(("aviso", "No se puede leer el saldo de Whop", err))
    last = get_setting("last_refresh_at", "")
    try:
        late = 26 if REFRESH_AT else (REFRESH_HOURS * 2 + 1 if REFRESH_HOURS else 0)
        if late and last and datetime.utcnow() - datetime.fromisoformat(last) > timedelta(hours=late):
            out.append(("aviso", "Las vistas llevan tiempo sin actualizarse", f"Última lectura: {get_setting('last_refresh', '—')}"))
    except ValueError:
        pass
    bad = Submission.query.filter(Submission.views_error != "", Submission.status != "rechazado").count()
    if bad:
        out.append(("aviso", f"{bad} clips sin poder leer sus vistas", "Puede ser un vídeo borrado o privado, o un fallo de Apify. Míralos en «Clips por revisar»."))
    failed = Payout.query.filter(Payout.error != "", Payout.created_at >= datetime.utcnow() - timedelta(days=7)).count()
    if failed:
        out.append(("aviso", f"{failed} pagos de Whop fallidos esta semana", "El motivo está en Retiros → «Últimos pagos que Whop no pudo enviar»."))
    if not VAPID_PUBLIC_KEY:
        out.append(("aviso", "Avisos al móvil sin activar", "Genera la clave de avisos abajo y ponla en Railway."))
    if not bunny_ready():
        out.append(("aviso", "Vídeos sin activar (Bunny)", "Faltan BUNNY_LIBRARY_ID y BUNNY_API_KEY: la formación y la vista previa de los clips usan el reproductor de cada red."))
    if not TECH_EMAILS:
        out.append(("aviso", "Panel técnico abierto a todos los dueños", "Pon tu correo en la variable TECH_EMAILS de Railway para que solo lo veas tú."))
    return out


def tech_alert_count():
    try:
        return sum(1 for lvl, _, _ in tech_alerts() if lvl == "error") if is_tech() else 0
    except Exception:  # noqa: BLE001
        return 0


@app.route("/admin/ajustes/probar-app-whop", methods=["POST"])
@admin_required
def admin_whop_app_test():
    """Prueba de la opción 2: pagar con la clave de la APP desde el saldo de la empresa del creador que la instaló."""
    if not WHOP_APP_API_KEY:
        flash("Falta WHOP_APP_API_KEY en las variables de Railway.", "error")
        return redirect(url_for("admin_settings"))
    biz = (request.form.get("company") or "").strip()
    username = (request.form.get("username") or "").strip().lstrip("@")
    amount = parse_float(request.form.get("amount"), 0) or 0
    if not biz.startswith("biz_") or not re.fullmatch(r"[A-Za-z0-9_.-]{1,60}", username) or not (0 < amount <= 5):
        flash("Pon el código biz_ de la empresa, un usuario de Whop y una cantidad entre 0,01 y 5 $.", "error")
        return redirect(url_for("admin_settings"))
    auth = {"Authorization": f"Bearer {WHOP_APP_API_KEY}"}
    steps = []
    ok, code, data = whop_call("GET", f"/users/{username}", headers=auth)
    steps.append(("1. Buscar al usuario que recibe", ok, code, data))
    user_id = str(data.get("id", "")) if ok and isinstance(data, dict) else ""
    if user_id.startswith("user_"):
        key = f"apptest-{secrets.token_hex(8)}"
        body = {"type": "balance", "amount": round(amount, 2), "currency": "usd", "origin_id": biz,
                "destination_id": user_id, "notes": f"Prueba app · {APP_NAME}"[:50], "idempotence_key": key}
        ok2, code2, data2 = whop_call("POST", "/transfers", json=body, headers={**auth, "Idempotency-Key": key})
        steps.append(("2. Enviar el pago desde la empresa del creador", ok2, code2, data2))
        success = ok2 and str((data2 or {}).get("status", "")).lower() != "failed"
    else:
        success = False
    audit("Prueba app de Whop", f"{biz} → @{username} {amount} $ · {'OK' if success else 'falló'}")
    db.session.commit()
    result = {"success": success, "steps": [(t, o, c, json.dumps(d, ensure_ascii=False, indent=2)[:3000]) for t, o, c, d in steps],
              "company": biz, "username": username, "amount": amount}
    return admin_settings(app_test=result)


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
    img.thumbnail((1200, 1200))
    out = io.BytesIO()
    img.save(out, "WEBP", quality=82, method=6)        # WebP: pesa mucho menos que JPG con la misma calidad
    return out.getvalue()


def _campaign_from_form(camp):
    camp.song = request.form.get("song", "").strip()[:200]
    camp.artist = request.form.get("artist", "").strip()[:200]
    if "cover_url" in request.form:                    # (antes se podía pegar un enlace; ahora solo se sube la imagen)
        cover_url = request.form.get("cover_url", "").strip()[:500]
        camp.cover_url = cover_url if cover_url.startswith("https://") else ""
    mats = []
    for line in request.form.get("materials", "").splitlines()[:12]:
        url = line.rpartition("|")[2].strip() if "|" in line else line.strip()
        if url.startswith(("https://", "http://")) and len(line) <= 600:
            mats.append(line.strip())
    camp.materials = "\n".join(mats)
    camp.sound_url = ""          # el enlace antiguo pasa a la lista de materiales
    camp.rules = request.form.get("rules", "").strip()[:8000]
    if "stats_form" in request.form:
        camp.stats_note = (request.form.get("stats_note") or "").strip()[:300]
    camp.rules_required = ""      # ahora todo va en un solo campo
    camp.rules_forbidden = ""
    qs = [q.strip()[:300] for q in (request.form.get("join_questions") or "").splitlines() if q.strip()]
    camp.join_questions = "\n".join(qs[:MAX_QUESTIONS])
    chosen = [p for p in PLATFORMS if request.form.get(f"pf_{p}")] or ["tiktok"]
    per = {p: parse_float(request.form.get(f"rate_k_{p}")) for p in PLATFORMS}
    if any(per[p] for p in chosen):                     # un pago distinto por cada red (por cada 1.000 vistas)
        for p in PLATFORMS:
            v = per[p] if p in chosen else None
            setattr(camp, f"rate_{p}", round(v * 1000, 4) if v and v > 0 else None)
        camp.rate = max((getattr(camp, f"rate_{p}") or 0) for p in chosen)
        for p in chosen:                                # una red sin precio cobra lo mismo que la que más paga
            if not getattr(camp, f"rate_{p}"):
                setattr(camp, f"rate_{p}", camp.rate)
    else:
        if request.form.get("rate_k") is not None:      # un único pago para todas las redes
            camp.rate = round((parse_float(request.form.get("rate_k"), 0) or 0) * 1000, 4)
        else:
            camp.rate = parse_float(request.form.get("rate"), 0) or 0
        for p in PLATFORMS:
            setattr(camp, f"rate_{p}", None)
    camp.budget = parse_float(request.form.get("budget"), 0) or 0
    if request.form.get("min_pay") is not None:         # y el pago mínimo por clip en dinero
        mp = parse_float(request.form.get("min_pay"), 0) or 0
        camp.min_views = int(math.ceil(mp * 1_000_000 / camp.rate)) if mp > 0 and camp.rate > 0 else 0
    else:
        camp.min_views = max(int(parse_float(request.form.get("min_views"), 0) or 0), 0)
    max_pv = parse_float(request.form.get("max_per_video"))
    camp.max_per_video = max_pv if max_pv and max_pv > 0 else None
    if any(request.form.get(f"min_k_{p}") is not None for p in PLATFORMS):   # mínimo y máximo de cada red
        for p in PLATFORMS:
            on = p in chosen
            mn = parse_float(request.form.get(f"min_k_{p}"))
            mx = parse_float(request.form.get(f"max_k_{p}"))
            setattr(camp, f"min_{p}", round(mn, 2) if on and mn and mn > 0 else (0.0 if on else None))
            setattr(camp, f"max_{p}", round(mx, 2) if on and mx and mx > 0 else (0.0 if on else None))
        camp.min_views, camp.max_per_video = 0, None   # ya no se usa el general
    else:
        for p in PLATFORMS:
            setattr(camp, f"min_{p}", None)
            setattr(camp, f"max_{p}", None)
    camp.platforms = ",".join(p for p in PLATFORMS if request.form.get(f"pf_{p}")) or "tiktok"
    status = request.form.get("status", "activa")
    status = status if status in ("activa", "pausada", "finalizada") else "activa"
    if status == "finalizada" and camp.id and camp.status != "finalizada":
        close_campaign(camp, current_user.id)          # al cerrarla, los clippers ya pueden retirar
    camp.status = status
    if not request.form.get("wizard"):                 # el formulario antiguo traía estas casillas
        camp.featured = bool(request.form.get("featured"))
        camp.full = bool(request.form.get("full"))
    if request.form.get("wizard"):                     # opciones del asistente de campaña
        camp.kind, camp.category = "clipping", "streamer"
        camp.max_members = max(int(parse_float(request.form.get("max_members"), 0) or 0), 0)
        if not camp.low_budget:
            camp.low_alert_sent = False
    file = request.files.get("cover_file")
    if file and file.filename:
        data = process_cover(file)
        if not data:
            return "La portada tiene que ser una imagen válida (JPG, PNG o WEBP)."
        camp.cover_data, camp.cover_mime = data, "image/webp"
        camp.cover_version = (camp.cover_version or 0) + 1
        try:
            camp.cover_tint, camp.cover_lqip = _cover_meta(data)
            camp.cover_meta_v = camp.cover_version
        except Exception:  # noqa: BLE001
            log.exception("No se pudo preparar la miniatura de la portada")
    if request.form.get("remove_cover"):
        camp.cover_data, camp.cover_version = None, 0
    if not (camp.song and camp.artist and camp.rate > 0 and camp.budget > 0):
        return "Faltan datos: nombre, creador, pago por cada 1.000 vistas y presupuesto."
    if camp.budget < (camp.spent or 0):
        return f"El presupuesto no puede ser menor que lo ya repartido ({money(camp.spent)})."
    for p in camp.platform_list:
        if camp.max_for(p) and camp.min_for(p) > camp.max_for(p):
            return f"En {PLATFORMS[p]} el pago mínimo por clip no puede ser mayor que el máximo."
    return None


@app.route("/admin/campana/nueva", methods=["GET", "POST"])
@app.route("/admin/campana/<int:cid>/editar", methods=["GET", "POST"])
@staff_required(lambda kw: kw.get("cid"))
def admin_campaign_form(cid=None):
    camp = db.session.get(Campaign, cid) if cid else Campaign(platforms="tiktok,instagram,youtube", status="activa",
                                                              category="streamer", spent=0.0)
    lead = db.session.get(CampaignLead, request.args.get("solicitud", type=int) or 0) if not cid else None
    if not cid and request.method == "GET":
        camp.join_questions = DEFAULT_QUESTIONS
    if lead and request.method == "GET":       # crear campaña a partir de una solicitud
        camp.song, camp.artist = "", lead.brand
        camp.sound_url = lead.link if lead.link.startswith(("https://", "http://")) else ""
    if cid and not camp:
        abort(404)
    as_mod = bool(cid) and acting_as_mod()
    keep = {}
    if as_mod:                                  # lo que el mod no puede cambiar se queda como estaba, mande lo que mande
        locked = ["featured", "creator_id", "category", "kind"]
        if not mod_can(current_user, cid, "dinero"):
            locked += [c.name for c in Campaign.__table__.columns
                       if c.name.startswith(("rate", "min_", "max_")) and c.name != "max_members"]
            locked += ["budget", "spent", "platforms", "bonus_per_clip", "earn_days", "expire_days"]
        if not mod_can(current_user, cid, "estado"):
            locked += ["status", "ended_at"]
        keep = {k: getattr(camp, k) for k in locked if hasattr(camp, k)}
    if request.method == "POST":
        error = _campaign_from_form(camp)
        for k, v in keep.items():
            setattr(camp, k, v)
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
            if not cid and camp.status == "activa":
                announce_campaign(camp)
            audit("campaña_creada" if not cid else "campaña_editada",
                  f"#{camp.id} {camp.song} · {money(camp.rate, 0)}/1M · presupuesto {money(camp.budget, 0)} · {camp.status}")
            db.session.commit()
            flash(return_msg, "ok")
            return redirect(url_for("admin_campaign", cid=camp.id))
        db.session.rollback()
        flash(error, "error")
        if cid:
            camp = db.session.get(Campaign, cid)
    return render_template("admin/campaign_form.html", camp=camp, editing=bool(cid), creators=Creator.query.order_by(Creator.name).all(),
                           can={k: perm_ok(cid, k) for k in ("dinero", "estado", "csv")} if cid else {"dinero": True, "estado": True, "csv": True},
                           pend=pending_by_campaign().get(cid, {}) if cid else {}, sec="ajustes")


def home_feature():
    """Qué sale en el bloque grande del Inicio: una campaña, un anuncio general o nada ("" = la campaña más nueva)."""
    try:
        d = json.loads(get_setting("home_feature", "") or "{}")
    except ValueError:
        d = {}
    return {"mode": d.get("mode", ""), "campaign_id": d.get("campaign_id"), "title": d.get("title", ""),
            "body": d.get("body", ""), "btn_text": d.get("btn_text", ""), "btn_url": d.get("btn_url", ""),
            "image_v": d.get("image_v", 0)}


WA_GROUP_RE = re.compile(r"^https://chat\.whatsapp\.com/[A-Za-z0-9]+/?(\?[\w=&.-]*)?$")


@app.route("/admin/destacado", methods=["GET", "POST"])
@admin_required
def admin_featured():
    hf = home_feature()
    if request.method == "POST" and request.form.get("act") == "whatsapp":
        url = (request.form.get("whatsapp_url") or "").strip()[:300]
        if url and not WA_GROUP_RE.match(url):
            flash("Pega un enlace de grupo de WhatsApp (empieza por https://chat.whatsapp.com/…).", "error")
            return redirect(url_for("admin_featured"))
        set_setting("whatsapp_general", url)
        audit("whatsapp_general", url or "quitado")
        db.session.commit()
        flash("Grupo general de WhatsApp guardado." if url else "Grupo general de WhatsApp quitado.", "ok")
        return redirect(url_for("admin_featured"))
    if request.method == "POST":
        mode = request.form.get("mode", "")
        if mode not in ("campaign", "announce", "none", ""):
            abort(400)
        new = dict(hf, mode=mode)
        if mode == "campaign":
            cid = request.form.get("campaign_id", type=int)
            camp = db.session.get(Campaign, cid or 0)
            if not camp:
                flash("Elige una campaña.", "error")
                return redirect(url_for("admin_featured"))
            new["campaign_id"] = camp.id
            Campaign.query.update({"featured": False})
            camp.featured = True
        elif mode == "announce":
            new["title"] = (request.form.get("title") or "").strip()[:120]
            new["body"] = (request.form.get("body") or "").strip()[:600]
            new["btn_text"] = (request.form.get("btn_text") or "").strip()[:40]
            url = (request.form.get("btn_url") or "").strip()[:300]
            new["btn_url"] = url if url.startswith(("https://", "/")) and not url.startswith("//") else ""
            if not new["title"]:
                flash("Escribe el título del anuncio.", "error")
                return redirect(url_for("admin_featured"))
            f = request.files.get("image")
            if f and f.filename:
                data = process_cover(f)
                if not data:
                    flash("La imagen tiene que ser JPG, PNG o WEBP.", "error")
                    return redirect(url_for("admin_featured"))
                set_setting("home_feature_img", base64.b64encode(data).decode())
                new["image_v"] = int(new.get("image_v") or 0) + 1
            if request.form.get("remove_image"):
                set_setting("home_feature_img", "")
                new["image_v"] = 0
            if request.form.get("notify"):
                n = 0
                for u in User.query.filter(User.is_admin.is_(False), User.is_banned.is_(False)).all():
                    notify_user(u.id, "destacado", new["title"], new["body"][:200], "/")
                    n += 1
                flash(f"Avisados {n} clippers por la campanita.", "ok")
        else:
            Campaign.query.update({"featured": False})
        set_setting("home_feature", json.dumps(new, ensure_ascii=False))
        audit("destacado_cambiado", f"{mode or 'automático'} · {new.get('title') or new.get('campaign_id') or ''}")
        db.session.commit()
        flash("Inicio actualizado.", "ok")
        return redirect(url_for("admin_featured"))
    camps = Campaign.query.filter(Campaign.status != "finalizada").order_by(Campaign.created_at.desc()).all()
    return render_template("admin/featured.html", hf=hf, camps=camps, wa_general=get_setting("whatsapp_general", ""))


@app.route("/destacado/imagen")
@login_required
def home_feature_image():
    raw = get_setting("home_feature_img", "")
    if not raw:
        abort(404)
    resp = Response(base64.b64decode(raw), mimetype="image/webp")
    resp.headers["Cache-Control"] = "private, max-age=31536000, immutable"
    return resp


COPY_SKIP = {"id", "spent", "report_token", "created_at", "ended_at", "status", "featured", "full", "low_alert_sent"}


@app.route("/admin/campana/<int:cid>/duplicar", methods=["POST"])
@admin_required
def admin_campaign_duplicate(cid):
    """Copia una campaña (sin clips ni dinero gastado) y la deja pausada para revisarla."""
    src = db.session.get(Campaign, cid) or abort(404)
    camp = Campaign(status="pausada", spent=0.0, report_token=secrets.token_urlsafe(18))
    for col in Campaign.__table__.columns:
        if col.name not in COPY_SKIP:
            setattr(camp, col.name, getattr(src, col.name))
    camp.song = (src.song + " (copia)")[:200]
    db.session.add(camp)
    db.session.flush()
    audit("campaña_duplicada", f"#{src.id} → #{camp.id}")
    db.session.commit()
    flash("Campaña duplicada. Está pausada: revisa los datos y actívala.", "ok")
    return redirect(url_for("admin_campaign_form", cid=camp.id))


@app.route("/admin/campana/<int:cid>/estado", methods=["POST"])
@staff_required(lambda kw: kw.get("cid"))
def admin_campaign_state(cid):
    """Botones rápidos: pausar, reanudar o añadir presupuesto."""
    camp = lock_row(Campaign, cid) or abort(404)
    accion = request.form.get("accion", "")
    if not perm_ok(cid, "dinero" if accion == "presupuesto" else "estado"):
        abort(403)
    if accion == "pausar" and camp.status == "activa":
        camp.status = "pausada"
        flash("Campaña pausada: ya no se ve ni acepta clips.", "ok")
    elif accion == "reanudar" and camp.status == "pausada":
        if camp.exhausted:
            flash("No tiene presupuesto libre. Añade presupuesto primero.", "error")
            return redirect(url_for("admin_campaign", cid=cid))
        camp.status = "activa"
        flash("Campaña activa otra vez.", "ok")
    elif accion == "plazas":
        camp.full = not camp.full
        if not camp.full:
            n = notify_waitlist(camp, current_user.id)
            flash("Plazas abiertas." + (f" Hemos avisado a {n} {'clipper' if n == 1 else 'clippers'} que esperaban." if n else ""), "ok")
        else:
            flash("Plazas completas: ya no se aceptan solicitudes nuevas.", "ok")
    elif accion == "presupuesto":
        extra = parse_float(request.form.get("amount"), 0) or 0
        if extra <= 0:
            flash("Escribe cuánto presupuesto quieres añadir.", "error")
            return redirect(url_for("admin_campaign", cid=cid))
        camp.budget = cents((camp.budget or 0) + extra)
        if not camp.low_budget:
            camp.low_alert_sent = False
        if camp.status == "finalizada" and not camp.ended_at:
            camp.status = "activa"
        flash(f"Añadidos {money(extra)}. Presupuesto total: {money(camp.budget)}.", "ok")
    else:
        abort(400)
    audit("campaña_" + accion, f"#{camp.id} {camp.song}" + (f" · +{request.form.get('amount')}" if accion == "presupuesto" else ""))
    db.session.commit()
    return redirect(url_for("admin_campaign", cid=cid))


def campaign_hook(camp):
    """Frase para vender una campaña: «Gana tu parte de 5.000 $. Convierte 500.000 vistas en 100 $.»"""
    parts = []
    if camp.budget:
        parts.append(f"Gana tu parte de {money(camp.budget, 0)}.")
    if camp.rate:
        target = 100
        views = int(round(target / camp.rate * 1_000_000, -3))
        if views:
            parts.append(f"Convierte {num(views)} vistas en {money(target, 0)}.")
    return " ".join(parts) or "Entra y pide unirte."


def announce_campaign(camp):
    """Aviso de campaña nueva a los clippers que pueden entrar (país permitido): a la campanita y, si está activo, al móvil."""
    hook = campaign_hook(camp)
    q = User.query.filter(User.is_admin.is_(False), User.is_banned.is_(False))
    n = 0
    for u in q.all():
        if camp.country_list and u.country and u.country not in camp.country_list:
            continue
        notify_user(u.id, "campana_nueva", f"Nueva campaña de {camp.artist}", hook, f"/campana/{camp.id or ''}", camp_id=camp.id)
        n += 1
    return n


@app.route("/admin/campana/<int:cid>/canales", methods=["GET", "POST"])
@staff_required(lambda kw: kw.get("cid"))
def admin_campaign_channels(cid):
    """Canales de la campaña: secciones, canales, publicaciones y el grupo de WhatsApp."""
    camp = db.session.get(Campaign, cid) or abort(404)
    sections = campaign_channels(camp)
    chans = {c.id: c for sec in sections for c in sec.channels}
    secs = {sec.id: sec for sec in sections}
    chid = request.args.get("canal", type=int) or request.form.get("canal", type=int)
    if request.method == "POST":
        act = request.form.get("act", "")
        name = " ".join((request.form.get("name") or "").split())[:60]
        emoji = (request.form.get("emoji") or "").strip()[:8]
        back = lambda c=None: redirect(url_for("admin_campaign_channels", cid=cid, canal=c or chid or None))  # noqa: E731
        if acting_as_mod() and not mod_can(current_user, cid, "canales" if act in MOD_CHANNEL_ACTS else "gestionar_canales"):
            abort(403)
        if act == "whatsapp":
            url = (request.form.get("whatsapp_url") or "").strip()[:300]
            if url and not WA_GROUP_RE.match(url):
                flash("Pega un enlace de grupo de WhatsApp (empieza por https://chat.whatsapp.com/…).", "error")
                return back()
            camp.whatsapp_url = url
            flash("Grupo de WhatsApp guardado." if url else "Grupo de WhatsApp quitado.", "ok")
        elif act == "seccion_nueva" and name:
            db.session.add(ChannelSection(campaign_id=cid, title=name[:80], position=len(sections)))
        elif act.startswith("seccion_"):
            sec = secs.get(request.form.get("section", type=int)) or abort(404)
            if act == "seccion_nombre" and name:
                sec.title = name[:80]
            elif act == "seccion_borrar":
                db.session.delete(sec)
            elif act in ("seccion_subir", "seccion_bajar"):
                _move(ChannelSection, sec, sections, "subir" if act == "seccion_subir" else "bajar")
        elif act == "canal_nuevo" and name:
            sec = secs.get(request.form.get("section", type=int)) or abort(404)
            ch = Channel(campaign_id=cid, section_id=sec.id, name=name, emoji=emoji or "#", position=len(sec.channels))
            db.session.add(ch)
            db.session.commit()
            return back(ch.id)
        elif act.startswith("canal_"):
            ch = chans.get(request.form.get("channel", type=int)) or abort(404)
            if act == "canal_editar" and name:
                ch.name, ch.emoji = name, emoji or ch.emoji
            elif act == "canal_borrar":
                db.session.delete(ch)
                db.session.commit()
                return redirect(url_for("admin_campaign_channels", cid=cid))
            elif act in ("canal_subir", "canal_bajar"):
                _move(Channel, ch, ch.section.channels, "subir" if act == "canal_subir" else "bajar")
        elif act == "publicar":
            ch = chans.get(chid) or abort(404)
            body = (request.form.get("body") or "").strip()[:8000]
            img, mime = None, ""
            f = request.files.get("image")
            if f and f.filename:
                img = process_photo(f)
                if not img:
                    flash("La imagen tiene que ser JPG, PNG o WEBP.", "error")
                    return back()
                mime = "image/jpeg"
            if not body and not img:
                flash("Escribe algo o añade una imagen.", "error")
                return back()
            post_ = Post(channel_id=ch.id, author_id=current_user.id, body=body, image_data=img, image_mime=mime)
            db.session.add(post_)
            db.session.flush()
            n = 0
            if request.form.get("notify"):
                for m in CampaignMember.query.filter_by(campaign_id=cid, status="aceptada").all():
                    notify_user(m.user_id, "anuncio", f"{ch.emoji} {ch.name} · {camp.song}".strip(), body[:200] or "Nueva imagen",
                                url_for("campaign", cid=cid, canal=ch.id))
                    n += 1
            audit("publicacion_canal", f"«{camp.song}» · {ch.name}")
            flash("Publicado." + (f" Avisados {n} clippers." if n else ""), "ok")
        elif act in ("post_editar", "post_borrar", "post_fijar"):
            post_ = db.session.get(Post, request.form.get("post", type=int) or 0)
            if not post_ or post_.channel_id not in chans:
                abort(404)
            if act == "post_borrar":
                db.session.delete(post_)
            elif act == "post_fijar":
                post_.pinned = not post_.pinned
            else:
                post_.body, post_.edited_at = (request.form.get("body") or "").strip()[:8000], datetime.utcnow()
        else:
            abort(400)
        db.session.commit()
        return back()
    chan = chans.get(chid) or (next(iter(chans.values()), None))
    posts = (Post.query.filter_by(channel_id=chan.id).options(selectinload(Post.likes), selectinload(Post.comments))
             .order_by(Post.pinned.desc(), Post.created_at.desc()).limit(100).all()) if chan else []
    return render_template("admin/channels.html", camp=camp, sections=sections, chan=chan, posts=posts, mod_ids=campaign_mod_ids(cid),
                           pend=pending_by_campaign().get(cid, {}), sec="canales")


@app.route("/admin/campana/<int:cid>/contenido", methods=["POST"])
@admin_required
def admin_campaign_content(cid):
    """Anuncios e ideas de la campaña, que se van añadiendo uno a uno."""
    camp = db.session.get(Campaign, cid) or abort(404)
    accion = request.form.get("accion", "")
    text_ = " ".join((request.form.get("text") or "").split())
    i = request.form.get("i", type=int)
    if accion == "anuncio" and text_:
        line = f"{datetime.utcnow().strftime('%d/%m/%Y')} | {text_[:600]}"
        camp.announcements = "\n".join([x for x in (camp.announcements or "").splitlines() if x.strip()] + [line])
        ids = [m.user_id for m in CampaignMember.query.filter_by(campaign_id=camp.id, status="aceptada")]
        for uid in ids:
            notify_user(uid, "anuncio", f"Anuncio en «{camp.song}»", text_[:200], url_for("campaign", cid=camp.id, tab="anuncios"))
        flash(f"Anuncio publicado. Lo verán {len(ids)} {'clipper' if len(ids) == 1 else 'clippers'}.", "ok")
    elif accion == "idea" and text_:
        camp.ideas = "\n".join(camp.idea_list + [text_[:300]])
        flash("Idea añadida.", "ok")
    elif accion in ("borrar_anuncio", "borrar_idea") and i is not None:
        field = "announcements" if accion == "borrar_anuncio" else "ideas"
        lines = [x for x in (getattr(camp, field) or "").splitlines() if x.strip()]
        if 0 <= i < len(lines):
            lines.pop(i)
            setattr(camp, field, "\n".join(lines))
    else:
        abort(400)
    db.session.commit()
    return redirect(url_for("admin_campaign", cid=cid))


# ---------------------------------------------------------------- creadores (pagan desde su propio Whop)
def creator_owed(creator_id):
    """Lo que el Whop de un creador todavía tiene que pagar: lo ganado en sus campañas menos lo ya enviado."""
    earned = (db.session.query(db.func.coalesce(db.func.sum(Submission.earned), 0))
              .join(Campaign, Submission.campaign_id == Campaign.id)
              .filter(Submission.status == "aprobado", Campaign.creator_id == creator_id).scalar() or 0)
    sent = (db.session.query(db.func.coalesce(db.func.sum(Payout.amount), 0))
            .filter(Payout.creator_id == creator_id, Payout.status.in_(("pagado", "procesando"))).scalar() or 0)
    return cents(float(earned) - float(sent))


def creators_overview():
    """Estado de cada creador: si su Whop está conectado, su saldo y si le alcanza para lo que debe."""
    rows, short = [], 0
    for c in Creator.query.order_by(Creator.name).all():
        key = creator_key(c)
        company = creator_company_id(c) if key else ""
        balance, err = whop_balance(c.id) if key and company else (None, "")
        owed = creator_owed(c.id)
        missing = cents(owed - balance) if balance is not None and balance + CENT < owed else 0
        short += 1 if missing else 0
        rows.append({"c": c, "has_key": bool(key), "company": company, "balance": balance, "error": err or
                     (_creator_company.get(c.key_var, {}).get("error", "") if key and not company else ""),
                     "owed": owed, "missing": missing, "campaigns": len(c.campaigns),
                     "live": sum(1 for x in c.campaigns if x.status == "activa")})
    set_setting("creators_short", str(short))
    return rows


@app.route("/admin/creadores")
@admin_required
def admin_creators():
    env_keys = sorted(k for k in os.environ if CREATOR_KEY_RE.match(k))
    return render_template("admin/creators.html", rows=creators_overview(), env_keys=env_keys,
                           main_ready=bool(WHOP_API_KEY and whop_company_id()), main_owed=creator_owed(None))


@app.route("/admin/creadores/nuevo", methods=["GET", "POST"])
@app.route("/admin/creadores/<int:crid>", methods=["GET", "POST"])
@admin_required
def admin_creator_form(crid=None):
    c = db.session.get(Creator, crid) if crid else Creator(name="")
    if crid and not c:
        abort(404)
    if request.method == "POST":
        if request.form.get("action") == "borrar" and c.id:
            if c.campaigns:
                flash("No se puede borrar: tiene campañas. Cambia antes el creador de esas campañas.", "error")
                return redirect(url_for("admin_creator_form", crid=c.id))
            audit("creador_borrado", c.name)
            db.session.delete(c)
            db.session.commit()
            flash("Creador borrado.", "ok")
            return redirect(url_for("admin_creators"))
        name = (request.form.get("name") or "").strip()[:120]
        key_var = (request.form.get("key_var") or "").strip().upper()
        company = (request.form.get("company_id") or "").strip()
        if not name:
            flash("Escribe el nombre del creador.", "error")
        elif key_var and not CREATOR_KEY_RE.match(key_var):
            flash("El nombre de la variable tiene que empezar por WHOP_KEY_ y llevar solo letras, números y _ (p. ej. WHOP_KEY_PABLO).", "error")
        elif company and not re.fullmatch(r"biz_[A-Za-z0-9]{6,30}", company):
            flash("El código de empresa tiene que empezar por biz_.", "error")
        else:
            changed_key = c.key_var != key_var
            c.name, c.key_var, c.company_id = name, key_var, company
            c.contact = (request.form.get("contact") or "").strip()[:255]
            c.notes = (request.form.get("notes") or "").strip()[:2000]
            if not c.id:
                db.session.add(c)
            if changed_key:
                _creator_company.pop(key_var, None)
            audit("creador_guardado", f"{c.name} · {key_var or 'sin clave'}")
            db.session.commit()
            flash("Creador guardado." + ("" if not key_var or os.environ.get(key_var) else
                  f" Falta añadir la variable {key_var} en Railway con su clave de Whop."), "ok")
            return redirect(url_for("admin_creators"))
    env_keys = sorted(k for k in os.environ if CREATOR_KEY_RE.match(k))
    return render_template("admin/creator_form.html", c=c, env_keys=env_keys)


@app.route("/admin/campana/<int:cid>")
@staff_required(lambda kw: kw.get("cid"))
def admin_campaign(cid):
    camp = db.session.get(Campaign, cid) or abort(404)
    if not camp.report_token:
        camp.report_token = secrets.token_urlsafe(18)
        db.session.commit()
    st = campaign_stats(camp)
    report_url = (BASE_URL or request.host_url.rstrip("/")) + url_for("report", token=camp.report_token)
    can = {k: perm_ok(cid, k) for k in ("estadisticas", "informe", "dinero", "estado")}
    shortfall = None
    if not acting_as_mod() and camp.status == "activa" and wallet_ready(camp.creator_id):
        bal, _ = whop_balance(camp.creator_id)
        owed = creator_owed(camp.creator_id)
        if bal is not None and bal + CENT < owed:
            shortfall = {"balance": bal, "owed": owed, "missing": cents(owed - bal)}
    return render_template("admin/campaign.html", camp=camp, st=st, report_url=report_url, can=can,
                           an=campaign_analytics(camp) if can["estadisticas"] else None, shortfall=shortfall, pend=pending_by_campaign().get(cid, {}), sec="resumen")


@app.route("/admin/campana/<int:cid>/clips.csv")
@staff_required(lambda kw: kw.get("cid"))
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


def _back():
    if request.referrer:
        return redirect(request.referrer)
    mod_cid = getattr(g, "mod_cid", None)
    return redirect(url_for("admin_campaign_review", cid=mod_cid) if mod_cid else url_for("admin_clips"))


def tell_clipper(user_id, body):
    db.session.add(Message(user_id=user_id, from_admin=True, sender_id=current_user.id if current_user.is_authenticated else None,
                           body=body, read_by_admin=True, read_by_user=False))


@app.route("/admin/envio/<int:sid>/<accion>", methods=["POST"])
@staff_required(_sub_camp)
def admin_review(sid, accion):
    sub = db.session.get(Submission, sid) or abort(404)
    camp = sub.campaign
    if acting_as_mod():
        if sub.user_id == current_user.id:
            flash("No puedes revisar tus propios clips: los revisa otra persona del equipo.", "error")
            return _back()
        need = "vistas" if accion == "visitas" else "verificar" if accion == "pedir_estadisticas" else "revisar"
        if not mod_can(current_user, sub.campaign_id, need):
            abort(403)
    nxt = request.form.get("next", "")
    if accion == "aprobar":
        if not account_verified(sub):
            who = f"@{sub.author}" if sub.author else f"de {PLATFORMS[sub.platform]}"
            flash(f"No puedes aprobar este clip: la cuenta {who} de {sub.user.name} aún no está verificada. "
                  "Verifícala desde su chat.", "error")
            return _back()
        if needs_stats(sub):
            flash("Aún no puedes aprobarlo: falta que el clipper verifique las vistas.", "error")
            return _back()
        approve_sub(sub)
        if camp.exhausted and not sub.earned:
            flash("Aprobado, pero la campaña ya no tiene presupuesto: este clip no suma dinero.", "error")
    elif accion == "rechazar":
        code = request.form.get("code", "")
        message = request.form.get("reason", "").strip()
        if code not in REJECT_REASONS:
            code = "otro"
        if code == "otro" and not message:
            flash("Elige un motivo o escribe por qué lo rechazas: el clipper lo verá.", "error")
            return _back()
        removed = reject_sub(sub, code, message)
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
    elif accion == "pedir_estadisticas":
        if sub.status != "pendiente":
            abort(400)
        reason = (request.form.get("reason") or "").strip()[:300]
        sub.stats_status, sub.stats_asked_at, sub.stats_reason = "pedido", datetime.utcnow(), reason
        notify_user(sub.user_id, "estadisticas", f"Te pedimos otra grabación · {camp.song}",
                    (reason or "La verificación de vistas no es válida.") + f" Tienes {STATS_HOURS} h para subir otra.",
                    "/mis-clips?tab=estadisticas", camp_id=camp.id)
        audit("estadisticas_pedidas", f"#{sub.id} · {reason}")
        flash("Le hemos pedido otra grabación al clipper.", "ok")
    elif accion in ("apelacion_aceptar", "apelacion_rechazar"):
        if sub.appeal_status != "pendiente":
            abort(400)
        reply = request.form.get("reply", "").strip()[:500]
        if accion == "apelacion_aceptar":
            sub.appeal_status, sub.status, sub.reject_reason, sub.reject_code = "aceptada", "pendiente", "", ""
            if needs_stats(sub):                       # vuelve a tener su plazo para subir las estadísticas
                sub.stats_asked_at, sub.stats_status = datetime.utcnow(), "pedido"
            tell_clipper(sub.user_id, f"Hemos revisado tu apelación del clip en «{camp.song}» y te damos la razón: "
                                      "vuelve a estar en revisión." + (f" {reply}" if reply else ""))
            if clip_stage(sub) == "revisar" and account_verified(sub) and request.form.get("approve"):
                approve_sub(sub)
        else:
            sub.appeal_status = "rechazada"
            tell_clipper(sub.user_id, f"Hemos revisado tu apelación del clip en «{camp.song}» y mantenemos el rechazo."
                                      + (f" {reply}" if reply else ""))
        audit("apelacion_" + accion.split("_")[1][:-1] + "da", f"#{sub.id} de {sub.user.email}")
    else:
        abort(400)
    db.session.commit()
    if nxt.startswith("/admin/"):
        return redirect(nxt)
    return _back()


@app.route("/admin/clips/lote", methods=["POST"])
@staff_required(_bulk_camp)
def admin_review_bulk():
    """Aprobar o rechazar varios clips a la vez."""
    ids = [int(x) for x in request.form.getlist("ids") if x.isdigit()][:200]
    accion = request.form.get("accion", "")
    code = request.form.get("code", "")
    message = request.form.get("reason", "").strip()
    if not ids:
        flash("Marca al menos un clip.", "error")
        return _back()
    if accion == "rechazar" and (code not in REJECT_REASONS or (code == "otro" and not message)):
        flash("Elige el motivo del rechazo.", "error")
        return _back()
    done = skipped = 0
    for sub in Submission.query.filter(Submission.id.in_(ids)).all():
        if sub.status != "pendiente" or (acting_as_mod() and sub.user_id == current_user.id):
            skipped += 1
        elif accion == "aprobar":
            if account_verified(sub) and not needs_stats(sub):
                approve_sub(sub)
                done += 1
            else:
                skipped += 1
        elif accion == "rechazar":
            reject_sub(sub, code, message)
            done += 1
        else:
            abort(400)
        db.session.flush()
    db.session.commit()
    verb = "aprobados" if accion == "aprobar" else "rechazados"
    flash(f"{done} clips {verb}." + (f" {skipped} no se pudieron (cuenta sin verificar o ya revisados)." if skipped else ""),
          "ok" if done else "error")
    return _back()


@app.route("/clip/<int:sid>/apelar", methods=["POST"])
@login_required
def appeal_clip(sid):
    """El clipper pide que revisemos otra vez un clip rechazado (una sola vez por clip)."""
    sub = db.session.get(Submission, sid)
    if not sub or sub.user_id != current_user.id:
        abort(404)
    back = redirect(request.referrer or url_for("my_clips"))
    text_ = request.form.get("text", "").strip()[:1000]
    if sub.status != "rechazado" or sub.appeal_status:
        flash("Este clip ya no se puede apelar.", "error")
        return back
    if sub.reject_code == "caducado":
        flash("Los clips que no llegaron al mínimo a tiempo no se pueden apelar.", "error")
        return back
    if len(text_) < 10:
        flash("Explica en una frase por qué crees que el clip cumple las reglas.", "error")
        return back
    sub.appeal_text, sub.appeal_status, sub.appeal_at = text_, "pendiente", datetime.utcnow()
    db.session.commit()
    notify_phone(f"⚖️ Apelación de {current_user.name}", f"«{sub.campaign.song}»: {text_[:120]}",
                 url_for("admin_clips", tab="apelaciones"), tags="scales")
    flash("Apelación enviada. La revisaremos y te contestaremos por el chat.", "ok")
    return back


@app.route("/admin/actualizar-visitas", methods=["POST"])
@admin_required
def admin_refresh():
    refresh_in_background()
    flash("Actualizando vistas en segundo plano. Recarga en unos minutos.", "ok")
    return redirect(url_for("admin_settings"))


@app.route("/admin/probar-apify", methods=["GET", "POST"])
@admin_required
def admin_probe():
    result = None
    if request.method == "POST":
        result = viewcounter.probe(request.form.get("url", ""))
    return render_template("admin/probe.html", result=result, has_token=bool(viewcounter.APIFY_TOKEN))


@app.route("/admin/campana/<int:cid>/cerrar", methods=["POST"])
@staff_required(lambda kw: kw.get("cid"))
def admin_campaign_close(cid):
    camp = db.session.get(Campaign, cid) or abort(404)
    if camp.status == "finalizada":
        flash("La campaña ya estaba cerrada.", "info")
    else:
        close_campaign(camp, current_user.id)
        audit("campaña_cerrada", f"#{camp.id} {camp.song} · repartido {money(camp.spent)}")
        db.session.commit()
        flash("Campaña cerrada. Los clippers ya pueden retirar lo que ganaron y les hemos avisado por el chat.", "ok")
    return redirect(url_for("admin_campaign", cid=cid))


@app.route("/admin/clipper/<int:uid>")
@admin_required
def admin_clipper(uid):
    user = db.session.get(User, uid) or abort(404)
    m = user_money(user)
    subs = sorted(user.submissions, key=lambda s: s.created_at, reverse=True)
    accounts = CampaignAccount.query.filter_by(user_id=uid).order_by(CampaignAccount.created_at).all()
    per_camp = {}
    for mb in CampaignMember.query.filter_by(user_id=uid).order_by(CampaignMember.created_at).all():
        per_camp[mb.campaign_id] = {"camp": mb.campaign, "member": mb, "accounts": [], "answers": mb.answer_list,
                                    "clips": 0, "views": 0, "earned": 0.0, "pending": 0}
    for a in accounts:
        row = per_camp.setdefault(a.campaign_id, {"camp": a.campaign, "member": None, "accounts": [], "answers": [], "clips": 0,
                                                 "views": 0, "earned": 0.0, "pending": 0})
        row["accounts"].append(a)
    for s in subs:
        row = per_camp.setdefault(s.campaign_id, {"camp": s.campaign, "member": None, "accounts": [], "answers": [], "clips": 0,
                                                 "views": 0, "earned": 0.0, "pending": 0})
        if s.status == "aprobado":
            row["clips"] += 1
            row["views"] += s.views or 0
            row["earned"] = cents(row["earned"] + (s.earned or 0))
        elif s.status == "pendiente":
            row["pending"] += 1
    payouts = sorted(user.payouts, key=lambda p: p.created_at, reverse=True)
    views = sum(s.views or 0 for s in subs if s.status == "aprobado")
    return render_template("admin/clipper.html", user=user, m=m, subs=subs, camps=list(per_camp.values()),
                           payouts=payouts, views=views, join_statuses=JOIN_STATUSES, member_statuses=MEMBER_STATUSES,
                           unread=Message.query.filter_by(user_id=uid, from_admin=False, read_by_admin=False).count())


def whop_call(method, path, api_key=None, **kw):
    """Llama a la API de Whop y devuelve (ok, código, datos). Sin `api_key`, usa la clave principal (WHOP_API_KEY)."""
    headers = {"Authorization": f"Bearer {api_key or WHOP_API_KEY}", "Content-Type": "application/json"}
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


_whop_company = {"id": "", "tried": 0.0, "error": ""}


def whop_company_id():
    """El biz_ de la empresa: el de Railway, o si no está, se lo preguntamos a Whop con la clave."""
    if WHOP_COMPANY_ID:
        return WHOP_COMPANY_ID
    if not _whop_company["id"] and WHOP_API_KEY and time.time() - _whop_company["tried"] > 60:
        _whop_company["tried"] = time.time()
        ok, code, data = whop_call("GET", "/accounts/me")
        cid = str((data or {}).get("id", "")) if isinstance(data, dict) else ""
        if ok and cid.startswith("biz_"):
            _whop_company["id"] = cid
            log.info("Whop: empresa detectada %s", cid)
        else:
            _whop_company["error"] = f"Whop respondió {code}: {str(data)[:300]}"
            log.warning("Whop: no se pudo saber la empresa (%s) %s", code, str(data)[:200])
    return _whop_company["id"]


CREATOR_KEY_RE = re.compile(r"^WHOP_KEY_[A-Z0-9_]{1,40}$")
_creator_company = {}          # nombre de variable → {"id", "tried", "error"}
_balance_cache = {}            # (clave de cartera) → (momento, saldo o None, error)


def creator_key(creator):
    """La clave de Whop de un creador, leída de su variable de Railway (nunca de la base de datos)."""
    if not creator or not CREATOR_KEY_RE.match(creator.key_var or ""):
        return ""
    return os.environ.get(creator.key_var, "").strip()


def creator_company_id(creator):
    """El biz_ de la empresa del creador: el que se escribió en su ficha o el que Whop dice con su clave."""
    if creator.company_id:
        return creator.company_id
    key = creator_key(creator)
    if not key:
        return ""
    st = _creator_company.setdefault(creator.key_var, {"id": "", "tried": 0.0, "error": ""})
    if not st["id"] and time.time() - st["tried"] > 60:
        st["tried"] = time.time()
        ok, code, data = whop_call("GET", "/accounts/me", api_key=key)
        cid = str((data or {}).get("id", "")) if isinstance(data, dict) else ""
        if ok and cid.startswith("biz_"):
            st["id"], st["error"] = cid, ""
        else:
            st["error"] = ("No se pudo conectar con Whop. Se vuelve a intentar en un minuto." if code == 0 else
                           "Whop no acepta la clave: revisa que esté bien copiada en Railway y que tenga los permisos."
                           if code in (401, 403) else f"Whop respondió {code}.")
            log.warning("Whop: no se pudo saber la empresa del creador %s (%s) %s", creator.id, code, str(data)[:200])
    return st["id"]


def wallet(creator_id):
    """(clave, empresa, nombre) del Whop del que sale el dinero de un creador; sin creador = la cuenta principal."""
    if creator_id and CREATORS_ENABLED:
        c = db.session.get(Creator, creator_id)
        if c:
            return creator_key(c), creator_company_id(c), c.name
        return "", "", "Creador borrado"
    return WHOP_API_KEY, (whop_company_id() if WHOP_API_KEY else ""), "Cuenta principal"


def wallet_ready(creator_id):
    key, company, _ = wallet(creator_id)
    return bool(key and company)


def whop_balance(creator_id):
    """Saldo en dólares del Whop de un creador (o de la cuenta principal). None si no se puede leer. Guarda 2 minutos."""
    key, company, _ = wallet(creator_id)
    if not key or not company:
        return None, "Whop no conectado"
    hit = _balance_cache.get((creator_id, company))
    if hit and time.time() - hit[0] < 120:
        return hit[1], hit[2]
    ok, code, data = whop_call("GET", f"/ledger_accounts/{company}", api_key=key)
    value, err = None, ""
    if ok and isinstance(data, dict):
        for b in data.get("balances") or []:
            if str(b.get("currency", "")).lower() == "usd":
                value = parse_float(b.get("balance"), None)
        if value is None and isinstance(data.get("treasury_balance"), dict):
            value = parse_float(data["treasury_balance"].get("balance_usd"), None)
    else:
        err = (f"Whop respondió {code}. Para ver el saldo, la clave necesita los permisos "
               "«company:balance:read» y «payout:account:read».") if code in (401, 403) else f"Whop respondió {code}"
    _balance_cache[(creator_id, company)] = (time.time(), value, err)
    return value, err


def whop_enabled():
    """¿Se puede pagar por Whop? Con la cuenta principal o con el Whop de algún creador."""
    if WHOP_API_KEY and whop_company_id():
        return True
    return CREATORS_ENABLED and any(creator_key(c) for c in Creator.query.all())


def whop_lookup_key():
    """Clave con la que buscar usuarios de Whop: la principal o, si no hay, la de cualquier creador conectado."""
    if WHOP_API_KEY:
        return WHOP_API_KEY
    return next((k for k in (creator_key(c) for c in Creator.query.all()) if k), "")


def whop_find_user(username):
    """Busca un usuario de Whop por su nombre de usuario. Devuelve (id, username) o (None, None)."""
    ok, code, data = whop_call("GET", f"/users/{username}", api_key=whop_lookup_key())
    if ok and isinstance(data, dict) and str(data.get("id", "")).startswith("user_"):
        return data["id"], (data.get("username") or username)
    log.warning("Whop: usuario %s no encontrado (%s) %s", username, code, str(data)[:300])
    return None, None


WHOP_FAIL_NOTE = "No se pudo enviar por Whop. El dinero ha vuelto a tu saldo."
OLD_WHOP_FAIL_NOTE = "Pago automático pendiente de revisar"


def release_stuck_payouts(query=None):
    """Retiros por Whop que fallaron con la versión anterior: se cancelan y el dinero vuelve al clipper."""
    q = query if query is not None else Payout.query
    stuck = q.filter(Payout.status == "solicitado", Payout.method == WHOP_METHOD, Payout.note == OLD_WHOP_FAIL_NOTE).all()
    for p in stuck:
        p.status, p.note = "rechazado", WHOP_FAIL_NOTE
    if stuck:
        db.session.commit()


def _whop_apply(payout, ok, code, data):
    """Actualiza el retiro según la respuesta de Whop. Devuelve 'pagado', 'procesando' o 'fallido'."""
    data = data if isinstance(data, dict) else {}
    status = str(data.get("status") or "").lower()
    if data.get("id"):
        payout.ext_id = str(data["id"])[:80]
    if ok and status not in ("failed", "processing", "pending", "created"):
        payout.status, payout.paid_at = "pagado", datetime.utcnow()
        payout.note = f"Enviado a tu Whop · {payout.ext_id}"[:255]
        notify_user(payout.user_id, "retiro", "Retiro enviado", f"{money(payout.amount)} ya están en tu Whop.", "/ganancias?tab=retiros")
        return "pagado"
    if ok and status != "failed":
        payout.status, payout.note = "procesando", "Enviando a tu cuenta de Whop"
        return "procesando"
    err = data.get("error")
    reason = data.get("failure_reason") or data.get("message") or (err.get("message") if isinstance(err, dict) else err)
    payout.status, payout.note = "rechazado", WHOP_FAIL_NOTE      # se cancela solo: el dinero vuelve a su saldo
    payout.error = f"Whop {code}: {reason or str(data)[:300]}"[:400]
    log.error("Whop: fallo al pagar el retiro #%s (%s) %s", payout.id, code, str(data)[:500])
    notify_phone(f"⚠️ Falló un pago por Whop ({money(payout.amount)})",
                 f"{payout.user.name}: {str(reason or code)[:150]}. El dinero ha vuelto a su saldo.",
                 url_for("admin_clippers") if has_request_context() else "", tags="warning")
    return "fallido"


def auto_paid_last_day():
    """Lo que ha salido (o está saliendo) por Whop en las últimas 24 h: para el tope diario de pagos automáticos."""
    since = datetime.utcnow() - timedelta(hours=24)
    total = db.session.query(db.func.coalesce(db.func.sum(Payout.amount), 0)).filter(
        Payout.method == WHOP_METHOD, Payout.status.in_(("procesando", "pagado")), Payout.created_at >= since).scalar()
    return cents(total or 0)


def whop_pay(payout):
    """Manda el retiro al saldo de Whop del clipper, desde el Whop del creador que paga esas campañas.
    Es seguro repetirlo: la clave de idempotencia evita pagar dos veces."""
    api_key, company, _ = wallet(payout.creator_id)
    if not api_key or not company:
        return _whop_apply(payout, False, 0, {"error": "El Whop de este creador no está conectado"})
    key = f"{APP_NAME.lower().replace(' ', '')}-payout-{payout.id}"
    body = {"type": "balance", "amount": round(payout.amount, 2), "currency": "usd", "origin_id": company,
            "destination_id": payout.user.whop_user_id, "notes": f"Retiro #{payout.id} · {APP_NAME}"[:50],
            "idempotence_key": key, "metadata": {"payout_id": str(payout.id)}}
    ok, code, data = whop_call("POST", "/transfers", api_key=api_key, json=body, headers={"Idempotency-Key": key})
    result = _whop_apply(payout, ok, code, data)
    log.info("Whop: retiro #%s → %s (%s)", payout.id, result, code)
    return result


def whop_sync(payouts):
    """Consulta en Whop los retiros que seguían «procesando»."""
    changed = False
    for p in payouts:
        if p.status != "procesando" or not p.ext_id:
            continue
        api_key, _, _ = wallet(p.creator_id)
        if not api_key:
            continue
        ok, code, data = whop_call("GET", f"/transfers/{p.ext_id}", api_key=api_key)
        if ok:
            _whop_apply(p, ok, code, data)
            changed = True
    if changed:
        db.session.commit()


BACKUP_SKIP = {("campaign", "cover_data"), ("message", "image_data"), ("course", "cover_data")}    # imágenes: pesan mucho y no son datos de negocio


@app.route("/admin/copia-de-seguridad.json")
@admin_required
def admin_backup():
    """Copia de todos los datos (sin imágenes) en un archivo JSON, para guardarla fuera de Railway."""
    from base64 import b64encode
    out = {"app": APP_NAME, "created_at": datetime.utcnow().isoformat() + "Z", "tables": {}}
    with db.engine.connect() as conn:
        for table in db.metadata.sorted_tables:
            cols = [c for c in table.columns if (table.name, c.name) not in BACKUP_SKIP]
            rows = []
            for r in conn.execute(db.select(*cols)):
                row = {}
                for c, v in zip(cols, r):
                    if isinstance(v, datetime):
                        v = v.isoformat()
                    elif isinstance(v, (bytes, bytearray, memoryview)):
                        v = b64encode(bytes(v)).decode()
                    elif v is not None and not isinstance(v, (int, float, str, bool)):
                        v = str(v)
                    row[c.name] = v
                rows.append(row)
            out["tables"][table.name] = rows
    audit("copia_descargada", f"{sum(len(v) for v in out['tables'].values())} filas")
    db.session.commit()
    name = f"copia-{APP_NAME.lower().replace(' ', '-')}-{datetime.utcnow():%Y%m%d-%H%M}.json"
    return Response(json.dumps(out, ensure_ascii=False), mimetype="application/json",
                    headers={"Content-Disposition": f"attachment; filename={name}"})


@app.route("/admin/borrar-datos", methods=["POST"])
@admin_required
def admin_wipe():
    """Borra todos los datos de prueba (campañas, clips, retiros, mensajes, clippers…). Solo quedan los admins."""
    if not ALLOW_WIPE:
        flash("Borrar datos está desactivado en la web real. Para activarlo, añade ALLOW_WIPE=1 en Railway.", "error")
        return redirect(url_for("admin_settings"))
    if (request.form.get("confirm") or "").strip().upper() != "BORRAR":
        flash("Para borrar todo, escribe BORRAR en la casilla.", "error")
        return redirect(url_for("admin_settings"))
    admin_ids = [u.id for u in User.query.filter_by(is_admin=True).all()]
    if current_user.id not in admin_ids:
        admin_ids.append(current_user.id)
    keep_campaigns = request.form.get("mode") == "keep_campaigns"
    models = (ViewSnapshot, Submission, Payout, CampaignAccount, CampaignMember, CampaignWaitlist, Message, CampaignLead,
              Notice, PushSub, LessonProgress, PostComment, PostLike, Post, Channel, ChannelSection, CampaignMod, CampMessage, IgnoredClip,
              Campaign, Creator,
              AuditLog, Setting)
    for model in models:
        if keep_campaigns and model in (Campaign, Creator, Post, Channel, ChannelSection):
            continue
        model.query.delete(synchronize_session=False)
    if keep_campaigns:      # las campañas se quedan como nuevas: sin nada repartido
        for c in Campaign.query.all():
            c.spent = 0.0
    User.query.filter(~User.id.in_(admin_ids)).delete(synchronize_session=False)
    for u in User.query.all():   # los admins se quedan, pero sin cuentas ni datos de cobro de prueba
        u.payout_method = u.payout_details = u.whop_user_id = u.whop_username = ""
        for p in PLATFORMS:
            setattr(u, f"{p}_handles", "")
    db.session.commit()
    log.warning("Datos de prueba borrados por %s", current_user.email)
    flash("Listo: se han borrado los clips, retiros, mensajes, solicitudes y clippers de prueba. Las campañas se quedan." if keep_campaigns
          else "Listo: se han borrado todas las campañas, clips, retiros, mensajes y clippers de prueba.", "ok")
    return redirect(url_for("admin_home"))


@app.route("/admin/clippers", methods=["GET", "POST"])
@admin_required
def admin_clippers():
    if request.method == "POST":
        payout = db.session.get(Payout, request.form.get("payout_id", type=int) or 0) or abort(404)
        if payout.status != "solicitado":
            flash("Ese retiro ya estaba gestionado.", "error")
            return redirect(url_for("admin_payouts"))
        note = request.form.get("note", "").strip()[:255]
        if request.form.get("action") == "whop":
            if payout.method != WHOP_METHOD or not payout.user.whop_user_id or not wallet_ready(payout.creator_id):
                flash("Este retiro no se puede pagar por Whop: el Whop de su creador no está conectado.", "error")
                return redirect(url_for("admin_payouts"))
            payout.status = "procesando"
            db.session.commit()
            result = whop_pay(payout)
            audit("retiro_whop", f"#{payout.id} {money(payout.amount)} a {payout.user.email} · {result} · {payout.ext_id}")
            db.session.commit()
            flash({"pagado": f"Enviado {money(payout.amount)} a @{payout.user.whop_username} por Whop.",
                   "procesando": "Whop está procesando el pago. Se actualizará solo."}.get(
                result, f"Whop no ha aceptado el pago ({payout.error}). El dinero ha vuelto al saldo del clipper."),
                "ok" if result != "fallido" else "error")
            return redirect(url_for("admin_payouts"))
        if request.form.get("action") == "pagar":
            payout.status, payout.paid_at, payout.note = "pagado", datetime.utcnow(), note
            audit("retiro_pagado", f"#{payout.id} {money(payout.amount)} a {payout.user.email} · {payout.method} · {note}")
            notify_user(payout.user_id, "retiro", "Retiro pagado", f"Te hemos pagado {money(payout.amount)}.", "/ganancias?tab=retiros")
            flash(f"Retiro de {money(payout.amount)} marcado como pagado.", "ok")
        elif request.form.get("action") == "rechazar":
            payout.status, payout.note = "rechazado", note or "Rechazado"
            audit("retiro_rechazado", f"#{payout.id} {money(payout.amount)} de {payout.user.email} · {payout.note}")
            flash("Retiro rechazado. El dinero vuelve a su saldo disponible.", "ok")
        db.session.commit()
        return redirect(url_for("admin_payouts"))
    q = request.args.get("q", "").strip().lower()
    users = (User.query.options(selectinload(User.submissions).joinedload(Submission.campaign), selectinload(User.payouts))
             .order_by(User.created_at.desc()).all())          # todo en 3 consultas aunque haya miles de clippers
    if q:
        users = [u for u in users if q in u.email.lower() or q in u.name.lower()
                 or any(q in h for p in PLATFORMS for h in u.handles(p))]
    rows = [(u, user_money(u), len(u.submissions)) for u in users]
    order = request.args.get("orden", "ganado")
    if order == "actividad":                       # los que entraron hace menos, primero; los que nunca, al final
        rows.sort(key=lambda r: r[0].last_seen_at or datetime.min, reverse=True)
    else:
        rows.sort(key=lambda r: -r[1]["earned"])
    now = datetime.utcnow()
    clippers = [u for u in users if not u.is_admin]
    activity = {"online": sum(1 for u in clippers if u.last_seen_at and (now - u.last_seen_at).total_seconds() < ONLINE_FOR),
                "day": sum(1 for u in clippers if u.last_seen_at and now - u.last_seen_at < timedelta(days=1)),
                "idle": sum(1 for u in clippers if not u.last_seen_at or now - u.last_seen_at >= timedelta(days=7))}
    return render_template("admin/clippers.html", rows=rows, q=q, order=order, activity=activity)


def delete_user_account(user):
    """Borra un clipper y todo lo suyo. Lo que ganó vuelve al presupuesto de cada campaña."""
    subs = Submission.query.filter_by(user_id=user.id).all()
    for s in subs:
        if s.earned and s.campaign:
            camp = lock_row(Campaign, s.campaign_id)
            camp.spent = max(0.0, cents((camp.spent or 0) - s.earned))
    sub_ids = [s.id for s in subs]
    if sub_ids:
        ViewSnapshot.query.filter(ViewSnapshot.submission_id.in_(sub_ids)).delete(synchronize_session=False)
    for model in (Submission, Payout, CampaignAccount, CampaignMember, CampaignWaitlist, Message, Notice, PushSub, LessonProgress,
                  PostComment, PostLike, CampaignMod, CampMessage, IgnoredClip):
        model.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    CampMessage.query.filter_by(sender_id=user.id).update({"sender_id": None}, synchronize_session=False)
    Post.query.filter_by(author_id=user.id).update({"author_id": None}, synchronize_session=False)
    Message.query.filter_by(sender_id=user.id).update({"sender_id": None}, synchronize_session=False)
    AuditLog.query.filter_by(admin_id=user.id).update({"admin_id": None}, synchronize_session=False)
    db.session.delete(user)


@app.route("/admin/clipper/<int:uid>/<accion>", methods=["POST"])
@admin_required
def admin_ban(uid, accion):
    user = db.session.get(User, uid) or abort(404)
    if user.is_admin:
        flash("No puedes bloquear ni eliminar a un administrador.", "error")
    elif accion == "eliminar":
        if (request.form.get("confirm") or "").strip().upper() != "ELIMINAR":
            flash("Para eliminar la cuenta, escribe ELIMINAR en la casilla.", "error")
            return redirect(url_for("admin_clipper", uid=uid))
        delete_user_account(user)
        audit("clipper_eliminado", user.email)
        db.session.commit()
        flash(f"La cuenta de {user.name} se ha eliminado con todos sus datos.", "ok")
        return redirect(url_for("admin_clippers"))
    elif accion == "bloquear":
        user.is_banned, user.ban_reason = True, request.form.get("reason", "").strip()[:255] or "Incumplimiento de los términos"
        audit("clipper_bloqueado", f"{user.email} · {user.ban_reason}")
        flash(f"{user.name} ha sido bloqueado.", "ok")
    elif accion == "desbloquear":
        user.is_banned, user.ban_reason = False, ""
        audit("clipper_desbloqueado", user.email)
        flash(f"{user.name} vuelve a tener acceso.", "ok")
    elif accion == "confianza":
        user.trusted = not user.trusted
        audit("clipper_confianza", f"{user.email} · {'sí' if user.trusted else 'no'}")
        flash(f"{user.name} {'ahora es de confianza: en las campañas que lo permitan sus clips se aprueban solos' if user.trusted else 'ya no es de confianza'}.", "ok")
    else:
        abort(400)
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


AUDIT_GROUPS = {  # grupo: (nombre, prefijos de la acción)
    "clips": ("Clips", ("clip_", "apelacion_", "vistas_", "estadisticas_")),
    "dinero": ("Dinero", ("retiro_",)),
    "clippers": ("Clippers y mods", ("clipper_", "cuenta_", "solicitud_", "mod_")),
    "campanas": ("Campañas", ("campaña_", "campana_")),
    "contenido": ("Contenido y avisos", ("curso_", "creador_", "publicacion_", "destacado_", "aviso", "whatsapp_")),
    "sistema": ("Sistema", ("demo_", "mi_demo_", "copia_")),
}


def audit_group(action):
    for k, (_n, pref) in AUDIT_GROUPS.items():
        if action.startswith(pref):
            return k
    return "sistema"


@app.route("/admin/actividad")
@admin_required
def admin_activity():
    logs = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(300).all()
    grupo = request.args.get("grupo", "")
    counts = {k: 0 for k in AUDIT_GROUPS}
    for l in logs:
        counts[audit_group(l.action)] += 1
    if grupo in AUDIT_GROUPS:
        logs = [l for l in logs if audit_group(l.action) == grupo]
    return render_template("admin/activity.html", logs=logs, grupo=grupo, counts=counts, groups=AUDIT_GROUPS,
                           total=sum(counts.values()), audit_group=audit_group)


@app.route("/admin/actividad/<int:lid>/borrar", methods=["POST"])
@admin_required
def admin_activity_delete(lid):
    entry = db.session.get(AuditLog, lid) or abort(404)
    db.session.delete(entry)
    db.session.commit()
    flash("Registro borrado.", "ok")
    return redirect(url_for("admin_activity"))


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


NOTIFY_EVENTS = {
    "clip_aprobado": "Cuando aprobamos un clip tuyo",
    "clip_rechazado": "Cuando rechazamos un clip tuyo",
    "campana_nueva": "Cuando sale una campaña nueva",
    "plazas": "Cuando se abren plazas en una campaña que esperas",
    "retiro": "Cuando te llega un retiro",
    "anuncio": "Anuncios de tus campañas",
    "mensaje": "Mensajes y avisos del equipo",
}
WHATSAPP_ENABLED = False          # WhatsApp en pausa hasta hablarlo con el cliente (el código se queda listo)
NOTIFY_CHANNELS = {"push": "En este móvil u ordenador"}


def push_ready():
    return bool(VAPID_PRIVATE_KEY and VAPID_PUBLIC_KEY)


def whatsapp_ready():
    return WHATSAPP_ENABLED and bool(WHATSAPP_TOKEN and WHATSAPP_PHONE_ID)


def wants(user, event, channel):
    return f"{event}.{channel}" not in (user.notify_off or "").split(",")


def whatsapp_send(phone, text):
    """Mensaje por la API oficial de WhatsApp (Meta). Usa una plantilla aprobada con un solo hueco {{1}}."""
    text = " ".join((text or "").split())[:1000]           # WhatsApp no deja saltos de línea en los huecos
    r = requests.post(f"https://graph.facebook.com/v21.0/{WHATSAPP_PHONE_ID}/messages", timeout=12,
                      headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"},
                      json={"messaging_product": "whatsapp", "to": phone.lstrip("+"), "type": "template",
                            "template": {"name": WHATSAPP_TEMPLATE, "language": {"code": WHATSAPP_LANG},
                                         "components": [{"type": "body", "parameters": [{"type": "text", "text": text}]}]}})
    return r.status_code


def _deliver(user_id, event, title, body, url):
    """Envía un aviso por los canales que el clipper tiene encendidos. Va en segundo plano."""
    with app.app_context():
        u = db.session.get(User, user_id)
        if not u or u.is_banned:
            return
        if push_ready() and wants(u, event, "push"):
            for sub in PushSub.query.filter_by(user_id=u.id).all():
                try:
                    code = webpush.send({"endpoint": sub.endpoint, "p256dh": sub.p256dh, "auth": sub.auth},
                                        {"title": title, "body": body, "url": url}, VAPID_PRIVATE_KEY,
                                        subject=f"mailto:{SUPPORT_EMAIL or 'soporte@clipprofit.app'}")
                    if code in (404, 410):                  # el navegador ya no existe o quitó el permiso
                        db.session.delete(sub)
                except Exception as e:  # noqa: BLE001
                    log.warning("Aviso push no enviado: %s", e)
            db.session.commit()
        if whatsapp_ready() and u.whatsapp and wants(u, event, "whatsapp"):
            try:
                code = whatsapp_send(u.whatsapp, f"{title}. {body}")
                if code >= 400:
                    log.warning("WhatsApp respondió %s al avisar a %s", code, u.id)
            except Exception as e:  # noqa: BLE001
                log.warning("WhatsApp no enviado: %s", e)


def _spawn(fn, *args):
    threading.Thread(target=fn, args=args, daemon=True).start()


def notify_user(user_id, event, title, body, path="", bell=True, camp_id=None):
    """Aviso personal a un clipper: siempre en su campanita y, si lo tiene activado, en su móvil."""
    u = db.session.get(User, user_id)
    if not u or is_demo(u):
        return
    if bell:                                       # los mensajes del chat ya salen en la campanita por su cuenta
        if camp_id is None:
            m_ = re.match(r"^/campana/(\d+)", path or "")
            camp_id = int(m_.group(1)) if m_ else None
        db.session.add(Notice(user_id=user_id, kind=event, title=title[:120], body=body[:400], url=(path or "")[:300],
                              campaign_id=camp_id))
    if not push_ready():
        return
    base = BASE_URL or (request.host_url.rstrip("/") if has_request_context() else "")
    _spawn(_deliver, user_id, event, title[:80], body[:300], (base + path) if path else base)


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
    text_ = last.body or ("📷 Foto" if last.image_mime else "")
    body = text_ if len(text_) <= 140 else text_[:137].rstrip() + "…"
    return {"unread": n, "id": last.id, "title": title, "who": who, "body": body, "url": url,
            "ago": ago(last.created_at)}


# ---------------------------------------------------------------- formación (cursos con vídeo)
def bunny_ready():
    return bool(BUNNY_LIBRARY_ID and BUNNY_API_KEY)


def bunny_call(method, path, **kw):
    r = requests.request(method, f"https://video.bunnycdn.com/library/{BUNNY_LIBRARY_ID}{path}", timeout=20,
                         headers={"AccessKey": BUNNY_API_KEY, "accept": "application/json"}, **kw)
    try:
        data = r.json()
    except ValueError:
        data = {}
    return r.status_code < 300, r.status_code, data


def lesson_embed(lesson):
    """Reproductor de la lección: Bunny (subido desde el panel), YouTube o Vimeo."""
    if lesson.video_guid and BUNNY_LIBRARY_ID:
        url = f"https://iframe.mediadelivery.net/embed/{BUNNY_LIBRARY_ID}/{lesson.video_guid}?autoplay=false&preload=true&responsive=true"
        if BUNNY_TOKEN_KEY:
            exp = int(time.time()) + 6 * 3600
            tok = hashlib.sha256(f"{BUNNY_TOKEN_KEY}{lesson.video_guid}{exp}".encode()).hexdigest()
            url += f"&token={tok}&expires={exp}"
        return url
    u = lesson.video_url or ""
    if "youtu" in u:
        vid = viewcounter.video_id(u)
        origin = (BASE_URL or (request.host_url if has_request_context() else "")).rstrip("/")
        return f"https://www.youtube-nocookie.com/embed/{vid}?enablejsapi=1&rel=0&origin={quote(origin, safe='')}" if vid else ""
    m = re.search(r"vimeo\.com/(?:video/)?(\d+)", u)
    if m:
        return f"https://player.vimeo.com/video/{m.group(1)}"
    return ""


def lesson_thumb(lesson):
    """Miniatura de la lección: la de Bunny o la de YouTube. Vacío = se pinta una gris con el icono de play."""
    if lesson.video_guid and BUNNY_CDN_HOST and lesson.video_status in ("", "listo"):
        return f"https://{BUNNY_CDN_HOST}/{lesson.video_guid}/thumbnail.jpg"
    u = lesson.video_url or ""
    if "youtu" in u:
        vid = viewcounter.video_id(u)
        return f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg" if vid else ""
    return ""


def clip_thumb(s):
    """Portada de un clip: la copia en Bunny, la de YouTube o la que dio Apify. Vacío = se pinta la portada de la campaña."""
    if s.video_guid and BUNNY_CDN_HOST and s.video_status == "listo":
        return f"https://{BUNNY_CDN_HOST}/{s.video_guid}/thumbnail.jpg"
    if s.platform == "youtube":
        vid = (s.video_key or "").split(":", 1)[-1]
        if vid:
            return f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"
    return s.thumb_url or ""


def dur_label(secs):
    """1 h 34 min · 57 min · 3:52 (lecciones)."""
    secs = int(secs or 0)
    if secs >= 3600:
        return f"{secs // 3600} h {secs % 3600 // 60} min"
    return f"{max(secs // 60, 1)} min" if secs else ""


def course_progress(course, user_id):
    ids = [ls.id for ls in course.visible_lessons]
    done = {p.lesson_id for p in LessonProgress.query.filter(LessonProgress.user_id == user_id,
                                                             LessonProgress.lesson_id.in_(ids or [0]),
                                                             LessonProgress.completed_at.isnot(None))}
    nxt = next((ls for ls in course.visible_lessons if ls.id not in done), None)
    chapters = sum(1 for ch in course.chapters if any(ls.published for ls in ch.lessons))
    return {"total": len(ids), "done": len(done), "pct": round(len(done) / len(ids) * 100) if ids else 0,
            "next": nxt or (course.visible_lessons[0] if ids else None), "done_ids": done, "finished": bool(ids) and not nxt,
            "chapters": chapters, "duration": dur_label(sum(ls.duration or 0 for ls in course.visible_lessons))}


@app.route("/formacion")
@login_required
def courses():
    rows = [(c, course_progress(c, current_user.id)) for c in
            Course.query.filter_by(published=True).order_by(Course.position, Course.id).all()]
    return render_template("courses.html", rows=[r for r in rows if r[1]["total"]], hl=request.args.get("destacar", type=int))


@app.route("/formacion/<int:cid>")
@login_required
def course(cid):
    c = db.session.get(Course, cid)
    if not c or (not c.published and not current_user.is_admin):
        abort(404)
    pr = course_progress(c, current_user.id)
    order = c.visible_lessons if not current_user.is_admin else c.lessons
    if not order:
        flash("Este curso aún no tiene lecciones.", "info")
        return redirect(url_for("courses"))
    # como en Whop: entras directamente donde lo dejaste (la última lección empezada y sin acabar, o la siguiente sin ver)
    ids = [ls.id for ls in order]
    last = (LessonProgress.query.filter(LessonProgress.user_id == current_user.id, LessonProgress.lesson_id.in_(ids),
                                        LessonProgress.completed_at.is_(None))
            .order_by(LessonProgress.updated_at.desc()).first())
    target = db.session.get(Lesson, last.lesson_id) if last else (pr["next"] if pr["next"] in order else order[0])
    return redirect(url_for("lesson", lid=target.id))


@app.route("/formacion/<int:cid>/portada")
def course_cover(cid):
    c = db.session.get(Course, cid)
    if not c or not c.cover_version:
        abort(404)
    resp = Response(c.cover_data, mimetype="image/webp")
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


@app.route("/formacion/leccion/<int:lid>")
@login_required
def lesson(lid):
    ls = db.session.get(Lesson, lid)
    if not ls or ((not ls.published or not ls.chapter.course.published) and not current_user.is_admin):
        abort(404)
    c = ls.chapter.course
    order = c.visible_lessons if not current_user.is_admin else c.lessons
    i = order.index(ls) if ls in order else 0
    pr = course_progress(c, current_user.id)
    mine = LessonProgress.query.filter_by(user_id=current_user.id, lesson_id=ls.id).first()
    rows = {r.lesson_id: r for r in LessonProgress.query.filter(LessonProgress.user_id == current_user.id,
                                                                  LessonProgress.lesson_id.in_([x.id for x in order] or [0]))}
    seen = {x.id: (100 if rows.get(x.id) and rows[x.id].completed_at else watch_pct(rows.get(x.id), x.duration)) for x in order}
    return render_template("lesson.html", ls=ls, c=c, pr=pr, embed=lesson_embed(ls), mine=mine, thumb=lesson_thumb, seen=seen,
                           prev_=order[i - 1] if i > 0 else None, next_=order[i + 1] if i < len(order) - 1 else None)


WATCH_DONE = 0.9          # la lección cuenta como vista al ver de verdad el 90 % del vídeo (saltar al final no vale)
WATCH_MAX_SPEED = 2.2     # como mucho se pueden ver así de rápido (vídeo a 2x + margen); más rápido = no se cuenta


def parse_ranges(text_, limit):
    """"0-120,300-410" → [(0, 120), (300, 410)], unidos y dentro de [0, limit]."""
    out = []
    for part in (text_ or "").split(",")[:400]:
        a, _, b = part.partition("-")
        try:
            a, b = max(int(float(a)), 0), min(int(float(b)), limit)
        except ValueError:
            continue
        if b > a:
            out.append((a, b))
    out.sort()
    merged = []
    for a, b in out:
        if merged and a <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def ranges_total(rs):
    return sum(b - a for a, b in rs)


def watch_pct(row, duration):
    if not row or not duration:
        return 0
    return min(round(ranges_total(parse_ranges(row.watched, duration)) / duration * 100), 100)


@app.route("/formacion/leccion/<int:lid>/progreso", methods=["POST"])
@login_required
def lesson_progress(lid):
    """Guarda lo que se ha visto de verdad del vídeo y marca la lección como vista sola al llegar al 90 %.
    Las lecciones sin vídeo (texto o archivo) se marcan al abrirlas un rato (done=1)."""
    ls = db.session.get(Lesson, lid) or abort(404)
    row = LessonProgress.query.filter_by(user_id=current_user.id, lesson_id=ls.id).first()
    now = datetime.utcnow()
    if not row:
        row = LessonProgress(user_id=current_user.id, lesson_id=ls.id, watched="", updated_at=now - timedelta(seconds=30))
        db.session.add(row)
    secs = request.form.get("seconds", type=int)
    if secs is not None and secs >= 0:
        row.seconds = min(secs, 24 * 3600)
    has_video = bool(lesson_embed(ls))
    duration = ls.duration or 0
    if has_video and not duration:                              # YouTube / Vimeo: la duración la dice el reproductor
        duration = min(max(request.form.get("duration", type=int) or 0, 0), 6 * 3600)
    if has_video and duration and request.form.get("ranges"):
        old = parse_ranges(row.watched, duration)
        new = parse_ranges((row.watched or "") + "," + request.form["ranges"], duration)
        gained = ranges_total(new) - ranges_total(old)
        elapsed = (now - (row.updated_at or now)).total_seconds()
        if gained <= elapsed * WATCH_MAX_SPEED + 15:            # no se puede «ver» más rápido de lo que pasa el tiempo
            row.watched = ",".join(f"{a}-{b}" for a, b in new)
        if not row.completed_at and ranges_total(parse_ranges(row.watched, duration)) >= duration * WATCH_DONE:
            row.completed_at = now
    elif not has_video and request.form.get("done") == "1" and not row.completed_at:
        row.completed_at = now
    row.updated_at = now
    db.session.commit()
    pct = 100 if row.completed_at else watch_pct(row, duration)
    if wants_json():
        return {"ok": True, "done": bool(row.completed_at), "pct": pct}
    return redirect(url_for("lesson", lid=ls.id))


@app.route("/formacion/leccion/<int:lid>/archivo")
@login_required
def lesson_file(lid):
    ls = db.session.get(Lesson, lid)
    if not ls or not ls.file_name or ((not ls.published or not ls.chapter.course.published) and not current_user.is_admin):
        abort(404)
    resp = Response(ls.file_data or b"", mimetype=ls.file_mime or "application/octet-stream")
    safe = re.sub(r"[^A-Za-z0-9._ -]", "_", ls.file_name)[:120] or "archivo"
    resp.headers["Content-Disposition"] = f'attachment; filename="{safe}"'
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


# ---- panel
def _move(model, item, siblings, direction):
    siblings = list(siblings)
    for n, x in enumerate(siblings):
        x.position = n
    i = siblings.index(item)
    j = i - 1 if direction == "subir" else i + 1
    if 0 <= j < len(siblings):
        siblings[i].position, siblings[j].position = siblings[j].position, siblings[i].position


@app.route("/admin/formacion", methods=["GET", "POST"])
@admin_required
def admin_courses():
    if request.method == "POST":
        title = (request.form.get("title") or "").strip()[:160]
        if not title:
            flash("Escribe el nombre del curso.", "error")
            return redirect(url_for("admin_courses"))
        c = Course(title=title, position=Course.query.count())
        db.session.add(c)
        db.session.flush()
        db.session.add(Chapter(course_id=c.id, title="Capítulo 1", position=0))
        audit("curso_creado", title)
        db.session.commit()
        return redirect(url_for("admin_course", cid=c.id))
    rows = []
    for c in Course.query.order_by(Course.position, Course.id).all():
        ids = [ls.id for ls in c.lessons]
        learners = (db.session.query(db.func.count(db.distinct(LessonProgress.user_id)))
                    .filter(LessonProgress.lesson_id.in_(ids or [0])).scalar() or 0)
        completed = (LessonProgress.query.filter(LessonProgress.lesson_id.in_(ids or [0]),
                                                 LessonProgress.completed_at.isnot(None)).count())
        rows.append({"c": c, "lessons": len(ids), "learners": learners,
                     "avg": round(completed / (learners * len(ids)) * 100) if learners and ids else 0})
    return render_template("admin/courses.html", rows=rows, bunny=bunny_ready())


@app.route("/admin/formacion/<int:cid>", methods=["GET", "POST"])
@admin_required
def admin_course(cid):
    c = db.session.get(Course, cid) or abort(404)
    if request.method == "POST":
        act = request.form.get("act", "")
        if act == "guardar":
            c.title = (request.form.get("title") or c.title).strip()[:160] or c.title
            c.description = (request.form.get("description") or "").strip()[:4000]
            c.published = bool(request.form.get("published"))
            f = request.files.get("cover_file")
            if f and f.filename:
                data = process_cover(f)
                if not data:
                    flash("La portada tiene que ser una imagen (JPG, PNG o WEBP).", "error")
                    return redirect(url_for("admin_course", cid=c.id))
                c.cover_data, c.cover_version = data, (c.cover_version or 0) + 1
            elif request.form.get("remove_cover"):
                c.cover_data, c.cover_version = None, 0
            flash("Curso guardado.", "ok")
        elif act == "capitulo":
            db.session.add(Chapter(course_id=c.id, title=(request.form.get("title") or "Capítulo nuevo").strip()[:160],
                                   position=len(c.chapters)))
        elif act in ("cap_subir", "cap_bajar", "cap_borrar", "cap_nombre"):
            ch = db.session.get(Chapter, request.form.get("chapter", type=int) or 0)
            if not ch or ch.course_id != c.id:
                abort(404)
            if act == "cap_borrar":
                db.session.delete(ch)
            elif act == "cap_nombre":
                ch.title = (request.form.get("title") or ch.title).strip()[:160] or ch.title
            else:
                _move(Chapter, ch, c.chapters, "subir" if act == "cap_subir" else "bajar")
        elif act == "leccion":
            ch = db.session.get(Chapter, request.form.get("chapter", type=int) or 0)
            if not ch or ch.course_id != c.id:
                abort(404)
            ls = Lesson(chapter_id=ch.id, title=(request.form.get("title") or "Lección nueva").strip()[:160],
                        position=len(ch.lessons))
            db.session.add(ls)
            db.session.commit()
            return redirect(url_for("admin_lesson", lid=ls.id))
        elif act in ("les_subir", "les_bajar"):
            ls = db.session.get(Lesson, request.form.get("lesson", type=int) or 0)
            if not ls or ls.chapter.course_id != c.id:
                abort(404)
            _move(Lesson, ls, ls.chapter.lessons, "subir" if act == "les_subir" else "bajar")
        elif act == "borrar":
            audit("curso_borrado", c.title)
            db.session.delete(c)
            db.session.commit()
            flash("Curso borrado.", "ok")
            return redirect(url_for("admin_courses"))
        else:
            abort(400)
        db.session.commit()
        return redirect(url_for("admin_course", cid=c.id))
    stats = {ls.id: LessonProgress.query.filter(LessonProgress.lesson_id == ls.id,
                                                LessonProgress.completed_at.isnot(None)).count() for ls in c.lessons}
    return render_template("admin/course.html", c=c, stats=stats)


ALLOWED_FILES = {".pdf", ".zip", ".png", ".jpg", ".jpeg", ".txt", ".docx", ".xlsx", ".pptx", ".mp3", ".srt"}


@app.route("/admin/formacion/leccion/<int:lid>", methods=["GET", "POST"])
@admin_required
def admin_lesson(lid):
    ls = db.session.get(Lesson, lid) or abort(404)
    if request.method == "POST":
        act = request.form.get("act", "guardar")
        if act == "borrar":
            cid = ls.chapter.course_id
            if ls.video_guid and bunny_ready():
                try:
                    bunny_call("DELETE", f"/videos/{ls.video_guid}")
                except requests.RequestException:
                    log.warning("No se pudo borrar el vídeo de Bunny %s", ls.video_guid)
            db.session.delete(ls)
            db.session.commit()
            flash("Lección borrada.", "ok")
            return redirect(url_for("admin_course", cid=cid))
        if act == "quitar_video":
            ls.video_guid, ls.video_status, ls.duration = "", "", 0
        elif act == "quitar_archivo":
            ls.file_name, ls.file_mime, ls.file_data = "", "", None
        else:
            ls.title = (request.form.get("title") or ls.title).strip()[:160] or ls.title
            ls.body = (request.form.get("body") or "").strip()[:20000]
            url = (request.form.get("video_url") or "").strip()[:500]
            ls.video_url = url if url.startswith("https://") else ""
            ls.published = bool(request.form.get("published"))
            ch = request.form.get("chapter", type=int)
            if ch and ch != ls.chapter_id:
                target = db.session.get(Chapter, ch)
                if target and target.course_id == ls.chapter.course_id:
                    ls.chapter_id, ls.position = target.id, len(target.lessons)
            f = request.files.get("file")
            if f and f.filename:
                ext = os.path.splitext(f.filename.lower())[1]
                if ext not in ALLOWED_FILES:
                    flash("Ese tipo de archivo no se puede subir. Usa PDF, ZIP, imagen, texto u Office.", "error")
                    return redirect(url_for("admin_lesson", lid=ls.id))
                ls.file_data, ls.file_name = f.read(), os.path.basename(f.filename)[:160]
                ls.file_mime = (f.mimetype or "application/octet-stream")[:80]
        db.session.commit()
        flash("Lección guardada.", "ok")
        return redirect(url_for("admin_lesson", lid=ls.id))
    return render_template("admin/lesson.html", ls=ls, c=ls.chapter.course, bunny=bunny_ready(), embed=lesson_embed(ls))


@app.route("/admin/formacion/leccion/<int:lid>/video", methods=["POST"])
@admin_required
def admin_lesson_video(lid):
    """Prepara la subida directa del navegador a Bunny: crea el vídeo y da una firma que caduca en 2 horas."""
    ls = db.session.get(Lesson, lid) or abort(404)
    if not bunny_ready():
        return {"ok": False, "error": "Faltan BUNNY_LIBRARY_ID y BUNNY_API_KEY en Railway."}, 400
    try:
        ok, code, data = bunny_call("POST", "/videos", json={"title": f"{ls.chapter.course.title} · {ls.title}"[:200]})
    except requests.RequestException:
        return {"ok": False, "error": "Bunny no responde. Inténtalo en un momento."}, 502
    if not ok or not data.get("guid"):
        return {"ok": False, "error": f"Bunny respondió {code}. Revisa la clave y la biblioteca."}, 502
    old = ls.video_guid
    ls.video_guid, ls.video_status, ls.duration = data["guid"], "subiendo", 0
    db.session.commit()
    if old:
        try:
            bunny_call("DELETE", f"/videos/{old}")
        except requests.RequestException:
            pass
    exp = int(time.time()) + 2 * 3600
    sig = hashlib.sha256(f"{BUNNY_LIBRARY_ID}{BUNNY_API_KEY}{exp}{ls.video_guid}".encode()).hexdigest()
    return {"ok": True, "guid": ls.video_guid, "library": BUNNY_LIBRARY_ID, "expire": exp, "signature": sig,
            "endpoint": "https://video.bunnycdn.com/tusupload"}


@app.route("/mis-clips/<int:sid>/estadisticas", methods=["POST"])
@login_required
def stats_video_prepare(sid):
    """El clipper sube la grabación de sus estadísticas directo a Bunny (como en la formación)."""
    sub = db.session.get(Submission, sid)
    if not sub or sub.user_id != current_user.id:
        abort(404)
    if not needs_stats(sub) and sub.stats_status != "subiendo":
        return {"ok": False, "error": "Este clip ya no necesita el vídeo."}, 400
    if not bunny_ready():
        return {"ok": False, "error": "La subida de vídeos no está disponible ahora mismo."}, 503
    try:
        ok, code, data = bunny_call("POST", "/videos", json={"title": f"Estadísticas clip {sub.id} · {sub.campaign.song}"[:200]})
    except requests.RequestException:
        return {"ok": False, "error": "El servidor de vídeos no responde. Inténtalo en un momento."}, 502
    if not ok or not data.get("guid"):
        log.warning("Bunny no creó el vídeo de estadísticas (%s)", code)
        return {"ok": False, "error": "No se pudo preparar la subida. Inténtalo en un momento."}, 502
    old = sub.stats_guid
    sub.stats_guid, sub.stats_status = data["guid"], "subiendo"
    db.session.commit()
    if old:
        try:
            bunny_call("DELETE", f"/videos/{old}")
        except requests.RequestException:
            pass
    exp = int(time.time()) + 2 * 3600
    sig = hashlib.sha256(f"{BUNNY_LIBRARY_ID}{BUNNY_API_KEY}{exp}{sub.stats_guid}".encode()).hexdigest()
    return {"ok": True, "guid": sub.stats_guid, "library": BUNNY_LIBRARY_ID, "expire": exp, "signature": sig,
            "endpoint": "https://video.bunnycdn.com/tusupload", "max": STATS_MAX_MB * 1024 * 1024}


@app.route("/mis-clips/<int:sid>/estadisticas/listo", methods=["POST"])
@login_required
def stats_video_done(sid):
    sub = db.session.get(Submission, sid)
    if not sub or sub.user_id != current_user.id or not sub.stats_guid:
        abort(404)
    if sub.stats_status != "enviado":
        sub.stats_status, sub.stats_at, sub.stats_reason = "enviado", datetime.utcnow(), ""
        audit("estadisticas_enviadas", f"#{sub.id} de {current_user.email}")
        db.session.commit()
    if wants_json():
        return {"ok": True}
    flash("Verificación enviada. Ya está en revisión.", "ok")
    return redirect(url_for("my_clips", tab="estadisticas"))


def stats_embed(sub):
    if sub.stats_guid and BUNNY_LIBRARY_ID:
        return f"https://iframe.mediadelivery.net/embed/{BUNNY_LIBRARY_ID}/{sub.stats_guid}?autoplay=false&preload=false&responsive=true"
    return ""


BUNNY_STATUS = {0: "subiendo", 1: "subiendo", 2: "procesando", 3: "procesando", 4: "listo", 5: "error", 6: "error"}


@app.route("/admin/formacion/leccion/<int:lid>/video/estado")
@admin_required
def admin_lesson_video_status(lid):
    ls = db.session.get(Lesson, lid) or abort(404)
    if not ls.video_guid or not bunny_ready():
        return {"status": ls.video_status or ""}
    try:
        ok, _, data = bunny_call("GET", f"/videos/{ls.video_guid}")
    except requests.RequestException:
        return {"status": ls.video_status}
    if ok:
        ls.video_status = BUNNY_STATUS.get(data.get("status"), ls.video_status)
        ls.duration = int(data.get("length") or ls.duration or 0)
        db.session.commit()
    return {"status": ls.video_status, "duration": ls.duration}


@app.route("/sw.js")
def service_worker():
    """Programa del navegador que muestra los avisos aunque la web esté cerrada."""
    resp = app.send_static_file("sw.js")
    resp.headers["Content-Type"] = "application/javascript; charset=utf-8"
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["Service-Worker-Allowed"] = "/"
    return resp


@app.route("/notificaciones", methods=["GET", "POST"])
@login_required
def notification_settings():
    u = current_user
    if request.method == "POST":
        off = [f"{e}.{c}" for e in NOTIFY_EVENTS for c in NOTIFY_CHANNELS if not request.form.get(f"{e}.{c}")]
        u.notify_off = ",".join(off)[:400]
        if "whatsapp" in request.form:
            wa = re.sub(r"[^\d+]", "", request.form.get("whatsapp", ""))[:20]
            if wa and not re.match(r"^\+?\d{8,15}$", wa):
                flash("Escribe tu WhatsApp con el prefijo del país, por ejemplo +34 600 000 000.", "error")
                return redirect(url_for("notification_settings"))
            u.whatsapp = wa
        db.session.commit()
        flash("Avisos guardados.", "ok")
        return redirect(url_for("notification_settings"))
    return render_template("notifications.html", events=NOTIFY_EVENTS, channels=NOTIFY_CHANNELS, wants=wants,
                           devices=PushSub.query.filter_by(user_id=u.id).count(),
                           push_ready=push_ready(), whatsapp_ready=whatsapp_ready(), vapid=VAPID_PUBLIC_KEY)


@app.route("/push/suscribir", methods=["POST"])
@login_required
def push_subscribe():
    endpoint = (request.form.get("endpoint") or "").strip()
    p256dh, auth = (request.form.get("p256dh") or "").strip(), (request.form.get("auth") or "").strip()
    if not endpoint.startswith("https://") or len(endpoint) > 800 or not p256dh or not auth or len(p256dh) > 200 or len(auth) > 60:
        return {"ok": False}, 400
    row = PushSub.query.filter_by(endpoint=endpoint).first()
    if row:
        row.user_id, row.p256dh, row.auth = current_user.id, p256dh, auth
    else:
        if PushSub.query.filter_by(user_id=current_user.id).count() >= 10:
            db.session.delete(PushSub.query.filter_by(user_id=current_user.id).order_by(PushSub.created_at).first())
        db.session.add(PushSub(user_id=current_user.id, endpoint=endpoint, p256dh=p256dh, auth=auth))
    db.session.commit()
    return {"ok": True}


@app.route("/push/quitar", methods=["POST"])
@login_required
def push_unsubscribe():
    PushSub.query.filter_by(user_id=current_user.id, endpoint=(request.form.get("endpoint") or "").strip()).delete()
    db.session.commit()
    return {"ok": True}


def render_for(text_, user):
    """Personaliza un aviso: {nombre} → el nombre del clipper."""
    first = (user.name or "").split()[0] if user.name else ""
    return text_.replace("{nombre}", first).replace("{Nombre}", first)


@app.route("/admin/avisos", methods=["GET", "POST"])
@admin_required
def admin_notify():
    """Enviar un aviso personalizado a todos, a los de una campaña o a un clipper."""
    camps = Campaign.query.order_by(Campaign.created_at.desc()).all()
    if request.method == "POST":
        audience = request.form.get("audience", "todos")
        if (request.form.get("one") or "").strip():
            audience = "u:" + request.form.get("one", "")
        body = (request.form.get("body") or "").strip()[:1000]
        title = (request.form.get("title") or "").strip()[:80] or "Aviso del equipo"
        if len(body) < 3:
            flash("Escribe el mensaje.", "error")
            return redirect(url_for("admin_notify"))
        q = User.query.filter(User.is_admin.is_(False), User.is_banned.is_(False))
        if audience.startswith("c:"):
            cid_ = int(audience[2:] or 0) if audience[2:].isdigit() else 0
            ids = [m.user_id for m in CampaignMember.query.filter_by(campaign_id=cid_, status="aceptada")]
            q = q.filter(User.id.in_(ids or [0]))
        elif audience.startswith("u:"):
            q = q.filter(User.email == audience[2:].strip().lower())
        users = q.all()
        for u in users:
            text_ = render_for(body, u)
            if request.form.get("chat"):
                db.session.add(Message(user_id=u.id, from_admin=True, sender_id=current_user.id, body=text_,
                                       read_by_admin=True, read_by_user=False))
            notify_user(u.id, "mensaje", render_for(title, u), text_, request.form.get("path", "")[:200] or "/",
                        bell=not request.form.get("chat"))
        audit("aviso_enviado", f"{audience} · {len(users)} clippers · {title}")
        db.session.commit()
        flash(f"Aviso enviado a {len(users)} {'clipper' if len(users) == 1 else 'clippers'}.", "ok" if users else "error")
        return redirect(url_for("admin_notify"))
    return render_template("admin/notify.html", camps=camps, push_ready=push_ready(), whatsapp_ready=whatsapp_ready(),
                           devices=PushSub.query.count(), with_wa=User.query.filter(User.whatsapp != "").count())


@app.route("/admin/ajustes/claves-avisos", methods=["POST"])
@admin_required
def admin_vapid_keys():
    """Genera un par de claves para los avisos (no se guarda: se enseña una vez para pegarla en Railway)."""
    priv, pub = webpush.new_vapid_keys()
    return render_template("admin/vapid.html", priv=priv, pub=pub)


BELL_ICONS = {"clip_nuevo": "film", "inactividad": "clock", "mod": "users", "estadisticas": "film", "clip_aprobado": "ok", "clip_rechazado": "x", "campana_nueva": "flag", "plazas": "users", "retiro": "cash",
              "mensaje": "chat", "anuncio": "megaphone", "destacado": "megaphone"}


def _bell_img(camp):
    """Miniatura de la portada para un aviso (vacío si la campaña no tiene o ya no existe)."""
    return cover_src(camp, 160) if camp else ""


def bell_data():
    """Lo que sale en la campanita: lista de avisos y cuántos hay sin leer."""
    if not current_user.is_authenticated:
        return {"rows": [], "unread": 0}
    items = []
    if current_user.is_admin:
        nc = admin_counts()
        camps_ = {c.id: c for c in Campaign.query.all()}
        names = {k: c.song for k, c in camps_.items()}
        for cid_, pd in pending_by_campaign().items():
            if pd["clips"] and admin_can("admin_campaign_review"):
                items.append({"id": None, "icon": "film", "title": f"{pd['clips']} {'clip' if pd['clips'] == 1 else 'clips'} por revisar",
                              "body": names.get(cid_, ""), "url": url_for("admin_campaign_review", cid=cid_), "ago": "", "unread": True,
                              "img": _bell_img(camps_.get(cid_)), "group": "Por hacer"})
            n = pd["requests"] + pd["accounts"]
            if n and admin_can("admin_campaign_requests"):
                items.append({"id": None, "icon": "inbox", "title": f"{n} {'solicitud' if n == 1 else 'solicitudes'}",
                              "body": names.get(cid_, ""), "url": url_for("admin_campaign_requests", cid=cid_), "ago": "", "unread": True,
                              "img": _bell_img(camps_.get(cid_)), "group": "Por hacer"})
            if pd.get("support") and admin_can("admin_campaign_support"):
                items.append({"id": None, "icon": "chat", "title": f"{pd['support']} {'consulta' if pd['support'] == 1 else 'consultas'} en soporte",
                              "body": names.get(cid_, ""), "url": url_for("admin_campaign_support", cid=cid_), "ago": "", "unread": True,
                              "img": _bell_img(camps_.get(cid_)), "group": "Por hacer"})
        for n, ico, one, many, ep in [
                (nc["payouts"], "cash", "retiro en curso", "retiros en curso", "admin_payouts"),
                (nc["messages"], "chat", "mensaje sin leer", "mensajes sin leer", "admin_inbox"),
                (nc["leads"], "megaphone", "creador interesado", "creadores interesados", "admin_leads")]:
            if n > 0 and admin_can(ep):
                items.append({"id": None, "icon": ico, "title": f"{n} {one if n == 1 else many}", "body": "", "url": url_for(ep),
                              "ago": "", "unread": True, "img": "", "group": "Por hacer"})
        return {"rows": items, "unread": len(items)}
    msgs = unread_messages()
    if msgs:
        items.append({"id": None, "icon": "chat", "title": f"{msgs} mensaje{'s' if msgs > 1 else ''} del equipo", "body": "",
                      "url": url_for("support"), "ago": "", "unread": True, "img": "", "group": "Hoy"})
    rows = Notice.query.filter_by(user_id=current_user.id).order_by(Notice.created_at.desc()).limit(30).all()
    cids = {n.campaign_id for n in rows if n.campaign_id}
    camps_ = {c.id: c for c in Campaign.query.filter(Campaign.id.in_(cids))} if cids else {}
    tz = ZoneInfo(os.environ.get("APP_TZ", "Europe/Madrid"))
    local = lambda dt: dt.replace(tzinfo=timezone.utc).astimezone(tz).date()  # noqa: E731
    today = local(datetime.utcnow())
    for n in rows:
        d = (today - local(n.created_at)).days
        group = "Hoy" if d <= 0 else "Ayer" if d == 1 else "Esta semana" if d < 7 else "Anteriores"
        items.append({"id": n.id, "icon": BELL_ICONS.get(n.kind, "bell"), "title": n.title, "body": n.body,
                      "url": url_for("open_notice", nid=n.id), "ago": ago(n.created_at), "unread": not n.read,
                      "img": _bell_img(camps_.get(n.campaign_id)), "group": group})
    unread = Notice.query.filter_by(user_id=current_user.id, read=False).count() + (1 if msgs else 0)
    return {"rows": items, "unread": unread}


@app.route("/avisos/<int:nid>")
@login_required
def open_notice(nid):
    n = db.session.get(Notice, nid)
    if not n or n.user_id != current_user.id:
        abort(404)
    if not n.read:
        n.read = True
        db.session.commit()
    url = n.url or "/"
    return redirect(url if url.startswith("/") and not url.startswith("//") else "/")


@app.route("/avisos/leer", methods=["POST"])
@login_required
def read_notices():
    Notice.query.filter_by(user_id=current_user.id, read=False).update({"read": True})
    db.session.commit()
    if wants_json():
        return {"ok": True}
    return redirect(request.referrer or url_for("index"))


@app.route("/avisos")
@login_required
def notices():
    d = notice_data()
    d["bell"] = bell_data()["unread"]
    return d


def message_json(m, viewer_is_admin):
    mine = m.from_admin == viewer_is_admin
    who = f"Equipo {APP_NAME}" if m.from_admin else m.user.name
    return {"id": m.id, "body": m.body, "mine": mine,
            "image": url_for("chat_image", mid=m.id) if m.image_mime else "", "who": who if not mine else "Tú",
            "from_admin": m.from_admin, "ago": ago(m.created_at), "at": m.created_at.strftime("%d/%m/%Y %H:%M")}


def wants_json():
    return request.headers.get("X-Requested-With") == "fetch"


def process_photo(file):
    """Foto del chat: se comprueba que sea una imagen y se guarda limpia y a un tamaño razonable."""
    from PIL import Image, ImageOps, UnidentifiedImageError
    try:
        img = Image.open(file.stream)
        img.verify()
        file.stream.seek(0)
        img = ImageOps.exif_transpose(Image.open(file.stream)).convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError):
        return None
    img.thumbnail((1600, 1600))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=82, optimize=True)
    return out.getvalue()


def post_message(owner_id, from_admin):
    body = (request.form.get("body") or "").strip()
    file = request.files.get("image")
    photo = None
    if file and file.filename:
        photo = process_photo(file)
        if not photo:
            return None, "Esa foto no se puede abrir. Prueba con una captura en JPG o PNG."
    if not body and not photo:
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
    if photo:
        m.image_data, m.image_mime = photo, "image/jpeg"
    db.session.add(m)
    db.session.commit()
    if not from_admin:
        notify_phone(f"💬 {current_user.name}", body or "📷 Ha enviado una foto", url_for("admin_chat", uid=owner_id))
    else:
        notify_user(owner_id, "mensaje", "Mensaje del equipo", body[:200] or "Te hemos enviado una foto", "/soporte", bell=False)
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


@app.route("/chat/foto/<int:mid>")
@login_required
def chat_image(mid):
    m = db.session.get(Message, mid)
    if not m or not m.image_mime or (m.user_id != current_user.id and not current_user.is_admin):
        abort(404)
    resp = Response(m.image_data, mimetype=m.image_mime)
    resp.headers["Cache-Control"] = "private, max-age=31536000, immutable"
    return resp


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
    members = CampaignMember.query.filter_by(user_id=user.id).order_by(CampaignMember.created_at.desc()).all()
    return render_template("admin/chat.html", user=user, msgs=msgs, m=user_money(user), accounts=accounts,
                           members=members, member_statuses=MEMBER_STATUSES,
                           join_statuses=JOIN_STATUSES)


@app.route("/admin/mensajes/<int:uid>/nuevos")
@admin_required
def admin_chat_poll(uid):
    db.session.get(User, uid) or abort(404)
    msgs = conversation(uid, request.args.get("after", type=int) or 0)
    mark_read(uid, by_admin=True)
    return {"messages": [message_json(m, True) for m in msgs]}


@app.route("/admin/cuenta-campana/<int:aid>/<accion>", methods=["POST"])
@staff_required(_account_camp)
def admin_campaign_account(aid, accion):
    acc = db.session.get(CampaignAccount, aid) or abort(404)
    if accion == "quitar":
        cid_, name, handle = acc.campaign_id, acc.user.name, acc.handle
        n = account_open_clips(acc)
        remove_campaign_account(acc, by_staff=True)
        flash(f"@{handle} de {name} quitada de la campaña." + (f" Sus {n} clips en marcha siguen ahí, pero ya no podrás aprobarlos si no vuelve a añadir la cuenta." if n else ""), "ok")
        return redirect(request.referrer or url_for("admin_campaign_clippers", cid=cid_))
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
    return redirect(request.referrer or url_for("admin_campaign_requests", cid=acc.campaign_id))


@app.route("/admin/solicitud/<int:mid>/<accion>", methods=["POST"])
@staff_required(_member_camp)
def admin_campaign_member(mid, accion):
    """Aceptar o rechazar la solicitud de un clipper para entrar en una campaña."""
    mb = db.session.get(CampaignMember, mid) or abort(404)
    if accion not in ("aceptar", "rechazar"):
        abort(400)
    mb.status = "aceptada" if accion == "aceptar" else "rechazada"
    mb.decided_at = datetime.utcnow()
    camp_ = mb.campaign
    if accion == "aceptar" and camp_.max_members:
        db.session.flush()
        if camp_.members_accepted >= camp_.max_members and not camp_.full:
            camp_.full = True
            flash(f"Se han llenado las {camp_.max_members} plazas: la campaña pasa a «plazas completas».", "info")
    if accion == "aceptar":
        note = (f"¡Te hemos aceptado en «{mb.campaign.song}»! Entra en la campaña, añade la cuenta con la que vas a publicar "
                "y verifícala con el código en su bio. En cuanto esté verificada podrás subir tus clips.")
    else:
        note = f"Esta vez no hemos podido aceptarte en «{mb.campaign.song}». Gracias por tu interés."
    db.session.add(Message(user_id=mb.user_id, from_admin=True, sender_id=current_user.id, body=note,
                           read_by_admin=True, read_by_user=False))
    audit("clipper_" + ("aceptado" if accion == "aceptar" else "rechazado"), f"{mb.user.email} · «{mb.campaign.song}»")
    db.session.commit()
    flash(f"{mb.user.name} {'aceptado' if accion == 'aceptar' else 'rechazado'} en «{mb.campaign.song}».", "ok")
    return redirect(request.referrer or url_for("admin_campaign_requests", cid=mb.campaign_id))


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
            error = "Escribe tu nombre y el de tu canal."
        elif "@" not in form["email"] or "." not in form["email"].split("@")[-1]:
            error = "Escribe un correo válido para poder responderte."
        elif recent >= 5:
            error = "Has enviado varias solicitudes seguidas. Inténtalo más tarde."
        if error:
            flash(error, "error")
        else:
            db.session.add(CampaignLead(
                name=form["name"][:120], email=form["email"][:255].lower(), brand=form["brand"][:200],
                category="streamer",
                budget=form["budget"] if form["budget"] in BUDGETS else "", link=form["link"][:500],
                message=form["message"][:3000], ip=ip))
            db.session.commit()
            log.info("Nueva solicitud de campaña: %s", form["brand"])
            notify_phone(f"🎬 Nueva campaña: {form['brand']}", f"{form['name']} · {form['email']} · {form['budget'] or 'sin presupuesto'}",
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


with app.app_context():                 # al arrancar, cuando ya está todo definido
    rename_beginner_course()
