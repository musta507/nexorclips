// ClipProfit Hispano — JavaScript de la web.
// La web usa Turbo: al pulsar un enlace no se recarga la página entera, solo se cambia el contenido.
// Por eso hay dos partes:
//   1) Escuchas globales (clics, envíos…): se ponen una sola vez y sirven para todas las páginas.
//   2) pagina(): lo que hay que preparar cada vez que se muestra una página (chat, color de la destacada…).

// =============================================================== Turbo
if (window.Turbo) {
  Turbo.session.drive = true;
  // Los formularios también van con Turbo: al pulsar un botón no se recarga la página entera ni se vuelve arriba
  if (Turbo.config && Turbo.config.forms) Turbo.config.forms.mode = "on"; else if (Turbo.setFormMode) Turbo.setFormMode("on");
  if (Turbo.config && Turbo.config.drive) Turbo.config.drive.progressBarDelay = 400; else if (Turbo.setProgressBarDelay) Turbo.setProgressBarDelay(400);   // la barra de carga solo aparece si algo tarda mucho
}

// =============================================================== 1) escuchas globales

// Formularios: evitar envíos dobles y confirmar acciones delicadas
document.addEventListener("submit", function (e) {
  var form = e.target;
  if (form.hasAttribute("data-chat-form")) return;   // el chat se envía por su cuenta
  var confirmMsg = (e.submitter && e.submitter.getAttribute("data-confirm")) || form.getAttribute("data-confirm");
  if (confirmMsg && !window.confirm(confirmMsg)) { e.preventDefault(); return; }
  if (form.dataset.sending === "1") { e.preventDefault(); return; }
  form.dataset.sending = "1";
  var btn = e.submitter || form.querySelector("button[type=submit], button:not([type])");
  if (btn) { btn.setAttribute("aria-busy", "true"); btn.classList.add("busy"); }
  setTimeout(function () { form.dataset.sending = ""; if (btn) { btn.classList.remove("busy"); btn.removeAttribute("aria-busy"); } }, 8000);
}, true);   // antes que Turbo: si se cancela la confirmación, no se envía
document.addEventListener("turbo:submit-end", function (e) {
  var form = e.target;
  form.dataset.sending = "";
  form.querySelectorAll(".btn.busy").forEach(function (b) { b.classList.remove("busy"); b.removeAttribute("aria-busy"); });
});

// Copiar enlaces
document.addEventListener("click", function (e) {
  var lit = e.target.closest("[data-copy-text]");
  if (lit) {
    var dd = lit.closest("details"); if (dd) dd.removeAttribute("open");
    var ok = function () { toast("Enlace copiado", "ok"); };
    if (navigator.clipboard) navigator.clipboard.writeText(lit.getAttribute("data-copy-text")).then(ok, function () {});
    return;
  }
  var btn = e.target.closest("[data-copy]");
  if (!btn) return;
  var target = document.getElementById(btn.getAttribute("data-copy"));
  if (!target) return;
  var text = (target.value || target.textContent).trim();
  var done = function () { var t = btn.textContent; btn.textContent = "Copiado"; setTimeout(function () { btn.textContent = t; }, 1600); };
  if (navigator.clipboard) navigator.clipboard.writeText(text).then(done, function () { target.select && target.select(); });
  else if (target.select) target.select();
});

// Cerrar el menú de usuario al hacer clic fuera
document.addEventListener("click", function (e) {
  document.querySelectorAll("details.user-menu[open], details.bell-menu[open], details.rowmenu[open]").forEach(function (d) { if (!d.contains(e.target)) d.removeAttribute("open"); });
});

// Menú «⋯» de cada fila: se coloca encima de todo (las tablas recortan lo que sobresale) y hacia arriba si no cabe
document.addEventListener("toggle", function (e) {
  var d = e.target;
  if (!d.classList || !d.classList.contains("rowmenu")) return;
  var list = d.querySelector(".rowmenu-list");
  if (!list) return;
  if (!d.open) { list.removeAttribute("style"); return; }
  var r = d.querySelector("summary").getBoundingClientRect();
  list.style.position = "fixed";
  list.style.right = Math.max(window.innerWidth - r.right, 8) + "px";
  var h = list.offsetHeight;
  if (r.bottom + 6 + h > window.innerHeight - 8 && r.top - 6 - h > 8) list.style.top = (r.top - 6 - h) + "px";
  else list.style.top = (r.bottom + 6) + "px";
}, true);
window.addEventListener("scroll", function () {
  document.querySelectorAll("details.rowmenu[open]").forEach(function (d) { d.removeAttribute("open"); });
}, { passive: true, capture: true });

// Ordenar campañas (Más nuevas / Más presupuesto / Mejor pago), con las tarjetas moviéndose suave
document.addEventListener("click", function (e) {
  var b = e.target.closest("[data-sort]");
  if (!b) return;
  b.parentNode.querySelectorAll("button").forEach(function (x) { x.classList.toggle("on", x === b); });
  var grid = document.querySelector(".grid");
  if (!grid) return;
  var before = new Map();
  Array.prototype.forEach.call(grid.children, function (el) { before.set(el, el.getBoundingClientRect()); });
  var k = b.dataset.sort;
  Array.prototype.slice.call(grid.children)
    .sort(function (x, y) {
      var f = (x.dataset.full === "1") - (y.dataset.full === "1");    // las de plazas completas siempre al final
      return f || parseFloat(y.dataset[k] || 0) - parseFloat(x.dataset[k] || 0);
    })
    .forEach(function (el) { grid.appendChild(el); });
  Array.prototype.forEach.call(grid.children, function (el) {
    var a = before.get(el), n = el.getBoundingClientRect();
    var dx = a.left - n.left, dy = a.top - n.top;
    if (!dx && !dy) return;
    el.style.transition = "none";
    el.style.transform = "translate(" + dx + "px," + dy + "px)";
    void el.offsetWidth;
    el.style.transition = "transform .45s cubic-bezier(.3,.9,.3,1)";
    el.style.transform = "";
  });
});

// Ventana de retirar
document.addEventListener("click", function (e) {
  var all = e.target.closest(".wd-all");
  if (all) { var i = document.getElementById("wd-amount"); if (i) { i.value = all.getAttribute("data-all"); i.focus(); } return; }
  var o = e.target.closest("[data-open]");
  if (o) { var d = document.getElementById(o.getAttribute("data-open")); if (d && d.showModal) { d.showModal(); var a = d.querySelector("input[name=amount]"); if (a) a.select(); } return; }
  var c = e.target.closest("[data-close]");
  if (c) { var dl = c.closest("dialog"); if (dl) dl.close(); return; }
  if (e.target.tagName === "DIALOG" && e.target.open) e.target.close();   // clic fuera de la ventana
});

// Avisos de mensajes nuevos: cada 20 s mira si te han escrito y lo muestra (aviso arriba, número en el chat, punto en tu foto)
(function () {
  function paint(d) {
    var n = d.unread || 0;
    document.title = (n ? "(" + n + ") " : "") + document.title.replace(/^\(\d+\)\s*/, "");
    var count = document.getElementById("fab-count");
    var fab = document.getElementById("chat-fab");
    var dot = document.getElementById("avatar-dot");
    var staff = document.body.hasAttribute("data-staff");      // el equipo no ve sus mensajes del panel en el chat de clipper
    if (count) { count.textContent = staff ? 0 : n; count.hidden = staff || !n; }
    if (fab) fab.classList.toggle("has-unread", !staff && !!n);
    if (dot) dot.hidden = !n;
    var bell = document.getElementById("bell-n");
    if (bell && typeof d.bell === "number") { bell.textContent = d.bell > 99 ? "99+" : d.bell; bell.hidden = !d.bell; }
    var slot = document.getElementById("msg-notice");
    if (!slot) return;
    if (!n) { slot.innerHTML = ""; return; }
    var a = document.createElement("a");
    a.className = "msg-notice";
    a.href = d.url;
    a.setAttribute("role", "status");
    a.innerHTML = '<span class="mn-ico"><span class="logo-mark"></span></span>' +
      '<span class="mn-txt"><span class="mn-top"><b></b><span></span></span><span class="mn-body"></span></span>' +
      '<span class="mn-btn"></span>';
    a.querySelector(".mn-top b").textContent = d.title;
    a.querySelector(".mn-top span").textContent = d.ago;
    a.querySelector(".mn-body").textContent = d.body;
    a.querySelector(".mn-btn").textContent = d.url.indexOf("/admin") === 0 ? "Abrir" : "Responder";
    slot.innerHTML = "";
    slot.appendChild(a);
  }
  var shown = null;
  function check() {
    var body = document.body;
    var url = body.getAttribute("data-notices");
    if (!url || document.hidden || body.getAttribute("data-on-chat") === "1") return;
    if (shown === null) shown = parseInt(body.getAttribute("data-unread") || "0", 10);
    fetch(url, { headers: { "X-Requested-With": "fetch" }, credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d) return;
        var bell = document.getElementById("bell-n");
        if (bell && typeof d.bell === "number") { bell.textContent = d.bell > 99 ? "99+" : d.bell; bell.hidden = !d.bell; }
        if ((d.unread || 0) === shown) return;
        var more = (d.unread || 0) > shown;
        shown = d.unread || 0;
        paint(d);
        if (more && navigator.vibrate) { try { navigator.vibrate(60); } catch (e) {} }
      })
      .catch(function () {});
  }
  setInterval(check, 20000);
  document.addEventListener("visibilitychange", function () { if (!document.hidden) check(); });
  // Cada página nueva trae su propio número de mensajes sin leer
  document.addEventListener("turbo:load", function () { shown = parseInt(document.body.getAttribute("data-unread") || "0", 10); });
})();

// =============================================================== 2) lo que se prepara en cada página
var limpiar = [];   // cosas que hay que parar al salir de la página (temporizadores del chat…)

function pagina() {
  // «Rellenar formulario» desde el destacado: la campaña se abre ya con la ventana de solicitud
  var auto = document.querySelector("dialog[data-auto-open]");
  if (auto && auto.showModal && !auto.open) { auto.removeAttribute("data-auto-open"); auto.showModal(); }
  pestanasHash();
  listas();
  var navOn = document.querySelector(".adm-side .adm-link.on"), side = document.querySelector(".adm-side");
  if (navOn && side && side.scrollWidth > side.clientWidth) side.scrollLeft = Math.max(0, navOn.offsetLeft - 16);
  iniciarChat();
  pastillaOrden();
  graficas();
  asistente();
  avisosPush();
  subidaVideo();
  progresoLeccion();
  destacarCurso();
  desplegables();
  destacado();
  canalVisible();
  comprobarBio();
  avisosToast();
  horasLocales();
  ajustes();
  avisosPanel();
  carruseles();
  buscadoresVivos();
}

// Aviso flotante creado desde aquí (p. ej. «Enlace copiado»)
function toast(msg, cat) {
  var box = Array.prototype.find.call(document.querySelectorAll(".toasts"), function (b) { return b.parentNode === document.body; }) || document.querySelector(".toasts");
  if (!box) { box = document.createElement("div"); box.className = "toasts"; box.setAttribute("aria-live", "polite"); document.body.appendChild(box); }
  var t = document.createElement("div");
  t.className = "flash " + (cat || "info"); t.setAttribute("role", "status"); t.setAttribute("data-toast", "");
  t.textContent = msg;
  var x = document.createElement("button"); x.type = "button"; x.className = "toast-x"; x.setAttribute("aria-label", "Cerrar aviso"); x.setAttribute("data-toast-x", ""); x.textContent = "×";
  t.appendChild(x); box.appendChild(t);
  avisosToast();
}

// =============================================================== avisos flotantes (toast): se van solos, el de error dura más
function avisosToast() {
  // Siempre colgados del <body>: si están dentro de algo con animación, «fixed» deja de ser fijo y se van al final de la página
  var boxes = document.querySelectorAll(".toasts");
  if (boxes.length) {
    var main = Array.prototype.find.call(boxes, function (b) { return b.parentNode === document.body; }) || boxes[0];
    if (main.parentNode !== document.body) document.body.appendChild(main);
    Array.prototype.forEach.call(boxes, function (b) {
      if (b === main) return;
      while (b.firstChild) main.appendChild(b.firstChild);
      b.remove();
    });
  }
  document.querySelectorAll("[data-toast]").forEach(function (t, i) {
    if (t.dataset.ready) return;
    t.dataset.ready = "1";
    var vida = parseInt(t.getAttribute("data-toast-life") || "0", 10) || (t.classList.contains("error") ? 9000 : 5000);
    var quedan = vida, desde = Date.now(), timer = null;
    var bar = t.querySelector(".toast-bar");
    if (bar) bar.style.animationDuration = vida + "ms";
    function cierra() {
      clearTimeout(timer);
      t.classList.add("out");
      setTimeout(function () { var box = t.parentNode; t.remove(); if (box && !box.children.length) box.remove(); }, 220);
    }
    function arranca() { desde = Date.now(); timer = setTimeout(cierra, quedan); }
    t.style.animationDelay = (i * 60) + "ms";
    t.addEventListener("mouseenter", function () { clearTimeout(timer); quedan -= Date.now() - desde; if (bar) bar.style.animationPlayState = "paused"; });
    t.addEventListener("mouseleave", function () { if (bar) bar.style.animationPlayState = "running"; arranca(); });
    var x = t.querySelector("[data-toast-x]");
    if (x) x.addEventListener("click", cierra);
    arranca();
    limpiar.push(tuyo(t, function () { clearTimeout(timer); }));
  });
}

// =============================================================== horas en la zona del que mira (el servidor las da en UTC)
var FMT_FECHA = null;
function horasLocales() {
  try {
    FMT_FECHA = FMT_FECHA || new Intl.DateTimeFormat("es-ES", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" });
  } catch (err) { return; }
  document.querySelectorAll("time[data-local]").forEach(function (el) {
    if (el.dataset.ready) return;
    el.dataset.ready = "1";
    var d = new Date(el.getAttribute("datetime"));
    if (isNaN(d)) return;
    if (el.hasAttribute("data-local-hm")) {             // solo la hora: 03:00 en España, 21:00 en México…
      el.textContent = d.toLocaleTimeString("es-ES", { hour: "2-digit", minute: "2-digit" });
      var zona = el.parentNode && el.parentNode.querySelector("[data-local-zone]");
      var tz = ""; try { tz = Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch (err) {}
      if (zona) zona.textContent = /Madrid|Ceuta/.test(tz) ? "(hora de España)" : "(tu hora)";
      return;
    }
    if (el.hasAttribute("data-local-full")) {             // «jueves 13 nov · 12:00»
      try {
        var dia = d.toLocaleDateString("es-ES", { weekday: "long", day: "numeric", month: "short" }).replace(",", "").replace(".", "");
        el.textContent = dia + " · " + d.toLocaleTimeString("es-ES", { hour: "2-digit", minute: "2-digit" });
      } catch (err) {}
      return;
    }
    var full = FMT_FECHA.format(d).replace(",", "");
    if (el.hasAttribute("data-local-show")) el.textContent = full;   // si no, deja el «hace 2 h» y pone la fecha local al pasar el ratón
    el.title = full + " (tu hora)";
  });
}

// =============================================================== código en la bio: el resultado aparece solo, sin recargar a mano
function comprobarBio() {
  document.querySelectorAll("[data-bio-poll]").forEach(function (box) {
    if (box.dataset.ready) return;
    box.dataset.ready = "1";
    var url = box.getAttribute("data-bio-poll"), tries = 0;
    var t = setInterval(function () {
      tries++;
      fetch(url, { credentials: "same-origin", headers: { "X-Requested-With": "fetch" } })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (d.checking && tries < 60) return;
          clearInterval(t);
          if (window.Turbo) Turbo.visit(location.href, { action: "replace" }); else location.reload();
        }).catch(function () {});
    }, 4000);
    limpiar.push(tuyo(box, function () { clearInterval(t); }));
  });
}

// =============================================================== gráfica de estadísticas (informe y panel)
var MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"];
var METRICA = { views: "Visualizaciones", likes: "Me gusta", comments: "Comentarios", shares: "Compartidos",
                earned: "Ganado", sent: "Envíos", approved: "Aprobados", spent: "Repartido", paid: "Pagado", users: "Clippers nuevos" };
function cifra(metric, v) {         // el dinero con decimales y su símbolo; el resto, número entero
  if (metric === "earned" || metric === "spent" || metric === "paid") return (Math.round(v * 100) / 100).toFixed(2).replace(".", ",").replace(/\B(?=(\d{3})+(?!\d))/g, ".") + " $";
  return numES(v);
}
function curva(xs, ys) {            // línea suave que no se pasa de los puntos (monótona): sin bajar de 0 ni hacer picos falsos
  var n = ys.length;
  if (n < 3) return ys.map(function (y, i) { return (i ? "L" : "M") + xs[i].toFixed(1) + " " + y.toFixed(1); }).join(" ");
  var dx = [], m = [], t = [];
  for (var i = 0; i < n - 1; i++) { dx[i] = xs[i + 1] - xs[i]; m[i] = (ys[i + 1] - ys[i]) / (dx[i] || 1); }
  t[0] = m[0]; t[n - 1] = m[n - 2];
  for (i = 1; i < n - 1; i++) t[i] = m[i - 1] * m[i] <= 0 ? 0 : (m[i - 1] + m[i]) / 2;
  for (i = 0; i < n - 1; i++) {
    if (m[i] === 0) { t[i] = 0; t[i + 1] = 0; continue; }
    var a = t[i] / m[i], b = t[i + 1] / m[i], h = a * a + b * b;
    if (h > 9) { var k = 3 / Math.sqrt(h); t[i] = k * a * m[i]; t[i + 1] = k * b * m[i]; }
  }
  var d = "M" + xs[0].toFixed(1) + " " + ys[0].toFixed(1);
  for (i = 0; i < n - 1; i++) {
    var c = dx[i] / 3;
    d += " C" + (xs[i] + c).toFixed(1) + " " + (ys[i] + t[i] * c).toFixed(1) + " " + (xs[i + 1] - c).toFixed(1) + " " + (ys[i + 1] - t[i + 1] * c).toFixed(1) + " " + xs[i + 1].toFixed(1) + " " + ys[i + 1].toFixed(1);
  }
  return d;
}
function numES(v) { return Math.round(v).toString().replace(/\B(?=(\d{3})+(?!\d))/g, "."); }
function corto(v) {                // 12,5M · 380K · 2,5K: hasta un decimal, sin «,0»
  var f = function (x) { return (Math.round(x * 10) / 10).toString().replace(".", ","); };
  if (v >= 1e6) return f(v / 1e6) + "M";
  if (v >= 1e3) return f(v / 1e3) + "K";
  return f(v);
}
function fechaCorta(iso) { var p = iso.split("-"); return parseInt(p[2], 10) + " " + MESES[parseInt(p[1], 10) - 1]; }
function ejeY(max) {                // escalones «redondos» (1, 2 o 5 × potencia de 10), como mucho 5 líneas
  if (max <= 0) return { step: 1, top: 4 };
  var raw = max / 4, p = Math.pow(10, Math.floor(Math.log10(raw))), f = raw / p;
  var step = (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * p;
  return { step: step, top: Math.ceil(max / step) * step };
}
document.addEventListener("click", function (e) {      // paso «Únete al WhatsApp»: al volver a la pestaña se marca como hecho
  var a = e.target.closest && e.target.closest("[data-reload-soon]");
  if (a) setTimeout(function () { location.reload(); }, 1200);
});

function graficas() {
  document.querySelectorAll("[data-chart]").forEach(function (box) {
    if (box.dataset.ready) return;
    var src = box.querySelector("[data-chart-data]"), plot = box.querySelector("[data-chart-plot]");
    if (!src || !plot) return;
    box.dataset.ready = "1";
    var data = JSON.parse(src.textContent), state = { mode: box.getAttribute("data-chart-mode-start") || "cumulative", metric: box.getAttribute("data-chart-start") || "views" };
    var suave = box.hasAttribute("data-chart-smooth");        // Mis clips: también el diario va en línea suave
    var NS = "http://www.w3.org/2000/svg";
    function el(tag, attrs, parent) {
      var e = document.createElementNS(NS, tag);
      for (var k in attrs) e.setAttribute(k, attrs[k]);
      if (parent) parent.appendChild(e);
      return e;
    }
    var uid = "an" + Math.random().toString(36).slice(2, 8), transicion = null, geo = null;
    var calma = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    function draw(anim, morph) {                           // anim: devuelve la animación de entrada · morph: viene de otra métrica
      var labels = data.series.labels, vals = data.series[state.mode][state.metric];
      var W = plot.clientWidth || 600, H = W < 560 ? 220 : 300, L = 44, R = 12, T = 12, B = 30;
      var iw = W - L - R, ih = H - T - B, n = vals.length, eje = ejeY(Math.max.apply(null, vals.concat([0]))), max = eje.top;
      var x = function (i) { return L + (n > 1 ? i / (n - 1) : 0.5) * iw; };
      var y = function (v) { return T + ih - (v / max) * ih; };
      plot.innerHTML = "";
      if (suave && !vals.some(function (v) { return v > 0; })) {   // nada que dibujar: se dice, no se deja la gráfica plana
        var vacio = document.createElement("div");
        vacio.className = "empty mc-empty";
        vacio.innerHTML = "<h3>No hay datos que ver</h3><p class=\"muted\">" + (data.totals.sent
          ? "Aún no hay " + (state.metric === "earned" ? "dinero ganado" : METRICA[state.metric].toLowerCase()) + " en este periodo. Prueba con otro filtro o con un periodo más largo."
          : "Aún no hay clips en este periodo. Cuando los tengas, aquí verás cómo crecen sus vistas y lo que ganas.") + "</p>";
        plot.appendChild(vacio);
        geo = null;
        var t0v = data.totals[state.metric] || 0;
        box.querySelector("[data-chart-total]").textContent = cifra(state.metric, t0v);
        box.querySelector("[data-chart-label]").textContent = METRICA[state.metric];
        box.setAttribute("data-metric", state.metric);
        return function () {};
      }
      var svg = el("svg", { viewBox: "0 0 " + W + " " + H, width: W, height: H, role: "img",
        "aria-label": METRICA[state.metric] + " " + (state.mode === "daily" ? "por día" : "acumuladas") }, plot);
      for (var gv = 0; gv <= max + eje.step / 2; gv += eje.step) {   // rejilla y eje Y
        var gy = y(gv);
        el("line", { x1: L, x2: W - R, y1: gy, y2: gy, class: "an-grid" + (morph ? " an-fade" : "") }, svg);
        el("text", { x: L - 8, y: gy + 4, class: "an-axis" + (morph ? " an-fade" : "") , "text-anchor": "end" }, svg).textContent = corto(gv);
      }
      var base = T + ih, ys = vals.map(function (v) { return y(v); }), linePath = null, fillPath = null, bars = [];
      var step = Math.max(1, Math.ceil(n / (W < 560 ? 4 : 7)));   // fechas del eje X
      for (var i = 0; i < n; i += step) el("text", { x: x(i), y: H - 8, class: "an-axis", "text-anchor": "middle" }, svg).textContent = fechaCorta(labels[i]);
      var trazo = function (arr) {
        var xs = arr.map(function (_, i) { return x(i); });
        return suave ? curva(xs, arr) : arr.map(function (yy, i) { return (i ? "L" : "M") + xs[i].toFixed(1) + " " + yy.toFixed(1); }).join(" ");
      };
      if (state.mode === "daily" && !suave) {              // barras por día
        var bw = Math.max(2, Math.min(18, iw / n * 0.7));
        vals.forEach(function (v, i) { bars.push(el("rect", { x: x(i) - bw / 2, y: v > 0 ? ys[i] : base, width: bw, height: v > 0 ? Math.max(1, base - ys[i]) : 0, rx: Math.min(4, bw / 2), class: "an-barv" }, svg)); });
      } else {                                             // línea con relleno
        var d = trazo(ys);
        var defs = el("defs", {}, svg), grad = el("linearGradient", { id: "an-fill-g", x1: 0, x2: 0, y1: 0, y2: 1 }, defs);
        el("stop", { offset: "0%", class: "an-g0" }, grad); el("stop", { offset: "100%", class: "an-g1" }, grad);
        var clip = el("clipPath", { id: uid + "-c" }, defs), rect = el("rect", { x: 0, y: 0, width: W, height: H }, clip);
        fillPath = el("path", { d: d + " L" + x(n - 1).toFixed(1) + " " + base + " L" + x(0).toFixed(1) + " " + base + " Z", fill: "url(#an-fill-g)", "clip-path": "url(#" + uid + "-c)" }, svg);
        linePath = el("path", { d: d, class: "an-line", "clip-path": "url(#" + uid + "-c)" }, svg);
      }
      function ponY(arr) {                                 // mueve la línea / las barras a estas alturas (en píxeles)
        if (linePath) {
          var dd = trazo(arr);
          linePath.setAttribute("d", dd);
          fillPath.setAttribute("d", dd + " L" + x(n - 1).toFixed(1) + " " + base + " L" + x(0).toFixed(1) + " " + base + " Z");
        } else {
          bars.forEach(function (b, i) { var h = Math.max(0, base - arr[i]); b.setAttribute("y", base - h); b.setAttribute("height", h); });
        }
      }
      if (morph && geo && geo.mode === state.mode && geo.ys.length === n && geo.W === W && !calma) {
        var desde = geo.ys, t0 = null, dur = 380, token = {};
        transicion = token;
        ponY(desde);
        (function paso(t) {
          if (transicion !== token || !svg.isConnected) return;
          t0 = t0 || t;
          var k = Math.min(1, (t - t0) / dur), e = 1 - Math.pow(1 - k, 3);
          ponY(desde.map(function (a, i) { return a + (ys[i] - a) * e; }));
          if (k < 1) requestAnimationFrame(paso);
        })(performance.now());
      }
      geo = { mode: state.mode, ys: ys, W: W };
      var play = function () {};
      if (anim && !calma) {
        if (bars.length) {                                 // barras: crecen desde abajo una detrás de otra
          var stagger = Math.min(18, 500 / Math.max(1, bars.length));
          bars.forEach(function (b, i) { b.classList.add("an-grow"); b.style.transitionDelay = (i * stagger) + "ms"; });
          play = function () { bars.forEach(function (b) { b.classList.add("in"); }); };
        } else {                                           // línea y relleno: se dibujan de izquierda a derecha
          rect.setAttribute("width", L);
          play = function () {
            var t0 = null, dur = 900;
            (function paso(t) {
              if (!rect.isConnected) return;
              t0 = t0 || t;
              var k = Math.min(1, (t - t0) / dur), e = 1 - Math.pow(1 - k, 3);
              rect.setAttribute("width", L + (W - L) * e);
              if (k < 1) requestAnimationFrame(paso);
            })(performance.now());
          };
        }
      }
      var guide = el("line", { x1: 0, x2: 0, y1: T, y2: T + ih, class: "an-guide" }, svg);
      var dot = el("circle", { cx: 0, cy: 0, r: 5, class: "an-dot" }, svg);
      var tip = document.createElement("div"); tip.className = "an-tip"; plot.appendChild(tip);
      var cur = -1, frame = 0, lastX = 0, shown = false;
      function place(i) {                                  // mueve guía, punto y etiqueta (con transición CSS)
        var r = svg.getBoundingClientRect(), cx = x(i), cy = y(vals[i]), jump = !shown;
        if (jump) [guide, dot, tip].forEach(function (e) { e.style.transition = "none"; });   // al entrar aparece en su sitio, sin deslizar desde la izquierda
        guide.style.transform = "translate(" + cx + "px,0)";
        dot.style.transform = "translate(" + cx + "px," + cy + "px)";
        tip.innerHTML = "<b>" + cifra(state.metric, vals[i]) + "</b><span>" + fechaCorta(labels[i]) + "</span>";
        var tw = tip.offsetWidth, th = tip.offsetHeight, sx = r.width / W;
        var left = Math.min(Math.max(cx * sx - tw / 2, 0), r.width - tw), top = Math.max(0, cy * r.height / H - th - 12);
        tip.style.transform = "translate(" + left + "px," + top + "px)";
        if (jump) {
          void tip.offsetWidth;
          [guide, dot, tip].forEach(function (e) { e.style.transition = ""; });
          box.classList.add("an-hover"); shown = true;
        }
      }
      function tick() {
        frame = 0;
        var r = svg.getBoundingClientRect(), px = lastX - r.left;
        var i = Math.round((px * W / r.width - L) / iw * (n - 1)); i = Math.max(0, Math.min(n - 1, i));
        if (i !== cur) { cur = i; place(i); }
      }
      function move(ev) {
        lastX = ev.touches ? ev.touches[0].clientX : ev.clientX;
        if (!frame) frame = requestAnimationFrame(tick);
      }
      function leave() { box.classList.remove("an-hover"); shown = false; cur = -1; }
      svg.addEventListener("mousemove", move); svg.addEventListener("touchmove", move, { passive: true });
      svg.addEventListener("mouseleave", leave); svg.addEventListener("touchend", leave);
      var tot = data.totals[state.metric] || 0;
      box.querySelector("[data-chart-total]").textContent = suave && ["earned", "spent", "paid"].indexOf(state.metric) < 0 && tot >= 10000 ? corto(tot) : cifra(state.metric, tot);
      box.setAttribute("data-metric", state.metric);
      box.querySelector("[data-chart-label]").textContent = METRICA[state.metric];
      return play;
    }
    box.addEventListener("click", function (e) {
      var b = e.target.closest("[data-chart-mode],[data-chart-metric]");
      if (!b) return;
      var key = b.hasAttribute("data-chart-mode") ? "mode" : "metric";
      var nuevo = b.getAttribute(key === "mode" ? "data-chart-mode" : "data-chart-metric");
      if (state[key] === nuevo) return;
      state[key] = nuevo;
      b.parentNode.querySelectorAll("button").forEach(function (x) { x.classList.toggle("on", x === b); });
      if (b.hasAttribute("data-chart-kpi")) {              // tarjeta de arriba: la fila de «Me gusta / Comentarios…» vuelve a Visualizaciones
        box.querySelectorAll(".mc-sub button").forEach(function (x) { x.classList.toggle("on", x.getAttribute("data-chart-metric") === "views"); });
      } else if (key === "metric") {                       // «Me gusta», etc. pertenecen a la tarjeta de Visualizaciones
        box.querySelectorAll("[data-chart-kpi]").forEach(function (x) { x.classList.toggle("on", x.getAttribute("data-chart-metric") === "views"); });
      }
      if (key === "mode" || suave && b.hasAttribute("data-chart-kpi")) {   // otro tipo de gráfica: animación de entrada
        var go = draw(true); requestAnimationFrame(function () { requestAnimationFrame(go); });
        return;
      }
      draw(false, true);                                  // misma gráfica, otra métrica: se transforma suave
    });
    var entrada = draw(true);                              // la animación de entrada sale cuando la gráfica aparece en pantalla
    if ("IntersectionObserver" in window) {
      var io = new IntersectionObserver(function (es) {
        if (es.some(function (e) { return e.isIntersecting; })) { io.disconnect(); requestAnimationFrame(function () { requestAnimationFrame(entrada); }); }
      }, { threshold: 0.25 });
      io.observe(plot);
      limpiar.push(tuyo(box, function () { io.disconnect(); }));
    } else { entrada(); }
    var redraw = function () { draw(false); };
    window.addEventListener("resize", redraw);
    limpiar.push(tuyo(box, function () { window.removeEventListener("resize", redraw); }));
  });
}

// Chat de soporte
function iniciarChat(chat) {
  chat = chat || document.querySelector("main [data-chat]");
  if (!chat || chat.dataset.ready) return;
  var enVentanita = !!chat.closest("#chat-pop");      // el chat pequeño sigue abierto al cambiar de página
  chat.dataset.ready = "1";
  var log = chat.querySelector(".chat-log");
  var form = chat.querySelector("[data-chat-form]");
  var box = form.querySelector("textarea");
  var errorBox = chat.querySelector(".chat-error");
  var fileIn = form.querySelector("[data-chat-file]");
  var fileBadge = form.querySelector(".chat-attach-n");
  var fileName = chat.querySelector("[data-chat-file-name]");
  var after = parseInt(chat.getAttribute("data-after") || "0", 10);
  var sending = false;

  function showFile() {
    var f = fileIn && fileIn.files && fileIn.files[0];
    if (fileBadge) fileBadge.hidden = !f;
    if (fileName) { fileName.hidden = !f; fileName.textContent = f ? "📷 " + f.name + " · pulsa Enviar" : ""; }
  }
  function scrollDown() { log.scrollTop = log.scrollHeight; }
  function add(m) {
    if (log.querySelector('[data-id="' + m.id + '"]')) return;
    var empty = chat.querySelector(".chat-empty");
    if (empty) empty.remove();
    var row = document.createElement("div");
    row.className = "msg " + (m.mine ? "mine" : "theirs") + (m.agent ? " agent" : "");
    row.setAttribute("data-id", m.id);
    if (m.agent) {
      var av = document.createElement("span"); av.className = "avatar msg-av"; av.setAttribute("aria-hidden", "true");
      if (m.agent.avatar) { var ai = document.createElement("img"); ai.src = m.agent.avatar; ai.alt = ""; ai.referrerPolicy = "no-referrer"; av.appendChild(ai); }
      else av.textContent = m.agent.initial;
      row.appendChild(av);
    }
    if (m.image) {
      var a = document.createElement("a");
      a.className = "chat-photo"; a.href = m.image; a.target = "_blank"; a.rel = "noopener";
      var img = document.createElement("img"); img.src = m.image; img.alt = "Foto";
      img.addEventListener("load", scrollDown);
      a.appendChild(img); row.appendChild(a);
    }
    if (m.body) { var bubble = document.createElement("div"); bubble.className = "bubble"; bubble.textContent = m.body; row.appendChild(bubble); }
    var meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = (m.mine ? "" : m.who + " · ") + m.ago;
    meta.title = m.at + " UTC";
    row.appendChild(meta);
    log.appendChild(row);
    after = Math.max(after, m.id);
    scrollDown();
  }
  function poll() {
    if (document.hidden || !document.body.contains(chat) || chat.closest("[hidden]")) return;
    fetch(chat.getAttribute("data-poll") + "?after=" + after, { headers: { "X-Requested-With": "fetch" }, credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { if (d) d.messages.forEach(add); })
      .catch(function () {});
  }
  function grow() { box.style.height = "auto"; box.style.height = Math.min(box.scrollHeight, 160) + "px"; }

  if (fileIn) fileIn.addEventListener("change", showFile);
  box.addEventListener("input", grow);
  box.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit ? form.requestSubmit() : form.submit(); }
  });
  form.addEventListener("submit", function (e) {
    e.preventDefault();
    var hasFile = fileIn && fileIn.files && fileIn.files.length;
    if (sending || (!box.value.trim() && !hasFile)) return;
    sending = true; errorBox.hidden = true;
    fetch(form.action, { method: "POST", body: new FormData(form), headers: { "X-Requested-With": "fetch" }, credentials: "same-origin" })
      .then(function (r) {
        if (r.status === 413) return { ok: false, d: { error: "La foto es demasiado grande (máximo 8 MB)." } };
        return r.json().then(function (d) { return { ok: r.ok, d: d }; });
      })
      .then(function (res) {
        if (res.ok && res.d.ok) { add(res.d.message); box.value = ""; if (fileIn) fileIn.value = ""; showFile(); grow(); }
        else { errorBox.textContent = res.d.error || "No se pudo enviar. Inténtalo otra vez."; errorBox.hidden = false; }
      })
      .catch(function () { errorBox.textContent = "Sin conexión. Inténtalo otra vez."; errorBox.hidden = false; })
      .then(function () { sending = false; box.focus(); });
  });
  scrollDown();
  var timer = setInterval(poll, 8000);
  var onVisible = function () { if (!document.hidden) poll(); };
  document.addEventListener("visibilitychange", onVisible);
  if (!enVentanita) limpiar.push(tuyo(chat, function () { clearInterval(timer); document.removeEventListener("visibilitychange", onVisible); }));
  chat._poll = poll;
  chat._scroll = scrollDown;
}

// =============================================================== barras de pestañas con pastilla que se desliza
// Dos capas: abajo, las pestañas en gris; arriba, una copia en blanco sobre negro que solo se ve a través de un
// recorte con forma de pastilla. Lo único que se anima es ese recorte, así que el texto se lee siempre
// (blanco donde está la pastilla, gris donde no) y la barra de verdad no se toca.
// Las barras que cambian de página llevan data-pill-nav y data-turbo-permanent: la barra no se repinta al cambiar
// de página (Turbo deja la misma), así que la pastilla se desliza como en las de la misma página. Al llegar, la
// pestaña activa se comprueba con la dirección (sirve también para «atrás») y los números se copian de la página nueva.
var PILL_MS = 320;
function pillItems(box) { return box.querySelectorAll(":scope > a, :scope > button"); }
function pillCambiaPagina(it) {
  var h = it.tagName === "A" ? (it.getAttribute("href") || "") : "";
  return h !== "" && h.charAt(0) !== "#" && !it.hasAttribute("data-tab");
}
// El recorrido se anima a mano, fotograma a fotograma (no con una transición CSS): así sigue suave aunque Turbo
// saque y vuelva a meter la barra al cambiar de página, que es lo que antes cortaba la animación.
function pillPaint(pill, q) {
  pill.style.clipPath = "inset(" + q[0].toFixed(2) + "px " + q[1].toFixed(2) + "px " + q[2].toFixed(2) + "px " + q[3].toFixed(2) + "px round 999px)";
  pill._q = q;
  if (pill._sh) {                                  // barras claras: la sombra de la pastilla blanca va por debajo, a la par
    var s = pill._sh.style;
    s.left = q[3] + "px"; s.top = q[0] + "px";
    s.width = Math.max(0, pill._W - q[1] - q[3]) + "px"; s.height = Math.max(0, pill._H - q[0] - q[2]) + "px";
  }
}
function pillClip(box, pill, l, t, w, h, animate) {
  var W = box.scrollWidth, H = box.clientHeight;
  var to = [t, Math.max(0, W - l - w), Math.max(0, H - t - h), l];
  pill._W = W; pill._H = H;
  pill.style.transition = "none";
  if (pill._raf) { cancelAnimationFrame(pill._raf); pill._raf = 0; }
  var from = pill._q, quieto = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  box._pillAt = { l: l, w: w, t: Date.now(), anim: !!animate };
  if (!animate || !from || quieto) { pillPaint(pill, to); return; }
  var t0 = performance.now();
  (function paso(now) {
    var p = Math.min(1, (now - t0) / PILL_MS), e = 1 - Math.pow(1 - p, 3.4);
    pillPaint(pill, [0, 1, 2, 3].map(function (k) { return from[k] + (to[k] - from[k]) * e; }));
    pill._raf = p < 1 ? requestAnimationFrame(paso) : 0;
  })(t0);
}
function pillSync(box, target, animate) {
  var pill = box._pill;
  if (!pill) return;
  var cs = getComputedStyle(box), its = pillItems(box);
  pill.style.width = "0px";                       // que no cuente al medir el ancho real de la barra
  pill.style.width = box.scrollWidth + "px";
  pill.style.height = box.clientHeight + "px";
  // La copia de arriba: mismo texto, misma letra y mismo sitio que cada pestaña (misma etiqueta: le valen los mismos estilos)
  if (pill.children.length !== its.length) {
    pill.textContent = "";
    for (var k = 0; k < its.length; k++) {
      var el = document.createElement(its[k].tagName);
      el.tabIndex = -1;
      if (el.tagName === "BUTTON") el.type = "button";
      pill.appendChild(el);
    }
  }
  for (var i = 0; i < its.length; i++) {
    var it = its[i], c = pill.children[i], s = getComputedStyle(it);
    if (c._html !== it.innerHTML) { c.innerHTML = it.innerHTML; c._html = it.innerHTML; }
    c.className = it.className.replace(/(^|\s)on(\s|$)/, " ");
    c.style.cssText = "left:" + it.offsetLeft + "px;top:" + it.offsetTop + "px;width:" + it.offsetWidth + "px;height:" + it.offsetHeight +
      "px;padding:" + s.padding + ";font:" + s.font + ";letter-spacing:" + s.letterSpacing + ";text-align:" + s.textAlign;
  }
  target = target || box.querySelector(":scope > .on");
  if (!target) { pill.style.opacity = 0; return; }
  pill.style.opacity = 1;
  var ic = box.classList.contains("tabs-ico") && target.querySelector("svg");
  if (ic) {                                        // menú de arriba: el recuadro gris va solo detrás del icono
    var b = box.getBoundingClientRect(), r = ic.getBoundingClientRect();
    pillClip(box, pill, r.left - b.left + box.scrollLeft - 11, r.top - b.top - 5, r.width + 22, r.height + 10, animate);
  } else pillClip(box, pill, target.offsetLeft, target.offsetTop, target.offsetWidth, target.offsetHeight, animate);
  box._pillTarget = target;
}
function pillAnimando(box) { var at = box._pillAt; return !!(at && at.anim && Date.now() - at.t < PILL_MS + 60); }
function montarPastilla(box) {
  if (box.classList.contains("tabs") && window.matchMedia("(max-width: 640px)").matches) return;   // en el móvil es el menú de abajo
  if (box._srv === undefined) box._srv = pillIdx(box, box.querySelector(":scope > .on"));
  if (box._pill && box._pill.isConnected) { if (!pillAnimando(box)) pillSync(box, null, false); return; }
  // Una página restaurada (atrás) trae la copia vieja sin sus escuchas: se rehace
  box.querySelectorAll(":scope > .pill").forEach(function (old) { old.remove(); });
  var pill = document.createElement("span");
  pill.className = "pill";
  pill.setAttribute("aria-hidden", "true");
  var ico = box.classList.contains("tabs-ico");
  if (ico || box.classList.contains("an-tg")) {   // gráficas: pastilla blanca con sombra; menú de arriba: recuadro gris tras el icono
    if (!ico) box.classList.add("pill-light");
    box.querySelectorAll(":scope > .pill-sh").forEach(function (o) { o.remove(); });
    var sh = document.createElement("span"); sh.className = "pill-sh"; sh.setAttribute("aria-hidden", "true");
    box.appendChild(sh); pill._sh = sh;
  }
  box.appendChild(pill);
  box._pill = pill;
  box.classList.add("has-pill");
  pillSync(box, box.querySelector(":scope > .on"), false);   // la pastilla aparece ya en su sitio, sin deslizarse
  if (box._pillBound) return;
  box._pillBound = true;
  box.addEventListener("click", function (e) {
    var it = e.target.closest("a, button");
    if (!it || it.parentNode !== box || e.metaKey || e.ctrlKey || e.shiftKey || e.button) return;
    // Si la pestaña cambia de página (cambia el enlace de arriba), no hay deslizamiento: la página nueva trae la
    // pastilla ya en su sitio. Solo se desliza en las pestañas que cambian dentro de la misma página.
    if (pillCambiaPagina(it) && !box.hasAttribute("data-pill-nav")) return;
    pillItems(box).forEach(function (x) { if (x !== it) { x.classList.remove("on"); x.removeAttribute("aria-current"); } });
    it.classList.add("on");
    pillSync(box, it, true);
  });
  if (window.ResizeObserver) {                   // cambia el ancho (móvil girado, letra cargada, números nuevos): se recoloca
    var ro = new ResizeObserver(function () {
      if (!box.isConnected) { ro.disconnect(); return; }
      var at = box._pillAt;
      if (at && at.anim && Date.now() - at.t < PILL_MS + 60) return;      // a mitad de animación no se toca
      pillSync(box, box._pillTarget && box._pillTarget.isConnected ? box._pillTarget : null, false);
    });
    ro.observe(box);
  }
}
function pillIdx(box, el) { return el ? Array.prototype.indexOf.call(pillItems(box), el) : -1; }
function pastillaOrden() {
  document.querySelectorAll(".seg, .sortbar, #pn-top, .an-tg, .fchips").forEach(montarPastilla);
}
// La copia que Turbo guarda para «atrás» queda como la manda el servidor
document.addEventListener("turbo:before-cache", function () {
  // La copia guardada para «atrás» lleva marcada la pestaña de esa página (no la que se acaba de pulsar)
  document.querySelectorAll("[data-pill-nav]").forEach(function (box) {
    var its = pillItems(box);
    if (box._srv >= 0 && its[box._srv]) its.forEach(function (x, i) { x.classList.toggle("on", i === box._srv); });
  });
  document.querySelectorAll(".seg.has-pill, .sortbar.has-pill").forEach(function (box) { if (box._pill && !pillAnimando(box)) pillSync(box, null, false); });
});
// Barras que cambian de página: la de la página nueva se descarta y se queda la de ahora, con sus números al día.
// Si la barra nueva es distinta (otras pestañas), se pinta la nueva sin más.
function pillAdoptar(root) {
  if (!root || !root.querySelectorAll) return;
  root.querySelectorAll("[data-pill-nav][id]").forEach(function (nw) {
    var old = document.getElementById(nw.id);
    if (!old || old === nw) return;
    var a = pillItems(old), b = pillItems(nw), igual = a.length === b.length;
    for (var i = 0; igual && i < a.length; i++) igual = a[i].getAttribute("href") === b[i].getAttribute("href");
    if (!igual) { nw.removeAttribute("data-turbo-permanent"); return; }
    for (var k = 0; k < a.length; k++) if (a[k].innerHTML !== b[k].innerHTML) a[k].innerHTML = b[k].innerHTML;
    old._srvNext = pillIdx(nw, nw.querySelector(":scope > .on"));     // la pestaña que marca la página nueva
  });
}
document.addEventListener("turbo:before-render", function (e) { pillAdoptar(e.detail && e.detail.newBody); });
document.addEventListener("turbo:before-frame-render", function (e) { pillAdoptar(e.detail && e.detail.newFrame); });
// La pestaña activa al llegar: la que coincide con la dirección (ruta igual y sus parámetros en la dirección);
// si ninguna coincide, la que marca la página nueva. Si la pastilla no está ya ahí, se desliza hasta ella.
function pillSegunDireccion() {
  var cur = new URL(location.href);
  document.querySelectorAll("[data-pill-nav]").forEach(function (box) {
    var its = pillItems(box), best = null, score = -1;
    its.forEach(function (it) {
      var h = it.getAttribute("href"); if (!h) return;
      var u = new URL(h, location.href), ok = u.pathname === cur.pathname, n = 0;
      u.searchParams.forEach(function (v, k) { if (cur.searchParams.get(k) === v) n++; else ok = false; });
      if (ok && n > score) { best = it; score = n; }
    });
    if (!best && box._srvNext >= 0) best = its[box._srvNext];
    box._srvNext = undefined;
    if (!best) best = box.querySelector(":scope > .on");
    if (!best) return;
    box._srv = pillIdx(box, best);
    its.forEach(function (x) { var on = x === best; x.classList.toggle("on", on); if (on) x.setAttribute("aria-current", "page"); else x.removeAttribute("aria-current"); });
    if (box._pill && box._pillTarget !== best) pillSync(box, best, true);
  });
}
document.addEventListener("turbo:load", pillSegunDireccion);
document.addEventListener("turbo:frame-load", pillSegunDireccion);
document.addEventListener("turbo:render", pastillaOrden);          // antes de que se pinte: sin un fotograma sin pastilla
document.addEventListener("turbo:frame-render", pastillaOrden);


// =============================================================== cuándo se ejecuta
// Algo que se limpia solo cuando desaparece su parte de la página (al cambiar un marco) o al salir de la página
function tuyo(el, fn) { fn.el = el; return fn; }
function salirDePagina() {
  limpiar.forEach(function (f) { try { f(); } catch (e) {} });
  limpiar = [];
}
if (window.Turbo) {
  document.addEventListener("turbo:load", pagina);              // también en la primera carga
  // Marcos: una pestaña o un filtro cambia solo su parte de la página. Lo de fuera (la gráfica, la cabecera) no se toca.
  document.addEventListener("turbo:before-frame-render", function (e) {
    var fr = e.target;
    limpiar = limpiar.filter(function (f) {
      if (f.el && fr.contains(f.el)) { try { f(); } catch (err) {} return false; }
      return true;
    });
  });
  // Si la respuesta no trae el marco (p. ej. una redirección a otra página), se abre la página entera
  document.addEventListener("turbo:frame-missing", function (e) {
    e.preventDefault();
    e.detail.visit(e.detail.response);
  });
  // Dentro de un marco, el fundido solo va en el contenido: las barras de pestañas, el buscador y los filtros no parpadean
  var FR_FIJO = ".seg, .tl, .sortbar, .fchips, .vr-bar, form[data-autosubmit], .mc-tools";
  function contenidoMarco(fr) {
    var out = [];
    (function walk(el) {
      Array.prototype.forEach.call(el.children, function (c) {
        if (c.matches(FR_FIJO)) return;
        if (c.querySelector(FR_FIJO)) walk(c); else out.push(c);
      });
    })(fr);
    return out;
  }
  document.addEventListener("turbo:before-fetch-request", function (e) {
    var fr = e.target;
    if (fr && fr.tagName === "TURBO-FRAME") contenidoMarco(fr).forEach(function (c) { c.classList.add("fr-dim"); });
  });
  document.addEventListener("turbo:frame-load", function (e) {
    pagina();                                                    // solo arranca lo nuevo: lo de fuera ya está listo
    contenidoMarco(e.target).forEach(function (c) {
      c.classList.remove("fr-dim", "fr-in"); void c.offsetWidth; c.classList.add("fr-in");
    });
  });
  document.addEventListener("turbo:before-render", salirDePagina);
  // Animación de entrada: solo el contenido (la barra de arriba no se mueve) y solo al navegar
  // Si solo cambia una pestaña o un filtro de la misma página (misma dirección, distinto «?…»),
  // no hay animación y la página se queda donde estaba, como si fuera una pestaña.
  var pathActual = location.pathname, pathNuevo = null, scrollGuardado = null;
  document.addEventListener("turbo:visit", function (e) {
    if (e.detail.action === "restore") { pathNuevo = null; return; }   // «atrás»: Turbo ya recoloca la página
    try { pathNuevo = new URL(e.detail.url, location.href).pathname; } catch (err) { pathNuevo = null; }
  });
  document.addEventListener("turbo:before-render", function (e) {
    var misma = pathNuevo !== null && pathNuevo === pathActual;
    scrollGuardado = misma ? window.scrollY : null;
    // Misma página (pestaña, filtro o un botón que vuelve aquí): Turbo no la sube arriba, así no salta
    try { var v = Turbo.session.navigator.currentVisit; if (misma && v) { v.scrolled = true; scrollGuardado = null; } } catch (err) {}
    var main = e.detail.newBody && e.detail.newBody.querySelector("#contenido");
    if (main && !misma) main.classList.add("entra");
  });
  document.addEventListener("turbo:render", function () {
    if (scrollGuardado !== null) window.scrollTo(0, scrollGuardado);
  });
  document.addEventListener("turbo:load", function () {
    if (scrollGuardado !== null) { window.scrollTo(0, scrollGuardado); scrollGuardado = null; }
    pathActual = location.pathname; pathNuevo = null;
  });
  // Antes de guardar la página para el botón «atrás»: cerrar menús y ventanas y quitar el «cargando» de los botones
  document.addEventListener("turbo:before-cache", function () {
    document.querySelectorAll("details[open]").forEach(function (d) { d.removeAttribute("open"); });
    document.querySelectorAll("dialog[open]").forEach(function (d) { d.close(); });
    document.querySelectorAll(".btn.busy").forEach(function (b) { b.classList.remove("busy"); b.removeAttribute("aria-busy"); });
    document.querySelectorAll("form[data-sending='1']").forEach(function (f) { f.dataset.sending = ""; });
    document.querySelectorAll(".msg-notice-slot").forEach(function (s) { s.innerHTML = ""; });
  });
} else {
  document.addEventListener("DOMContentLoaded", pagina);
}
// Al volver atrás con la página guardada por el navegador, los botones dejan de estar «cargando»
window.addEventListener("pageshow", function (ev) {
  if (!ev.persisted) return;
  document.querySelectorAll("form[data-sending='1']").forEach(function (f) { f.dataset.sending = ""; });
  document.querySelectorAll(".btn.busy").forEach(function (b) { b.classList.remove("busy"); b.removeAttribute("aria-busy"); });
});

// Turbo pide la página al pasar el ratón por un enlace. Estos enlaces hacen algo al abrirse
// (cerrar sesión, marcar mensajes como leídos…), así que no se piden por adelantado.
document.addEventListener("turbo:before-prefetch", function (e) {
  var a = e.target, path = "";
  try { path = new URL(a.href, location.href).pathname; } catch (err) { return; }
  if (/^\/(salir|auth\/|login|empezar|cron\/|soporte|admin\/mensajes|avisos|static\/|portada\/)/.test(path) || /\.csv$/.test(path)) e.preventDefault();
});

// Filtros que se aplican solos al cambiarlos (sin botón)
document.addEventListener("change", function (e) {
  var f = e.target.closest("form[data-autosubmit]");
  if (f) f.requestSubmit ? f.requestSubmit() : f.submit();
});

// =============================================================== revisión de clips: atajos de teclado
// ← / → (o ↑ / ↓, J / K) cambian de clip · A aprueba el clip en el que estás · R abre el rechazo. No hacen nada mientras escribes.
var clipActual = 0;
function irAClip(i) {
  var cards = document.querySelectorAll("[data-card]");
  if (!cards.length) return;
  clipActual = Math.max(0, Math.min(cards.length - 1, i));
  cards.forEach(function (c, j) { c.classList.toggle("focus", j === clipActual); });
  cards[clipActual].scrollIntoView({ behavior: "smooth", block: "start" });
  cards[clipActual].focus({ preventScroll: true });
}
document.addEventListener("keydown", function (e) {
  if (!document.querySelector("[data-review]") || e.metaKey || e.ctrlKey || e.altKey) return;
  var t = e.target;
  if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable || t.closest(".dd"))) return;
  var k = e.key;
  if (k === "ArrowRight" || k === "ArrowDown" || k === "j") { e.preventDefault(); irAClip(clipActual + 1); return; }
  if (k === "ArrowLeft" || k === "ArrowUp" || k === "k") { e.preventDefault(); irAClip(clipActual - 1); return; }
  var card = document.querySelectorAll("[data-card]")[clipActual];
  if (!card) return;
  if (k === "a" || k === "A") {
    var b = card.querySelector("[data-key-approve]:not([disabled])");
    if (b) { e.preventDefault(); b.form.requestSubmit ? b.form.requestSubmit(b) : b.click(); }
  } else if (k === "r" || k === "R") {
    var r = card.querySelector("[data-key-reject]");
    if (r) { e.preventDefault(); if (r.tagName === "SUMMARY") { r.parentNode.open = true; var sel = r.parentNode.querySelector(".dd-btn, select"); if (sel) sel.focus(); } else r.click(); }
  }
});
document.addEventListener("click", function (e) {
  var n = e.target.closest("[data-next]");
  if (!n) return;
  e.preventDefault();
  var cards = Array.prototype.slice.call(document.querySelectorAll("[data-card]"));
  irAClip(cards.indexOf(n.closest("[data-card]")) + 1);
});
document.addEventListener("focusin", function (e) {
  var c = e.target.closest && e.target.closest("[data-card]");
  if (!c) return;
  var cards = Array.prototype.slice.call(document.querySelectorAll("[data-card]"));
  clipActual = cards.indexOf(c);
});
// Cerrar el «Rechazar» y el «···» al pulsar fuera
document.addEventListener("click", function (e) {
  document.querySelectorAll("details.cr-reject[open], details.cr-more[open]").forEach(function (d) { if (!d.contains(e.target)) d.removeAttribute("open"); });
});

// Gráfica pequeña de cada clip en la revisión (vistas, me gusta, comentarios o compartidos día a día)
function graficasClips() {
  document.querySelectorAll("[data-mini-chart]").forEach(function (box) {
    var data; try { data = JSON.parse(box.getAttribute("data-mini-chart")); } catch (e) { data = null; }
    var card = box.closest("[data-card]");
    var metric = "views";
    function draw() {
      box.innerHTML = "";
      if (!data || !data.labels || data.labels.length < 2) { box.innerHTML = '<p class="small muted cr-nochart">La gráfica sale cuando haya lecturas de varios días.</p>'; return; }
      var W = box.clientWidth || 500, H = box.clientHeight || 170, L = 8, R = 44, T = 10, B = 22;
      var vals = data[metric] || [], n = vals.length, max = Math.max.apply(null, vals.concat([1]));
      var ax = ejeY(max), top = ax.top || max, ticks = [];
      for (var tv0 = 0; tv0 <= top + 1e-9; tv0 += ax.step) ticks.push(tv0);
      var NS = "http://www.w3.org/2000/svg", svg = document.createElementNS(NS, "svg");
      svg.setAttribute("viewBox", "0 0 " + W + " " + H); svg.setAttribute("class", "cr-svg");
      function el(tag, attrs) { var x = document.createElementNS(NS, tag); for (var k in attrs) x.setAttribute(k, attrs[k]); svg.appendChild(x); return x; }
      var iw = W - L - R, ih = H - T - B;
      var X = function (i) { return L + i / (n - 1) * iw; }, Y = function (v) { return T + ih - v / top * ih; };
      ticks.forEach(function (tv) {
        el("line", { x1: L, x2: L + iw, y1: Y(tv), y2: Y(tv), class: "cr-grid" });
        var t = el("text", { x: W - 4, y: Y(tv) + 4, class: "cr-ax", "text-anchor": "end" }); t.textContent = corto(tv);
      });
      var step = Math.max(1, Math.ceil(n / 5));
      for (var i = 0; i < n; i += step) { var t2 = el("text", { x: X(i), y: H - 4, class: "cr-ax", "text-anchor": i === 0 ? "start" : "middle" }); t2.textContent = fechaCorta(data.labels[i]); }
      var d = vals.map(function (v, i) { return (i ? "L" : "M") + X(i).toFixed(1) + " " + Y(v).toFixed(1); }).join(" ");
      el("path", { d: d + " L" + X(n - 1) + " " + (T + ih) + " L" + X(0) + " " + (T + ih) + " Z", class: "cr-area" });
      el("path", { d: d, class: "cr-line" });
      var tip = document.createElement("div"); tip.className = "cr-tip"; tip.hidden = true;
      box.appendChild(svg); box.appendChild(tip);
      svg.addEventListener("mousemove", function (ev) {
        var r = svg.getBoundingClientRect(), x = (ev.clientX - r.left) / r.width * W;
        var i = Math.max(0, Math.min(n - 1, Math.round((x - L) / iw * (n - 1))));
        tip.hidden = false; tip.textContent = fechaCorta(data.labels[i]) + " · " + numES(vals[i]);
        tip.style.left = (X(i) / W * 100) + "%"; tip.style.top = (Y(vals[i]) / H * 100) + "%";
      });
      svg.addEventListener("mouseleave", function () { tip.hidden = true; });
    }
    if (card) card.querySelectorAll("[data-metric]").forEach(function (b) {
      b.addEventListener("click", function () {
        metric = b.getAttribute("data-metric");
        card.querySelectorAll("[data-metric]").forEach(function (x) { x.classList.toggle("on", x === b); });
        draw();
      });
    });
    draw();
    var redraw = function () { draw(); };
    window.addEventListener("resize", redraw);
    limpiar.push(function () { window.removeEventListener("resize", redraw); });
  });
  clipActual = 0;
}

// Casilla «Todos»: marca o desmarca todas las de la lista
document.addEventListener("change", function (e) {
  var all = e.target.closest("[data-check-all]");
  if (!all) return;
  var name = all.getAttribute("data-check-all");
  document.querySelectorAll("input[type=checkbox][name='" + name + "']").forEach(function (c) { c.checked = all.checked; });
});

// =============================================================== asistente de campaña (5 pasos) con vista previa
function asistente() {
  var wz = document.querySelector("[data-wizard]");
  if (!wz || wz.dataset.ready) return;
  wz.dataset.ready = "1";
  var steps = Array.prototype.slice.call(wz.querySelectorAll(".wz-step"));
  var pills = Array.prototype.slice.call(wz.querySelectorAll("[data-wz-go]"));
  var back = wz.querySelector("[data-wz-back]"), next = wz.querySelector("[data-wz-next]"), save = wz.querySelector("[data-wz-save]");
  var editing = wz.hasAttribute("data-editing");
  var cur = 0;
  wz.classList.add("wz-on");
  function valid(i) {
    var bad = Array.prototype.slice.call(steps[i].querySelectorAll("input,select,textarea")).filter(function (f) { return !f.checkValidity(); });
    if (bad.length) { bad[0].reportValidity(); return false; }
    return true;
  }
  function show(i) {
    cur = Math.max(0, Math.min(steps.length - 1, i));
    steps.forEach(function (s, j) { s.hidden = j !== cur; });
    pills.forEach(function (p, j) { p.classList.toggle("on", j === cur); p.classList.toggle("done", j < cur); p.setAttribute("aria-current", j === cur ? "step" : "false"); });
    back.hidden = cur === 0;
    next.hidden = cur === steps.length - 1;
    save.hidden = !editing && cur !== steps.length - 1;
    var top = wz.getBoundingClientRect().top + window.scrollY - 80;
    if (window.scrollY > top) window.scrollTo(0, top);
    preview();
  }
  next.addEventListener("click", function () { if (valid(cur)) show(cur + 1); });
  back.addEventListener("click", function () { show(cur - 1); });
  pills.forEach(function (p, j) {
    p.addEventListener("click", function () {
      if (j <= cur || editing) return show(j);
      for (var k = cur; k < j; k++) { if (!valid(k)) return show(k); }
      show(j);
    });
  });
  // Si al guardar falta algo en un paso escondido, se abre ese paso
  wz.closest("form").addEventListener("invalid", function (e) {
    var st = e.target.closest(".wz-step");
    if (st && st.hidden) show(steps.indexOf(st));
  }, true);
  // Vista previa en directo de la tarjeta de la campaña
  var pv = document.querySelector("[data-wz-preview]");
  function val(n) { var f = wz.querySelector("[name='" + n + "']"); return f ? f.value.trim() : ""; }
  function num(v) { v = (v || "").replace(/\s/g, ""); if (/^\d{1,3}(\.\d{3})+$/.test(v)) v = v.replace(/\./g, ""); return parseFloat(v.replace(",", ".")) || 0; }
  function money(v) { return "$" + (Math.round(v * 100) / 100).toLocaleString("es-ES", { minimumFractionDigits: v % 1 ? 2 : 0, maximumFractionDigits: 2 }); }
  function preview() {
    if (!pv) return;
    var on = Array.prototype.slice.call(wz.querySelectorAll("input[name^='pf_']:checked")).map(function (c) { return c.name.slice(3); });
    var top = 0;
    on.forEach(function (p) { top = Math.max(top, num(val("rate_k_" + p))); });
    pv.querySelector("[data-pv=name]").textContent = val("song") || "Nombre de la campaña";
    pv.querySelector("[data-pv=artist]").textContent = val("artist") || "Creador";
    pv.querySelector("[data-pv=cpm]").textContent = top ? money(top) + " / 1K vistas" : "— / 1K vistas";
    pv.querySelector("[data-pv=budget]").textContent = num(val("budget")) ? money(num(val("budget"))) : "—";
    pv.querySelectorAll("[data-pv-pf]").forEach(function (i) { i.hidden = on.indexOf(i.getAttribute("data-pv-pf")) < 0; });
  }
  wz.addEventListener("input", preview);
  wz.addEventListener("change", preview);
  show(0);
}

// =============================================================== avisos en este dispositivo (Web Push)
function avisosPush() {
  var box = document.querySelector("[data-push]");
  if (!box || box.dataset.ready) return;
  box.dataset.ready = "1";
  var on = box.querySelector("[data-push-on]"), off = box.querySelector("[data-push-off]"), state = box.querySelector("[data-push-state]");
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
    on.hidden = true; state.textContent = "Este navegador no permite avisos. En iPhone: añade la web a la pantalla de inicio y ábrela desde ahí.";
    return;
  }
  function key(b64) {
    var pad = "=".repeat((4 - b64.length % 4) % 4), raw = atob((b64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
    var out = new Uint8Array(raw.length); for (var i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i); return out;
  }
  function b64(buf) { return btoa(String.fromCharCode.apply(null, new Uint8Array(buf))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, ""); }
  function send(url, sub) {
    var f = new FormData(); f.append("_csrf", box.getAttribute("data-csrf")); f.append("endpoint", sub.endpoint);
    if (sub.getKey) { f.append("p256dh", b64(sub.getKey("p256dh"))); f.append("auth", b64(sub.getKey("auth"))); }
    return fetch(url, { method: "POST", body: f, credentials: "same-origin" });
  }
  function show(sub) { on.hidden = !!sub; off.hidden = !sub; if (sub) state.textContent = "Avisos activados en este dispositivo."; }
  navigator.serviceWorker.register("/sw.js", { scope: "/" }).then(function (reg) {
    reg.pushManager.getSubscription().then(show);
    on.addEventListener("click", function () {
      Notification.requestPermission().then(function (perm) {
        if (perm !== "granted") { state.textContent = "Has bloqueado los avisos. Actívalos en los ajustes del navegador."; return; }
        reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key(box.getAttribute("data-key")) })
          .then(function (sub) { return send(box.getAttribute("data-sub"), sub).then(function () { show(sub); }); })
          .catch(function () { state.textContent = "No se pudieron activar. Inténtalo otra vez."; });
      });
    });
    off.addEventListener("click", function () {
      reg.pushManager.getSubscription().then(function (sub) {
        if (!sub) return show(null);
        send(box.getAttribute("data-unsub"), sub).then(function () { return sub.unsubscribe(); }).then(function () { show(null); state.textContent = "Avisos desactivados en este dispositivo."; });
      });
    });
  });
}

// =============================================================== formación: subir vídeo a Bunny (directo desde el navegador)
function subidaVideo() {
  document.querySelectorAll("[data-bunny-upload]").forEach(function (box) { if (!box.dataset.ready) { box.dataset.ready = "1"; subidaUna(box); } });
}
function subidaUna(box) {
  var file = box.querySelector("[data-file]"), start = box.querySelector("[data-start]"), bar = box.querySelector("[data-bar]"),
      fill = bar.querySelector("span"), msg = box.querySelector("[data-msg]");
  var CHUNK = 16 * 1024 * 1024, MAX = parseInt(box.getAttribute("data-max") || "0", 10);
  function b64(s) { return btoa(unescape(encodeURIComponent(s))); }
  function fail(t) { msg.textContent = t; start.disabled = false; box.classList.remove("busy"); box.classList.add("err"); }
  var drop = box.querySelector("[data-drop]"), picked = box.querySelector("[data-picked]"), prev = box.querySelector("[data-preview]");
  function mb(n) { return (n / 1048576).toFixed(n < 10485760 ? 1 : 0).replace(".", ",") + " MB"; }
  function elegido() {
    var f = file.files && file.files[0];
    if (!f) return;
    if (!drop) { msg.textContent = f.name; return; }
    drop.hidden = true; picked.hidden = false; msg.textContent = ""; box.classList.remove("done", "err");
    box.querySelector("[data-fname]").textContent = f.name;
    box.querySelector("[data-fsize]").textContent = mb(f.size) + (MAX && f.size > MAX ? " · demasiado grande" : "");
    if (prev) { try { if (prev.src) URL.revokeObjectURL(prev.src); prev.src = URL.createObjectURL(f) + "#t=0.5"; } catch (e) {} }
  }
  file.addEventListener("change", elegido);
  if (drop) {
    ["dragenter", "dragover"].forEach(function (ev) { drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.add("over"); }); });
    ["dragleave", "drop"].forEach(function (ev) { drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.remove("over"); }); });
    drop.addEventListener("drop", function (e) {
      var f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (!f) return;
      try { var dt = new DataTransfer(); dt.items.add(f); file.files = dt.files; } catch (x) { return; }
      elegido();
    });
    var guide = drop.querySelector(".vv-guide");
    if (guide) guide.addEventListener("click", function (e) { e.preventDefault(); e.stopPropagation(); var d = document.getElementById("stats-guide"); if (d && d.showModal) d.showModal(); });
    var ch = box.querySelector("[data-change]");
    if (ch) ch.addEventListener("click", function () { if (!start.disabled) file.click(); });
  }
  start.addEventListener("click", function () {
    var f = file.files && file.files[0];
    if (!f) { msg.textContent = "Elige primero el vídeo."; return; }
    if (MAX && f.size > MAX) { msg.textContent = "El vídeo pesa demasiado (máximo " + Math.round(MAX / 1048576) + " MB)."; return; }
    if (f.type && f.type.indexOf("video/") !== 0) { msg.textContent = "Tiene que ser un vídeo."; return; }
    start.disabled = true; box.classList.add("busy"); box.classList.remove("err"); msg.textContent = "Preparando la subida…";
    var fd = new FormData(); fd.append("_csrf", box.getAttribute("data-csrf"));
    fetch(box.getAttribute("data-prepare"), { method: "POST", body: fd, credentials: "same-origin", headers: { "X-Requested-With": "fetch" } })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d.ok) return fail(d.error || "No se pudo preparar la subida.");
        var h = { "Tus-Resumable": "1.0.0", "AuthorizationSignature": d.signature, "AuthorizationExpire": String(d.expire),
                  "VideoId": d.guid, "LibraryId": String(d.library) };
        var x = new XMLHttpRequest();
        x.open("POST", d.endpoint);
        Object.keys(h).forEach(function (k) { x.setRequestHeader(k, h[k]); });
        x.setRequestHeader("Upload-Length", String(f.size));
        x.setRequestHeader("Upload-Metadata", "filetype " + b64(f.type || "video/mp4") + ",title " + b64(box.getAttribute("data-title") || f.name));
        x.onload = function () {
          var loc = x.getResponseHeader("Location");
          if (x.status !== 201 || !loc) return fail("No se aceptó la subida (" + x.status + "). Vuelve a intentarlo.");
          loc = new URL(loc, d.endpoint).href;
          bar.hidden = false;
          (function send(offset) {
            if (offset >= f.size) { fill.style.width = "100%"; return terminado(); }
            var p = new XMLHttpRequest();
            p.open("PATCH", loc);
            Object.keys(h).forEach(function (k) { p.setRequestHeader(k, h[k]); });
            p.setRequestHeader("Upload-Offset", String(offset));
            p.setRequestHeader("Content-Type", "application/offset+octet-stream");
            p.upload.onprogress = function (e) { var pc = Math.round((offset + e.loaded) / f.size * 100); fill.style.width = pc + "%"; msg.textContent = "Subiendo… " + pc + " %"; };
            p.onload = function () {
              if (p.status !== 204) return fail("Se cortó la subida (" + p.status + "). Vuelve a intentarlo.");
              send(parseInt(p.getResponseHeader("Upload-Offset") || String(offset + CHUNK), 10));
            };
            p.onerror = function () { fail("Sin conexión durante la subida. Vuelve a intentarlo."); };
            p.send(f.slice(offset, offset + CHUNK));
          })(0);
        };
        x.onerror = function () { fail("No se pudo conectar con el servidor de vídeos."); };
        x.send(null);
      })
      .catch(function () { fail("No se pudo preparar la subida."); });
  });
  function terminado() {
    var done = box.getAttribute("data-done");
    if (!done) { msg.textContent = "¡Subido! Bunny lo está procesando (unos minutos)."; return estado(); }
    msg.textContent = "¡Subido! Guardando…";
    var fd = new FormData(); fd.append("_csrf", box.getAttribute("data-csrf"));
    fetch(done, { method: "POST", body: fd, credentials: "same-origin", headers: { "X-Requested-With": "fetch" } })
      .then(function (r) { if (!r.ok) throw 0; box.classList.remove("busy"); box.classList.add("done"); msg.textContent = "✓ Enviado. Ya está en revisión."; setTimeout(function () { location.reload(); }, 1200); })
      .catch(function () { fail("Se subió, pero no se pudo guardar. Recarga la página."); });
  }
  function estado() {
    var t = setInterval(function () {
      fetch(box.getAttribute("data-status"), { credentials: "same-origin" }).then(function (r) { return r.json(); }).then(function (d) {
        if (d.status === "listo") { clearInterval(t); msg.textContent = "Vídeo listo."; location.reload(); }
        else if (d.status === "error") { clearInterval(t); msg.textContent = "Bunny no pudo procesar el vídeo. Prueba con otro archivo."; }
      }).catch(function () {});
    }, 8000);
    limpiar.push(function () { clearInterval(t); });
  }
}

// =============================================================== formación: progreso de la lección (sigue donde lo dejaste)
// Lección vista: check en la lista, progreso arriba y «✓ Vista», sin recargar
function vista(id) {
  var item = id && document.querySelector('[data-lesson-item="' + id + '"]');
  var chip = document.querySelector("[data-watch-chip]");
  if (chip) { chip.textContent = "✓ Vista"; chip.classList.add("ok"); }
  if (!item || item.classList.contains("done")) return;
  item.classList.add("done");
  var part = item.querySelector(".lx-part"); if (part) part.remove();
  var pr = document.querySelector("[data-pr]");
  if (pr) {
    var total = parseInt(pr.getAttribute("data-total"), 10) || 1, n = (parseInt(pr.getAttribute("data-done"), 10) || 0) + 1;
    pr.setAttribute("data-done", n);
    var pct = Math.round(n / total * 100);
    pr.querySelector("[data-pr-n]").textContent = n;
    pr.querySelector("[data-pr-bar]").style.width = pct + "%";
    pr.querySelector("[data-pr-pct]").textContent = pct + "%";
  }
}

// Formación: ocultar la lista de lecciones (se recuerda en este navegador) y quitar miniaturas que no cargan
document.addEventListener("click", function (e) {
  var t = e.target.closest && e.target.closest("[data-lx-toggle]");
  if (!t) return;
  var lx = document.querySelector("[data-lx]"); if (!lx) return;
  lx.classList.toggle("closed");
  try { localStorage.setItem("lx-closed", lx.classList.contains("closed") ? "1" : ""); } catch (err) {}
});
document.addEventListener("error", function (e) {
  if (e.target && e.target.hasAttribute && e.target.hasAttribute("data-hide-broken")) e.target.remove();
}, true);
// Formación: si vienes del inicio, el curso se marca un momento con un recuadro (no se abre)
function destacarCurso() {
  var card = document.querySelector("[data-highlight]");
  if (!card) return;
  card.removeAttribute("data-highlight");
  var r = card.getBoundingClientRect();
  if (r.top < 0 || r.bottom > window.innerHeight) card.scrollIntoView({ block: "center", behavior: "smooth" });
  setTimeout(function () { card.classList.add("ring"); }, 150);
  setTimeout(function () { card.classList.remove("ring"); }, 1700);
  try { history.replaceState(history.state, "", location.pathname); } catch (err) {}
}
function listaLecciones() {
  var lx = document.querySelector("[data-lx]"); if (!lx) return;
  try { if (localStorage.getItem("lx-closed") === "1") lx.classList.add("closed"); } catch (err) {}
  var on = lx.querySelector(".lx-item.on"), side = lx.querySelector(".lx-side");
  if (on && side && window.innerWidth > 900) side.scrollTop = Math.max(on.offsetTop - side.clientHeight / 2, 0);
}

function progresoLeccion() {
  listaLecciones();
  // Lección sin vídeo (texto o archivo): cuenta como vista tras 10 s con la página abierta
  var auto = document.querySelector("[data-auto-done]");
  if (auto && !auto.dataset.ready) {
    auto.dataset.ready = "1";
    var waited = 0, ti = setInterval(function () {
      if (document.visibilityState === "visible") waited++;
      if (waited < 10) return;
      clearInterval(ti);
      var f = new FormData(); f.append("_csrf", auto.getAttribute("data-csrf")); f.append("done", "1");
      fetch(auto.getAttribute("data-auto-done"), { method: "POST", body: f, credentials: "same-origin", headers: { "X-Requested-With": "fetch" } })
        .then(function (r) { return r.json(); }).then(function (d) { if (d.done) vista(auto.getAttribute("data-lesson")); }).catch(function () {});
    }, 1000);
    limpiar.push(function () { clearInterval(ti); });
  }
  var box = document.querySelector("[data-lesson-player]");
  if (!box || box.dataset.ready) return;
  box.dataset.ready = "1";
  // Vídeo: se apuntan solo los tramos que se reproducen de verdad (si se salta hacia delante, ese hueco no cuenta).
  // El servidor suma los tramos y la marca como vista al llegar al 90 % del vídeo.
  var frame = box.querySelector("iframe"), url = box.getAttribute("data-url"), csrf = box.getAttribute("data-csrf");
  var kind = box.getAttribute("data-kind"), id = box.getAttribute("data-lesson");
  var start = parseInt(box.getAttribute("data-start") || "0", 10);
  var tramos = [], cur = null, lastT = null, dur = 0, dirty = false, done = false, ytOk = false, ytState = -1, playing = false;
  function apunta(t, d) {
    if (d) dur = d;
    if (lastT !== null && t >= lastT && t - lastT <= 3) { cur[1] = t; dirty = true; }
    else { cur = [t, t]; tramos.push(cur); }
    lastT = t;
  }
  function envia() {
    if (!dirty || done) return;
    dirty = false;
    var f = new FormData(); f.append("_csrf", csrf);
    f.append("ranges", tramos.filter(function (r) { return r[1] - r[0] >= 1; })
      .map(function (r) { return Math.floor(r[0]) + "-" + Math.ceil(r[1]); }).join(","));
    if (lastT !== null) f.append("seconds", Math.floor(lastT));
    if (dur) f.append("duration", Math.round(dur));
    fetch(url, { method: "POST", body: f, credentials: "same-origin", headers: { "X-Requested-With": "fetch" } })
      .then(function (r) { return r.json(); }).then(function (d) {
        if (d.done) { done = true; vista(id); return; }
        var item = document.querySelector('[data-lesson-item="' + id + '"] .lx-thumb');
        if (item && d.pct) {
          var p = item.querySelector(".lx-part");
          if (!p) { p = document.createElement("span"); p.className = "lx-part"; item.appendChild(p); }
          p.style.width = d.pct + "%";
        }
      }).catch(function () { dirty = true; });
  }
  function manda(obj) { try { frame.contentWindow.postMessage(JSON.stringify(obj), "*"); } catch (err) {} }
  function onMsg(e) {
    if (!frame || e.source !== frame.contentWindow) return;
    var d; try { d = typeof e.data === "string" ? JSON.parse(e.data) : e.data; } catch (err) { return; }
    if (!d) return;
    if (kind === "youtube") {                      // YouTube: manda «infoDelivery» con el segundo actual
      if (d.event === "onReady" || d.event === "initialDelivery") { ytOk = true; if (start > 5) manda({ event: "command", func: "seekTo", args: [start, true] }); }
      var info = d.info || {};
      if (typeof info.duration === "number" && info.duration) dur = info.duration;
      if (typeof info.playerState === "number") {
        ytState = info.playerState; playing = ytState === 1;
        if (ytState === 2 || ytState === 0) { lastT = null; envia(); }
      }
      if (typeof info.currentTime === "number" && ytState === 1) apunta(info.currentTime);
      return;
    }
    if (d.context === "player.js") {               // Bunny
      if (d.event === "ready") {
        ["timeupdate", "play", "pause", "ended"].forEach(function (ev) { manda({ context: "player.js", version: "0.0.11", method: "addEventListener", value: ev, listener: "cp" }); });
        if (start > 5) manda({ context: "player.js", version: "0.0.11", method: "setCurrentTime", value: start, listener: "cp" });
      } else if (d.event === "timeupdate" && d.value) { playing = true; apunta(d.value.seconds || 0, d.value.duration); }
      else if (d.event === "play") playing = true;
      else if (d.event === "pause" || d.event === "ended") { playing = false; lastT = null; envia(); }
      return;
    }
    if (kind === "vimeo") {                        // Vimeo
      if (d.event === "ready") {
        ["timeupdate", "play", "pause", "ended"].forEach(function (ev) { manda({ method: "addEventListener", value: ev }); });
        if (start > 5) manda({ method: "setCurrentTime", value: start });
      } else if (d.event === "timeupdate" && d.data) { playing = true; apunta(d.data.seconds || 0, d.data.duration); }
      else if (d.event === "play") playing = true;
      else if (d.event === "pause" || d.event === "ended") { playing = false; lastT = null; envia(); }
    }
  }
  window.addEventListener("message", onMsg);
  // Barra espaciadora: pausa o sigue el vídeo (salvo escribiendo en un campo)
  function tecla(e) {
    if (e.code !== "Space" && e.key !== " ") return;
    var t = e.target, tag = t && t.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || tag === "BUTTON" || (t && t.isContentEditable)) return;
    e.preventDefault();
    var poner = !playing;
    if (kind === "youtube") manda({ event: "command", func: poner ? "playVideo" : "pauseVideo", args: [] });
    else if (kind === "vimeo") manda({ method: poner ? "play" : "pause" });
    else manda({ context: "player.js", version: "0.0.11", method: poner ? "play" : "pause", listener: "cp" });
    playing = poner;
  }
  document.addEventListener("keydown", tecla);
  var cada = setInterval(envia, 10000);
  var yt = kind === "youtube" ? setInterval(function () {          // YouTube no habla hasta que le decimos que escuchamos
    if (ytOk) { clearInterval(yt); return; }
    manda({ event: "listening", id: "cp", channel: "widget" });
  }, 1000) : null;
  function oculto() { if (document.visibilityState === "hidden") { lastT = null; envia(); } }
  document.addEventListener("visibilitychange", oculto);
  limpiar.push(function () {
    envia();
    window.removeEventListener("message", onMsg);
    document.removeEventListener("visibilitychange", oculto);
    document.removeEventListener("keydown", tecla);
    clearInterval(cada); if (yt) clearInterval(yt);
  });
}

// Botón «Descargar PDF / imprimir» del informe (el navegador deja guardarlo como PDF)
document.addEventListener("click", function (e) {
  if (e.target.closest("[data-print]")) window.print();
});

// Mis clips: desplegar la fila con más detalles (motivo, apelación, verificación)
document.addEventListener("click", function (e) {
  var b = e.target.closest && e.target.closest("[data-row-toggle]");
  if (!b) return;
  var row = document.getElementById(b.getAttribute("data-row-toggle"));
  if (!row) return;
  row.hidden = !row.hidden;
  document.querySelectorAll('[data-row-toggle="' + row.id + '"]').forEach(function (x) { x.setAttribute("aria-expanded", row.hidden ? "false" : "true"); });
  var dd = b.closest("details"); if (dd) dd.removeAttribute("open");
  var f = !row.hidden && b.getAttribute("data-focus");
  if (f) { var el = row.querySelector(f); if (el) el.focus(); }
});

// «Otro país»: aparece un campo para escribirlo
document.addEventListener("change", function (e) {
  var sel = e.target.closest && e.target.closest("[data-other-toggle]");
  if (!sel) return;
  var box = document.querySelector(sel.getAttribute("data-other-toggle")), on = sel.value === "OT";
  if (!box) return;
  box.hidden = !on;
  var inp = box.querySelector("input"); if (inp) { inp.required = on; if (on) inp.focus(); }
});

// Selectores de archivo: enseñar el nombre del archivo elegido y, en la foto de perfil, la vista previa
document.addEventListener("change", function (e) {
  var inp = e.target;
  if (!inp || inp.type !== "file") return;
  var pick = inp.closest(".file-pick"), f = inp.files && inp.files[0];
  var name = pick && pick.querySelector("[data-file-name]");
  if (name && f) name.textContent = f.name;
  if (inp.hasAttribute("data-photo-input") && f) {
    var prev = document.querySelector("[data-photo-prev]");
    if (prev) { prev.innerHTML = ""; var img = document.createElement("img"); img.alt = ""; img.src = URL.createObjectURL(f); prev.appendChild(img); }
  }
});

// Portada de la campaña: vista previa al elegir la imagen
document.addEventListener("change", function (e) {
  var inp = e.target.closest("[data-cover-input]");
  if (!inp || !inp.files || !inp.files[0]) return;
  var prev = document.querySelector("[data-cover-prev]");
  if (prev) { prev.style.backgroundImage = "url('" + URL.createObjectURL(inp.files[0]) + "')"; prev.textContent = ""; }
  var pv = document.querySelector("[data-pv=cover]");
  if (pv) pv.style.backgroundImage = "url('" + URL.createObjectURL(inp.files[0]) + "')";
});

// =============================================================== desplegables con el diseño de la web
// Cada <select> se queda (escondido) para que el formulario funcione igual; encima va un botón y una lista propia.
// En el móvil se usa el selector del teléfono, que es más cómodo con el dedo.
function desplegables() {
  if (window.matchMedia && window.matchMedia("(pointer: coarse)").matches) return;
  document.querySelectorAll("select:not([multiple]):not([data-native])").forEach(function (sel) {
    if (sel.closest(".dd")) return;
    var box = document.createElement("div");
    box.className = "dd";
    var btn = document.createElement("button");
    btn.type = "button"; btn.className = "dd-btn";
    if (sel.dataset.ico) btn.dataset.ico = sel.dataset.ico;   // icono delante (filtros)
    btn.setAttribute("aria-haspopup", "listbox"); btn.setAttribute("aria-expanded", "false");
    var lab = sel.getAttribute("aria-label") || (sel.id && document.querySelector("label[for='" + sel.id + "']") || {}).textContent || "";
    if (lab) btn.setAttribute("aria-label", lab.trim());
    var list = document.createElement("ul");
    list.className = "dd-list"; list.setAttribute("role", "listbox"); list.hidden = true;
    sel.parentNode.insertBefore(box, sel);
    box.appendChild(sel); box.appendChild(btn); box.appendChild(list);
    sel.classList.add("dd-native"); sel.tabIndex = -1;
    function paint() {
      var o = sel.options[sel.selectedIndex];
      var t = btn.firstChild || btn.appendChild(document.createElement("span"));   // sin rehacer el botón: el clic sigue «dentro»
      t.textContent = o ? o.text : "";
      btn.classList.toggle("dd-empty", !o || !o.value);          // «Elige tu país»: se ve como texto de ayuda
      list.innerHTML = "";
      Array.prototype.forEach.call(sel.options, function (op, i) {
        if (op.hidden) return;                                     // el texto de ayuda no es una opción
        var li = document.createElement("li");
        li.setAttribute("role", "option"); li.textContent = op.text; li.dataset.i = i;
        if (i === sel.selectedIndex) { li.className = "on"; li.setAttribute("aria-selected", "true"); }
        if (op.disabled) li.setAttribute("aria-disabled", "true");
        list.appendChild(li);
      });
    }
    var hi = -1;
    function mark(i) {
      var items = list.children; if (!items.length) return;
      hi = Math.max(0, Math.min(items.length - 1, i));
      Array.prototype.forEach.call(items, function (li, j) { li.classList.toggle("hi", j === hi); });
      items[hi].scrollIntoView({ block: "nearest" });
    }
    function open() {
      document.querySelectorAll(".dd.open").forEach(function (d) { if (d !== box) d.ddClose(); });
      paint(); list.hidden = false; box.classList.add("open"); btn.setAttribute("aria-expanded", "true");
      var r = box.getBoundingClientRect();
      box.classList.toggle("up", window.innerHeight - r.bottom < Math.min(280, list.scrollHeight + 16) && r.top > 300);
      var cur = list.querySelector("li.on");
      mark(cur ? Array.prototype.indexOf.call(list.children, cur) : 0);
    }
    function close() { list.hidden = true; box.classList.remove("open"); btn.setAttribute("aria-expanded", "false"); }
    box.ddClose = close;
    function choose(i) {
      if (sel.options[i] && !sel.options[i].disabled && i !== sel.selectedIndex) {
        sel.selectedIndex = i;
        sel.dispatchEvent(new Event("change", { bubbles: true }));
      }
      paint(); close(); btn.focus();
    }
    btn.addEventListener("click", function () { list.hidden ? open() : close(); });
    list.addEventListener("mousedown", function (e) { e.preventDefault(); });
    list.addEventListener("click", function (e) { var li = e.target.closest("li"); if (li) choose(parseInt(li.dataset.i, 10)); });
    btn.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); if (list.hidden) open(); else mark(hi + (e.key === "ArrowDown" ? 1 : -1)); }
      else if (e.key === "Enter" || e.key === " ") { if (!list.hidden && hi >= 0) { e.preventDefault(); choose(parseInt(list.children[hi].dataset.i, 10)); } }
      else if (e.key === "Escape" || e.key === "Tab") close();
    });
    btn.addEventListener("blur", function () { setTimeout(function () { if (!box.contains(document.activeElement)) close(); }, 120); });
    sel.addEventListener("change", paint);
    if (sel.disabled) btn.disabled = true;
    paint();
  });
}
document.addEventListener("click", function (e) {
  document.querySelectorAll(".dd.open").forEach(function (d) { if (!d.contains(e.target)) d.ddClose(); });
});
document.addEventListener("turbo:before-cache", function () {
  document.querySelectorAll(".dd").forEach(function (d) {
    var sel = d.querySelector("select");
    if (sel) { sel.classList.remove("dd-native"); sel.removeAttribute("tabindex"); d.parentNode.insertBefore(sel, d); }
    d.remove();
  });
});

// Destacado de inicio: enseña solo los campos de la opción elegida
function destacado() {
  var radios = document.querySelectorAll("[data-feat-mode]");
  if (!radios.length || radios[0].dataset.ready) return;
  radios[0].dataset.ready = "1";
  function show() {
    var m = (document.querySelector("[data-feat-mode]:checked") || {}).value;
    document.querySelectorAll("[data-feat]").forEach(function (b) { b.hidden = b.getAttribute("data-feat") !== m; });
  }
  radios.forEach(function (r) { r.addEventListener("change", show); });
  show();
}

// Canales de la campaña en el móvil: la barra de canales se desliza hasta el canal abierto
function canalVisible() {
  var on = document.querySelector(".hub-side .hub-ch.on");
  var bar = on && on.closest(".hub-side");
  if (!bar || bar.scrollWidth <= bar.clientWidth) return;
  bar.scrollLeft = on.offsetLeft - bar.clientWidth / 2 + on.offsetWidth / 2;
}


// =============================================================== buscador instantáneo en las listas (filtra mientras escribes)
function quitarAcentos(t) { return (t || "").toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, ""); }
function buscadoresVivos() {
  document.querySelectorAll("[data-live-search]").forEach(function (inp) {
    if (inp.dataset.ready) return;
    inp.dataset.ready = "1";
    var box = document.querySelector(inp.getAttribute("data-live-search"));
    if (!box) return;
    var empty = box.parentNode.querySelector(".live-empty") || box.querySelector(".live-empty");
    if (!empty) {
      empty = document.createElement(box.tagName === "TBODY" ? "tr" : "p");
      empty.className = "live-empty small muted"; empty.hidden = true;
      if (box.tagName === "TBODY") { var td = document.createElement("td"); td.colSpan = 12; empty.appendChild(td); }
      box.appendChild(empty);
    }
    function filtra() {
      var q = quitarAcentos(inp.value.trim()), shown = 0;
      box.querySelectorAll("[data-search]").forEach(function (el) {
        var ok = !q || quitarAcentos(el.getAttribute("data-search")).indexOf(q) !== -1;
        el.hidden = !ok; if (ok) shown++;
      });
      var msg = q && !shown ? "Nadie coincide con «" + inp.value.trim() + "»." : "";
      (empty.tagName === "TR" ? empty.firstChild : empty).textContent = msg;
      empty.hidden = !msg;
    }
    inp.addEventListener("input", filtra);
    inp.addEventListener("keydown", function (e) { if (e.key === "Enter" && !inp.name) { e.preventDefault(); } });
    if (inp.value) filtra();
  });
}
document.addEventListener("turbo:load", buscadoresVivos);
document.addEventListener("DOMContentLoaded", buscadoresVivos);

// Editor de recursos de una campaña: elegir icono, añadir y quitar filas
document.addEventListener("click", function (e) {
  var set = e.target.closest("[data-res-set]");
  if (set) {
    var row = set.closest(".res-erow"), k = set.getAttribute("data-res-set");
    row.querySelector('input[name="mat_icon"]').value = k;
    var ico = row.querySelector("[data-res-ico]");
    ico.className = "res-ico ri-" + k; ico.innerHTML = set.innerHTML;
    set.closest("details").removeAttribute("open");
    return;
  }
  var add = e.target.closest("[data-res-add]");
  if (add) {
    var box = add.closest("[data-res-edit]"), tpl = box.querySelector("[data-res-tpl]");
    if (box.querySelectorAll(".res-erow:not([hidden])").length >= 12) return;
    if (tpl.hidden) { tpl.hidden = false; tpl.querySelector('input[name="mat_name"]').focus(); return; }
    var copy = tpl.cloneNode(true);
    copy.removeAttribute("data-res-tpl");
    copy.querySelectorAll("input:not([type=hidden])").forEach(function (i) { i.value = ""; });
    box.insertBefore(copy, add);
    copy.querySelector('input[name="mat_name"]').focus();
    return;
  }
  var del = e.target.closest("[data-res-del]");
  if (del) {
    var r = del.closest(".res-erow");
    r.querySelectorAll("input:not([type=hidden])").forEach(function (i) { i.value = ""; });
    if (r.hasAttribute("data-res-tpl")) r.hidden = true; else r.remove();
  }
});

// =============================================================== Ajustes: índice que sigue al scroll y avisos que se guardan solos
function ajustes() {
  var box = document.querySelector("[data-set]");
  if (!box || box.dataset.ready) return;
  box.dataset.ready = "1";
  var links = box.querySelectorAll(".set-nav a");
  if ("IntersectionObserver" in window) {
    var io = new IntersectionObserver(function (ents) {
      ents.forEach(function (en) {
        if (!en.isIntersecting) return;
        links.forEach(function (l) { l.classList.toggle("on", l.getAttribute("href") === "#" + en.target.id); });
      });
    }, { rootMargin: "-30% 0px -60% 0px" });
    box.querySelectorAll(".set-sec").forEach(function (s) { io.observe(s); });
    limpiar.push(tuyo(box, function () { io.disconnect(); }));
  }
  var f = box.querySelector("[data-autosave]");
  if (f) {
    var save = f.querySelector(".set-save"); if (save) save.hidden = true;
    f.addEventListener("change", function () {
      fetch(f.action, { method: "POST", body: new FormData(f), credentials: "same-origin", headers: { "X-Requested-With": "fetch", "Accept": "application/json" } })
        .then(function (r) { if (!r.ok) throw 0; toast("Avisos guardados", "ok"); })
        .catch(function () { if (save) save.hidden = false; toast("No se pudo guardar. Pulsa «Guardar avisos».", "error"); });
    });
  }
}

// Preguntas sugeridas del soporte: rellenan el mensaje
document.addEventListener("click", function (e) {
  var b = e.target.closest("[data-sugg]");
  if (!b) return;
  var c = b.closest("[data-chat]"), ta = c && c.querySelector("textarea");
  if (!ta) return;
  ta.value = b.getAttribute("data-sugg");
  ta.focus();
  ta.dispatchEvent(new Event("input", { bubbles: true }));
});

// Pestañas dentro de una tarjeta (ranking del inicio): cambian sin recargar
document.addEventListener("click", function (e) {
  var a = e.target.closest("[data-tabs] [data-tab]");
  if (!a) return;
  e.preventDefault();
  var box = a.closest("[data-tabs]");
  box.querySelectorAll("[data-tab]").forEach(function (x) {
    var on = x === a;
    x.classList.toggle("on", on);
    var p = document.getElementById(x.getAttribute("data-tab"));
    if (p) p.hidden = !on;
  });
  if (box.hasAttribute("data-hash-tabs") && history.replaceState) history.replaceState(history.state, "", "#" + a.getAttribute("data-tab"));
});
// Pestañas que recuerdan dónde estabas (Panel técnico): al volver de un botón se abre la misma
function pestanasHash() {
  var box = document.querySelector("[data-hash-tabs]"), h = (location.hash || "").slice(1);
  if (!box || !h) return;
  var a = box.querySelector('[data-tab="' + h.replace(/[^a-z0-9_-]/gi, "") + '"]');
  if (!a || a.classList.contains("on")) return;
  box.querySelectorAll("[data-tab]").forEach(function (x) {
    var on = x === a;
    x.classList.toggle("on", on);
    var p = document.getElementById(x.getAttribute("data-tab"));
    if (p) p.hidden = !on;
  });
}

window.addEventListener("hashchange", pestanasHash);

// Filas de tabla que llevan a una ficha: se puede pulsar en cualquier parte de la fila
document.addEventListener("click", function (e) {
  var tr = e.target.closest("tr[data-row-link]");
  if (!tr || e.target.closest("a, button, input, select, label, form")) return;
  if (window.getSelection && String(window.getSelection())) return;
  var url = tr.getAttribute("data-row-link");
  if (e.metaKey || e.ctrlKey) { window.open(url, "_blank"); return; }
  if (window.Turbo) window.Turbo.visit(url); else location.href = url;
});

// =============================================================== Avisos del panel: elegir clippers con filtros y vista previa
function avisosPanel() {
  var f = document.querySelector("[data-notify]");
  if (!f || f.dataset.ready) return;
  f.dataset.ready = "1";
  var rows = Array.prototype.slice.call(f.querySelectorAll(".nt-row")), filt = "todos";
  var q = f.querySelector("[data-nt-q]"), camp = f.querySelector("[data-nt-camp]"), all = f.querySelector("[data-nt-all]");
  function visibles() { return rows.filter(function (r) { return !r.hidden; }); }
  function cuenta() {
    var n = f.querySelectorAll('input[name="uids"]:checked').length;
    f.querySelector("[data-nt-count]").textContent = n;
    f.querySelector("[data-nt-count2]").textContent = n;
    f.querySelector("[data-nt-send]").disabled = !n;
    var v = visibles();
    all.checked = v.length > 0 && v.every(function (r) { return r.querySelector("input").checked; });
    var first = f.querySelector('input[name="uids"]:checked');
    var name = first ? first.closest(".nt-row").getAttribute("data-first") : "";
    f.querySelector("[data-nt-pname]").textContent = name || "el clipper";
    previa(name);
  }
  function aplica() {
    var t = quitarAcentos(q.value.trim()), c = camp.value, shown = 0;
    rows.forEach(function (r) {
      var ok = (filt === "todos" || (" " + r.getAttribute("data-tags") + " ").indexOf(" " + filt + " ") >= 0)
        && (!c || r.getAttribute("data-camps").indexOf("," + c + ",") >= 0)
        && (!t || quitarAcentos(r.getAttribute("data-name")).indexOf(t) >= 0);
      r.hidden = !ok; if (ok) shown++;
    });
    f.querySelector("[data-nt-visible]").textContent = shown;
    f.querySelector("[data-nt-none]").hidden = !!shown;
    cuenta();
  }
  function previa(name) {
    var nom = name || "Ana";
    var b = f.querySelector("[data-nt-body]").value.trim().replace(/\{nombre\}/gi, nom) || "Escribe el mensaje…";
    var t = f.querySelector("[data-nt-title]").value.trim().replace(/\{nombre\}/gi, nom) || "Mensaje del equipo";
    f.querySelector("[data-nt-pbody]").textContent = b;
    f.querySelector("[data-nt-pbody2]").textContent = b;
    f.querySelector("[data-nt-ptitle]").textContent = t;
  }
  f.addEventListener("click", function (e) {
    var a = e.target.closest("[data-nt-filter]");
    if (!a) return;
    e.preventDefault();
    filt = a.getAttribute("data-nt-filter");
    f.querySelectorAll("[data-nt-filter]").forEach(function (x) { x.classList.toggle("on", x === a); });
    aplica();
  });
  q.addEventListener("input", aplica);
  camp.addEventListener("change", aplica);
  all.addEventListener("change", function () { visibles().forEach(function (r) { r.querySelector("input").checked = all.checked; }); cuenta(); });
  f.addEventListener("change", function (e) { if (e.target.name === "uids") cuenta(); });
  f.querySelector("[data-nt-body]").addEventListener("input", cuenta);
  f.querySelector("[data-nt-title]").addEventListener("input", cuenta);
  aplica();
}

// =============================================================== carrusel de campañas destacadas (pasa solo cada 6 s)
// Las diapositivas están una encima de otra: la nueva entra con un fundido, la portada se desliza un poco desde el lado
// por el que llega y el texto sube en cascada. El punto activo se va llenando mientras corre el tiempo.
// Con el dedo se pasa deslizando. Si el sistema pide menos movimiento, cambia sin animación.
var CAR_MS = 6000;
function carruseles() {
  document.querySelectorAll("[data-carousel]").forEach(function (box) {
    if (box.dataset.ready) return;
    box.dataset.ready = "1";
    var track = box.querySelector("[data-car-track]"), dots = box.querySelectorAll("[data-car-dot]");
    var slides = Array.prototype.slice.call(track.children), n = slides.length, cur = 0, timer = null, busy = false;
    box.classList.add("fcar-fx");
    slides.forEach(function (sl, k) { sl.classList.toggle("is-on", k === 0); sl.setAttribute("aria-hidden", k === 0 ? "false" : "true"); if (k) sl.inert = true; });
    if (n < 2) return;
    var quieto = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    function puntos() {
      dots.forEach(function (d, k) {
        d.classList.remove("on", "run");
        if (k === cur) { void d.offsetWidth; d.classList.add("on"); if (timer) d.classList.add("run"); }
      });
    }
    function ir(i, dir) {
      i = (i + n) % n;
      if (i === cur || busy) return;
      dir = dir || (i > cur ? 1 : -1);
      var from = slides[cur], to = slides[i];
      busy = true;
      box.style.setProperty("--dir", dir);
      from.classList.remove("is-on"); from.classList.add("is-out"); from.setAttribute("aria-hidden", "true"); from.inert = true;
      to.classList.remove("is-out"); to.classList.add("is-on"); to.setAttribute("aria-hidden", "false"); to.inert = false;
      cur = i;
      puntos();
      setTimeout(function () { from.classList.remove("is-out"); busy = false; }, quieto ? 0 : 700);
    }
    function play() {
      clearInterval(timer); timer = null;
      if (box.hasAttribute("data-autoplay") && !quieto) timer = setInterval(function () { if (!document.hidden) ir(cur + 1, 1); }, CAR_MS);
      puntos();
    }
    function pausa() { clearInterval(timer); timer = null; dots.forEach(function (d) { d.classList.remove("run"); }); }
    box.addEventListener("click", function (e) {
      var d = e.target.closest("[data-car-dot]"), p = e.target.closest("[data-car-prev]"), nx = e.target.closest("[data-car-next]");
      if (d) ir(+d.getAttribute("data-car-dot")); else if (p) ir(cur - 1, -1); else if (nx) ir(cur + 1, 1); else return;
      e.preventDefault(); play();
    });
    // Deslizar con el dedo (o arrastrar con el ratón en horizontal)
    var x0 = null, y0 = 0, moved = false;
    track.addEventListener("pointerdown", function (e) { if (e.pointerType === "mouse" && e.button) return; x0 = e.clientX; y0 = e.clientY; moved = false; }, { passive: true });
    track.addEventListener("pointermove", function (e) { if (x0 !== null && Math.abs(e.clientX - x0) > 8) moved = true; }, { passive: true });
    track.addEventListener("pointerup", function (e) {
      if (x0 === null) return;
      var dx = e.clientX - x0, dy = e.clientY - y0; x0 = null;
      if (Math.abs(dx) > 45 && Math.abs(dx) > Math.abs(dy) * 1.2) { ir(cur + (dx < 0 ? 1 : -1), dx < 0 ? 1 : -1); play(); }
    });
    track.addEventListener("pointercancel", function () { x0 = null; });
    track.addEventListener("dragstart", function (e) { e.preventDefault(); });   // que el enlace no se arrastre al deslizar
    track.addEventListener("click", function (e) { if (moved) { e.preventDefault(); e.stopPropagation(); moved = false; } }, true);
    box.addEventListener("keydown", function (e) {
      if (e.key === "ArrowRight") { ir(cur + 1, 1); play(); } else if (e.key === "ArrowLeft") { ir(cur - 1, -1); play(); }
    });
    box.addEventListener("mouseenter", pausa);
    box.addEventListener("mouseleave", play);
    box.addEventListener("focusin", pausa);
    play();
    limpiar.push(tuyo(box, function () { clearInterval(timer); }));
  });
}

// =============================================================== Chat de soporte en una ventanita (como los asistentes de otras webs)
// El botón flotante abre un panel pequeño con la conversación; arriba hay un botón discreto para verla a pantalla completa.
// El panel se queda abierto al cambiar de página (data-turbo-permanent).
(function () {
  function pop() { return document.getElementById("chat-pop"); }
  function fab() { return document.getElementById("chat-fab"); }
  function cargar(p) {
    var body = p.querySelector("[data-cp-body]");
    if (!body || body.dataset.loaded === "1" || body.dataset.loaded === "…") return;
    body.dataset.loaded = "…";
    fetch(body.getAttribute("data-src"), { headers: { "X-Requested-With": "fetch" }, credentials: "same-origin" })
      .then(function (r) { if (!r.ok) throw r; return r.text(); })
      .then(function (html) {
        body.innerHTML = html; body.dataset.loaded = "1";
        var chat = body.querySelector("[data-chat]");
        iniciarChat(chat);
        if (chat && chat._scroll) chat._scroll();
        var ta = body.querySelector("textarea"); if (ta && window.matchMedia("(min-width:641px)").matches) ta.focus();
      })
      .catch(function () { body.dataset.loaded = ""; body.innerHTML = '<p class="cp-err">No se ha podido abrir el chat. <a class="link" href="' + (pop().getAttribute("data-full") || "/soporte") + '">Abrirlo aquí</a></p>'; });
  }
  function abrir(on) {
    var p = pop(), f = fab();
    if (!p) return false;
    clearTimeout(p._cierre);
    if (on) { p.classList.remove("closing"); p.hidden = false; }
    else if (!p.hidden) {                         // se cierra con una animación corta y luego se esconde
      var quieto = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      if (quieto) p.hidden = true;
      else { p.classList.add("closing"); p._cierre = setTimeout(function () { p.hidden = true; p.classList.remove("closing"); }, 190); }
    }
    if (f) { f.classList.toggle("open", on); f.setAttribute("aria-expanded", on ? "true" : "false"); }
    if (on) {
      cargar(p);
      var chat = p.querySelector("[data-chat]");
      if (chat && chat._poll) { chat._poll(); if (chat._scroll) chat._scroll(); }
      var n = document.getElementById("fab-count"); if (n) n.hidden = true;
      if (f) f.classList.remove("has-unread");
    }
    try { sessionStorage.setItem("chatPop", on ? "1" : ""); } catch (e) {}
    return true;
  }
  document.addEventListener("click", function (e) {
    var f = e.target.closest("#chat-fab");
    if (f && !e.metaKey && !e.ctrlKey && !e.shiftKey && pop()) { e.preventDefault(); abrir(pop().hidden); return; }
    if (e.target.closest("[data-cp-close]")) { e.preventDefault(); abrir(false); if (fab()) fab().focus(); }
    if (e.target.closest("[data-cp-full]")) abrir(false);    // a pantalla completa: la ventanita se cierra (y no vuelve al ir atrás)
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && pop() && !pop().hidden) { abrir(false); if (fab()) fab().focus(); }
  });
  // Al recargar la página, si estaba abierto, se vuelve a abrir
  document.addEventListener("turbo:load", function () {
    var p = pop();
    if (!p) return;
    if (fab()) { fab().classList.toggle("open", !p.hidden); fab().setAttribute("aria-expanded", p.hidden ? "false" : "true"); }
    var was = ""; try { was = sessionStorage.getItem("chatPop") || ""; } catch (e) {}
    if (was && p.hidden) abrir(true);
  });
})();
// Al guardar la página para «atrás», el carrusel se deja listo para arrancar otra vez
document.addEventListener("turbo:before-cache", function () {
  document.querySelectorAll("[data-carousel][data-ready]").forEach(function (b) { b.removeAttribute("data-ready"); });
});

// =============================================================== Listas cómodas en vez de «una por línea»
// Cuentas: elegir la red y escribir el @ (si pegas un enlace de perfil, se queda solo la cuenta y se elige la red).
// Preguntas: una casilla por pregunta. Por debajo se sigue mandando el mismo texto de siempre, línea a línea.
var REDES = [["tiktok", "TikTok"], ["instagram", "Instagram"], ["youtube", "YouTube"]];
function listas() {
  document.querySelectorAll("[data-rows]").forEach(function (box) {
    if (box.dataset.ready) return;
    box.dataset.ready = "1";
    var ta = box.querySelector("textarea"), acc = box.getAttribute("data-rows") === "acc";
    var list = document.createElement("div"); list.className = "ri-list";
    var add = document.createElement("button"); add.type = "button"; add.className = "ri-add";
    add.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>' + (box.getAttribute("data-add") || "Añadir otra cuenta");
    ta.hidden = true;
    box.appendChild(list); box.appendChild(add);
    function leerCuenta(v, sel) {
      v = (v || "").trim();
      var low = v.toLowerCase();
      if (/tiktok\.com/.test(low)) sel.value = "tiktok";
      else if (/instagram\.com/.test(low)) sel.value = "instagram";
      else if (/youtu\.?be/.test(low)) sel.value = "youtube";
      if (v.indexOf("/") > -1) v = v.replace(/[?#].*$/, "").replace(/\/+$/, "").split("/").pop();
      return v.replace(/^@+/, "").replace(/\s+/g, "");
    }
    function guardar() {
      var out = [];
      list.querySelectorAll(".ri-row").forEach(function (r) {
        var inp = r.querySelector("input"), v = inp.value.trim();
        if (!v) return;
        out.push(acc ? r.querySelector("select").value + " @" + v.replace(/^@+/, "") : v);
      });
      ta.value = out.join("\n");
    }
    function fila(red, valor, foco) {
      var r = document.createElement("div"); r.className = "ri-row";
      var inp = document.createElement("input"); inp.type = "text"; inp.value = valor || "";
      if (acc) {
        var sel = document.createElement("select"); sel.setAttribute("aria-label", "Red"); sel.setAttribute("data-native", "");
        REDES.forEach(function (x) { var o = document.createElement("option"); o.value = x[0]; o.textContent = x[1]; sel.appendChild(o); });
        sel.value = red || "tiktok";
        var at = document.createElement("label"); at.className = "ri-at";
        var pre = document.createElement("span"); pre.textContent = "@"; pre.setAttribute("aria-hidden", "true");
        inp.placeholder = "cuenta"; inp.autocapitalize = "none"; inp.autocomplete = "off"; inp.spellcheck = false; inp.setAttribute("aria-label", "Cuenta");
        inp.addEventListener("change", function () { inp.value = leerCuenta(inp.value, sel); guardar(); });
        inp.addEventListener("paste", function () { setTimeout(function () { inp.value = leerCuenta(inp.value, sel); guardar(); }, 0); });
        sel.addEventListener("change", guardar);
        at.appendChild(pre); at.appendChild(inp);
        r.appendChild(sel); r.appendChild(at);
      } else {
        inp.placeholder = box.getAttribute("data-ph") || ""; inp.maxLength = 300; inp.setAttribute("aria-label", inp.placeholder || "Texto");
        r.appendChild(inp);
      }
      var del = document.createElement("button"); del.type = "button"; del.className = "ri-del"; del.setAttribute("aria-label", "Quitar"); del.title = "Quitar";
      del.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>';
      del.addEventListener("click", function () {
        r.remove();
        if (!list.children.length) fila(acc ? (sel && sel.value) : "", "");
        guardar();
      });
      inp.addEventListener("input", guardar);
      inp.addEventListener("keydown", function (e) {
        if (e.key === "Enter") { e.preventDefault(); if (inp.value.trim()) fila(acc ? r.querySelector("select").value : "", "", true); }
      });
      r.appendChild(del);
      list.appendChild(r);
      if (foco) inp.focus();
      return r;
    }
    // Lo que ya hubiera escrito (al editar) se reparte en filas
    var lineas = ta.value.split(/\n/).map(function (l) { return l.trim(); }).filter(Boolean);
    lineas.forEach(function (l) {
      if (!acc) { fila("", l); return; }
      var tmp = document.createElement("select"); REDES.forEach(function (x) { var o = document.createElement("option"); o.value = x[0]; tmp.appendChild(o); });
      var low = l.toLowerCase(), red = REDES.map(function (x) { return x[0]; }).filter(function (k) { return low.indexOf(k) > -1; })[0] || "tiktok";
      tmp.value = red;
      var h = leerCuenta(l.indexOf("/") > -1 ? l : l.split(/\s+/).pop(), tmp);
      fila(tmp.value, h);
    });
    if (!lineas.length) fila("", "");
    add.addEventListener("click", function () {
      var last = list.lastElementChild, red = acc && last ? last.querySelector("select").value : "";
      fila(red, "", true);
    });
    var form = box.closest("form");
    if (form) form.addEventListener("submit", guardar);
    guardar();
  });
}

// =============================================================== Filtros de tabla dentro de la página (Campañas del panel)
// La barra no cambia de página: esconde las filas que no tocan y deja la pastilla deslizarse.
document.addEventListener("click", function (e) {
  var b = e.target.closest("[data-row-filter] [data-f]");
  if (!b) return;
  var nav = b.closest("[data-row-filter]"), body = document.querySelector(nav.getAttribute("data-row-filter"));
  if (!body) return;
  e.preventDefault();
  var f = b.getAttribute("data-f"), shown = 0;
  nav.querySelectorAll("[data-f]").forEach(function (x) { var on = x === b; x.classList.toggle("on", on); if (on) x.setAttribute("aria-current", "true"); else x.removeAttribute("aria-current"); });
  body.querySelectorAll("tr[data-f]").forEach(function (tr) {
    var out = !!f && tr.getAttribute("data-f") !== f;
    tr.classList.toggle("f-out", out);
    if (!out) shown++;
  });
  var empty = body.querySelector("[data-f-empty]"); if (empty) empty.hidden = shown > 0;
  var p = nav.getAttribute("data-param");
  if (p && history.replaceState) {
    var u = new URL(location.href);
    if (f) u.searchParams.set(p, f); else u.searchParams.delete(p);
    history.replaceState(history.state, "", u.pathname + u.search + u.hash);
  }
});

// =============================================================== Resumen del panel: al cambiar de periodo, las gráficas
// pasan de los valores de antes a los nuevos (las barras se estiran o encogen y las líneas se transforman),
// en vez de volver a crecer desde cero como al entrar.
(function () {
  var prev = null;
  function pts(el) { return (el.getAttribute("points") || "").trim().split(/\s+/).map(function (p) { return p.split(",").map(Number); }); }
  function resample(a, n) {                         // misma forma con n puntos, para poder mezclar dos líneas
    if (!a.length) return [];
    var out = [];
    for (var i = 0; i < n; i++) {
      var t = n === 1 ? 0 : i / (n - 1) * (a.length - 1), j = Math.floor(t), f = t - j, b = a[Math.min(j + 1, a.length - 1)];
      out.push([a[j][0] + (b[0] - a[j][0]) * f, a[j][1] + (b[1] - a[j][1]) * f]);
    }
    return out;
  }
  document.addEventListener("turbo:before-render", function (ev) {
    var plot = document.querySelector(".v2-plot"), nb = ev.detail && ev.detail.newBody;
    if (!plot || !nb || !nb.querySelector(".v2-plot")) { prev = null; document.documentElement.classList.remove("v2-morph"); return; }
    document.documentElement.classList.add("v2-morph");   // sin la animación de entrada: se pasa de lo de antes a lo nuevo
    prev = {
      bars: Array.prototype.map.call(plot.querySelectorAll(".v2-trio"), function (t) {
        return Array.prototype.map.call(t.children, function (s) { return s.style.height; });
      }),
      sparks: Array.prototype.map.call(document.querySelectorAll(".v2-spark polyline"), pts)
    };
  });
  document.addEventListener("turbo:load", function () {
    var p = prev; prev = null;
    var plot = document.querySelector(".v2-plot");
    if (!p || !plot) return;
    if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    Array.prototype.forEach.call(plot.querySelectorAll(".v2-trio"), function (t, i) {
      Array.prototype.forEach.call(t.children, function (s, k) {
        var to = s.style.height;
        s.style.transition = "none";
        s.style.height = (p.bars[i] && p.bars[i][k]) || "0%";
        void s.offsetHeight;
        s.style.transition = "height .6s cubic-bezier(.3,.7,.2,1) " + (i * 35) + "ms";
        s.style.height = to;
      });
    });
    var lines = document.querySelectorAll(".v2-spark polyline");
    var jobs = [];
    Array.prototype.forEach.call(lines, function (pl, i) {
      var to = pts(pl), from = resample(p.sparks[i] || [], to.length), poly = pl.parentNode.querySelector("polygon");
      if (from.length !== to.length || !to.length) return;
      jobs.push({ pl: pl, poly: poly, from: from, to: to });
    });
    var t0 = performance.now(), D = 600;
    function paint(now) {
      var k = Math.min(1, (now - t0) / D), e = 1 - Math.pow(1 - k, 3);
      jobs.forEach(function (j) {
        var s = j.to.map(function (q, n) { return (j.from[n][0] + (q[0] - j.from[n][0]) * e).toFixed(1) + "," + (j.from[n][1] + (q[1] - j.from[n][1]) * e).toFixed(1); }).join(" ");
        j.pl.setAttribute("points", s);
        if (j.poly) j.poly.setAttribute("points", "0,34 " + s + " 120,34");
      });
      if (k < 1) requestAnimationFrame(paint);
    }
    if (jobs.length) requestAnimationFrame(paint);
  });
  document.addEventListener("turbo:before-cache", function () { document.documentElement.classList.remove("v2-morph"); });
})();

// Ajustes: «Cambiar de cuenta de Whop» enseña las filas para poner otro usuario
document.addEventListener("click", function (e) {
  var b = e.target.closest("[data-show-whop]");
  if (!b) return;
  var f = document.querySelector("[data-whop-change]");
  if (f) { f.hidden = false; b.hidden = true; var i = f.querySelector("input[name=whop_username]"); if (i) i.focus(); }
});

// Resumen: pestañas de la tarjeta de gráficas (cambia sin recargar y repite la animación de entrada)
document.addEventListener("click", function (e) {
  var b = e.target.closest("[data-ov-tab]");
  if (!b) return;
  var box = b.closest("[data-ov-charts]"), k = b.getAttribute("data-ov-tab");
  box.querySelectorAll("[data-ov-tab]").forEach(function (x) { x.classList.toggle("on", x === b); x.setAttribute("aria-selected", x === b ? "true" : "false"); });
  box.querySelectorAll("[data-ov-pane]").forEach(function (p) {
    var on = p.getAttribute("data-ov-pane") === k;
    p.hidden = !on;
    if (on) { var c = p.cloneNode(true); p.parentNode.replaceChild(c, p); }   // copia nueva: las animaciones vuelven a empezar
  });
  var t = box.querySelector("[data-ov-title]"); if (t) t.textContent = b.textContent.trim();
  try { sessionStorage.setItem("ov-tab", k); } catch (_) {}
});
document.addEventListener("turbo:load", function () {
  var k; try { k = sessionStorage.getItem("ov-tab"); } catch (_) {}
  var b = k && document.querySelector("[data-ov-charts] [data-ov-tab='" + k + "']");
  if (!b || b.classList.contains("on")) return;
  var box = b.closest("[data-ov-charts]");
  box.querySelectorAll("[data-ov-tab]").forEach(function (x) { x.classList.toggle("on", x === b); x.setAttribute("aria-selected", x === b ? "true" : "false"); });
  box.querySelectorAll("[data-ov-pane]").forEach(function (p) { p.hidden = p.getAttribute("data-ov-pane") !== k; });
  var t = box.querySelector("[data-ov-title]"); if (t) t.textContent = b.textContent.trim();
});
