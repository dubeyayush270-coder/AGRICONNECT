(() => {
    "use strict";
    const distance = (metres) => metres < 1000 ? `${Math.round(metres)} m` : `${(metres / 1000).toFixed(1)} km`;
    const duration = (seconds) => {
        const minutes = Math.max(1, Math.ceil(seconds / 60));
        return minutes < 60 ? `${minutes} min` : `${Math.floor(minutes / 60)} hr ${minutes % 60} min`;
    };
    function separation(a, b) {
        if (!a || !b) return Infinity;
        const lat = (a.latitude + b.latitude) * Math.PI / 360;
        return Math.hypot((a.latitude - b.latitude) * 111195,
            (a.longitude - b.longitude) * 111195 * Math.cos(lat));
    }
    const signature = (route) => route ? `${route.route_id}:${route.route_version}` : "none";
    if (typeof module !== "undefined") module.exports = {distance, duration, separation, signature};
    if (typeof document === "undefined") return;

    document.addEventListener("DOMContentLoaded", () => {
        const byId = (id) => document.getElementById(id);
        const panel = byId("route-optimization");
        if (!panel) return;
        let snapshot = null, roads = null, selected = 0, map = null, layers = null;
        let requestNumber = 0, activeController = null, loading = false, saving = false, destroyed = false, snapshotValid = false;
        let lastDirectionsAt = 0, lastDirectionsAttemptAt = 0, driverMarker = null;
        const status = (message, error = false) => {
            byId("routeMessage").textContent = message;
            byId("routeMessage").classList.toggle("route-message-error", error);
        };
        const element = (tag, className, text) => {
            const node = document.createElement(tag);
            if (className) node.className = className;
            if (text !== undefined) node.textContent = text;
            return node;
        };
        function setBusy(value) {
            loading = value;
            byId("refreshRoute").disabled = value || saving;
            byId("routeScope").disabled = value || saving;
            byId("completeRouteStop").disabled = value || saving || !snapshotValid || !snapshot?.route?.stops.length;
        }
        async function api(url, options = {}, signal) {
            const response = await fetch(url, {...options, signal, cache: "no-store"});
            let body;
            try { body = await response.json(); }
            catch { throw new Error("Unable to load route information. Please try again."); }
            if (!response.ok || !body.success) {
                const error = new Error(body.message || "Unable to load your route.");
                error.code = body.code;
                error.status = response.status;
                throw error;
            }
            return body;
        }
        function initMap() {
            if (map || typeof L === "undefined") return;
            map = L.map("routeMap").setView([23.2599, 77.4126], 12);
            L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
                maxZoom: 19, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a> · Directions: OSRM',
            }).addTo(map);
            layers = L.featureGroup().addTo(map);
        }
        function fitMap() {
            if (map && layers && layers.getLayers().length) {
                map.invalidateSize();
                map.fitBounds(layers.getBounds().pad(0.12), {maxZoom: 17});
            }
        }
        function marker(point, label, className, popupText) {
            const badge = element("span", `route-pin ${className}`, label);
            const icon = L.divIcon({html: badge, className: "route-map-marker", iconSize: [28, 28], iconAnchor: [14, 14]});
            return L.marker(point, {icon, title: popupText}).addTo(layers)
                .bindPopup(element("div", "", popupText));
        }
        function drawMap(fit = false) {
            initMap();
            if (!map) {
                byId("routeMap").textContent = "The map could not load. Stops and turn directions remain available below.";
                return;
            }
            layers.clearLayers();
            driverMarker = null;
            if (roads) {
                roads.routes.forEach((route, index) => {
                    if (index === selected) return;
                    L.geoJSON(route.geometry, {style: {color: "#91a7e8", weight: 5, opacity: 0.75}})
                        .addTo(layers).on("click", () => selectRoute(index));
                });
                const route = roads.routes[selected];
                L.geoJSON(route.geometry, {style: {color: "#ffffff", weight: 10, opacity: 0.95}}).addTo(layers);
                L.geoJSON(route.geometry, {style: {color: "#3454f5", weight: 6, opacity: 1}}).addTo(layers);
                const coords = route.geometry.coordinates;
                const stride = Math.max(1, Math.floor(coords.length / 12));
                for (let i = stride; i < coords.length - 1; i += stride) {
                    const a = coords[i], b = coords[i + 1];
                    if (a[0] === b[0] && a[1] === b[1]) continue;
                    const angle = Math.atan2(-(b[1] - a[1]), (b[0] - a[0]) * Math.cos(a[1] * Math.PI / 180)) * 180 / Math.PI;
                    const arrow = element("span", "route-arrow", "➤");
                    arrow.style.transform = `rotate(${angle}deg)`;
                    L.marker([a[1], a[0]], {interactive: false, keyboard: false, icon: L.divIcon({html: arrow,
                        className: "route-arrow-icon", iconSize: [18, 18], iconAnchor: [9, 9]})}).addTo(layers);
                }
            }
            const position = snapshot?.location_state.location;
            if (position) driverMarker = marker([position.latitude, position.longitude], "●", "route-pin-driver", "Vehicle's last saved position");
            for (const [index, stop] of (snapshot?.route?.stops || []).entries()) {
                marker([stop.latitude, stop.longitude], String(index + 1), stop.stop_type === "PICKUP" ? "" : "route-pin-delivery",
                    `${index + 1}. ${stop.stop_type === "PICKUP" ? "Pickup" : "Delivery"} · Order #${stop.order_id} · ${stop.address}`);
            }
            if (fit) fitMap();
        }
        function clearDirections() {
            roads = null;
            byId("routeDistance").textContent = "—";
            byId("routeDuration").textContent = "—";
            byId("routeAlternatives").replaceChildren();
            byId("routeInstructions").replaceChildren(element("p", "", "Refresh directions using a recent vehicle location."));
        }
        function selectRoute(index) {
            selected = index;
            const route = roads.routes[index];
            byId("routeDistance").textContent = distance(route.distance_m);
            byId("routeDuration").textContent = duration(route.duration_s);
            byId("routeAlternatives").replaceChildren(...roads.routes.map((option, i) => {
                const button = element("button", `route-option${i === index ? " selected" : ""}`);
                button.type = "button";
                button.setAttribute("aria-pressed", String(i === index));
                button.append(element("strong", "", duration(option.duration_s)),
                    element("span", "", `${distance(option.distance_m)} · ${i === 0 ? "Recommended" : "Alternative"}`));
                button.addEventListener("click", () => selectRoute(i));
                return button;
            }));
            const instructions = byId("routeInstructions");
            instructions.replaceChildren();
            route.legs.forEach((leg, index) => {
                const stop = snapshot.route.stops.find((s) => s.stop_id === roads.stop_ids[index]);
                instructions.append(element("h4", "", `To ${stop?.stop_type === "PICKUP" ? "pickup" : "delivery"} · Order #${stop?.order_id}`));
                const list = element("ol", "route-turns");
                for (const step of leg.steps) {
                    const row = element("li", "");
                    row.append(element("span", "", step.instruction), element("small", "", distance(step.distance_m)));
                    list.append(row);
                }
                instructions.append(list);
            });
            drawMap();
        }
        function renderSnapshot() {
            const route = snapshot.route;
            byId("routeEmpty").hidden = Boolean(route);
            byId("routeContent").hidden = !route;
            if (!route) {
                clearDirections();
                if (layers) layers.clearLayers();
                status(snapshot.message);
                return;
            }
            byId("routeStopCount").textContent = String(route.stops.length);
            byId("routeLoad").textContent = `${route.current_load_kg} / ${route.vehicle_capacity_kg} kg`;
            byId("routeProgress").textContent = `${route.completed_stops} completed`;
            byId("routeScope").querySelector('option[value="all"]').disabled = route.stops.length > 24;
            if (route.stops.length > 24) byId("routeScope").value = "next";
            byId("routeStops").replaceChildren(...route.stops.map((stop, index) => {
                const item = element("li", index === 0 ? "route-stop next" : "route-stop");
                const badge = element("span", `route-stop-number${stop.stop_type === "DELIVERY" ? " delivery" : ""}`, String(index + 1));
                const copy = element("div", "route-stop-copy");
                copy.append(element("strong", "", `${stop.stop_type === "PICKUP" ? "Pickup" : "Delivery"} · Order #${stop.order_id}`),
                    element("p", "", stop.address), element("small", "", `${stop.quantity_kg} kg · ${stop.load_after_kg} kg aboard after stop`));
                if (index === 0) copy.prepend(element("span", "route-next-label", "NEXT STOP"));
                item.append(badge, copy);
                return item;
            }));
            const next = route.stops[0];
            byId("completeRouteStop").textContent = next?.next_status === "PICKED_UP" ? "Mark next pickup collected" :
                next?.next_status === "IN_TRANSIT" ? "Start delivery to next stop" : "Mark next delivery completed";
            drawMap(!roads);
        }
        async function refresh(force = false) {
            if (saving || destroyed || document.hidden || loading && !force) return;
            const id = ++requestNumber;
            activeController?.abort();
            const controller = new AbortController();
            activeController = controller;
            const signal = controller.signal;
            const timeout = setTimeout(() => controller.abort(), 15000);
            snapshotValid = false;
            setBusy(true);
            try {
                const data = await api(panel.dataset.routeUrl, {}, signal);
                if (id !== requestNumber || destroyed) return;
                const changed = signature(snapshot?.route) !== signature(data.route);
                snapshot = data;
                snapshotValid = true;
                if (changed) clearDirections();
                renderSnapshot();
                if (!data.route) return;
                const state = data.location_state;
                if (!state.location || state.last_seen_seconds === null || state.last_seen_seconds > state.stale_after_seconds) {
                    clearDirections();
                    drawMap();
                    status("Update GPS above to get road directions from your current location.");
                    return;
                }
                const scope = byId("routeScope").value;
                const moved = separation(roads?.origin, state.location) >= 100;
                if (!force && roads && !changed && roads.scope === scope && !moved) return;
                if (!force && roads && !changed && Date.now() - lastDirectionsAt < 30000) return;
                clearDirections();
                drawMap();
                status("Finding a driving route through the road network…");
                lastDirectionsAttemptAt = Date.now();
                const query = new URLSearchParams({route_id: data.route.route_id, route_version: data.route.route_version, scope});
                const result = await api(`${panel.dataset.directionsUrl}?${query}`, {}, signal);
                if (id !== requestNumber || destroyed) return;
                roads = result;
                selected = 0;
                lastDirectionsAt = Date.now();
                selectRoute(0);
                fitMap();
                status(`Driving directions ready · ${scope === "next" ? "next stop" : "all remaining stops"} · pickup and delivery order preserved.`);
            } catch (error) {
                if (id !== requestNumber || destroyed) return;
                clearDirections();
                if (error.code === "ROUTE_CHANGED" || error.code === "INVALID_ROUTE") snapshotValid = false;
                if (error.status === 401 || error.status === 403) {
                    snapshot = null;
                    byId("routeContent").hidden = true;
                    byId("routeEmpty").hidden = true;
                    if (layers) layers.clearLayers();
                } else drawMap();
                status(error.name === "AbortError" ? "The route request timed out. Please refresh to try again." : error.message, true);
            } finally {
                clearTimeout(timeout);
                if (id === requestNumber) setBusy(false);
            }
        }
        byId("refreshRoute").addEventListener("click", () => refresh(true));
        byId("routeScope").addEventListener("change", () => refresh(true));
        byId("fitRoute").addEventListener("click", fitMap);
        byId("routeUpdateLocation").addEventListener("click", async () => {
            byId("routeUpdateLocation").disabled = true;
            try { if (window.getCurrentLocation) await window.getCurrentLocation(); }
            finally { byId("routeUpdateLocation").disabled = false; await refresh(true); }
        });
        byId("completeRouteStop").addEventListener("click", async () => {
            if (saving || loading || !snapshotValid || !snapshot?.route?.stops.length) return;
            const next = snapshot.route.stops[0];
            saving = true;
            setBusy(true);
            activeController?.abort();
            ++requestNumber;
            const controller = new AbortController();
            const timeout = setTimeout(() => controller.abort(), 12000);
            let failure = null;
            try {
                status("Saving delivery progress…");
                await api(panel.dataset.statusUrl.replace("/0/status", `/${next.order_id}/status`), {
                    method: "POST", headers: {"Content-Type": "application/json", "X-CSRF-Token": snapshot.csrf_token},
                    body: JSON.stringify({status: next.next_status}),
                }, controller.signal);
                clearDirections();
            } catch (error) { failure = error; }
            finally {
                clearTimeout(timeout);
                saving = false;
                setBusy(false);
                await refresh(true);
                if (failure) status(failure.name === "AbortError" ? "Could not confirm the update. The route was refreshed; check the next stop before retrying." : failure.message, true);
            }
        });
        window.addEventListener("logistics:location", (event) => {
            const state = event.detail;
            if (driverMarker && state?.location) driverMarker.setLatLng([state.location.latitude, state.location.longitude]);
            if (state?.location && separation(roads?.origin, state.location) >= 100 && Date.now() - lastDirectionsAttemptAt >= 30000) refresh();
        });
        const timer = setInterval(() => refresh(), 30000);
        window.addEventListener("pagehide", () => { destroyed = true; ++requestNumber; clearInterval(timer); activeController?.abort(); });
        refresh();
    });
})();
