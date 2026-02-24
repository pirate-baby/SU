/*
 * Push notification registration for SU.
 *
 * Registers the service worker, requests notification permission,
 * subscribes to Web Push, and sends the subscription to the backend.
 */

const VAPID_PUBLIC_KEY_META = document.querySelector('meta[name="vapid-public-key"]');

async function initNotifications() {
    if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
        console.log("Push notifications not supported in this browser");
        return;
    }

    try {
        const registration = await navigator.serviceWorker.register("/static/sw.js");
        console.log("Service worker registered:", registration.scope);

        // Check if we already have a subscription
        const existing = await registration.pushManager.getSubscription();
        if (existing) {
            console.log("Push subscription already active");
            return;
        }

        // Request permission
        const permission = await Notification.requestPermission();
        if (permission !== "granted") {
            console.log("Notification permission denied");
            return;
        }

        // Get VAPID public key from the API
        const configResp = await fetch("/api/push/vapid-key");
        if (!configResp.ok) {
            console.log("Push notifications not configured on server");
            return;
        }
        const { public_key } = await configResp.json();
        if (!public_key) return;

        // Convert base64 VAPID key to Uint8Array
        const applicationServerKey = urlBase64ToUint8Array(public_key);

        // Subscribe
        const subscription = await registration.pushManager.subscribe({
            userVisibleOnly: true,
            applicationServerKey,
        });

        // Send subscription to backend
        await fetch("/api/push/subscribe", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(subscription.toJSON()),
        });

        console.log("Push notifications enabled");
    } catch (err) {
        console.error("Failed to initialize push notifications:", err);
    }
}

function urlBase64ToUint8Array(base64String) {
    const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
    const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
    const rawData = atob(base64);
    const outputArray = new Uint8Array(rawData.length);
    for (let i = 0; i < rawData.length; i++) {
        outputArray[i] = rawData.charCodeAt(i);
    }
    return outputArray;
}

// Auto-initialize when the script loads
initNotifications();
