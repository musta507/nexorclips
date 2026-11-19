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
- **Cifras guardadas unos segundos** (`ttl_cache`): totales de campaña, ranking, contador de revisión. Nunca guardar ahí el dinero de un clipper ni filas de la base de datos.
- **Barras de pestañas** (en `app.js`): todas con la pastilla que se desliza, como pidió Mustapha: negra en `.seg`/`.sortbar`; en el menú de arriba (`#pn-top.tabs-ico`, solo en PC; en el móvil es el menú de abajo) el icono va encima del nombre y lo que se desliza es un recuadro gris detrás del icono; el icono de la sección en la que estás va en negro y más grueso (capa `.pill` con los iconos en negro) (`.pill-sh`), como en la referencia que pasó Mustapha, y blanca sobre gris en las de las gráficas (`.an-tg`, con su sombra en `.pill-sh`). El recorrido se anima a mano fotograma a fotograma (`pillClip`), no con transición CSS. Las barras que cambian de página llevan `data-pill-nav data-turbo-permanent` y un `id` único (`pn-…`): Turbo deja la misma barra al cambiar de página, `pillAdoptar` copia los números de la página nueva y `pillSegunDireccion` marca la pestaña según la dirección (sirve para «atrás»). Hay `turbo-cache-control: no-preview` para que no haya un repintado de más. Una barra nueva que cambie de página necesita esos atributos. Los filtros de Vídeos virales y Cuentas TOP del hub son chips sueltos (`.fchips`, opción B) y también llevan la pastilla negra que se desliza (`pn-vr-red-…`, `pn-vr-per-…`, `pn-ta-red-…`); mientras carga no se atenúan. El filtro de Campañas del panel filtra las filas sin recargar (`data-row-filter`).
- **Tablas sin desplazamiento de lado:** ninguna tabla se desliza horizontalmente. Si no cabe, se esconden columnas menos importantes según el ancho de la propia tabla (`@container tbl` al final de `style.css`), y en el móvil cada fila es una tarjeta (`.rt`). Una tabla nueva con muchas columnas necesita sus reglas ahí. Mustapha no quiere desplazamientos horizontales.
- **Soporte a pantalla completa** (`body.page-support`): la página no se mueve; solo se desplazan los mensajes.
- **Chat de soporte en ventanita:** el botón flotante abre `#chat-pop` (en `base.html`, `data-turbo-permanent`: sigue abierto al cambiar de página). La conversación se carga de `/soporte/mini` al abrirla. El botón de arriba a la izquierda lleva a `/soporte` a pantalla completa.
- **Avisos al móvil:** apagados para todos (`CLIPPER_PUSH`, `PHONE_ALERTS`). Los avisos van a la campanita y al Panel técnico.
- **Bunny:** las copias de vídeo se hacen clip a clip y en segundo plano.
- **Panel de admin:** menú por grupos en `base.html` (Campañas, Comunidad, Sistema). No meter en el menú lo que hacen los mods (revisar clips, solicitudes: van dentro de cada campaña) ni lo que no le interesa al cliente; Mustapha lo pidió así; buscador en `/admin/buscar`; el Panel técnico va por pestañas (`data-hash-tabs`) y los botones vuelven a su pestaña con `_anchor`.
- **APIs oficiales:** la guía de solicitud está en `templates/admin/_api_guide.html` (Panel técnico → APIs oficiales). Los códigos de verificación del dominio se guardan en el ajuste `site_verify` y se sirven en `_head.html`, `/tiktok….txt` y `/google….html`. Si cambian los permisos de `official.SCOPES`, hay que cambiar también los textos de la guía y la política de privacidad.

## Cosas que a Mustapha le gustan (no cambiarlas sin que lo pida)

- Pantallas sin título ni frase explicativa arriba (Mis clips, Ganancias, Ajustes, Soporte, Ranking, Formación y todo el panel). El título está solo para lectores de pantalla (`sr-only`); en el panel, `ui.head` ya no pinta título, solo los botones. No volver a ponerlos.

- Una sola letra en toda la web, también en el logo: Inter (opción A, elegida por Mustapha). Poppins ya no se carga. No meter otra letra en ningún sitio. Cifras en Inter seminegrita con números de ancho fijo. Ganancias mantiene sus cuatro tarjetas iguales: no cambiar su estructura.
- Nada de rótulos en MAYÚSCULAS con letras separadas («POR 1.000 VISTAS»): en minúscula normal.
- Tarjeta de campaña: el estado va con icono y texto debajo de la fecha (Ya participas, Solicitud enviada, Plazas completas).
- Estados con color fijo: verde bien, rojo mal, naranja te toca hacer algo, gris en marcha. Nada de azul ni morado.
- Panel de admin con el estilo de la referencia «BentoFlow» (noviembre 2026): fondo gris suave, tarjetas blancas redondeadas con sombra ligera, cifras con icono y minigráfica. La referencia era solo de estilo: los colores son los de la web (negro `--ac`). Excepción pedida por Mustapha: el Resumen usa los colores de Mis clips (vistas naranja, dinero verde, enviados morado, clippers/aprobados turquesa; las clases `.k-views/.k-spent/.k-sent/.k-users` las comparten Mis clips y el Resumen). Reglas al final de `style.css`, bajo `body.admin-shell`. La parte de los clippers aún no se ha pasado a este estilo.
- El Resumen del panel: cuatro cifras con minigráfica y cambio frente al mes anterior, clips por semana, actividad reciente, campañas activas y equipo (`overview_v2` en `app.py`).
- Los colores de las estadísticas de Mis clips (naranja, verde, morado, turquesa). La tarjeta elegida: fondo suave de su color y borde fino del mismo color (opción A), sin brillo.
- Pastillas negras, tarjetas blancas limpias, verde solo para el dinero.
- Desplegables de filtro (Mis clips, Revisar clips): pastilla blanca redonda con icono delante (`data-ico`: flag, cal, sort) y lista propia con ✓ negro sobre gris (opción A). En el móvil es el selector del teléfono, con el mismo aspecto cerrado.
- Barras «Visualizaciones por plataforma» de Mis clips en gris, como en las estadísticas de la campaña.
- Campana de avisos: solo un punto rojo cuando hay algo sin leer, sin número (opción C).
- Ajustes (`/cuenta`) sin índice lateral (Perfil, Cobros, Cuenta): una sola columna centrada. Mustapha lo quitó.
- El ranking del inicio y el destacado de campañas, recién hechos a su gusto.

## Pendiente

Acordado y sin hacer:
1. Aviso automático de errores (Sentry, plan gratuito): cada fallo con página, usuario y línea.
2. Copia de seguridad diaria automática de la base de datos (ahora solo hay botón manual).
3. Copia de pruebas de la web en Railway, para ver los cambios antes de pasarlos a la real.
4. Arquitectura modular: repartir `app.py` en carpetas por temas (dinero, clips, lectura, campañas, comunidad, admin), un tema cada vez, sin reescribir y sin cambiar la base de datos. Empezar por dinero y lectura.

Para el final:
- Poner los datos del titular (nombre, NIF y domicilio de Felipe) en Panel técnico → Datos → «Datos del aviso legal». Salen en /aviso-legal y en la privacidad. Mientras falten, hay un aviso en el Panel técnico.
- Probar un retiro real por Whop de principio a fin.
- Conectar el dominio clipprofit.es y poner la región de Railway en Europa.
- Comprobar en producción que yt-dlp trae las vistas (Panel técnico → «Probar yt-dlp con una cuenta»).

Sin decidir:
- APIs oficiales de TikTok, Instagram y YouTube: el código, las páginas legales y la guía están hechos; falta que Mustapha cree las apps en cada red y pase su revisión (necesita antes el dominio propio).
- Botón «Conectar con Whop» para identificar a los miembros del Whop.
- Acceso del cliente en los planes de distribución.

Flojo, sin urgencia:
- Avisos del admin carga todos los clippers de golpe.
- La hoja de estilos arrastra parches; limpiarla al modularizar.
