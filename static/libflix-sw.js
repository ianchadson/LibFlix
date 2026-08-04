/* LibFlix service worker: a deliberately small, public static shell only. */
'use strict';

const scriptUrl = new URL(self.location.href);
const CURRENT_VERSION = /^[A-Za-z0-9._-]{1,40}$/.test(scriptUrl.searchParams.get('v') || '')
  ? scriptUrl.searchParams.get('v')
  : '1';
const STATIC_CACHE_PREFIX = 'libflix-static-';
const STATIC_CACHE = STATIC_CACHE_PREFIX + CURRENT_VERSION;
const TEMP_CACHE = STATIC_CACHE + '-installing';
const SHELL_PATHS = Object.freeze([
  '/static/libflix.css',
  '/static/download-ui.js',
  '/static/libflix-pwa.js',
  '/static/manifest.webmanifest',
  '/static/libflix-offline.html',
  '/static/icons/libflix-icon-192.png',
  '/static/icons/libflix-icon-512.png',
  '/static/icons/libflix-icon-maskable-512.png',
]);
const SHELL_PATH_SET = new Set(SHELL_PATHS);
const SENSITIVE_PATHS = Object.freeze([
  '/download/',
  '/api/kindle/',
  '/api/sendtokindle',
  '/api/search',
  '/search',
  '/settings',
  '/admin',
  '/login',
  '/logout',
]);

const versionedUrl = pathname => {
  const url = new URL(pathname, self.location.origin);
  url.searchParams.set('v', CURRENT_VERSION);
  return url;
};

const shellRequest = pathname => new Request(versionedUrl(pathname).href, {
  credentials: 'same-origin',
  cache: 'reload',
});

const isAllowedShellRequest = url => (
  url.origin === self.location.origin &&
  SHELL_PATH_SET.has(url.pathname) &&
  url.searchParams.size === 1 &&
  url.searchParams.get('v') === CURRENT_VERSION
);

const isSensitivePath = pathname => SENSITIVE_PATHS.some(prefix => (
  prefix.endsWith('/') ? pathname.startsWith(prefix) : pathname === prefix || pathname.startsWith(prefix + '/')
));

const isPublicShellResponse = response => {
  if (!response || !response.ok || response.type !== 'basic') return false;
  const cacheControl = response.headers.get('cache-control') || '';
  return /(?:^|,)\s*public(?:\s|,|=|$)/i.test(cacheControl) &&
    !/(?:^|,)\s*(?:private|no-store|no-cache)(?:\s|,|=|$)/i.test(cacheControl);
};

const populateShellCache = async () => {
  await caches.delete(TEMP_CACHE);
  const temporary = await caches.open(TEMP_CACHE);
  try {
    const entries = await Promise.all(SHELL_PATHS.map(async pathname => {
      const request = shellRequest(pathname);
      const response = await fetch(request);
      if (!isPublicShellResponse(response)) {
        throw new Error('Shell asset is not publicly cacheable: ' + pathname);
      }
      return { request, response };
    }));
    await Promise.all(entries.map(({ request, response }) => temporary.put(request, response)));

    // Only copy a completely fetched shell into the live cache. Existing live
    // entries remain available if any fetch or write above fails.
    const live = await caches.open(STATIC_CACHE);
    for (const pathname of SHELL_PATHS) {
      const request = shellRequest(pathname);
      const response = await temporary.match(request);
      if (!response) throw new Error('Temporary shell cache is incomplete');
      await live.put(request, response);
    }
  } catch (error) {
    await caches.delete(TEMP_CACHE);
    throw error;
  }
  await caches.delete(TEMP_CACHE);
};

const cachedOfflineDocument = async () => {
  const cache = await caches.open(STATIC_CACHE);
  const response = await cache.match(shellRequest('/static/libflix-offline.html'));
  if (response) return response;
  return new Response('LibFlix is offline.', {
    status: 503,
    headers: { 'Content-Type': 'text/plain; charset=utf-8', 'Cache-Control': 'no-store' },
  });
};

const cacheFirstShell = async request => {
  const cache = await caches.open(STATIC_CACHE);
  const cached = await cache.match(request);
  if (cached) return cached;
  const response = await fetch(request);
  if (isPublicShellResponse(response)) await cache.put(request, response.clone());
  return response;
};

self.addEventListener('install', event => {
  // A failed/incomplete shell fails installation, leaving the previous worker
  // and its complete cache untouched. Updates wait until existing tabs close.
  event.waitUntil(populateShellCache());
});

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    const names = await caches.keys();
    await Promise.all(names
      .filter(name => (
        (name.startsWith(STATIC_CACHE_PREFIX) && name !== STATIC_CACHE) ||
        name.startsWith('libflix-metadata-') ||
        name.startsWith('libflix-navigation-')
      ))
      .map(name => caches.delete(name)));
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', event => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (isAllowedShellRequest(url)) {
    event.respondWith(cacheFirstShell(request));
    return;
  }

  // Dynamic responses are never cached. Safe app navigations only receive the
  // static offline fallback if the network itself is unavailable.
  if (request.mode === 'navigate' && !isSensitivePath(url.pathname)) {
    event.respondWith(fetch(request).catch(cachedOfflineDocument));
  }
});
