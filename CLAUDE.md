# ClipProfit Hispano

Plataforma de clipping (pago por vistas) que Mustapha desarrolla para un cliente, Felipe.
Repositorio `musta507/nexorclips`, rama `main`. Cada push a `main` se despliega solo en Railway.

## Cómo trabajar con Mustapha

- Responde en español, corto y sin jerga. No programa: explica qué cambia para quien usa la web, no cómo está hecho.
- Cambios grandes o de gusto (diseño): proponlos numerados y espera a «ejecuta», «hazlo» o «sí». Arreglos de un fallo claro: hazlos directamente.
- En diseño, pide una referencia o una captura antes de cambiar el aspecto de algo. No rediseñes lo que no se ha pedido.
- Avisa de los riesgos y de lo que cuesta dinero (Apify, Bunny) antes de hacerlo.
- Di siempre qué has comprobado y qué no.
- Nunca pidas ni repitas claves. Las claves van en las variables de Railway.
- Lo técnico (Panel técnico) solo lo ve Mustapha.

## El negocio

- Los clientes son creadores de contenido. En la web, en textos y en documentos se les llama solo «creadores».
- Escala prevista: 10 campañas activas (ampliable a 20), 30–100 clippers por campaña, unos 4.000 usuarios registrados, la mayoría inactivos.
- Presupuesto de servicios: menos de 100 $ al mes.
- Cada cuenta de un clipper es de una sola campaña: todo lo que publica después de entrar cuenta como clip de esa campaña.
- La grabación de verificación de vistas solo se pide a los clips que pasan el mínimo.
- Referencia de producto: Content Rewards (Whop). Ante la duda, mirar cómo lo hacen ellos.
- La web aún no tiene usuarios reales. Dominio previsto: clipprofit.es.

## Cómo está hecha

- Flask + SQLAlchemy + Jinja, Postgres en Railway, Turbo 8 en el navegador. Sin paso de compilación.
- `app.py` (≈9.100 líneas): modelos, rutas y lógica, todo junto.
- `views.py`: lectura de redes (Apify y yt-dlp). `official.py`: APIs oficiales (OAuth). `push.py`: avisos al navegador (apagados).
- `templates/` (pantallas), `static/style.css`, `static/app.js`.
- `ensure_schema` añade solas las columnas e índices nuevos al arrancar: no hay migraciones.
- Producción: gunicorn con 2 procesos y 8 hilos. Las tareas automáticas las lleva un solo proceso (candado de líder).

## Probar antes de subir

```bash
python -m pytest tests -q                 # 153 tests, en SQLite
TEST_DATABASE_URL=postgresql://... python -m pytest tests -q   # los mismos en Postgres
```

- No subas nada con un test en rojo. Cada función nueva lleva su test.
- Las plantillas no se recargan sin reiniciar el servidor local.
- Para ver la web en local: `DEV_LOGIN=1 ADMIN_EMAIL=admin@demo.com SECRET_KEY=x flask --app app run` y entrar con el formulario de pruebas del login.
- Cambios de diseño: captura con Playwright antes y después, en PC y en móvil (375 px), y míralas.

## Piezas delicadas (leer antes de tocar)

- **Dinero.** `accrue` reparte por orden de envío hasta agotar el presupuesto; nunca baja lo ya ganado. `user_money` es el cálculo completo del saldo; `my_available` es la versión rápida para el menú y debe dar lo mismo.
- **Lectura diaria** (03:00, hora de España): `run_daily_refresh` → `refresh_views`. El día solo se marca cuando termina; si se corta, reintenta a los 45 min, hasta 3 veces. Trabaja por tandas para no llenar la memoria.
- **Gasto de Apify.** Tope diario (`apify_daily_max`, 800 resultados). Clips de más de 14 días se leen cada 3 días. Cuentas paradas se miran un día a la semana. Si yt-dlp falla, como mucho 40 cuentas al día pasan a Apify. No quitar estos límites sin avisar del coste.
- **yt-dlp** busca los clips de TikTok y YouTube y, si el listado trae las vistas, se usan en vez de pedirlas a Apify (ajuste `ytdlp_stats`). Instagram va entero por Apify.
- **Rendimiento del panel (auditoría de noviembre 2026):** los números de «por revisar» del menú, las pestañas y la campanita salen de `review_campaign_ids()` (cuatro columnas, sin cargar los clips). No volver a usar `review_queue()` para contar: carga todos los clips pendientes enteros y, con miles, cada pantalla del panel tardaba medio segundo. Regla: toda lista nueva va por tandas («Ver más») y toda cifra de menú sale de una cuenta, no de cargar filas. Clippers de una campaña va por tandas de 100 con búsqueda en el servidor (Intro). Las portadas y fotos que aún no están guardadas se bajan con `fetch_image_guarded` (como mucho 2 a la vez; si no, sale la de la campaña). El sondeo de avisos del navegador se espacia solo (20 s en uso, 60 s tras 2 min quieto, nada tras 15 min). Pendiente de esa auditoría: tablas de resumen para el Resumen, «Todos los clips» y Avisos sin cargar todo, y sacar la lectura nocturna a un servicio aparte. Para medir: copia local ampliada (10.000 usuarios, 66.000 clips) y simulador de carga; nunca contra la web real.
- **Listo para encender con variables de Railway (sin ellas no hacen nada):** aviso de errores con `SENTRY_DSN` (librería `sentry-sdk`); correo con `RESEND_API_KEY` y `MAIL_FROM` (Railway Hobby bloquea SMTP; el SMTP sigue como alternativa), que hace aparecer «He olvidado mi contraseña» en el acceso (`/recuperar`, enlace de un solo uso que vale 1 hora; nunca dice si el correo existe). El Panel técnico avisa si faltan. Las páginas que tardan más de 1,5 s (`SLOW_MS`) quedan apuntadas 24 h en memoria (`note_slow`, `slow_pages`) y salen como aviso en el Panel técnico.
- **Fluidez del menú:** al cambiar de página, el pintado de la pantalla nueva se retrasa hasta que la pastilla casi ha llegado (`PILL_HOLD`, como mucho 0,2 s) para que no se congele a medio camino. No quitarlo: sin eso el menú da un tirón en cada cambio. Las cifras del Resumen (`admin_analytics`) se guardan 2 minutos. La audiencia inventada sale con la etiqueta «Datos de demostración».
- **Cifras guardadas unos segundos** (`ttl_cache`): totales de campaña, ranking, contador de revisión. Nunca guardar ahí el dinero de un clipper ni filas de la base de datos.
- **Barras de pestañas** (en `app.js`): todas con la pastilla que se desliza, como pidió Mustapha: negra en `.seg`/`.sortbar`; en el menú de arriba (`#pn-top.tabs-ico`, solo en PC; en el móvil es el menú de abajo) el icono va encima del nombre y lo que se desliza es un recuadro gris detrás del icono; el icono de la sección en la que estás va relleno en negro (iconos `*_f` con clase `.ico-f` en `_icons.html`, solo se ven en la capa `.pill`) (`.pill-sh`), como en la referencia que pasó Mustapha, y blanca sobre gris en las de las gráficas (`.an-tg`, con su sombra en `.pill-sh`). El recorrido se anima a mano fotograma a fotograma (`pillClip`), no con transición CSS. Las barras que cambian de página llevan `data-pill-nav data-turbo-permanent` y un `id` único (`pn-…`): Turbo deja la misma barra al cambiar de página, `pillAdoptar` copia los números de la página nueva y `pillSegunDireccion` marca la pestaña según la dirección (sirve para «atrás»). Hay `turbo-cache-control: no-preview` para que no haya un repintado de más. Una barra nueva que cambie de página necesita esos atributos. Los filtros de Vídeos virales y Cuentas TOP del hub son barras normales (`.seg`, ya no chips sueltos: Mustapha quiere todo en barra, también dentro del hub) con la pastilla negra que se desliza (`pn-vr-red-…`, `pn-vr-per-…`, `pn-ta-red-…`); en el móvil van sin icono para que quepan. El filtro de Campañas del panel filtra las filas sin recargar (`data-row-filter`).
- **Tablas sin desplazamiento de lado:** ninguna tabla se desliza horizontalmente. Si no cabe, se esconden columnas menos importantes según el ancho de la propia tabla (`@container tbl` al final de `style.css`), y en el móvil cada fila es una tarjeta (`.rt`). Una tabla nueva con muchas columnas necesita sus reglas ahí. Mustapha no quiere desplazamientos horizontales.
- **Panel en móvil y tablet (≤900 px):** barra de abajo `.adm-bar` (Resumen, Campañas, Mensajes y «Más»); «Más» abre el menú completo (`#adm-side`) como hoja desde abajo. La barra de revisar clips (`.cr-bar`) va encima de esa barra.
- **Ranking:** `/ranking` abre en «Siempre» (la misma clasificación del Inicio, por dinero ganado). «Este mes» y «Esta semana» ordenan por vistas conseguidas dentro del periodo (vistas de ahora menos la última lectura anterior), sin cifra de dinero.
- **Portada pública** (`landing.html`, referencia de Vyro que pasó Mustapha, con los colores de la web): titular grande en el centro y, a los lados, cuatro columnas de portadas de clips reales que se desplazan despacio y se difuminan hacia el centro; en el móvil van en una franja debajo. «Imágenes de la portada» (pegar enlaces de vídeos en Campaña destacada) se quitó del panel el 23 nov a petición de Mustapha (la tabla `LandingTile` y su código siguen, sin usarse). `landing_wall()` usa los clips aprobados con la portada ya guardada, de más a menos vistas; si hay menos de 8, se completan con las de las campañas) y se sirven sin sesión en `/portada-clip/<id>`, solo de clips aprobados y nunca pidiendo nada a las redes.
- **Bienvenida** (`/bienvenida`, opción 3 elegida por Mustapha): un solo paso a pantalla limpia (solo el logo), sin «Saltar». Obligatorio: nombre, @usuario de la web (`User.username`, único, 3–24 letras, números, punto o guion bajo; se propone uno a partir del nombre y se cambia en Ajustes) y país. La foto es opcional (Mustapha) y se guarda aunque falte otro dato. Una cuenta nueva (no admin ni mod) no puede abrir ninguna otra página hasta completarla (`ONBOARDING_FREE` en `before`): al entrar con Google por primera vez va siempre a «Crea tu perfil». El acceso con Google lleva el «state» firmado (`_google_state`, 15 min), sin depender de la cookie; si el navegador repite la vuelta, el código ya usado (`gcode:` en Setting) entra sin error.
- **Errores: se apunta todo** (23 nov, pedido por Mustapha: «la más mínima cosa»). `note_error` guarda en `ErrorLog` (en su propia conexión, sin tocar la de la página) con código de 6 letras, tipo (`kind`: servidor, aviso, pagina, navegador, tarea, google) y una explicación en palabras normales (`explain_error`, `_cause`). Entra: cada error 500 (con código en pantalla), cada `flash(..., "error")` que ve un usuario (el `flash` de app.py está envuelto), cada 4xx salvo robots (`BOT_PATHS`), cada fallo de JavaScript (app.js y book.js mandan a `/api/error-navegador`, 20/h por IP), cada fallo de Google y todo lo que se apunte como error en el registro, también las tareas automáticas (`_ErrorsToPanel`, apagado en los tests). El mismo fallo, en la misma página y del mismo usuario, en 10 min suma veces (`times`). Se ven en Panel técnico → Estado → buscar por código o «Ver los últimos» (`/admin/errores`, filtros por tipo); aviso en Estado si hay fallos en 24 h. Se borran a los 30 días.
- **Notas** (Panel técnico → pestaña «Notas», tabla `TechNote`): notas de Mustapha con casilla de hecha y borrar; solo las ve quien ve el Panel técnico. Los formularios van sin Turbo para volver a la pestaña.
- **Solo Google para entrar** (23 nov, Mustapha): en la web real (`PASSWORD_LOGIN` apagado; `PASSWORD_LOGIN=1` en Railway lo vuelve a encender) el formulario de correo y contraseña se sigue viendo por el diseño, pero al enviarlo sale «Por ahora solo se puede entrar con Google». No hace falta correo (Resend) mientras siga así.
- **Pie de página:** solo sale en las páginas públicas (sin sesión). Con sesión iniciada, Términos, Privacidad, Cookies y Aviso legal van al final del menú del perfil (`.pm-legal`), como pidió Mustapha.
- **Soporte a pantalla completa** (`body.page-support`): la página no se mueve; solo se desplazan los mensajes.
- **Chat de soporte en ventanita:** el botón flotante abre `#chat-pop` (en `base.html`; se cierra solo al cambiar de sección, como pidió Mustapha). La conversación se carga de `/soporte/mini` al abrirla. El botón de arriba a la izquierda lleva a `/soporte` a pantalla completa.
- **Avisos al móvil:** apagados para todos (`CLIPPER_PUSH`, `PHONE_ALERTS`). Los avisos van a la campanita y al Panel técnico.
- **Bunny:** las copias de vídeo se hacen clip a clip y en segundo plano.
- **Portadas y fotos de cuentas:** los enlaces de TikTok/Instagram caducan y no se dejan enlazar. Cuando Apify o yt-dlp traen una portada o foto (yt-dlp: `_ytdlp_thumb`, `_ytdlp_avatar` en `views.py`; la foto del autor de TikTok se rescata con `_ytdlp_keep_avatar`, porque yt-dlp la descarta), se descarga y se guarda pequeña en la base de datos (`keep_image`, `thumb_data`/`avatar_data`); la web las sirve desde `/clip/<id>/portada` y `/cuenta-red/<id>/foto`. Si fallan 8 seguidas, para 10 min para no alargar la lectura. TikTok no da la portada por oEmbed desde Railway. Prueba: Panel técnico → Vistas y clips → «Probar portadas y fotos».
- **Panel de admin:** menú por grupos en `base.html` (Campañas, Comunidad, Sistema). «Creadores interesados» (/admin/solicitudes) vuelve al menú y a la campanita (23 nov): ahí llegan los creadores que reservan llamada en la landing clipprofit.es. En lugar de Calendly, la landing mete la página `/reservar` (calendario con huecos libres en la hora del visitante; pide nombre, WhatsApp, correo y canal opcional; al final «Te enviaremos el enlace de la llamada por WhatsApp»). Solo esa página se deja meter dentro de clipprofit.es, www y la copia de pruebas de Netlify (`_framable`, `BOOK_FRAME_ORIGINS`; funciona sin sesión ni cookies). El resto sigue con `X-Frame-Options: DENY`. Horario de llamadas en la misma página del panel (ajuste `booking`, hora de España); una hora reservada no se vuelve a ofrecer; botón «Enviar enlace por WhatsApp» (wa.me; al pulsarlo pasa a «Contactada» y deja de contar en el menú) y «Borrar» (la hora vuelve a quedar libre). Sin «Crear campaña» ni «Marcar…» en cada reserva (Mustapha). Mustapha no quiere tocar el diseño de la landing. Lo que el creador ya rellenó en la landing llega a la ventana por postMessage (la ventana avisa «clipprofit-reservar-lista», la landing responde «clipprofit-datos» con sus respuestas; solo de esas direcciones) y no se vuelve a pedir; lo que no tiene casilla propia se guarda en el mensaje (x_<pregunta>). Al terminar avisa «clipprofit-reservado». La landing está hecha aparte (otro chat, subida a Hostinger) y manda el formulario a `/api/creador-interesado` (JSON, solo desde `LANDING_ORIGINS`, por defecto clipprofit.es y www; campo trampa `website`; como mucho 5 por hora y IP; los campos de más se guardan en el mensaje). No meter en el menú lo que hacen los mods (revisar clips, solicitudes: van dentro de cada campaña) ni lo que no le interesa al cliente; Mustapha lo pidió así; buscador en `/admin/buscar`; el Panel técnico va por pestañas (`data-hash-tabs`) y los botones vuelven a su pestaña con `_anchor`.
- **APIs oficiales:** TikTok, Instagram, YouTube y Facebook (solo páginas: `pages_show_list`, `pages_read_engagement`, `read_insights`; el token de la página no caduca; variables `FACEBOOK_APP_ID`/`FACEBOOK_APP_SECRET`). Las cuentas de Facebook solo se buscan solas si la página está conectada. La guía de solicitud está en `templates/admin/_api_guide.html` (Panel técnico → APIs oficiales). Los códigos de verificación del dominio se guardan en el ajuste `site_verify` y se sirven en `_head.html`, `/tiktok….txt` y `/google….html`. «Añadir cuenta» en la campaña abre una ventana: «Por la web» (elige la red y va a su OAuth con `cid`; al volver, la cuenta entra verificada) o «Con un código». Aunque falten las claves, se manda igualmente a la página real de la red (lo pidió Mustapha; sin claves la red da su error); nunca recrear pantallas de las redes. Si cambian los permisos de `official.SCOPES`, hay que cambiar también los textos de la guía y la política de privacidad.

## Cosas que a Mustapha le gustan (no cambiarlas sin que lo pida)

- Pantallas sin título ni frase explicativa arriba (Mis clips, Ganancias, Ajustes, Soporte, Ranking, Formación y todo el panel). El título está solo para lectores de pantalla (`sr-only`); en el panel, `ui.head` ya no pinta título, solo los botones. No volver a ponerlos.

- Una sola letra en toda la web, también en el logo: Inter (opción A, elegida por Mustapha). Poppins ya no se carga. No meter otra letra en ningún sitio. Cifras en Inter seminegrita con números de ancho fijo. Ganancias mantiene sus cuatro tarjetas iguales: no cambiar su estructura.
- Nada de rótulos en MAYÚSCULAS con letras separadas («POR 1.000 VISTAS»): en minúscula normal.
- Tarjeta de campaña: el estado va con icono y texto debajo de la fecha (Ya participas, Solicitud enviada, Plazas completas).
- Estados con color fijo: verde bien, rojo mal, naranja te toca hacer algo, gris en marcha. Nada de azul ni morado.
- Panel de admin con el estilo de la referencia «BentoFlow» (noviembre 2026): fondo gris suave, tarjetas blancas redondeadas con sombra ligera, cifras con icono y minigráfica. La referencia era solo de estilo: los colores son los de la web (negro `--ac`). Excepción pedida por Mustapha: el Resumen usa los colores de Mis clips (vistas naranja, dinero verde, enviados morado, clippers/aprobados turquesa; las clases `.k-views/.k-spent/.k-sent/.k-users` las comparten Mis clips y el Resumen). Reglas al final de `style.css`, bajo `body.admin-shell`. La parte de los clippers aún no se ha pasado a este estilo.
- El Resumen del panel: sin título; arriba el periodo (Últimos 7 días, 30 días, Desde siempre, `?p=`), cuatro cifras al estilo de Mis clips (icono, cifra y nombre) con minigráfica y cambio frente al periodo anterior, una tarjeta con tres gráficas (Vistas y dinero, Coste por 1.000 y Campañas; al entrar siempre empieza en «Vistas y dinero»; el cambio en % no se enseña si la base es menor de 10), actividad reciente, campañas activas con su portada y equipo con su foto y «En línea» en pastilla (`overview_v2` en `app.py`). Al entrar, las gráficas crecen desde cero; al cambiar de periodo, pasan de los valores de antes a los nuevos (barras que se estiran o encogen y líneas que se transforman, en `app.js`, clase `v2-morph`).
- Los colores de las estadísticas de Mis clips (naranja, verde, morado, turquesa). La tarjeta elegida: fondo suave de su color y borde fino del mismo color (opción A), sin brillo.
- Pastillas negras, tarjetas blancas limpias, verde solo para el dinero.
- Tabla única de valores (noviembre 2026, tras la auditoría): radios solo 8 px (etiqueta), 12 px (campo y cajas pequeñas), 20 px (tarjeta) y 999 px (pastilla); sombras solo `--sh-1`, `--sh-2` (tarjeta, es `--shadow`) y `--sh-3` (ventanas), más los anillos de foco; letra solo 12, 13, 14, 16, 18, 22, 26 y 34 px (los títulos grandes de la portada usan `clamp`); gris de texto secundario único `--muted` (#686971, cumple contraste). No meter otros valores. Nada de emojis en la interfaz: iconos de línea de `_icons.html` (los canales del hub usan `chan_icon`, ya no su emoji). Nada de azul. Un color por cifra en toda la web: vistas naranja, dinero verde, clips morado, clippers turquesa; las derivadas (coste por 1.000), en gris.
- Sistema único (al final de `style.css`, «Sistema único»): todos los botones son pastillas redondas, en dos tamaños (40 px y 32 px `.sm`), letra 600, sin sombra; estilos: negro (principal), gris `.soft` (secundario), blanco con borde `.white`, rojo suave `.red` (borrar). Campos y desplegables de formulario con esquina 12 px; tarjetas con esquina 20 px, borde fino y sombra suave. No meter botones con otras esquinas o tamaños.
- Desplegables de filtro (Mis clips, Revisar clips): pastilla blanca redonda con icono delante (`data-ico`: flag, cal, sort) y lista propia con ✓ negro sobre gris (opción A). En el móvil es el selector del teléfono, con el mismo aspecto cerrado.
- Barras «Visualizaciones por plataforma» de Mis clips en gris, como en las estadísticas de la campaña. En las dos, cada barra es su parte del total y como mucho llega al 60 % de la línea: nunca casi llenas.
- Campana de avisos: solo un punto rojo cuando hay algo sin leer, sin número (opción C).
- Contadores (mensajes, menú del panel, chat): todos rojos, pequeños y redondos (`.count`, `.adm-n`). Etiqueta «Admin» junto al logo en gris claro con borde fino.
- Ajustes (`/cuenta`) en lista, sin índice lateral y sin iconos (opción A): foto y nombre arriba con «Cambiar foto», luego grupos Perfil, Cobros (Whop también en filas: usuario en la fila y botón Conectar) y Cuenta con filas (`.lst`, `.lr`) y «Cerrar sesión» en rojo al final.
- El ranking del inicio y el destacado de campañas, recién hechos a su gusto.

## Skills de diseño (en `.claude/skills/`, sirven en Claude Code y en el chat)

- `design-audit` (Bencium, licencia MIT): revisa la web pantalla a pantalla y propone un plan por fases (crítico, refinamiento, pulido) sin tocar el funcionamiento. Sus «DESIGN_SYSTEM», «APP_FLOW» y demás documentos son, aquí, este `CLAUDE.md`. Presenta el plan y espera el visto bueno, como ya pide Mustapha.
- `web-design-guidelines` (Vercel): repasa un archivo contra las reglas de interfaz de Vercel (accesibilidad, foco, textos de botones, formularios) y da los fallos por archivo y línea. Necesita poder abrir la web de las reglas.
- Lo que le gusta a Mustapha (sección de arriba) manda sobre lo que diga cualquier skill.

## Pendiente

Acordado y sin hacer:
1. Aviso automático de errores (Sentry, plan gratuito): el código ya está; falta crear la cuenta y poner `SENTRY_DSN` en Railway.
2. Copia de seguridad diaria automática de la base de datos (ahora solo hay botón manual).
3. Copia de pruebas de la web en Railway, para ver los cambios antes de pasarlos a la real.
4. Arquitectura modular: repartir `app.py` en carpetas por temas (dinero, clips, lectura, campañas, comunidad, admin), un tema cada vez, sin reescribir y sin cambiar la base de datos. Empezar por dinero y lectura.

Para el final:
- Poner los datos del titular (nombre, NIF y domicilio de Felipe) en Panel técnico → Datos → «Datos del aviso legal». Salen en /aviso-legal y en la privacidad. Mientras falten, hay un aviso en el Panel técnico.
- Probar un retiro real por Whop de principio a fin.
- Dominio: la plataforma vive en app.clipprofit.es (BASE_URL por defecto en producción); la dirección de Railway manda ahí con 301 (salvo /salud, /cron/ y los POST). Google OAuth ya tiene la dirección nueva.
- Comprobar en producción que yt-dlp trae las vistas (Panel técnico → «Probar yt-dlp con una cuenta»).

Sin decidir:
- APIs oficiales de TikTok, Instagram, YouTube y Facebook (páginas): el código, las páginas legales y la guía están hechos; falta que Mustapha cree las apps en cada red y pase su revisión (necesita antes el dominio propio).
- Botón «Conectar con Whop» para identificar a los miembros del Whop.
- Acceso del cliente en los planes de distribución.

Flojo, sin urgencia:
- Avisos del admin carga todos los clippers de golpe.
- La hoja de estilos arrastra parches; limpiarla al modularizar.
