(() => {
    const panel = document.getElementById('buyer-tracking');
    if (!panel) return;
    const status = document.getElementById('buyer-location-status');
    let map, marker, timer;
    async function refresh() {
        clearTimeout(timer);
        if (document.hidden) return;
        try {
            const response = await fetch(panel.dataset.locationUrl, {cache: 'no-store', headers: {'Accept': 'application/json'}});
            if (response.redirected || [401, 403, 404].includes(response.status)) {
                status.textContent = 'Location unavailable. Refresh the page and sign in again if needed.';
                if (marker) { marker.remove(); marker = null; }
                return;
            }
            if (!response.ok) throw new Error('Unable to load location');
            const data = await response.json();
            if (!data.location) {
                if (marker) { marker.remove(); marker = null; }
                status.textContent = 'No shared driver location available for this order.';
            } else {
                const point = [data.location.latitude, data.location.longitude];
                status.textContent = data.is_live ? 'Live driver location' : 'Last known location — no fresh update';
                if (data.last_seen_seconds !== null) status.textContent += ` · ${data.last_seen_seconds} seconds ago`;
                if (typeof L !== 'undefined') {
                    if (!map) {
                        map = L.map('buyer-location-map').setView(point, 14);
                        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {maxZoom: 19, attribution: '&copy; OpenStreetMap contributors'}).addTo(map);
                    }
                    if (marker) marker.setLatLng(point);
                    else marker = L.marker(point).addTo(map);
                } else status.textContent += ' · Map unavailable';
            }
            if (['DELIVERED', 'CANCELLED'].includes(data.order_status)) return;
        } catch (_) {
            status.textContent = 'Unable to refresh. Any marker shown is the last known location.';
        }
        timer = setTimeout(refresh, 15000);
    }
    document.addEventListener('visibilitychange', () => {
        clearTimeout(timer);
        if (!document.hidden) refresh();
    });
    window.addEventListener('pagehide', () => clearTimeout(timer));
    refresh();
})();
