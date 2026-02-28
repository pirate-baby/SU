/*
 * Service Worker for SU.
 *
 * Minimal shell — push notifications have been replaced by Telegram.
 * This file is kept for potential future offline/caching support.
 */

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
