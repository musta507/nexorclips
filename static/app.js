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
    var bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = m.body;
    var meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = (m.mine ? "" : m.who + " · ") + m.ago;
    meta.title = m.at + " UTC";
    row.appendChild(bubble); row.appendChild(meta);
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
    if (sending || !box.value.trim()) return;
    sending = true; errorBox.hidden = true;
    fetch(form.action, { method: "POST", body: new FormData(form), headers: { "X-Requested-With": "fetch" }, credentials: "same-origin" })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (res) {
        if (res.ok && res.d.ok) { add(res.d.message); box.value = ""; grow(); }
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
