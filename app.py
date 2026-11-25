"""ClipProfit Hispano — plataforma de clipping: campañas, clips, vistas y pagos."""
import base64
import csv
import hashlib
import io
import json
from concurrent.futures import ThreadPoolExecutor
import logging
import math
import os
import re
import secrets
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from functools import wraps
from urllib.parse import quote, urlencode, urlparse

import requests
from flask import (Flask, Response, abort, flash as _flask_flash, has_request_context, jsonify, redirect, render_template,
                   g, request, session, url_for)
from flask_login import (LoginManager, UserMixin, current_user, login_required,
                         login_user, logout_user)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect as sa_inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload, selectinload
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

import push as webpush
import views as viewcounter
import official

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
SENTRY_DSN = os.environ.get("SENTRY_DSN", "").strip()
if SENTRY_DSN:                                    # aviso automático de errores: página, usuario y línea
    try:
        import sentry_sdk
        sentry_sdk.init(dsn=SENTRY_DSN, traces_sample_rate=0, send_default_pii=False,
                        environment="produccion" if PRODUCTION else "pruebas")
    except Exception:  # noqa: BLE001  (si falta la librería o la clave está mal, la web arranca igual)
        SENTRY_DSN = ""
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
# Pagos (24 nov, Mustapha, opción A): el clipper ya no pide retiros; al terminar cada campaña el equipo la revisa y paga a
# todos de una vez (Pagos de la campaña). CLIPPER_WITHDRAW=1 en Railway vuelve a enseñar «Retirar».
CLIPPER_WITHDRAW = os.environ.get("CLIPPER_WITHDRAW", "").strip() == "1"
MAX_CLIPS_DAY = int(os.environ.get("MAX_CLIPS_DAY", "0"))      # 0 = sin límite de clips al día
PAYOUT_DAYS = int(os.environ.get("PAYOUT_DAYS", "7"))          # plazo de pago que se promete al clipper
ADMIN_EMAILS = {e.strip().lower() for e in os.environ.get("ADMIN_EMAIL", "").split(",") if e.strip()}          # dueños: todo
REVIEWER_EMAILS = {e.strip().lower() for e in os.environ.get("REVIEWER_EMAILS", "").split(",") if e.strip()}   # revisan clips y solicitudes
SUPPORT_EMAILS = {e.strip().lower() for e in os.environ.get("SUPPORT_EMAILS", "").split(",") if e.strip()}     # atienden mensajes
ADMIN_ROLES = {"owner": "Dueño", "reviewer": "Revisor", "support": "Soporte"}
# Panel técnico (claves, errores, Apify, Whop, copias…): solo estos correos. Vacío = cualquier dueño (hasta configurarlo)
TECH_EMAILS = {e.strip().lower() for e in os.environ.get("TECH_EMAILS", "").split(",") if e.strip()}


def creators_on():
    """Cada creador paga desde su propio Whop (con la app de ClipProfit instalada). Es la única forma (Mustapha, 24 nov):
    el dinero va del creador al clipper sin pasar por ClipProfit. Solo las campañas sin creador usan el Whop principal."""
    return True
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
# El dinero de cada campaña sale del Whop de su creador, nunca de la cuenta principal (Mustapha, 24 nov): en la web real
# no se puede activar una campaña sin creador. REQUIRE_CREATOR=0 en Railway lo quita
REQUIRE_CREATOR = os.environ.get("REQUIRE_CREATOR", "1" if PRODUCTION else "0").strip() == "1"
NO_CREATOR_MSG = "Elige qué creador paga esta campaña («Paga desde el Whop de»). Si aún no está, añádelo en Creadores."
# Modo oscuro: apagado (24 nov, Mustapha: «quítalo y luego lo ponemos mejor»). El código y los colores siguen; DARK_MODE=1 lo enciende
DARK_MODE = os.environ.get("DARK_MODE", "").strip() == "1"
WHOP_COMPANY_ID = os.environ.get("WHOP_COMPANY_ID", "").strip()
WHOP_APP_API_KEY = os.environ.get("WHOP_APP_API_KEY", "").strip()
# «Conectar con Whop» para los clippers (OAuth de Whop): el código app_… de la app y, si Whop lo pide, su secreto
WHOP_CLIENT_ID = os.environ.get("WHOP_CLIENT_ID", "").strip()
WHOP_CLIENT_SECRET = os.environ.get("WHOP_CLIENT_SECRET", "").strip()
# La app de ClipProfit en Whop que instalan los creadores (sin claves): su app_… para el enlace de instalación
WHOP_APP_ID = (os.environ.get("WHOP_APP_ID", "").strip() or WHOP_CLIENT_ID)   # prueba de la opción 2: la app de Whop que instalan los creadores
WHOP_API = os.environ.get("WHOP_API_URL", "https://api.whop.com/api/v1").rstrip("/")
WHOP_AUTO_MAX = float(os.environ.get("WHOP_AUTO_MAX", "50"))    # retiros por encima de esto los aprueba un admin (0 = sin tope)
WHOP_AUTO_DAILY = float(os.environ.get("WHOP_AUTO_DAILY", "300"))  # máximo pagado solo en 24 h entre todos; lo que pase espera a un admin (0 = sin tope)
WHOP_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{2,40}$")
# En producción la web vive en app.clipprofit.es (la dirección de Railway manda ahí); BASE_URL en Railway lo cambia
BASE_URL = os.environ.get("BASE_URL", "https://app.clipprofit.es" if PRODUCTION else "").strip().strip('"').rstrip("/")
if BASE_URL and not BASE_URL.startswith("http"):
    BASE_URL = "https://" + BASE_URL
if PRODUCTION and ".up.railway.app" in BASE_URL:      # una BASE_URL vieja con la dirección de Railway: manda el dominio propio
    BASE_URL = "https://app.clipprofit.es"
IMG_FORMATS = ("JPEG", "PNG", "WEBP", "GIF")      # solo se abren estos formatos de imagen: los raros (TIFF, BMP, PDF…) son los que traen los fallos
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
    admin_seen_at = db.Column(db.DateTime, nullable=True)       # última vez que abrió el panel (Panel técnico → Salud)
    email_verified_at = db.Column(db.DateTime, nullable=True)   # confirmó su correo (Google ya lo confirma)
    email_off = db.Column(db.Boolean, default=False)           # no quiere avisos por correo (los de su cuenta sí le llegan)
    admin_seen_ua = db.Column(db.String(200), default="")      # con qué dispositivo
    staff_perms = db.Column(db.String(300), default="")        # funciones generales de un mod (STAFF_PERMS, separadas por comas)
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
    username = db.Column(db.String(24), default="", index=True)   # @usuario en la web (se elige al entrar la primera vez)
    theme = db.Column(db.String(8), default="")   # "dark" = modo oscuro (lo elige el usuario; por defecto claro)

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
    rank_pos = db.Column(db.Integer, nullable=True)        # puesto en la clasificación en la última lectura diaria
    rank_prev = db.Column(db.Integer, nullable=True)       # …y en la anterior (para enseñar cuántos puestos sube)
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
    spots_opened_at = db.Column(db.DateTime, nullable=True)  # última vez que se abrieron plazas (para el destacado automático)
    apply_form_url = db.Column(db.String(500), default="")   # formulario de Google para apuntarse (opcional)
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
        return [(r["name"], r["url"], r["kind"]) for r in self.resources]

    @property
    def resources(self):
        """Recursos de la campaña: [{icon, name, url, desc, kind}]. Cada línea: «icono | Nombre | enlace | descripción»
        (el icono y la descripción son opcionales; las campañas antiguas tenían «Nombre | enlace»)."""
        out = []
        lines = (self.materials or "").splitlines() or ([self.sound_url] if self.sound_url else [])
        for line in lines:
            parts = [p.strip() for p in line.split("|")]
            ui = next((i for i, p in enumerate(parts) if p.startswith(("https://", "http://"))), None)
            if ui is None:
                continue
            url, before, desc = parts[ui], parts[:ui], " | ".join(parts[ui + 1:]).strip()
            ico = before[0] if len(before) >= 2 and before[0] in RESOURCE_ICONS else ""
            name = (before[1] if ico else " | ".join(before)).strip() if before else ""
            host = urlparse(url).netloc.lower()
            kind = ("tiktok" if "tiktok." in host else "instagram" if "instagram." in host
                    else "youtube" if ("youtube." in host or "youtu.be" in host)
                    else "facebook" if ("facebook." in host or "fb.watch" in host)
                    else "drive" if host.endswith(("drive.google.com", "docs.google.com")) else "link")
            if not name:
                name = {"tiktok": "TikTok", "instagram": "Instagram", "youtube": "YouTube", "facebook": "Facebook",
                        "drive": "Google Drive"}.get(kind) or host.removeprefix("www.") or "Enlace"
            if not ico:
                ico = kind if kind in RESOURCE_ICONS else "folder" if kind == "drive" else "link"
            out.append({"icon": ico, "name": name[:80], "url": url, "desc": desc[:200], "kind": kind})
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
    video_error = db.Column(db.String(300), default="")      # por qué falló la copia en Bunny
    thumb_url = db.Column(db.String(1000), default="")       # portada que da Apify (se renueva en cada lectura)
    thumb_data = db.deferred(db.Column(db.LargeBinary, nullable=True))   # portada guardada (las de las redes caducan)
    thumb_ok = db.Column(db.Boolean, default=False)
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
    __table_args__ = (db.Index("ix_view_snapshot_sub_time", "submission_id", "taken_at"),)   # «última lectura hasta tal día»
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
    campaign_id = db.Column(db.Integer, db.ForeignKey("campaign.id"), nullable=True, index=True)  # pago de campaña (vacío = retiro)
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
    admin_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)   # Mods: actividad de cada uno
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
    avatar_url = db.Column(db.String(1000), default="")        # foto de perfil que da Apify (se renueva)
    avatar_data = db.deferred(db.Column(db.LargeBinary, nullable=True))  # foto guardada (las de las redes caducan)
    avatar_ok = db.Column(db.Boolean, default=False)
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
    def load():
        q = CampaignAccount.query.filter_by(user_id=user_id, campaign_id=campaign_id)
        if status:
            q = q.filter_by(status=status)
        if platform:
            q = q.filter_by(platform=platform)
        return q.order_by(CampaignAccount.created_at).all()
    if has_request_context() and request.method == "GET":      # al pintar listas se pide lo mismo por cada clip: una vez basta
        return list(_per_request(("camp_accs", user_id, campaign_id, status, platform), load))
    return load()


class EmailLog(db.Model):
    """Cada correo que manda la web (Panel técnico → Salud): para contar el tope de Resend y ver los que fallan."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=True, index=True)
    to = db.Column(db.String(255), default="")
    kind = db.Column(db.String(30), default="", index=True)   # confirmar · recuperar · y las claves de los avisos
    subject = db.Column(db.String(200), default="")
    status = db.Column(db.String(12), default="enviando", index=True)   # enviando · ok · error · tope
    error = db.Column(db.String(300), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


class StatHour(db.Model):
    """Cifras del servidor por hora para Panel técnico → Salud (cada proceso las junta en memoria y las suma aquí cada minuto)."""
    hour = db.Column(db.DateTime, primary_key=True)
    requests = db.Column(db.Integer, default=0)        # páginas servidas (sin archivos ni consultas automáticas)
    ms_total = db.Column(db.Float, default=0)          # suma de lo que tardó el servidor en prepararlas
    slow = db.Column(db.Integer, default=0)            # las que tardaron más de SLOW_MS
    errors = db.Column(db.Integer, default=0)          # fallos de la web (500)
    blocked = db.Column(db.Integer, default=0)         # frenadas por demasiados intentos
    csrf = db.Column(db.Integer, default=0)            # formularios caducados o falsos
    bots = db.Column(db.Integer, default=0)            # robots buscando fallos (wp-admin, .env…)


class TechNote(db.Model):
    """Notas de Mustapha en Panel técnico → Notas (solo las ve quien ve el Panel técnico)."""
    id = db.Column(db.Integer, primary_key=True)
    text = db.Column(db.Text, nullable=False)
    done = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


class ErrorLog(db.Model):
    """Fallos con código (p. ej. «K7M2QX»): el usuario ve el código y Mustapha lo busca en Panel técnico → Errores."""
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(8), index=True, nullable=False)
    kind = db.Column(db.String(20), default="servidor", index=True)   # servidor · aviso · pagina · navegador · tarea · google
    what = db.Column(db.String(200), default="")
    explain = db.Column(db.Text, default="")                          # qué pasó, en palabras normales
    times = db.Column(db.Integer, default=1)                          # veces que se ha repetido (mismo fallo, misma página)
    last_at = db.Column(db.DateTime, nullable=True, index=True)
    path = db.Column(db.String(300), default="")
    user_id = db.Column(db.Integer, nullable=True)
    detail = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


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
    phone = db.Column(db.String(30), default="")               # WhatsApp (reservas de llamada desde la landing)
    meet_at = db.Column(db.DateTime, nullable=True, index=True)  # hora de la llamada reservada (UTC)
    plan = db.Column(db.String(80), default="", index=True)    # plan de la landing desde el que reservó
    button = db.Column(db.String(80), default="", index=True)  # botón de la landing que pulsó
    tz = db.Column(db.String(64), default="")                  # zona horaria del navegador del creador (p. ej. America/Mexico_City)
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
    deleted_at = db.Column(db.DateTime, nullable=True)      # borrada (se puede deshacer o restaurar)
    channels = db.relationship("Channel", backref="section", lazy=True, order_by="Channel.position",
                               cascade="all, delete-orphan")

    @property
    def live(self):
        return [c for c in self.channels if not c.deleted_at]


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
    deleted_at = db.Column(db.DateTime, nullable=True)      # borrado (se puede deshacer o restaurar)
    posts = db.relationship("Post", backref="channel", lazy=True, order_by="Post.created_at.desc()",
                            cascade="all, delete-orphan")


class ApiToken(db.Model):
    """Cuenta de TikTok / Instagram / YouTube conectada con la API oficial. Los tokens se guardan cifrados."""
    __table_args__ = (db.UniqueConstraint("platform", "ext_id", name="uq_api_token"),)
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    platform = db.Column(db.String(20), nullable=False)
    ext_id = db.Column(db.String(120), nullable=False)        # id de la cuenta en la red
    handle = db.Column(db.String(100), default="", index=True)
    avatar_url = db.Column(db.String(1000), default="")
    access_enc = db.Column(db.Text, default="")
    refresh_enc = db.Column(db.Text, default="")
    expires_at = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(20), default="ok")           # ok · caducado
    error = db.Column(db.String(300), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_read_at = db.Column(db.DateTime, nullable=True)
    audience_at = db.Column(db.DateTime, nullable=True)
    user = db.relationship("User")


class LandingTile(db.Model):
    """Imágenes de las columnas de la portada pública: portadas de vídeos que elige el admin pegando sus enlaces."""
    id = db.Column(db.Integer, primary_key=True)
    url = db.Column(db.String(500), nullable=False)
    views = db.Column(db.BigInteger, nullable=True)
    thumb_data = db.deferred(db.Column(db.LargeBinary, nullable=True))
    status = db.Column(db.String(12), default="cargando")      # cargando · ok · error
    error = db.Column(db.String(200), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class AudienceStat(db.Model):
    """Audiencia (país, edad y sexo, en %) de una cuenta o de un clip, según lo que deja cada red."""
    id = db.Column(db.Integer, primary_key=True)
    platform = db.Column(db.String(20), nullable=False)
    handle = db.Column(db.String(100), default="", index=True)
    submission_id = db.Column(db.Integer, db.ForeignKey("submission.id"), nullable=True, index=True)
    data = db.Column(db.Text, default="{}")                   # {"country": {...}, "age": {...}, "gender": {...}}
    taken_at = db.Column(db.DateTime, default=datetime.utcnow)
    demo = db.Column(db.Boolean, default=False)


class Invite(db.Model):
    """Enlace de invitación a una campaña: entra directo, aceptado. Sirve una sola vez."""
    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey("campaign.id"), nullable=False, index=True)
    token = db.Column(db.String(40), unique=True, nullable=False)
    note = db.Column(db.String(120), default="")             # para quién es (lo ve solo el equipo)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    used_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    used_at = db.Column(db.DateTime, nullable=True)
    campaign = db.relationship("Campaign")
    user = db.relationship("User", foreign_keys=[used_by])


class Call(db.Model):
    """Llamada de una campaña (directo con el equipo). La hora se guarda en UTC y se enseña en España y en la hora de cada uno."""
    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey("campaign.id"), nullable=False, index=True)
    title = db.Column(db.String(120), nullable=False)
    notes = db.Column(db.String(600), default="")
    starts_at = db.Column(db.DateTime, nullable=False, index=True)
    minutes = db.Column(db.Integer, default=60)
    link = db.Column(db.String(500), default="")             # Zoom / Meet / Discord…
    recording_url = db.Column(db.String(500), default="")    # grabación, cuando la haya
    reminded = db.Column(db.Boolean, default=False)          # ya se avisó 30 min antes
    author_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    demo = db.Column(db.Boolean, default=False)
    campaign = db.relationship("Campaign")

    @property
    def ends_at(self):
        return self.starts_at + timedelta(minutes=self.minutes or 60)


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
    demo = db.Column(db.Boolean, default=False)              # de los datos de demostración (se quita con ellos)
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
             "dinero": "Presupuesto y tarifas",
             "estado": "Pausar, abrir/cerrar plazas y finalizar"}
MOD_DEFAULT = ("revisar", "verificar", "solicitudes", "canales", "comentarios", "soporte")
MOD_RISKY = {"dinero": "toca el dinero de la campaña", "estado": "puede cerrar la campaña"}

# Funciones generales de un mod (24 nov, Mustapha: «todas las que tiene un admin y ahí puede elegir»). Cada casilla abre una
# sección del panel en TODAS las campañas. Clave: (nombre, explicación, pantallas del panel, permisos de campaña que da en
# todas, aviso si es arriesgada). El Panel técnico (y lo que solo se ve desde él: Creadores, Actividad) nunca se puede dar.
STAFF_PERMS = {
    "resumen": ("Resumen", "Las cifras generales del panel", {"admin_home"}, (), ""),
    "revisar": ("Revisar clips y solicitudes", "En todas las campañas, sin añadirlo una a una",
                {"admin_campaigns", "admin_clips", "admin_requests", "admin_campaign", "admin_campaign_review",
                 "admin_campaign_requests", "admin_review", "admin_review_bulk", "admin_campaign_member",
                 "admin_campaign_account", "admin_invites"}, ("revisar", "verificar", "solicitudes", "estadisticas"), ""),
    "campanas": ("Crear y editar campañas", "Crear, editar, pausar y cerrar campañas, canales y soporte de cada campaña",
                 {"admin_campaigns", "admin_campaign", "admin_campaign_form", "admin_campaign_duplicate", "admin_campaign_state",
                  "admin_campaign_close", "admin_campaign_channels", "admin_campaign_content", "admin_campaign_csv",
                  "admin_campaign_clippers", "admin_campaign_support", "admin_invites"}, tuple(MOD_PERMS),
                 "toca presupuestos y tarifas"),
    "retiros": ("Pagos y retiros", "Revisar y pagar las campañas terminadas", {"admin_payouts", "admin_payouts_csv",
                                                                             "admin_campaign_pay"}, (), "mueve dinero"),
    "destacado": ("Campaña destacada", "Elegir qué campaña sale destacada", {"admin_featured"}, (), ""),
    "distribucion": ("Distribución", "Los planes de distribución", {"admin_dists", "admin_dist"}, (), ""),
    "interesados": ("Creadores interesados", "Las reservas de llamada que llegan de la portada",
                    {"admin_leads", "admin_lead_whatsapp", "admin_book_hours"}, (), ""),
    "clippers": ("Clippers", "Buscar y ver la ficha de cualquier clipper", {"admin_clippers", "admin_clipper", "admin_find"},
                 ("clippers",), ""),
    "mensajes": ("Mensajes", "Leer y contestar el soporte general", {"admin_inbox", "admin_chat", "admin_chat_poll"}, (), ""),
    "avisos": ("Avisos", "Enviar avisos y elegir los automáticos", {"admin_notify", "admin_auto_notices"}, (), ""),
    "formacion": ("Formación", "Cursos y lecciones",
                  {"admin_courses", "admin_course", "admin_lesson", "admin_lesson_video", "admin_lesson_video_status"}, (), ""),
    "mods": ("Mods de campaña", "Añadir y quitar mods de campaña (las funciones generales solo las das tú)",
             {"admin_mods", "admin_campaign_mods"}, (), "da permisos a otros"),
}


def staff_keys(user=None):
    """Funciones generales de un mod (vacío para los admins, que ya lo tienen todo según su rol, y para el resto)."""
    user = user or current_user
    if not getattr(user, "is_authenticated", False) or getattr(user, "is_admin", False) or getattr(user, "is_banned", False):
        return set()
    return {k for k in (getattr(user, "staff_perms", "") or "").split(",") if k in STAFF_PERMS}


def staff_endpoints(user=None):
    return set().union(*(STAFF_PERMS[k][2] for k in staff_keys(user))) if staff_keys(user) else set()


def staff_home_url(user=None):
    """Primera pantalla del panel que puede abrir: el Resumen si lo tiene; si no, la primera de sus funciones."""
    user = user or current_user
    if getattr(user, "is_admin", False):
        return url_for("admin_home")
    first = {"resumen": "admin_home", "revisar": "admin_clips", "campanas": "admin_campaigns", "retiros": "admin_payouts",
             "destacado": "admin_featured", "distribucion": "admin_dists", "interesados": "admin_leads",
             "clippers": "admin_clippers", "mensajes": "admin_inbox", "avisos": "admin_notify", "formacion": "admin_courses",
             "mods": "admin_mods"}
    keys = staff_keys(user)
    for k in STAFF_PERMS:
        if k in keys:
            return url_for(first[k])
    return url_for("index")
ENDPOINT_PERM = {"admin_campaign_review": "revisar", "admin_review": ("revisar", "verificar"), "admin_review_bulk": "revisar",
                 "admin_campaign_requests": "solicitudes", "admin_invites": "solicitudes", "admin_campaign_member": "solicitudes",
                 "admin_campaign_account": "solicitudes", "admin_campaign_channels": ("canales", "gestionar_canales"),
                 "admin_campaign_support": "soporte", "admin_campaign": ("estadisticas", "informe"),
                 "admin_campaign_clippers": "clippers", "admin_campaign_csv": "csv", "admin_campaign_form": "editar",
                 "admin_campaign_state": ("estado", "dinero"), "admin_campaign_close": "estado"}


def mod_perms(user, cid):
    if not getattr(user, "is_authenticated", False) or not cid:
        return set()
    m = CampaignMod.query.filter_by(campaign_id=cid, user_id=user.id).first()
    general = set().union(*(STAFF_PERMS[k][3] for k in staff_keys(user))) if staff_keys(user) else set()
    return (m.perm_set if m else set()) | set(general)             # lo de esta campaña + lo que le dan sus funciones generales


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
MOD_CHANNEL_ACTS = {"publicar", "post_editar", "post_borrar", "post_fijar", "llamada_nueva", "llamada_editar", "llamada_borrar"}


DIST_STATUS = "distribucion"     # plan de distribución: cuentas del cliente que lleva la agencia (no es una campaña de clippers)

RESOURCE_ICONS = {   # biblioteca de iconos para los recursos de una campaña
    "folder": "Carpeta", "film": "Vídeos", "music": "Sonido", "doc": "Guía", "bulb": "Ideas", "cap": "Curso",
    "play": "Ejemplos", "trend": "Tendencias", "stack": "Plantillas", "link": "Enlace",
    "tiktok": "TikTok", "instagram": "Instagram", "youtube": "YouTube",
}

DEFAULT_CHANNELS = [
    ("Empieza aquí", [("📜", "Normas", "rules")]),
    ("Comunidad", [("📣", "Anuncios", "posts"), ("📞", "Llamadas", "calls")]),
    ("Contenido y herramientas", [("🏆", "Cuentas TOP", "top"), ("💡", "Ideas virales", "posts"),
                                  ("📁", "Recursos", "resources"), ("🔥", "Vídeos virales", "winners")]),
]
CHANNEL_ICONS = {"rules": "doc", "calls": "phone", "top": "trophy", "resources": "folder", "winners": "trend"}


def chan_icon(chan):
    """Icono de línea de un canal del hub (ya no se pintan emojis): según su tipo y, en los de publicaciones, su nombre."""
    kind = getattr(chan, "kind", "") or ""
    if kind in CHANNEL_ICONS:
        return CHANNEL_ICONS[kind]
    name = (getattr(chan, "name", "") or "").lower()
    return "bulb" if "idea" in name else "megaphone"


CHANNEL_KINDS = {"posts": "Publicaciones", "calls": "Llamadas", "top": "Cuentas TOP (automático)",
                 "winners": "Vídeos virales (automático)"}   # los que se pueden crear a mano (normas y recursos ya vienen)


def upgrade_channels(camp, sections):
    """Campañas creadas antes: «Cuentas TOP» pasa a ser automático, «Clips ganadores» a «Vídeos virales» y se añade «Llamadas»."""
    changed = False
    chans = [c for s in sections for c in s.channels]
    for c in chans:
        if c.kind == "posts" and c.name == "Cuentas TOP" and not c.posts:
            c.kind, changed = "top", True
        if c.kind == "winners" and c.name == "Clips ganadores":
            c.name, c.emoji, changed = "Vídeos virales", "🔥", True
    if not any(c.kind == "calls" for c in chans) and not get_setting(f"calls_ch_{camp.id}", ""):
        sec = next((s for s in sections if s.title == "Comunidad" and not s.deleted_at), None) or next(
            (s for s in sections if not s.deleted_at), None)
        if sec:
            db.session.add(Channel(campaign_id=camp.id, section_id=sec.id, name="Llamadas", emoji="📞", kind="calls",
                                   position=len(sec.channels)))
            changed = True
        db.session.merge(Setting(key=f"calls_ch_{camp.id}", value="1"))   # solo una vez: si lo borran, no vuelve
        changed = True
    if changed:
        db.session.commit()
    return changed


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


def campaign_channels(camp, deleted=False):
    """Secciones de la campaña (sin las borradas). Con deleted=True, todas (para restaurar)."""
    ensure_channels(camp)
    q = lambda: (ChannelSection.query.filter_by(campaign_id=camp.id).order_by(ChannelSection.position, ChannelSection.id)  # noqa: E731
                 .options(selectinload(ChannelSection.channels)).all())
    secs = q()
    if upgrade_channels(camp, secs):
        secs = q()
    return secs if deleted else [s for s in secs if not s.deleted_at]


def is_campaign_member(user, camp):
    if getattr(user, "is_admin", False) or is_mod(user, camp.id):     # los mods de la campaña también ven y comentan el hub
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


def near_min_notice(sub):
    """Aviso (una vez por clip) cuando un clip que aún cuenta vistas vale ya el 80 % del pago mínimo de su red."""
    camp = sub.campaign
    low = camp.min_for(sub.platform)
    if not low:
        return False
    value = (sub.views or 0) / 1_000_000 * camp.rate_for(sub.platform)
    if not (low * 0.8 <= value < low):
        return False
    url = f"/mis-clips?clip={sub.id}"
    if Notice.query.filter_by(user_id=sub.user_id, kind="minimo_cerca", url=url).first():
        return False
    notify_user(sub.user_id, "minimo_cerca", f"Tu clip está a punto de cobrar · {camp.song}",
                f"Ya vale {money(value)} y el mínimo es {money(low)}. Unas pocas vistas más y pasa a revisión.", url, camp_id=camp.id)
    return True


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
        if CLIPPER_WITHDRAW:
            fin = (f"Ya puedes retirar {money(amount)}. Ve a Ganancias y pulsa «Retirar».",
                   f"La campaña «{camp.song}» ha terminado. Ya puedes retirar lo que ganaste en ella: "
                   f"{money(amount)}. Ve a Ganancias y pulsa «Retirar».")
        else:
            fin = (f"Has ganado {money(amount)}. Revisamos la campaña y te lo enviamos a tu Whop.",
                   f"La campaña «{camp.song}» ha terminado. Has ganado {money(amount)}: revisamos los clips y te lo "
                   "enviamos a tu Whop en los próximos días. Si aún no lo tienes conectado, conéctalo en Ajustes → Cobros.")
        notify_user(uid, "campana_fin", f"{camp.song} ha terminado", fin[0], "/ganancias", camp_id=camp.id)
        db.session.add(Message(user_id=uid, from_admin=True, sender_id=sender_id, read_by_admin=True, read_by_user=False,
                               body=fin[1]))


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


def _camp_aggs():
    """Clips y vistas de cada campaña por estado, en UNA consulta (y una sola vez por página).
    Antes se cargaban todos los clips de todas las campañas para sumarlos uno a uno."""
    def load():
        out = {}
        q = (db.session.query(Submission.campaign_id, Submission.status, db.func.count(Submission.id),
                              db.func.coalesce(db.func.sum(Submission.views), 0))
             .filter(Submission.status.in_(("aprobado", "pendiente")))
             .group_by(Submission.campaign_id, Submission.status))
        for cid, st, n, v in q:
            out.setdefault(cid, {})[st] = (int(n), int(v or 0))
        return out
    return ttl_cache("camp_aggs", 30, load)


def campaign_stats(camp):
    ag = _camp_aggs().get(camp.id, {})
    clips, views = ag.get("aprobado", (0, 0))
    pending = ag.get("pendiente", (0, 0))[0]
    spent = cents(camp.spent)
    pct = min(100, round(spent / camp.budget * 100)) if camp.budget else 100
    return {
        "spent": spent,
        "remaining": camp.remaining,
        "pct": pct,
        "left_pct": 0 if camp.exhausted else max(0, 100 - pct),
        "views": views,
        "clips": clips,
        "pending": pending,
        "exhausted": camp.exhausted,
        "effective_cpm": cents(spent / (views or 1) * 1_000_000) if clips else 0,
    }


METRICS = ("views", "likes", "comments", "shares")


def campaign_analytics(camp, max_days=180):
    """Estadísticas de una campaña para el panel: totales, por red y la evolución día a día de cada métrica.
    Los días sin lectura arrastran la última conocida. Si un clip no tiene historial de me gusta / comentarios /
    compartidos (se empezaron a guardar después), se estiman en proporción a sus vistas de ese momento.
    Solo se leen las columnas necesarias y cada lectura suma su diferencia en su día (no se recorre clip × día)."""
    subs = (db.session.query(Submission.id, Submission.created_at, Submission.platform, Submission.views,
                             Submission.likes, Submission.comments, Submission.shares)
            .filter(Submission.campaign_id == camp.id, Submission.status == "aprobado").all())
    cur = {s.id: (s.views or 0, s.likes or 0, s.comments or 0, s.shares or 0) for s in subs}
    totals = {m: sum(v[k] for v in cur.values()) for k, m in enumerate(METRICS)}
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
    now = datetime.utcnow()
    today = now.date()
    start = max(min(s.created_at.date() for s in subs), today - timedelta(days=max_days - 1))
    n = (today - start).days + 1
    diff = [[0] * n for _ in METRICS]
    last = {s.id: [0, 0, 0, 0] for s in subs}

    def put(sid, when, vals):
        i = (when.date() - start).days
        if i >= n:                                   # una lectura «del futuro»: no entra en ningún día
            return
        i = max(i, 0)
        prev = last[sid]
        for k in range(4):
            d = vals[k] - prev[k]
            if d:
                diff[k][i] += d
        last[sid] = vals

    q = (db.session.query(ViewSnapshot.submission_id, ViewSnapshot.taken_at, ViewSnapshot.views, ViewSnapshot.likes,
                          ViewSnapshot.comments, ViewSnapshot.shares)
         .join(Submission, Submission.id == ViewSnapshot.submission_id)
         .filter(Submission.campaign_id == camp.id, Submission.status == "aprobado")
         .order_by(ViewSnapshot.taken_at))
    for sid, when, v, lk, cm, sh in q:
        c = cur.get(sid)
        if c is None or when > now:
            continue
        v = v or 0
        cv = c[0]
        put(sid, when, [v,
                        lk if lk is not None else round(v * (c[1] / cv if cv else 0)),
                        cm if cm is not None else round(v * (c[2] / cv if cv else 0)),
                        sh if sh is not None else round(v * (c[3] / cv if cv else 0))])
    for sid, c in cur.items():                       # la lectura de ahora
        put(sid, now, list(c))
    cum = {}
    for k, m in enumerate(METRICS):
        run, col = 0, []
        for d in diff[k]:
            run += d
            col.append(run)
        cum[m] = col
    days = [start + timedelta(days=i) for i in range(n)]
    daily = {m: [max(0, cum[m][i] - (cum[m][i - 1] if i else 0)) for i in range(n)] for m in METRICS}
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
    now = datetime.utcnow()
    n = len(dates)
    if live:                                            # vistas, me gusta…: la última lectura de cada día (se arrastra)
        cur = {s.id: (s.views or 0, s.likes or 0, s.comments or 0, s.shares or 0) for s in live}
        last = {sid: [0, 0, 0, 0] for sid in cur}
        diff = [[0] * n for _ in METRICS]

        def put(sid, when, vals):                       # cada lectura suma su diferencia en su día (no clip × día)
            i = (when.date() - start).days
            if i >= n:
                return
            i = max(i, 0)
            prev = last[sid]
            for k in range(4):
                d = vals[k] - prev[k]
                if d:
                    diff[k][i] += d
            last[sid] = vals
        ids = list(cur)
        for a in range(0, len(ids), 500):
            for sid, when, v, lk, cm, sh in (db.session.query(ViewSnapshot.submission_id, ViewSnapshot.taken_at, ViewSnapshot.views,
                                                              ViewSnapshot.likes, ViewSnapshot.comments, ViewSnapshot.shares)
                                             .filter(ViewSnapshot.submission_id.in_(ids[a:a + 500])).order_by(ViewSnapshot.taken_at)):
                if when > now:
                    continue
                c = cur[sid]
                v = v or 0
                cv = c[0]
                put(sid, when, [v, lk if lk is not None else round(v * (c[1] / cv if cv else 0)),
                                cm if cm is not None else round(v * (c[2] / cv if cv else 0)),
                                sh if sh is not None else round(v * (c[3] / cv if cv else 0))])
        for sid, c in cur.items():
            put(sid, now, list(c))
        for k, m in enumerate(METRICS):
            run = 0
            for i2 in range(n):
                run += diff[k][i2]
                cum[m][i2] = run
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
    earned_rows, live_rows, pending_rows = [], [], []
    for s in user.submissions:
        if s.status == "aprobado":
            (earned_rows if s.campaign.status == "finalizada" else live_rows).append((s, cents(s.earned)))
        elif s.status == "pendiente":
            pending_rows.append((s, min(raw_earning(s, s.campaign), s.campaign.remaining)))
    closed = cents(sum(a for _, a in earned_rows))
    live = cents(sum(a for _, a in live_rows))
    earned = cents(closed + live)
    withdrawn = cents(sum(p.amount for p in user.payouts if p.status in PAYOUT_OPEN + ("pagado",)))
    paid = cents(sum(p.amount for p in user.payouts if p.status == "pagado"))
    balance = cents(closed - withdrawn)
    # Saldo por creador: cada creador paga desde su Whop lo que se ganó en SUS campañas
    by_creator, on_cr = {}, creators_on()
    for s_, a in earned_rows:
        k = s_.campaign.creator_id if on_cr else None
        by_creator[k] = by_creator.get(k, 0) + a
    for p in user.payouts:
        if p.status in PAYOUT_OPEN + ("pagado",):
            k = p.creator_id if on_cr else None
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
    """¿El clip viene de una cuenta aceptada por el equipo en esta campaña?
    Auditoría 24 nov: si aún no sabemos de qué cuenta es el vídeo (Instagram o YouTube enviados a mano, antes de la
    primera lectura), NO cuenta como verificado. Antes bastaba con tener alguna cuenta aceptada, y así se podía enviar y
    aprobar el vídeo viral de otra persona y cobrarlo."""
    if not sub.author:
        return False
    ok = {a.handle for a in campaign_accounts(sub.user_id, sub.campaign_id, "aceptada", sub.platform)}
    return sub.author in ok


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


READ_DAILY_DAYS = int(os.environ.get("READ_DAILY_DAYS", "14"))     # clips más nuevos que esto: lectura diaria
READ_OLD_EVERY = float(os.environ.get("READ_OLD_EVERY", "3"))       # los más antiguos: cada tantos días
APIFY_DAILY_MAX = int(os.environ.get("APIFY_DAILY_MAX", "800"))     # resultados de Apify al día como mucho (0 = sin tope)


def _drop(objs):
    """Suelta de la memoria una tanda de clips ya guardada (con decenas de miles, no caben todos a la vez)."""
    for o in objs:
        try:
            db.session.expunge(o)
        except Exception:  # noqa: BLE001
            pass


def read_due(sub, now):
    """¿Toca leer hoy las vistas de este clip? Los nuevos, cada día; los antiguos (que ya casi no crecen), cada 3 días."""
    age = now - (sub.posted_at or sub.created_at or now)
    if age < timedelta(days=READ_DAILY_DAYS) or not sub.checked_at:
        return True
    return now - sub.checked_at >= timedelta(days=READ_OLD_EVERY) - timedelta(hours=6)


def apify_daily_max():
    try:
        return int(get_setting("apify_daily_max", "") or APIFY_DAILY_MAX)
    except ValueError:
        return APIFY_DAILY_MAX


def apify_used_today():
    try:
        st = json.loads(get_setting("apify_day", "") or "{}")
    except ValueError:
        st = {}
    return int(st.get("n", 0)) if st.get("day") == _local().strftime("%Y-%m-%d") else 0


def apify_left():
    """Resultados de Apify que quedan hoy antes del tope (cada resultado cuesta dinero). Sin tope: muchos."""
    top = apify_daily_max()
    return 10 ** 9 if top <= 0 else max(0, top - apify_used_today())


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
    if isinstance(info, dict) and info.get("video_url") and (sub.platform != "tiktok" or "apify" in str(info["video_url"])):
        sub.media_url = str(info["video_url"])[:1000]       # el enlace directo de TikTok pide cookies: solo vale el de Apify
    if isinstance(info, dict) and info.get("thumb"):
        sub.thumb_url = str(info["thumb"])[:1000]
        if not sub.thumb_ok:                                   # la portada se guarda una vez, al llegar (luego caduca)
            keep_image(sub, "thumb", sub.thumb_url, (240, 320))
    if isinstance(info, dict) and info.get("caption") and not sub.caption:
        sub.caption = str(info["caption"])[:300]
    if isinstance(info, dict) and info.get("avatar") and (author or sub.author):   # foto de la cuenta, también al leer vistas
        h = viewcounter.clean_handle(author or sub.author)
        acc = CampaignAccount.query.filter_by(campaign_id=sub.campaign_id, platform=sub.platform, handle=h).first()
        if acc and (acc.avatar_url != info["avatar"] or not acc.avatar_ok):
            acc.avatar_url = str(info["avatar"])[:1000]
            keep_image(acc, "avatar", acc.avatar_url, (96, 96))
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
             .filter(Campaign.status.in_(("activa", DIST_STATUS)), Submission.status != "rechazado"))
        if REFRESH_DAYS > 0:
            q = q.filter(Submission.created_at >= datetime.utcnow() - timedelta(days=REFRESH_DAYS))
        # Con decenas de miles de clips no se cargan todos a la vez: primero solo sus números, luego por tandas
        all_ids = [i for (i,) in q.with_entities(Submission.id).order_by(Submission.created_at, Submission.id)]
        apply_schedule()
        now0 = datetime.utcnow()
        todo, live_n, waiting = {p: [] for p in PLATFORMS}, 0, 0
        for a in range(0, len(all_ids), 1000):
            page = (Submission.query.options(joinedload(Submission.user), joinedload(Submission.campaign))
                    .filter(Submission.id.in_(all_ids[a:a + 1000])).all())
            for s in page:
                if is_fake(s) or not (s.campaign.status == DIST_STATUS
                                      or (not s.campaign.exhausted and not earn_window_closed(s, now0))):
                    continue
                live_n += 1
                if s.id in _api_fresh:                    # ya lo leyó hoy la API oficial o el perfil (gratis)
                    continue
                if not read_due(s, now0):                 # clip antiguo: no toca hoy
                    waiting += 1
                    continue
                todo.setdefault(s.platform, []).append((s.checked_at or datetime.min, s.id))
            _drop(page)
        read, errors, deferred = 0, [], 0
        for platform in PLATFORMS:
            ids = [i for _, i in sorted(todo.get(platform, []))]      # primero los que llevan más tiempo sin leerse
            if api_mode() != "off" and ids:               # clips de cuentas conectadas: por la API; el resto, a Apify
                rest = []
                for a in range(0, len(ids), 500):
                    objs = Submission.query.filter(Submission.id.in_(ids[a:a + 500])).all()
                    rest += [s.id for s in api_refresh(platform, objs)]
                    read += sum(1 for s in objs if s.id in _api_fresh)
                    db.session.commit()
                    _drop(objs)
                keep = set(rest)
                ids = [i for i in ids if i in keep]
            if api_mode() == "solo":
                ids = []                                  # modo «solo APIs»: no se gasta nada en Apify
            for a in range(0, len(ids), 50):
                left = apify_left()
                take = ids[a:a + min(50, left)] if left > 0 else []
                if not take:                              # tope diario de Apify: lo que falta, mañana (los más atrasados primero)
                    deferred += len(ids) - a
                    break
                chunk = Submission.query.filter(Submission.id.in_(take)).all()
                try:
                    found = viewcounter.fetch_views(platform, [s.url for s in chunk])
                except Exception as exc:  # noqa: BLE001
                    log.exception("Error de Apify (%s)", platform)
                    errors.append(f"{PLATFORMS[platform]}: {exc}")
                    for s in chunk:
                        s.views_error = "Error al consultar Apify"
                    db.session.commit()
                    _drop(chunk)
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
                _drop(chunk)
                if len(take) < len(ids[a:a + 50]):        # el tope cortó esta tanda
                    deferred += len(ids) - a - len(take)
                    break
        set_setting("apify_deferred", json.dumps({"day": _local().strftime("%Y-%m-%d"), "n": deferred}))
        # Reparto del dinero por orden de envío, hasta agotar cada presupuesto
        assigned = 0.0
        auto = {"aprobado": 0, "rechazado": 0}
        pend = (Submission.query.join(Campaign)
                .filter(Campaign.status == "activa", Submission.status == "pendiente").all())
        near_on = auto_on("minimo_cerca")
        for s in pend:                                   # reglas automáticas (también caduca lo que no llegó al mínimo)
            if not s.review_since and reached_min(s):
                s.review_since = datetime.utcnow()
            if is_fake(s):
                continue
            if near_on and not s.review_since:
                near_min_notice(s)
            r = auto_review(s)
            if r:
                auto[r] += 1
        db.session.commit()
        _drop(pend)
        ok_ids = [i for (i,) in q.with_entities(Submission.id).filter(Submission.status == "aprobado")
                  .order_by(Submission.created_at, Submission.id)]
        for a in range(0, len(ok_ids), 1000):             # por tandas, en el mismo orden de envío
            page = (Submission.query.options(joinedload(Submission.user), joinedload(Submission.campaign))
                    .filter(Submission.id.in_(ok_ids[a:a + 1000])).all())
            for s in sorted(page, key=lambda s: (s.created_at, s.id)):
                if is_fake(s) or s.campaign.status != "activa" and s.campaign.status != DIST_STATUS:
                    continue
                if s.campaign.exhausted or earn_window_closed(s, now0):
                    continue
                if cents(raw_earning(s, s.campaign) - float(s.earned or 0)) < CENT:
                    continue                              # ya tiene lo que le toca: sin tocar el presupuesto
                assigned += accrue(s)
            db.session.commit()
            _drop(page)
        subs_n = live_n
        db.session.commit()
        budget_alerts()
        try:
            copy_review_videos()
        except Exception:  # noqa: BLE001
            log.exception("Fallo copiando vídeos a Bunny")
        msg = f"Vistas leídas en {read + len(_api_fresh)} de {subs_n} clips · {money(assigned)} repartidos."
        if waiting:
            msg += f" {waiting} clips antiguos no tocaban hoy."
        if deferred:
            msg += f" Tope diario de Apify: {deferred} clips se leerán mañana."
        if auto["aprobado"] or auto["rechazado"]:
            msg += f" Automático: {auto['aprobado']} aprobados, {auto['rechazado']} rechazados."
        if errors:
            msg += " Errores: " + " | ".join(errors)
        log.info(msg)
        _ttl.clear()                                   # cifras nuevas: fuera lo guardado (ranking, totales de campaña…)
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
            if auto_on("presupuesto_bajo"):
                for (uid,) in db.session.query(CampaignMember.user_id).filter_by(campaign_id=c.id, status="aceptada").all():
                    notify_user(uid, "presupuesto_bajo", f"A {c.song} le queda poco dinero",
                                f"Quedan {money(c.remaining, 0)} por repartir. Sube tus clips ya para no quedarte fuera.",
                                f"/campana/{c.id}", camp_id=c.id)
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
    return copy_videos_now()[0]


def bunny_test():
    """(ok, mensaje): comprueba que las variables llegan y que la clave de Bunny funciona."""
    if not BUNNY_LIBRARY_ID or not BUNNY_API_KEY:
        falta = [n for n, v in (("BUNNY_LIBRARY_ID", BUNNY_LIBRARY_ID), ("BUNNY_API_KEY", BUNNY_API_KEY)) if not v]
        return False, "Falta en Railway: " + ", ".join(falta)
    try:
        ok, code, data = bunny_call("GET", "/videos", params={"page": 1, "itemsPerPage": 1})
    except requests.RequestException as e:
        return False, f"No se pudo conectar con Bunny: {e.__class__.__name__}"
    if code == 401:
        return False, "Bunny rechaza la clave (401): revisa BUNNY_API_KEY (es la clave de la librería, pestaña API)"
    if code == 404:
        return False, "Bunny no encuentra la librería (404): revisa BUNNY_LIBRARY_ID"
    if not ok:
        return False, f"Bunny respondió {code}"
    return True, f"Conexión correcta · {data.get('totalItems', 0)} vídeos en la librería"


def copy_candidates(ids=None):
    q = Submission.query.filter(Submission.status == "pendiente", Submission.video_guid == "",
                                Submission.platform.in_(("tiktok", "instagram")))
    if ids:
        q = q.filter(Submission.id.in_(ids))
    else:
        q = q.filter(Submission.video_tries < 3)
    subs = q.options(joinedload(Submission.user), joinedload(Submission.campaign)).order_by(Submission.created_at).all()
    return [s_ for s_ in subs if not is_fake(s_) and clip_stage(s_) != "contando"]       # (antes: 2 consultas por clip)


def poll_bunny_copies():
    """Mira en Bunny si las copias que se están subiendo ya están listas."""
    if not bunny_ready():
        return
    for s_ in Submission.query.filter(Submission.video_status == "subiendo").limit(100).all():
        try:
            ok, code, data = bunny_call("GET", f"/videos/{s_.video_guid}")
        except requests.RequestException:
            continue
        if ok:
            st = data.get("status")
            s_.video_status = {4: "listo", 5: "error", 6: "error"}.get(st, "subiendo")
            if s_.video_status == "error":
                s_.video_error = "Bunny no pudo procesar el vídeo"
        elif code == 404:
            s_.video_guid, s_.video_status, s_.video_error = "", "error", "El vídeo ya no está en Bunny"
    db.session.commit()


def _bunny_upload(s_, content):
    """Sube un vídeo ya descargado a Bunny y deja el clip en «subiendo»."""
    if len(content) < 10_000 or len(content) > 300 * 1024 * 1024:
        raise RuntimeError(f"la descarga no parece un vídeo ({len(content)} bytes)")
    ok, code, data = bunny_call("POST", "/videos", json={"title": f"Clip {s_.id} · {s_.campaign.song}"[:200]})
    if not ok or not data.get("guid"):
        raise RuntimeError(f"Bunny no dejó crear el vídeo ({code})")
    guid = data["guid"]
    up = requests.put(f"https://video.bunnycdn.com/library/{BUNNY_LIBRARY_ID}/videos/{guid}", data=content,
                      headers={"AccessKey": BUNNY_API_KEY, "Content-Type": "application/octet-stream"}, timeout=180)
    if up.status_code >= 300:
        raise RuntimeError(f"la subida a Bunny dio {up.status_code}")
    s_.video_guid, s_.video_status, s_.video_error = guid, "subiendo", ""


def copy_videos_now(ids=None, limit=30):
    """Copia a Bunny los clips que ya están en revisión, de uno en uno (descargar → subir → soltar la memoria).
    Devuelve (copiados, [(clip, resultado)])."""
    if not bunny_ready():
        return 0, []
    todo = copy_candidates(ids)[:limit]
    report, done, later = [], 0, []
    OK = "copiado: Bunny lo está procesando (1–3 min)"

    def fail(s_, msg):
        s_.video_status, s_.video_error = "error", str(msg)[:300]
        s_.media_url = ""                            # la próxima vez se pide un enlace nuevo
        report.append((s_, s_.video_error))

    # 1) TikTok con yt-dlp (gratis): cada vídeo se sube nada más descargarlo; nunca hay más de uno en memoria
    for s_ in todo:
        if not (s_.platform == "tiktok" and search_engine() == "ytdlp"):
            later.append(s_)
            continue
        try:
            content = viewcounter.ytdlp_download(s_.url)
        except Exception as e:  # noqa: BLE001
            log.warning("yt-dlp no pudo descargar el clip %s: %s", s_.id, e)
            later.append(s_)                         # este, por Apify
            continue
        s_.video_tries = (s_.video_tries or 0) + 1
        try:
            _bunny_upload(s_, content)
            done += 1
            report.append((s_, OK))
        except Exception as e:  # noqa: BLE001
            log.warning("No se pudo copiar el vídeo del clip %s: %s", s_.id, e)
            fail(s_, e)
        del content
        db.session.commit()
    # 2) El resto (Instagram, y los TikTok que yt-dlp no pudo): con el enlace de descarga de Apify
    tiktok = [s_ for s_ in later if s_.platform == "tiktok" and "apify" not in (s_.media_url or "")]
    if tiktok and api_mode() == "solo":              # sin Apify no hay enlace de descarga de TikTok
        for s_ in tiktok:
            report.append((s_, "sin copiar: en modo «solo APIs» no se puede descargar de TikTok"))
        later = [s_ for s_ in later if s_ not in tiktok]
        tiktok = []
    if tiktok:
        try:
            links = viewcounter.fetch_download_urls("tiktok", [s_.url for s_ in tiktok])
        except Exception as e:  # noqa: BLE001
            log.warning("Apify no dio los vídeos de TikTok: %s", e)
            links = {}
            for s_ in tiktok:
                s_.video_error = f"Apify falló al pedir la descarga: {e}"[:300]
        for s_ in tiktok:
            s_.media_url = (links.get(s_.video_key.split(":", 1)[1]) or "")[:1000]
    for s_ in later:
        s_.video_tries = (s_.video_tries or 0) + 1
        if not s_.media_url:
            s_.video_status = "error"
            s_.video_error = (s_.video_error if (s_.video_error or "").startswith("Apify") else
                              "Apify no guardó el vídeo (no devolvió mediaUrls): vídeo privado, borrado o Apify no pudo descargarlo")[:300]
            report.append((s_, s_.video_error))
            db.session.commit()
            continue
        try:
            r = viewcounter.download_video(s_.media_url)
            if r.status_code >= 400:
                raise RuntimeError(f"la descarga del vídeo dio {r.status_code} desde {urlparse(s_.media_url).netloc}")
            _bunny_upload(s_, r.content)
            done += 1
            report.append((s_, OK))
        except Exception as e:  # noqa: BLE001
            log.warning("No se pudo copiar el vídeo del clip %s: %s", s_.id, e)
            fail(s_, e)
        r = None
        db.session.commit()
    poll_bunny_copies()
    return done, report


_bunny_lock = GlobalLock(731903)         # una sola copia de vídeos a la vez


def copy_videos_background(limit=15):
    """«Copiar vídeos ahora» sin bloquear la web: corre aparte y deja el informe en el ajuste bunny_report."""
    def run():
        with app.app_context():
            if not _bunny_lock.acquire():
                return
            try:
                set_setting("bunny_report", json.dumps({"running": True, "at": datetime.utcnow().isoformat(timespec="seconds")}))
                db.session.commit()
                done, rep_ = copy_videos_now(limit=limit)
                rows = [[s_.id, f"{s_.campaign.song} · {PLATFORMS.get(s_.platform, s_.platform)}", r] for s_, r in rep_][:15]
                set_setting("bunny_report", json.dumps({"running": False, "done": done, "rows": rows,
                                                        "at": datetime.utcnow().isoformat(timespec="seconds")}))
                db.session.commit()
            except Exception:  # noqa: BLE001
                db.session.rollback()
                log.exception("Fallo copiando vídeos a Bunny en segundo plano")
                set_setting("bunny_report", json.dumps({"running": False, "error": "La copia falló: revisa los logs.",
                                                        "at": datetime.utcnow().isoformat(timespec="seconds")}))
                db.session.commit()
            finally:
                _bunny_lock.release()
    threading.Thread(target=run, daemon=True, name="bunny-copy").start()


def bunny_report():
    try:
        rep_ = json.loads(get_setting("bunny_report", "") or "null")
    except ValueError:
        return None
    if rep_ and rep_.get("running"):                 # si lleva más de 20 min «en marcha», es que se cortó
        try:
            if datetime.utcnow() - datetime.fromisoformat(rep_["at"]) > timedelta(minutes=20):
                rep_ = {"running": False, "error": "La copia se interrumpió. Vuelve a pulsar el botón.", "at": rep_["at"]}
        except (KeyError, ValueError):
            pass
    return rep_


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


# ---------------------------------------------------------------- APIs oficiales (TikTok, Instagram, YouTube)
API_MODES = {"off": "Apagadas (todo con Apify)", "on": "Opcional: quien conecta su cuenta se lee con la API",
             "required": "Obligatoria: para verificar una cuenta hay que conectarla (Apify de respaldo)",
             "solo": "Solo APIs: sin Apify (las cuentas sin conectar no se leen)"}


def api_mode():
    m = get_setting("api_mode", "off")
    return m if m in API_MODES else "off"


def api_ready(platform):
    """¿Se puede conectar esta red? (claves puestas en Railway y las APIs encendidas)."""
    return api_mode() != "off" and official.configured(platform)


def api_required():
    return api_mode() in ("required", "solo")


def _fernet():
    from cryptography.fernet import Fernet
    import hashlib
    key = os.environ.get("TOKEN_KEY") or app.secret_key or "dev"
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(str(key).encode()).digest()))


def enc(text_):
    return _fernet().encrypt(text_.encode()).decode() if text_ else ""


def dec(text_):
    from cryptography.fernet import InvalidToken
    if not text_:
        return ""
    try:
        return _fernet().decrypt(text_.encode()).decode()
    except InvalidToken:
        return ""


def api_token_for(platform, handle):
    """Token que funciona para esa cuenta (o None)."""
    h = viewcounter.clean_handle(handle)
    return (ApiToken.query.filter(ApiToken.platform == platform, db.func.lower(ApiToken.handle) == h,
                                  ApiToken.status == "ok").first() if h else None)


def api_access(tok):
    """El token de acceso, renovado si está a punto de caducar."""
    if tok.expires_at and tok.expires_at - datetime.utcnow() < timedelta(days=1 if tok.platform == "instagram" else 0, minutes=10):
        r = official.refresh_token(tok.platform, dec(tok.refresh_enc), dec(tok.access_enc))
        tok.access_enc = enc(r["access"])
        if r.get("refresh"):
            tok.refresh_enc = enc(r["refresh"])
        tok.expires_at = datetime.utcnow() + timedelta(seconds=int(r.get("expires_in") or 3600))
        db.session.commit()
    return dec(tok.access_enc)


def api_failed(tok, e):
    tok.error = str(e)[:300]
    if getattr(e, "expired", False) and tok.status == "ok":
        tok.status = "caducado"
        notify_user(tok.user_id, "conexion", f"Vuelve a conectar tu {official.LABELS[tok.platform]}",
                    f"El permiso de @{tok.handle} ha caducado. Mientras tanto seguimos contando tus vistas, pero conéctala otra vez.",
                    "/cuenta#redes")
    db.session.commit()


def api_redirect(platform):
    return (BASE_URL or request.host_url.rstrip("/")) + url_for("api_callback", platform=platform)


def api_refresh(platform, subs):
    """Lee por la API oficial las cifras de los clips de cuentas conectadas. Devuelve los que quedan para Apify."""
    if api_mode() == "off" or not subs:
        return subs
    by_tok, rest = {}, []
    for s in subs:
        tok = api_token_for(platform, s.author or "")
        if tok:
            by_tok.setdefault(tok.id, (tok, []))[1].append(s)
        else:
            rest.append(s)
    now = datetime.utcnow()
    for tok, group in by_tok.values():
        try:
            found = official.stats_by_ids(platform, api_access(tok), [s.video_key.split(":", 1)[1] for s in group], tok.handle)
        except official.ApiError as e:
            api_failed(tok, e)
            rest.extend(group)
            continue
        for s in group:
            info = found.get(s.video_key.split(":", 1)[1])
            if info and info.get("views") is not None:
                apply_reading(s, info, now)
                _api_fresh.add(s.id)
            else:
                rest.append(s)                        # no lo encuentra la API (¿borrado?): que lo mire Apify
        tok.last_read_at = now
        db.session.commit()
    return rest


def save_audience(tok, now=None):
    """Una vez al día por cuenta conectada: audiencia de la cuenta y de sus clips recientes (lo que deje cada red)."""
    now = now or datetime.utcnow()
    if tok.audience_at and now - tok.audience_at < timedelta(hours=20):
        return 0
    subs = (Submission.query.filter(Submission.platform == tok.platform, db.func.lower(Submission.author) == tok.handle.lower(),
                                    Submission.created_at >= now - timedelta(days=30), Submission.status != "rechazado").all())
    keys = {s.video_key.split(":", 1)[1]: s for s in subs}
    data = official.audience(tok.platform, api_access(tok), list(keys))
    n = 0
    if data.get("account"):
        AudienceStat.query.filter_by(platform=tok.platform, handle=tok.handle, submission_id=None).delete()
        db.session.add(AudienceStat(platform=tok.platform, handle=tok.handle, data=json.dumps(data["account"]), taken_at=now))
        n += 1
    for vid, d in (data.get("videos") or {}).items():
        s = keys.get(vid)
        if s and d:
            AudienceStat.query.filter_by(submission_id=s.id).delete()
            db.session.add(AudienceStat(platform=tok.platform, handle=tok.handle, submission_id=s.id, data=json.dumps(d), taken_at=now))
            n += 1
    tok.audience_at = now
    db.session.commit()
    return n


def read_audiences():
    """Después de la lectura diaria: la audiencia de todas las cuentas conectadas."""
    if api_mode() == "off":
        return 0
    n = 0
    for tok in ApiToken.query.filter_by(status="ok").all():
        try:
            n += save_audience(tok)
        except official.ApiError as e:
            api_failed(tok, e)
        except Exception:  # noqa: BLE001
            log.exception("Fallo leyendo la audiencia de @%s", tok.handle)
            db.session.rollback()
    return n


def audience_for(subs):
    """Audiencia media (en %) de unos clips, pesada por sus vistas. Usa la del clip y, si no hay, la de su cuenta."""
    if not subs:
        return {}
    ids = [s.id for s in subs]
    aud_rows = AudienceStat.query.filter(AudienceStat.submission_id.in_(ids)).all()
    per_sub = {r.submission_id: json.loads(r.data or "{}") for r in aud_rows}
    demo_only = bool(aud_rows) and all(r.demo for r in aud_rows)      # todo lo que hay es inventado (datos de demostración)
    handles = {(s.platform, (s.author or "").lower()) for s in subs}
    per_acc = {(r.platform, r.handle.lower()): json.loads(r.data or "{}") for r in
               AudienceStat.query.filter(AudienceStat.submission_id.is_(None)).all() if (r.platform, r.handle.lower()) in handles}
    acc = {"country": {}, "age": {}, "gender": {}}
    weight = {"country": 0, "age": 0, "gender": 0}
    sources = 0
    for s in subs:
        d = per_sub.get(s.id) or per_acc.get((s.platform, (s.author or "").lower()))
        if not d:
            continue
        sources += 1
        w = max(s.views or 0, 1)
        for k in acc:
            if d.get(k):
                weight[k] += w
                for key, pct in d[k].items():
                    acc[k][key] = acc[k].get(key, 0) + pct * w
    out = {k: dict(sorted(((key, round(v / weight[k], 1)) for key, v in acc[k].items()), key=lambda kv: -kv[1])[:8])
           for k in acc if weight[k]}
    if out:
        out["clips"] = sources
        out["demo"] = demo_only
    return out


_api_fresh = set()      # clips cuyas cifras ya leyó la API oficial en esta pasada (refresh_views no los manda a Apify)


YTDLP_PARALLEL = int(os.environ.get("YTDLP_PARALLEL", "6"))            # perfiles que se miran a la vez con yt-dlp
YTDLP_PER = int(os.environ.get("YTDLP_PER", "30"))                     # últimos vídeos que se miran de cada cuenta
APIFY_FALLBACK_MAX = int(os.environ.get("APIFY_FALLBACK_MAX", "40"))   # cuentas al día que pueden pasar a Apify si yt-dlp falla
DORMANT_AFTER = 21                                                      # días sin publicar para tratar una cuenta como parada


def scan_today(acc, now):
    """Las cuentas paradas (aceptadas hace tiempo y sin publicar en 3 semanas) se miran un día a la semana, no cada día.
    Con miles de personas que entran, ponen su cuenta y no vuelven, mirarlas a diario no sirve de nada."""
    since = acc.decided_at or acc.created_at or now
    if now - since < timedelta(days=DORMANT_AFTER):
        return True
    if acc.last_post_at and now - acc.last_post_at < timedelta(days=DORMANT_AFTER):
        return True
    return (acc.id + now.toordinal()) % 7 == 0


def ytdlp_stats_on():
    """¿Se usan las cifras que trae el listado de yt-dlp (gratis) en vez de pedirlas a Apify? Por defecto, sí."""
    return get_setting("ytdlp_stats", "on") != "off"


def discover_clips(now=None):
    """Mira el perfil de cada cuenta aceptada en una campaña activa y añade solos los vídeos nuevos.
    Cada cuenta es de una sola campaña, así que todo lo que publica después de entrar es un clip de esa campaña.
    Orden: API oficial si la cuenta está conectada; si no, yt-dlp (gratis, varias cuentas a la vez); si falla, Apify
    (con un máximo de cuentas al día, para que un bloqueo de TikTok no dispare el gasto)."""
    now = now or datetime.utcnow()
    _api_fresh.clear()
    accs = (CampaignAccount.query.join(Campaign)
            .filter(CampaignAccount.status == "aceptada", Campaign.status.in_(("activa", DIST_STATUS)),
                    CampaignAccount.platform.in_(AUTO_PLATFORMS + ("facebook",))).all())
    # Facebook solo se busca solo si la página está conectada con la API oficial (no hay otra forma gratis de leerla)
    accs = [a for a in accs if a.platform != "facebook" or (api_mode() != "off" and api_token_for(a.platform, a.handle))]
    accs = [a for a in accs if not is_fake(a) and (a.campaign.status == DIST_STATUS
            or (not a.campaign.exhausted and a.platform in a.campaign.platform_list))]
    skipped = len(accs)
    accs = [a for a in accs if scan_today(a, now)]
    skipped -= len(accs)
    added, errors = 0, []
    by_platform, with_ytdlp = {}, []
    engine, use_stats = search_engine(), ytdlp_stats_on()
    for a in accs:
        if api_mode() != "off":
            tok = api_token_for(a.platform, a.handle)
            if tok:
                try:
                    items = official.videos(a.platform, api_access(tok), a.handle)
                    tok.last_read_at, tok.error = now, ""
                    a.last_scan_at = now
                    added += _ingest(a.platform, [a], items, now, update_existing=True)
                    continue
                except official.ApiError as e:
                    api_failed(tok, e)
                    errors.append(f"API {PLATFORMS[a.platform]} @{a.handle}: {e}")
                except Exception as e:  # noqa: BLE001
                    log.warning("API oficial de %s falló para @%s: %s", a.platform, a.handle, e)
        if a.platform in viewcounter.YTDLP_PLATFORMS and engine == "ytdlp":
            with_ytdlp.append(a)
        elif api_mode() != "solo":
            by_platform.setdefault(a.platform, []).append(a)   # sin API ni yt-dlp: Apify
    # yt-dlp: se piden varios perfiles a la vez (solo la parte de red); guardar, de uno en uno
    failed = []
    if with_ytdlp:
        jobs = [(a, a.platform, a.handle, _known_vids(a)) for a in with_ytdlp]

        def fetch(job):
            try:
                return job[0], viewcounter.ytdlp_profile_videos(job[1], job[2], per=YTDLP_PER, known=job[3]), None
            except Exception as e:  # noqa: BLE001
                return job[0], None, e
        with ThreadPoolExecutor(max_workers=max(1, YTDLP_PARALLEL)) as pool:
            for a, items, err in pool.map(fetch, jobs):
                if err is not None:
                    ytdlp_note(False, f"@{a.handle} ({PLATFORMS[a.platform]}): {str(err)[:160]}")
                    log.warning("yt-dlp no pudo leer @%s (%s): %s", a.handle, a.platform, err)
                    failed.append(a)
                    continue
                if use_stats:                              # el listado ya trae las cifras: no hace falta pedirlas a Apify
                    for it in items:
                        if it.get("yt_views") is not None:
                            it["views"] = it["yt_views"]
                a.last_scan_at = now
                added += _ingest(a.platform, [a], items, now, update_existing=use_stats)
                ytdlp_note(True)
    if failed and api_mode() != "solo":
        room = failed[:max(0, APIFY_FALLBACK_MAX)]
        for a in room:
            by_platform.setdefault(a.platform, []).append(a)
        if len(failed) > len(room):
            errors.append(f"yt-dlp falló en {len(failed)} cuentas: solo {len(room)} se buscan hoy con Apify (tope de gasto); "
                          f"las otras {len(failed) - len(room)}, mañana")
    for platform, group in by_platform.items():
        for i in range(0, len(group), SCAN_CHUNK):
            chunk = group[i:i + SCAN_CHUNK]
            if apify_left() <= 0:
                errors.append(f"{PLATFORMS[platform]}: tope diario de Apify alcanzado, {len(group) - i} cuentas sin mirar hoy")
                break
            try:
                items = viewcounter.fetch_profile_videos(platform, [a.handle for a in chunk])
            except Exception as e:  # noqa: BLE001
                errors.append(f"{PLATFORMS[platform]}: {e}")
                log.warning("No se pudieron leer perfiles de %s: %s", platform, e)
                continue
            for a in chunk:
                a.last_scan_at = now
            added += _ingest(platform, chunk, items, now, update_existing=True)   # el perfil ya trae las cifras: no se piden otra vez
    db.session.commit()
    set_setting("scan_info", json.dumps({"at": now.isoformat(timespec="seconds"), "scanned": len(accs), "dormant": skipped,
                                         "ytdlp_failed": len(failed), "added": added}))
    if added:
        log.info("Clips detectados solos: %s", added)
    return added, errors


SEARCH_ENGINES = {"ytdlp": "yt-dlp busca los clips de TikTok y YouTube (gratis) · Apify si falla",
                  "apify": "Solo Apify (como antes)"}


def search_engine():
    v = get_setting("search_engine", os.environ.get("SEARCH_ENGINE", "ytdlp"))
    if v == "ytdlp" and not viewcounter.ytdlp_available():
        return "apify"
    return v if v in SEARCH_ENGINES else "ytdlp"


def _known_vids(acc):
    rows = (Submission.query.with_entities(Submission.video_key)
            .filter(Submission.campaign_id == acc.campaign_id, Submission.platform == acc.platform,
                    Submission.user_id == acc.user_id).all())
    return {k.split(":", 1)[1] for (k,) in rows if k and ":" in k}


def ytdlp_note(ok, err=""):
    """Lleva la cuenta de los fallos de yt-dlp de hoy (para el aviso del panel técnico)."""
    day = _local().strftime("%Y-%m-%d")
    try:
        st = json.loads(get_setting("ytdlp_stats", "{}") or "{}")
    except ValueError:
        st = {}
    if st.get("day") != day:
        st = {"day": day, "ok": 0, "fail": 0, "errors": []}
    st["ok" if ok else "fail"] += 1
    if err:
        st["errors"] = ([err] + st.get("errors", []))[:10]
    set_setting("ytdlp_stats", json.dumps(st))


def apify_log(kind, platform, n_in, n_out, code):
    """Apunta cada llamada a Apify (para ver qué es lo que gasta)."""
    try:
        with app.app_context():
            try:
                rows = json.loads(get_setting("apify_log", "[]") or "[]")
            except ValueError:
                rows = []
            rows.insert(0, [datetime.utcnow().isoformat(timespec="seconds"), kind, platform, n_in, n_out, code])
            set_setting("apify_log", json.dumps(rows[:300]))
            day = _local().strftime("%Y-%m-%d")
            try:
                st = json.loads(get_setting("apify_day", "") or "{}")
            except ValueError:
                st = {}
            st = st if st.get("day") == day else {"day": day, "n": 0}
            st["n"] += int(n_out or 0)
            set_setting("apify_day", json.dumps(st))
            db.session.commit()
    except Exception:  # noqa: BLE001
        log.exception("No se pudo apuntar la llamada a Apify")


viewcounter.APIFY_HOOK = apify_log


def apify_summary():
    try:
        rows = json.loads(get_setting("apify_log", "[]") or "[]")
    except ValueError:
        rows = []
    week = (datetime.utcnow() - timedelta(days=7)).isoformat()
    by = {}
    for r in rows:
        if r[0] >= week:
            k = by.setdefault(r[1], {"calls": 0, "items": 0})
            k["calls"] += 1
            k["items"] += r[4] or 0
    return {"rows": rows[:40], "by": by}


YTDLP_CHECK_AT = [x.strip() for x in os.environ.get("YTDLP_CHECK_AT", "03:30,11:00,19:00").split(",") if x.strip()]


YTDLP_CHECK_MAX = int(os.environ.get("YTDLP_CHECK_MAX", "300"))     # cuentas por comprobación como mucho


def ytdlp_check_slot(now=None):
    """Si toca una comprobación de yt-dlp, devuelve su clave (día + hora); si no, None."""
    local = _local(now)
    cur = local.hour * 60 + local.minute
    due = None
    for t in YTDLP_CHECK_AT:
        try:
            hh, mm = (int(x) for x in t.split(":"))
        except ValueError:
            continue
        if cur >= hh * 60 + mm:
            due = f"{local.strftime('%Y-%m-%d')} {t}"
    if due and get_setting("ytdlp_check_slot", "") != due:
        return due
    return None


def ytdlp_check(now=None):
    """Comprobación con yt-dlp que NO cambia nada: compara los perfiles con lo que tiene la web.
    Avisa de vídeos publicados antes de la última búsqueda que no están, de clips que ya no están en el perfil
    y de cuentas que yt-dlp no puede leer."""
    now = now or datetime.utcnow()
    try:
        last_search = datetime.fromisoformat(get_setting("last_refresh_at", ""))
    except ValueError:
        last_search = None
    accs = (CampaignAccount.query.join(Campaign)
            .filter(CampaignAccount.status == "aceptada", Campaign.status.in_(("activa", DIST_STATUS)),
                    CampaignAccount.platform.in_(viewcounter.YTDLP_PLATFORMS)).all())
    accs = [a for a in accs if not is_fake(a) and scan_today(a, now)]      # las paradas no se miran a diario
    if len(accs) > YTDLP_CHECK_MAX:                  # con muchas cuentas, una muestra distinta cada vez
        import random as _rnd
        accs = _rnd.sample(accs, YTDLP_CHECK_MAX)
    issues, ok = [], 0
    jobs = [(a, a.platform, a.handle, _known_vids(a)) for a in accs]

    def fetch(job):
        try:
            return job[0], job[3], viewcounter.ytdlp_profile_videos(job[1], job[2], known=job[3]), None
        except Exception as e:  # noqa: BLE001
            return job[0], job[3], None, e
    with ThreadPoolExecutor(max_workers=max(1, YTDLP_PARALLEL)) as pool:
        results = list(pool.map(fetch, jobs))
    for a, known, items, err in results:
        if err is not None:
            issues.append(["cuenta", f"@{a.handle} ({PLATFORMS[a.platform]}) · {a.campaign.song}",
                           f"yt-dlp no puede leer la cuenta: {str(err)[:150]}", ""])
            continue
        ok += 1
        since = (a.decided_at or a.created_at or now) - timedelta(hours=1)
        listed = {it["vid"] for it in items}
        for it in items:
            p = it.get("posted_at")
            if (it["vid"] not in known and p and last_search and since <= p < last_search
                    and not IgnoredClip.query.filter_by(video_key=f"{a.platform}:{it['vid']}").first()):
                issues.append(["falta", f"@{a.handle} ({PLATFORMS[a.platform]}) · {a.campaign.song}",
                               f"Vídeo publicado el {p:%d/%m %H:%M} (UTC) que la búsqueda no añadió", it["url"]])
        dates = [it["posted_at"] for it in items if it.get("posted_at")]
        if a.platform == "tiktok" and dates and len(items) >= 1:
            oldest = min(dates)
            gone = (Submission.query.filter(Submission.campaign_id == a.campaign_id, Submission.platform == "tiktok",
                                            Submission.user_id == a.user_id, Submission.status != "rechazado",
                                            Submission.posted_at.isnot(None), Submission.posted_at >= oldest).all())
            for s_ in gone:
                if viewcounter.clean_handle(s_.author or "") == viewcounter.clean_handle(a.handle) \
                        and s_.video_key.split(":", 1)[1] not in listed:
                    issues.append(["borrado", f"@{a.handle} (TikTok) · {a.campaign.song}",
                                   f"El clip #{s_.id} ya no está en el perfil (¿borrado o privado?)", s_.url])
    res = {"at": now.isoformat(timespec="seconds"), "accounts": len(accs), "ok": ok, "issues": issues[:60]}
    prev = get_setting("ytdlp_check", "")
    set_setting("ytdlp_check", json.dumps(res))
    db.session.commit()
    try:
        prev_issues = json.loads(prev).get("issues", []) if prev else []
    except ValueError:
        prev_issues = []
    new = [i for i in issues if i not in prev_issues]
    if new:
        notify_phone(f"🔎 Comprobación de clips: {len(new)} cosas raras",
                     "; ".join(i[2] for i in new[:3])[:300], url_for_safe("admin_settings", "ytdlp"), tags="mag")
    return res


def url_for_safe(endpoint, anchor=""):
    try:
        return url_for(endpoint, _anchor=anchor or None)
    except RuntimeError:
        return f"/admin/ajustes#{anchor}" if anchor else ""


def _ingest(platform, chunk, items, now, update_existing=False):
    """Añade los vídeos nuevos de esas cuentas. Con la API, además actualiza las cifras de los clips que ya estaban."""
    added = 0
    by_handle = {viewcounter.clean_handle(a.handle): a for a in chunk}
    for it in items:
        acc = by_handle.get(viewcounter.clean_handle(it.get("author") or ""))
        if not acc and len(chunk) == 1:
            acc = chunk[0]
        if not acc:
            continue
        posted = it.get("posted_at")
        if posted and (not acc.last_post_at or posted > acc.last_post_at):
            acc.last_post_at = posted
        if it.get("avatar") and (acc.avatar_url != it["avatar"][:1000] or not acc.avatar_ok):
            acc.avatar_url = it["avatar"][:1000]
            keep_image(acc, "avatar", acc.avatar_url, (96, 96))
        key = f"{platform}:{it['vid']}"
        old = Submission.query.filter_by(video_key=key).first()
        if old:
            if update_existing and it.get("views") is not None and old.status != "rechazado":
                apply_reading(old, it, now)                   # la API ya trae las vistas: no hace falta Apify
                _api_fresh.add(old.id)
            continue
        since = (acc.decided_at or acc.created_at or now) - timedelta(hours=1)
        if not posted or posted < since:
            continue                                          # solo cuentan los vídeos publicados después de entrar
        if IgnoredClip.query.filter_by(video_key=key).first():
            continue                                          # el clipper lo borró a propósito
        camp = acc.campaign
        dist = camp.status == DIST_STATUS
        sub = Submission(user_id=acc.user_id, campaign_id=camp.id, url=viewcounter.normalize_url(it["url"], resolve=False),
                         platform=platform, video_key=key, author=acc.handle, source="auto", posted_at=posted,
                         review_since=None if (dist or camp.min_for(platform)) else now)
        if dist:                                          # plan de distribución: sin revisión ni dinero, solo estadísticas
            sub.status, sub.reviewed_at, sub.earned = "aprobado", now, 0
        db.session.add(sub)
        db.session.flush()
        if it.get("views") is not None:
            apply_reading(sub, it, now)
            if update_existing:
                _api_fresh.add(sub.id)
        added += 1
        if not dist:
            notify_user(acc.user_id, "clip_nuevo", f"Clip detectado en {camp.song}",
                        f"Hemos añadido solo tu nuevo vídeo de @{acc.handle}. Ya está contando vistas.",
                        "/mis-clips", camp_id=camp.id)
    db.session.commit()
    return added


def parse_madrid(d, t):
    """«2026-11-13» + «19:00» en hora de España → UTC (con el cambio de hora). None si no es válida."""
    try:
        local = datetime.strptime(f"{d.strip()} {t.strip()}", "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo(APP_TZ))
    except ValueError:
        return None
    return local.astimezone(timezone.utc).replace(tzinfo=None)


DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES_ES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]


def when_madrid(dt, fmt="full"):
    """Fecha UTC → «jueves 13 nov · 19:00» en hora de España."""
    if not dt:
        return ""
    l_ = dt.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(APP_TZ))
    if fmt == "date":
        return l_.strftime("%Y-%m-%d")
    if fmt == "time":
        return l_.strftime("%H:%M")
    return f"{DIAS[l_.weekday()]} {l_.day} {MESES_ES[l_.month - 1]} · {l_.strftime('%H:%M')}"


def inactivity_reminders(now=None):
    """Aviso diario (apagado por defecto: admin → Avisos → «Automáticos»): cuentas de campañas activas que no han publicado en 24 h."""
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
        if not a.last_post_at or now - a.last_post_at > timedelta(days=DORMANT_AFTER):
            continue                                          # cuenta parada (nunca publicó o lleva semanas): sin aviso diario
        st = campaign_stats(camp)
        notify_user(a.user_id, "inactividad", f"@{a.handle} no ha subido ningún clip en 24 h",
                    f"Quedan {money(st['remaining'], 0)} por repartir en {camp.song}. Sube un clip para no quedarte fuera.",
                    f"/campana/{camp.id}", camp_id=camp.id)
        n += 1
    db.session.commit()
    return n


def call_reminders(now=None):
    """30 min antes de cada llamada: aviso a los clippers de la campaña (campanita y móvil). Una sola vez por llamada."""
    now = now or datetime.utcnow()
    n = 0
    for call in Call.query.filter(Call.reminded.is_(False), Call.starts_at > now,
                                  Call.starts_at <= now + timedelta(minutes=35)).all():
        call.reminded = True
        ch = Channel.query.filter_by(campaign_id=call.campaign_id, kind="calls", deleted_at=None).first()
        for m in CampaignMember.query.filter_by(campaign_id=call.campaign_id, status="aceptada").all():
            notify_user(m.user_id, "llamada", "La llamada empieza en 30 min",
                        f"{call.title} · {call.campaign.song}. Entra desde el canal de llamadas.",
                        f"/campana/{call.campaign_id}" + (f"?canal={ch.id}" if ch else ""), camp_id=call.campaign_id)
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
REFRESH_AT = (os.environ.get("REFRESH_AT", "") or "").strip() or "03:00"    # siempre una vez al día (las lecturas cuestan)      # hora fija (España) de la lectura diaria: clips nuevos + vistas


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


REFRESH_TRIES = 3            # intentos al día de la lectura diaria
REFRESH_RETRY_MIN = 45       # si un intento no termina (reinicio, fallo), se reintenta pasado este tiempo


def _refresh_run():
    try:
        st = json.loads(get_setting("refresh_run", "") or "{}")
    except ValueError:
        st = {}
    today = _local().strftime("%Y-%m-%d")
    return st if st.get("day") == today else {"day": today, "tries": 0}


def job_mark(name, ok=True, info="", started=None):
    """Apunta cómo salió una tarea automática (Panel técnico → Salud → Tareas automáticas)."""
    now = datetime.utcnow()
    secs = (now - started).total_seconds() if started else None
    set_setting("job:" + name, json.dumps({"at": now.isoformat(timespec="seconds"), "ok": bool(ok), "info": str(info)[:300],
                                          "secs": round(secs) if secs is not None else None}))


def job_info(name):
    try:
        return json.loads(get_setting("job:" + name, "") or "{}")
    except ValueError:
        return {}


def run_daily_refresh(now=None):
    """La lectura diaria (perfiles, vistas, dinero). El día solo se da por hecho CUANDO TERMINA: si la web se reinicia
    a mitad o falla, se reintenta a los 45 min (hasta 3 veces) y, si no sale, avisa al móvil. Devuelve True si se completó."""
    now = now or datetime.utcnow()
    st = _refresh_run()
    if st.get("done") or st["tries"] >= REFRESH_TRIES:
        return False
    if st.get("started"):
        try:
            if now - datetime.fromisoformat(st["started"]) < timedelta(minutes=REFRESH_RETRY_MIN):
                return False                          # hay un intento en marcha (o recién fallado): se espera
        except ValueError:
            pass
    st["tries"] += 1
    st["started"] = now.isoformat(timespec="seconds")
    set_setting("refresh_run", json.dumps(st))
    set_setting("last_refresh_at", now.isoformat())
    db.session.commit()
    ok = False
    try:
        res = refresh_views()                         # primero busca los clips nuevos en los perfiles, luego las vistas
        ok = res != "Ya hay una actualización en marcha."
        if ok:
            read_audiences()                          # audiencia de las cuentas conectadas con la API oficial
            check_creator_balances()
            try:
                snapshot_ranks()
            except Exception:  # noqa: BLE001
                db.session.rollback()
                log.exception("Fallo guardando los puestos de la clasificación")
            try:
                prune_snapshots()
            except Exception:  # noqa: BLE001
                db.session.rollback()
                log.exception("Fallo resumiendo el historial de vistas antiguo")
    except Exception:  # noqa: BLE001
        db.session.rollback()
        log.exception("La lectura diaria falló (intento %s)", st["tries"])
    job_mark("lectura", ok, get_setting("last_refresh", "").split(" · ", 1)[-1] if ok else
             f"No terminó (intento {st['tries']} de {REFRESH_TRIES})", started=now)
    st = _refresh_run()
    if ok:
        st["done"] = True
        set_setting("refresh_day", st["day"])
    elif st["tries"] >= REFRESH_TRIES and not st.get("alerted"):
        st["alerted"] = True
        notify_phone("⚠️ La lectura diaria no ha terminado",
                     f"Se intentó {REFRESH_TRIES} veces y no se completó: hoy no se han leído vistas ni repartido dinero. "
                     "Pulsa «Actualizar vistas» en el Panel técnico.", tags="warning")
    set_setting("refresh_run", json.dumps(st))
    db.session.commit()
    return ok


def refresh_pending():
    """¿Queda por hacer la lectura de hoy y aún se va a intentar?"""
    return _refresh_due() and _refresh_run()["tries"] < REFRESH_TRIES


SNAP_KEEP_DAYS = int(os.environ.get("SNAP_KEEP_DAYS", "90"))


def prune_snapshots(now=None):
    """El historial de vistas crece una fila por clip y día. De lo que tiene más de 90 días se deja una lectura por
    semana y clip (las gráficas antiguas siguen saliendo, con menos detalle). Una vez a la semana, solo en Postgres."""
    now = now or datetime.utcnow()
    week = now.strftime("%G-%V")
    if db.engine.dialect.name != "postgresql" or get_setting("snap_pruned", "") == week:
        return 0
    res = db.session.execute(text("""
        DELETE FROM view_snapshot v USING (
            SELECT id, row_number() OVER (PARTITION BY submission_id, date_trunc('week', taken_at) ORDER BY taken_at DESC) AS rn
            FROM view_snapshot WHERE taken_at < :lim) d
        WHERE v.id = d.id AND d.rn > 1"""), {"lim": now - timedelta(days=SNAP_KEEP_DAYS)})
    set_setting("snap_pruned", week)
    job_mark("historial", True, f"{res.rowcount or 0} lecturas antiguas resumidas")
    db.session.commit()
    return res.rowcount or 0


def check_creator_balances():
    """Una vez al día: si a algún creador no le llega el saldo de su Whop para lo que deben sus campañas, aviso al móvil."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if get_setting("creators_checked", "") == today:
        return
    set_setting("creators_checked", today)
    short = []
    for r in (creators_overview() if creators_on() else []):
        if r["missing"]:
            short.append(f"{r['c'].name}: debe {money(r['owed'])}, tiene {money(r['balance'])} (faltan {money(r['missing'])})")
    set_setting("creators_short_list", json.dumps(short))                # sale como aviso en el panel (los del móvil están apagados)
    job_mark("saldos", not short, f"{len(short)} {'creador' if len(short) == 1 else 'creadores'} sin saldo suficiente" if short
             else "A todos los creadores les llega el saldo")


_leader_lock = GlobalLock(731900)       # quién lleva el reloj interno cuando hay varios procesos


def _auto_refresh_loop():
    """Reloj interno: cada 10 minutos mira si toca la lectura diaria (o la de cada REFRESH_HOURS horas)."""
    time.sleep(90)                          # deja arrancar la web primero
    leader = False
    while True:
        try:
            with app.app_context():
                if not leader:                            # con varios procesos, solo uno lleva las tareas automáticas
                    leader = _leader_lock.acquire()
                if not leader:
                    time.sleep(600)
                    continue
                set_setting("clock_tick", datetime.utcnow().isoformat(timespec="seconds"))   # el reloj interno sigue vivo
                db.session.commit()
                call_reminders()
                try:
                    poll_bunny_copies()
                except Exception:  # noqa: BLE001
                    log.exception("Fallo mirando las copias de Bunny")
                fresh = False
                slot = ytdlp_check_slot()
                if slot and search_engine() == "ytdlp" and not refresh_pending():
                    set_setting("ytdlp_check_slot", slot)
                    db.session.commit()
                    t0 = datetime.utcnow()
                    try:
                        ytdlp_check()
                        job_mark("ytdlp", True, "Comprobación hecha", started=t0)
                    except Exception as e:  # noqa: BLE001
                        log.exception("Fallo en la comprobación con yt-dlp")
                        job_mark("ytdlp", False, str(e)[:200], started=t0)
                    db.session.commit()
                if _refresh_due():
                    fresh = run_daily_refresh()           # el día se marca al terminar; si se corta, se reintenta
                if backup_due() and not refresh_pending():
                    run_offsite_backup()                  # copia de la noche a Bunny Storage
                due, day = reminders_due()
                if due:                                   # justo después de la lectura (el aviso «no has publicado en 24 h» se quitó el 24 nov)
                    set_setting("reminders_sent", day)
                    db.session.commit()
                    if not fresh and not REFRESH_AT:
                        discover_clips()
                    if auto_on("inactividad"):            # solo si Mustapha lo enciende en Avisos → «Automáticos»
                        inactivity_reminders()
                        db.session.commit()
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


CSRF_EXEMPT = {"meta_data_deletion", "landing_lead", "book_call", "browser_error", "mail_unsubscribe"}     # Meta (firmada) y el formulario de la landing de clipprofit.es (otra web)


@app.before_request
def _start_timer():
    g._t0 = time.perf_counter()


CANON_HOST = urlparse(BASE_URL).netloc.lower() if BASE_URL else ""
NO_CANON = {"health", "static"}          # el chequeo de Railway y los archivos no se mandan al dominio


@app.before_request
def _canonical_host():
    """Con BASE_URL puesto (https://app.clipprofit.es), quien entre por la dirección de Railway pasa al dominio propio.
    Solo visitas normales (GET); los avisos de otros servicios (POST) siguen funcionando por cualquiera de las dos."""
    if (CANON_HOST and request.method in ("GET", "HEAD") and request.host.lower() != CANON_HOST
            and request.host.lower().endswith(".up.railway.app") and request.endpoint not in NO_CANON
            and not request.path.startswith("/cron/")):
        return redirect(BASE_URL + request.full_path.rstrip("?"), code=301)


SEEN_EVERY = 120      # cada cuánto se guarda la última visita de un usuario (segundos)
ONLINE_FOR = 300      # si entró hace menos de esto, sale «En línea»


@app.before_request
def before():
    if request.method == "POST" and request.endpoint not in CSRF_EXEMPT:
        sent, expected = request.form.get("_csrf", ""), session.get("_csrf", "")
        if not sent or not expected or not secrets.compare_digest(sent, expected):
            stat_add(csrf=1)
            abort(400, description="csrf")
    if current_user.is_authenticated:
        now = datetime.utcnow()
        if (current_user.is_admin and (request.endpoint or "").startswith("admin_")
                and (not current_user.admin_seen_at or (now - current_user.admin_seen_at).total_seconds() > 300)):
            current_user.admin_seen_at = now                     # Panel técnico → Salud: últimos accesos al panel
            current_user.admin_seen_ua = (request.headers.get("User-Agent") or "")[:200]
            db.session.commit()
        if not current_user.last_seen_at or (now - current_user.last_seen_at).total_seconds() > SEEN_EVERY:
            current_user.last_seen_at = now
            db.session.commit()
        if needs_confirm(current_user) and request.endpoint not in CONFIRM_FREE:
            if request.method == "GET" and not request.path.startswith("/api/"):
                return redirect(url_for("confirm_pending"))
            abort(403)
        if current_user.is_banned and request.endpoint not in ("logout", "static", "terms", "privacy", "health",
                                                               "support", "support_poll", "notices"):
            return render_template("banned.html"), 403
        # Cuenta nueva: hasta completar «Crea tu perfil» (nombre, @usuario y país) no se entra en la web (Mustapha)
        if (not current_user.onboarded_at and not current_user.is_admin and request.endpoint not in ONBOARDING_FREE
                and not mod_campaigns(current_user) and not staff_keys(current_user)):
            if request.method == "GET" and not request.path.startswith("/api/"):
                return redirect(url_for("onboarding"))
    return None


ONBOARDING_FREE = {"onboarding", "logout", "static", "terms", "privacy", "legal_notice", "cookies_page", "health", "avatar",
                   "confirm_pending", "confirm_email", "mail_unsubscribe", "mail_prefs",
                   "browser_error", "notices", "google_callback", "login_google", "book_call", "login", None}


_BOOT = time.time()                                       # cuándo arrancó este proceso (Panel técnico → Salud)
STAT_SKIP = {"static", "health", "notices", "support_poll", "browser_error", "clip_cover", "account_photo", "cover",
             "avatar", "landing_cover"}                   # archivos y consultas automáticas: no cuentan como páginas
_stat, _stat_lock = {"hour": None, "flushed": 0.0, "n": {}}, threading.Lock()


def stat_add(**inc):
    """Suma cifras a la hora actual (en memoria); cada minuto, o al cambiar de hora, se pasan a StatHour."""
    hour = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
    with _stat_lock:
        if _stat["hour"] != hour:
            old, _stat["hour"], _stat["n"] = (_stat["hour"], dict(_stat["n"])), hour, {}
        else:
            old = None
        for k, v in inc.items():
            _stat["n"][k] = _stat["n"].get(k, 0) + v
        due = time.time() - _stat["flushed"] >= 60
    if old and old[0] and old[1]:
        _stat_write(*old)
    if due:
        stat_flush()


def stat_flush():
    with _stat_lock:
        hour, n = _stat["hour"], dict(_stat["n"])
        _stat["n"], _stat["flushed"] = {}, time.time()
    if hour and n:
        _stat_write(hour, n)


def _stat_write(hour, n):
    """Suma en su propia conexión (no toca la de la página); si falla, se pierde ese minuto y ya está."""
    t = StatHour.__table__
    vals = {k: getattr(t.c, k) + v for k, v in n.items() if k in t.c}
    if not vals:
        return
    try:
        with db.engine.begin() as conn:
            if not conn.execute(t.update().where(t.c.hour == hour).values(**vals)).rowcount:
                conn.execute(t.insert().values(hour=hour, **{k: v for k, v in n.items() if k in t.c}))
    except Exception:  # noqa: BLE001 — dos procesos a la vez: el otro creó la fila, se suma encima
        try:
            with db.engine.begin() as conn:
                conn.execute(t.update().where(t.c.hour == hour).values(**vals))
        except Exception:  # noqa: BLE001
            logging.getLogger("salud").debug("No se pudieron guardar las cifras de la hora", exc_info=True)


SLOW_MS = int(os.environ.get("SLOW_MS", "1500"))          # a partir de aquí una página cuenta como lenta
SLOW_SKIP = {"static", "admin_backup", "admin_refresh", "admin_probe", "admin_probe_covers", "clip_cover", "account_photo",
             "api_callback", "google_callback"}             # descargas, lecturas a mano y cosas que dependen de fuera
_slow, _slow_lock = [], threading.Lock()                   # (cuándo, pantalla, ms) de las últimas 24 h, en este proceso


def note_slow(endpoint, ms):
    now = time.time()
    with _slow_lock:
        _slow[:] = [x for x in _slow if now - x[0] < 86400][-400:]
        _slow.append((now, endpoint, int(ms)))


def slow_pages():
    """[(pantalla, veces, peor tiempo en ms)] de las últimas 24 h en este proceso, de la más repetida a la menos."""
    now, agg = time.time(), {}
    with _slow_lock:
        rows = [x for x in _slow if now - x[0] < 86400]
    for _t, ep, ms in rows:
        n, worst = agg.get(ep, (0, 0))
        agg[ep] = (n + 1, max(worst, ms))
    return sorted(((ep, n, w) for ep, (n, w) in agg.items()), key=lambda r: -r[1])


CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "font-src 'self'; img-src 'self' data: https:; connect-src 'self' https://video.bunnycdn.com; "
       "frame-src https://www.tiktok.com https://www.instagram.com https://www.youtube-nocookie.com "
       "https://iframe.mediadelivery.net https://player.vimeo.com https://www.facebook.com; "
       "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    if not getattr(g, "_framable", False):
        resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    resp.headers.setdefault("Content-Security-Policy", CSP)
    # Con Turbo, un formulario que responde con una página (sin redirigir) tiene que llevar un código 4xx para que se pinte
    if (request.method == "POST" and resp.status_code == 200 and request.headers.get("X-Turbo-Request-Id")
            and resp.mimetype == "text/html"):
        resp.status_code = 422
    if getattr(g, "_t0", None):     # cuánto tarda el servidor en preparar la página (sin contar internet)
        ms = (time.perf_counter() - g._t0) * 1000
        resp.headers["Server-Timing"] = f"app;dur={ms:.0f}"
        if ms >= SLOW_MS and request.endpoint not in SLOW_SKIP:
            note_slow(request.endpoint or request.path, ms)
        if request.endpoint not in STAT_SKIP and not request.path.startswith("/static/"):
            stat_add(requests=1, ms_total=ms, slow=int(ms >= SLOW_MS and request.endpoint not in SLOW_SKIP),
                     errors=int(resp.status_code >= 500))
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
    if BOT_PATHS.search(request.path):
        stat_add(bots=1)
    elif e.code >= 400 and request.endpoint != "static":
        note_error(f"{e.code} {e.description or e.name}"[:200], kind="pagina", http_code=e.code)
    title, body = ERROR_TEXT.get(e.code, (e.name, e.description or ""))
    return render_template("error.html", code=e.code, title=title, body=body), e.code


ERROR_CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"     # sin 0/O ni 1/I, para que no se confundan al copiarlo
ERROR_KINDS = {"servidor": "Fallo de la web", "aviso": "Aviso a un usuario", "pagina": "Página no válida",
               "navegador": "Fallo en el navegador", "tarea": "Tarea automática", "google": "Acceso con Google"}
_err_local = threading.local()
BOT_PATHS = re.compile(r"(\.php|wp-|\.env|\.git|/cgi-bin|/admin\.|xmlrpc|/vendor/|\.aspx?|/boaform|/HNAP)", re.I)


def _cause(exc_name, text=""):
    """La causa probable de un fallo, en palabras normales."""
    t = f"{exc_name} {text}"
    for keys, why in (
            (("OperationalError", "DisconnectionError", "could not connect", "server closed the connection"),
             "La base de datos no respondió a tiempo o se cortó la conexión."),
            (("IntegrityError", "UniqueViolation", "duplicate key"), "Se intentó guardar algo repetido o incompleto en la base de datos."),
            (("Timeout", "timed out"), "Un servicio de fuera (Apify, Whop, Google, Bunny…) tardó demasiado en responder."),
            (("ConnectionError", "Max retries", "Name or service not known"), "No se pudo conectar con un servicio de fuera."),
            (("UndefinedError", "TemplateSyntaxError", "TemplateNotFound"), "Fallo al dibujar la pantalla: hay que arreglarlo en el código."),
            (("MemoryError",), "La web se quedó sin memoria."),
            (("KeyError", "AttributeError", "TypeError", "ValueError", "IndexError", "NameError", "ZeroDivisionError",
              "AssertionError"), "Fallo en el código de la web (un dato no estaba como se esperaba): hay que arreglarlo."),
            (("csrf",), "El formulario estaba caducado (página abierta mucho rato o en otra pestaña).")):
        if any(k in t for k in keys):
            return why
    return "Fallo inesperado."


def _who(uid):
    if not uid:
        return "un visitante sin sesión"
    try:
        u = db.session.get(User, uid)
        return f"{u.name} ({u.email})" if u else f"el usuario #{uid}"
    except Exception:
        return f"el usuario #{uid}"


def explain_error(kind, what, path, uid, code=None):
    who, page = _who(uid), path or "—"
    if kind == "servidor":
        return f"A {who} le falló la página {page} y vio la pantalla de error. {_cause(what)}"
    if kind == "aviso":
        return f"A {who} le salió este aviso en {page}: «{what}»."
    if kind == "pagina":
        return {404: f"{who.capitalize()} intentó abrir {page}, que no existe (enlace roto o mal escrito).",
                403: f"{who.capitalize()} intentó abrir {page} sin tener permiso.",
                400: f"La web rechazó lo que envió {who} en {page}. {_cause(what)}",
                405: f"{who.capitalize()} abrió {page} de una forma que esa página no admite.",
                413: f"{who.capitalize()} intentó subir un archivo demasiado grande en {page}.",
                429: f"{who.capitalize()} hizo demasiadas peticiones seguidas en {page} y la web le frenó."}.get(
            code, f"{who.capitalize()} recibió el error {code} en {page}.")
    if kind == "navegador":
        return f"En el navegador de {who}, en {page}, falló algo de la pantalla: {what}"
    if kind == "google":
        return f"{who.capitalize()} no pudo entrar con Google: {what}"
    return f"Tarea automática de la web: {what}. {_cause(what)}"


def note_error(what, detail="", kind="servidor", http_code=None, path=None):
    """Apunta un fallo (en su propia conexión, sin tocar lo que esté haciendo la página) y devuelve su código.
    El mismo fallo en la misma página y del mismo usuario en 10 minutos no se repite: suma una vez más."""
    if getattr(_err_local, "busy", False):
        return ""
    _err_local.busy = True
    code = "".join(secrets.choice(ERROR_CODE_CHARS) for _ in range(6))
    try:
        try:
            uid = current_user.id if has_request_context() and current_user.is_authenticated else None
        except Exception:
            uid = None
        path = (path if path is not None else (request.full_path.rstrip("?") if has_request_context() else ""))[:300]
        what = str(what)[:200]
        now = datetime.utcnow()
        log.warning("Fallo %s · %s · %s · %s", code, kind, what, path, extra={"noted": True})
        explain = explain_error(kind, what, path, uid, http_code)[:2000]
        t = ErrorLog.__table__
        with db.engine.begin() as conn:
            prev = conn.execute(db.select(t.c.id, t.c.code).where(
                t.c.kind == kind, t.c.what == what, t.c.path == path,
                (t.c.user_id == uid) if uid else t.c.user_id.is_(None),
                db.func.coalesce(t.c.last_at, t.c.created_at) >= now - timedelta(minutes=10)).limit(1)).first()
            if prev:
                conn.execute(t.update().where(t.c.id == prev.id).values(times=t.c.times + 1, last_at=now))
                code = prev.code
            else:
                conn.execute(t.insert().values(code=code, kind=kind, what=what, explain=explain, path=path, user_id=uid,
                                               detail=str(detail)[:20000], times=1, created_at=now, last_at=now))
            if secrets.randbelow(50) == 0:                       # de vez en cuando, fuera los de hace más de un mes
                conn.execute(t.delete().where(t.c.created_at < now - timedelta(days=30)))
    except Exception:
        logging.getLogger("errores").warning("No se pudo guardar el fallo %s", code, exc_info=True)
    finally:
        _err_local.busy = False
    return code


def flash(message, category="message"):
    """Igual que el de Flask, pero cada aviso de error que ve un usuario queda apuntado en Panel técnico → Errores."""
    _flask_flash(message, category)
    if category == "error":
        note_error(str(message), kind="aviso")


class _ErrorsToPanel(logging.Handler):
    """Todo lo que la web apunta como error (también las tareas automáticas: lectura, Apify, Whop, Bunny…) va al panel."""
    def emit(self, record):
        if getattr(record, "noted", False) or getattr(_err_local, "busy", False) or record.name in ("errores",):
            return
        try:
            import traceback
            detail = "".join(traceback.format_exception(*record.exc_info)) if record.exc_info else ""
            what = record.getMessage()
            if record.exc_info and record.exc_info[1] is not None:
                what = f"{what} ({type(record.exc_info[1]).__name__}: {record.exc_info[1]})"
            kind = "servidor" if has_request_context() else "tarea"
            if has_app_context_():
                note_error(what, detail, kind=kind)
            else:
                with app.app_context():
                    note_error(what, detail, kind=kind)
        except Exception:
            pass


def has_app_context_():
    from flask import has_app_context
    return has_app_context()


if not os.environ.get("PYTEST_CURRENT_TEST") and "pytest" not in sys.modules:
    _h = _ErrorsToPanel(level=logging.ERROR)
    logging.getLogger().addHandler(_h)


@app.errorhandler(Exception)
def unhandled_error(e):
    import traceback
    db.session.rollback()
    log.error("Error no controlado: %s", e, exc_info=True, extra={"noted": True})
    code = note_error(f"{type(e).__name__}: {e}"[:200], traceback.format_exc())
    title, body = ERROR_TEXT[500]
    return render_template("error.html", code=500, title=title, body=body, error_code=code), 500


# Qué puede abrir cada rol. El dueño, todo. Lo que no está en la lista es solo para el dueño (dinero, campañas, ajustes…).
ROLE_ENDPOINTS = {
    "reviewer": {"admin_home", "admin_clips", "admin_requests", "admin_review", "admin_review_bulk", "admin_campaign_member",
                 "admin_campaign_review", "admin_campaign_requests", "admin_campaign_clippers",
                 "admin_campaign_account", "admin_campaign", "admin_campaigns", "admin_clipper", "admin_find"},
    "support": {"admin_home", "admin_inbox", "admin_chat", "admin_chat_poll", "admin_clipper", "admin_find"},
}


TECH_ENDPOINTS = {"admin_settings", "admin_backups_ok", "admin_backup_now", "admin_delete_test_users", "admin_errors", "admin_notes", "admin_probe", "admin_refresh", "admin_whop_app_test", "admin_vapid_keys", "admin_backup",
                  "admin_demo", "admin_my_demo", "admin_wipe", "admin_activity", "admin_activity_delete", "admin_creators", "admin_creator_form"}


def is_tech(user=None):
    user = user or current_user
    if not getattr(user, "is_admin", False) or (user.admin_role or "owner") != "owner":
        return False
    return (user.email or "").lower() in TECH_EMAILS if TECH_EMAILS else True


def admin_can(endpoint, user=None):
    user = user or current_user
    if not getattr(user, "is_admin", False):
        return endpoint not in TECH_ENDPOINTS and endpoint in staff_endpoints(user)     # mod con funciones generales
    if endpoint in ("admin_creators", "admin_creator_form") and not creators_on():
        return False
    if endpoint in TECH_ENDPOINTS:
        return is_tech(user)
    role = user.admin_role or "owner"
    return role == "owner" or endpoint in ROLE_ENDPOINTS.get(role, set())


def admin_required(f):
    @wraps(f)
    @login_required
    def wrapper(*a, **kw):
        if not admin_can(request.endpoint):          # admin según su rol, o mod con esa función general
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
            if admin_can(request.endpoint):          # mod con funciones generales (p. ej. crear campañas, sin campaña aún)
                return f(*a, **kw)
            abort(403)
        return wrapper
    return deco


def staff_can(endpoint, cid):
    """Para las plantillas: ¿puede el usuario entrar en esta pantalla de la campaña?"""
    if getattr(current_user, "is_admin", False):
        return admin_can(endpoint)
    return ((endpoint in MOD_ENDPOINTS and mod_can(current_user, cid, ENDPOINT_PERM.get(endpoint, "")))
            or admin_can(endpoint))                       # mod con funciones generales


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
    img = Image.open(io.BytesIO(data), formats=IMG_FORMATS).convert("RGB")
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
    return {"csrf_token": csrf_token, "DARK_MODE": DARK_MODE, "REQUIRE_CREATOR": REQUIRE_CREATOR, "admin_can": admin_can, "legal_info": legal_info, "LEGAL_FIELDS": LEGAL_FIELDS, "site_verify": site_verify, "camp_admin_url": camp_admin_url, "is_tech": is_tech, "CLIPPER_WITHDRAW": CLIPPER_WITHDRAW, "storage_ready": storage_ready, "BACKUP_AT": BACKUP_AT, "staff_keys": staff_keys, "staff_home_url": staff_home_url, "bell_data": bell_data, "wallet_ready": wallet_ready, "ADMIN_ROLES": ADMIN_ROLES, "static_url": static_url, "APP_NAME": APP_NAME, "CUR": CURRENCY, "CUR_CODE": CURRENCY_CODE,
            "PLATFORMS": PLATFORMS, "cover_src": cover_src, "chan_icon": chan_icon, "cover_meta": cover_meta, "min_pay": min_pay, "MIN_WITHDRAW": MIN_WITHDRAW,
            "PAYOUT_METHODS": PAYOUT_METHODS, "PAYOUT_AVAILABLE": PAYOUT_AVAILABLE, "USDT_METHOD": USDT_METHOD, "WHOP_METHOD": WHOP_METHOD, "WHOP_CLIENT_ID": WHOP_CLIENT_ID, "WHOP_AUTO_MAX": WHOP_AUTO_MAX, "whop_enabled": whop_enabled, "account_verified": account_verified, "profile_url": profile_url, "CATEGORIES": CATEGORIES, "CATEGORY_ONE": CATEGORY_ONE,
            "LOGO_MAIN": LOGO_MAIN, "LOGO_SUB": LOGO_SUB, "SUPPORT_EMAIL": SUPPORT_EMAIL,
            "FLAG_LABELS": FLAG_LABELS, "PAYOUT_DAYS": PAYOUT_DAYS, "YEAR": datetime.utcnow().year, "now_utc": datetime.utcnow(),
            "REFRESH_DAYS": REFRESH_DAYS, "REFRESH_HOURS": REFRESH_HOURS, "REFRESH_AT": REFRESH_AT, "next_scan_utc": next_scan_utc, "admin_counts": admin_counts, "unread_messages": unread_messages, "notice_data": notice_data, "new_leads": new_leads,
            "REJECT_REASONS": REJECT_REASONS, "COUNTRIES": COUNTRIES, "LANGUAGES": LANGUAGES, "CAMPAIGN_KINDS": CAMPAIGN_KINDS,
            "clip_stage": clip_stage, "STAGE_LABELS": STAGE_LABELS, "stats_embed": stats_embed, "stats_due": stats_due, "staff_can": staff_can, "perm_ok": perm_ok, "clip_deletable": clip_deletable, "clip_thumb": clip_thumb, "pop_undo": pop_undo, "api_ready": api_ready, "api_mode": api_mode, "OFFICIAL": official, "BASE_URL": BASE_URL, "my_available": my_available, "when_madrid": when_madrid, "RESOURCE_ICONS": RESOURCE_ICONS, "is_mod": is_mod, "mod_can": mod_can, "mod_home": mod_home,
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


@app.template_filter("pair_platform")
def pair_platform(p):
    return (p, PLATFORMS.get(p, p))


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
    camps = Campaign.query.filter_by(status="activa").order_by(Campaign.created_at.desc()).all()
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
    if sub and not user.email_verified_at:
        user.email_verified_at = datetime.utcnow()           # Google ya comprobó que el correo es suyo
    if not user.avatar_ver:                  # si subió su propia foto, no la cambiamos por la de Google
        user.avatar_url = (avatar or user.avatar_url or "")[:500]
    if not user.accepted_terms_at:
        user.accepted_terms_at = datetime.utcnow()
    if role_for(email) and password_user is None:   # el admin solo se activa entrando con Google, nunca con contraseña
        user.is_admin, user.admin_role = True, role_for(email)
    db.session.commit()
    give_my_demo(user)
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
    return render_template("login.html", google_ready=bool(GOOGLE_CLIENT_ID), dev_login=DEV_LOGIN, mode=mode, mail_ready=mail_ready(),
                           password_login=PASSWORD_LOGIN,
                           form_email=session.pop("form_email", ""), form_name=session.pop("form_name", ""))


EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
SIGNUP_HOUR_MAX = int(os.environ.get("SIGNUP_HOUR_MAX", "30"))   # altas con correo sin confirmar en 1 h, entre todos
RECOVER_HOUR_MAX = 3                                             # correos de «he olvidado mi contraseña» por cuenta y hora
_login_fails = {}


def too_many_fails(key):
    now = time.time()
    tries = [t for t in _login_fails.get(key, []) if now - t < 900]
    _login_fails[key] = tries
    if len(tries) >= 10:
        stat_add(blocked=1)
        return True
    return False


# ---------------------------------------------------------------- correo y «he olvidado mi contraseña»
# Se enciende solo al poner en Railway SMTP_HOST, SMTP_USER, SMTP_PASS y MAIL_FROM (cualquier servicio de correo vale).
# Sin esas variables no se enseña el enlace ni existe la página: nada cambia.
SMTP_HOST = os.environ.get("SMTP_HOST", "").strip()
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587") or 587)
SMTP_USER = os.environ.get("SMTP_USER", "").strip()
SMTP_PASS = os.environ.get("SMTP_PASS", "")
MAIL_FROM = os.environ.get("MAIL_FROM", "").strip()
# Railway (plan Hobby) bloquea el correo normal (SMTP): por eso se usa Resend, que envía por internet normal (HTTPS).
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "").strip()
RESET_HOURS = 1


def mail_ready():
    return bool((RESEND_API_KEY or SMTP_HOST) and MAIL_FROM)


# Correos (24 nov, Mustapha): Resend, plan gratis = 100 al día y 3.000 al mes. Los avisos dejan sitio a los
# imprescindibles (confirmar el correo, recuperar la contraseña): MAIL_RESERVE al día quedan solo para ellos.
MAIL_DAY_MAX = int(os.environ.get("MAIL_DAY_MAX", "100"))
MAIL_MONTH_MAX = int(os.environ.get("MAIL_MONTH_MAX", "3000"))
MAIL_RESERVE = int(os.environ.get("MAIL_RESERVE", "15"))
MAIL_ESSENTIAL = {"confirmar", "recuperar"}


def mail_counts(now=None):
    """Correos enviados (o enviándose) hoy y este mes, en hora UTC como los cuenta Resend."""
    now = now or datetime.utcnow()
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month0 = day0.replace(day=1)
    q = EmailLog.query.filter(EmailLog.status.in_(("ok", "enviando")))
    return q.filter(EmailLog.created_at >= day0).count(), q.filter(EmailLog.created_at >= month0).count()


def mail_room(kind):
    """¿Cabe otro correo de este tipo hoy y este mes?"""
    day, month = mail_counts()
    if kind in MAIL_ESSENTIAL:
        return day < MAIL_DAY_MAX and month < MAIL_MONTH_MAX
    return day < MAIL_DAY_MAX - MAIL_RESERVE and month < MAIL_MONTH_MAX - MAIL_RESERVE * 4


def mail_html(title, paragraphs, button=None, unsub=""):
    """Correo con el aspecto de la web (logo, botón negro, letra limpia). Estilos en línea: los programas de correo no leen CSS."""
    from markupsafe import escape
    ps = "".join(f'<p style="margin:0 0 14px;font-size:15px;line-height:1.55;color:#3b3b40">{escape(p_)}</p>' for p_ in paragraphs)
    btn = (f'<p style="margin:22px 0 6px"><a href="{escape(button[1])}" style="display:inline-block;background:#0e0e0e;color:#fff;'
           f'text-decoration:none;font-weight:600;font-size:15px;padding:12px 22px;border-radius:999px">{escape(button[0])}</a></p>') if button else ""
    foot = (f'<p style="margin:18px 0 0;font-size:12px;color:#686971">¿No quieres estos avisos? '
            f'<a href="{escape(unsub)}" style="color:#686971">Dejar de recibirlos por correo</a>.</p>') if unsub else ""
    return (f'<!doctype html><html lang="es"><body style="margin:0;background:#f6f7f9;font-family:Inter,-apple-system,Segoe UI,Helvetica,Arial,sans-serif">'
            f'<div style="max-width:520px;margin:0 auto;padding:28px 16px">'
            f'<p style="margin:0 0 18px;font-size:17px;font-weight:700;color:#0e0e0e">{escape(APP_NAME)}</p>'
            f'<div style="background:#fff;border:1px solid #e9eaee;border-radius:20px;padding:26px 24px">'
            f'<h1 style="margin:0 0 14px;font-size:22px;line-height:1.25;color:#0e0e0e">{escape(title)}</h1>{ps}{btn}</div>'
            f'<p style="margin:16px 4px 0;font-size:12px;color:#686971">{escape(APP_NAME)} · app.clipprofit.es</p>{foot}</div></body></html>')


def send_mail(to, subject, body, html=None, kind="", user_id=None, unsub=""):
    """Envía un correo sin hacer esperar a la página (en un hilo aparte) y lo apunta en EmailLog.
    Si no cabe en el tope del día o del mes, no se envía y queda apuntado como «tope». Devuelve True si se mandó."""
    if not mail_ready() or not to:
        return False
    t = EmailLog.__table__
    if not mail_room(kind):
        with db.engine.begin() as conn:
            conn.execute(t.insert().values(user_id=user_id, to=to[:255], kind=kind[:30], subject=subject[:200], status="tope",
                                           created_at=datetime.utcnow()))
        return False
    with db.engine.begin() as conn:
        log_id = conn.execute(t.insert().values(user_id=user_id, to=to[:255], kind=kind[:30], subject=subject[:200],
                                                status="enviando", created_at=datetime.utcnow())).inserted_primary_key[0]
        if secrets.randbelow(40) == 0:                       # de vez en cuando, fuera los de hace más de 60 días
            conn.execute(t.delete().where(t.c.created_at < datetime.utcnow() - timedelta(days=60)))

    def done(status, error=""):
        try:
            with app.app_context(), db.engine.begin() as conn:
                conn.execute(t.update().where(t.c.id == log_id).values(status=status, error=str(error)[:300]))
        except Exception:  # noqa: BLE001
            log.warning("No se pudo apuntar el correo %s", log_id, exc_info=True)

    def run():
        import smtplib
        from email.message import EmailMessage
        if RESEND_API_KEY:
            payload = {"from": f"{APP_NAME} <{MAIL_FROM}>", "to": [to], "subject": subject, "text": body}
            if html:
                payload["html"] = html
            if unsub:
                payload["headers"] = {"List-Unsubscribe": f"<{unsub}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}
            try:
                r = requests.post("https://api.resend.com/emails", timeout=15,
                                  headers={"Authorization": f"Bearer {RESEND_API_KEY}"}, json=payload)
                if r.status_code >= 300:
                    log.error("Resend no envió el correo (%s): %s", r.status_code, r.text[:300])
                    done("error", f"Resend {r.status_code}: {r.text[:250]}")
                else:
                    done("ok")
            except requests.RequestException as e:
                log.exception("No se pudo enviar el correo por Resend")
                done("error", e)
            return
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = f"{APP_NAME} <{MAIL_FROM}>", to, subject
        msg.set_content(body)
        if html:
            msg.add_alternative(html, subtype="html")
        try:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as smtp:
                smtp.starttls()
                if SMTP_USER:
                    smtp.login(SMTP_USER, SMTP_PASS)
                smtp.send_message(msg)
            done("ok")
        except Exception as e:  # noqa: BLE001
            log.exception("No se pudo enviar el correo")
            done("error", e)
    if os.environ.get("PYTEST_CURRENT_TEST"):
        run()                                                # en las pruebas, al momento
    else:
        threading.Thread(target=run, daemon=True).start()
    return True


def _mail_serializer(salt):
    from itsdangerous import URLSafeTimedSerializer
    return URLSafeTimedSerializer(app.secret_key, salt=salt)


def public_url(path):
    """Dirección completa para un correo: BASE_URL, la de la visita o, si no hay ninguna (tarea de la noche), la de siempre."""
    return (BASE_URL or (request.host_url.rstrip("/") if has_request_context() else "") or "https://app.clipprofit.es") + path


def unsub_url(user):
    return public_url("/baja/" + _mail_serializer("baja-correos").dumps(user.id))     # sin url_for: también desde las tareas de la noche


def mail_ok_for(user):
    """¿Se le pueden mandar avisos por correo? Correo confirmado (o de Google), no lo ha apagado y no está bloqueado."""
    return bool(user and user.email and not user.email_off and not user.is_banned and not is_demo(user)
                and (user.email_verified_at or user.google_sub or not user.password_hash))


def send_confirm_mail(user):
    link = public_url("/confirmar/" + _mail_serializer("confirmar-correo").dumps(user.id))
    first = (user.name or "").split()[0] if user.name else ""
    lines = [f"Hola{(', ' + first) if first else ''}:", "Pulsa el botón para confirmar que este correo es tuyo y empezar a usar "
             f"{APP_NAME}. El enlace vale 2 días.", "Si no te has registrado tú, no hagas nada."]
    return send_mail(user.email, f"Confirma tu correo · {APP_NAME}", "\n\n".join(lines) + f"\n\n{link}",
                     html=mail_html("Confirma tu correo", lines, ("Confirmar mi correo", link)), kind="confirmar", user_id=user.id)


def _reset_serializer():
    from itsdangerous import URLSafeTimedSerializer
    return URLSafeTimedSerializer(app.secret_key, salt="recuperar-clave")


def reset_token(user):
    """Enlace de un solo uso: lleva un trozo de la contraseña cifrada actual, así deja de valer en cuanto se cambia."""
    return _reset_serializer().dumps({"u": user.id, "h": (user.password_hash or "")[-16:]})


def reset_user(token):
    from itsdangerous import BadData
    try:
        data = _reset_serializer().loads(token, max_age=RESET_HOURS * 3600)
    except BadData:
        return None
    user = db.session.get(User, data.get("u") or 0)
    if not user or not user.password_hash or user.is_banned or (user.password_hash or "")[-16:] != data.get("h"):
        return None
    return user


@app.route("/recuperar", methods=["GET", "POST"])
def recover():
    if not mail_ready():
        abort(404)
    if request.method == "POST":
        ip = "r:" + (request.remote_addr or "")
        if too_many_fails(ip):
            flash("Demasiados intentos. Espera unos minutos y vuelve a probar.", "error")
            return redirect(url_for("recover"))
        _login_fails.setdefault(ip, []).append(time.time())
        email = (request.form.get("email") or "").strip().lower()
        user = User.query.filter_by(email=email).first() if email else None
        if user and EmailLog.query.filter(EmailLog.user_id == user.id, EmailLog.kind == "recuperar",
                                          EmailLog.created_at >= datetime.utcnow() - timedelta(hours=1)).count() >= RECOVER_HOUR_MAX:
            user = None                                  # ya se le mandaron varios: no se llena su correo ni se gasta el cupo
        if user and user.password_hash and not user.is_banned:
            link = (BASE_URL or request.host_url.rstrip("/")) + url_for("recover_set", token=reset_token(user))
            lines = [f"Hola, {user.name}:", f"Para poner una contraseña nueva, pulsa el botón. El enlace vale {RESET_HOURS} hora.",
                     "Si no lo has pedido tú, no hagas nada: tu contraseña sigue siendo la misma."]
            send_mail(user.email, f"Cambia tu contraseña de {APP_NAME}", "\n\n".join(lines) + f"\n\n{link}",
                      html=mail_html("Cambia tu contraseña", lines, ("Poner contraseña nueva", link)), kind="recuperar", user_id=user.id)
        # Siempre el mismo mensaje: no se dice si el correo existe o no
        flash("Si ese correo tiene una cuenta con contraseña, te hemos enviado un enlace para cambiarla. Mira también en spam.", "ok")
        return redirect(url_for("login"))
    return render_template("recover.html", step="pedir")


@app.route("/recuperar/<token>", methods=["GET", "POST"])
def recover_set(token):
    if not mail_ready():
        abort(404)
    user = reset_user(token)
    if not user:
        flash("El enlace ha caducado o ya se usó. Pide otro.", "error")
        return redirect(url_for("recover"))
    if request.method == "POST":
        password = request.form.get("password") or ""
        if len(password) < 8:
            flash("La contraseña tiene que tener al menos 8 caracteres.", "error")
            return redirect(url_for("recover_set", token=token))
        user.password_hash = generate_password_hash(password)
        audit("clave_cambiada", user.email)
        db.session.commit()
        flash("Contraseña cambiada. Ya puedes entrar.", "ok")
        return redirect(url_for("login"))
    return render_template("recover.html", step="nueva", token=token)


# Solo Google (Mustapha, 23 nov): correo y contraseña apagados en la web real; PASSWORD_LOGIN=1 en Railway los vuelve a poner.
PASSWORD_LOGIN = os.environ.get("PASSWORD_LOGIN", "0" if PRODUCTION else "1") == "1"


@app.route("/login/correo", methods=["POST"])
def login_email():
    if not PASSWORD_LOGIN:                  # el formulario se sigue viendo (por el diseño), pero se entra con Google
        _flask_flash("Por ahora solo se puede entrar con Google. Pulsa «Continuar con Google».", "error")
        return redirect(url_for("login"))
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    ip = request.remote_addr or ""
    if too_many_fails(ip) or too_many_fails("em:" + email):    # por IP y también por cuenta (ataques desde muchas IP)
        flash("Demasiados intentos. Espera unos minutos y vuelve a probar.", "error")
        return redirect(url_for("login"))
    user = User.query.filter_by(email=email).first()
    if not user or not user.password_hash or not check_password_hash(user.password_hash, password):
        _login_fails.setdefault(ip, []).append(time.time())
        _login_fails.setdefault("em:" + email, []).append(time.time())
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
    if not PASSWORD_LOGIN:                  # el formulario se sigue viendo (por el diseño), pero se entra con Google
        _flask_flash("Por ahora solo se puede entrar con Google. Pulsa «Continuar con Google».", "error")
        return redirect(url_for("login", modo="registro"))
    name = (request.form.get("name") or "").strip()[:120]
    email = (request.form.get("email") or "").strip().lower()[:255]
    password = request.form.get("password") or ""
    back = redirect(url_for("login", modo="registro"))
    session["form_email"], session["form_name"] = email, name
    if too_many_fails("reg:" + (request.remote_addr or "")):
        flash("Has creado varias cuentas seguidas. Inténtalo más tarde.", "error")
        return back
    if not EMAIL_RE.match(email):                       # el nombre ya no se pide aquí: va en «Crea tu perfil» (Mustapha, 24 nov)
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
    if mail_ready() and SIGNUP_HOUR_MAX and User.query.filter(               # auditoría 24 nov: altas en masa (robots) gastarían
            User.password_hash != "", User.email_verified_at.is_(None),       # los 100 correos del día de Resend y nadie más
            User.created_at >= datetime.utcnow() - timedelta(hours=1)).count() >= SIGNUP_HOUR_MAX:   # podría confirmar
        flash("Ahora mismo hay muchas altas. Inténtalo en unos minutos o entra con Google.", "error")
        return back
    user = User(email=email, name=name, password_hash=generate_password_hash(password), accepted_terms_at=datetime.utcnow())
    db.session.add(user)
    db.session.commit()
    _login_fails.setdefault("reg:" + (request.remote_addr or ""), []).append(time.time())
    session.pop("form_email", None)
    session.pop("form_name", None)
    log.info("Nuevo usuario con correo: %s", email)
    sign_in(email, name, password_user=user)
    if mail_ready():                                   # hay que confirmar el correo antes de usar la web
        send_confirm_mail(user)
        return redirect(url_for("confirm_pending"))
    flash("¡Bienvenido! Completa tu perfil para empezar.", "ok")
    return redirect(url_for("onboarding"))


CONFIRM_FREE = {"confirm_pending", "confirm_email", "mail_unsubscribe", "logout", "static", "terms", "privacy", "legal_notice",
                "cookies_page", "health", "browser_error", "notices", None}


def needs_confirm(user):
    """Cuenta con contraseña y correo sin confirmar (solo si el correo está conectado: si no, no se podría confirmar)."""
    return bool(user.password_hash and not user.email_verified_at and not user.google_sub and not user.is_admin and mail_ready())


@app.route("/confirmar-correo", methods=["GET", "POST"])
@login_required
def confirm_pending():
    """Pantalla de espera: «te hemos enviado un enlace». Botón para reenviarlo (como mucho 3 por hora)."""
    if not needs_confirm(current_user):
        return redirect(url_for("index"))
    if request.method == "POST":
        key = f"conf:{current_user.id}"
        tries = [t_ for t_ in _login_fails.get(key, []) if time.time() - t_ < 3600]
        if len(tries) >= 3:
            flash("Ya te hemos enviado varios enlaces. Espera un rato y mira también en spam.", "error")
        else:
            _login_fails[key] = tries + [time.time()]
            send_confirm_mail(current_user)
            flash("Te hemos enviado otro enlace. Mira también en spam.", "ok")
        return redirect(url_for("confirm_pending"))
    return render_template("confirm.html", step="pending")


@app.route("/confirmar/<token>")
def confirm_email(token):
    from itsdangerous import BadData
    try:
        uid = _mail_serializer("confirmar-correo").loads(token, max_age=2 * 86400)
    except BadData:
        uid = None
    user = db.session.get(User, uid) if uid else None
    if not user:
        flash("El enlace ha caducado o no es válido. Entra y pide otro.", "error")
        return redirect(url_for("login"))
    first = not user.email_verified_at
    if first:
        user.email_verified_at = datetime.utcnow()
        audit("correo_confirmado", user.email)
        db.session.commit()
    if first and not current_user.is_authenticated and not user.is_banned:
        # Abierto en otro navegador (el del móvil, el del correo): el enlace demuestra que el correo es suyo, así que
        # entra ya y va a «Crea tu perfil» (Mustapha, 24 nov). Solo la primera vez: un enlace ya usado no abre sesión.
        sign_in(user.email, user.name, password_user=user)
        flash("Correo confirmado. ¡Ya puedes empezar!", "ok")
        return redirect(url_for("onboarding") if not user.onboarded_at else url_for("index"))
    if current_user.is_authenticated and current_user.id == user.id:
        flash("Correo confirmado. ¡Ya puedes empezar!", "ok")
        return redirect(url_for("onboarding") if not user.onboarded_at else url_for("index"))
    flash("Correo confirmado. Entra con tu correo y contraseña.", "ok")
    return redirect(url_for("login"))


@app.route("/baja/<token>", methods=["GET", "POST"])
def mail_unsubscribe(token):
    """Enlace de cada aviso por correo: deja de recibirlos con un clic (sin entrar). Los correos de su cuenta siguen."""
    from itsdangerous import BadData
    try:
        uid = _mail_serializer("baja-correos").loads(token)
    except BadData:
        abort(404)
    user = db.session.get(User, uid) or abort(404)
    if not user.email_off:
        user.email_off = True
        audit("baja_correos", user.email)
        db.session.commit()
    if request.method == "POST":                     # «un clic» de Gmail y Outlook (List-Unsubscribe-Post)
        return ("", 204)
    return render_template("confirm.html", step="baja")


@app.route("/cuenta/correos", methods=["POST"])
@login_required
def mail_prefs():
    """Ajustes → «Avisos por correo»: encender o apagar."""
    current_user.email_off = request.form.get("on") != "1"
    db.session.commit()
    if request.headers.get("X-Requested-With") == "fetch":
        return ("", 204)
    return redirect(url_for("account", _anchor="cuenta"))


@app.route("/login/google")
def login_google():
    if not GOOGLE_CLIENT_ID:
        flash("El acceso con Google aún no está configurado.", "error")
        return redirect(url_for("login"))
    back = urlparse(redirect_uri())
    if back.netloc and request.host.lower() != back.netloc.lower():   # empezar en la misma dirección a la que vuelve Google
        return redirect(f"{back.scheme}://{back.netloc}{request.full_path.rstrip('?')}")
    # El «state» va firmado por la web y caduca en 15 min: así no depende de la cookie (móvil que abre Google en otra
    # ventana, app instalada, doble clic, dos pestañas…), que era lo que hacía fallar el acceso «a veces».
    state = _google_state().dumps({"n": secrets.token_urlsafe(8), "next": request.args.get("next", "")[:300]})
    params = {"client_id": GOOGLE_CLIENT_ID, "redirect_uri": redirect_uri(), "response_type": "code",
              "scope": "openid email profile", "state": state, "prompt": "select_account"}
    return redirect("https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params))


def _google_state():
    from itsdangerous import URLSafeTimedSerializer
    return URLSafeTimedSerializer(app.secret_key, salt="google-state")


def _gcode_key(code):
    return "gcode:" + hashlib.sha256(code.encode()).hexdigest()[:24]


@app.route("/auth/google/callback")
def google_callback():
    if request.args.get("error"):
        flash("Has cancelado el inicio de sesión con Google.", "error")
        return redirect(url_for("login"))
    from itsdangerous import BadSignature, SignatureExpired
    try:
        st = _google_state().loads(request.args.get("state", ""), max_age=900)
    except SignatureExpired:
        st = None
        why = "tardó más de 15 minutos en elegir la cuenta"
    except BadSignature:
        st = None
        why = "la vuelta de Google no traía una firma válida"
    gcode = request.args.get("code", "")
    if st is not None and gcode:
        done = get_setting(_gcode_key(gcode), "")              # el navegador repite la vuelta: el código ya se usó bien
        if done:
            u = db.session.get(User, int(done.split("|")[0]))
            if u:
                login_user(u, remember=True)
                return redirect(safe_next(st.get("next", "")))
    if st is None or not gcode:
        if current_user.is_authenticated:                      # ya ha entrado: no molestar con un error
            return redirect(url_for("index"))
        code = note_error(f"no se pudo comprobar la vuelta de Google: {why if st is None else 'sin código'}", kind="google",
                          detail=f"host={request.host} base={BASE_URL}")
        _flask_flash(f"No se pudo iniciar sesión. Inténtalo otra vez. (Código {code})", "error")
        return redirect(url_for("login"))
    try:
        token = requests.post("https://oauth2.googleapis.com/token", timeout=15, data={
            "code": request.args["code"], "client_id": GOOGLE_CLIENT_ID, "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": redirect_uri(), "grant_type": "authorization_code"}).json()
        info = requests.get("https://openidconnect.googleapis.com/v1/userinfo", timeout=15,
                            headers={"Authorization": f"Bearer {token.get('access_token', '')}"}).json()
    except (requests.RequestException, ValueError):
        import traceback
        log.exception("Fallo en el login con Google")
        code = note_error("Google no respondió", traceback.format_exc(), kind="google")
        _flask_flash(f"Google no respondió. Inténtalo otra vez. (Código {code})", "error")
        return redirect(url_for("login"))
    if not info.get("email") and (token or {}).get("error") == "invalid_grant":
        for _ in range(8):                                    # la otra vuelta repetida aún está entrando: esperarla
            time.sleep(0.5)
            db.session.expire_all()
            done = get_setting(_gcode_key(gcode), "")
            if done and (u := db.session.get(User, int(done.split("|")[0]))):
                login_user(u, remember=True)
                return redirect(safe_next(st.get("next", "")))
    if not info.get("email") or not info.get("email_verified"):
        bad = {k: v for k, v in (token or {}).items() if k in ("error", "error_description")}
        code = note_error("Google no dio un correo verificado o rechazó el permiso", kind="google", detail=f"token={bad} info={ {k: info.get(k) for k in ('error', 'email_verified')} } redirect_uri={redirect_uri()}")
        _flask_flash(f"No se pudo iniciar sesión con Google. (Código {code})", "error")
        return redirect(url_for("login"))
    u = sign_in(info["email"], info.get("name", ""), info.get("picture", ""), info.get("sub"))
    try:                                                      # por si el navegador repite esta misma vuelta (2 min)
        set_setting(_gcode_key(gcode), f"{u.id}|{int(time.time())}")
        old = [k for (k,) in db.session.query(Setting.key).filter(Setting.key.like("gcode:%")).limit(500).all()]
        if len(old) > 200:
            Setting.query.filter(Setting.key.in_(old[:-50])).delete(synchronize_session=False)
            db.session.commit()
    except Exception:
        db.session.rollback()
    if u and not u.is_admin and not u.onboarded_at and not u.submissions:
        return redirect(url_for("onboarding"))
    return redirect(safe_next(st.get("next", "")))


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


LEGAL_FIELDS = {"owner": "Titular (nombre y apellidos o empresa)", "nif": "NIF / CIF", "address": "Domicilio",
                "email": "Correo de contacto legal", "registry": "Datos registrales (solo si es empresa)"}


def legal_info():
    """Datos del titular de la web para el aviso legal (los pone Mustapha en el Panel técnico)."""
    def read():
        try:
            d = json.loads(get_setting("legal_info", "") or "{}")
        except ValueError:
            d = {}
        d = {k: (d.get(k) or "").strip() for k in LEGAL_FIELDS}
        d["email"] = d["email"] or SUPPORT_EMAIL
        return d
    return ttl_cache("legal_info", 60, read)


@app.route("/aviso-legal")
def legal_notice():
    return render_template("legal_notice.html", li=legal_info())


@app.route("/cookies")
def cookies_page():
    return render_template("legal_cookies.html")


@app.route("/admin/ajustes/legal", methods=["POST"])
@admin_required
def admin_legal():
    if not is_tech():
        abort(403)
    data = {k: " ".join((request.form.get(k) or "").split())[:300] for k in LEGAL_FIELDS}
    set_setting("legal_info", json.dumps(data))
    _ttl.pop("legal_info", None)
    audit("datos_legales", ", ".join(k for k, v in data.items() if v) or "vacío")
    db.session.commit()
    flash("Datos del aviso legal guardados.", "ok")
    return redirect(url_for("admin_settings", _anchor="datos"))


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
                                    creators=len({s.user_id for s in approved}), an=campaign_analytics(camp),
                                    dist=camp.status == DIST_STATUS, aud=audience_for(approved)))
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
    img = Image.open(io.BytesIO(data), formats=IMG_FORMATS).convert("RGB")
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


AUTO_FEATURE_DAYS = 10


def auto_featured(cards, now=None):
    """Destacado automático: todas las campañas abiertas con plazas, en carrusel (lo pidió Mustapha: da igual su antigüedad).
    Primero las nuevas y las que acaban de abrir plazas (últimos días, con su etiqueta) y luego el resto, de más nueva a más antigua.
    [(campaña, stats, motivo)]. Si no hay ninguna con plazas, la más nueva."""
    now = now or datetime.utcnow()
    since = now - timedelta(days=AUTO_FEATURE_DAYS)
    out = []
    for c, st in cards:
        if c.full or st.get("exhausted"):
            continue
        opened = c.spots_opened_at if c.spots_opened_at and c.spots_opened_at >= since else None
        new = c.created_at and c.created_at >= since
        why = ""
        if opened or new:
            why = "plazas" if opened and (not new or opened > c.created_at + timedelta(hours=1)) else "nueva"
        fresh = max((d for d in (opened, c.created_at if new else None) if d), default=None)
        out.append((c, st, why, bool(why), fresh or c.created_at or datetime.min))
    out.sort(key=lambda x: (x[3], x[4]), reverse=True)
    if not out and cards:
        c, st = cards[0]
        return [(c, st, "")]
    return [(c, st, why) for c, st, why, _f, _d in out]


@app.route("/campanas")
@login_required
def campaigns_page():
    all_cards = [(c, campaign_stats(c)) for c in active_campaigns()]
    hf = home_feature()
    slides = []
    if hf["mode"] == "campaign":
        featured = next((x for x in all_cards if x[0].id == hf["campaign_id"]), None)
        slides = [(featured[0], featured[1], "")] if featured else []
    elif hf["mode"] == "none":
        featured = None
    else:                                          # automático (también si quedó guardado el antiguo «anuncio») ("auto" o sin elegir): carrusel de nuevas y con plazas recién abiertas
        slides = auto_featured(all_cards)
        featured = (slides[0][0], slides[0][1]) if slides else None
    cat = request.args.get("cat", "")
    cards = [x for x in all_cards if not cat or x[0].category == cat]
    counts = {k: sum(1 for c, _ in all_cards if c.category == k) for k in CATEGORIES}
    my_join = {m.campaign_id: m.status for m in CampaignMember.query.filter_by(user_id=current_user.id).all()}
    cards.sort(key=lambda x: bool(x[0].full and x[0].id not in my_join))   # plazas completas (si no estás dentro) al final
    apply_schedule()
    return render_template("campaigns.html", cards=cards, featured=featured, slides=slides, cat=cat, counts=counts,
                           total=len(all_cards), new_user=not current_user.submissions, my_join=my_join)


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
    # clasificación global: tu puesto (siempre sale) y los primeros
    rank = my_rank(u)
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
            "steps_done": sum(1 for st in steps if st["done"]), "wa_general": wa, "week_clips": clips_of_week(8),
            "rank": rank, "rank_top": rank_top(6), "masked": mask_name}


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
    try:
        db.session.commit()
    except IntegrityError:                         # doble clic o dos pestañas a la vez: el segundo ya no entra (antes, error 500)
        db.session.rollback()
        return False, "Ese vídeo ya se envió antes."
    if any(camp.min_for(p) for p in camp.platform_list):
        return True, "¡Clip enviado! Ya está contando vistas. Lo revisaremos cuando llegue al pago mínimo."
    return True, "¡Clip enviado! Lo revisaremos y empezará a sumar vistas."


@app.route("/campana/<int:cid>", methods=["GET", "POST"])
@login_required
def campaign(cid):
    camp = db.session.get(Campaign, cid) or abort(404)
    if camp.status == DIST_STATUS:
        if current_user.is_admin:
            return redirect(url_for("admin_dist", did=cid))
        abort(404)
    if camp.status == "pausada" and not current_user.is_admin:
        abort(404)
    if request.method == "POST":
        ok, msg = submit_clip(camp, request.form.get("url", ""))
        flash(msg, "ok" if ok else "error")
        return redirect(url_for("campaign", cid=cid))
    st = campaign_stats(camp)
    mine = (Submission.query.filter_by(campaign_id=camp.id, user_id=current_user.id)
            .order_by(Submission.created_at.desc()).all())
    accounts = campaign_accounts(current_user.id, camp.id)
    accepted = [a for a in accounts if a.status == "aceptada"]
    waiting = bool(CampaignWaitlist.query.filter_by(user_id=current_user.id, campaign_id=camp.id).first())
    winners = (Submission.query.options(joinedload(Submission.user))
               .filter(Submission.campaign_id == camp.id, Submission.status == "aprobado", Submission.views > 0)
               .order_by(Submission.views.desc()).limit(10).all())
    ctab = request.args.get("tab", "resumen")
    real_member = (not current_user.is_admin and is_campaign_member(current_user, camp)) or is_mod(current_user, camp.id)
    staff_view = current_user.is_admin and request.args.get("hub") == "1"   # el equipo ve la campaña como un clipper de fuera…
    hub = real_member or staff_view                                         # …salvo que pida ver el hub
    sections, chan, posts, liked = [], None, [], set()
    support, extra = None, {}
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
            chan = next((c for sec in sections for c in sec.live if c.id == chid), None)
            if not chan:
                return redirect(url_for("campaign", cid=cid))
            if chan.kind == "top":
                extra["top"] = top_accounts(camp, request.args.get("red", ""))
            elif chan.kind == "winners":
                extra["viral"] = viral_clips(camp, request.args.get("red", ""), request.args.get("periodo", "30"))
            elif chan.kind == "calls":
                calls_ = Call.query.filter_by(campaign_id=cid).order_by(Call.starts_at).all()
                now_ = datetime.utcnow()
                extra["calls_next"] = [c for c in calls_ if c.ends_at >= now_]
                extra["calls_past"] = [c for c in reversed(calls_) if c.ends_at < now_][:20]
            if chan.kind in ("posts", "rules", "resources", "calls"):
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
                           mod_ids_all=campaign_mod_ids(cid) if support and support["open"] else set(), **extra)


def _camp_nets(camp, with_views=False):
    q = db.session.query(Submission.platform).filter(Submission.campaign_id == camp.id).distinct()
    have = {p for (p,) in q}
    return [p for p in camp.platform_list if p in have]


def top_accounts(camp, red=""):
    """Cuentas de la campaña con más vistas (sin contar clips rechazados), por red. Con su foto de perfil."""
    handle = db.func.lower(Submission.author)
    q = (db.session.query(Submission.platform, handle, db.func.coalesce(db.func.sum(Submission.views), 0),
                          db.func.count(Submission.id), db.func.coalesce(db.func.max(Submission.views), 0),
                          db.func.min(Submission.user_id), db.func.min(Submission.author))
         .filter(Submission.campaign_id == camp.id, Submission.status != "rechazado", Submission.author != "",
                 Submission.author.isnot(None))
         .group_by(Submission.platform, handle))
    nets = [p for p in camp.platform_list if p in {r[0] for r in q.with_entities(Submission.platform).distinct()}]
    if red in nets:
        q = q.filter(Submission.platform == red)
    top = q.order_by(db.func.coalesce(db.func.sum(Submission.views), 0).desc()).limit(15).all()
    accs = {(a.platform, a.handle.lower()): a for a in CampaignAccount.query.filter_by(campaign_id=camp.id, status="aceptada")
            .filter(db.func.lower(CampaignAccount.handle).in_([r[1] for r in top] or [""]))}
    users = {u.id: u for u in User.query.filter(User.id.in_([r[5] for r in top] or [0]))}
    rows = [{"platform": r[0], "handle": r[6], "views": int(r[2] or 0), "clips": r[3], "best": int(r[4] or 0),
             "acc": accs.get((r[0], r[1])), "user": users.get(r[5])} for r in top]
    return {"rows": rows, "nets": nets, "red": red if red in nets else ""}


VIRAL_PERIODS = {"7": ("7 días", 7), "30": ("30 días", 30), "todo": ("Siempre", 0)}


def viral_clips(camp, red="", periodo="30"):
    """Los vídeos con más vistas de la campaña, para ver qué funciona. Filtros: red y periodo."""
    periodo = periodo if periodo in VIRAL_PERIODS else "30"
    days = VIRAL_PERIODS[periodo][1]
    nets = _camp_nets(camp)
    q = (Submission.query.options(joinedload(Submission.campaign))
         .filter(Submission.campaign_id == camp.id, Submission.status != "rechazado", Submission.views > 0))
    if days:
        q = q.filter(db.func.coalesce(Submission.posted_at, Submission.created_at) >= datetime.utcnow() - timedelta(days=days))
    if red in nets:
        q = q.filter(Submission.platform == red)
    return {"clips": q.order_by(Submission.views.desc()).limit(24).all(), "nets": nets, "red": red if red in nets else "",
            "periodo": periodo, "periods": VIRAL_PERIODS}


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
    return redirect(safe_next(nxt) if nxt else url_for("campaign", cid=ch.campaign_id, canal=ch.id))


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
    try:
        db.session.commit()
    except IntegrityError:                         # doble clic: la solicitud ya estaba
        db.session.rollback()
        flash("Ya enviaste tu solicitud para esta campaña.", "info")
        return back
    notify_phone(f"🙋 {current_user.name} quiere unirse", f"«{camp.song}». Revisa su solicitud.",
                 url_for("admin_home", _anchor="unirse"), tags="raising_hand")
    flash("¡Solicitud enviada! Te avisaremos cuando el equipo la revise.", "ok")
    return back


def campaign_account_problem(camp, platform, handle):
    """¿Por qué no puede el clipper añadir esta cuenta a la campaña? None si puede (y la deja vinculada a su perfil)."""
    cid = camp.id
    if platform not in camp.platform_list:
        return "Elige una red de las que acepta la campaña."
    if not handle:
        return "Escribe el usuario de la cuenta con la que vas a publicar."
    if handle_owner(platform, handle, exclude_user_id=current_user.id):
        return f"La cuenta @{handle} ya está vinculada a otro clipper."
    if False and camp.new_accounts_only:               # (opción quitada)
        used = (CampaignAccount.query.filter(CampaignAccount.platform == platform, CampaignAccount.handle == handle,
                                             CampaignAccount.campaign_id != cid).first()
                or Submission.query.filter(Submission.platform == platform, Submission.author == handle,
                                           Submission.campaign_id != cid).first())
        if used:
            return f"Esta campaña pide una cuenta nueva, solo para ella. @{handle} ya se usó en otra campaña."
    other = (CampaignAccount.query.filter(CampaignAccount.platform == platform, CampaignAccount.handle == handle,
                                          CampaignAccount.campaign_id != cid, CampaignAccount.status != "rechazada")
             .join(Campaign).filter(Campaign.status != "finalizada").first())
    if other:                                          # cada cuenta es de una sola campaña: todo lo que publica es de ella
        return f"La cuenta @{handle} ya está en la campaña «{other.campaign.song}». Cada cuenta solo puede estar en una campaña."
    existing = CampaignAccount.query.filter_by(campaign_id=cid, platform=platform, handle=handle).first()
    if existing:
        return ("Ya has añadido esa cuenta a esta campaña." if existing.user_id == current_user.id
                else f"La cuenta @{handle} ya está en esta campaña con otro clipper.")
    if len(campaign_accounts(current_user.id, cid)) >= MAX_JOIN_ACCOUNTS:
        return f"Puedes añadir como máximo {MAX_JOIN_ACCOUNTS} cuentas por campaña."
    linked = current_user.handles(platform)
    if handle not in linked:
        if len(linked) >= MAX_HANDLES:
            return f"Ya tienes {MAX_HANDLES} cuentas de {PLATFORMS[platform]} vinculadas. Quita alguna en «Mi cuenta»."
        setattr(current_user, f"{platform}_handles", ",".join(linked + [handle]))
    return None


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
    problem = campaign_account_problem(camp, platform, handle)
    if problem:
        flash(problem, "error")
        return back
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
    if api_required() and api_ready(acc.platform):
        flash(f"Para verificar esta cuenta, conéctala con {official.LABELS.get(acc.platform, acc.platform)}.", "info")
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
    aud = None                                      # en Mis clips ya no se enseña la audiencia (ubicación, edad, sexo)
    page = max(request.args.get("p", 1, type=int), 1)             # de 50 en 50; filtros y búsqueda van sobre todos
    total = len(subs)
    subs = subs[: page * LIST_PAGE]
    return render_template("submit.html", total=total, more=total > len(subs), page=page, camps=camps, subs=subs, selected=request.args.get("c", type=int), aud=aud,
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
RANK_PERIODS = {"global": ("Siempre", 0), "mes": ("Este mes", 30), "semana": ("Esta semana", 7)}


def mask_name(name):
    """«Ana García López» → «Ana G.» — en el ranking nadie ve el nombre completo de los demás."""
    parts = (name or "Clipper").split()
    return parts[0][:16] + (f" {parts[1][0].upper()}." if len(parts) > 1 and parts[1] else "")


def ranking(period="global", limit=50):
    """Clasificación. «Siempre»: por dinero ganado (la misma del Inicio). «Esta semana» y «Este mes»: por las vistas
    conseguidas DENTRO del periodo (vistas de ahora menos la última lectura anterior al periodo), no por clips enviados."""
    days = RANK_PERIODS.get(period, RANK_PERIODS["global"])[1]
    base_q = (db.session.query(Submission.user_id).join(User, User.id == Submission.user_id)
              .filter(Submission.status == "aprobado", User.is_banned.is_(False), User.is_admin.is_(False)))
    if days:
        since = datetime.utcnow() - timedelta(days=days)
        before = (db.session.query(ViewSnapshot.submission_id.label("sid"), db.func.max(ViewSnapshot.views).label("v0"))
                  .filter(ViewSnapshot.taken_at < since).group_by(ViewSnapshot.submission_id).subquery())
        gained = db.func.sum(db.case((Submission.views > db.func.coalesce(before.c.v0, 0),
                                      Submission.views - db.func.coalesce(before.c.v0, 0)), else_=0))
        q = (base_q.outerjoin(before, before.c.sid == Submission.id)
             .with_entities(Submission.user_id, gained, db.literal(None), db.func.count(Submission.id))
             .group_by(Submission.user_id).having(gained > 0).order_by(gained.desc(), Submission.user_id))
    else:
        q = (base_q.with_entities(Submission.user_id, db.func.sum(Submission.views), db.func.sum(Submission.earned),
                                  db.func.count(Submission.id))
             .group_by(Submission.user_id)                  # por dinero ganado; a igualdad, por vistas
             .order_by(db.func.sum(Submission.earned).desc(), db.func.sum(Submission.views).desc()))
    def load():
        return [tuple(r) for r in q.limit(limit).all()]
    rows = ttl_cache(f"ranking:{period}:{limit}", 120, load)
    users = {u.id: u for u in User.query.filter(User.id.in_([r[0] for r in rows])).all()} if rows else {}
    return [{"pos": i + 1, "user": users[r[0]], "name": mask_name(users[r[0]].name), "views": int(r[1] or 0),
             "earned": cents(r[2]) if r[2] is not None else None, "clips": r[3]} for i, r in enumerate(rows) if r[0] in users]


RANK_GOALS = (1000, 500, 250, 100, 50, 25, 10)      # «te faltan X para el top N»
RANK_FIRE = 10                                        # subir al menos estos puestos de un día a otro lleva fueguito


def rank_table():
    """Clasificación global: (usuario, ganado, vistas) de quien tiene algún clip aprobado, ya en orden. 2 min en caché."""
    def load():
        e, v = db.func.coalesce(db.func.sum(Submission.earned), 0), db.func.coalesce(db.func.sum(Submission.views), 0)
        q = (db.session.query(Submission.user_id, e, v).join(User, User.id == Submission.user_id)
             .filter(Submission.status == "aprobado", User.is_banned.is_(False), User.is_admin.is_(False))
             .group_by(Submission.user_id).order_by(e.desc(), v.desc(), Submission.user_id))
        return [(uid, cents(a), int(b or 0)) for uid, a, b in q]
    return ttl_cache("rank_table", 120, load)


def _clippers_before(uid=None):
    q = db.session.query(db.func.count(User.id)).filter(User.is_banned.is_(False), User.is_admin.is_(False))
    if uid is not None:
        q = q.filter(User.id < uid)
    return int(q.scalar() or 0)


def my_rank(user):
    """Tu puesto en la clasificación global. Sale siempre: quien aún no ha ganado nada va detrás, por orden de llegada."""
    rows = rank_table()
    total = max(ttl_cache("clippers_total", 120, _clippers_before), len(rows), 1)
    idx = next((i for i, r in enumerate(rows) if r[0] == user.id), None)
    if idx is not None:
        pos, earned, views = idx + 1, rows[idx][1], rows[idx][2]
    else:
        ranked_before = sum(1 for r in rows if r[0] < user.id)
        pos, earned, views = len(rows) + 1 + max(0, _clippers_before(user.id) - ranked_before), 0.0, 0
    total = max(total, pos)
    goal = pos - 1 if pos <= 10 else next((g for g in RANK_GOALS if g < pos), 10)
    goal = min(goal, len(rows))                       # nadie por delante con dinero más allá del último de la lista
    target = rows[goal - 1] if goal >= 1 and goal < pos else None
    need = cents(target[1] - earned + 0.01) if target else 0
    up = (user.rank_prev - pos) if user.rank_prev and user.rank_prev > pos else 0
    return {"pos": pos, "total": total, "earned": earned, "views": views, "pct": max(1, round(pos / total * 100)),
            "goal": goal if target else 0, "goal_top": bool(target) and goal >= 10, "need": need,
            "bar": 100 if not target else (min(100, round(earned / target[1] * 100)) if target[1] else 0),
            "up": up, "fire": up >= RANK_FIRE}


def rank_top(n=6):
    rows = rank_table()[:n]
    users = {u.id: u for u in User.query.filter(User.id.in_([r[0] for r in rows] or [0]))}
    return [{"pos": i + 1, "user": users[r[0]], "name": mask_name(users[r[0]].name), "earned": r[1], "views": r[2]}
            for i, r in enumerate(rows) if r[0] in users]


def snapshot_ranks():
    """Tras la lectura diaria: guarda el puesto de cada clipper (y el del día anterior) para enseñar las subidas."""
    _ttl.pop("rank_table", None)                      # con las cifras recién leídas, no las guardadas
    rows = rank_table()
    ranked = {r[0]: i + 1 for i, r in enumerate(rows)}
    rest = [uid for (uid,) in db.session.query(User.id).filter(User.is_banned.is_(False), User.is_admin.is_(False))
            .order_by(User.id) if uid not in ranked]
    pos = dict(ranked)
    for k, uid in enumerate(rest):
        pos[uid] = len(rows) + 1 + k
    db.session.execute(db.update(User).values(rank_prev=User.rank_pos))
    db.session.bulk_update_mappings(User, [{"id": uid, "rank_pos": p_} for uid, p_ in pos.items()])
    db.session.commit()
    return len(pos)


@app.route("/ranking")
@login_required
def ranking_page():
    period = request.args.get("p", "global")             # por defecto, la misma clasificación que enseña el Inicio
    period = period if period in RANK_PERIODS else "global"
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
    """Los clips aprobados con más vistas de los últimos 7 días. Si la semana trae pocos, la tabla se completa con los
    mejores de los últimos 30 días y, si aún faltan, con los mejores de siempre (nunca queda medio vacía)."""
    base = (Submission.query.options(joinedload(Submission.user), joinedload(Submission.campaign))
            .join(Campaign).join(User, User.id == Submission.user_id)
            .filter(Submission.status == "aprobado", User.is_banned.is_(False), User.is_admin.is_(False),
                    Submission.views > 0))                                                   # igual que el ranking
    out, now = [], datetime.utcnow()
    for days in (7, 30, 0):
        q = base.filter(Submission.id.notin_([s.id for s in out] or [0]))
        if days:
            q = q.filter(Submission.created_at >= now - timedelta(days=days))
        out += q.order_by(Submission.views.desc()).limit(limit - len(out)).all()
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- bienvenida (un solo paso, todo obligatorio)
USERNAME_RE = re.compile(r"^[a-z0-9._]{3,24}$")


def clean_username(raw):
    return (raw or "").strip().lstrip("@").lower()


def username_problem(un, uid):
    """Por qué no vale ese @usuario (o None si vale)."""
    if not USERNAME_RE.match(un):
        return "Tu usuario: de 3 a 24 letras, números, punto o guion bajo, sin espacios."
    if User.query.filter(db.func.lower(User.username) == un, User.id != uid).first():
        return f"El usuario @{un} ya está cogido. Prueba otro."
    return None


def suggest_username(u):
    import unicodedata
    base = unicodedata.normalize("NFKD", u.name or "").encode("ascii", "ignore").decode().lower()
    base = re.sub(r"[^a-z0-9]", "", base)[:18] or "clipper"
    base = base if len(base) >= 3 else base + "clip"
    for i in range(30):
        cand = base if i == 0 else f"{base}{secrets.randbelow(900) + 100}"
        if not username_problem(cand, u.id):
            return cand
    return ""


@app.route("/bienvenida", methods=["GET", "POST"])
@login_required
def onboarding():
    u = current_user
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()[:120]
        un = clean_username(request.form.get("username"))
        c = request.form.get("country", "")
        other = " ".join((request.form.get("country_other") or "").split())[:60]
        file = request.files.get("photo")
        photo = None
        if file and file.filename:
            photo = process_avatar(file)
            if not photo:
                flash("La foto tiene que ser una imagen (JPG, PNG o WEBP).", "error")
                return redirect(url_for("onboarding"))
        if photo:                                # la foto se guarda ya, aunque falte otro dato
            u.avatar_data, u.avatar_ver = photo, (u.avatar_ver or 0) + 1
            u.avatar_url = url_for("avatar", uid=u.id, v=u.avatar_ver)
            db.session.commit()
        problem = ("Escribe tu nombre." if len(name) < 2 else
                   username_problem(un, u.id) or
                   ("Elige tu país." if c not in COUNTRIES else
                    "Escribe en qué país vives." if c == "OT" and len(other) < 2 else None))
        if problem:
            session["onb_form"] = {"name": name, "username": un, "country": c, "country_other": other}
            flash(problem, "error")
            return redirect(url_for("onboarding"))
        u.name, u.username = name, un
        u.country, u.country_other = c, (other if c == "OT" else "")
        u.languages = "es"                       # todos publican en español: ya no se pregunta
        u.onboarded_at = datetime.utcnow()       # el cobro (Whop) se conecta luego en «Mi cuenta»
        session.pop("onb_form", None)
        notify_user(u.id, "bienvenida", f"Bienvenido a {APP_NAME}, {name.split()[0]}",
                    "Así empiezas: mira la guía de Formación, elige una campaña y pide unirte.", "/formacion")
        db.session.commit()
        flash("¡Perfil listo! Elige una campaña y pide unirte.", "ok")
        return redirect(url_for("index"))
    form = session.pop("onb_form", None) or {"name": u.name, "username": u.username or suggest_username(u),
                                             "country": u.country, "country_other": u.country_other}
    return render_template("onboarding.html", form=form)


def process_avatar(file):
    """Foto de perfil: cuadrada, 256 px, JPEG limpio (sin datos de la cámara)."""
    from PIL import Image, ImageOps, UnidentifiedImageError
    try:
        img = Image.open(file.stream, formats=IMG_FORMATS)
        img.verify()
        file.stream.seek(0)
        img = ImageOps.exif_transpose(Image.open(file.stream, formats=IMG_FORMATS)).convert("RGB")
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
    if not CLIPPER_WITHDRAW:
        flash("Ya no hace falta pedir retiros: cuando termina cada campaña, te enviamos lo ganado a tu Whop.", "info")
        return redirect(url_for("earnings"))
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


@app.route("/cuenta/tema", methods=["POST"])
@login_required
def set_theme():
    """Modo oscuro: lo elige cada usuario (Ajustes o menú del perfil) y se guarda en su cuenta."""
    current_user.theme = "dark" if request.form.get("theme") == "dark" else ""
    db.session.commit()
    if request.headers.get("X-Requested-With") == "fetch":
        return ("", 204)
    return redirect(url_for("account", _anchor="cuenta"))


@app.route("/cuenta", methods=["GET", "POST"])
@login_required
def account():
    """Ajustes del clipper: perfil, cobros, notificaciones y cuenta, en una sola página con índice a la izquierda."""
    if request.method == "POST":
        section = request.form.get("section")
        if section == "perfil":
            u = current_user
            name = (request.form.get("name") or "").strip()[:120]
            if len(name) < 2:
                flash("Escribe tu nombre.", "error")
                return redirect(url_for("account", _anchor="perfil"))
            file = request.files.get("photo")
            if file and file.filename:
                data = process_avatar(file)
                if not data:
                    flash("La foto tiene que ser una imagen (JPG, PNG o WEBP).", "error")
                    return redirect(url_for("account", _anchor="perfil"))
                u.avatar_data, u.avatar_ver = data, (u.avatar_ver or 0) + 1
                u.avatar_url = url_for("avatar", uid=u.id, v=u.avatar_ver)
            c = request.form.get("country", "")
            other = " ".join((request.form.get("country_other") or "").split())[:60]
            if c in COUNTRIES:
                if c == "OT" and len(other) < 2:
                    flash("Escribe en qué país vives.", "error")
                    return redirect(url_for("account", _anchor="perfil"))
                u.country, u.country_other = c, (other if c == "OT" else "")
            un = clean_username(request.form.get("username"))
            if un and un != (u.username or ""):
                problem = username_problem(un, u.id)
                if problem:
                    flash(problem, "error")
                    return redirect(url_for("account", _anchor="perfil"))
                u.username = un
            u.name = name
            db.session.commit()
            flash("Perfil guardado.", "ok")
            return redirect(url_for("account", _anchor="perfil"))
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
    return render_template("account.html", events=NOTIFY_EVENTS, channels=NOTIFY_CHANNELS, wants=wants,
                           tokens=ApiToken.query.filter_by(user_id=current_user.id).order_by(ApiToken.platform).all(),
                           devices=PushSub.query.filter_by(user_id=current_user.id).count(),
                           push_ready=push_ready(), vapid=VAPID_PUBLIC_KEY, clipper_push=CLIPPER_PUSH)


# ---------------------------------------------------------------- panel de admin
def _per_request(key, fn):
    """Calcula algo una sola vez por petición (la campanita y el menú del panel piden lo mismo varias veces)."""
    if not has_request_context():
        return fn()
    memo = g.setdefault("_memo", {})
    if key not in memo:
        memo[key] = fn()
    return memo[key]


_ttl, _ttl_lock = {}, threading.Lock()


def ttl_cache(key, seconds, fn):
    """Guarda un cálculo caro unos segundos (cifras iguales para todos: rankings, totales de campaña…).
    Con miles de usuarios, cada página deja de repetir las mismas sumas. Solo valores sencillos (nunca filas de la base
    de datos). En las pruebas no se guarda nada."""
    if app.testing or os.environ.get("PYTEST_CURRENT_TEST"):
        return _per_request(key, fn)
    now = time.time()
    hit = _ttl.get(key)
    if hit and hit[0] > now:
        return hit[1]
    val = fn()
    with _ttl_lock:
        _ttl[key] = (now + seconds, val)
        if len(_ttl) > 500:                           # limpieza de lo caducado
            for k in [k for k, v in _ttl.items() if v[0] <= now]:
                _ttl.pop(k, None)
    return val


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


def review_campaign_ids():
    """La campaña de cada clip que está «por revisar», SIN cargar los clips enteros (solo cuatro columnas).
    Misma regla que clip_stage: pendiente, ya llegó al mínimo y, si se piden estadísticas, ya las envió.
    Con miles de clips pendientes, cargar los clips completos en cada pantalla del panel costaba medio segundo."""
    def load():
        camps = {c.id: c for c in Campaign.query.all()}
        ask_stats = bunny_ready()
        out, rule = [], {}                                 # (campaña, red) -> (mínimo, tarifa): se calcula una vez, no por clip
        for cid, plat, views, st in (db.session.query(Submission.campaign_id, Submission.platform, Submission.views,
                                                       Submission.stats_status).filter(Submission.status == "pendiente")):
            camp = camps.get(cid)
            if not camp:
                continue
            if (cid, plat) not in rule:
                rule[(cid, plat)] = (camp.min_for(plat), camp.rate_for(plat))
            low, rate = rule[(cid, plat)]
            if low and (views or 0) / 1_000_000 * rate + 1e-9 < low:
                continue                                   # contando vistas
            if ask_stats and st != "enviado":
                continue                                   # falta el vídeo de estadísticas
            out.append(cid)
        return out
    return _per_request("review_campaign_ids", load)


def _pending_by_campaign():
    out = {}
    for cid_ in review_campaign_ids():
        out.setdefault(cid_, {"clips": 0, "requests": 0, "accounts": 0})["clips"] += 1
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
    return ttl_cache("review_count", 15, lambda: len(review_campaign_ids())
                     + Submission.query.filter_by(appeal_status="pendiente").count())


def admin_counts():
    """Números del menú lateral del panel: lo que está esperando a que hagas algo."""
    return _per_request("admin_counts", _admin_counts)


def _admin_counts():
    def n(model, *cond):                              # cada número, una subconsulta: todos juntos en UNA consulta
        return db.select(db.func.count()).select_from(model).where(*cond).scalar_subquery()
    req, accs, sup, pay, msg, leads = db.session.execute(db.select(
        n(CampaignMember, CampaignMember.status == "pendiente"),
        n(CampaignAccount, CampaignAccount.status == "pendiente"),
        db.select(db.func.count(db.distinct(CampMessage.user_id)))
        .where(CampMessage.from_staff.is_(False), CampMessage.read_by_staff.is_(False)).scalar_subquery(),
        n(Payout, Payout.status.in_(PAYOUT_OPEN)),
        n(Message, Message.from_admin.is_(False), Message.read_by_admin.is_(False)),
        n(CampaignLead, CampaignLead.status == "nueva"))).one()
    req, accs, sup = int(req or 0), int(accs or 0), int(sup or 0)
    clips = review_queue_count()
    return {
        "requests": req,
        "clips": clips,
        "accounts": accs,
        "payouts": int(pay or 0),
        "messages": int(msg or 0),
        "leads": int(leads or 0),
        "creators_alert": 0,
        "support": sup,
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


def _views_as_of(when):
    """Vistas que sumaban los clips aprobados en un momento dado (la última lectura de cada clip hasta entonces)."""
    last = (db.session.query(ViewSnapshot.submission_id.label("sid"), db.func.max(ViewSnapshot.taken_at).label("mt"))
            .join(Submission, Submission.id == ViewSnapshot.submission_id)
            .filter(Submission.status == "aprobado", Submission.created_at <= when, ViewSnapshot.taken_at <= when)
            .group_by(ViewSnapshot.submission_id).subquery())
    per_clip = (db.session.query(ViewSnapshot.submission_id, db.func.max(ViewSnapshot.views).label("v"))
                .join(last, db.and_(ViewSnapshot.submission_id == last.c.sid, ViewSnapshot.taken_at == last.c.mt))
                .group_by(ViewSnapshot.submission_id).subquery())
    return int(db.session.query(db.func.coalesce(db.func.sum(per_clip.c.v), 0)).scalar() or 0)


def views_week():
    """Vistas conseguidas en los últimos 7 días y el cambio frente a los 7 anteriores."""
    c = int(db.session.query(db.func.coalesce(db.func.sum(Submission.views), 0))
            .filter(Submission.status == "aprobado").scalar() or 0)
    if not Submission.query.filter(Submission.status == "aprobado").first():
        return {"views": 0, "change": None}
    now = datetime.utcnow()
    a, b = _views_as_of(now - timedelta(days=14)), _views_as_of(now - timedelta(days=7))
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
        "week": ttl_cache("views_week", 600, views_week),
        "views": db.session.query(db.func.coalesce(db.func.sum(Submission.views), 0)).filter(Submission.status == "aprobado").scalar() or 0,
        "live_camps": sum(1 for c in camps if c.status == "activa"),
    }


@app.route("/admin")
@admin_required
def admin_home():
    camps = Campaign.query.filter(Campaign.status != DIST_STATUS).order_by(Campaign.created_at.desc()).all()
    cards = [(c, campaign_stats(c)) for c in camps]
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
    pend = pending_by_campaign()
    todo = [(c, pend[c.id]) for c, _st in cards if c.id in pend and any(pend[c.id].get(k) for k in ("clips", "requests", "accounts", "support"))]
    general = [al for al in alerts if not al[1].startswith("«") or "agotarse" in al[1]]
    return render_template("admin/overview.html", **overview_v2(camps, cards, live, todo, general, request.args.get("p", "30")))


ADMIN_METRICS = ("views", "sent", "approved", "spent", "paid", "users")


def views_daily(start, days):
    """Vistas que ganaron los clips aprobados cada día (diferencia entre la última lectura de un día y la del anterior)."""
    since = datetime.combine(start, datetime.min.time())
    base = dict(db.session.query(ViewSnapshot.submission_id, db.func.max(ViewSnapshot.views))
                .join(Submission, Submission.id == ViewSnapshot.submission_id)
                .filter(Submission.status == "aprobado", ViewSnapshot.taken_at < since)
                .group_by(ViewSnapshot.submission_id).all())
    level = {}                                   # clip → {día: vistas más altas leídas ese día}
    for sid, when, v in (ViewSnapshot.query.join(Submission, Submission.id == ViewSnapshot.submission_id)
                         .filter(Submission.status == "aprobado", ViewSnapshot.taken_at >= since)
                         .with_entities(ViewSnapshot.submission_id, ViewSnapshot.taken_at, ViewSnapshot.views)):
        i = (when.date() - start).days
        if 0 <= i < days:
            d = level.setdefault(sid, {})
            d[i] = max(d.get(i, 0), v or 0)
    out = [0.0] * days
    for sid, by_day in level.items():
        prev = base.get(sid) or 0
        for i in range(days):
            if i in by_day:
                cur = max(prev, by_day[i])       # las vistas no bajan: una lectura más baja no resta
                out[i] += cur - prev
                prev = cur
    return [float(v) for v in out]


def views_split(start, days, from_i=0):
    """Como views_daily, pero repartido: vistas por red y día, y vistas por campaña desde el día from_i (para el ranking)."""
    since = datetime.combine(start, datetime.min.time())
    base = dict(db.session.query(ViewSnapshot.submission_id, db.func.max(ViewSnapshot.views))
                .join(Submission, Submission.id == ViewSnapshot.submission_id)
                .filter(Submission.status == "aprobado", ViewSnapshot.taken_at < since)
                .group_by(ViewSnapshot.submission_id).all())
    level, meta = {}, {}
    for sid, when, v, pl, cid in (ViewSnapshot.query.join(Submission, Submission.id == ViewSnapshot.submission_id)
                                  .filter(Submission.status == "aprobado", ViewSnapshot.taken_at >= since)
                                  .with_entities(ViewSnapshot.submission_id, ViewSnapshot.taken_at, ViewSnapshot.views,
                                                 Submission.platform, Submission.campaign_id)):
        i = (when.date() - start).days
        if 0 <= i < days:
            d = level.setdefault(sid, {})
            d[i] = max(d.get(i, 0), v or 0)
            meta[sid] = (pl, cid)
    plat, camp = {}, {}
    for sid, by_day in level.items():
        pl, cid = meta[sid]
        row = plat.setdefault(pl, [0.0] * days)
        prev = base.get(sid) or 0
        for i in range(days):
            if i in by_day:
                cur = max(prev, by_day[i])
                row[i] += cur - prev
                if i >= from_i:
                    camp[cid] = camp.get(cid, 0) + cur - prev
                prev = cur
    return {"plat": plat, "camp": camp}


OVERVIEW_PERIODS = {"7": "Últimos 7 días", "30": "Últimos 30 días", "todo": "Desde siempre"}


def overview_periods(p):
    """Periodo del Resumen: días que se miran y tramos de la gráfica de clips (por días, semanas o meses)."""
    today = datetime.utcnow().date()
    if p == "7":
        days, buckets = 7, [(today - timedelta(days=i), today - timedelta(days=i - 1)) for i in range(6, -1, -1)]
        return days, buckets, "day"
    if p == "todo":
        first = db.session.query(db.func.min(Submission.created_at)).scalar() or datetime.utcnow()
        days = max(30, min(730, (today - first.date()).days + 1))
        months, y, m = [], today.year, today.month
        for _ in range(min(12, days // 28 + 1)):
            a0 = datetime(y, m, 1).date()
            a1 = datetime(y + (m == 12), m % 12 + 1, 1).date()
            months.append((a0, a1))
            y, m = (y - 1, 12) if m == 1 else (y, m - 1)
        return days, months[::-1], "month"
    return 30, [(today - timedelta(days=7 * w + 6), today - timedelta(days=7 * w - 1)) for w in range(5, -1, -1)], "week"


def overview_v2(camps, cards, live, todo, general, p="30"):
    """Datos del Resumen: cuatro cifras con su minigráfica y su cambio frente al periodo anterior, clips por tramo,
    actividad reciente, campañas activas y equipo. Periodo: 7 días, 30 días o desde siempre."""
    p = p if p in OVERVIEW_PERIODS else "30"
    days, buckets, unit = overview_periods(p)
    allt = p == "todo"
    an = admin_analytics(days if allt else days * 2)
    cards_k = []
    for key, label, ico, money_ in (("views", "Vistas", "eye", False), ("spent", "Dinero repartido", "cash", True),
                                    ("sent", "Clips enviados", "film", False), ("users", "Clippers nuevos", "users", False)):
        d = an["series"]["daily"][key]
        cur = d if allt else d[days:]
        now_, prev = sum(cur), (0 if allt else sum(d[:days]))
        if len(cur) > 60:                                   # minigráfica: como mucho 60 puntos
            k_ = -(-len(cur) // 60); cur = [sum(cur[i:i + k_]) for i in range(0, len(cur), k_)]
        cards_k.append({"key": key, "label": label, "icon": ico, "money": money_, "value": now_, "spark": cur if len(cur) > 1 else [0] + cur,
                        "change": round((now_ - prev) / prev * 100, 1) if prev and max(now_, prev) >= 10 else None})   # con menos de 10, el % engaña
    MES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
    weeks = []
    for a0, a1 in buckets:
        q = Submission.query.filter(Submission.created_at >= datetime.combine(a0, datetime.min.time()),
                                    Submission.created_at < datetime.combine(a1, datetime.min.time()))
        label = MES[a0.month - 1] if unit == "month" else f"{a0.day}/{a0.month:02d}"
        weeks.append({"label": label, "sent": q.count(), "ok": q.filter(Submission.status == "aprobado").count(),
                      "ko": q.filter(Submission.status == "rechazado").count()})
    # gráficas del Resumen (Mustapha quiere probarlas todas): vistas y dinero, coste por 1.000, por red, campañas y clippers activos
    dates = [datetime.fromisoformat(x).date() for x in an["series"]["labels"]]
    dv, ds = an["series"]["daily"]["views"], an["series"]["daily"]["spent"]
    span = len(dates)
    split = ttl_cache(f"views_split_{p}_{dates[0]}", 900, lambda: views_split(dates[0], span, 0 if allt else span - days))
    def bsum(arr, a0, a1):
        return sum(v for d_, v in zip(dates, arr) if a0 <= d_ < a1)
    ch_vm, ch_cpm, ch_net, ch_act = [], [], [], []
    for (a0, a1), w in zip(buckets, weeks):
        v_, m_ = bsum(dv, a0, a1), bsum(ds, a0, a1)
        ch_vm.append({"label": w["label"], "views": v_, "money": m_})
        ch_cpm.append({"label": w["label"], "v": round(m_ / v_ * 1000, 2) if v_ >= 1000 else None})
        ch_net.append({"label": w["label"], **{k: bsum(split["plat"].get(k, []), a0, a1) for k in PLATFORMS}})
        ch_act.append({"label": w["label"], "n": db.session.query(db.func.count(db.distinct(Submission.user_id))).filter(
            Submission.created_at >= datetime.combine(a0, datetime.min.time()),
            Submission.created_at < datetime.combine(a1, datetime.min.time())).scalar() or 0})
    rates = [c.rate for c, _st in live if c.rate]
    names = {c.id: c.song for c in Campaign.query.filter(Campaign.id.in_(list(split["camp"]) or [0])).all()}
    top = sorted(((names.get(cid, "?"), v_) for cid, v_ in split["camp"].items() if v_ > 0), key=lambda x: -x[1])[:5]
    charts = {"vm": ch_vm, "cpm": ch_cpm, "cpm_ref": round(sum(rates) / len(rates) / 1000, 2) if rates else None,
              "net": ch_net, "nets": [k for k in PLATFORMS if any(r[k] for r in ch_net)], "top": top, "act": ch_act}
    ev = []
    for s_ in Submission.query.filter(Submission.status == "aprobado", Submission.reviewed_at.isnot(None)).order_by(Submission.reviewed_at.desc()).limit(4):
        ev.append((s_.reviewed_at, "ok", "green", f"Clip aprobado · {s_.views:,} vistas".replace(",", ".")))
    for u in User.query.filter(User.is_admin.is_(False)).order_by(User.created_at.desc()).limit(3):
        ev.append((u.created_at, "users", "violet", f"Nuevo clipper: {u.name}"))
    for p_ in Payout.query.filter(Payout.status == "pagado").order_by(Payout.paid_at.desc()).limit(2):
        ev.append((p_.paid_at, "cash", "green", f"Retiro pagado: {cents(p_.amount):.2f} $".replace(".", ",")))
    for c in Campaign.query.order_by(Campaign.created_at.desc()).limit(2):
        ev.append((c.created_at, "flag", "amber", f"Campaña creada: «{c.song}»"))
    ev = sorted([e for e in ev if e[0]], key=lambda e: e[0], reverse=True)[:5]
    mods = []
    for m in CampaignMod.query.all():
        u = db.session.get(User, m.user_id)
        if u and all(x["u"].id != u.id for x in mods):
            mods.append({"u": u, "camps": CampaignMod.query.filter_by(user_id=u.id).count()})
    for u in User.query.filter_by(is_admin=True).all():
        if all(x["u"].id != u.id for x in mods):
            mods.append({"u": u, "camps": None})
    n_rev = sum(pd.get("clips", 0) for _c, pd in todo)
    n_req = sum(pd.get("requests", 0) + pd.get("accounts", 0) for _c, pd in todo)
    return dict(kc=cards_k, weeks=weeks, ev=ev, live=live, mods=mods[:4], n_rev=n_rev, n_req=n_req, general=general,
                period=p, periods=OVERVIEW_PERIODS, unit=unit, charts=charts)


def admin_analytics(days=30):
    """Las cifras del Resumen, guardadas 2 minutos: con decenas de miles de clips recalcularlas en cada visita costaba
    casi medio segundo. Se devuelve una copia para que nadie cambie lo guardado."""
    import copy
    return copy.deepcopy(ttl_cache(f"admin_analytics_{days}", 120, lambda: _admin_analytics(days)))


def _admin_analytics(days=30):
    """Gráfica del resumen: envíos, aprobados, dinero repartido, pagado y clippers nuevos, día a día (consultas ligeras)."""
    today = datetime.utcnow().date()
    start = today - timedelta(days=days - 1)
    since = datetime.combine(start, datetime.min.time())
    dates = [start + timedelta(days=i) for i in range(days)]
    idx = {d: i for i, d in enumerate(dates)}
    daily = {m: [0.0] * days for m in ADMIN_METRICS}

    def add(metric, when, v=1):
        i = idx.get(when.date()) if when else None
        if i is not None:
            daily[metric][i] += float(v or 0)
    for (when,) in Submission.query.filter(Submission.created_at >= since).with_entities(Submission.created_at):
        add("sent", when)
    for when, earned in (Submission.query.filter(Submission.status == "aprobado", Submission.reviewed_at >= since)
                         .with_entities(Submission.reviewed_at, Submission.earned)):
        add("approved", when)
        add("spent", when, earned)
    for when, amount in (Payout.query.filter(Payout.status == "pagado", Payout.paid_at >= since)
                         .with_entities(Payout.paid_at, Payout.amount)):
        add("paid", when, amount)
    for (when,) in User.query.filter(User.created_at >= since, User.is_admin.is_(False)).with_entities(User.created_at):
        add("users", when)
    daily["views"] = list(ttl_cache(f"views_daily_{days}", 900, lambda: views_daily(start, days)))
    cum = {}
    for m in ADMIN_METRICS:
        run, out = 0.0, []
        for v in daily[m]:
            run += v
            out.append(round(run, 2))
        cum[m] = out
        daily[m] = [round(v, 2) for v in daily[m]]
    totals = {m: cum[m][-1] if cum[m] else 0 for m in ADMIN_METRICS}
    return {"totals": totals, "series": {"labels": [d.isoformat() for d in dates], "cumulative": cum, "daily": daily}}


@app.route("/admin/campanas")
@admin_required
def admin_campaigns():
    camps = Campaign.query.filter(Campaign.status != DIST_STATUS).order_by(Campaign.created_at.desc()).all()
    filt = request.args.get("estado", "")
    filt = filt if filt in ("activa", "pausada", "finalizada") else ""   # se filtra en la página: llegan todas
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
    invites = (Invite.query.filter_by(campaign_id=cid).options(joinedload(Invite.user))
               .order_by(Invite.used_at.isnot(None), Invite.created_at.desc()).limit(100).all() if cid else [])
    return render_template("admin/requests.html", joins=joins, estado=estado, camp=camp, accounts=accounts,
                           pend=pending_by_campaign().get(cid, {}) if cid else {}, sec="solicitudes", invites=invites)


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
    ok = Submission.status == "aprobado"                        # todo en dos consultas, haya 10 clippers o 500
    stats = {uid: {"clips": int(n), "approved": int(ap or 0), "views": int(v or 0), "earned": cents(float(e or 0)), "last": last}
             for uid, n, ap, v, e, last in db.session.query(
                 Submission.user_id, db.func.count(Submission.id), db.func.sum(db.case((ok, 1), else_=0)),
                 db.func.sum(db.case((ok, Submission.views), else_=0)), db.func.sum(db.case((ok, Submission.earned), else_=0)),
                 db.func.max(Submission.created_at)).filter(Submission.campaign_id == cid).group_by(Submission.user_id)}
    accs = {}
    for a_ in CampaignAccount.query.filter_by(campaign_id=cid).order_by(CampaignAccount.created_at).all():
        accs.setdefault(a_.user_id, []).append(a_)
    rows = sorted(((m, stats.get(m.user_id, {"clips": 0, "approved": 0, "views": 0, "earned": 0.0, "last": None}),
                    accs.get(m.user_id, [])) for m in members), key=lambda r: -r[1]["views"])
    q = (request.args.get("q") or "").strip().lower().lstrip("@")
    if q:                                              # la búsqueda mira a todos, no solo a los que se ven
        rows = [r for r in rows if q in (r[0].user.name or "").lower() or q in (r[0].user.email or "").lower()
                or any(q in (a_.handle or "").lower() for a_ in r[2])]
    total = len(rows)
    page = max(request.args.get("p", 1, type=int), 1)
    rows = rows[: page * LIST_PAGE * 2]                # por tandas de 100: con mil clippers la página pesaba más de 1 MB
    mods = CampaignMod.query.filter_by(campaign_id=cid).options(joinedload(CampaignMod.user)).all()
    return render_template("admin/camp_clippers.html", camp=camp, rows=rows, pend=pending_by_campaign().get(cid, {}),
                           sec="clippers", mods=mods, mod_ids={m_.user_id for m_ in mods}, total=total, page=page,
                           more=total > len(rows), q=request.args.get("q", ""))


def mod_pick_user(uid, text):
    """Usuario elegido para hacerlo mod: por la lista (id), o escribiendo su nombre exacto o su correo."""
    if uid:
        u = db.session.get(User, uid)
        if u:
            return u
    t = (text or "").strip()
    if not t:
        return None
    if "@" in t and "." in t.split("@")[-1]:
        return User.query.filter(db.func.lower(User.email) == t.lower()).first()
    rows = User.query.filter(db.func.lower(User.name) == t.lower()).limit(2).all()
    return rows[0] if len(rows) == 1 else None


@app.route("/admin/mods", methods=["GET", "POST"])
@admin_required
def admin_mods():
    """Todos los mods en un sitio: en qué campañas están, qué pueden hacer y cuánto trabajan."""
    if request.method == "POST":
        act = request.form.get("act", "")
        chosen = set(request.form.getlist("perms"))
        perms = [p_ for p_ in MOD_PERMS if p_ in chosen]
        perm_str = ",".join(perms)
        boss = current_user.is_admin                  # las funciones generales solo las da un admin (un mod no se las da a sí mismo)
        general = [k for k in STAFF_PERMS if k in set(request.form.getlist("staff"))] if boss else []
        if act == "anadir":
            u = mod_pick_user(request.form.get("uid", type=int), request.form.get("user") or request.form.get("email") or "")
            cids = [int(x) for x in request.form.getlist("campaigns") if x.isdigit()]
            if not u:
                flash("No encontramos a ese usuario. Elígelo de la lista mientras escribes su nombre.", "error")
            elif u.is_admin:
                flash(f"{u.name} ya es del equipo: puede hacerlo todo.", "info")
            elif u.is_banned:
                flash("No puedes hacer mod a un usuario bloqueado.", "error")
            elif u.id == current_user.id:
                flash("No puedes cambiar tus propios permisos.", "error")
            elif not cids and not general:
                flash("Elige al menos una campaña o una función general.", "error")
            elif cids and not perms:
                flash("Marca al menos un permiso de campaña.", "error")
            else:
                n = 0
                if general:
                    set_staff_perms(u, general)
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
                audit("mod_añadido", f"{u.email} · {len(cids)} campañas · {perm_str or 'todos'}"
                      + (f" · generales: {','.join(general)}" if general else ""))
                flash(f"{u.name} es mod" + (f" en {n} {'campaña nueva' if n == 1 else 'campañas nuevas'}" if cids else "")
                      + (" con funciones generales" if general else "") + ".", "ok")
        elif act == "general":
            if not boss:
                abort(403)
            u = db.session.get(User, request.form.get("uid", type=int) or 0) or abort(404)
            if u.is_admin or u.id == current_user.id:
                abort(400)
            set_staff_perms(u, general)
            audit("mod_generales", f"{u.email} · {','.join(general) or 'ninguna'}")
            flash(f"Funciones generales de {u.name} guardadas." if general else f"{u.name} ya no tiene funciones generales.", "ok")
        elif act == "permisos":
            row = db.session.get(CampaignMod, request.form.get("mid", type=int) or 0) or abort(404)
            if row.user_id == current_user.id:          # un mod con «Mods de campaña» no se sube los permisos a sí mismo
                abort(403)
            if not perms:
                flash("Marca al menos un permiso (o quita al mod de esa campaña).", "error")
            else:
                row.perms = perm_str
                audit("mod_permisos", f"{row.user.email} · «{row.campaign.song}» · {perm_str or 'todos'}")
                flash(f"Permisos de {row.user.name} en «{row.campaign.song}» guardados.", "ok")
        elif act == "quitar":
            row = db.session.get(CampaignMod, request.form.get("mid", type=int) or 0) or abort(404)
            if row.user_id == current_user.id:
                abort(403)
            audit("mod_quitado", f"{row.user.email} · «{row.campaign.song}»")
            flash(f"{row.user.name} ya no es mod de «{row.campaign.song}».", "ok")
            db.session.delete(row)
        elif act == "quitar_todo":
            uid = request.form.get("uid", type=int) or 0
            if uid == current_user.id:
                abort(403)
            rows = CampaignMod.query.filter_by(user_id=uid).all()
            for row in rows:
                db.session.delete(row)
            u = db.session.get(User, uid)
            if u and boss:
                u.staff_perms = ""
            audit("mod_quitado", f"{u.email if u else uid} · todas las campañas")
            flash("Ya no es mod de nada." if boss else "Ya no es mod de ninguna campaña.", "ok")
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
    for u in User.query.filter(User.staff_perms.isnot(None), User.staff_perms != "", User.is_admin.is_(False)).all():
        mods.setdefault(u.id, {"user": u, "rows": []})                  # mods solo con funciones generales
    for uid, m in mods.items():
        m["general"] = staff_keys(m["user"])
        acts = dict(db.session.query(AuditLog.action, db.func.count(AuditLog.id))
                    .filter(AuditLog.admin_id == uid, AuditLog.created_at >= week).group_by(AuditLog.action).all())
        m["activity"] = {
            "clips": acts.get("clip_aprobado", 0) + acts.get("clip_rechazado", 0),
            "requests": acts.get("clipper_aceptado", 0) + acts.get("clipper_rechazado", 0),
            "support": CampMessage.query.filter(CampMessage.sender_id == uid, CampMessage.from_staff.is_(True),
                                                CampMessage.created_at >= week).count(),
            "posts": Post.query.filter(Post.author_id == uid, Post.created_at >= week).count(),
        }
        m["recent"] = AuditLog.query.filter_by(admin_id=uid).order_by(AuditLog.created_at.desc()).limit(6).all()
        m["last"] = m["recent"][0].created_at if m["recent"] else None
        m["total"] = sum(m["activity"].values())
    camps = Campaign.query.filter(Campaign.status.notin_(("finalizada", DIST_STATUS))).order_by(Campaign.created_at.desc()).all()
    pick = User.query.filter(User.is_admin.is_(False), User.is_banned.is_(False)).with_entities(User.id, User.name).order_by(User.name).all()
    seen = {}
    for _id, nm in pick:
        seen[(nm or "").lower()] = seen.get((nm or "").lower(), 0) + 1
    pick = [(i, n if seen[(n or "").lower()] == 1 else f"{n} #{i}") for i, n in pick if n]
    return render_template("admin/mods.html", pick=pick, mods=sorted(mods.values(), key=lambda m: m["user"].name.lower()),
                           camps=camps, perms=MOD_PERMS, defaults=MOD_DEFAULT, risky=MOD_RISKY, staff_perms=STAFF_PERMS,
                           boss=current_user.is_admin)


def set_staff_perms(user, keys):
    """Guarda las funciones generales de un mod y, si es la primera vez, le avisa de que ya tiene panel."""
    keys = [k for k in STAFF_PERMS if k in set(keys)]
    had = bool(staff_keys(user))
    user.staff_perms = ",".join(keys)
    if keys and not had:
        names = ", ".join(STAFF_PERMS[k][0] for k in keys)
        notify_user(user.id, "mod", "Ahora tienes acceso al panel del equipo",
                    f"Puedes entrar desde tu menú → «Panel del equipo». Tienes: {names}.", staff_home_url(user))


DIST_PLATFORMS = ("tiktok", "instagram", "youtube")


def parse_dist_accounts(text_):
    """«tiktok @cuenta» o un enlace de perfil por línea → [(red, cuenta)]."""
    out = []
    for line in (text_ or "").splitlines():
        line = line.strip()
        if not line:
            continue
        low = line.lower()
        plat = next((p for p in DIST_PLATFORMS if p in low or (p == "youtube" and "youtu" in low)), None)
        handle = line.rstrip("/").split("/")[-1] if "/" in line else line.split()[-1]
        handle = viewcounter.clean_handle(handle)
        if plat and handle and handle not in DIST_PLATFORMS and (plat, handle) not in out:
            out.append((plat, handle))
    return out[:40]


def dist_add_accounts(camp, pairs):
    n = 0
    for p, h in pairs:
        if CampaignAccount.query.filter_by(campaign_id=camp.id, platform=p, handle=h).first():
            continue
        db.session.add(CampaignAccount(user_id=current_user.id, campaign_id=camp.id, platform=p, handle=h, status="aceptada",
                                       bio_verified=True, bio_status="Cuenta del cliente", decided_at=datetime.utcnow()))
        n += 1
    return n


def dist_summary(camp, days=30):
    since = datetime.utcnow() - timedelta(days=days)
    subs = list(camp.submissions)
    recent = [s for s in subs if (s.posted_at or s.created_at) >= since]
    accs = CampaignAccount.query.filter_by(campaign_id=camp.id).order_by(CampaignAccount.platform, CampaignAccount.handle).all()
    per = {}
    for s in subs:
        r = per.setdefault((s.platform, (s.author or "").lower()), {"views": 0, "clips": 0, "recent": 0, "best": 0})
        r["views"] += s.views or 0
        r["clips"] += 1
        r["best"] = max(r["best"], s.views or 0)
        if s in recent:
            r["recent"] += 1
    rows = [{"a": a_, **per.get((a_.platform, a_.handle.lower()), {"views": 0, "clips": 0, "recent": 0, "best": 0})} for a_ in accs]
    return {"accounts": rows, "views": sum(s.views or 0 for s in subs), "clips": len(subs),
            "recent_views": sum(s.views or 0 for s in recent), "recent_clips": len(recent),
            "posting": sum(1 for r in rows if r["recent"]), "n_accounts": len(accs)}


@app.route("/admin/distribucion", methods=["GET", "POST"])
@admin_required
def admin_dists():
    """Planes de distribución: cuentas de un cliente que lleva la agencia. Se detectan solos sus vídeos y se ven sus estadísticas."""
    if request.method == "POST":
        client = " ".join((request.form.get("client") or "").split())[:200]
        name = " ".join((request.form.get("name") or "").split())[:200] or "Plan de distribución"
        pairs = parse_dist_accounts(request.form.get("accounts", ""))
        if not client:
            flash("Escribe el nombre del cliente.", "error")
            return redirect(url_for("admin_dists"))
        camp = Campaign(song=name, artist=client, status=DIST_STATUS, budget=0, rate=0, kind="clipping",
                        platforms=",".join(DIST_PLATFORMS), report_token=secrets.token_urlsafe(18))
        db.session.add(camp)
        db.session.flush()
        n = dist_add_accounts(camp, pairs)
        audit("distribucion_creada", f"«{name}» · {client} · {n} cuentas")
        db.session.commit()
        flash(f"Plan creado con {n} {'cuenta' if n == 1 else 'cuentas'}. Sus vídeos se leerán solos en la próxima lectura diaria.", "ok")
        return redirect(url_for("admin_dist", did=camp.id))
    plans = (Campaign.query.filter_by(status=DIST_STATUS).options(selectinload(Campaign.submissions))
             .order_by(Campaign.created_at.desc()).all())
    return render_template("admin/dists.html", plans=[(p, dist_summary(p)) for p in plans])


@app.route("/admin/distribucion/<int:did>", methods=["GET", "POST"])
@admin_required
def admin_dist(did):
    camp = db.session.get(Campaign, did)
    if not camp or camp.status != DIST_STATUS:
        abort(404)
    if request.method == "POST":
        act = request.form.get("act", "")
        if act == "cuentas":
            n = dist_add_accounts(camp, parse_dist_accounts(request.form.get("accounts", "")))
            flash(f"{n} {'cuenta añadida' if n == 1 else 'cuentas añadidas'}." if n else "No había cuentas nuevas (revisa el formato).",
                  "ok" if n else "error")
        elif act == "quitar":
            acc = db.session.get(CampaignAccount, request.form.get("aid", type=int) or 0)
            if not acc or acc.campaign_id != did:
                abort(404)
            db.session.delete(acc)
            flash(f"@{acc.handle} quitada del plan. Sus vídeos ya contados se quedan.", "ok")
        elif act == "nombre":
            camp.song = " ".join((request.form.get("name") or "").split())[:200] or camp.song
            camp.artist = " ".join((request.form.get("client") or "").split())[:200] or camp.artist
            flash("Guardado.", "ok")
        elif act == "borrar":
            sids = [s.id for s in camp.submissions]
            if sids:
                ViewSnapshot.query.filter(ViewSnapshot.submission_id.in_(sids)).delete(synchronize_session=False)
                AudienceStat.query.filter(AudienceStat.submission_id.in_(sids)).delete(synchronize_session=False)
            Submission.query.filter_by(campaign_id=did).delete(synchronize_session=False)
            CampaignAccount.query.filter_by(campaign_id=did).delete(synchronize_session=False)
            Notice.query.filter_by(campaign_id=did).delete(synchronize_session=False)
            audit("distribucion_borrada", f"«{camp.song}» · {camp.artist}")
            db.session.delete(camp)
            db.session.commit()
            flash("Plan borrado.", "ok")
            return redirect(url_for("admin_dists"))
        else:
            abort(400)
        db.session.commit()
        return redirect(url_for("admin_dist", did=did))
    if not camp.report_token:
        camp.report_token = secrets.token_urlsafe(18)
        db.session.commit()
    report_url = (BASE_URL or request.host_url.rstrip("/")) + url_for("report", token=camp.report_token)
    clips = sorted(camp.submissions, key=lambda s: s.posted_at or s.created_at, reverse=True)[:60]
    return render_template("admin/dist.html", camp=camp, sm=dist_summary(camp), an=campaign_analytics(camp), clips=clips,
                           aud=audience_for(list(camp.submissions)),
                           report_url=report_url, dist=True)


@app.route("/conectar/<platform>")
@login_required
def api_connect(platform):
    """Conectar una cuenta con la API oficial: manda a la red a pedir permiso."""
    if platform not in official.SCOPES:
        abort(404)
    # Aunque aún no estén las claves, se manda a la página real de la red (lo pidió Mustapha): sin claves, la red
    # enseña su propio error. Al volver, el aviso de «muy pronto» lo da api_callback.
    state = secrets.token_urlsafe(16)
    session["api_state"] = {"s": state, "p": platform, "next": safe_next(request.args.get("next", "")) or "",
                            "cid": request.args.get("cid", type=int)}       # desde «Añadir cuenta» de una campaña
    return redirect(official.auth_url(platform, state, api_redirect(platform)))


@app.route("/conectar/<platform>/vuelta")
@login_required
def api_callback(platform):
    """Vuelta de la red: guardamos el permiso (cifrado) y, si es una cuenta de campaña, queda verificada."""
    st = session.pop("api_state", None) or {}
    back = st.get("next") or url_for("account", _anchor="redes")
    if platform not in official.SCOPES or st.get("p") != platform or not secrets.compare_digest(st.get("s", ""), request.args.get("state", "")):
        flash("La conexión ha caducado. Vuelve a intentarlo.", "error")
        return redirect(back)
    if not api_ready(platform):
        flash(f"Muy pronto podrás conectar {official.LABELS[platform]}. Mientras tanto, añade tus cuentas con el código en la bio.", "info")
        return redirect(back)
    if request.args.get("error") or not request.args.get("code"):
        flash("No se ha conectado la cuenta: no se dio permiso.", "info")
        return redirect(back)
    try:
        tk = official.exchange_code(platform, request.args["code"], api_redirect(platform))
        prof = official.profile(platform, tk["access"])
    except official.ApiError as e:
        log.warning("Fallo conectando %s: %s", platform, e)
        flash(f"No hemos podido conectar tu {official.LABELS[platform]}. Inténtalo de nuevo en un rato.", "error")
        return redirect(back)
    ext_id, handle = str(prof.get("id") or tk.get("open_id") or ""), viewcounter.clean_handle(prof.get("handle") or "")
    if not ext_id:
        flash("La red no nos ha dicho qué cuenta es. Inténtalo de nuevo.", "error")
        return redirect(back)
    tok = ApiToken.query.filter_by(platform=platform, ext_id=ext_id).first()
    if tok and tok.user_id != current_user.id:
        flash(f"La cuenta @{handle} ya está conectada por otro clipper. Si es tuya, escríbenos.", "error")
        return redirect(back)
    if not tok:
        tok = ApiToken(user_id=current_user.id, platform=platform, ext_id=ext_id)
        db.session.add(tok)
    tok.handle, tok.avatar_url = handle, (prof.get("avatar") or "")[:1000]
    tok.access_enc, tok.status, tok.error = enc(tk["access"]), "ok", ""
    if tk.get("refresh"):
        tok.refresh_enc = enc(tk["refresh"])
    tok.expires_at = datetime.utcnow() + timedelta(seconds=int(tk.get("expires_in") or 3600))
    added = ""
    camp = db.session.get(Campaign, st["cid"]) if st.get("cid") else None
    if camp:                                           # «Añadir cuenta → Conectar»: la cuenta entra en la campaña ya verificada
        member = campaign_member(current_user.id, camp.id)
        mine = CampaignAccount.query.filter_by(campaign_id=camp.id, platform=platform, handle=handle, user_id=current_user.id).first()
        if member and member.status == "aceptada" and not mine:
            problem = campaign_account_problem(camp, platform, handle)
            if problem:
                db.session.commit()
                flash(f"{official.LABELS[platform]} conectado (@{handle}), pero no se ha añadido a la campaña: {problem}", "error")
                return redirect(back)
            db.session.add(CampaignAccount(user_id=current_user.id, campaign_id=camp.id, platform=platform, handle=handle,
                                           status="pendiente"))
            db.session.flush()
            added = f" Cuenta @{handle} añadida y verificada: ya puedes publicar con ella."
    n = 0
    for acc_ in CampaignAccount.query.filter_by(user_id=current_user.id, platform=platform).all():
        if viewcounter.clean_handle(acc_.handle) == handle:          # la cuenta es suya: verificada sin código en la bio
            acc_.bio_verified, acc_.bio_status = True, "Verificada (conectada)"
            if tok.avatar_url:
                acc_.avatar_url = tok.avatar_url
            if acc_.status == "pendiente":
                acc_.status, acc_.decided_at = "aceptada", datetime.utcnow()
                n += 1
    audit("cuenta_conectada", f"{official.LABELS[platform]} @{handle}")
    db.session.commit()
    flash(f"{official.LABELS[platform]} conectado: @{handle}." + (added or (" Cuenta verificada en tu campaña." if n else "")), "ok")
    return redirect(back)


@app.route("/desconectar/<int:tid>", methods=["POST"])
@login_required
def api_disconnect(tid):
    tok = db.session.get(ApiToken, tid)
    if not tok or (tok.user_id != current_user.id and not current_user.is_admin):
        abort(404)
    AudienceStat.query.filter_by(platform=tok.platform, handle=tok.handle, submission_id=None).delete()
    audit("cuenta_desconectada", f"{official.LABELS[tok.platform]} @{tok.handle}")
    db.session.delete(tok)
    db.session.commit()
    flash("Cuenta desconectada. Sus datos de audiencia se han borrado; seguimos contando sus vistas como antes.", "ok")
    return redirect(url_for("account", _anchor="redes"))


@app.route("/eliminar-datos")
def data_deletion_page():
    """Página pública que piden Meta, TikTok y Google: cómo borrar tus datos."""
    return render_template("data_deletion.html", code=request.args.get("codigo", ""))


@app.route("/meta/borrar-datos", methods=["POST"])
def meta_data_deletion():
    """Petición de Meta para borrar los datos de una cuenta de Instagram (signed_request firmado con la clave de la app)."""
    import hashlib
    import hmac
    raw = request.form.get("signed_request", "")
    try:
        sig_b64, payload_b64 = raw.split(".", 1)
        pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
        sig = base64.urlsafe_b64decode(pad(sig_b64))
        payload = json.loads(base64.urlsafe_b64decode(pad(payload_b64)))
        good = hmac.new(official.INSTAGRAM_APP_SECRET.encode(), payload_b64.encode(), hashlib.sha256).digest()
        if not official.INSTAGRAM_APP_SECRET or not hmac.compare_digest(sig, good):
            abort(400)
    except (ValueError, TypeError):
        abort(400)
    uid = str(payload.get("user_id", ""))
    code = secrets.token_hex(8)
    for tok in ApiToken.query.filter_by(platform="instagram", ext_id=uid).all():
        AudienceStat.query.filter_by(platform="instagram", handle=tok.handle).delete()
        db.session.delete(tok)
    audit("datos_borrados_meta", f"instagram {uid} · {code}")
    db.session.commit()
    return {"url": (BASE_URL or request.host_url.rstrip("/")) + url_for("data_deletion_page", codigo=code), "confirmation_code": code}


@app.route("/invitacion/<token>")
@login_required
def use_invite(token):
    """Enlace de invitación de un solo uso: entras directo en la campaña, ya aceptado."""
    inv = Invite.query.filter_by(token=token).first()
    if not inv or not inv.campaign or inv.campaign.status == "finalizada":
        flash("Este enlace de invitación no existe o la campaña ya terminó.", "error")
        return redirect(url_for("campaigns_page"))
    camp = inv.campaign
    if inv.used_by and inv.used_by != current_user.id:
        flash("Este enlace de invitación ya se ha usado. Cada enlace sirve para una sola persona: pide uno nuevo al equipo.", "error")
        return redirect(url_for("campaign", cid=camp.id))
    if current_user.is_admin:
        flash("Este enlace es para clippers. Como equipo ya puedes ver la campaña.", "info")
        return redirect(url_for("campaign", cid=camp.id))
    m = campaign_member(current_user.id, camp.id)
    if m and m.status == "aceptada":
        flash(f"Ya estás dentro de «{camp.song}».", "info")
        return redirect(url_for("campaign", cid=camp.id))
    if not inv.used_by:
        inv.used_by, inv.used_at = current_user.id, datetime.utcnow()
    if m:
        m.status, m.decided_at = "aceptada", datetime.utcnow()
    else:
        db.session.add(CampaignMember(user_id=current_user.id, campaign_id=camp.id, status="aceptada", decided_at=datetime.utcnow()))
    audit("invitacion_usada", f"«{camp.song}» · {current_user.email}")
    db.session.commit()
    flash(f"¡Bienvenido a «{camp.song}»! Ahora añade la cuenta con la que vas a publicar y verifícala.", "ok")
    return redirect(url_for("campaign", cid=camp.id))


@app.route("/admin/campana/<int:cid>/invitaciones", methods=["POST"])
@staff_required(lambda kw: kw.get("cid"))
def admin_invites(cid):
    """Crear o borrar enlaces de invitación de un solo uso."""
    camp = db.session.get(Campaign, cid) or abort(404)
    act = request.form.get("act", "")
    if act == "crear":
        n = max(1, min(request.form.get("n", type=int) or 1, 50))
        note = " ".join((request.form.get("note") or "").split())[:120]
        for _ in range(n):
            db.session.add(Invite(campaign_id=cid, token=secrets.token_urlsafe(12), note=note, created_by=current_user.id))
        audit("invitaciones_creadas", f"«{camp.song}» · {n}")
        flash(f"{n} {'enlace creado' if n == 1 else 'enlaces creados'}. Cada uno sirve para una sola persona.", "ok")
    elif act == "borrar":
        inv = db.session.get(Invite, request.form.get("id", type=int) or 0)
        if not inv or inv.campaign_id != cid:
            abort(404)
        db.session.delete(inv)
        flash("Enlace borrado: ya no sirve.", "ok")
    else:
        abort(400)
    db.session.commit()
    return redirect(url_for("admin_campaign_requests", cid=cid, _anchor="invitaciones"))


@app.route("/modero")
@login_required
def modero():
    """Panel «Modero»: todas las campañas que modera esta persona, con lo pendiente de cada una."""
    rows = CampaignMod.query.filter_by(user_id=current_user.id).all()
    if not rows:
        abort(404)
    pend = pending_by_campaign()
    items = []
    for r in rows:
        if not r.campaign:
            continue
        ps = mod_perms(current_user, r.campaign_id)
        pd = pend.get(r.campaign_id, {})
        links = [(perm, label, ep, n) for perm, label, ep, n in (
            ("revisar", "Revisar clips", "admin_campaign_review", pd.get("clips", 0)),
            ("solicitudes", "Solicitudes", "admin_campaign_requests", pd.get("requests", 0) + pd.get("accounts", 0)),
            ("soporte", "Soporte", "admin_campaign_support", pd.get("support", 0)),
            ("canales", "Canales", "admin_campaign_channels", 0),
            ("estadisticas", "Estadísticas", "admin_campaign", 0),
            ("clippers", "Clippers", "admin_campaign_clippers", 0),
            ("editar", "Ajustes", "admin_campaign_form", 0)) if perm in ps]
        items.append({"camp": r.campaign, "links": links, "todo": sum(n for *_x, n in links)})
    items.sort(key=lambda it: (-it["todo"], it["camp"].song.lower()))
    week = datetime.utcnow() - timedelta(days=7)
    acts = dict(db.session.query(AuditLog.action, db.func.count(AuditLog.id))
                .filter(AuditLog.admin_id == current_user.id, AuditLog.created_at >= week).group_by(AuditLog.action).all())
    mine = {"clips": acts.get("clip_aprobado", 0) + acts.get("clip_rechazado", 0),
            "requests": acts.get("clipper_aceptado", 0) + acts.get("clipper_rechazado", 0)}
    return render_template("modero.html", items=items, mine=mine)


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
    # Los números de las pestañas, siempre (antes Aprobados/Rechazados salían a 0 fuera de su pestaña)
    nq = db.session.query(Submission.status, db.func.count(Submission.id)).filter(Submission.status.in_(("aprobado", "rechazado")))
    if cid:
        nq = nq.filter(Submission.campaign_id == cid)
    if acting_as_mod():
        nq = nq.filter(Submission.user_id != current_user.id)
    n_done = dict(nq.group_by(Submission.status).all())
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
    camps = Campaign.query.filter(Campaign.status != DIST_STATUS).order_by(Campaign.created_at.desc()).all() if not acting_as_mod() or admin_can("admin_clips") else []
    camp = db.session.get(Campaign, cid) if cid else None
    return render_template("admin/clips.html", rows=shown, total=total, more=total > len(shown), page=page, tab=tab, f=f,
                           q=request.args.get("q", ""), camps=camps, cid=cid, camp=camp, counts=counts,
                           n={"revisar": len(rev), "contando": len(cnt), "apelaciones": len(appeals),
                              "aprobados": n_done.get("aprobado", 0), "rechazados": n_done.get("rechazado", 0)},
                           growth=clip_growth(shown), players={s_.id: clip_player(s_) for s_ in shown},
                           bots={s_.id: bot_check(s_) for s_ in shown}, hours={s_.id: waiting_hours(s_) for s_ in shown},
                           sla=REVIEW_SLA_HOURS, filters=REVIEW_FILTERS, in_camp=bool(camp_id), bunny_on=bunny_ready(),
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
    return render_template("admin/payouts.html", open_requests=open_requests, failed=failed, history=history,
                           to_pay=campaigns_to_pay())


def campaigns_to_pay():
    """Campañas terminadas a las que aún les queda dinero por pagar: [(campaña, por pagar, clippers)]. Dos consultas."""
    earned = dict(((cid_, uid_), float(v)) for cid_, uid_, v in db.session.query(
        Submission.campaign_id, Submission.user_id, db.func.sum(Submission.earned)).join(Campaign)
        .filter(Campaign.status == "finalizada", Submission.status == "aprobado", Submission.earned > 0)
        .group_by(Submission.campaign_id, Submission.user_id))
    sent = dict(((cid_, uid_), float(v)) for cid_, uid_, v in db.session.query(
        Payout.campaign_id, Payout.user_id, db.func.sum(Payout.amount))
        .filter(Payout.campaign_id.isnot(None), Payout.status.in_(PAYOUT_OPEN + ("pagado",)))
        .group_by(Payout.campaign_id, Payout.user_id))
    per = {}
    for (cid_, uid_), v in earned.items():
        left = cents(v - sent.get((cid_, uid_), 0))
        if left >= CENT:
            tot, n = per.get(cid_, (0.0, 0))
            per[cid_] = (cents(tot + left), n + 1)
    camps = {c.id: c for c in Campaign.query.filter(Campaign.id.in_(list(per))).all()} if per else {}
    return sorted([(camps[k], v[0], v[1]) for k, v in per.items() if k in camps], key=lambda x: x[0].ended_at or datetime.min)


def search_info():
    def load(k, d):
        try:
            return json.loads(get_setting(k, "") or d)
        except ValueError:
            return json.loads(d)
    return {"engine": search_engine(), "engines": SEARCH_ENGINES, "installed": viewcounter.ytdlp_available(),
            "stats": load("ytdlp_stats", "{}"), "check": load("ytdlp_check", "{}"), "times": YTDLP_CHECK_AT,
            "test": session.pop("ytdlp_test", None), "apify": apify_summary(), "stats_on": ytdlp_stats_on(),
            "daily_max": apify_daily_max(), "used_today": apify_used_today(), "scan": load("scan_info", "{}"),
            "deferred": load("apify_deferred", "{}")}


def bunny_info():
    q = Submission.query.filter(Submission.platform.in_(("tiktok", "instagram")), Submission.status == "pendiente")
    return {"ready": bunny_ready(), "lib": BUNNY_LIBRARY_ID, "cdn": BUNNY_CDN_HOST,
            "listo": q.filter(Submission.video_status == "listo").count(),
            "subiendo": q.filter(Submission.video_status == "subiendo").count(),
            "error": q.filter(Submission.video_status == "error", Submission.video_guid == "").count(),
            "waiting": len(copy_candidates())}


# ---------------------------------------------------------------- Panel técnico → Salud (24 nov, Mustapha: «muy muy completo»)
# Todo con datos reales de la web. Lo que la web no puede saber (las copias de Railway) se dice tal cual.
TEST_WHOP_APP = "app_Ljk0vbU7F4IBzy"          # la app de prueba del 24 nov, en una cuenta que no es de ClipProfit
TABLE_NAMES = {"view_snapshot": "Lecturas de vistas", "submission": "Clips enviados", "user": "Usuarios", "message": "Mensajes",
               "audit_log": "Actividad", "error_log": "Fallos apuntados", "campaign": "Campañas", "payout": "Retiros",
               "campaign_account": "Cuentas de redes", "notice": "Avisos", "setting": "Ajustes", "camp_message": "Chat de campañas",
               "post": "Publicaciones", "post_comment": "Comentarios", "stat_hour": "Cifras por hora", "campaign_member": "Clippers en campañas",
               "audience_stat": "Audiencias", "api_token": "Conexiones de redes", "lesson_progress": "Progreso de formación"}
SALUD_STATES = ("ok", "warn", "bad", "off")


def _ua_label(ua):
    ua = ua or ""
    nav = ("Edge" if "Edg/" in ua else "Opera" if "OPR/" in ua else "Chrome" if "Chrome/" in ua or "CriOS" in ua
           else "Firefox" if "Firefox/" in ua or "FxiOS" in ua else "Safari" if "Safari/" in ua else "Navegador")
    dev = ("iPhone" if "iPhone" in ua else "iPad" if "iPad" in ua else "Android" if "Android" in ua else "Windows" if "Windows" in ua
           else "Mac" if "Mac OS" in ua else "Linux" if "Linux" in ua else "")
    return f"{nav} en {dev}" if dev else nav


def _mb(n):
    return f"{n / 1048576:,.0f} MB".replace(",", ".") if n >= 10 * 1048576 else f"{n / 1048576:.1f} MB".replace(".", ",")


def _when(dt):
    """«hoy 03:41», «ayer 23:10», «12 nov 09:00» en hora de España."""
    if not dt:
        return "nunca"
    loc, today = _local(dt), _local().date()
    hm = loc.strftime("%H:%M")
    if loc.date() == today:
        return f"hoy {hm}"
    if loc.date() == today - timedelta(days=1):
        return f"ayer {hm}"
    return f"{loc.day} {MESES_ES[loc.month - 1]} {hm}"


def _iso(v):
    try:
        return datetime.fromisoformat(v) if v else None
    except ValueError:
        return None


def _mem():
    """Memoria de este proceso y el límite del contenedor, si se puede leer."""
    rss = limit = None
    try:
        for line in open("/proc/self/status"):
            if line.startswith("VmRSS:"):
                rss = int(line.split()[1]) * 1024
    except OSError:
        pass
    for f in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            v = open(f).read().strip()
            if v.isdigit() and int(v) < 1 << 50:
                limit = int(v)
                break
        except OSError:
            pass
    return rss, limit


def _db_stats():
    t0 = time.perf_counter()
    db.session.execute(text("SELECT 1"))
    ping = (time.perf_counter() - t0) * 1000
    tables = []
    if db.engine.dialect.name == "postgresql":
        size = int(db.session.execute(text("SELECT pg_database_size(current_database())")).scalar() or 0)
        for name, n, b in db.session.execute(text(
                "SELECT relname, n_live_tup, pg_total_relation_size(relid) FROM pg_stat_user_tables ORDER BY 3 DESC")):
            tables.append((name, int(n or 0), int(b or 0)))
        for i, (name, n, b) in enumerate(tables[:6]):      # las 6 que más ocupan: filas exactas (la estadística de Postgres va con retraso)
            if re.fullmatch(r"[a-z_]+", name):
                tables[i] = (name, int(db.session.execute(text(f'SELECT count(*) FROM "{name}"')).scalar() or 0), b)
    else:
        size = int(db.session.execute(text("PRAGMA page_count")).scalar() or 0) * int(db.session.execute(text("PRAGMA page_size")).scalar() or 0)
        for t in db.metadata.sorted_tables:
            tables.append((t.name, int(db.session.execute(db.select(db.func.count()).select_from(t)).scalar() or 0), 0))
        tables.sort(key=lambda r: -r[1])
    try:
        hist = json.loads(get_setting("db_size_hist", "") or "{}")
    except ValueError:
        hist = {}
    today = _local().strftime("%Y-%m-%d")
    if hist.get(today) != size:
        hist[today] = size
        hist = dict(sorted(hist.items())[-60:])
        set_setting("db_size_hist", json.dumps(hist))
        db.session.commit()
    old = [v for d, v in hist.items() if d <= (_local() - timedelta(days=6)).strftime("%Y-%m-%d")]
    growth = size - old[-1] if old else None
    top = [{"name": TABLE_NAMES.get(n, n.replace("_", " ")), "rows": r, "bytes": b} for n, r, b in tables[:6]]
    rest = tables[6:]
    maxb = max([t["bytes"] for t in top] + [1])
    maxr = max([t["rows"] for t in top] + [1])
    for t in top:
        t["pct"] = round(100 * (t["bytes"] / maxb if t["bytes"] else t["rows"] / maxr))
    last_copy = AuditLog.query.filter_by(action="copia_descargada").order_by(AuditLog.created_at.desc()).first()
    return {"ping": ping, "size": size, "size_txt": _mb(size), "tables": top, "n_tables": len(tables),
            "rest": {"n": len(rest), "rows": sum(r[1] for r in rest), "bytes": sum(r[2] for r in rest)},
            "growth": growth, "growth_txt": (("+" if growth >= 0 else "−") + _mb(abs(growth))) if growth is not None else "",
            "last_copy": last_copy.created_at if last_copy else None, "backups_ok": get_setting("backups_ok", ""),
            "backup": backup_last()}


def backup_check():
    """Estado de la copia de la noche para «Lista para abrir»: ok si la última salió bien hace menos de 36 h."""
    if not storage_ready():
        return "bad"
    j, b = job_info("copia"), backup_last()
    if j and not j.get("ok"):
        return "bad"
    if b.get("when") and datetime.utcnow() - b["when"] < timedelta(hours=36):
        return "ok"
    return "warn"


def _hours24():
    """Las últimas 24 horas de StatHour, hora a hora, y los totales."""
    stat_flush()
    now = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
    since = now - timedelta(hours=23)
    rows = {r.hour: r for r in StatHour.query.filter(StatHour.hour >= since).all()}
    hours, tot = [], {k: 0 for k in ("requests", "ms_total", "slow", "errors", "blocked", "csrf", "bots")}
    for i in range(24):
        h = since + timedelta(hours=i)
        r = rows.get(h)
        vals = {k: (getattr(r, k) or 0) if r else 0 for k in tot}
        for k in tot:
            tot[k] += vals[k]
        hours.append({"label": _local(h).strftime("%H h"), "n": vals["requests"], "err": vals["errors"]})
    peak = max(hours, key=lambda x: x["n"])
    mx = max(peak["n"], 1)
    for x in hours:
        x["pct"] = round(100 * x["n"] / mx)
    tot["avg_ms"] = round(tot["ms_total"] / tot["requests"]) if tot["requests"] else None
    return hours, tot, peak


def health_data():
    now = datetime.utcnow()
    day_ago, week_ago = now - timedelta(hours=24), now - timedelta(days=7)
    local_midnight = _local().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).replace(tzinfo=None)
    D = {"now": now}

    # --- servidor
    hours, tot, peak = _hours24()
    rss, limit = _mem()
    sha = (os.environ.get("RAILWAY_GIT_COMMIT_SHA") or "")[:7]
    up = int(time.time() - _BOOT)
    D["server"] = {"sha": sha or "local", "msg": (os.environ.get("RAILWAY_GIT_COMMIT_MESSAGE") or "").split("\n")[0][:90],
                   "boot": datetime.utcfromtimestamp(_BOOT), "up": f"{up // 86400} d {up % 86400 // 3600} h" if up >= 86400
                   else f"{up // 3600} h {up % 3600 // 60} min" if up >= 3600 else f"{max(1, up // 60)} min",
                   "rss": _mb(rss) if rss else "—", "limit": _mb(limit) if limit else "", "rss_pct": round(100 * rss / limit) if rss and limit else None,
                   "hours": hours, "tot": tot, "peak": peak, "slow": slow_pages()[:3]}
    err_24 = db.session.query(db.func.coalesce(db.func.sum(ErrorLog.times), 0)).filter(
        ErrorLog.kind == "servidor", db.func.coalesce(ErrorLog.last_at, ErrorLog.created_at) >= day_ago).scalar() or 0
    D["server"]["err_24"] = int(err_24)

    # --- base de datos
    D["db"] = _db_stats()

    # --- lectura de vistas
    run = _refresh_run()
    lect = job_info("lectura")
    try:
        yst = json.loads(get_setting("ytdlp_stats", "") or "{}")
    except ValueError:
        yst = {}
    live = Submission.query.filter(Submission.status != "rechazado")
    per = []
    for p, label in PLATFORMS.items():
        q = live.filter(Submission.platform == p)
        n = q.count()
        if n:
            per.append({"label": label, "n": n, "today": q.filter(Submission.checked_at >= local_midnight).count(),
                        "bad": q.filter(Submission.views_error != "").count()})
    bad_clips = sum(x["bad"] for x in per)
    week_items = sum(v["items"] for v in apify_summary()["by"].values())
    h, m = (int(x) for x in REFRESH_AT.split(":")[:2])
    loc = _local()
    passed = loc.hour * 60 + loc.minute >= h * 60 + m + 75
    if not viewcounter.APIFY_TOKEN:
        rs, rmsg = "bad", "Falta la clave de Apify: no se leen vistas"
    elif run["tries"] >= REFRESH_TRIES and not run.get("done"):
        rs, rmsg = "bad", "La lectura de hoy no terminó"
    elif passed and not run.get("done"):
        rs, rmsg = "warn", "La lectura de hoy aún no ha terminado"
    elif bad_clips:
        rs, rmsg = "warn", f"{bad_clips} clips no se pudieron leer"
    else:
        rs, rmsg = "ok", "Al día"
    D["read"] = {"state": rs, "msg": rmsg, "done": bool(run.get("done")), "tries": run["tries"], "at": REFRESH_AT,
                 "last": get_setting("last_refresh", "Nunca"), "job": lect, "job_at": _iso(lect.get("at")),
                 "used": apify_used_today(), "max": apify_daily_max(), "week_items": week_items, "per": per, "bad": bad_clips,
                 "ytdlp_ok": yst.get("ok", 0), "ytdlp_fail": yst.get("fail", 0), "engine": search_engine()}
    D["read"]["used_pct"] = min(100, round(100 * D["read"]["used"] / D["read"]["max"])) if D["read"]["max"] > 0 else 0

    # --- dinero
    crs = creators_overview() if creators_on() else []
    last_paid = dict(db.session.query(Payout.creator_id, db.func.max(Payout.paid_at)).filter(Payout.status == "pagado")
                     .group_by(Payout.creator_id).all())
    for r in crs:
        r["last_paid"] = last_paid.get(r["c"].id)
        r["state"] = ("bad" if r["missing"] or (r["owed"] and r["balance"] is None) else
                      "warn" if r["balance"] is not None and r["owed"] and r["balance"] < r["owed"] * 1.2 else "ok")
    main_owed = creator_owed(None)
    main_bal, main_err = whop_balance(None) if WHOP_API_KEY and whop_company_id() else (None, "")
    pend = Payout.query.filter(Payout.status.in_(("solicitado", "procesando")))
    failed = Payout.query.filter(Payout.error != "", Payout.created_at >= week_ago).count()
    active = Campaign.query.filter(Campaign.status == "activa")
    D["money"] = {"creators": crs, "budget": float(db.session.query(db.func.coalesce(db.func.sum(Campaign.budget), 0))
                                                 .filter(Campaign.status == "activa").scalar() or 0),
                  "spent": float(db.session.query(db.func.coalesce(db.func.sum(Campaign.spent), 0)).scalar() or 0),
                  "pend_n": pend.count(), "pend_sum": float(db.session.query(db.func.coalesce(db.func.sum(Payout.amount), 0))
                                                           .filter(Payout.status.in_(("solicitado", "procesando"))).scalar() or 0),
                  "failed": failed, "main_owed": main_owed, "main_bal": main_bal, "main_err": main_err,
                  "lonely": active.filter(Campaign.creator_id.is_(None)).count(), "active": active.count()}
    short = [r for r in crs if r["missing"]]
    if short:
        ms, mmsg = "bad", (f"A {short[0]['c'].name} le faltan {money(short[0]['missing'])} en Whop" if len(short) == 1
                           else f"A {len(short)} creadores no les llega el saldo de Whop")
    elif failed:
        ms, mmsg = "warn", f"{failed} {'pago fallido' if failed == 1 else 'pagos fallidos'} esta semana"
    elif D["money"]["lonely"]:
        ms, mmsg = "warn", f"{D['money']['lonely']} {'campaña activa' if D['money']['lonely'] == 1 else 'campañas activas'} sin creador"
    else:
        ms, mmsg = "ok", "Todo cuadra"
    D["money"].update(state=ms, msg=mmsg)

    # --- usuarios
    clippers = User.query.filter(User.is_admin.is_(False))
    n_cl = clippers.count()
    with_whop = clippers.filter(db.or_(User.whop_user_id != "", User.whop_username != "")).count()
    regs = [u.created_at for u in User.query.with_entities(User.created_at).filter(User.created_at >= now - timedelta(days=30))]
    by_day = {}
    for c in regs:
        if c:
            k = _local(c).strftime("%Y-%m-%d")
            by_day[k] = by_day.get(k, 0) + 1
    days = [(_local() - timedelta(days=29 - i)).strftime("%Y-%m-%d") for i in range(30)]
    series = [by_day.get(d, 0) for d in days]
    mx = max(series + [1])
    pts = " ".join(f"{round(i * 500 / 29)},{round(64 - 56 * v / mx)}" for i, v in enumerate(series))
    g_fail = db.session.query(db.func.coalesce(db.func.sum(ErrorLog.times), 0)).filter(
        ErrorLog.kind == "google", db.func.coalesce(ErrorLog.last_at, ErrorLog.created_at) >= day_ago).scalar() or 0
    D["users"] = {"today": User.query.filter(User.created_at >= local_midnight).count(),
                  "week": User.query.filter(User.created_at >= week_ago).count(),
                  "online": User.query.filter(User.last_seen_at >= now - timedelta(seconds=ONLINE_FOR)).count(),
                  "active_day": User.query.filter(User.last_seen_at >= day_ago).count(),
                  "clips_today": Submission.query.filter(Submission.created_at >= local_midnight).count(),
                  "no_profile": clippers.filter(User.onboarded_at.is_(None)).count(), "clippers": n_cl,
                  "whop_pct": round(100 * with_whop / n_cl) if n_cl else 0, "google_fail": int(g_fail),
                  "pts": pts, "regs_30": sum(series), "regs_max": max(series)}

    # --- servicios
    mail_day, mail_month = mail_counts() if mail_ready() else (0, 0)
    mail_fail = EmailLog.query.filter(EmailLog.status == "error", EmailLog.created_at >= day_ago).count()
    mail_cap = EmailLog.query.filter(EmailLog.status == "tope", EmailLog.created_at >= day_ago).count()
    D["mail"] = {"ready": mail_ready(), "day": mail_day, "month": mail_month,
                 "last": EmailLog.query.order_by(EmailLog.id.desc()).limit(8).all()}
    yt_ok = viewcounter.ytdlp_available()
    apis_ok = [official.LABELS[p] for p in ("tiktok", "instagram", "youtube", "facebook") if official.configured(p)]
    svc = [
        ("Google (entrar)", ("ok" if not g_fail else "warn") if GOOGLE_CLIENT_ID else "bad",
         (f"Puesto · {g_fail} fallos al entrar en 24 h" if g_fail else "Puesto · sin fallos al entrar en 24 h") if GOOGLE_CLIENT_ID
         else "Falta GOOGLE_CLIENT_ID: nadie puede entrar"),
        ("Whop principal", ("ok" if main_bal is not None else "warn") if WHOP_API_KEY else "bad",
         (f"Responde · saldo {money(main_bal)}" if main_bal is not None else f"No se puede leer el saldo: {main_err or 'sin respuesta'}")
         if WHOP_API_KEY else "Falta WHOP_API_KEY"),
        ("App de Whop (creadores)", ("warn" if WHOP_APP_ID == TEST_WHOP_APP else "ok") if WHOP_APP_API_KEY and WHOP_APP_ID else "bad",
         (f"{sum(1 for r in crs if r['via_app'])} creadores conectados con la app"
          + (" · es la app de prueba: pásala a la cuenta de ClipProfit" if WHOP_APP_ID == TEST_WHOP_APP else ""))
         if WHOP_APP_API_KEY and WHOP_APP_ID else "Falta WHOP_APP_API_KEY o WHOP_APP_ID"),
        ("Conectar con Whop (clippers)", "ok" if WHOP_CLIENT_ID else "off",
         f"Puesto · {with_whop} de {n_cl} clippers con Whop" if WHOP_CLIENT_ID else "Sin WHOP_CLIENT_ID: solo escribiendo el usuario"),
        ("Apify (vistas)", "ok" if viewcounter.APIFY_TOKEN else "bad",
         f"Puesto · {D['read']['used']} de {D['read']['max']} resultados hoy" if viewcounter.APIFY_TOKEN else "Falta APIFY_TOKEN"),
        ("yt-dlp (búsqueda gratis)", ("warn" if yst.get("fail", 0) > yst.get("ok", 0) else "ok") if yt_ok else "warn",
         f"Hoy: {yst.get('ok', 0)} bien, {yst.get('fail', 0)} mal" if yt_ok else "No está instalado en el servidor"),
        ("Bunny (vídeos)", "ok" if bunny_ready() else "off",
         "Puesto · copias de vídeo y formación" if bunny_ready() else "Sin clave: no se copian los vídeos de verificación"),
        ("Correo (Resend)", (("bad" if mail_fail else "warn" if mail_day >= MAIL_DAY_MAX - MAIL_RESERVE else "ok") if mail_ready()
                             else "bad" if PASSWORD_LOGIN else "off"),
         (f"Hoy {mail_day} de {MAIL_DAY_MAX} · este mes {mail_month} de {MAIL_MONTH_MAX}"
          + (f" · {mail_fail} fallidos en 24 h" if mail_fail else "") + (f" · {mail_cap} sin enviar por el tope" if mail_cap else ""))
         if mail_ready() else ("Falta: nadie puede confirmar su correo ni recuperar la contraseña" if PASSWORD_LOGIN
                               else "Sin poner (solo se entra con Google)")),
        ("Sentry (avisos de errores)", "ok" if SENTRY_DSN else "off",
         "Puesto · los fallos también llegan a Sentry" if SENTRY_DSN else "Sin poner: los fallos solo se ven aquí"),
        ("APIs oficiales de redes", "ok" if len(apis_ok) == 4 else "warn" if apis_ok else "off",
         ("Puestas: " + ", ".join(apis_ok)) if apis_ok else "Ninguna todavía (se leen con Apify y yt-dlp)"),
    ]
    D["services"] = [{"name": a, "state": b, "text": c} for a, b, c in svc]
    must = [x for x in D["services"] if x["name"] in ("Google (entrar)", "Whop principal", "App de Whop (creadores)", "Apify (vistas)")]
    n_ok = sum(1 for x in D["services"] if x["state"] == "ok")

    # --- tareas automáticas
    tick = _iso(get_setting("clock_tick", ""))
    jobs = []
    jobs.append({"name": "Reloj interno", "state": "off" if not tick else "ok" if now - tick < timedelta(minutes=25) else "bad",
                 "info": ("Lleva las tareas de abajo · mira cada 10 min" if tick and now - tick < timedelta(minutes=25)
                          else "¡Parado! Sin él no se hace nada automático" if tick else "Solo funciona en la web real"),
                 "last": tick, "next": "Cada 10 min"})
    for key, name, nxt in (("lectura", "Lectura diaria de vistas y reparto", f"Cada día {REFRESH_AT}"),
                           ("saldos", "Comprobar saldos de los creadores", "Tras la lectura"),
                           ("ytdlp", "Comprobación de yt-dlp", " · ".join(YTDLP_CHECK_AT)),
                           ("historial", "Resumir historial de vistas antiguo", "Una vez por semana")):
        j = job_info(key)
        jobs.append({"name": name, "state": ("ok" if j.get("ok") else "bad") if j else "off", "info": j.get("info") or "Aún no se ha hecho",
                     "last": _iso(j.get("at")), "secs": j.get("secs"), "next": nxt})
    jc = job_info("copia")
    jobs.append({"name": "Copia de seguridad (Bunny Storage)",
                 "state": ("ok" if jc.get("ok") else "bad") if jc and storage_ready() else "off",
                 "info": (jc.get("info") or "Aún no se ha hecho") if storage_ready() else "Apagado: faltan BUNNY_STORAGE_ZONE y BUNNY_STORAGE_KEY",
                 "last": _iso(jc.get("at")), "secs": jc.get("secs"), "next": f"Cada día {BACKUP_AT}" if storage_ready() else "—"})
    jobs.append({"name": "Copias de vídeo (Bunny)", "state": "ok" if bunny_ready() else "off",
                 "info": "Copia los clips que llegan a revisión" if bunny_ready() else "Apagado: falta la clave", "last": None,
                 "next": "Cada 10 min" if bunny_ready() else "—"})
    D["jobs"] = jobs

    # --- seguridad
    admins = User.query.filter(User.is_admin.is_(True)).order_by(User.admin_seen_at.desc().nullslast()).all()
    D["security"] = {"tot": tot, "admins": [{"u": u, "role": ADMIN_ROLES.get(u.admin_role or "owner", "Dueño"), "tech": is_tech(u)}
                                            for u in admins],
                     "mods": db.session.query(db.func.count(db.distinct(CampaignMod.user_id))).scalar() or 0,
                     "seen": [{"u": u, "at": u.admin_seen_at, "dev": _ua_label(u.admin_seen_ua)} for u in admins if u.admin_seen_at][:6],
                     "admins_day": sum(1 for u in admins if u.admin_seen_at and u.admin_seen_at >= day_ago)}

    # --- lista para abrir
    li_ = legal_info()
    host = urlparse(BASE_URL or request.host_url).hostname or ""
    demo_users = User.query.filter(User.email.like(f"%{DEMO_DOMAIN}")).count()
    demo_subs = Submission.query.filter(db.or_(Submission.my_demo.is_(True),
                                               Submission.user.has(User.email.like(f"%{DEMO_DOMAIN}")))).count()
    last_read = _iso(get_setting("last_refresh_at", ""))
    chk = [
        ("ok" if (BASE_URL or "").startswith("https://") and not host.endswith(("railway.app", "localhost")) else "bad",
         "Dominio propio con HTTPS", host or "Sin BASE_URL", "", ""),
        ("ok" if GOOGLE_CLIENT_ID else "bad", "Entrar con Google", "Puesto" if GOOGLE_CLIENT_ID else "Falta GOOGLE_CLIENT_ID en Railway", "", ""),
        ("ok" if TECH_EMAILS else "bad", "Panel técnico solo para ti",
         "TECH_EMAILS puesto" if TECH_EMAILS else "Ahora lo ven todos los dueños: pon tu correo en TECH_EMAILS (Railway)", "", ""),
        ("ok" if viewcounter.APIFY_TOKEN and last_read and now - last_read < timedelta(hours=30) else "bad",
         "Clave de Apify y lectura diaria",
         (f"Última lectura: {_when(last_read)}" if last_read else "Aún no se ha leído ninguna vez") if viewcounter.APIFY_TOKEN
         else "Falta APIFY_TOKEN en Railway", "", ""),
        ("ok" if WHOP_API_KEY and whop_company_id() else "bad", "Whop principal conectado",
         "Puesto" if WHOP_API_KEY else "Falta WHOP_API_KEY", "", ""),
        ("ok" if WHOP_APP_API_KEY and WHOP_APP_ID and WHOP_APP_ID != TEST_WHOP_APP else "bad",
         "App de Whop en la cuenta de ClipProfit",
         "Puesta" if WHOP_APP_API_KEY and WHOP_APP_ID != TEST_WHOP_APP else
         "Ahora es la app de prueba, en una cuenta que no es vuestra" if WHOP_APP_ID == TEST_WHOP_APP else "Falta la app", "", ""),
        ("ok" if li_["owner"] and li_["nif"] and li_["address"] else "bad", "Datos legales del titular",
         "Puestos" if li_["owner"] and li_["nif"] and li_["address"] else "Faltan nombre, NIF y domicilio de Felipe",
         "Arreglar", "#datos"),
        ("ok" if not demo_users and not demo_subs else "bad", "Datos de demostración borrados",
         "No queda nada de prueba" if not demo_users and not demo_subs else
         f"Quedan {demo_users} clippers y {demo_subs} clips de prueba", "Ir a Datos", "#datos"),
        ("ok" if not D["money"]["lonely"] else "warn", "Todas las campañas activas con creador",
         "Todas tienen quien las pague" if not D["money"]["lonely"] else
         f"{D['money']['lonely']} {'campaña activa' if D['money']['lonely'] == 1 else 'campañas activas'} sin creador",
         "Ver campañas", url_for("admin_campaigns")),
        (backup_check(),
         "Copia de seguridad cada noche (Bunny)",
         ("Última: " + _local(D["db"]["backup"]["when"]).strftime("%d/%m %H:%M") + " · " + _mb(D["db"]["backup"]["size"])
          if D["db"]["backup"].get("when") else "Conectada; la primera se hace esta noche")
         if storage_ready() else "Falta crear la Storage Zone en Bunny y poner sus 2 variables en Railway",
         "Hacer copia ahora" if storage_ready() else "", "backup_now" if storage_ready() else ""),
        ("ok" if mail_ready() else ("bad" if PASSWORD_LOGIN else "warn"), "Correo conectado (Resend)",
         "Puesto: confirmar el correo, recuperar contraseña y avisos" if mail_ready() else
         "Falta RESEND_API_KEY y MAIL_FROM en Railway, y verificar clipprofit.es en Resend", "", ""),
        ("ok" if SENTRY_DSN else "warn", "Aviso de errores por correo (Sentry)",
         "Puesto" if SENTRY_DSN else "Opcional: los fallos ya se apuntan aquí, pero no te llega aviso", "", ""),
        ("ok" if bunny_ready() else "warn", "Vídeos de verificación (Bunny)",
         "Puesto" if bunny_ready() else "Sin Bunny los vídeos de estadísticas no se guardan", "", ""),
    ]
    D["checklist"] = [{"state": a, "title": b, "text": c, "link": d, "url": e} for a, b, c, d, e in chk]
    D["check_ok"] = sum(1 for x in D["checklist"] if x["state"] == "ok")

    # --- semáforo y resumen de arriba
    web_s = "bad" if err_24 > 5 else "warn" if err_24 or (tot["avg_ms"] or 0) > 800 else "ok"
    lights = [
        {"name": "Web", "state": web_s, "big": "Funciona" if web_s == "ok" else f"{err_24} fallos" if err_24 else "Lenta",
         "text": (f"{tot['avg_ms']} ms de media" if tot["avg_ms"] is not None else "Sin visitas aún") + f" · {err_24} fallos en 24 h"},
        {"name": "Base de datos", "state": "ok" if D["db"]["ping"] < 200 else "warn", "big": D["db"]["size_txt"],
         "text": f"Responde en {D['db']['ping']:.0f} ms · {D['db']['n_tables']} tablas"},
        {"name": "Lectura de vistas", "state": rs, "big": _when(D["read"]["job_at"]) if D["read"]["job_at"] else "Sin leer",
         "text": rmsg},
        {"name": "Pagos", "state": ms, "big": "Bien" if ms == "ok" else "Revisar", "text": mmsg},
        {"name": "Servicios", "state": "bad" if any(x["state"] == "bad" for x in must) else "ok" if n_ok == len(D["services"]) else "warn",
         "big": f"{n_ok} de {len(D['services'])}",
         "text": " · ".join(t for t in (f"{sum(1 for x in D['services'] if x['state'] == 'bad')} con fallo" if any(x["state"] == "bad" for x in D["services"]) else "",
                                         f"{sum(1 for x in D['services'] if x['state'] in ('warn', 'off'))} sin poner o a medias"
                                         if any(x["state"] in ("warn", "off") for x in D["services"]) else "") if t) or "Todos bien"},
    ]
    D["lights"] = lights
    issues = [f"{x['name']}: {x['text']}" for x in lights if x["state"] in ("warn", "bad")]
    worst = "bad" if any(x["state"] == "bad" for x in lights) else "warn" if issues else "ok"
    D["hero"] = {"state": worst, "title": "Todo funciona" if not issues else
                 (f"Todo funciona, con {len(issues)} {'cosa' if len(issues) == 1 else 'cosas'} que mirar" if worst == "warn"
                  else f"Hay {len(issues)} {'problema' if len(issues) == 1 else 'problemas'} que arreglar"),
                 "text": " · ".join(issues) or "Nada que mirar ahora mismo."}
    return D


@app.route("/admin/salud/copias", methods=["POST"])
@admin_required
def admin_backups_ok():
    """Mustapha confirma que activó las copias diarias en Railway (la web no puede comprobarlo)."""
    if not is_tech():
        abort(403)
    set_setting("backups_ok", _local().strftime("%d/%m/%Y"))
    audit("copias_railway_confirmadas", "")
    db.session.commit()
    flash("Apuntado: copias diarias activadas en Railway.", "ok")
    return redirect(url_for("admin_settings", _anchor="salud"))


@app.route("/admin/ajustes")
@admin_required
def admin_settings(app_test=None):
    whop = {"key": bool(WHOP_API_KEY), "company": whop_company_id() if WHOP_API_KEY else "",
            "error": _whop_company.get("error", ""), "from_env": bool(WHOP_COMPANY_ID)}
    base = BASE_URL or request.host_url.rstrip("/")
    apis = [{"key": p, "label": official.LABELS[p], "ok": official.configured(p), "env": official.ENV_NEEDED[p],
             "scopes": official.SCOPES[p], "redirect": base + url_for("api_callback", platform=p),
             "n": ApiToken.query.filter_by(platform=p, status="ok").count(),
             "bad": ApiToken.query.filter_by(platform=p).filter(ApiToken.status != "ok").count()} for p in ("tiktok", "instagram", "youtube", "facebook")]
    api_info = {"mode": api_mode(), "modes": API_MODES, "apis": apis, "deletion": base + url_for("data_deletion_page"),
                "meta_deletion": base + url_for("meta_data_deletion"), "privacy": base + url_for("privacy"), "terms": base + url_for("terms")}
    host = urlparse(base).hostname or ""
    api_info.update(base=base, host=host, https=base.startswith("https://"), own_domain=not host.endswith(("railway.app", "localhost")) and host != "127.0.0.1",
                    support_email=SUPPORT_EMAIL, verify=site_verify())
    return render_template("admin/settings.html", whop=whop, last_refresh=get_setting("last_refresh", "Nunca"), allow_wipe=ALLOW_WIPE,
                           api_info=api_info, bunny_info=bunny_info(), search=search_info(), bunny_report=bunny_report(),
                           alerts=tech_alerts(), creators_on=creators_on(), tech_set=bool(TECH_EMAILS),
                           has_token=bool(viewcounter.APIFY_TOKEN), has_app_key=bool(WHOP_APP_API_KEY), app_test=app_test,
                           demo_n=User.query.filter(User.email.like(f"%{DEMO_DOMAIN}")).count(),
                           my_demo_n=Submission.query.filter_by(user_id=current_user.id, my_demo=True).count(),
                           tu=test_users_summary(),
                           notes=TechNote.query.order_by(TechNote.done, TechNote.created_at.desc()).limit(300).all(),
                           H=health_data())


@app.route("/admin/notas", methods=["POST"])
@admin_required
def admin_notes():
    if not admin_can("admin_notes"):
        abort(403)
    act, nid = request.form.get("act", ""), request.form.get("nid", type=int) or 0
    if act == "nueva":
        text = (request.form.get("text") or "").strip()[:5000]
        if text:
            db.session.add(TechNote(text=text))
    elif act in ("hecha", "pendiente", "borrar", "editar"):
        n = db.session.get(TechNote, nid) or abort(404)
        if act == "borrar":
            db.session.delete(n)
        elif act == "editar":
            n.text = (request.form.get("text") or "").strip()[:5000] or n.text
        else:
            n.done = act == "hecha"
    db.session.commit()
    return redirect(url_for("admin_settings", _anchor="notas"))


@app.route("/admin/busqueda/<accion>", methods=["POST"])
@admin_required
def admin_search(accion):
    if not is_tech():
        abort(403)
    if accion == "motor":
        v = request.form.get("engine", "")
        if v not in SEARCH_ENGINES:
            abort(400)
        set_setting("search_engine", v)
        audit("motor_busqueda", v)
        db.session.commit()
        flash(f"Búsqueda de clips: {SEARCH_ENGINES[v]}.", "ok")
    elif accion == "probar":
        platform = request.form.get("platform", "tiktok")
        handle = viewcounter.clean_handle(request.form.get("handle", ""))
        if platform not in viewcounter.YTDLP_PLATFORMS + ("instagram",) or not handle:
            flash("Pon una cuenta de TikTok, YouTube o Instagram.", "error")
        elif not viewcounter.ytdlp_available():
            flash("yt-dlp no está instalado en el servidor.", "error")
        else:
            t0 = time.time()
            try:
                items = viewcounter.ytdlp_profile_videos(platform, handle)
                session["ytdlp_test"] = {"handle": handle, "platform": platform, "secs": round(time.time() - t0, 1),
                                         "items": [[it["url"], (it["posted_at"].strftime("%d/%m/%Y %H:%M") if it.get("posted_at") else "—")
                                                    + (f" · {it['yt_views']:,} vistas".replace(",", ".") if it.get("yt_views") is not None else " · sin cifras")]
                                                   for it in items]}
                flash(f"yt-dlp encontró {len(items)} vídeos de @{handle} en {round(time.time() - t0, 1)} s.", "ok")
            except Exception as e:  # noqa: BLE001
                session["ytdlp_test"] = {"handle": handle, "platform": platform, "error": str(e)[:400]}
                flash(f"yt-dlp falló con @{handle}.", "error")
    elif accion == "ahorro":
        set_setting("ytdlp_stats", "on" if request.form.get("ytdlp_stats") else "off")
        try:
            top = max(0, int(request.form.get("apify_daily_max", "") or APIFY_DAILY_MAX))
        except ValueError:
            top = APIFY_DAILY_MAX
        set_setting("apify_daily_max", str(top))
        audit("ahorro_apify", f"cifras de yt-dlp: {'sí' if request.form.get('ytdlp_stats') else 'no'} · tope diario: {top}")
        db.session.commit()
        flash("Guardado.", "ok")
    elif accion == "comprobar":
        res = ytdlp_check()
        flash(f"Comprobación hecha: {res['ok']} de {res['accounts']} cuentas leídas · {len(res['issues'])} cosas raras.", "ok")
    elif accion == "borrar_registro":
        set_setting("apify_log", "[]")
        db.session.commit()
        flash("Registro de Apify vaciado.", "ok")
    else:
        abort(404)
    return redirect(url_for("admin_settings", _anchor="lectura"))


@app.route("/admin/bunny/<accion>", methods=["POST"])
@admin_required
def admin_bunny(accion):
    if accion == "reintentar":                         # desde Revisar: un clip
        sub = db.get_or_404(Submission, request.form.get("sid", type=int))
        if sub.video_guid:
            flash("Ese clip ya tiene copia.", "ok")
        else:
            sub.video_tries, sub.video_error = 0, ""
            db.session.commit()
            ok, msg = bunny_test()
            if not ok:
                flash(msg, "error")
            else:
                _, rep_ = copy_videos_now(ids=[sub.id])
                flash(rep_[0][1].capitalize() if rep_ else "Este clip aún no está en revisión: no se copia.",
                      "ok" if sub.video_guid else "error")
        return redirect(request.referrer or url_for("admin_clips"))
    if not is_tech():
        abort(403)
    ok, msg = bunny_test()
    if accion == "probar":
        flash(msg, "ok" if ok else "error")
    elif accion == "copiar":
        if not ok:
            flash(msg, "error")
        else:
            Submission.query.filter(Submission.video_guid == "", Submission.status == "pendiente").update(
                {"video_tries": 0}, synchronize_session=False)
            db.session.commit()
            if not copy_candidates():
                flash("No hay clips en revisión sin copia (los que están en «Contando» no se copian).", "ok")
            elif (bunny_report() or {}).get("running"):
                flash("Ya hay una copia en marcha. Recarga en un minuto para ver el resultado.", "info")
            else:
                copy_videos_background(limit=15)
                flash("Copiando en segundo plano: recarga esta página en un minuto para ver el resultado.", "ok")
    else:
        abort(404)
    return redirect(url_for("admin_settings", _anchor="videos"))


@app.route("/admin/ajustes/apis", methods=["POST"])
@admin_required
def admin_api_mode():
    if not is_tech():
        abort(403)
    mode = request.form.get("mode", "off")
    if mode not in API_MODES:
        abort(400)
    set_setting("api_mode", mode)
    audit("apis_oficiales", mode)
    db.session.commit()
    flash(f"APIs oficiales: {API_MODES[mode]}.", "ok")
    return redirect(url_for("admin_settings", _anchor="apis"))


# ---- Verificación del dominio que piden Google, Meta y TikTok al solicitar sus APIs
_VERIFY_CODE = re.compile(r"[A-Za-z0-9_-]{6,120}")


def _verify_code(raw):
    """Acepta el código suelto o la etiqueta <meta …> entera pegada tal cual; devuelve solo el código."""
    raw = (raw or "").strip()
    m = re.search(r'content\s*=\s*["\']([^"\']+)["\']', raw)
    code = (m.group(1) if m else raw).strip()
    return code if _VERIFY_CODE.fullmatch(code) else ""


def site_verify():
    """Códigos de verificación del dominio guardados en el Panel técnico (se leen una vez cada pocos minutos)."""
    def read():
        try:
            return json.loads(get_setting("site_verify", "") or "{}")
        except ValueError:
            return {}
    return ttl_cache("site_verify", 60, read)


@app.route("/admin/ajustes/verificacion", methods=["POST"])
@admin_required
def admin_site_verify():
    if not is_tech():
        abort(403)
    f = request.form
    tk_name = re.sub(r"\.txt$", "", (f.get("tiktok_file") or "").strip().rsplit("/", 1)[-1])
    tk_name = tk_name if re.fullmatch(r"tiktok[A-Za-z0-9_-]{4,80}", tk_name) else ""
    tk_body = (f.get("tiktok_body") or "").strip()[:300]
    if not re.fullmatch(r"[A-Za-z0-9_=. -]{6,300}", tk_body):
        tk_body = ""
    g_file = re.sub(r"\.html$", "", (f.get("google_file") or "").strip().rsplit("/", 1)[-1])
    data = {"google": _verify_code(f.get("google")), "meta": _verify_code(f.get("meta")),
            "google_file": g_file if re.fullmatch(r"google[a-f0-9]{8,40}", g_file) else "",
            "tiktok_file": tk_name if tk_body else "", "tiktok_body": tk_body if tk_name else ""}
    set_setting("site_verify", json.dumps(data))
    _ttl.pop("site_verify", None)
    audit("verificacion_dominio", ", ".join(k for k, v in data.items() if v) or "vacío")
    db.session.commit()
    bad = [n for n, k in (("Google", "google"), ("Meta", "meta")) if (f.get(k) or "").strip() and not data[k]]
    if (f.get("tiktok_file") or f.get("tiktok_body") or "").strip() and not data["tiktok_file"]:
        bad.append("TikTok")
    if bad:
        flash("No he podido leer el código de " + ", ".join(bad) + ". Pégalo tal cual te lo da la red.", "error")
    else:
        flash("Verificación del dominio guardada. Ya puedes pulsar «Verificar» en la red.", "ok")
    return redirect(url_for("admin_settings", _anchor="apis"))


@app.route("/tiktok<code>.txt")
def tiktok_verify_file(code):
    v = site_verify()
    if not v.get("tiktok_file") or v["tiktok_file"] != "tiktok" + code:
        abort(404)
    return Response(v.get("tiktok_body", ""), mimetype="text/plain")


@app.route("/google<code>.html")
def google_verify_file(code):
    v = site_verify()
    if not v.get("google_file") or v["google_file"] != "google" + code:
        abort(404)
    return Response(f"google-site-verification: google{code}.html", mimetype="text/html")


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
    db.session.flush()
    all_ids = [c.id for c in Campaign.query.filter(Campaign.status.in_(("activa", "pausada", DIST_STATUS)))]
    real = (Submission.query.filter(Submission.campaign_id.in_(all_ids or [0]), Submission.status != "rechazado")
            .filter(~Submission.id.in_(db.session.query(AudienceStat.submission_id).filter(AudienceStat.submission_id.isnot(None))))
            .all())
    demo_audience(real, r)                 # audiencia de demostración para todos los clips de las campañas (también los reales)
    load_demo_hub(camps, [u for u, _h, _s in users], r, now)
    db.session.commit()
    total = len(plan)
    return f"Demo cargada: {len(users)} clippers y {total} clips de los últimos {days} días."


DEMO_COUNTRY_MIX = [("ES", 42), ("MX", 18), ("AR", 10), ("CO", 8), ("CL", 6), ("US", 5), ("PE", 4), ("VE", 3)]
DEMO_AGES = [("13-17", 14), ("18-24", 38), ("25-34", 28), ("35-44", 12), ("45-54", 5), ("55-64", 2), ("65-", 1)]


def demo_audience(subs, r):
    """Audiencia inventada para los clips de la demo (así se ve cómo queda el apartado con datos)."""
    def mix(base, spread):
        vals = [(k, max(0.5, w * r.uniform(1 - spread, 1 + spread))) for k, w in base]
        tot = sum(v for _k, v in vals)
        return {k: round(v * 100 / tot, 1) for k, v in sorted(vals, key=lambda kv: -kv[1])}
    for s_ in subs:
        fem = r.uniform(38, 64)
        AudienceStat.query.filter_by(submission_id=s_.id, demo=True).delete()
        db.session.add(AudienceStat(platform=s_.platform, handle=s_.author or "", submission_id=s_.id, demo=True,
                                    data=json.dumps({"country": mix(DEMO_COUNTRY_MIX, .45), "age": mix(DEMO_AGES, .35),
                                                     "gender": {"female": round(fem, 1), "male": round(100 - fem, 1)}})))


DEMO_POSTS = {
    "Anuncios": [("📌 ¡Bienvenidos a la campaña! Leed las normas antes de publicar y usad el sonido oficial en TikTok. "
                  "Cualquier duda, por el canal de soporte.", True),
                 ("🚀 Hemos subido el pago por 1.000 vistas en Instagram esta semana. ¡A por ello!", False),
                 ("⏰ Recordatorio: los clips se detectan solos cada día a las 03:00. No hace falta subirlos a mano.", False)],
    "Ideas virales": [("💡 Empezad con la frase más fuerte en los primeros 2 segundos: el gancho es lo que más retiene.", True),
                      ("🔥 Los clips de 15-25 s con subtítulos grandes están funcionando el doble que los largos.", False),
                      ("🎯 Probad el formato «reacción + momento clave»: los 3 vídeos más vistos de la semana lo usan.", False)],
}
DEMO_COMMENTS = ["¡Genial, gracias!", "¿Vale también para YouTube Shorts?", "Lo pruebo hoy mismo 🔥", "Top, esto funciona",
                 "¿Cuántos clips al día recomendáis?", "Gracias por el aviso 🙌"]
DEMO_RULES = ("1. Publica solo en las cuentas verificadas de la campaña.\n2. Usa el sonido oficial y menciona al creador.\n"
              "3. Nada de contenido resubido de otros clippers ni vistas compradas.\n4. Mínimo 10 segundos, formato vertical.\n"
              "5. Los hashtags obligatorios van en la descripción.")
DEMO_MATS = ("folder | Vídeos en bruto | https://drive.google.com/drive/folders/demo | Todo el material sin editar para recortar\n"
             "music | Sonido oficial | https://www.tiktok.com/music/demo | Úsalo en todos los clips de TikTok\n"
             "doc | Guía de estilo | https://docs.google.com/document/d/demo | Subtítulos, colores y tipografía\n"
             "play | Ejemplos que funcionan | https://www.tiktok.com/@demo | Los clips con más vistas de la campaña")


def load_demo_hub(camps, users, r, now):
    """Contenido de demostración en el hub de cada campaña: publicaciones con me gusta y comentarios, llamadas,
    recursos y normas (solo donde estaban vacíos). Se quita con remove_demo()."""
    restore = json.loads(get_setting("demo_hub_restore", "") or "{}")
    for c in camps:
        sections = campaign_channels(c)
        if not c.materials:
            restore.setdefault(str(c.id), {})["materials"] = ""
            c.materials = DEMO_MATS
        if not (c.rules or "").strip() and not c.rules_text:
            restore.setdefault(str(c.id), {})["rules"] = ""
            c.rules = DEMO_RULES
        for ch in [x for s in sections for x in s.live]:
            if ch.kind == "calls":
                madrid = ZoneInfo(APP_TZ)
                nxt = (datetime.now(madrid) + timedelta(days=2)).replace(hour=19, minute=0, second=0, microsecond=0)
                db.session.add(Call(campaign_id=c.id, title="Directo: cómo llegar al mínimo de vistas", demo=True,
                                    starts_at=nxt.astimezone(timezone.utc).replace(tzinfo=None), minutes=60,
                                    link="https://meet.google.com/demo-llamada", notes="Repasamos los clips que mejor funcionan y resolvemos dudas."))
                db.session.add(Call(campaign_id=c.id, title="Bienvenida a la campaña", demo=True, minutes=45,
                                    starts_at=now - timedelta(days=r.randint(4, 9), hours=2),
                                    recording_url="https://www.youtube.com/watch?v=demo"))
            elif ch.kind in ("posts", "rules", "resources"):
                texts = DEMO_POSTS.get(ch.name) or ([("👋 Pasad por aquí para estar al día de todo lo de " + c.song + ".", False)]
                                                    if ch.kind == "posts" else [])
                for k, (body, pin) in enumerate(texts):
                    p_ = Post(channel_id=ch.id, author_id=None, body=body, pinned=pin, demo=True,
                              created_at=now - timedelta(days=k * 3 + r.randint(0, 2), hours=r.randint(0, 20)))
                    db.session.add(p_)
                    db.session.flush()
                    for u in r.sample(users, k=min(len(users), r.randint(3, 12))):
                        db.session.add(PostLike(post_id=p_.id, user_id=u.id))
                    for u in r.sample(users, k=min(len(users), r.randint(0, 3))):
                        db.session.add(PostComment(post_id=p_.id, user_id=u.id, body=r.choice(DEMO_COMMENTS),
                                                   created_at=p_.created_at + timedelta(hours=r.randint(1, 30))))
    for acc in CampaignAccount.query.join(User, User.id == CampaignAccount.user_id).filter(User.email.like(f"%{DEMO_DOMAIN}")):
        if not acc.avatar_url and acc.user.avatar_url:
            acc.avatar_url = acc.user.avatar_url      # foto en «Cuentas TOP»
    set_setting("demo_hub_restore", json.dumps(restore))


def remove_demo_hub():
    pids = [p.id for p in Post.query.filter_by(demo=True)]
    if pids:
        PostLike.query.filter(PostLike.post_id.in_(pids)).delete(synchronize_session=False)
        PostComment.query.filter(PostComment.post_id.in_(pids)).delete(synchronize_session=False)
        Post.query.filter(Post.id.in_(pids)).delete(synchronize_session=False)
    Call.query.filter_by(demo=True).delete(synchronize_session=False)
    AudienceStat.query.filter_by(demo=True).delete(synchronize_session=False)
    for cid, vals in json.loads(get_setting("demo_hub_restore", "") or "{}").items():
        c = db.session.get(Campaign, int(cid))
        if c:
            for k, v in vals.items():
                setattr(c, k, v)
    set_setting("demo_hub_restore", "")


def remove_demo():
    remove_demo_hub()
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
    r = rnd.Random(seed)                  # la misma semilla para todos: cada admin ve la misma demo (Mustapha, 24 nov)
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
    demo_audience([s_ for s_, _ in plan], r)
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
        AudienceStat.query.filter(AudienceStat.submission_id.in_(ids)).delete(synchronize_session=False)
        Submission.query.filter(Submission.id.in_(ids)).delete(synchronize_session=False)
    for model in (CampaignAccount, CampaignMember, Notice):
        model.query.filter_by(user_id=user.id, my_demo=True).delete(synchronize_session=False)
    db.session.commit()
    return len(ids)


def test_users():
    """Usuarios reales (no de demostración) que no son administradores: los que se han registrado para probar la web."""
    return (User.query.filter(User.is_admin.is_(False), ~User.email.like(f"%{DEMO_DOMAIN}"))
            .order_by(User.created_at.desc()).all())


def test_users_summary(limit=50):
    """Para el Panel técnico: cuántos usuarios reales hay y los últimos, con sus clips contados en la base de datos.
    (Antes se cargaban todos y sus clips uno a uno: con 10.000 usuarios el Panel técnico tardaba 20 s.)"""
    q = User.query.filter(User.is_admin.is_(False), ~User.email.like(f"%{DEMO_DOMAIN}"))
    n_clips = (db.session.query(Submission.user_id, db.func.count(Submission.id).label("n"))
               .group_by(Submission.user_id).subquery())
    rows = (q.outerjoin(n_clips, n_clips.c.user_id == User.id).add_columns(db.func.coalesce(n_clips.c.n, 0))
            .order_by(User.created_at.desc()).limit(limit).all())
    return {"n": q.count(), "rows": [(u, n) for u, n in rows if not role_for(u.email)]}


@app.route("/admin/ajustes/borrar-usuarios", methods=["POST"])
@admin_required
def admin_delete_test_users():
    """Borra de una vez todos los usuarios reales de prueba con todo lo suyo. Los de demostración y los admins se quedan."""
    if (current_user.admin_role or "owner") != "owner" or not is_tech():
        abort(403)
    if not ALLOW_WIPE:                    # con clippers reales esto borraría a todos y lo que se les debe: igual que «Borrar datos»
        flash("Borrar usuarios está desactivado en la web real. Para activarlo un momento, añade ALLOW_WIPE=1 en Railway.", "error")
        return redirect(url_for("admin_settings", _anchor="datos"))
    if (request.form.get("confirm") or "").strip().upper() != "ELIMINAR":
        flash("Para borrar los usuarios, escribe ELIMINAR en la casilla.", "error")
        return redirect(url_for("admin_settings", _anchor="datos"))
    users = [u for u in test_users() if not role_for(u.email)]      # por si alguien es admin y aún no ha vuelto a entrar
    for u in users:
        delete_user_account(u)
    audit("usuarios_prueba_borrados", f"{len(users)} usuarios")
    db.session.commit()
    flash(f"Se han borrado {len(users)} usuarios con todos sus datos. Los de demostración y los administradores siguen igual.", "ok")
    return redirect(url_for("admin_settings", _anchor="datos"))


def has_my_demo(user):
    return bool(Submission.query.filter_by(user_id=user.id, my_demo=True).first())


def give_my_demo(user):
    """Si la demo de los administradores está puesta, el admin que entra por primera vez también la recibe."""
    if user.is_admin and get_setting("my_demo_all") == "1" and not has_my_demo(user):
        try:
            load_my_demo(user)
        except Exception:  # noqa: BLE001
            db.session.rollback()
            log.warning("No se pudo cargar la demo del admin %s", user.id, exc_info=True)


@app.route("/admin/ajustes/mi-demo", methods=["POST"])
@admin_required
def admin_my_demo():
    """Demo como clipper para TODOS los administradores a la vez, con los mismos datos (Mustapha, 24 nov)."""
    admins = User.query.filter_by(is_admin=True).order_by(User.id).all()
    if request.form.get("accion") == "quitar":
        n = sum(remove_my_demo(u) for u in admins)
        set_setting("my_demo_all", "")
        audit("mi_demo_quitada", f"{n} clips · {len(admins)} admins")
        db.session.commit()
        flash(f"Demo como clipper quitada a todos los administradores ({n} clips). La demo de la plataforma y lo real siguen igual.", "ok")
    else:
        todo = [u for u in admins if not has_my_demo(u)]
        msg = ""
        for u in todo:
            msg = load_my_demo(u)
        set_setting("my_demo_all", "1")
        audit("mi_demo_cargada", f"{len(todo)} admins · {msg}")
        db.session.commit()
        if todo and msg.startswith("No hay"):
            flash(msg, "error")
        else:
            flash(f"Demo como clipper cargada a {len(admins)} {'administrador' if len(admins) == 1 else 'administradores'}, "
                  "con los mismos datos. Los que entren por primera vez también la tendrán.", "ok")
    return redirect(url_for("admin_settings", _anchor="datos"))


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
    return redirect(url_for("admin_settings", _anchor="datos"))


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
    if creators_on():
        try:
            short = json.loads(get_setting("creators_short_list", "[]") or "[]")
        except ValueError:
            short = []
        if short:
            out.append(("error", "A algún creador no le llega el saldo de Whop", " · ".join(short[:5]) +
                        ". Sus retiros se quedan esperando hasta que meta saldo."))
    lonely = Campaign.query.filter(Campaign.status == "activa", Campaign.creator_id.is_(None)).count()
    if lonely:
        out.append(("aviso", f"{lonely} {'campaña activa' if lonely == 1 else 'campañas activas'} sin creador",
                    "Se pagarían desde la cuenta principal. En cada campaña, elige en «Paga desde el Whop de» qué creador paga."))
    try:
        day = datetime.utcnow() - timedelta(hours=24)
        n_err = (ErrorLog.query.filter(db.func.coalesce(ErrorLog.last_at, ErrorLog.created_at) >= day,
                                       ErrorLog.kind.in_(("servidor", "tarea", "navegador", "google"))).count())
        if n_err:
            out.append(("error", f"Fallos en las últimas 24 h: {n_err}",
                        "Mira qué pasó en «Ver los últimos», en «Buscar un error por su código», aquí arriba."))
    except Exception:
        db.session.rollback()
    slow = slow_pages()
    if slow:
        ep, n, worst = slow[0]
        out.append(("aviso", f"Páginas lentas en las últimas 24 h: {sum(r[1] for r in slow)}",
                    f"La que más: «{ep}», {n} {'vez' if n == 1 else 'veces'}, hasta {worst / 1000:.1f} s".replace(".", ",")
                    + (f". También: {', '.join(r[0] for r in slow[1:4])}." if len(slow) > 1 else ".")))
    if PRODUCTION and not SENTRY_DSN:
        out.append(("aviso", "Sin aviso automático de errores", "Crea una cuenta gratuita en Sentry y pon su dirección en la variable SENTRY_DSN de Railway: cada fallo te llegará con página, usuario y línea."))
    if PRODUCTION and not mail_ready():
        out.append(("aviso", "Sin correo: no se puede recuperar la contraseña", "Pon RESEND_API_KEY y MAIL_FROM en Railway (Railway no deja enviar correo normal en el plan Hobby) y aparecerá «He olvidado mi contraseña» en el acceso."))
    if not viewcounter.APIFY_TOKEN:
        out.append(("error", "Falta la clave de Apify", "Sin APIFY_TOKEN no se leen las vistas de los clips."))
    li_ = legal_info()
    if not (li_["owner"] and li_["nif"] and li_["address"]):
        out.append(("aviso", "Faltan los datos del aviso legal", "En España la web tiene que decir quién es el titular (nombre, NIF y domicilio). Ponlos en Panel técnico → Datos."))
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
    try:
        dfr = json.loads(get_setting("apify_deferred", "") or "{}")
    except ValueError:
        dfr = {}
    if dfr.get("n") and dfr.get("day") == _local().strftime("%Y-%m-%d"):
        out.append(("aviso", "Tope diario de Apify alcanzado",
                    f"Hoy quedaron {dfr['n']} clips sin leer (se leen mañana, los más atrasados primero). "
                    "Si pasa a menudo, sube el tope en Panel técnico → Búsqueda de clips, o conecta las APIs oficiales."))
    run = _refresh_run()
    if run["tries"] >= REFRESH_TRIES and not run.get("done"):
        out.append(("error", "La lectura diaria de hoy no ha terminado",
                    f"Se intentó {REFRESH_TRIES} veces y no se completó: hoy no se han leído vistas ni repartido dinero. "
                    "Pulsa «Actualizar vistas ahora» y, si vuelve a fallar, mira los logs de Railway."))
    if search_engine() == "ytdlp":
        try:
            st = json.loads(get_setting("ytdlp_stats", "{}") or "{}")
        except ValueError:
            st = {}
        if st.get("fail") and st.get("fail", 0) >= max(1, st.get("ok", 0)):
            out.append(("aviso", "yt-dlp está fallando hoy",
                        f"{st['fail']} cuentas no se pudieron leer con yt-dlp (se buscaron con Apify). "
                        f"Puede que TikTok/YouTube estén bloqueando el servidor. Último error: {(st.get('errors') or ['—'])[0]}"))
    bad = Submission.query.filter(Submission.views_error != "", Submission.status != "rechazado").count()
    if bad:
        out.append(("aviso", f"{bad} clips sin poder leer sus vistas", "Puede ser un vídeo borrado o privado, o un fallo de Apify. Míralos en «Clips por revisar»."))
    failed = Payout.query.filter(Payout.error != "", Payout.created_at >= datetime.utcnow() - timedelta(days=7)).count()
    if failed:
        out.append(("aviso", f"{failed} pagos de Whop fallidos esta semana", "El motivo está en Retiros → «Últimos pagos que Whop no pudo enviar»."))
    if not VAPID_PUBLIC_KEY and CLIPPER_PUSH:
        out.append(("aviso", "Avisos al móvil sin activar", "Falta la clave de avisos en Railway."))
    if not bunny_ready():
        out.append(("aviso", "Vídeos sin activar (Bunny)", "Faltan BUNNY_LIBRARY_ID y BUNNY_API_KEY: la formación y la vista previa de los clips usan el reproductor de cada red."))
    if not TECH_EMAILS:
        out.append(("aviso", "Panel técnico abierto a todos los dueños", "Pon tu correo en la variable TECH_EMAILS de Railway para que solo lo veas tú."))
    return out


def tech_alert_count():
    try:                                             # 2 min guardado: tech_alerts pregunta a Whop su saldo y, si Whop va lento,
        return (ttl_cache("tech_alert_count", 120,   # cada página del panel esperaba (auditoría 24 nov)
                          lambda: sum(1 for lvl, _, _ in tech_alerts() if lvl == "error")) if is_tech() else 0)
    except Exception:  # noqa: BLE001
        return 0


def _whop_state():
    from itsdangerous import URLSafeTimedSerializer
    return URLSafeTimedSerializer(app.secret_key, salt="whop-connect")


def whop_redirect_uri():
    return (BASE_URL or request.url_root.rstrip("/")) + "/auth/whop/callback"


@app.route("/cobros/whop/conectar")
@login_required
def whop_connect():
    """«Conectar con Whop»: el clipper entra en su Whop y autoriza; así no se puede equivocar de cuenta."""
    if not WHOP_CLIENT_ID:
        flash("Aún no se puede conectar con el botón: escribe tu usuario de Whop.", "error")
        return redirect(url_for("account", _anchor="cobro"))
    import base64
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    session["whop_pkce"] = verifier
    state = _whop_state().dumps({"u": current_user.id, "n": secrets.token_urlsafe(8)})
    params = {"response_type": "code", "client_id": WHOP_CLIENT_ID, "redirect_uri": whop_redirect_uri(),
              "scope": "openid profile", "state": state, "nonce": secrets.token_urlsafe(12),
              "code_challenge": challenge, "code_challenge_method": "S256"}
    return redirect("https://api.whop.com/oauth/authorize?" + urlencode(params))


@app.route("/auth/whop/callback")
@login_required
def whop_callback():
    from itsdangerous import BadSignature
    back = redirect(url_for("account", _anchor="cobro"))
    if request.args.get("error"):
        _flask_flash("Has cancelado la conexión con Whop.", "error")
        return back
    try:
        st = _whop_state().loads(request.args.get("state", ""), max_age=900)
    except BadSignature:
        st = None
    verifier = session.pop("whop_pkce", "")
    if not st or st.get("u") != current_user.id or not verifier or not request.args.get("code"):
        code = note_error("no se pudo comprobar la vuelta de Whop (conectar cuenta)", kind="aviso",
                          detail=f"state={'ok' if st else 'mal'} verifier={'sí' if verifier else 'no'}")
        _flask_flash(f"No se pudo conectar con Whop. Inténtalo otra vez. (Código {code})", "error")
        return back
    body = {"grant_type": "authorization_code", "code": request.args["code"], "redirect_uri": whop_redirect_uri(),
            "client_id": WHOP_CLIENT_ID, "code_verifier": verifier}
    if WHOP_CLIENT_SECRET:
        body["client_secret"] = WHOP_CLIENT_SECRET
    try:
        tok = requests.post("https://api.whop.com/oauth/token", json=body, timeout=15).json()
        info = requests.get("https://api.whop.com/oauth/userinfo", timeout=15,
                            headers={"Authorization": f"Bearer {tok.get('access_token', '')}"}).json()
    except (requests.RequestException, ValueError):
        import traceback
        code = note_error("Whop no respondió al conectar la cuenta", traceback.format_exc(), kind="aviso")
        _flask_flash(f"Whop no respondió. Inténtalo otra vez. (Código {code})", "error")
        return back
    uid, uname = str(info.get("sub") or ""), str(info.get("preferred_username") or "").lstrip("@")
    if not uid.startswith("user_"):
        code = note_error("Whop no devolvió la cuenta al conectar", f"token={ {k: tok.get(k) for k in ('error', 'error_description')} }",
                          kind="aviso")
        _flask_flash(f"No se pudo conectar con Whop. Inténtalo otra vez. (Código {code})", "error")
        return back
    current_user.whop_user_id, current_user.whop_username = uid, (uname or uid)[:80]
    current_user.payout_method = WHOP_METHOD
    db.session.commit()
    audit("whop_conectado", f"@{current_user.whop_username} ({uid})")
    _flask_flash(f"Whop conectado: @{current_user.whop_username}. Cuando retires, el dinero te llegará al momento.", "ok")
    return back


@app.route("/admin/ajustes/probar-app-whop", methods=["POST"])
@admin_required
def admin_whop_app_test():
    """Prueba de la opción 2: pagar con la clave de la APP desde el saldo de la empresa del creador que la instaló."""
    if not WHOP_APP_API_KEY:
        flash("Falta WHOP_APP_API_KEY en las variables de Railway.", "error")
        return redirect(url_for("admin_settings", _anchor="pagos"))
    biz = (request.form.get("company") or "").strip()
    username = (request.form.get("username") or "").strip().lstrip("@")
    amount = parse_float(request.form.get("amount"), 0) or 0
    if not biz.startswith("biz_") or not re.fullmatch(r"[A-Za-z0-9_.-]{1,60}", username) or not (0 < amount <= 5):
        flash("Pon el código biz_ de la empresa, un usuario de Whop y una cantidad entre 0,01 y 5 $.", "error")
        return redirect(url_for("admin_settings", _anchor="pagos"))
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
        img = Image.open(file.stream, formats=IMG_FORMATS)
        img.verify()
        file.stream.seek(0)
        img = Image.open(file.stream, formats=IMG_FORMATS)
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
    if "apply_form_url" in request.form:
        fu = (request.form.get("apply_form_url") or "").strip()[:500]
        camp.apply_form_url = fu if fu.startswith("https://") else ""
    mat_text = request.form.get("materials", "")
    if "mat_url" in request.form:                      # editor de recursos: icono, nombre, enlace y descripción
        lines = []
        for ico, name, url, desc in zip(request.form.getlist("mat_icon"), request.form.getlist("mat_name"),
                                        request.form.getlist("mat_url"), request.form.getlist("mat_desc")):
            clean = lambda t: " ".join((t or "").replace("|", "/").split())  # noqa: E731
            url = clean(url)
            if not url.startswith(("https://", "http://")):
                continue
            ico = ico if ico in RESOURCE_ICONS else "link"
            lines.append(f"{ico} | {clean(name)[:80]} | {url[:400]} | {clean(desc)[:200]}".rstrip(" |"))
        mat_text = "\n".join(lines[:12])
    if "cover_url" in request.form:                    # (antes se podía pegar un enlace; ahora solo se sube la imagen)
        cover_url = request.form.get("cover_url", "").strip()[:500]
        camp.cover_url = cover_url if cover_url.startswith("https://") else ""
    mats = []
    for line in mat_text.splitlines()[:12]:
        if any(p.strip().startswith(("https://", "http://")) for p in line.split("|")) and len(line) <= 800:
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
    if "creator_id" in request.form:                   # «Paga desde el Whop de» (se perdió el 3 nov; vuelve el 24 nov)
        crid = request.form.get("creator_id", type=int)
        camp.creator_id = crid if crid and db.session.get(Creator, crid) else None
    status = request.form.get("status", "activa")
    status = status if status in ("activa", "pausada", "finalizada") else "activa"
    if REQUIRE_CREATOR and status == "activa" and not camp.creator_id and (not camp.id or camp.status != "activa"):
        return NO_CREATOR_MSG
    if status == "finalizada" and camp.id and camp.status != "finalizada":
        close_campaign(camp, current_user.id)          # al cerrarla, los clippers ya pueden retirar
    camp.status = status
    if not request.form.get("wizard"):                 # el formulario antiguo traía estas casillas
        camp.featured = bool(request.form.get("featured"))
        was_full, camp.full = camp.full, bool(request.form.get("full"))
        if was_full and not camp.full:
            camp.spots_opened_at = datetime.utcnow()
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
    act = request.form.get("act") if request.method == "POST" else ""
    if act == "tiles":                                 # imágenes de la portada pública: enlaces de vídeos
        known = {t.url for t in LandingTile.query.all()}
        links = [viewcounter.normalize_url(l.strip(), resolve=False) for l in (request.form.get("links") or "").split()
                 if l.strip().startswith("http")]
        bad = [l for l in links if not viewcounter.detect_platform(l)]
        new = [l for l in dict.fromkeys(links) if l not in known and l not in bad][:40]
        for l in new:
            db.session.add(LandingTile(url=l[:500]))
        db.session.commit()
        if new:
            threading.Thread(target=load_landing_tiles, daemon=True).start()
        flash((f"Añadidos {len(new)} vídeos: sus portadas tardan unos segundos en salir. Recarga en un momento."
               if new else "No había enlaces nuevos.") + (f" No se reconocen {len(bad)} enlaces." if bad else ""),
              "ok" if new else "error")
        return redirect(url_for("admin_featured", _anchor="portada"))
    if act in ("tile_del", "tile_retry"):
        t = db.session.get(LandingTile, request.form.get("tid", type=int) or 0) or abort(404)
        if act == "tile_del":
            db.session.delete(t)
        else:
            t.status, t.error = "cargando", ""
        db.session.commit()
        _ttl.pop("landing_wall", None)
        if act == "tile_retry":
            threading.Thread(target=load_landing_tiles, daemon=True).start()
        return redirect(url_for("admin_featured", _anchor="portada"))
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
        if mode not in ("auto", "campaign", "none", ""):
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
    camps = Campaign.query.filter(Campaign.status.notin_(("finalizada", DIST_STATUS))).order_by(Campaign.created_at.desc()).all()
    return render_template("admin/featured.html", hf=hf, camps=camps, wa_general=get_setting("whatsapp_general", ""),
                           tiles=LandingTile.query.order_by(LandingTile.id).all())


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
        if REQUIRE_CREATOR and not camp.creator_id:
            flash(NO_CREATOR_MSG, "error")
            return redirect(url_for("admin_campaign", cid=cid))
        if camp.exhausted:
            flash("No tiene presupuesto libre. Añade presupuesto primero.", "error")
            return redirect(url_for("admin_campaign", cid=cid))
        camp.status = "activa"
        flash("Campaña activa otra vez.", "ok")
    elif accion == "reabrir" and camp.status == "finalizada":
        # Seguir una campaña terminada (25 nov, Mustapha), esté pagada o no: lo ya ganado se queda tal cual y lo que
        # falte por pagar se paga cuando vuelva a terminar (Pagos solo resta lo ya enviado).
        if not perm_ok(cid, "dinero") and parse_float(request.form.get("amount"), 0):
            abort(403)
        if REQUIRE_CREATOR and not camp.creator_id:
            flash(NO_CREATOR_MSG, "error")
            return redirect(url_for("admin_campaign", cid=cid))
        extra = parse_float(request.form.get("amount"), 0) or 0
        if extra > 0:
            camp.budget = cents((camp.budget or 0) + extra)
        if camp.exhausted:
            flash(f"No le queda presupuesto ({money(camp.spent)} de {money(camp.budget)} repartidos). "
                  "Escribe cuánto añadir para reabrirla.", "error")
            return redirect(url_for("admin_campaign", cid=cid))
        camp.status, camp.ended_at, camp.low_alert_sent = "activa", None, False
        n = 0
        if request.form.get("avisar"):
            uids = {m.user_id for m in CampaignMember.query.filter_by(campaign_id=cid, status="aceptada")}
            for uid in uids:
                notify_user(uid, "mensaje", f"{camp.song} vuelve a estar abierta",
                            "Puedes seguir publicando. Lo que ya ganaste sigue guardado y se paga cuando termine.",
                            f"/campana/{cid}", camp_id=cid)
            n = len(uids)
        flash("Campaña abierta otra vez: vuelve a aceptar clips y se leen sus vistas cada noche."
              + (f" Añadidos {money(extra)} de presupuesto." if extra > 0 else "")
              + (f" Avisados {n} {'clipper' if n == 1 else 'clippers'}." if n else ""), "ok")
    elif accion == "plazas":
        camp.full = not camp.full
        if not camp.full:
            camp.spots_opened_at = datetime.utcnow()
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
    audit("campaña_" + accion, f"#{camp.id} {camp.song}"
          + (f" · +{request.form.get('amount')}" if accion in ("presupuesto", "reabrir") and request.form.get("amount") else ""))
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
    chans = {c.id: c for sec in sections for c in sec.live}
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
        elif act == "deshacer":                       # vuelve lo que se acaba de borrar
            kind_, oid = request.form.get("what", ""), request.form.get("id", type=int) or 0
            obj = db.session.get(ChannelSection if kind_ == "seccion" else Channel, oid)
            if not obj or obj.campaign_id != cid:
                abort(404)
            obj.deleted_at = None
            db.session.commit()
            flash(f"«{obj.title if kind_ == 'seccion' else obj.name}» ha vuelto.", "ok")
            return back(oid if kind_ == "canal" else None)
        elif act == "restaurar_todo":                 # todo lo borrado de esta campaña vuelve a su sitio
            n = 0
            for s in campaign_channels(camp, deleted=True):
                for c in s.channels:
                    if c.deleted_at:
                        c.deleted_at, n = None, n + 1
                if s.deleted_at:
                    s.deleted_at, n = None, n + 1
            audit("canales_restaurados", f"«{camp.song}» · {n}")
            flash("Se han restaurado todas las secciones y canales borrados." if n else "No había nada borrado.", "ok")
        elif act.startswith("seccion_"):
            sec = secs.get(request.form.get("section", type=int)) or abort(404)
            if act == "seccion_nombre" and name:
                sec.title = name[:80]
            elif act == "seccion_borrar":
                sec.deleted_at = datetime.utcnow()
                db.session.commit()
                session["undo"] = {"cid": cid, "what": "seccion", "id": sec.id, "label": f"Sección «{sec.title}» borrada"}
                return back()
            elif act in ("seccion_subir", "seccion_bajar"):
                _move(ChannelSection, sec, sections, "subir" if act == "seccion_subir" else "bajar")
        elif act == "canal_nuevo" and name:
            sec = secs.get(request.form.get("section", type=int)) or abort(404)
            kind_ = request.form.get("kind", "posts")
            ch = Channel(campaign_id=cid, section_id=sec.id, name=name, emoji=emoji or "#", position=len(sec.channels),
                         kind=kind_ if kind_ in CHANNEL_KINDS else "posts")
            db.session.add(ch)
            db.session.commit()
            return back(ch.id)
        elif act.startswith("canal_"):
            ch = chans.get(request.form.get("channel", type=int)) or abort(404)
            if act == "canal_editar" and name:
                ch.name, ch.emoji = name, emoji or ch.emoji
            elif act == "canal_borrar":
                ch.deleted_at = datetime.utcnow()
                db.session.commit()
                session["undo"] = {"cid": cid, "what": "canal", "id": ch.id, "label": f"Canal «{ch.name}» borrado"}
                return redirect(url_for("admin_campaign_channels", cid=cid))
            elif act in ("canal_subir", "canal_bajar"):
                _move(Channel, ch, ch.section.live, "subir" if act == "canal_subir" else "bajar")
        elif act in ("llamada_nueva", "llamada_editar", "llamada_borrar"):
            ch = chans.get(chid) or abort(404)
            call = None
            if act != "llamada_nueva":
                call = db.session.get(Call, request.form.get("call", type=int) or 0)
                if not call or call.campaign_id != cid:
                    abort(404)
            if act == "llamada_borrar":
                db.session.delete(call)
                flash("Llamada borrada.", "ok")
            else:
                title = " ".join((request.form.get("title") or "").split())[:120]
                when = parse_madrid(request.form.get("date", ""), request.form.get("time", ""))
                link = (request.form.get("link") or "").strip()[:500]
                rec = (request.form.get("recording_url") or "").strip()[:500]
                bad = [u for u in (link, rec) if u and not u.startswith("https://")]
                if not title or not when or bad:
                    flash("Pon un título, el día y la hora. Los enlaces tienen que empezar por https://.", "error")
                    return back()
                if call is None:
                    call = Call(campaign_id=cid, author_id=current_user.id, title=title, starts_at=when)
                    db.session.add(call)
                elif call.starts_at != when:
                    call.reminded = False                 # si cambia la hora, se vuelve a avisar
                call.title, call.starts_at = title, when
                call.minutes = max(15, min(request.form.get("minutes", type=int) or 60, 300))
                call.notes = (request.form.get("notes") or "").strip()[:600]
                call.link, call.recording_url = link, rec
                n = 0
                if act == "llamada_nueva" and request.form.get("notify"):
                    for m in CampaignMember.query.filter_by(campaign_id=cid, status="aceptada").all():
                        notify_user(m.user_id, "llamada", f"Nueva llamada en {camp.song}",
                                    f"{title} · {when_madrid(when)} (hora de España). Te avisamos 30 min antes.",
                                    url_for("campaign", cid=cid, canal=ch.id))
                        n += 1
                audit("llamada_guardada", f"«{camp.song}» · {title}")
                flash("Llamada guardada." + (f" Avisados {n} clippers." if n else ""), "ok")
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
                    notify_user(m.user_id, "anuncio", f"{ch.name} · {camp.song}", body[:200] or "Nueva imagen",
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
    calls = (Call.query.filter_by(campaign_id=cid).order_by(Call.starts_at.desc()).all()
             if chan and chan.kind == "calls" else [])
    n_deleted = sum(1 for s in campaign_channels(camp, deleted=True) for x in [s, *s.channels] if x.deleted_at)
    return render_template("admin/channels.html", camp=camp, sections=sections, chan=chan, posts=posts, mod_ids=campaign_mod_ids(cid),
                           pend=pending_by_campaign().get(cid, {}), sec="canales", calls=calls, n_deleted=n_deleted,
                           channel_kinds=CHANNEL_KINDS)


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
        via_app = not key and bool(c.company_id and WHOP_APP_API_KEY)     # instaló la app de ClipProfit: sin clave
        company = creator_company_id(c) if key else (c.company_id if via_app else "")
        balance, err = whop_balance(c.id) if (key or via_app) and company else (None, "")
        owed = creator_owed(c.id)
        missing = cents(owed - balance) if balance is not None and balance + CENT < owed else 0
        short += 1 if missing else 0
        rows.append({"c": c, "has_key": bool(key), "via_app": via_app, "company": company, "balance": balance, "error": err or
                     (_creator_company.get(c.key_var, {}).get("error", "") if key and not company else ""),
                     "owed": owed, "missing": missing, "campaigns": len(c.campaigns),
                     "live": sum(1 for x in c.campaigns if x.status == "activa")})
    set_setting("creators_short", str(short))
    return rows


@app.route("/admin/creadores")
@admin_required
def admin_creators():
    env_keys = sorted(k for k in os.environ if CREATOR_KEY_RE.match(k))
    return render_template("admin/creators.html", rows=creators_overview(), env_keys=env_keys, app_key=bool(WHOP_APP_API_KEY),
                           install_url=f"https://whop.com/apps/{WHOP_APP_ID}/install" if WHOP_APP_ID else "",
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
    return render_template("admin/creator_form.html", c=c, env_keys=env_keys, app_key=bool(WHOP_APP_API_KEY),
                           install_url=f"https://whop.com/apps/{WHOP_APP_ID}/install" if WHOP_APP_ID else "")


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
    aud = audience_for([s for s in camp.submissions if s.status == "aprobado"]) if can["estadisticas"] else {}
    return render_template("admin/campaign.html", camp=camp, st=st, report_url=report_url, can=can, aud=aud,
                           an=ttl_cache(f"analytics:{camp.id}", 300, lambda: campaign_analytics(camp)) if can["estadisticas"] else None, shortfall=shortfall, pend=pending_by_campaign().get(cid, {}), sec="resumen")


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
    if accion == "visitas":                       # corregir vistas a mano: ya no existe (Mustapha no lo quiere en ninguna parte)
        abort(404)
    sub = db.session.get(Submission, sid) or abort(404)
    camp = sub.campaign
    if acting_as_mod():
        if sub.user_id == current_user.id:
            flash("No puedes revisar tus propios clips: los revisa otra persona del equipo.", "error")
            return _back()
        need = "verificar" if accion == "pedir_estadisticas" else "revisar"
        if not mod_can(current_user, sub.campaign_id, need):
            abort(403)
    nxt = request.form.get("next", "")
    if accion == "aprobar":
        if not account_verified(sub):
            if not sub.author:
                flash("Aún no sabemos de qué cuenta es este vídeo: se sabrá en la próxima lectura de vistas. "
                      "Hasta entonces no se puede aprobar.", "error")
            else:
                flash(f"No puedes aprobar este clip: la cuenta @{sub.author} de {sub.user.name} aún no está verificada. "
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
    return redirect(url_for("admin_settings", _anchor="lectura"))


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
        if CLIPPER_WITHDRAW:
            flash("Campaña cerrada. Los clippers ya pueden retirar lo que ganaron y les hemos avisado por el chat.", "ok")
        else:
            flash("Campaña cerrada y clippers avisados. Revisa y paga en la pestaña «Pagos».", "ok")
            return redirect(url_for("admin_campaign_pay", cid=cid) if admin_can("admin_campaign_pay") else url_for("admin_campaign", cid=cid))
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
    if creator_id and creators_on():
        c = db.session.get(Creator, creator_id)
        if c:
            key = creator_key(c)
            if not key and c.company_id and WHOP_APP_API_KEY:     # sin clave: instaló la app de ClipProfit en su Whop
                return WHOP_APP_API_KEY, c.company_id, c.name
            return key, creator_company_id(c), c.name
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
    return creators_on() and any(creator_key(c) or (c.company_id and WHOP_APP_API_KEY) for c in Creator.query.all())


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
WHOP_WAIT_NOTE = "Whop no ha respondido: lo volvemos a intentar (sin pagarte dos veces)."
OLD_WHOP_FAIL_NOTE = "Pago automático pendiente de revisar"


def release_stuck_payouts(query=None):
    """Retiros por Whop que fallaron con la versión anterior: se cancelan y el dinero vuelve al clipper."""
    q = query if query is not None else Payout.query
    stuck = q.filter(Payout.status == "solicitado", Payout.method == WHOP_METHOD, Payout.note == OLD_WHOP_FAIL_NOTE).all()
    for p in stuck:
        p.status, p.note = "rechazado", WHOP_FAIL_NOTE
    if stuck:
        db.session.commit()


# ---------------------------------------------------------------- pagar una campaña de una vez
PAY_RED_FLAGS = ("subida_rapida", "pocos_likes", "cuenta_de_otro", "cuenta_no_declarada")


def campaign_pay_rows(camp):
    """Lo que se debe a cada clipper de esta campaña y las comprobaciones antes de pagarle.
    Cada fila: user, clips, views, earned (ganado aquí), paid (ya enviado o enviándose), owed (lo que falta), checks
    [(nivel, texto)], level (red · orange · ok), can_pay y last_error (si el último pago falló)."""
    subs = (Submission.query.filter(Submission.campaign_id == camp.id, Submission.status == "aprobado")
            .options(joinedload(Submission.user)).all())
    by_user = {}
    for s_ in subs:
        by_user.setdefault(s_.user_id, []).append(s_)
    if not by_user:
        return []
    payouts = Payout.query.filter(Payout.campaign_id == camp.id).order_by(Payout.created_at).all()
    sent, last_fail = {}, {}
    for p_ in payouts:
        if p_.status in PAYOUT_OPEN + ("pagado",):
            sent[p_.user_id] = sent.get(p_.user_id, 0) + p_.amount
        elif p_.status == "rechazado":
            last_fail[p_.user_id] = p_
        if p_.status == "pagado":
            last_fail.pop(p_.user_id, None)
    peaks = dict(db.session.query(ViewSnapshot.submission_id, db.func.max(ViewSnapshot.views))
                 .filter(ViewSnapshot.submission_id.in_([s_.id for s_ in subs])).group_by(ViewSnapshot.submission_id).all())
    whops = {}
    for uid_, wid in db.session.query(User.id, User.whop_user_id).filter(User.whop_user_id.isnot(None), User.whop_user_id != ""):
        whops.setdefault(wid, []).append(uid_)
    key = camp.creator_id if creators_on() else None
    rows = []
    for uid, us in by_user.items():
        u = us[0].user
        earned = cents(sum(x.earned or 0 for x in us))
        paid = cents(sent.get(uid, 0))
        owed = cents(earned - paid)
        if owed >= CENT and camp.status == "finalizada":   # nunca más de lo que tiene libre en el Whop de este creador
            owed = min(owed, cents(user_money(u)["by_creator"].get(key, 0)))
        checks = []
        bad = [x for x in us if set(x.flag_list) & set(PAY_RED_FLAGS)]
        if bad:
            names = sorted({FLAG_LABELS.get(f, f) for x in bad for f in x.flag_list if f in PAY_RED_FLAGS})
            checks.append(("red", f"{len(bad)} {'clip' if len(bad) == 1 else 'clips'} con alerta: {', '.join(names).lower()}"))
        over = [x for x in us if (x.earned or 0) > raw_earning(x, camp) + CENT]
        if over:
            checks.append(("red", f"Cobra más de lo que dan sus vistas en {len(over)} {'clip' if len(over) == 1 else 'clips'}"))
        others = [i for i in whops.get(u.whop_user_id, []) if i != uid] if u.whop_user_id else []
        if others:
            names = ", ".join(n for (n,) in db.session.query(User.name).filter(User.id.in_(others)).limit(3))
            checks.append(("red", f"Su Whop también lo usa: {names}"))
        if u.is_banned:
            checks.append(("red", "Está bloqueado"))
        drop = [x for x in us if peaks.get(x.id) and (x.views or 0) < peaks[x.id] * 0.7 and peaks[x.id] - (x.views or 0) >= 1000]
        if drop:
            checks.append(("orange", f"Las vistas bajaron en {len(drop)} {'clip' if len(drop) == 1 else 'clips'} "
                                     "(¿vídeo borrado o puesto en privado?)"))
        first = min(x.created_at for x in us)
        if u.created_at and first and (first - u.created_at) < timedelta(days=2):
            checks.append(("orange", "Cuenta nueva: se registró justo antes de enviar clips"))
        rows.append({"user": u, "clips": len(us), "views": sum(x.views or 0 for x in us), "earned": earned, "paid": paid,
                     "owed": max(owed, 0.0), "checks": checks, "last_error": last_fail.get(uid)})
    owed_list = sorted(r["owed"] for r in rows if r["owed"] >= CENT)
    median = owed_list[len(owed_list) // 2] if owed_list else 0
    for r in rows:
        if len(owed_list) >= 5 and r["owed"] >= 50 and r["owed"] > median * 5:
            r["checks"].append(("orange", f"Gana mucho más que la media de la campaña ({money(median)})"))
        if r["owed"] >= CENT and not r["user"].whop_user_id:
            r["checks"].append(("grey", "Sin Whop conectado: su dinero queda guardado"))
        lv = [c[0] for c in r["checks"]]
        r["level"] = "red" if "red" in lv else "orange" if "orange" in lv else "ok"
        r["can_pay"] = (r["owed"] >= MIN_WITHDRAW and bool(r["user"].whop_user_id) and not r["user"].is_banned)
    order = {"red": 0, "orange": 1, "ok": 2}
    return sorted(rows, key=lambda r: (r["owed"] < CENT, order[r["level"]], -r["owed"]))


def run_campaign_payouts(ids):
    """Envía por Whop, de uno en uno, los pagos que se acaban de poner en cola. Seguro de repetir (idempotencia)."""
    for pid in ids:
        p_ = db.session.get(Payout, pid)
        if not p_ or p_.status != "solicitado":
            continue
        try:
            whop_pay(p_)
        except Exception as e:  # noqa: BLE001        # no sabemos si salió: se queda en cola (misma clave al reintentar)
            log.exception("Fallo pagando el pago #%s", pid)
            db.session.rollback()
            p_ = db.session.get(Payout, pid)
            p_.status, p_.note, p_.error = "solicitado", WHOP_WAIT_NOTE, str(e)[:400]
        db.session.commit()


@app.route("/admin/campana/<int:cid>/pagar", methods=["GET", "POST"])
@admin_required
def admin_campaign_pay(cid):
    """Pagos de la campaña (24 nov, Mustapha): al terminar, revisar a cada clipper y pagar a todos con un clic."""
    camp = db.session.get(Campaign, cid) or abort(404)
    if request.method == "POST":
        act = request.form.get("act", "")
        if act == "seguir":                               # pagos que se quedaron en cola (la web se reinició o Whop no respondió)
            queued = [p_.id for p_ in Payout.query.filter_by(campaign_id=cid, status="solicitado").all()]
            Payout.query.filter(Payout.id.in_(queued or [0])).update({"error": ""}, synchronize_session=False)
            db.session.commit()
            _start_payouts(queued)
            flash("Reanudando los pagos en cola.", "ok")
            return redirect(url_for("admin_campaign_pay", cid=cid))
        if camp.status != "finalizada":
            flash("Solo se paga una campaña terminada. Termínala primero (Ajustes → Finalizar).", "error")
            return redirect(url_for("admin_campaign_pay", cid=cid))
        chosen = {int(x) for x in request.form.getlist("uids") if x.isdigit()}
        with _withdraw_lock:
            lock_row(Campaign, cid)                       # dos clics a la vez: el segundo ya ve los pagos del primero
            rows = [r for r in campaign_pay_rows(camp) if r["user"].id in chosen and r["can_pay"]]
            total = cents(sum(r["owed"] for r in rows))
            if not rows:
                flash("No hay nada que pagar a los clippers marcados.", "error")
                return redirect(url_for("admin_campaign_pay", cid=cid))
            if not wallet_ready(camp.creator_id):
                flash("El Whop que paga esta campaña no está conectado. Conéctalo antes de pagar.", "error")
                return redirect(url_for("admin_campaign_pay", cid=cid))
            bal, _ = whop_balance(camp.creator_id)
            if bal is not None and bal + CENT < total:
                flash(f"El Whop que paga esta campaña tiene {money(bal)} y hacen falta {money(total)}. "
                      f"Faltan {money(cents(total - bal))}: no se ha pagado a nadie.", "error")
                return redirect(url_for("admin_campaign_pay", cid=cid))
            ids = []
            for r in rows:
                u = r["user"]
                p_ = Payout(user_id=u.id, creator_id=camp.creator_id if creators_on() else None, campaign_id=cid,
                            amount=r["owed"], method=WHOP_METHOD, status="solicitado", note="En cola para enviar",
                            details=f"@{u.whop_username} ({u.whop_user_id})")
                db.session.add(p_)
                db.session.flush()
                ids.append(p_.id)
            audit("campana_pagada", f"«{camp.song}» · {len(ids)} clippers · {money(total)}")
            db.session.commit()
        _start_payouts(ids)
        flash(f"Pagando {money(total)} a {len(ids)} {'clipper' if len(ids) == 1 else 'clippers'}. "
              "Esta página se actualiza sola hasta que terminen.", "ok")
        return redirect(url_for("admin_campaign_pay", cid=cid))
    whop_sync(Payout.query.filter_by(campaign_id=cid, status="procesando").all())
    rows = campaign_pay_rows(camp)
    queued = Payout.query.filter_by(campaign_id=cid, status="solicitado", error="").count()
    stuck = Payout.query.filter(Payout.campaign_id == cid, Payout.status == "solicitado", Payout.error != "").count()
    done = (Payout.query.filter(Payout.campaign_id == cid, Payout.status.in_(("pagado", "procesando")))
            .options(joinedload(Payout.user)).order_by(Payout.created_at.desc()).limit(200).all())
    bal = None
    if camp.status == "finalizada" and wallet_ready(camp.creator_id):
        bal, _ = whop_balance(camp.creator_id)
    return render_template("admin/camp_pay.html", camp=camp, rows=rows, queued=queued, stuck=stuck, done=done, bal=bal,
                           pend=pending_by_campaign().get(cid, {}), sec="pagos",
                           owed=cents(sum(r["owed"] for r in rows)),
                           ready=cents(sum(r["owed"] for r in rows if r["can_pay"] and r["level"] != "red")),
                           paid=cents(sum(r["paid"] for r in rows)))


def _start_payouts(ids):
    if not ids:
        return
    if os.environ.get("PYTEST_CURRENT_TEST"):
        run_campaign_payouts(ids)
        return

    def go():
        with app.app_context():
            run_campaign_payouts(ids)
    threading.Thread(target=go, daemon=True, name="pagos-campana").start()


def _whop_apply(payout, ok, code, data):
    """Actualiza el retiro según la respuesta de Whop. Devuelve 'pagado', 'procesando' o 'fallido'."""
    data = data if isinstance(data, dict) else {}
    status = str(data.get("status") or "").lower()
    if data.get("id"):
        payout.ext_id = str(data["id"])[:80]
    if ok and status not in ("failed", "processing", "pending", "created"):
        payout.status, payout.paid_at = "pagado", datetime.utcnow()
        payout.note = f"Enviado a tu Whop · {payout.ext_id}"[:255]
        camp_ = db.session.get(Campaign, payout.campaign_id) if payout.campaign_id else None
        notify_user(payout.user_id, "retiro", f"Pago de «{camp_.song}» enviado" if camp_ else "Retiro enviado",
                    f"{money(payout.amount)} ya están en tu Whop.", "/ganancias?tab=retiros")
        return "pagado"
    if ok and status != "failed":
        payout.status, payout.note = "procesando", "Enviando a tu cuenta de Whop"
        return "procesando"
    err = data.get("error")
    reason = data.get("failure_reason") or data.get("message") or (err.get("message") if isinstance(err, dict) else err)
    if not ok and (code == 0 or code == 429 or code >= 500):
        # Auditoría 24 nov: sin respuesta clara de Whop (se cortó la conexión, tardó demasiado o falló Whop por dentro)
        # NO sabemos si el dinero salió. Antes se cancelaba y el dinero «volvía» al clipper: al pagarle otra vez salía un
        # pago nuevo (con otra clave) y podía cobrar dos veces. Ahora se queda en cola y se reintenta con la MISMA clave:
        # si el primero sí llegó, Whop no lo repite.
        payout.status, payout.note = "solicitado", WHOP_WAIT_NOTE
        payout.error = f"Whop {code}: {reason or str(data)[:300]}"[:400]
        log.error("Whop: sin respuesta clara al pagar #%s (%s): queda en cola para reintentar", payout.id, code)
        return "espera"
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
    if not api_key or not company:                    # seguro que no salió nada: se cancela (código -1, no «sin respuesta»)
        return _whop_apply(payout, False, -1, {"error": "El Whop de este creador no está conectado"})
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
    buf = io.StringIO()
    n = write_backup(buf)
    audit("copia_descargada", f"{n} filas")
    db.session.commit()
    name = f"copia-{APP_NAME.lower().replace(' ', '-')}-{datetime.utcnow():%Y%m%d-%H%M}.json"
    return Response(buf.getvalue(), mimetype="application/json",
                    headers={"Content-Disposition": f"attachment; filename={name}"})


def _backup_val(v):
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(v)).decode()
    if v is not None and not isinstance(v, (int, float, str, bool)):
        return str(v)
    return v


def write_backup(fh):
    """Escribe la copia (JSON: {app, created_at, tables: {tabla: [filas]}}) fila a fila, sin cargarla entera en memoria.
    Devuelve cuántas filas lleva. Las imágenes (BACKUP_SKIP) no van: pesan mucho y no son datos del negocio."""
    total = 0
    fh.write(json.dumps({"app": APP_NAME, "created_at": datetime.utcnow().isoformat() + "Z"})[:-1] + ', "tables": {')
    with db.engine.connect() as conn:
        for i, table in enumerate(db.metadata.sorted_tables):
            cols = [c for c in table.columns if (table.name, c.name) not in BACKUP_SKIP]
            fh.write(("," if i else "") + json.dumps(table.name) + ":[")
            res = conn.execution_options(stream_results=True, yield_per=500).execute(db.select(*cols))
            for n, r in enumerate(res):
                fh.write(("," if n else "") + json.dumps({c.name: _backup_val(v) for c, v in zip(cols, r)}, ensure_ascii=False))
                total += 1
            fh.write("]")
    fh.write("}}")
    return total


def restore_backup(data):
    """Vuelve a meter una copia en una base de datos VACÍA (la web recién creada). Devuelve cuántas filas metió."""
    from sqlalchemy import Date, DateTime, LargeBinary
    tables = data.get("tables") or {}
    with db.engine.begin() as conn:
        for table in db.metadata.sorted_tables:
            if tables.get(table.name) and conn.execute(db.select(db.func.count()).select_from(table)).scalar():
                raise RuntimeError(f"La tabla «{table.name}» no está vacía: la copia solo se mete en una base de datos nueva.")
    total = 0
    with db.engine.begin() as conn:
        for table in db.metadata.sorted_tables:
            rows = tables.get(table.name) or []
            kinds = {c.name: c.type for c in table.columns}
            batch = []
            for row in rows:
                clean = {}
                for k, v in row.items():
                    t_ = kinds.get(k)
                    if t_ is None:
                        continue                                  # columna que ya no existe
                    if v is not None and isinstance(t_, DateTime):
                        v = datetime.fromisoformat(v.replace("Z", ""))
                    elif v is not None and isinstance(t_, Date):
                        v = datetime.fromisoformat(v).date()
                    elif v is not None and isinstance(t_, LargeBinary):
                        v = base64.b64decode(v)
                    clean[k] = v
                batch.append(clean)
                if len(batch) >= 500:
                    conn.execute(table.insert(), batch)
                    total, batch = total + len(batch), []
            if batch:
                conn.execute(table.insert(), batch)
                total += len(batch)
        if db.engine.dialect.name == "postgresql":           # que los números nuevos sigan después de los de la copia
            for table in db.metadata.sorted_tables:
                if "id" in table.columns and tables.get(table.name):
                    conn.execute(text(f"SELECT setval(pg_get_serial_sequence('\"{table.name}\"', 'id'), "
                                      f"(SELECT COALESCE(MAX(id), 1) FROM \"{table.name}\"))"))
    return total


@app.cli.command("restaurar-copia")
def restore_backup_cmd():
    """flask --app app restaurar-copia < copia.json(.gz): mete una copia en una base de datos vacía."""
    import gzip
    raw = sys.stdin.buffer.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    with app.app_context():
        db.create_all()
        print(f"Copia restaurada: {restore_backup(json.loads(raw))} filas.")


# Copia de cada noche fuera de Railway (24 nov, Mustapha): Railway solo programa copias en el plan Pro, así que la web
# guarda la suya en Bunny Storage. Variables: BUNNY_STORAGE_ZONE (nombre de la zona) y BUNNY_STORAGE_KEY (su contraseña,
# pestaña «FTP & API Access»); BUNNY_STORAGE_HOST solo si la zona no está en Falkenstein (p. ej. uk.storage.bunnycdn.com).
BUNNY_STORAGE_ZONE = os.environ.get("BUNNY_STORAGE_ZONE", "").strip()
BUNNY_STORAGE_KEY = os.environ.get("BUNNY_STORAGE_KEY", "").strip()
BUNNY_STORAGE_HOST = (os.environ.get("BUNNY_STORAGE_HOST", "").strip().removeprefix("https://").split("/")[0]
                      or "storage.bunnycdn.com")          # aunque peguen la dirección entera de Bunny, solo vale el host
BACKUP_AT = os.environ.get("BACKUP_AT", "04:30")          # hora de España, después de la lectura de las 03:00
BACKUP_KEEP_DAYS = 14                                     # las de cada día, 2 semanas
BACKUP_KEEP_MONTHS = 6                                    # y la del día 1 de cada mes, medio año


def storage_ready():
    return bool(BUNNY_STORAGE_ZONE and BUNNY_STORAGE_KEY)


def _storage_url(path=""):
    return f"https://{BUNNY_STORAGE_HOST}/{BUNNY_STORAGE_ZONE}/copias/{path}"


def backup_state():
    try:
        return json.loads(get_setting("backup_run", "") or "{}")
    except ValueError:
        return {}


def backup_due(now=None):
    """¿Toca la copia? Una al día a partir de BACKUP_AT; si falla, otro intento a la hora (hasta 3)."""
    if not storage_ready():
        return False
    loc = _local(now)
    if loc.strftime("%H:%M") < BACKUP_AT:
        return False
    st = backup_state()
    day = loc.strftime("%Y-%m-%d")
    if st.get("day") != day:
        return True
    if st.get("ok") or st.get("tries", 0) >= 3:
        return False
    last = st.get("at")
    return not last or datetime.utcnow() - datetime.fromisoformat(last) > timedelta(minutes=60)


def run_offsite_backup(now=None):
    """Hace la copia, la sube comprimida a Bunny Storage (copias/copia-AAAA-MM-DD.json.gz) y borra las viejas."""
    import gzip
    import tempfile
    t0 = datetime.utcnow()
    day = _local(now).strftime("%Y-%m-%d")
    st = backup_state()
    tries = (st.get("tries", 0) if st.get("day") == day else 0) + 1
    set_setting("backup_run", json.dumps({"day": day, "tries": tries, "at": t0.isoformat(timespec="seconds"), "ok": False}))
    db.session.commit()
    name = f"copia-{day}.json.gz"
    try:
        with tempfile.TemporaryFile() as tmp:
            gz = gzip.GzipFile(fileobj=tmp, mode="wb")
            txt = io.TextIOWrapper(gz, encoding="utf-8")
            rows = write_backup(txt)
            txt.flush()
            txt.detach()
            gz.close()
            size = tmp.tell()
            tmp.seek(0)
            r = requests.put(_storage_url(name), data=tmp, timeout=600,
                             headers={"AccessKey": BUNNY_STORAGE_KEY, "Content-Type": "application/octet-stream"})
        if r.status_code == 401:
            raise RuntimeError("Bunny rechaza la contraseña (401): revisa BUNNY_STORAGE_KEY (la de la Storage Zone, no la de vídeos)")
        if r.status_code == 404:
            raise RuntimeError("Bunny no encuentra la zona (404): revisa BUNNY_STORAGE_ZONE y, si no está en Falkenstein, BUNNY_STORAGE_HOST")
        if r.status_code >= 300:
            raise RuntimeError(f"Bunny Storage respondió {r.status_code}: {r.text[:150]}")
        gone = prune_offsite_backups(_local(now).date())
    except Exception as e:  # noqa: BLE001
        log.error("No se pudo hacer la copia de seguridad: %s", e)
        job_mark("copia", False, str(e)[:250], started=t0)
        db.session.commit()
        return False
    set_setting("backup_run", json.dumps({"day": day, "tries": tries, "at": t0.isoformat(timespec="seconds"), "ok": True}))
    set_setting("backup_last", json.dumps({"at": datetime.utcnow().isoformat(timespec="seconds"), "name": name,
                                           "size": size, "rows": rows}))
    job_mark("copia", True, f"{name} · {_mb(size)} · {rows:,} filas".replace(",", ".")
             + (f" · {gone} antiguas borradas" if gone else ""), started=t0)
    db.session.commit()
    return True


def prune_offsite_backups(today):
    """Borra de Bunny las copias de más de 14 días, salvo la del día 1 de cada mes (esas, 6 meses)."""
    r = requests.get(_storage_url(), headers={"AccessKey": BUNNY_STORAGE_KEY, "Accept": "application/json"}, timeout=30)
    if r.status_code != 200:
        return 0
    gone = 0
    for o in r.json() or []:
        m = re.match(r"copia-(\d{4})-(\d{2})-(\d{2})\.json\.gz$", o.get("ObjectName") or "")
        if not m:
            continue
        d = datetime(int(m[1]), int(m[2]), int(m[3])).date()
        age = (today - d).days
        if age < BACKUP_KEEP_DAYS or (d.day == 1 and age < 31 * BACKUP_KEEP_MONTHS):
            continue
        if requests.delete(_storage_url(m[0]), headers={"AccessKey": BUNNY_STORAGE_KEY}, timeout=30).status_code < 300:
            gone += 1
    return gone


def backup_last():
    try:
        b = json.loads(get_setting("backup_last", "") or "{}")
    except ValueError:
        return {}
    if b.get("at"):
        b["when"] = datetime.fromisoformat(b["at"])
    return b


@app.route("/admin/copia-ahora", methods=["POST"])
@admin_required
def admin_backup_now():
    """Botón del Panel técnico: hace la copia a Bunny en este momento (en segundo plano: no hace esperar a la página)."""
    if not storage_ready():
        flash("Falta conectar Bunny Storage: pon BUNNY_STORAGE_ZONE y BUNNY_STORAGE_KEY en Railway.", "error")
        return redirect(url_for("admin_settings", _anchor="salud"))
    audit("copia_manual", "")
    db.session.commit()

    def go():
        with app.app_context():
            set_setting("backup_run", "")
            db.session.commit()
            run_offsite_backup()
    if os.environ.get("PYTEST_CURRENT_TEST"):
        go()
    else:
        threading.Thread(target=go, daemon=True).start()
    flash("Haciendo la copia. En un minuto verás el resultado aquí, en «Tareas automáticas».", "ok")
    return redirect(url_for("admin_settings", _anchor="salud"))


@app.route("/admin/borrar-datos", methods=["POST"])
@admin_required
def admin_wipe():
    """Borra todos los datos de prueba (campañas, clips, retiros, mensajes, clippers…). Solo quedan los admins."""
    if not ALLOW_WIPE:
        flash("Borrar datos está desactivado en la web real. Para activarlo, añade ALLOW_WIPE=1 en Railway.", "error")
        return redirect(url_for("admin_settings", _anchor="datos"))
    if (request.form.get("confirm") or "").strip().upper() != "BORRAR":
        flash("Para borrar todo, escribe BORRAR en la casilla.", "error")
        return redirect(url_for("admin_settings", _anchor="datos"))
    admin_ids = [u.id for u in User.query.filter_by(is_admin=True).all()]
    if current_user.id not in admin_ids:
        admin_ids.append(current_user.id)
    keep_campaigns = request.form.get("mode") == "keep_campaigns"
    models = (ViewSnapshot, Submission, Payout, CampaignAccount, CampaignMember, CampaignWaitlist, Message, CampaignLead,
              Notice, PushSub, LessonProgress, PostComment, PostLike, Post, Channel, ChannelSection, CampaignMod, CampMessage, IgnoredClip, Invite, Call, AudienceStat, ApiToken,
              Campaign, Creator,
              AuditLog, Setting)
    for model in models:
        if keep_campaigns and model in (Campaign, Creator, Post, Channel, ChannelSection, Invite, Call):
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


@app.route("/admin/buscar")
@admin_required
def admin_find():
    """Buscador del panel: clippers (nombre, correo, @cuenta), campañas y clips por número."""
    q = request.args.get("q", "").strip()[:80]
    term = q.lstrip("@#").lower()
    users, camps, accs, clip = [], [], [], None
    if len(term) >= 2 or term.isdigit():
        like = f"%{term}%"
        if term.isdigit() and len(term) < 10:
            clip = db.session.get(Submission, int(term))
        users = (User.query.filter(db.or_(db.func.lower(User.email).like(like), db.func.lower(User.name).like(like)))
                 .order_by(User.name).limit(12).all())
        seen = {u.id for u in users}
        accs = [a for a in (CampaignAccount.query.filter(db.func.lower(CampaignAccount.handle).like(like))
                            .options(joinedload(CampaignAccount.user), joinedload(CampaignAccount.campaign))
                            .order_by(CampaignAccount.handle).limit(24).all()) if a.user_id not in seen][:12]
        if admin_can("admin_campaign"):
            camps = (Campaign.query.filter(db.or_(db.func.lower(Campaign.song).like(like), db.func.lower(Campaign.artist).like(like)))
                     .order_by(Campaign.created_at.desc()).limit(12).all())
    total = len(users) + len(camps) + len(accs) + (1 if clip else 0)
    people = {u.id for u in users} | {a.user_id for a in accs}
    if not clip and not camps and len(people) == 1:           # todo lleva a la misma persona: directo a su ficha
        return redirect(url_for("admin_clipper", uid=people.pop()))
    if not clip and not people and len(camps) == 1:
        return redirect(camp_admin_url(camps[0]))
    return render_template("admin/search.html", q=q, users=users, camps=camps, accs=accs, clip=clip, total=total)


def camp_admin_url(camp):
    return url_for("admin_dist", did=camp.id) if camp.status == DIST_STATUS else url_for("admin_campaign", cid=camp.id)


@app.route("/admin/clippers", methods=["GET", "POST"])
@admin_required
def admin_clippers():
    if request.method == "POST":
        if not admin_can("admin_payouts"):          # auditoría 24 nov: ver clippers no basta para pagar o marcar retiros
            abort(403)
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
                   "procesando": "Whop está procesando el pago. Se actualizará solo.",
                   "espera": "Whop no ha respondido. El retiro sigue pendiente: vuelve a pulsar «Pagar con Whop» en un rato "
                             "(si el primero llegó, Whop no lo repite)."}.get(
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
    # Con miles de clippers: se ordena, se busca y se cuenta en la base de datos, y solo se calculan los 50 que se ven
    agg = (db.session.query(Submission.user_id.label("uid"),
                            db.func.coalesce(db.func.sum(db.case((Submission.status == "aprobado", Submission.earned), else_=0)), 0).label("e"),
                            db.func.count(Submission.id).label("n"))
           .group_by(Submission.user_id).subquery())
    base = db.session.query(User, db.func.coalesce(agg.c.n, 0)).outerjoin(agg, agg.c.uid == User.id)
    if q:
        like = f"%{q}%"
        base = base.filter(db.or_(db.func.lower(User.email).like(like), db.func.lower(User.name).like(like),
                                  *[db.func.lower(getattr(User, f"{p}_handles")).like(like) for p in PLATFORMS
                                    if hasattr(User, f"{p}_handles")]))
    order = request.args.get("orden", "ganado")
    if order == "actividad":                       # los que entraron hace menos, primero; los que nunca, al final
        base = base.order_by(User.last_seen_at.desc().nullslast(), User.created_at.desc())
    else:
        base = base.order_by(db.func.coalesce(agg.c.e, 0).desc(), User.created_at.desc())
    page = max(request.args.get("p", 1, type=int), 1)             # de 50 en 50 (la búsqueda y el orden van sobre todos)
    total = base.count()
    page_rows = base.limit(page * LIST_PAGE).all()
    if page_rows:                                  # clips (con su campaña) y retiros de los que se ven, en 3 consultas (antes 2 por fila)
        User.query.filter(User.id.in_([u.id for u, _ in page_rows])).options(
            selectinload(User.submissions).joinedload(Submission.campaign), selectinload(User.payouts)).all()
    rows = [(u, user_money(u), n) for u, n in page_rows]
    now = datetime.utcnow()
    seen = db.session.query(db.func.count(User.id)).filter(User.is_admin.is_(False))
    activity = {"online": seen.filter(User.last_seen_at >= now - timedelta(seconds=ONLINE_FOR)).scalar(),
                "day": seen.filter(User.last_seen_at >= now - timedelta(days=1)).scalar(),
                "idle": seen.filter(db.or_(User.last_seen_at.is_(None), User.last_seen_at < now - timedelta(days=7))).scalar()}
    return render_template("admin/clippers.html", rows=rows, q=q, order=order, activity=activity,
                           total=total, more=total > len(rows), page=page)


LIST_PAGE = 50               # filas por tanda en las listas largas («Ver más»)


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
        AudienceStat.query.filter(AudienceStat.submission_id.in_(sub_ids)).delete(synchronize_session=False)
    for model in (Submission, Payout, CampaignAccount, CampaignMember, CampaignWaitlist, Message, Notice, PushSub, LessonProgress,
                  PostComment, PostLike, CampaignMod, CampMessage, IgnoredClip, ApiToken):
        model.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    CampMessage.query.filter_by(sender_id=user.id).update({"sender_id": None}, synchronize_session=False)
    Post.query.filter_by(author_id=user.id).update({"author_id": None}, synchronize_session=False)
    Message.query.filter_by(sender_id=user.id).update({"sender_id": None}, synchronize_session=False)
    AuditLog.query.filter_by(admin_id=user.id).update({"admin_id": None}, synchronize_session=False)
    Invite.query.filter_by(used_by=user.id).update({"used_by": None}, synchronize_session=False)
    Invite.query.filter_by(created_by=user.id).update({"created_by": None}, synchronize_session=False)
    Call.query.filter_by(author_id=user.id).update({"author_id": None}, synchronize_session=False)
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
        return _per_request("unread_admin", lambda: Message.query.filter_by(from_admin=False, read_by_admin=False).count())
    uid = current_user.id
    return _per_request("unread_user", lambda: Message.query.filter_by(user_id=uid, from_admin=True, read_by_user=False).count())


def notify_phone(title, message, path="", tags="speech_balloon"):
    """Manda un aviso al móvil del admin (app ntfy). Va en segundo plano: nunca frena ni rompe la web.
    Apagado (PHONE_ALERTS): los avisos se ven en el panel (campanita y Panel técnico), no llegan al móvil."""
    if not PHONE_ALERTS or not NTFY_TOPIC:
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
    "llamada": "Llamadas de tus campañas (y 30 min antes de empezar)",
    "mensaje": "Mensajes y avisos del equipo",
}
WHATSAPP_ENABLED = False          # WhatsApp en pausa hasta hablarlo con el cliente (el código se queda listo)
NOTIFY_CHANNELS = {"push": "En este móvil u ordenador"}


CLIPPER_PUSH = False              # avisos al móvil de los clippers: quitados (a ellos les llega todo a la campanita)
PHONE_ALERTS = False              # avisos al móvil del equipo (ntfy): quitados también


def push_ready():
    return CLIPPER_PUSH and bool(VAPID_PRIVATE_KEY and VAPID_PUBLIC_KEY)


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


# Avisos automáticos (admin → Avisos → «Automáticos», 24 nov, Mustapha): cada uno se enciende o apaga para todos.
# (clave, grupo, nombre, qué le llega al clipper, cuándo, encendido por defecto). Los que ya existían vienen encendidos;
# los nuevos, apagados, para que Mustapha elija. Los mensajes del equipo (soporte, avisos a mano) no se tocan aquí.
AUTO_NOTICES = [
    ("clip_nuevo", "Clips", "Clip detectado", "«Clip detectado en <campaña>: hemos añadido solo tu nuevo vídeo»", "Cuando la lectura encuentra un vídeo nuevo en su cuenta", True),
    ("minimo_cerca", "Clips", "Clip cerca del mínimo", "«Tu clip está a punto de cobrar: le falta poco para el mínimo»", "Una vez por clip, al llegar al 80 % del pago mínimo", False),
    ("estadisticas", "Clips", "Pedir la grabación de estadísticas", "«Verifica las vistas de tu clip: tienes 72 h»", "Cuando un clip llega al mínimo. Si lo apagas, el clipper no se entera del plazo y el clip se rechaza solo", True),
    ("clip_aprobado", "Clips", "Clip aprobado", "«Tu clip de <campaña> está aprobado»", "Cuando un admin o mod lo aprueba", True),
    ("clip_rechazado", "Clips", "Clip rechazado", "«Clip rechazado en <campaña>» con el motivo", "Cuando un admin o mod lo rechaza", True),
    ("campana_nueva", "Campañas", "Campaña nueva", "«Nueva campaña de <creador>» con lo que se puede ganar", "Al crear una campaña activa", True),
    ("plazas", "Campañas", "Plazas abiertas", "«Hay plazas en <campaña>»", "A quien pulsó «Avísame» cuando se reabren plazas", True),
    ("presupuesto_bajo", "Campañas", "Queda poco presupuesto", "«A <campaña> le queda poco dinero: sube tus clips ya»", "Una vez, cuando queda el 20 % del presupuesto. A los clippers de esa campaña", False),
    ("campana_fin", "Campañas", "Campaña terminada", "«<campaña> ha terminado: has ganado X $ y te lo enviamos a tu Whop»", "Al cerrarse la campaña, a quien ganó dinero en ella (además del mensaje del chat)", False),
    ("anuncio", "Campañas", "Anuncios de la campaña", "«<canal> · <campaña>» con el texto del anuncio", "Cuando el equipo publica en un canal de anuncios", True),
    ("llamada", "Campañas", "Llamadas", "«Nueva llamada en <campaña>» y «La llamada empieza en 30 min»", "Al programar una llamada y 30 min antes", True),
    ("retiro", "Dinero", "Pago enviado", "«Pago de <campaña> enviado: X $ ya están en tu Whop»", "Cuando Whop envía un pago", True),
    ("conexion", "Cuenta", "Volver a conectar una red", "«Vuelve a conectar tu TikTok»", "Cuando caduca el permiso de una red conectada por la web", True),
    ("mod", "Cuenta", "Nombrado mod", "«Ahora eres mod de <campaña>»", "Cuando le hacen mod de una campaña", True),
    ("bienvenida", "Cuenta", "Bienvenida", "«Bienvenido a ClipProfit: así empiezas» con enlace a Formación", "Al terminar «Crea tu perfil»", False),
    ("inactividad", "Actividad", "No ha publicado en 24 h", "«@cuenta no ha subido ningún clip en 24 h. Quedan X $ por repartir»", "Cada día tras la lectura, a cuentas activas que no publicaron (no a cuentas paradas)", False),
]
AUTO_KEYS = {k for k, *_ in AUTO_NOTICES}


def auto_notices():
    """{clave: encendido} con lo guardado por Mustapha y, si no, lo de por defecto."""
    try:
        saved = json.loads(get_setting("auto_notices", "") or "{}")
    except ValueError:
        saved = {}
    return {k: bool(saved.get(k, d)) for k, _g, _n, _t, _w, d in AUTO_NOTICES}


def auto_on(event):
    return event not in AUTO_KEYS or auto_notices().get(event, True)


# Qué avisos van también por correo (casilla «Correo»). Con el plan gratis de Resend (100 al día) solo lo importante.
AUTO_MAIL_DEFAULT = {"estadisticas": True, "retiro": True, "campana_fin": True}


def auto_mail():
    try:
        saved = json.loads(get_setting("auto_mail", "") or "{}")
    except ValueError:
        saved = {}
    return {k: bool(saved.get(k, AUTO_MAIL_DEFAULT.get(k, False))) for k in AUTO_KEYS}


def mail_on(event):
    return event in AUTO_KEYS and auto_mail().get(event, False)


def send_notice_mail(user, event, title, body, path=""):
    """El mismo aviso de la campanita, por correo, con botón a la web y enlace para darse de baja."""
    first = (user.name or "").split()[0] if user.name else ""
    lines = [f"Hola{(', ' + first) if first else ''}:", body]
    link = public_url(path or "/")
    unsub = unsub_url(user)
    return send_mail(user.email, title, "\n\n".join(lines) + f"\n\n{link}\n\nDejar de recibir avisos por correo: {unsub}",
                     html=mail_html(title, lines, ("Abrir " + APP_NAME, link), unsub=unsub), kind=event, user_id=user.id, unsub=unsub)


def notify_user(user_id, event, title, body, path="", bell=True, camp_id=None):
    """Aviso personal a un clipper: siempre en su campanita y, si lo tiene activado, en su móvil."""
    bell_ok = auto_on(event)                       # casillas de admin → Avisos → «Automáticos»
    mail_ok = mail_on(event) and mail_ready()
    if not bell_ok and not mail_ok:
        return
    u = db.session.get(User, user_id)
    if not u or is_demo(u):
        return
    if mail_ok and mail_ok_for(u):
        try:
            send_notice_mail(u, event, title, body, path)
        except Exception:  # noqa: BLE001
            log.exception("No se pudo preparar el aviso por correo")
    if not bell_ok:
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
    text_ = last.body or ("Foto" if last.image_mime else "")
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


def my_available():
    """Saldo que el clipper puede retirar ya (para el menú de perfil). Una vez por petición."""
    if not current_user.is_authenticated or current_user.is_admin:
        return 0
    uid = current_user.id

    def load():                                       # dos sumas en la base de datos (antes cargaba todos sus clips)
        closed = (db.session.query(db.func.coalesce(db.func.sum(Submission.earned), 0)).join(Campaign)
                  .filter(Submission.user_id == uid, Submission.status == "aprobado", Campaign.status == "finalizada").scalar())
        out = (db.session.query(db.func.coalesce(db.func.sum(Payout.amount), 0))
               .filter(Payout.user_id == uid, Payout.status.in_(PAYOUT_OPEN + ("pagado",))).scalar())
        return max(cents(cents(closed) - cents(out)), 0.0)
    return _per_request("my_available", load)


def pop_undo():
    """«Deshacer» del último borrado (canales): solo se enseña una vez, en el aviso flotante."""
    return session.pop("undo", None) if has_request_context() else None


def clip_thumb(s):
    """Portada de un clip: la copia en Bunny, la de YouTube o la que dio Apify. Vacío = se pinta la portada de la campaña."""
    if s.video_guid and BUNNY_CDN_HOST and s.video_status == "listo":
        return f"https://{BUNNY_CDN_HOST}/{s.video_guid}/thumbnail.jpg"
    if s.platform == "youtube":
        vid = (s.video_key or "").split(":", 1)[-1]
        if vid:
            return f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"
    if s.id and (s.platform == "tiktok" or s.thumb_ok):  # portada guardada o, en TikTok, pedida fresca
        return url_for("clip_cover", sid=s.id)
    return s.thumb_url or ""


_cover_cache, _cover_lock = {}, threading.Lock()
_img_cache = {}                                    # url -> (caduca, bytes, tipo): imágenes de redes servidas desde aquí
COVER_DIAG = {}                                    # último intento, para la prueba del Panel técnico
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0 Safari/537.36")


def tiktok_cover(url):
    """Portada actual de un vídeo de TikTok con su oEmbed público (gratis, sin clave). Se guarda 6 horas."""
    now = time.time()
    hit = _cover_cache.get(url)
    if hit and hit[0] > now:
        return hit[1]
    thumb = ""
    try:
        r = requests.get("https://www.tiktok.com/oembed", params={"url": url}, timeout=6,
                         headers={"User-Agent": BROWSER_UA, "Accept": "application/json"})
        COVER_DIAG["oembed"] = r.status_code
        if r.ok:
            j = r.json() or {}
            thumb = j.get("thumbnail_url") or ""
            if not thumb:
                COVER_DIAG["oembed_msg"] = str(j.get("status_msg") or j.get("message") or j.get("code") or "")[:80]
    except Exception as e:  # noqa: BLE001
        COVER_DIAG["oembed"] = type(e).__name__
    with _cover_lock:
        _cover_cache[url] = (now + (6 * 3600 if thumb else 900), thumb)
        if len(_cover_cache) > 4000:
            for k in [k for k, v in _cover_cache.items() if v[0] <= now] or list(_cover_cache)[:1000]:
                _cover_cache.pop(k, None)
    return thumb


def shrink_image(raw, size):
    """Imagen pequeña en JPEG (portada 240x320 o foto 96x96) para guardarla en la base de datos sin ocupar casi nada."""
    from PIL import Image, ImageOps, UnidentifiedImageError
    try:
        img = ImageOps.fit(Image.open(io.BytesIO(raw), formats=IMG_FORMATS).convert("RGB"), size)
    except (UnidentifiedImageError, OSError, ValueError):
        return None
    out = io.BytesIO()
    img.save(out, "JPEG", quality=80, optimize=True)
    return out.getvalue()


_keep_fails = []                                   # fallos recientes al guardar imágenes: si fallan seguidas, se para un rato


def keep_image(obj, kind, url, size):
    """Descarga y guarda la portada (kind="thumb") o la foto (kind="avatar") mientras el enlace de la red aún vale.
    Si fallan 8 seguidas en 10 minutos, deja de intentarlo un rato para no alargar la lectura diaria."""
    now = time.time()
    _keep_fails[:] = [t for t in _keep_fails if now - t < 600]
    if len(_keep_fails) >= 8:
        return False
    data = fetch_image(url)
    small = shrink_image(data[0], size) if data else None
    if not small:
        _keep_fails.append(now)
    else:
        _keep_fails.clear()
    if small:
        setattr(obj, kind + "_data", small)
        setattr(obj, kind + "_ok", True)
    return bool(small)


def public_host(url):
    """¿La dirección apunta a internet? (no a la red interna de Railway, a localhost ni a IPs privadas). Auditoría 24 nov:
    las direcciones de portadas y fotos vienen de datos de terceros; así nadie puede usar la web para mirar dentro."""
    import ipaddress
    try:
        u = urlparse(url)
    except ValueError:
        return False
    host = (u.hostname or "").lower().rstrip(".")
    if u.scheme not in ("http", "https") or not host or host == "localhost" or host.endswith((".internal", ".local", ".localhost")):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True                                  # un nombre normal (cdn.tiktok…)
    return ip.is_global


def fetch_image(url):
    """Descarga una imagen de una red (sin decir desde qué web se pide) y la guarda 6 horas. Así no la bloquean ni caduca a medias."""
    if not url or not url.startswith("http") or not public_host(url):
        return None
    now = time.time()
    hit = _img_cache.get(url)
    if hit and hit[0] > now:
        return hit[1:] if hit[1] else None
    data = None
    try:
        r, hops = None, 0
        while hops < 4:                              # las redirecciones se siguen a mano para comprobar cada salto
            r = requests.get(url, timeout=8, headers={"User-Agent": BROWSER_UA, "Accept": "image/*"}, allow_redirects=False)
            nxt = r.headers.get("Location") if r.is_redirect else None
            if not nxt:
                break
            url, hops = requests.compat.urljoin(url, nxt), hops + 1
            if not public_host(url):
                raise ValueError("redirección a una dirección interna")
        COVER_DIAG["img"] = r.status_code
        ct = (r.headers.get("Content-Type") or "").split(";")[0]
        if r.ok and ct.startswith("image/") and len(r.content) < 3_000_000:
            data = (r.content, ct)
    except Exception as e:  # noqa: BLE001
        COVER_DIAG["img"] = type(e).__name__
    with _cover_lock:
        _img_cache[url] = (now + (6 * 3600 if data else 900),) + (data or (None, None))
        if len(_img_cache) > 600:                  # como mucho unas 600 imágenes en memoria por proceso
            for k in sorted(_img_cache, key=lambda k: _img_cache[k][0])[:200]:
                _img_cache.pop(k, None)
    return data


_outside = threading.BoundedSemaphore(2)             # descargas de imágenes de fuera que se permiten a la vez dentro de la web


def fetch_image_guarded(url):
    """Como fetch_image, pero sin dejar que las redes lentas ocupen los hilos de la web: si ya hay dos descargas en
    marcha, no espera (devuelve None y la pantalla enseña la imagen de la campaña; la lectura nocturna la guardará)."""
    if not url or not _outside.acquire(blocking=False):
        return None
    try:
        return fetch_image(url)
    finally:
        _outside.release()


def image_response(data):
    body, ct = data
    resp = Response(body, mimetype=ct)
    resp.headers["Cache-Control"] = "private, max-age=21600"
    return resp


@app.route("/portada-inicio/<int:tid>")
def landing_tile_img(tid):
    t = db.session.get(LandingTile, tid)
    if not t or t.status != "ok" or not t.thumb_data:
        abort(404)
    resp = Response(t.thumb_data, mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


LANDING_TILE_SIZE = (360, 640)
_tiles_lock = threading.Lock()


def load_landing_tiles():
    """Saca la portada (y las vistas) de cada enlace pendiente. Gratis con yt-dlp; si falla y hay Apify, con Apify
    (1 resultado por enlace). Va en un hilo aparte para no hacer esperar al admin."""
    if not _tiles_lock.acquire(blocking=False):
        return
    try:
        with app.app_context():
            for t in LandingTile.query.filter_by(status="cargando").order_by(LandingTile.id).all():
                thumb, views, err = "", None, ""
                try:
                    got = viewcounter.ytdlp_video_cover(t.url)
                    thumb, views = got["thumb"], got["views"]
                except Exception as exc:  # noqa: BLE001
                    err = str(exc)[:160]
                if not thumb and viewcounter.APIFY_TOKEN:
                    try:
                        plat = viewcounter.detect_platform(t.url)
                        found = viewcounter.info_by_id(plat, viewcounter.fetch_items(plat, [viewcounter.normalize_url(t.url)]))
                        item = found.get(viewcounter.video_id(viewcounter.normalize_url(t.url))) or {}
                        thumb, views = item.get("thumb") or "", item.get("views") if views is None else views
                    except Exception as exc:  # noqa: BLE001
                        err = err or str(exc)[:160]
                data = fetch_image(thumb) if thumb else None
                small = shrink_image(data[0], LANDING_TILE_SIZE) if data else None
                if small:
                    t.thumb_data, t.status, t.views, t.error = small, "ok", views, ""
                else:
                    t.status, t.error = "error", (err or "No se pudo sacar la portada de este enlace.")[:200]
                db.session.commit()
            _ttl.pop("landing_wall", None)
    finally:
        _tiles_lock.release()


@app.route("/portada-clip/<int:sid>")
def landing_cover(sid):
    """Portada de un clip para la página pública: solo de clips aprobados (vídeos ya públicos en las redes) y solo la
    que está guardada en la base de datos. No pide nada a las redes desde aquí."""
    s = db.session.get(Submission, sid)
    if not s or s.status != "aprobado" or not (s.thumb_ok and s.thumb_data):
        abort(404)
    resp = Response(s.thumb_data, mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.route("/clip/<int:sid>/portada")
@login_required
def clip_cover(sid):
    """Portada del clip servida desde la web: la fresca de TikTok y, si no, la que dio Apify. Si no hay, 404 (sale la de la campaña)."""
    s = db.session.get(Submission, sid) or abort(404)
    if s.thumb_ok and s.thumb_data:
        return image_response((s.thumb_data, "image/jpeg"))
    for u in ((tiktok_cover(s.url) if s.platform == "tiktok" else ""), s.thumb_url):
        data = fetch_image_guarded(u)
        if data:
            if not s.thumb_ok:                             # se guarda ya: la próxima vez sale de la base de datos
                small = shrink_image(data[0], (240, 320))
                if small:
                    s.thumb_data, s.thumb_ok = small, True
                    db.session.commit()
            return image_response(data)
    abort(404)


@app.route("/cuenta-red/<int:aid>/foto")
@login_required
def account_photo(aid):
    """Foto de perfil de una cuenta de red, servida desde la web (las de TikTok e Instagram no se dejan enlazar y caducan)."""
    acc = db.session.get(CampaignAccount, aid) or abort(404)
    if acc.avatar_ok and acc.avatar_data:
        return image_response((acc.avatar_data, "image/jpeg"))
    data = fetch_image_guarded(acc.avatar_url)
    if not data:
        abort(404)
    return image_response(data)


@app.route("/admin/probar-portadas")
@admin_required
def admin_probe_covers():
    """Panel técnico: prueba con el último clip de TikTok si llegan la portada y la foto de la cuenta."""
    if not is_tech():
        abort(403)
    s = Submission.query.filter_by(platform="tiktok").order_by(Submission.id.desc()).first()
    if not s:
        flash("No hay clips de TikTok para probar.", "error")
        return redirect(url_for("admin_settings", _anchor="lectura"))
    COVER_DIAG.clear()
    _cover_cache.pop(s.url, None)
    th = tiktok_cover(s.url)
    if th:
        _img_cache.pop(th, None)
    img = fetch_image(th) if th else None
    accs = CampaignAccount.query.filter_by(campaign_id=s.campaign_id, platform="tiktok").all()
    with_photo = sum(1 for a in accs if a.avatar_url)
    saved = sum(1 for a in accs if a.avatar_ok)
    n_thumbs = Submission.query.filter_by(thumb_ok=True).count()
    msg = COVER_DIAG.get("oembed_msg")
    flash(f"Clip #{s.id}: TikTok respondió {COVER_DIAG.get('oembed', '—')}{' (' + msg + ')' if msg else ''} · portada {'sí' if th else 'no'}"
          f" · imagen {'descargada' if img else 'no descargada'} ({COVER_DIAG.get('img', '—')})"
          f" · cuentas de TikTok con enlace de foto: {with_photo} de {len(accs)}, con foto guardada: {saved}"
          f" · portadas guardadas en toda la web: {n_thumbs}.", "success" if img else "error")
    return redirect(url_for("admin_settings", _anchor="lectura"))


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
    if not CLIPPER_PUSH:
        return redirect(url_for("account"))
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
        if wants_json():
            return {"ok": True}
        flash("Avisos guardados.", "ok")
        return redirect(url_for("account", _anchor="notificaciones"))
    return redirect(url_for("account", _anchor="notificaciones"))


@app.route("/push/suscribir", methods=["POST"])
@login_required
def push_subscribe():
    if not CLIPPER_PUSH:
        abort(404)
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
    """Avisos a clippers: se eligen de una lista con filtros (mejores, peores, inactivos…), se ve cómo quedará
    y llegan como mensaje a su chat de soporte (y a la campanita / móvil)."""
    camps = Campaign.query.filter(Campaign.status != DIST_STATUS).order_by(Campaign.created_at.desc()).all()
    if request.method == "POST":
        ids = [int(x) for x in request.form.getlist("uids") if x.isdigit()][:5000]
        body = (request.form.get("body") or "").strip()[:1000]
        title = (request.form.get("title") or "").strip()[:80] or "Mensaje del equipo"
        if len(body) < 3:
            flash("Escribe el mensaje.", "error")
            return redirect(url_for("admin_notify"))
        if not ids:
            flash("Elige al menos un clipper de la lista.", "error")
            return redirect(url_for("admin_notify"))
        users = User.query.filter(User.id.in_(ids), User.is_admin.is_(False), User.is_banned.is_(False)).all()
        for u in users:                            # solo al chat de soporte (la campanita lo enseña sola como mensaje)
            text_ = render_for(body, u)
            db.session.add(Message(user_id=u.id, from_admin=True, sender_id=current_user.id, body=text_,
                                   read_by_admin=True, read_by_user=False))
            notify_user(u.id, "mensaje", title, text_, "/soporte", bell=False)
        audit("aviso_enviado", f"{len(users)} clippers · {body[:60]}")
        db.session.commit()
        flash(f"Mensaje enviado a {len(users)} {'clipper' if len(users) == 1 else 'clippers'}.", "ok" if users else "error")
        return redirect(url_for("admin_notify"))
    return render_template("admin/notify.html", camps=camps, push_ready=push_ready(),
                           devices=PushSub.query.count(), people=notify_people(),
                           auto=AUTO_NOTICES, auto_state=auto_notices(), mail_state=auto_mail(), mail_ready=mail_ready(),
                           mail_day=mail_counts()[0] if mail_ready() else 0, MAIL_DAY_MAX=MAIL_DAY_MAX)


@app.route("/admin/avisos/automaticos", methods=["POST"])
@admin_required
def admin_auto_notices():
    """Avisos → «Automáticos»: qué mensajes manda la web sola (una casilla por tipo)."""
    if not admin_can("admin_notify"):
        abort(403)
    state = {k: bool(request.form.get(k)) for k in AUTO_KEYS}
    mail = {k: bool(request.form.get("mail_" + k)) for k in AUTO_KEYS}
    set_setting("auto_notices", json.dumps(state))
    set_setting("auto_mail", json.dumps(mail))
    audit("avisos_automaticos", "campanita: " + (", ".join(k for k, v in state.items() if v) or "ninguno")
          + " · correo: " + (", ".join(k for k, v in mail.items() if v) or "ninguno"))
    db.session.commit()
    if request.headers.get("X-Requested-With") == "fetch":
        return ("", 204)
    flash("Avisos automáticos guardados.", "ok")
    return redirect(url_for("admin_notify", _anchor="automaticos"))


def notify_people():
    """Clippers para la lista de avisos, con lo justo para filtrarlos: vistas y dinero de 30 días, último clip y campañas."""
    since = datetime.utcnow() - timedelta(days=30)
    users = User.query.filter(User.is_admin.is_(False), User.is_banned.is_(False)).order_by(User.created_at.desc()).all()
    stats = {uid: (int(v or 0), float(e or 0), n) for uid, v, e, n in (
        db.session.query(Submission.user_id, db.func.sum(Submission.views),
                         db.func.sum(db.case((Submission.status == "aprobado", Submission.earned), else_=0)),
                         db.func.count(Submission.id))
        .filter(Submission.created_at >= since).group_by(Submission.user_id))}
    last = dict(db.session.query(Submission.user_id, db.func.max(Submission.created_at)).group_by(Submission.user_id).all())
    camps = {}
    for uid, cid in db.session.query(CampaignMember.user_id, CampaignMember.campaign_id).filter(CampaignMember.status == "aceptada"):
        camps.setdefault(uid, []).append(cid)
    now = datetime.utcnow()
    out = []
    for u in users:
        v, e, n = stats.get(u.id, (0, 0.0, 0))
        lc = last.get(u.id)
        out.append({"u": u, "views": v, "earned": round(e, 2), "clips": n, "last": lc,
                    "idle": (now - lc).days if lc else None, "camps": camps.get(u.id, []),
                    "new": (now - (u.created_at or now)).days <= 14})
    active = [p for p in out if p["camps"]]                    # los que están en alguna campaña
    for p in sorted(active, key=lambda p: (-p["earned"], -p["views"]))[:max(5, len(active) // 5)]:
        if p["earned"] > 0 or p["views"] > 0:
            p["best"] = True
    for p in sorted(active, key=lambda p: (p["views"], p["clips"]))[:max(5, len(active) // 5)]:
        if not p.get("best"):
            p["worst"] = True
    for p in out:                                              # actividad: para los filtros de la lista
        i = p["idle"]
        p["act"] = "never" if i is None else "week" if i < 7 else "idle30" if i >= 30 else "idle7"
        p["inactive"] = bool(p["camps"]) and (i is None or i >= 7)
    return sorted(out, key=lambda p: (-p["earned"], -p["views"]))


@app.route("/admin/ajustes/claves-avisos", methods=["POST"])
@admin_required
def admin_vapid_keys():
    """Genera un par de claves para los avisos (no se guarda: se enseña una vez para pegarla en Railway)."""
    priv, pub = webpush.new_vapid_keys()
    return render_template("admin/vapid.html", priv=priv, pub=pub)


BELL_ICONS = {"clip_nuevo": "film", "inactividad": "clock", "mod": "users", "estadisticas": "film", "clip_aprobado": "ok", "clip_rechazado": "x", "campana_nueva": "flag", "plazas": "users", "retiro": "cash",
              "mensaje": "chat", "anuncio": "megaphone", "destacado": "megaphone", "llamada": "bell"}


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
    return redirect(safe_next(n.url or "/"))         # (safe_next también corta «/\\otra-web»)


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


AGENT_ROLES = {"owner": "Responsable", "reviewer": "Revisor", "support": "Soporte"}


def agent_info(u):
    """Quién responde en el chat de soporte (lo ve el clipper): nombre, foto y puesto."""
    if not u:
        return {"name": f"Equipo {APP_NAME}", "role": "Soporte", "avatar": "", "initial": APP_NAME[:1]}
    return {"name": (u.name or "").split()[0] or f"Equipo {APP_NAME}", "role": AGENT_ROLES.get(u.admin_role or "owner", "Soporte"),
            "avatar": u.avatar_url or "", "initial": u.initial}


def support_team():
    """Los que contestan el soporte (para la cabecera del chat): primero soporte, luego el resto."""
    order = {"support": 0, "owner": 1, "reviewer": 2}
    admins = [u for u in User.query.filter_by(is_admin=True).all() if (u.admin_role or "owner") in ("support", "owner")]
    return [agent_info(u) for u in sorted(admins, key=lambda u: order.get(u.admin_role or "owner", 9))[:3]]


def message_json(m, viewer_is_admin):
    mine = m.from_admin == viewer_is_admin
    who = f"Equipo {APP_NAME}" if m.from_admin else m.user.name
    agent = agent_info(m.sender) if m.from_admin and not viewer_is_admin else None
    if agent:
        who = f"{agent['name']} · {agent['role']}"
    return {"id": m.id, "body": m.body, "mine": mine, "agent": agent,
            "image": url_for("chat_image", mid=m.id) if m.image_mime else "", "who": who if not mine else "Tú",
            "from_admin": m.from_admin, "ago": ago(m.created_at), "at": m.created_at.strftime("%d/%m/%Y %H:%M")}


def wants_json():
    return request.headers.get("X-Requested-With") == "fetch"


def process_photo(file):
    """Foto del chat: se comprueba que sea una imagen y se guarda limpia y a un tamaño razonable."""
    from PIL import Image, ImageOps, UnidentifiedImageError
    try:
        img = Image.open(file.stream, formats=IMG_FORMATS)
        img.verify()
        file.stream.seek(0)
        img = ImageOps.exif_transpose(Image.open(file.stream, formats=IMG_FORMATS)).convert("RGB")
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
    return render_template("support.html", msgs=msgs, prefill=prefill, team=support_team(), agent_info=agent_info)


@app.route("/soporte/mini")
@login_required
def support_mini():
    """La conversación de soporte sola, para la ventanita del botón flotante."""
    msgs = conversation(current_user.id)
    mark_read(current_user.id, by_admin=False)
    return render_template("_support_mini.html", msgs=msgs, prefill="", team=support_team(), agent_info=agent_info)



@app.route("/chat/foto/<int:mid>")
@login_required
def chat_image(mid):
    m = db.session.get(Message, mid)
    if not m or not m.image_mime or (m.user_id != current_user.id and not admin_can("admin_chat")):   # también mods con «Mensajes»
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
    return {"live_text": live_text, "showcase": [(c, campaign_stats(c)) for c in camps[:6]], "stats": stats,
            "wall": landing_wall()}


def landing_wall(n=24):
    """Portadas para las columnas de la portada pública: clips aprobados con su portada ya guardada, de más vistas a
    menos (solo enlaces internos: aquí nunca se sale a buscar nada fuera). Si hay pocas, se completan con las portadas
    de las campañas activas. [(url, vistas o None)]"""
    def load():
        picked = (db.session.query(LandingTile.id, LandingTile.views).filter(LandingTile.status == "ok")
                  .order_by(LandingTile.id).all())
        if picked:            # las imágenes que ya se eligieron a mano siguen saliendo (Mustapha, 24 nov: quitar el
            return [(url_for("landing_tile_img", tid=tid), views) for tid, views in picked]   # apartado no era quitarlas)
        rows = (db.session.query(Submission.id, Submission.views)
                .filter(Submission.status == "aprobado", Submission.thumb_ok.is_(True))
                .order_by(Submission.views.desc()).limit(n).all())
        out = [(url_for("landing_cover", sid=sid), views) for sid, views in rows]
        if len(out) < 8:
            out += [(cover_src(c, 480), None) for c in active_campaigns() if c.cover_version][: 8 - len(out)]
        return out
    return ttl_cache("landing_wall", 600, load)


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


LANDING_ORIGINS = {o.strip().rstrip("/") for o in os.environ.get(
    "LANDING_ORIGINS", "https://clipprofit.es,https://www.clipprofit.es").split(",") if o.strip()}


def save_lead(form, ip):
    """Guarda un creador interesado. Devuelve el texto del error, o None si ha ido bien."""
    recent = CampaignLead.query.filter(CampaignLead.ip == ip,
                                       CampaignLead.created_at >= datetime.utcnow() - timedelta(hours=1)).count()
    if not form.get("name") or not form.get("brand"):
        return "Escribe tu nombre y el de tu canal."
    email = form.get("email", "")
    if "@" not in email or "." not in email.split("@")[-1]:
        return "Escribe un correo válido para poder responderte."
    if recent >= 5:
        return "Has enviado varias solicitudes seguidas. Inténtalo más tarde."
    budget = form.get("budget", "")
    extra = [f"{k}: {v}" for k, v in form.get("extra", {}).items() if v]
    message = "\n".join(filter(None, [form.get("message", ""), *extra]))
    db.session.add(CampaignLead(
        name=form["name"][:120], email=email[:255].lower(), brand=form["brand"][:200], category="streamer",
        budget=budget[:40], link=form.get("link", "")[:500], message=message[:3000], ip=ip))
    db.session.commit()
    log.info("Nuevo creador interesado: %s", form["brand"][:60])
    return None


@app.route("/api/creador-interesado", methods=["POST", "OPTIONS"])
def landing_lead():
    """Recibe el formulario de la landing (clipprofit.es) y lo guarda en «Creadores interesados».
    Solo admite llamadas desde la landing (LANDING_ORIGINS); sin JavaScript, vuelve a la landing con ?enviado=1."""
    origin = (request.headers.get("Origin") or "").rstrip("/")
    allowed = origin in LANDING_ORIGINS

    def answer(body, code=200):
        resp = jsonify(body) if body is not None else Response(status=204)
        if body is not None:
            resp.status_code = code
        if allowed:
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
            resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
            resp.headers["Vary"] = "Origin"
        return resp

    if request.method == "OPTIONS":
        return answer(None)
    if origin and not allowed:
        return answer({"ok": False, "error": "Origen no permitido."}, 403)
    data = request.get_json(silent=True) if request.is_json else request.form
    data = data or {}
    get = lambda k: " ".join(str(data.get(k) or "").split()) if k != "message" else str(data.get(k) or "").strip()
    known = ("name", "email", "brand", "budget", "link", "message", "website")
    form = {k: get(k) for k in known if k != "website"}
    form["extra"] = {str(k)[:40]: " ".join(str(v).split())[:300] for k, v in list(data.items())[:20]
                     if k not in known and k != "_csrf" and str(v).strip()}
    if str(data.get("website") or "").strip():               # campo trampa: solo lo rellenan los bots
        return answer({"ok": True})
    error = save_lead(form, (request.remote_addr or "")[:64])
    if not request.is_json and not origin:                    # formulario normal sin JavaScript
        back = (request.referrer or "https://clipprofit.es").split("?")[0]
        return redirect(back + ("?enviado=1" if not error else "?error=1"))
    return answer({"ok": not error, "error": error}, 400 if error else 200)


# ---------------------------------------------------------------- reservar llamada (sustituye a Calendly en la landing)
BOOK_DEFAULT = {"days": [0, 1, 2, 3, 4], "start": "10:00", "end": "20:00", "slot": 30, "ahead": 3, "horizon": 14}
WEEKDAYS = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]


def book_cfg():
    try:
        cfg = {**BOOK_DEFAULT, **json.loads(get_setting("booking", "") or "{}")}
    except ValueError:
        cfg = dict(BOOK_DEFAULT)
    return cfg


def _hm(s):
    h, m = (s or "0:0").split(":")
    return int(h) * 60 + int(m)


def book_slots(now=None):
    """Huecos libres (UTC, sin zona) según el horario de «Creadores interesados» (hora de España)."""
    cfg, tz = book_cfg(), ZoneInfo(APP_TZ)
    now = (now or datetime.utcnow()).replace(tzinfo=timezone.utc)
    first = now + timedelta(hours=cfg["ahead"])
    taken = {l.meet_at for l in CampaignLead.query.filter(CampaignLead.meet_at >= now.replace(tzinfo=None),
                                                         CampaignLead.status != "descartada").all()}
    out, today = [], now.astimezone(tz).date()
    for d in range(cfg["horizon"] + 1):
        day = today + timedelta(days=d)
        if day.weekday() not in cfg["days"]:
            continue
        m = _hm(cfg["start"])
        while m + cfg["slot"] <= _hm(cfg["end"]):
            at = datetime(day.year, day.month, day.day, m // 60, m % 60, tzinfo=tz).astimezone(timezone.utc)
            if at >= first and at.replace(tzinfo=None) not in taken:
                out.append(at.replace(tzinfo=None))
            m += cfg["slot"]
    return out


BOOK_FRAME_ORIGINS = LANDING_ORIGINS | {o.strip().rstrip("/") for o in os.environ.get(
    "BOOK_FRAME_EXTRA", "https://cool-pavlova-5ecc27.netlify.app").split(",") if o.strip()}   # la de Netlify, solo para pruebas


def _framable(resp):
    """Solo /reservar: la landing (clipprofit.es y la copia de pruebas) puede meterla dentro de la suya.
    Funciona sin sesión (no usa cookies), así que da igual que el navegador las bloquee dentro del iframe."""
    anc = " ".join(sorted(BOOK_FRAME_ORIGINS))
    resp.headers["Content-Security-Policy"] = CSP.replace("frame-ancestors 'none'", f"frame-ancestors 'self' {anc}")
    g._framable = True                              # sin «X-Frame-Options: DENY» (ver security_headers)
    return resp


@app.route("/reservar", methods=["GET", "POST"])
def book_call():
    form, error, done = {}, None, None
    if request.method == "POST":
        form = {k: " ".join((request.form.get(k) or "").split()) for k in ("name", "phone", "email", "brand", "budget", "link", "at",
                                                                          "plan", "boton")}
        form["message"] = (request.form.get("message") or "").strip()[:3000]
        # Lo que el creador ya rellenó en la landing y no tiene casilla propia (llega como x_<pregunta>)
        form["extra"] = [(k[2:].replace("_", " ")[:60], " ".join(v.split())[:500]) for k, v in request.form.items()
                         if k.startswith("x_") and v.strip()][:20]
        if request.form.get("website"):                       # campo trampa: solo lo rellenan los bots
            return _framable(Response(render_template("book.html", slots=[], form={}, done=datetime.utcnow(), error=None, book_minutes=30)))
        ip = (request.remote_addr or "")[:64]
        recent = CampaignLead.query.filter(CampaignLead.ip == ip,
                                           CampaignLead.created_at >= datetime.utcnow() - timedelta(hours=1)).count()
        digits = re.sub(r"\D", "", form["phone"])
        try:
            at = datetime.strptime(form["at"], "%Y-%m-%dT%H:%M")
        except ValueError:
            at = None
        if not at or at not in book_slots():
            error = "Esa hora ya no está libre. Elige otra."
        elif len(form["name"]) < 2:
            error = "Escribe tu nombre."
        elif not 8 <= len(digits) <= 15:
            error = "Escribe tu WhatsApp con el prefijo del país (por ejemplo, +34 600 000 000)."
        elif "@" not in form["email"] or "." not in form["email"].split("@")[-1]:
            error = "Escribe un correo válido."
        elif recent >= 5:
            error = "Has hecho varias reservas seguidas. Inténtalo más tarde."
        if not error:
            phone = ("+" if form["phone"].startswith("+") or len(digits) > 9 else "") + digits
            link = form["link"] or (form["brand"] if "." in form["brand"] else "")
            message = "\n".join(filter(None, [form["message"]] + [f"{k}: {v}" for k, v in form["extra"]]))
            db.session.add(CampaignLead(tz=clean_tz(request.form.get("tz")), name=form["name"][:120], email=form["email"][:255].lower(),
                                        brand=(form["brand"] or form["name"])[:200], category="streamer",
                                        budget=form["budget"][:40], link=link[:500], message=message[:3000],
                                        phone=phone[:30], meet_at=at, ip=ip, plan=form["plan"][:80], button=form["boton"][:80]))
            db.session.commit()
            log.info("Llamada reservada: %s", form["name"][:60])
            done = at
    if request.method == "GET":                               # la landing puede decir el plan y el botón en la dirección
        form = {"plan": " ".join((request.args.get("plan") or "").split())[:80],
                "boton": " ".join((request.args.get("boton") or "").split())[:80]}
    slots = [] if done else [x.strftime("%Y-%m-%dT%H:%M") for x in book_slots()]
    return _framable(Response(render_template("book.html", slots=slots, form=form, done=done, error=error,
                                              origins=" ".join(sorted(BOOK_FRAME_ORIGINS)),
                                              book_minutes=book_cfg()["slot"]),
                              status=400 if error else 200))


@app.route("/lanzar-campana", methods=["GET", "POST"])
def launch():
    form = {}
    if request.method == "POST":
        form = {k: (request.form.get(k) or "").strip() for k in ("name", "email", "brand", "category", "budget", "link", "message")}
        if request.form.get("website"):                       # campo trampa: solo lo rellenan los bots
            return redirect(url_for("launch_thanks"))
        ip = (request.remote_addr or "")[:64]          # ProxyFix ya la toma del proxy de Railway
        if form["budget"] not in BUDGETS:
            form["budget"] = ""
        error = save_lead(form, ip)
        if error:
            flash(error, "error")
        else:
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
        if status == "borrar":
            audit("solicitud_borrada", f"#{lead.id} {lead.name}")
            db.session.delete(lead)
            db.session.commit()
            flash("Reserva borrada.", "ok")
            return redirect(url_for("admin_leads"))
        if status in LEAD_STATUSES:
            lead.status = status
            audit("solicitud_actualizada", f"#{lead.id} {lead.brand} → {LEAD_STATUSES[status]}")
            db.session.commit()
        return redirect(url_for("admin_leads"))
    leads = CampaignLead.query.order_by(CampaignLead.created_at.desc()).all()
    leads.sort(key=lambda l: l.status != "nueva")
    from collections import Counter
    by_plan = Counter(l.plan or "Sin plan" for l in leads if l.meet_at).most_common()
    by_button = Counter(l.button or "Sin indicar" for l in leads if l.meet_at).most_common()
    return render_template("admin/leads.html", leads=leads, statuses=LEAD_STATUSES, cfg=book_cfg(), weekdays=WEEKDAYS,
                           by_plan=by_plan, by_button=by_button,
                           madrid=lambda d: d.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(APP_TZ)), lead_times=lead_times)


_js_errs = {}


@app.route("/api/error-navegador", methods=["POST"])
def browser_error():
    """Fallos de JavaScript en el navegador de cualquiera (los manda app.js solo). Como mucho 20 por hora y dirección."""
    ip, now = (request.remote_addr or "")[:64], time.time()
    hits = [t for t in _js_errs.get(ip, []) if now - t < 3600]
    if len(hits) >= 20:
        stat_add(blocked=1)
        return "", 204
    _js_errs[ip] = hits + [now]
    if len(_js_errs) > 5000:
        _js_errs.clear()
    d = request.get_json(silent=True) or {}
    msg = " ".join(str(d.get("msg") or "").split())[:180]
    if not msg or "ResizeObserver" in msg or "Script error" == msg.strip("."):
        return "", 204
    detail = (f"página={str(d.get('page') or '')[:300]}\narchivo={str(d.get('src') or '')[:300]}:{d.get('line')}:{d.get('col')}\n"
              f"navegador={request.headers.get('User-Agent', '')[:300]}\n\n{str(d.get('stack') or '')[:4000]}")
    page = urlparse(str(d.get("page") or "")).path[:300]
    note_error(msg, detail, kind="navegador", path=page)
    return "", 204


@app.route("/admin/errores")
@admin_required
def admin_errors():
    if not admin_can("admin_errors"):
        abort(403)
    q = (request.args.get("codigo") or "").strip().upper().lstrip("#")
    tipo = request.args.get("tipo", "")
    base = ErrorLog.query
    if q:
        base = base.filter(ErrorLog.code == q)
    elif tipo in ERROR_KINDS:
        base = base.filter(ErrorLog.kind == tipo)
    rows = base.order_by(db.func.coalesce(ErrorLog.last_at, ErrorLog.created_at).desc()).limit(200).all()
    day = datetime.utcnow() - timedelta(hours=24)
    counts = dict(db.session.query(ErrorLog.kind, db.func.count()).filter(
        db.func.coalesce(ErrorLog.last_at, ErrorLog.created_at) >= day).group_by(ErrorLog.kind).all())
    users = {u.id: u for u in User.query.filter(User.id.in_({r.user_id for r in rows if r.user_id} or {0})).all()}
    return render_template("admin/errors.html", rows=rows, q=q, users=users, tipo=tipo, kinds=ERROR_KINDS, counts=counts)


@app.route("/admin/solicitudes/<int:lid>/whatsapp")
@admin_required
def admin_lead_whatsapp(lid):
    """Abre WhatsApp con el creador y la marca como contactada (así deja de contar en el menú)."""
    lead = db.session.get(CampaignLead, lid) or abort(404)
    if lead.status == "nueva":
        lead.status = "contactada"
        db.session.commit()
    when = lead_times(lead)
    if when:                                         # con la hora en SU zona (25 nov, Mustapha)
        d = when["theirs"] or when["spain"]
        text = (f"Hola {lead.name}, soy de {APP_NAME}. Te paso el enlace de nuestra llamada del {WEEKDAYS_ES[d.weekday()]} "
                f"{d.day}/{d.month} a las {d.strftime('%H:%M')}{' (tu hora)' if when['theirs'] else ' (hora de España)'}: ")
    else:
        text = f"Hola {lead.name}, soy de {APP_NAME}. Te paso el enlace de nuestra llamada: "
    digits = re.sub(r"\D", "", lead.phone or "")
    return redirect(f"https://wa.me/{digits}?text={quote(text)}")


# País de la zona horaria del navegador (25 nov, Mustapha: «¿la web detecta de qué país es?»). La zona sale del
# dispositivo del creador, no de su IP: es la que tiene configurada su móvil u ordenador.
TZ_COUNTRY = {
    "Europe/Madrid": "España", "Atlantic/Canary": "España (Canarias)", "Africa/Ceuta": "España",
    "America/Mexico_City": "México", "America/Monterrey": "México", "America/Merida": "México", "America/Cancun": "México",
    "America/Tijuana": "México", "America/Chihuahua": "México", "America/Hermosillo": "México", "America/Mazatlan": "México",
    "America/Matamoros": "México", "America/Bahia_Banderas": "México", "America/Ciudad_Juarez": "México",
    "America/Bogota": "Colombia", "America/Lima": "Perú", "America/Santiago": "Chile", "America/Punta_Arenas": "Chile",
    "America/Caracas": "Venezuela", "America/Guayaquil": "Ecuador", "America/La_Paz": "Bolivia", "America/Asuncion": "Paraguay",
    "America/Montevideo": "Uruguay", "America/Guatemala": "Guatemala", "America/El_Salvador": "El Salvador",
    "America/Tegucigalpa": "Honduras", "America/Managua": "Nicaragua", "America/Costa_Rica": "Costa Rica",
    "America/Panama": "Panamá", "America/Havana": "Cuba", "America/Santo_Domingo": "República Dominicana",
    "America/Puerto_Rico": "Puerto Rico", "Europe/Lisbon": "Portugal", "Europe/London": "Reino Unido", "Europe/Paris": "Francia",
    "Europe/Berlin": "Alemania", "Europe/Rome": "Italia", "Europe/Brussels": "Bélgica", "Europe/Amsterdam": "Países Bajos",
    "Europe/Zurich": "Suiza", "Europe/Andorra": "Andorra", "Africa/Casablanca": "Marruecos", "America/Sao_Paulo": "Brasil",
    "America/New_York": "Estados Unidos (Este)", "America/Chicago": "Estados Unidos (Centro)",
    "America/Denver": "Estados Unidos (Montaña)", "America/Phoenix": "Estados Unidos (Arizona)",
    "America/Los_Angeles": "Estados Unidos (Pacífico)", "America/Toronto": "Canadá", "America/Vancouver": "Canadá",
}
WEEKDAYS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def clean_tz(raw):
    """Zona horaria que manda el navegador, solo si existe de verdad (si no, vacía)."""
    raw = (raw or "").strip()[:64]
    if not raw or not re.fullmatch(r"[A-Za-z_]+(?:/[A-Za-z0-9_+-]+){0,2}", raw):
        return ""
    try:
        ZoneInfo(raw)
    except Exception:  # noqa: BLE001
        return ""
    return raw


def tz_place(tz):
    """«México», «Argentina»… o la ciudad de la zona si no está en la lista («Kiev»)."""
    if not tz:
        return ""
    if tz.startswith("America/Argentina/") or tz == "America/Buenos_Aires":
        return "Argentina"
    return TZ_COUNTRY.get(tz) or tz.rsplit("/", 1)[-1].replace("_", " ")


def lead_times(lead):
    """La hora de la llamada en España y, si el creador es de otra zona, en la suya: {spain, theirs, place, same}."""
    if not lead.meet_at:
        return None
    at = lead.meet_at.replace(tzinfo=timezone.utc)
    spain = at.astimezone(ZoneInfo(APP_TZ))
    theirs, place = None, tz_place(lead.tz)
    if lead.tz and lead.tz not in ("Europe/Madrid", "Africa/Ceuta"):
        t = at.astimezone(ZoneInfo(lead.tz))
        if t.utcoffset() != spain.utcoffset():
            theirs = t
    return {"spain": spain, "theirs": theirs, "place": place,
            "same": bool(lead.tz) and theirs is None and lead.tz not in ("Europe/Madrid", "Africa/Ceuta")}


@app.route("/admin/solicitudes/horario", methods=["POST"])
@admin_required
def admin_book_hours():
    f = request.form
    try:
        start, end = f.get("start", ""), f.get("end", "")
        cfg = {"days": sorted({int(d) for d in f.getlist("days") if d in "0123456"}), "start": start, "end": end,
               "slot": int(f.get("slot", 30)), "ahead": int(f.get("ahead", 3)), "horizon": int(f.get("horizon", 14))}
        assert re.match(r"^\d{2}:\d{2}$", start) and re.match(r"^\d{2}:\d{2}$", end) and _hm(start) < _hm(end)
        assert cfg["slot"] in (15, 20, 30, 45, 60) and 0 <= cfg["ahead"] <= 72 and 1 <= cfg["horizon"] <= 60 and cfg["days"]
    except (ValueError, AssertionError):
        flash("Revisa el horario: elige al menos un día y una hora de fin posterior a la de inicio.", "error")
        return redirect(url_for("admin_leads"))
    set_setting("booking", json.dumps(cfg))
    audit("horario_llamadas", json.dumps(cfg))
    flash("Horario de llamadas guardado.", "ok")
    return redirect(url_for("admin_leads"))


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
    if refresh_pending():                    # la lectura de pago solo una vez al día, aunque el aviso externo llame más
        def run():
            with app.app_context():
                run_daily_refresh()
        threading.Thread(target=run, daemon=True).start()
        return "ok"
    return "ok (ya se leyó hoy)"


if __name__ == "__main__":
    app.run(debug=not PRODUCTION, port=int(os.environ.get("PORT", 5000)))


with app.app_context():                 # al arrancar, cuando ya está todo definido
    rename_beginner_course()
