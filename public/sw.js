var CACHE_VERSION = 'lumn-v2';
var STATIC_ASSETS = ['/public/timeline.js'];

self.addEventListener('install', function(e) {
    e.waitUntil(
        caches.open(CACHE_VERSION).then(function(cache) {
            return cache.addAll(STATIC_ASSETS);
        }).then(function() {
            return self.skipWaiting();
        })
    );
});

self.addEventListener('activate', function(e) {
    e.waitUntil(
        caches.keys().then(function(keys) {
            return Promise.all(
                keys.filter(function(key) { return key !== CACHE_VERSION; })
                    .map(function(key) { return caches.delete(key); })
            );
        }).then(function() {
            return self.clients.claim();
        })
    );
});

self.addEventListener('fetch', function(e) {
    if (e.request.url.includes('/api/')) return;
    if (e.request.method !== 'GET') return;
    e.respondWith(
        fetch(e.request).then(function(response) {
            var clone = response.clone();
            caches.open(CACHE_VERSION).then(function(cache) {
                cache.put(e.request, clone);
            });
            return response;
        }).catch(function() {
            return caches.match(e.request);
        })
    );
});
