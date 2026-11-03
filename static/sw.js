// Avisos de ClipProfit: muestra la notificación y abre la página al pulsarla.
self.addEventListener("push", function (e) {
  var d = {};
  try { d = e.data ? e.data.json() : {}; } catch (err) { d = { body: e.data ? e.data.text() : "" }; }
  e.waitUntil(self.registration.showNotification(d.title || "ClipProfit", {
    body: d.body || "", icon: "/static/icon-180.png", badge: "/static/icon-180.png", data: { url: d.url || "/" }
  }));
});
self.addEventListener("notificationclick", function (e) {
  e.notification.close();
  var url = (e.notification.data && e.notification.data.url) || "/";
  e.waitUntil(clients.matchAll({ type: "window", includeUncontrolled: true }).then(function (list) {
    for (var i = 0; i < list.length; i++) { if (list[i].url.indexOf(self.location.origin) === 0 && "focus" in list[i]) { list[i].navigate(url); return list[i].focus(); } }
    return clients.openWindow(url);
  }));
});
