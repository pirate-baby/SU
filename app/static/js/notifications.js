/*
 * Push notification registration for SU.
 *
 * Registers the service worker on page load (allowed without gesture).
 * Permission request and push subscription happen on the first user
 * interaction, since modern browsers silently block requestPermission()
 * calls that aren't triggered by a user gesture.
 */

let _swRegistration = null;
let _pushSubscribed = false;

async function registerServiceWorker() {
    if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
        console.log("Push notifications not supported in this browser");
        return;
    }

    try {
        _swRegistration = await navigator.serviceWorker.register("/static/sw.js");
        console.log("Service worker registered:", _swRegistration.scope);

        // Check if we already have a subscription
        const existing = await _swRegistration.pushManager.getSubscription();
        if (existing) {
            console.log("Push subscription already active");
            _pushSubscribed = true;
        }
    } catch (err) {
        console.error("Service worker registration failed:", err);
    }
}

async function requestPushPermission() {
    if (_pushSubscribed || !_swRegistration) return;

    try {
        // This must be called from a user gesture (click/tap)
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
        const subscription = await _swRegistration.pushManager.subscribe({
            userVisibleOnly: true,
            applicationServerKey,
        });

        // Send subscription to backend
        await fetch("/api/push/subscribe", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(subscription.toJSON()),
        });

        _pushSubscribed = true;
        console.log("Push notifications enabled");
    } catch (err) {
        console.error("Failed to subscribe to push notifications:", err);
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

// Register service worker on load (no gesture needed)
registerServiceWorker();

// Request permission on first user click anywhere on the page
document.addEventListener("click", function onFirstClick() {
    document.removeEventListener("click", onFirstClick);
    requestPushPermission();
}, { once: true });
