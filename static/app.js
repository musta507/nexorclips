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
  var btn = e.target.closest("[data-copy]");
  if (!btn) return;
  var target = document.getElementById(btn.getAttribute("data-copy"));
  if (!target) return;
  var text = target.value || target.textContent;
  var done = function () { var t = btn.textContent; btn.textContent = "Copiado"; setTimeout(function () { btn.textContent = t; }, 1600); };
  if (navigator.clipboard) navigator.clipboard.writeText(text).then(done, function () { target.select && target.select(); });
  else if (target.select) target.select();
});

// Cerrar el menú de usuario al hacer clic fuera
document.addEventListener("click", function (e) {
  document.querySelectorAll("details.user-menu[open], details.bell-menu[open]").forEach(function (d) { if (!d.contains(e.target)) d.removeAttribute("open"); });
});

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
    if (count) { count.textContent = n; count.hidden = !n; }
    if (fab) fab.classList.toggle("has-unread", !!n);
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
  iniciarChat();
  pastillaOrden();
  graficas();
  asistente();
  avisosPush();
  subidaVideo();
  progresoLeccion();
  desplegables();
  destacado();
  graficasClips();
}

// =============================================================== gráfica de estadísticas (informe y panel)
var MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"];
var METRICA = { views: "Visualizaciones", likes: "Me gusta", comments: "Comentarios", shares: "Compartidos" };
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
function graficas() {
  document.querySelectorAll("[data-chart]").forEach(function (box) {
    if (box.dataset.ready) return;
    var src = box.querySelector("[data-chart-data]"), plot = box.querySelector("[data-chart-plot]");
    if (!src || !plot) return;
    box.dataset.ready = "1";
    var data = JSON.parse(src.textContent), state = { mode: "cumulative", metric: "views" };
    var NS = "http://www.w3.org/2000/svg";
    function el(tag, attrs, parent) {
      var e = document.createElementNS(NS, tag);
      for (var k in attrs) e.setAttribute(k, attrs[k]);
      if (parent) parent.appendChild(e);
      return e;
    }
    function draw() {
      var labels = data.series.labels, vals = data.series[state.mode][state.metric];
      var W = plot.clientWidth || 600, H = W < 560 ? 220 : 300, L = 44, R = 12, T = 12, B = 30;
      var iw = W - L - R, ih = H - T - B, n = vals.length, eje = ejeY(Math.max.apply(null, vals.concat([0]))), max = eje.top;
      var x = function (i) { return L + (n > 1 ? i / (n - 1) : 0.5) * iw; };
      var y = function (v) { return T + ih - (v / max) * ih; };
      plot.innerHTML = "";
      var svg = el("svg", { viewBox: "0 0 " + W + " " + H, width: W, height: H, role: "img",
        "aria-label": METRICA[state.metric] + " " + (state.mode === "daily" ? "por día" : "acumuladas") }, plot);
      for (var gv = 0; gv <= max + eje.step / 2; gv += eje.step) {   // rejilla y eje Y
        var gy = y(gv);
        el("line", { x1: L, x2: W - R, y1: gy, y2: gy, class: "an-grid" }, svg);
        el("text", { x: L - 8, y: gy + 4, class: "an-axis", "text-anchor": "end" }, svg).textContent = corto(gv);
      }
      var step = Math.max(1, Math.ceil(n / (W < 560 ? 4 : 7)));   // fechas del eje X
      for (var i = 0; i < n; i += step) el("text", { x: x(i), y: H - 8, class: "an-axis", "text-anchor": "middle" }, svg).textContent = fechaCorta(labels[i]);
      if (state.mode === "daily") {                        // barras por día
        var bw = Math.max(2, Math.min(18, iw / n * 0.7));
        vals.forEach(function (v, i) { if (v > 0) el("rect", { x: x(i) - bw / 2, y: y(v), width: bw, height: Math.max(1, T + ih - y(v)), rx: Math.min(4, bw / 2), class: "an-barv" }, svg); });
      } else {                                             // línea con relleno
        var d = vals.map(function (v, i) { return (i ? "L" : "M") + x(i).toFixed(1) + " " + y(v).toFixed(1); }).join(" ");
        var defs = el("defs", {}, svg), grad = el("linearGradient", { id: "an-fill-g", x1: 0, x2: 0, y1: 0, y2: 1 }, defs);
        el("stop", { offset: "0%", class: "an-g0" }, grad); el("stop", { offset: "100%", class: "an-g1" }, grad);
        el("path", { d: d + " L" + x(n - 1).toFixed(1) + " " + (T + ih) + " L" + x(0).toFixed(1) + " " + (T + ih) + " Z", fill: "url(#an-fill-g)" }, svg);
        el("path", { d: d, class: "an-line" }, svg);
      }
      var guide = el("line", { y1: T, y2: T + ih, class: "an-guide", visibility: "hidden" }, svg);
      var dot = el("circle", { r: 5, class: "an-dot", visibility: "hidden" }, svg);
      var tip = document.createElement("div"); tip.className = "an-tip"; tip.hidden = true; plot.appendChild(tip);
      function move(ev) {
        var r = svg.getBoundingClientRect(), px = (ev.touches ? ev.touches[0].clientX : ev.clientX) - r.left;
        var i = Math.round((px * W / r.width - L) / iw * (n - 1)); i = Math.max(0, Math.min(n - 1, i));
        var cx = x(i), cy = y(vals[i]);
        guide.setAttribute("x1", cx); guide.setAttribute("x2", cx); guide.setAttribute("visibility", "visible");
        dot.setAttribute("cx", cx); dot.setAttribute("cy", cy); dot.setAttribute("visibility", "visible");
        tip.innerHTML = "<b>" + numES(vals[i]) + "</b><span>" + fechaCorta(labels[i]) + "</span>";
        tip.hidden = false;
        var tx = cx * r.width / W, left = Math.min(Math.max(tx - tip.offsetWidth / 2, 0), r.width - tip.offsetWidth);
        tip.style.left = left + "px"; tip.style.top = Math.max(0, cy * r.height / H - tip.offsetHeight - 12) + "px";
      }
      function leave() { guide.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); tip.hidden = true; }
      svg.addEventListener("mousemove", move); svg.addEventListener("touchmove", move, { passive: true });
      svg.addEventListener("mouseleave", leave); svg.addEventListener("touchend", leave);
      box.querySelector("[data-chart-total]").textContent = numES(data.totals[state.metric] || 0);
      box.querySelector("[data-chart-label]").textContent = METRICA[state.metric];
    }
    box.addEventListener("click", function (e) {
      var b = e.target.closest("[data-chart-mode],[data-chart-metric]");
      if (!b) return;
      var key = b.hasAttribute("data-chart-mode") ? "mode" : "metric";
      state[key] = b.getAttribute(key === "mode" ? "data-chart-mode" : "data-chart-metric");
      b.parentNode.querySelectorAll("button").forEach(function (x) { x.classList.toggle("on", x === b); });
      draw();
    });
    draw();
    var redraw = function () { draw(); };
    window.addEventListener("resize", redraw);
    limpiar.push(function () { window.removeEventListener("resize", redraw); });
  });
}

// Chat de soporte
function iniciarChat() {
  var chat = document.querySelector("[data-chat]");
  if (!chat) return;
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
    var empty = document.getElementById("chat-empty");
    if (empty) empty.remove();
    var row = document.createElement("div");
    row.className = "msg " + (m.mine ? "mine" : "theirs");
    row.setAttribute("data-id", m.id);
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
    if (document.hidden || !document.body.contains(chat)) return;
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
  limpiar.push(function () { clearInterval(timer); document.removeEventListener("visibilitychange", onVisible); });
}

// Pastilla negra que se desliza: en «Más nuevas / Más presupuesto / Mejor pago» y en las pestañas de Ganancias. En los menús que cambian de página, se guarda dónde estaba la pastilla
// al pulsar y la página nueva la desliza desde ahí (solo desplazamiento: lo hace la tarjeta gráfica, sin tirones).
var PILL_KEY = "pill-from:";
function pastillaOrden() {
  var grupos = [[".sortbar", "button", ""], [".seg", "a", "seg"]];
  grupos.forEach(function (g) {
    document.querySelectorAll(g[0]).forEach(function (box) { montarPastilla(box, g[1], g[2]); });
  });
}
var pastillasMontadas = new WeakSet();
function montarPastilla(box, sel, name) {
  if (pastillasMontadas.has(box)) return;
  pastillasMontadas.add(box);
  // Una página restaurada por Turbo (atrás / volver) trae la pastilla vieja pero sin sus escuchas: se rehace de cero
  box.querySelectorAll(":scope > .pill").forEach(function (old) { old.remove(); });
  box.classList.remove("has-pill", "pill-ready");
  delete box.dataset.pill;
  var pill = document.createElement("span");
  pill.className = "pill";
  box.insertBefore(pill, box.firstChild);
  box.classList.add("has-pill");
  function place(el, animate) {
    if (!el) { pill.style.opacity = 0; return; }
    if (!animate) pill.style.transition = "none";
    pill.style.width = el.offsetWidth + "px";
    pill.style.height = el.offsetHeight + "px";
    pill.style.transform = "translate(" + el.offsetLeft + "px," + el.offsetTop + "px)";
    pill.style.opacity = 1;
    if (!animate) { void pill.offsetWidth; pill.style.transition = ""; }
  }
  var cur = box.querySelector(sel + ".on");
  var from = null;
  if (name) { try { from = JSON.parse(sessionStorage.getItem(PILL_KEY + name) || "null"); sessionStorage.removeItem(PILL_KEY + name); } catch (e) { from = null; } }
  if (from && cur && pill.animate && Math.abs(from.x - cur.offsetLeft) > 2) {
    var y = cur.offsetTop;
    var startT = "translate(" + Math.round(from.x + from.w / 2 - cur.offsetWidth / 2) + "px," + y + "px)";
    var endT = "translate(" + cur.offsetLeft + "px," + y + "px)";
    pill.style.transition = "none";
    pill.style.width = cur.offsetWidth + "px";
    pill.style.height = cur.offsetHeight + "px";
    pill.style.opacity = 1;
    pill.style.transform = endT;
    pill.animate([{ transform: startT }, { transform: endT }], { duration: 300, easing: "cubic-bezier(.3,.9,.3,1)" })
      .onfinish = function () { pill.style.transition = ""; };
  } else {
    place(cur, false);
  }
  box.classList.add("pill-ready");
  box.addEventListener("click", function (e) {
    var it = e.target.closest(sel);
    if (!it || !box.contains(it) || e.metaKey || e.ctrlKey || e.shiftKey) return;
    var was = box.querySelector(sel + ".on");
    if (was && was !== it) { was.classList.remove("on"); was.removeAttribute("aria-current"); }
    it.classList.add("on");
    place(it, true);                            // la pastilla se mueve al momento; la página nueva ya la trae en su sitio
  });
  var onResize = function () { place(box.querySelector(sel + ".on"), false); };
  window.addEventListener("resize", onResize);
  limpiar.push(function () { window.removeEventListener("resize", onResize); });
}

// =============================================================== cuándo se ejecuta
function salirDePagina() {
  limpiar.forEach(function (f) { try { f(); } catch (e) {} });
  limpiar = [];
}
if (window.Turbo) {
  document.addEventListener("turbo:load", pagina);              // también en la primera carga
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
  if (!wz) return;
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
  if (!box) return;
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
  var box = document.querySelector("[data-bunny-upload]");
  if (!box) return;
  var file = box.querySelector("[data-file]"), start = box.querySelector("[data-start]"), bar = box.querySelector("[data-bar]"),
      fill = bar.querySelector("span"), msg = box.querySelector("[data-msg]");
  var CHUNK = 16 * 1024 * 1024;
  function b64(s) { return btoa(unescape(encodeURIComponent(s))); }
  function fail(t) { msg.textContent = t; start.disabled = false; }
  start.addEventListener("click", function () {
    var f = file.files && file.files[0];
    if (!f) { msg.textContent = "Elige primero el vídeo."; return; }
    start.disabled = true; msg.textContent = "Preparando la subida…";
    var fd = new FormData(); fd.append("_csrf", box.getAttribute("data-csrf"));
    fetch(box.getAttribute("data-prepare"), { method: "POST", body: fd, credentials: "same-origin" })
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
          if (x.status !== 201 || !loc) return fail("Bunny no aceptó la subida (" + x.status + ").");
          loc = new URL(loc, d.endpoint).href;
          bar.hidden = false;
          (function send(offset) {
            if (offset >= f.size) { fill.style.width = "100%"; msg.textContent = "¡Subido! Bunny lo está procesando (unos minutos)."; return estado(); }
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
        x.onerror = function () { fail("No se pudo conectar con Bunny."); };
        x.send(null);
      })
      .catch(function () { fail("No se pudo preparar la subida."); });
  });
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
function progresoLeccion() {
  var box = document.querySelector("[data-lesson-player]");
  if (!box) return;
  var frame = box.querySelector("iframe"), url = box.getAttribute("data-url"), csrf = box.getAttribute("data-csrf");
  var start = parseInt(box.getAttribute("data-start") || "0", 10), last = 0, ended = false;
  function save(extra) {
    var f = new FormData(); f.append("_csrf", csrf);
    Object.keys(extra).forEach(function (k) { f.append(k, extra[k]); });
    return fetch(url, { method: "POST", body: f, credentials: "same-origin", headers: { "X-Requested-With": "fetch" } });
  }
  function cmd(method, value) {
    frame.contentWindow.postMessage(JSON.stringify({ context: "player.js", version: "0.0.11", method: method, value: value, listener: "cp" }), "*");
  }
  function onMsg(e) {
    if (!frame || e.source !== frame.contentWindow) return;
    var d; try { d = typeof e.data === "string" ? JSON.parse(e.data) : e.data; } catch (err) { return; }
    if (!d || d.context !== "player.js") return;
    if (d.event === "ready") {
      cmd("addEventListener", "timeupdate"); cmd("addEventListener", "ended");
      if (start > 5) cmd("setCurrentTime", start);
    } else if (d.event === "timeupdate" && d.value) {
      var s = Math.floor(d.value.seconds || 0);
      if (Math.abs(s - last) >= 15) { last = s; save({ seconds: s }); }
      if (!ended && d.value.duration && s >= d.value.duration * 0.92) { ended = true; save({ done: "1", seconds: s }); }
    } else if (d.event === "ended" && !ended) { ended = true; save({ done: "1" }); }
  }
  window.addEventListener("message", onMsg);
  limpiar.push(function () { window.removeEventListener("message", onMsg); });
}

// Botón «Descargar PDF / imprimir» del informe (el navegador deja guardarlo como PDF)
document.addEventListener("click", function (e) {
  if (e.target.closest("[data-print]")) window.print();
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
      list.innerHTML = "";
      Array.prototype.forEach.call(sel.options, function (op, i) {
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
      mark(sel.selectedIndex);
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
      else if (e.key === "Enter" || e.key === " ") { if (!list.hidden && hi >= 0) { e.preventDefault(); choose(hi); } }
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
  if (!radios.length) return;
  function show() {
    var m = (document.querySelector("[data-feat-mode]:checked") || {}).value;
    document.querySelectorAll("[data-feat]").forEach(function (b) { b.hidden = b.getAttribute("data-feat") !== m; });
  }
  radios.forEach(function (r) { r.addEventListener("change", show); });
  show();
}
