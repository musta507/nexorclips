# Auditoría de ingeniería — 24 de noviembre de 2026

Revisión del código real de ClipProfit Hispano: `app.py` (12.000 líneas), `views.py`, `official.py`, 82 plantillas, `app.js`
y `style.css`. Se revisaron las 157 rutas una a una (quién puede entrar y si comprueba que el objeto es suyo), el dinero de
principio a fin, las subidas de archivos, las llamadas a servicios externos y el rendimiento con una copia de 10.027 usuarios
y 66.223 clips (`cpbig`, nunca contra la web real). Todas las pantallas se recorrieron con navegador en PC y móvil: sin
errores de JavaScript, sin fallos 500 y sin desbordes horizontales.

Cada arreglo lleva su prueba (`tests/test_platform.py`, las que empiezan por `test_audit_`). Suite completa en verde en
SQLite y en Postgres: 218 pruebas.

## Arreglado (por gravedad e impacto económico)

| # | Gravedad | Problema | Arreglo |
|---|---|---|---|
| 1 | Crítica | **Pago doble si Whop no respondía.** Un corte de conexión, un tiempo agotado o un fallo 5xx cancelaba el pago y «devolvía» el dinero al clipper, aunque Whop sí lo hubiera enviado. Al pagarle otra vez salía un pago nuevo con otra clave: cobraba dos veces. | `_whop_apply`: sin respuesta clara (0, 429, 5xx) el pago queda «solicitado» con `WHOP_WAIT_NOTE` y se reintenta con la misma clave de idempotencia. «Seguir pagando» / «Reintentar» en Pagos. Un error inesperado en `run_campaign_payouts` tampoco libera el dinero. |
| 2 | Crítica | **«Borrar usuarios de prueba» borraba a todos los clippers reales** (con lo que se les debe) con escribir ELIMINAR; lo podía hacer cualquier dueño. | Solo Panel técnico (`TECH_ENDPOINTS`) y solo con `ALLOW_WIPE=1`, igual que «Borrar datos». |
| 3 | Alta | **Se podía cobrar el vídeo viral de otra persona.** Un enlace de Instagram o YouTube enviado a mano no trae el autor hasta la primera lectura; `account_verified` lo daba por bueno si el clipper tenía cualquier cuenta aceptada. | Sin autor conocido no se puede aprobar (mensaje claro en revisión). «Cuenta no vinculada» (`cuenta_no_declarada`) cuenta como alerta roja en Pagos. |
| 4 | Alta | Un mod con la función «Clippers» (solo ver) podía pagar por Whop, marcar como pagado o rechazar retiros desde `/admin/clippers`. | El POST de esa pantalla exige el permiso de Retiros. |
| 5 | Alta | Un mod con «Mods de campaña» podía subirse a sí mismo permisos de dinero en una campaña. | No puede cambiar ni quitar sus propias filas. |
| 6 | Alta | **Panel técnico: 21,5 s y 22.312 consultas** con 10.000 usuarios (cargaba todos los usuarios y sus clips uno a uno; y la tarea de copias de Bunny, cada 10 min, hacía 2 consultas por clip pendiente). | Cuenta en la base de datos y lista de los 50 últimos; clips con su usuario y campaña de una vez. Ahora ~1 s y 183 consultas. |
| 7 | Alta | Pillow 12.3.0 con 42 fallos conocidos (imágenes trucadas: caída de la web y, en el peor caso, ejecutar código). | Pillow 13.0.0 y solo se abren JPEG, PNG, WEBP y GIF (`IMG_FORMATS`). `pip-audit`: 0 fallos conocidos. |
| 8 | Media | Robots registrándose sin parar gastaban los 100 correos del día de Resend (nadie más podía confirmar su cuenta); y se podía llenar el correo de otro con «He olvidado mi contraseña». | `SIGNUP_HOUR_MAX` (30 altas sin confirmar por hora entre todos) y `RECOVER_HOUR_MAX` (3 por cuenta y hora). |
| 9 | Media | Contraseñas: el límite de intentos era solo por IP. | También por cuenta (`em:<correo>`). |
| 10 | Media | Las portadas y fotos se descargan de direcciones que vienen de terceros: se podía hacer que la web pidiera cosas de su red interna. | `public_host`: solo internet (ni localhost, ni IPs privadas, ni `*.internal`), también en cada redirección. |
| 11 | Media | La clave de Apify se mandaba a cualquier dirección que contuviera «api.apify.com» (por ejemplo, en un parámetro). | Solo si el servidor es exactamente `api.apify.com`. |
| 12 | Media | Cada página del panel del técnico preguntaba a Whop su saldo cada 2 min: si Whop iba lento, el panel se quedaba esperando. | `tech_alert_count` guardado 2 min. |
| 13 | Baja | Doble clic en «Enviar clip» o «Unirme» daba error 500. | Se captura el duplicado y sale el mensaje normal. |
| 14 | Baja | Lista de clippers del panel: 2 consultas por fila. | 3 consultas en total (147 → 31). |
| 15 | Baja | Los mods con «Mensajes» no veían las fotos del chat; los mods de campaña no veían las imágenes del hub. | Arreglado. |
| 16 | Baja | Redirección a otra web con «/\\…» en dos sitios. | `safe_next`. |
| 17 | Baja | Textos que aún decían «Retirar» con los pagos por campaña (menú, inicio, cierre de campaña, avisos automáticos). | Coherentes con `CLIPPER_WITHDRAW`. |
| 18 | Baja | Menú del panel: la pestaña de invitaciones apuntaba a una pantalla que no existe. Faltaba índice en la actividad de cada mod. | Arreglado. |

## Sin arreglar (necesitan una decisión o más trabajo)

| Gravedad | Problema | Propuesta |
|---|---|---|
| Alta (rendimiento) | **Avisos carga a todos los clippers** en la página: 2,4 s y varios MB con 10.000. | Filtros y lista en el servidor, por tandas. Cambia cómo se elige a quién escribir: proponer diseño antes. |
| Media | El pago va al Whop que tiene el clipper en el momento de enviarlo: si le roban la cuenta y lo cambia, el dinero iría al ladrón. | Aviso «cambió su Whop hace poco» en Pagos y pagar al Whop guardado al marcar. |
| Media | Sin confirmación extra para pagos grandes ni segundo factor propio. | Escribir la cantidad por encima de un tope; código por correo antes de pagar. |
| Media (externo) | Lo que más protege el dinero está fuera del código: Google, Railway, GitHub y Whop sin verificación en dos pasos. | Activarla en todas, de Mustapha y de Felipe. |
| Media | Las copias de cada noche llevan correos y contraseñas cifradas sin cifrar el archivo. | Cifrar la copia antes de subirla a Bunny. |
| Media (escala) | «Todos los clips» carga todos los pendientes (0,5 s con 6.500). La lectura de la noche corre dentro de la web. Los límites de intentos viven en la memoria de cada proceso (con 2 procesos, cuentan la mitad). | Ya en el plan de auditoría de rendimiento y arquitectura modular. |
| Baja | Salir de la sesión es un enlace (GET): otra web puede cerrar tu sesión. El registro dice si un correo ya existe. | Pequeños; decidir si merece la pena. |
| Calidad | `app.py` de 12.000 líneas; 3 funciones casi iguales para procesar imágenes; 2 bloques muertos (`if False`); 136 `datetime.utcnow()` (obsoleto en Python 3.12+); hoja de estilos con parches. | Modularizar por temas (pendiente acordado). |

## Funciones incompletas (lo que existe pero no funciona del todo)

- **Pago real por Whop: nunca probado.** Todo está probado con un Whop simulado. Falta la prueba de 0,50–1 $.
- **App de Whop en una cuenta que no es de ClipProfit** (permiso de pagos sin verificar la empresa).
- **APIs oficiales** de TikTok, Instagram, YouTube y Facebook: código hecho, sin claves ni revisión de cada red.
- Avisos al móvil apagados; modo oscuro apagado; Sentry sin `SENTRY_DSN`; datos legales del titular vacíos.
- Opciones quitadas que siguen en el código: fecha de inicio de campaña y «solo cuentas nuevas» (`if False`).
