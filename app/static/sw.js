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
  const isInterjectionLink = url.includes("/from-interjection/");

  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then((windowClients) => {
      // For interjection links, always open a new contextified session
      if (isInterjectionLink) {
        // Navigate an existing tab if one is open, otherwise open new
        for (const client of windowClients) {
          if (client.url.includes(self.location.origin) && "navigate" in client) {
            return client.navigate(url).then((c) => c.focus());
          }
        }
        return clients.openWindow(url);
      }

      // For generic URLs, focus an existing tab if one is open
      for (const client of windowClients) {
        if (client.url.includes(self.location.origin) && "focus" in client) {
          return client.focus();
        }
      }
      return clients.openWindow(url);
    })
  );
});
