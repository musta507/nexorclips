// ClipProfit Hispano — JavaScript de la web.
// La web usa Turbo: al pulsar un enlace no se recarga la página entera, solo se cambia el contenido.
// Por eso hay dos partes:
//   1) Escuchas globales (clics, envíos…): se ponen una sola vez y sirven para todas las páginas.
//   2) pagina(): lo que hay que preparar cada vez que se muestra una página (chat, color de la destacada…).

// =============================================================== Turbo
if (window.Turbo) {
  Turbo.session.drive = true;
  // Los formularios se envían de la forma normal (más seguro con la web tal como está); los enlaces, con Turbo.
  if (Turbo.config && Turbo.config.forms) Turbo.config.forms.mode = "off"; else if (Turbo.setFormMode) Turbo.setFormMode("off");
  if (Turbo.setProgressBarDelay) Turbo.setProgressBarDelay(400);   // la barra de carga solo aparece si algo tarda mucho
}

// =============================================================== 1) escuchas globales

// Formularios: evitar envíos dobles y confirmar acciones delicadas
document.addEventListener("submit", function (e) {
  var form = e.target;
  if (form.hasAttribute("data-chat-form")) return;   // el chat se envía por su cuenta
  var confirmMsg = form.getAttribute("data-confirm");
  if (confirmMsg && !window.confirm(confirmMsg)) { e.preventDefault(); return; }
  if (form.dataset.sending === "1") { e.preventDefault(); return; }
  form.dataset.sending = "1";
  var btn = e.submitter || form.querySelector("button[type=submit], button:not([type])");
  if (btn) { btn.setAttribute("aria-busy", "true"); btn.classList.add("busy"); }
  setTimeout(function () { form.dataset.sending = ""; if (btn) { btn.classList.remove("busy"); btn.removeAttribute("aria-busy"); } }, 8000);
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
  document.querySelectorAll("details.user-menu[open]").forEach(function (d) { if (!d.contains(e.target)) d.removeAttribute("open"); });
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
    .sort(function (x, y) { return parseFloat(y.dataset[k] || 0) - parseFloat(x.dataset[k] || 0); })
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
        if (!d || (d.unread || 0) === shown) return;
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
  colorDestacada();
  pastillaOrden();
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

// La tarjeta destacada toma el color de su imagen (como las portadas en Spotify)
function colorDestacada() {
  var card = document.querySelector(".featured");
  var img = card && card.querySelector(".art img");
  if (!img || card.dataset.tinted) return;
  function run() {
    try {
      var c = document.createElement("canvas"); c.width = c.height = 24;
      var x = c.getContext("2d"); x.drawImage(img, 0, 0, 24, 24);
      var d = x.getImageData(0, 0, 24, 24).data, r = 0, g = 0, bl = 0, n = 0;
      for (var i = 0; i < d.length; i += 4) {
        var w = 1 + (Math.max(d[i], d[i + 1], d[i + 2]) - Math.min(d[i], d[i + 1], d[i + 2])) / 40;
        r += d[i] * w; g += d[i + 1] * w; bl += d[i + 2] * w; n += w;
      }
      r /= n; g /= n; bl /= n;
      var k = (0.42 * 255) / Math.max(r, g, bl, 1);
      var deep = "rgb(" + Math.round(r * k) + "," + Math.round(g * k) + "," + Math.round(bl * k) + ")";
      var mid = "rgb(" + Math.round(r * .62) + "," + Math.round(g * .62) + "," + Math.round(bl * .62) + ")";
      card.style.background = "radial-gradient(120% 140% at 0% 0%, " + mid + " 0%, " + deep + " 45%, #0b0b0c 100%)";
      card.dataset.tinted = "1";
    } catch (err) { /* imagen de otra web: se queda en negro */ }
  }
  if (img.complete && img.naturalWidth) run(); else img.addEventListener("load", run, { once: true });
}

// Pastilla negra que se desliza en «Más nuevas / Más presupuesto / Mejor pago»
function pastillaOrden() {
  document.querySelectorAll(".sortbar").forEach(function (box) {
    if (box.dataset.pill) return;
    box.dataset.pill = "1";
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
    place(box.querySelector("button.on"), false);
    box.classList.add("pill-ready");
    box.addEventListener("click", function (e) { var it = e.target.closest("button"); if (it) place(it, true); });
    var onResize = function () { place(box.querySelector("button.on"), false); };
    window.addEventListener("resize", onResize);
    limpiar.push(function () { window.removeEventListener("resize", onResize); });
  });
}

// =============================================================== cuándo se ejecuta
function salirDePagina() {
  limpiar.forEach(function (f) { try { f(); } catch (e) {} });
  limpiar = [];
}
if (window.Turbo) {
  document.addEventListener("turbo:load", pagina);              // también en la primera carga
  document.addEventListener("turbo:before-render", salirDePagina);
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
