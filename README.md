# NexorClips — Plataforma de clipping

Artistas, streamers y marcas lanzan campañas. Los clippers entran con Google, publican clips desde sus cuentas y cobran por cada vista.

## Qué incluye

**Para los clippers**
- **Home:** campaña destacada, campañas activas con filtros por tipo y barra del presupuesto que queda.
- **Enviar clips:** se elige la campaña, se pega el enlace (también los enlaces cortos de TikTok) y se ve el estado y lo ganado en cada clip.
- **Ganancias:** saldo disponible, pendiente y total, historial y botón **Retirar** (mínimo 30 USD, un retiro a la vez).
- **Mi cuenta:** cuentas de TikTok, Instagram y YouTube vinculadas, y cuenta de cobro.

**Dinero que cuadra siempre (libro de cuentas)**
- Cada clip guarda lo que ha ganado. Esa cifra nunca baja sola: solo la cambia el admin al corregir vistas o rechazar un clip.
- El presupuesto se reparte por orden de envío y nunca se supera. Al agotarse, la campaña se cierra sola y deja de gastar Apify.
- Fórmula: vistas ÷ 1.000.000 × tarifa, con mínimo de vistas y tope por clip.

**Antifraude**
- Solo se aceptan vídeos de cuentas vinculadas por el clipper, y cada cuenta solo puede estar vinculada a una persona.
- La web comprueba el autor real de cada vídeo y avisa si es de otra cuenta.
- Guarda un historial de vistas y avisa de subidas sospechosas o de clips con muy pocos likes para sus vistas.
- Límite de clips por día, bloqueo de clippers y registro de todas las acciones del admin.

**Para ti (panel de admin)**
- Cifras clave: presupuesto activo, repartido, pagado, retiros por pagar, vistas y clippers.
- Revisión de clips con avisos, corrección de vistas, retiros, bloqueos y actividad.
- **Informe para el artista:** cada campaña tiene un enlace privado con sus resultados en directo, sin correos ni datos de pago.
- Exportación a CSV (Excel) de clips por campaña y de retiros.
- **Probar Apify con un enlace:** comprueba en un minuto si se leen bien las vistas.
- **Mensajes:** chat privado con cada clipper. Ellos escriben desde «Soporte» (menú del avatar) y tú respondes desde tu bandeja, con avisos de no leídos y mensajes que llegan solos sin recargar. Tus respuestas salen firmadas como «Equipo NexorClips».
- **Cobros:** de momento solo en USDT por la red TRC20. Si un clipper elige PayPal o transferencia y pulsa «Agregar cuenta», se le avisa de que solo está disponible cripto. La dirección se valida (empieza por T, 34 caracteres).
- **Verificación de cuentas:** cada cuenta de redes que añade un clipper queda «Pendiente de verificar». Él pone su código (NX-XXXX) en la bio y te avisa por el chat de soporte; tú abres su chat en Mensajes y pulsas «Verificar» o «Rechazar». Sus clips se pueden enviar, pero no se pueden aprobar ni pagar hasta que la cuenta esté verificada.
- **Avisos al móvil:** con la app gratuita ntfy y la variable `NTFY_TOPIC`, te llega una notificación cuando un clipper te escribe, pide unirse a una campaña o solicita un retiro, y cuando una marca pide lanzar campaña.
- **Portada y solicitudes:** quien entra sin sesión ve una portada corta con dos caminos: «Empezar a clipear» (login con Google) o «Lanzar una campaña» (formulario para artistas y marcas, con protección anti-spam). Las solicitudes llegan a Admin → Solicitudes; desde ahí respondes por correo y pulsas «Crear campaña» con los datos ya rellenados.

**Seguridad**
- Login solo con Google. Protección de formularios, cookies seguras, cabeceras de seguridad, imágenes revisadas y re-guardadas, páginas de error propias y comprobación de salud en `/salud`.

---

## Cómo ponerla en internet

### 1. Subir el código a GitHub
1. En github.com → **+** → **New repository** → nombre `nexorclips` → **Private** → **Create repository**.
2. Pulsa **uploading an existing file** y arrastra **todo lo que hay dentro** de la carpeta `nexorclips`.
3. Pulsa **Commit changes**.

### 2. Crear el proyecto en Railway
Puedes usar la misma cuenta y suscripción que en ClipLinks.
1. **New Project** → **Deploy from GitHub repo** → `nexorclips`.
2. En el mismo proyecto: **+ New** → **Database** → **PostgreSQL**.
3. Servicio `nexorclips` → **Settings** → **Networking** → **Generate Domain**. Copia la dirección.

### 3. Activar "Continuar con Google"
1. Entra en **console.cloud.google.com** → **Proyecto nuevo** → `nexorclips`.
2. **APIs y servicios** → **Pantalla de consentimiento de OAuth** → **Externo** → nombre `NexorClips` y tu correo → guardar hasta el final → **Publicar aplicación**.
3. **APIs y servicios** → **Credenciales** → **Crear credenciales** → **ID de cliente de OAuth** → **Aplicación web**.
4. En **URIs de redireccionamiento autorizados** añade: `https://TU-DOMINIO/auth/google/callback`
5. **Crear** y copia el **ID de cliente** y el **Secreto**.

### 4. Variables en Railway
Servicio `nexorclips` → **Variables**:

| Variable | Valor |
|---|---|
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` |
| `SECRET_KEY` | una frase larga inventada (obligatoria: sin ella la web no arranca) |
| `BASE_URL` | `https://TU-DOMINIO` (sin barra al final) |
| `ADMIN_EMAIL` | tu Gmail |
| `SUPPORT_EMAIL` | el correo de soporte que verán los clippers |
| `GOOGLE_CLIENT_ID` | del paso 3 |
| `GOOGLE_CLIENT_SECRET` | del paso 3 |
| `APIFY_TOKEN` | tu token de Apify |
| `CRON_SECRET` | otra palabra secreta inventada |

Opcionales: `MIN_WITHDRAW` (30), `MAX_CLIPS_DAY` (30), `PAYOUT_DAYS` (7), `REFRESH_DAYS` (14), `APP_NAME` (NexorClips).

### 5. Primer uso
1. Abre tu web y entra con el Gmail de `ADMIN_EMAIL`.
2. Avatar → **Panel de admin** → **Probar Apify con un enlace** con un vídeo real de cada red.
3. **+ Nueva campaña**, y comparte el enlace con tus clippers.

### 6. Actualización automática de vistas
En **cron-job.org** crea una tarea que llame **cada 12 horas** a:
`https://TU-DOMINIO/cron/actualizar?key=TU_CRON_SECRET`

---

## Pruebas automáticas
El código incluye 25 pruebas que comprueban el reparto del dinero, los retiros, el antifraude y la seguridad.
```
pip install -r requirements.txt pytest
python -m pytest
```

## Antes de crecer
- Los textos de **Términos** y **Privacidad** son una base sensata, pero revísalos con un abogado antes de mover mucho dinero, sobre todo en lo fiscal.
- Haz copias de seguridad de la base de datos desde Railway (PostgreSQL → Backups).

## Si algo falla
- **"redirect_uri_mismatch" con Google:** la dirección del paso 3.4 debe coincidir exactamente con tu dominio y terminar en `/auth/google/callback`.
- **La web no arranca:** mira los logs del servicio en Railway. Si dice que falta `SECRET_KEY`, añádela.
- **Las vistas no se leen:** usa **Probar Apify con un enlace**. Si Apify responde pero no se encuentran las vistas, copia el resultado y se ajusta. Los actores se pueden cambiar con `APIFY_ACTOR_TIKTOK`, `APIFY_ACTOR_INSTAGRAM` y `APIFY_ACTOR_YOUTUBE`.
