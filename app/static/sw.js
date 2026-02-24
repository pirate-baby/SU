/*
 * Service Worker for SU push notifications.
 *
 * Listens for push events from the Web Push API and displays native
 * notifications. Clicking a notification opens or focuses the SU app.
 */

self.addEventListener("push", (event) => {
  let payload = { title: "SU", body: "You have a new message." };
  if (event.data) {
    try {
      payload = event.data.json();
    } catch {
      payload.body = event.data.text();
    }
  }

  const options = {
    body: payload.body,
    icon: "/static/icons/icon-192.png",
    badge: "/static/icons/icon-192.png",
    tag: payload.tag || "su-interjection",
    renotify: true,
    data: {
      url: payload.url || "/",
      interjection_id: payload.interjection_id,
    },
  };

  event.waitUntil(self.registration.showNotification(payload.title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();

  const url = event.notification.data?.url || "/";

  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then((windowClients) => {
      // Focus an existing tab if one is open
      for (const client of windowClients) {
        if (client.url.includes(self.location.origin) && "focus" in client) {
          return client.focus();
        }
      }
      // Otherwise open a new tab
      return clients.openWindow(url);
    })
  );
});
