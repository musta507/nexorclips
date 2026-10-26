// Pequeñas mejoras de uso: evitar envíos dobles, confirmar acciones delicadas y copiar enlaces.
document.addEventListener("submit", function (e) {
  var form = e.target;
  if (form.hasAttribute("data-chat-form")) return;   // el chat se envía por su cuenta
  var confirmMsg = form.getAttribute("data-confirm");
  if (confirmMsg && !window.confirm(confirmMsg)) { e.preventDefault(); return; }
  if (form.dataset.sending === "1") { e.preventDefault(); return; }
  form.dataset.sending = "1";
  var btn = e.submitter || form.querySelector("button[type=submit], button:not([type])");
  if (btn) {
    btn.dataset.label = btn.innerHTML;
    btn.setAttribute("aria-busy", "true");
    btn.classList.add("busy");
  }
  // Si el navegador vuelve atrás, se reactiva el formulario
  setTimeout(function () { form.dataset.sending = ""; if (btn) { btn.classList.remove("busy"); btn.removeAttribute("aria-busy"); } }, 8000);
});

document.addEventListener("click", function (e) {
  var btn = e.target.closest("[data-copy]");
  if (!btn) return;
  var target = document.getElementById(btn.getAttribute("data-copy"));
  if (!target) return;
  var text = target.value || target.textContent;
  var done = function () { var t = btn.textContent; btn.textContent = "Copiado"; setTimeout(function () { btn.textContent = t; }, 1600); };
  if (navigator.clipboard) {
    navigator.clipboard.writeText(text).then(done, function () { target.select && target.select(); });
  } else if (target.select) { target.select(); }
});

// Cierra el menú de usuario al hacer clic fuera
document.addEventListener("click", function (e) {
  document.querySelectorAll("details.user-menu[open]").forEach(function (d) {
    if (!d.contains(e.target)) d.removeAttribute("open");
  });
});

// ---------------------------------------------------------------- chat de soporte
(function () {
  var chat = document.querySelector("[data-chat]");
  if (!chat) return;
  var log = chat.querySelector(".chat-log");
  var form = chat.querySelector("[data-chat-form]");
  var box = form.querySelector("textarea");
  var errorBox = chat.querySelector(".chat-error");
  var fileIn = form.querySelector("[data-chat-file]");
  var fileBadge = form.querySelector(".chat-attach-n");
  var fileName = chat.querySelector("[data-chat-file-name]");
  function showFile() {
    var f = fileIn && fileIn.files && fileIn.files[0];
    if (fileBadge) fileBadge.hidden = !f;
    if (fileName) { fileName.hidden = !f; fileName.textContent = f ? "📷 " + f.name + " · pulsa Enviar" : ""; }
  }
  if (fileIn) fileIn.addEventListener("change", showFile);
  var after = parseInt(chat.getAttribute("data-after") || "0", 10);
  var sending = false;

  function scrollDown() { log.scrollTop = log.scrollHeight; }
  function add(m) {
    if (m.id <= after && log.querySelector('[data-id="' + m.id + '"]')) return;
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
    var bubble = null;
    if (m.body) { bubble = document.createElement("div"); bubble.className = "bubble"; bubble.textContent = m.body; }
    var meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = (m.mine ? "" : m.who + " · ") + m.ago;
    meta.title = m.at + " UTC";
    if (bubble) row.appendChild(bubble);
    row.appendChild(meta);
    log.appendChild(row);
    after = Math.max(after, m.id);
    scrollDown();
  }
  function poll() {
    if (document.hidden) return;
    fetch(chat.getAttribute("data-poll") + "?after=" + after, { headers: { "X-Requested-With": "fetch" }, credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { if (d) d.messages.forEach(add); })
      .catch(function () {});
  }
  function grow() { box.style.height = "auto"; box.style.height = Math.min(box.scrollHeight, 160) + "px"; }

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
  setInterval(poll, 8000);
  document.addEventListener("visibilitychange", function () { if (!document.hidden) poll(); });
})();

// ---------------------------------------------------------------- avisos de mensajes nuevos
// Cada 20 segundos mira si el equipo (o un clipper, si eres admin) te ha escrito y lo muestra al momento:
// un aviso arriba de la página, el número en el botón del chat, un punto en tu avatar y el número en la pestaña.
(function () {
  var body = document.body;
  var url = body.getAttribute("data-notices");
  if (!url || body.getAttribute("data-on-chat") === "1") return;
  var shown = parseInt(body.getAttribute("data-unread") || "0", 10);
  var baseTitle = document.title.replace(/^\(\d+\)\s*/, "");

  function paint(d) {
    var n = d.unread || 0;
    document.title = (n ? "(" + n + ") " : "") + baseTitle;
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
    a.className = "msg-notice is-new";
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

  function check() {
    if (document.hidden) return;
    fetch(url, { headers: { "X-Requested-With": "fetch" }, credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d) return;
        if ((d.unread || 0) !== shown) {
          var more = (d.unread || 0) > shown;
          shown = d.unread || 0;
          paint(d);
          if (more && navigator.vibrate) { try { navigator.vibrate(60); } catch (e) {} }
        }
      })
      .catch(function () {});
  }
  setInterval(check, 20000);
  document.addEventListener("visibilitychange", function () { if (!document.hidden) check(); });
})();

// ---------------------------------------------------------------- respuesta inmediata al tocar
// Barra de carga arriba, pestaña marcada al momento y nada de esperar «a ciegas».
(function () {
  var bar = document.createElement("div");
  bar.id = "nav-progress";
  bar.setAttribute("aria-hidden", "true");
  document.body.appendChild(bar);
  var timer = null;

  function start() {
    clearTimeout(timer);
    bar.className = "";
    void bar.offsetWidth;            // reinicia la animación
    bar.className = "go";
    // Si a los 12 s no ha cambiado de página (p. ej. descarga), se quita
    timer = setTimeout(stop, 12000);
  }
  function stop() { clearTimeout(timer); bar.className = bar.className ? "done" : ""; setTimeout(function () { bar.className = ""; }, 300); }

  document.addEventListener("click", function (e) {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var a = e.target.closest("a[href]");
    if (!a || a.target === "_blank" || a.hasAttribute("download")) return;
    var url = new URL(a.href, location.href);
    if (url.origin !== location.origin) return;
    if (url.pathname === location.pathname && url.search === location.search && url.hash) return;  // ancla en la misma página
    // Pestañas: se marcan al instante
    var nav = a.closest(".tabs");
    if (nav) {
      nav.querySelectorAll(".tab.on").forEach(function (t) { t.classList.remove("on"); t.removeAttribute("aria-current"); });
      a.classList.add("on");
    }
    start();
  });
  // Formularios normales (no el chat, que va por su cuenta)
  document.addEventListener("submit", function (e) {
    if (e.defaultPrevented || e.target.hasAttribute("data-chat-form")) return;
    start();
  });
  // Al volver atrás con el botón del móvil, la página sale de la caché: se limpia todo
  window.addEventListener("pageshow", function (ev) {
    stop();
    if (ev.persisted) {
      document.querySelectorAll("form[data-sending='1']").forEach(function (f) { f.dataset.sending = ""; });
      document.querySelectorAll(".btn.busy").forEach(function (b) { b.classList.remove("busy"); b.removeAttribute("aria-busy"); });
    }
  });
})();

// ---------------------------------------------------------------- campañas: ordenar y color de la destacada
document.addEventListener("click", function (e) {
  var b = e.target.closest("[data-sort]");
  if (!b) return;
  b.parentNode.querySelectorAll("button").forEach(function (x) { x.classList.toggle("on", x === b); });
  var grid = document.querySelector(".grid");
  if (!grid) return;
  var k = b.dataset.sort;
  Array.prototype.slice.call(grid.children)
    .sort(function (x, y) { return parseFloat(y.dataset[k] || 0) - parseFloat(x.dataset[k] || 0); })
    .forEach(function (el) { grid.appendChild(el); });
});
(function () {
  // La tarjeta destacada toma el color de su imagen (como las portadas en Spotify)
  var card = document.querySelector(".featured");
  var img = card && card.querySelector(".art img");
  if (!img) return;
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
    } catch (err) { /* imagen de otra web: se queda en negro */ }
  }
  if (img.complete && img.naturalWidth) run(); else img.addEventListener("load", run);
})();

// ---------------------------------------------------------------- retirar: botón «Todo»
document.addEventListener("click", function (e) {
  var b = e.target.closest(".wd-all"); if (!b) return;
  var i = document.getElementById("wd-amount"); if (i) { i.value = b.getAttribute("data-all"); i.focus(); }
});

// ---------------------------------------------------------------- pastilla negra que se desliza en los selectores
(function () {
  function setup(box, sel) {
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
    place(box.querySelector(sel + ".on"), false);
    box.classList.add("pill-ready");
    box.addEventListener("click", function (e) {
      var it = e.target.closest(sel);
      if (!it || !box.contains(it)) return;
      box.querySelectorAll(sel + ".on").forEach(function (x) { x.classList.remove("on"); });
      it.classList.add("on");
      place(it, it.tagName !== "A");          // en enlaces se cambia de página al momento, sin esperar
    });
    window.addEventListener("resize", function () { place(box.querySelector(sel + ".on"), false); });
  }
  function init() {
    document.querySelectorAll(".sortbar").forEach(function (b) { setup(b, "button"); });
    document.querySelectorAll(".seg").forEach(function (b) { setup(b, "a"); });
    if (window.matchMedia("(min-width:641px)").matches) document.querySelectorAll(".topbar .tabs").forEach(function (b) { setup(b, ".tab"); });
  }
  init();
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(function () { window.dispatchEvent(new Event("resize")); });
})();

// ---------------------------------------------------------------- campañas: al ordenar, las tarjetas se mueven suave
document.addEventListener("click", function (e) {
  var b = e.target.closest("[data-sort]");
  var grid = b && document.querySelector(".grid");
  if (!grid) return;
  // Las posiciones de antes se guardan aquí; el reordenado lo hace el código de arriba en este mismo clic
  var before = new Map();
  Array.prototype.forEach.call(grid.children, function (el) { before.set(el, el.getBoundingClientRect()); });
  requestAnimationFrame(function () {
    Array.prototype.forEach.call(grid.children, function (el) {
      var a = before.get(el), n = el.getBoundingClientRect();
      if (!a) return;
      var dx = a.left - n.left, dy = a.top - n.top;
      if (!dx && !dy) return;
      el.style.transition = "none";
      el.style.transform = "translate(" + dx + "px," + dy + "px)";
      void el.offsetWidth;
      el.style.transition = "transform .45s cubic-bezier(.3,.9,.3,1)";
      el.style.transform = "";
    });
  });
}, true);

// ---------------------------------------------------------------- ventana de retirar
document.addEventListener("click", function (e) {
  var o = e.target.closest("[data-open]");
  if (o) { var d = document.getElementById(o.getAttribute("data-open")); if (d && d.showModal) { d.showModal(); var i = d.querySelector("input[name=amount]"); if (i) i.select(); } return; }
  var c = e.target.closest("[data-close]");
  if (c) { var dl = c.closest("dialog"); if (dl) dl.close(); return; }
  if (e.target.tagName === "DIALOG" && e.target.open) e.target.close();   // clic fuera de la ventana
});
