// Reservar llamada (/reservar): calendario con las horas libres en la hora del visitante.
(function () {
  var root = document.querySelector("[data-book]");
  if (!root) return;
  var MES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"];
  var DIA = ["domingo", "lunes", "martes", "miércoles", "jueves", "viernes", "sábado"];
  function utc(s) { return new Date(s + ":00Z"); }
  function key(d) { return d.getFullYear() + "-" + (d.getMonth() + 1) + "-" + d.getDate(); }
  function hora(d) { return d.toLocaleTimeString("es-ES", { hour: "2-digit", minute: "2-digit" }); }
  function largo(d) { var t = DIA[d.getDay()] + ", " + d.getDate() + " de " + MES[d.getMonth()]; return t.charAt(0).toUpperCase() + t.slice(1); }
  var zone = ""; try { zone = Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch (e) {}

  var done = root.querySelector("[data-utc]");
  if (done) { var dd = utc(done.getAttribute("data-utc")); done.textContent = largo(dd) + " · " + hora(dd); return; }

  var slots = JSON.parse(root.getAttribute("data-slots") || "[]");
  var byDay = {}, first = null;
  slots.forEach(function (s) { var d = utc(s), k = key(d); (byDay[k] = byDay[k] || []).push([s, d]); if (!first) first = d; });
  var at = root.querySelector("[data-at]"), daysEl = root.querySelector("[data-days]"), timesEl = root.querySelector("[data-times]");
  var mlabel = root.querySelector("[data-mlabel]"), dlabel = root.querySelector("[data-dlabel]");
  var step1 = root.querySelector('[data-step="1"]'), step2 = root.querySelector('[data-step="2"]'), sel = root.querySelector("[data-sel]");
  var tz = root.querySelector("[data-tz]"); if (tz && zone) tz.textContent = "Horas en tu zona horaria (" + zone.replace(/_/g, " ") + ")";
  var view = first ? new Date(first.getFullYear(), first.getMonth(), 1) : new Date(new Date().getFullYear(), new Date().getMonth(), 1);
  var chosenDay = first ? key(first) : null;

  function paintMonth() {
    mlabel.textContent = MES[view.getMonth()].charAt(0).toUpperCase() + MES[view.getMonth()].slice(1) + " " + view.getFullYear();
    daysEl.innerHTML = "";
    var lead = (view.getDay() + 6) % 7, n = new Date(view.getFullYear(), view.getMonth() + 1, 0).getDate();
    for (var i = 0; i < lead; i++) daysEl.appendChild(document.createElement("span"));
    for (var dnum = 1; dnum <= n; dnum++) {
      var d = new Date(view.getFullYear(), view.getMonth(), dnum), k = key(d), b = document.createElement("button");
      b.type = "button"; b.textContent = dnum; b.className = "bk-day";
      if (byDay[k]) { b.classList.add("free"); b.dataset.k = k; if (k === chosenDay) b.classList.add("on"); }
      else b.disabled = true;
      daysEl.appendChild(b);
    }
  }
  function paintTimes() {
    timesEl.innerHTML = "";
    var list = chosenDay && byDay[chosenDay];
    if (!list) { dlabel.textContent = "Elige un día"; return; }
    dlabel.textContent = largo(list[0][1]);
    list.forEach(function (it) {
      var b = document.createElement("button"); b.type = "button"; b.className = "bk-slot"; b.textContent = hora(it[1]); b.dataset.s = it[0];
      timesEl.appendChild(b);
    });
  }
  function pick(s) {
    at.value = s; var d = utc(s);
    sel.textContent = largo(d) + " · " + hora(d);
    step1.hidden = true; step2.hidden = false;
    var f = root.querySelector("#bk-name"); if (f && !f.value) f.focus();
  }
  root.addEventListener("click", function (e) {
    var t = e.target.closest("button"); if (!t) return;
    if (t.dataset.mv) { view = new Date(view.getFullYear(), view.getMonth() + parseInt(t.dataset.mv, 10), 1); paintMonth(); }
    else if (t.dataset.k) { chosenDay = t.dataset.k; paintMonth(); paintTimes(); if (window.innerWidth < 720) timesEl.scrollIntoView({ block: "nearest", behavior: "smooth" }); }
    else if (t.dataset.s) pick(t.dataset.s);
    else if (t.hasAttribute("data-back")) { step2.hidden = true; step1.hidden = false; }
  });
  root.querySelector("form").addEventListener("submit", function (e) {
    if (!at.value) { e.preventDefault(); step2.hidden = true; step1.hidden = false; return; }
    var b = root.querySelector(".bk-go"); b.disabled = true; b.textContent = "Reservando…";
  });
  paintMonth(); paintTimes();
  if (at.value && !step2.hidden) { var d0 = utc(at.value); sel.textContent = largo(d0) + " · " + hora(d0); }
})();
