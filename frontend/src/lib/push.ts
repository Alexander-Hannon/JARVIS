import { api } from '../api/client';

export function isIOS(): boolean {
  return /iPhone|iPad|iPod/i.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
}
export function isStandalone(): boolean {
  return window.matchMedia?.('(display-mode: standalone)').matches || (navigator as unknown as { standalone?: boolean }).standalone === true;
}

/** Returns why Web Push cannot work here, or null if the browser supports it. */
export function pushUnsupportedReason(): string | null {
  if (!window.isSecureContext) return 'Web Push requires HTTPS (or localhost). Open JARVIS over HTTPS to enable push.';
  if (!('serviceWorker' in navigator)) return 'This browser does not support service workers, so it cannot receive push notifications.';
  if (isIOS() && !isStandalone())
    return 'On iPhone/iPad, push only works from the installed home-screen app: tap Share → Add to Home Screen, open JARVIS from the icon, then enable push here (iOS 16.4+).';
  if (!('PushManager' in window)) return 'This browser does not support the Push API.';
  if (!('Notification' in window)) return 'This browser does not support notifications.';
  return null;
}

export async function registerServiceWorker(): Promise<ServiceWorkerRegistration> {
  const existing = await navigator.serviceWorker.getRegistration('/');
  if (existing) return existing;
  return navigator.serviceWorker.register('/sw.js', { scope: '/' });
}

function urlBase64ToUint8Array(base64: string): Uint8Array<ArrayBuffer> {
  const padding = '='.repeat((4 - (base64.length % 4)) % 4);
  const b64 = (base64 + padding).replace(/-/g, '+').replace(/_/g, '/');
  const raw = atob(b64);
  const out = new Uint8Array(new ArrayBuffer(raw.length));
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

export async function currentSubscription(): Promise<PushSubscription | null> {
  if (pushUnsupportedReason()) return null;
  const reg = await navigator.serviceWorker.getRegistration('/');
  return reg ? reg.pushManager.getSubscription() : null;
}

export async function subscribePush(vapidKey: string): Promise<PushSubscription> {
  const perm = await Notification.requestPermission();
  if (perm !== 'granted') throw new Error(perm === 'denied' ? 'Notification permission was denied. Re-enable it in browser/site settings.' : 'Notification permission was not granted.');
  const reg = await registerServiceWorker();
  await navigator.serviceWorker.ready;
  const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: urlBase64ToUint8Array(vapidKey) });
  const json = sub.toJSON();
  if (!json.endpoint || !json.keys?.p256dh || !json.keys?.auth) throw new Error('The browser returned an incomplete push subscription.');
  await api.pushSubscribe({ endpoint: json.endpoint, keys: { p256dh: json.keys.p256dh, auth: json.keys.auth } });
  return sub;
}

export async function unsubscribePush(): Promise<void> {
  const sub = await currentSubscription();
  if (!sub) return;
  await api.pushUnsubscribe(sub.endpoint).catch(() => undefined);
  await sub.unsubscribe();
}
