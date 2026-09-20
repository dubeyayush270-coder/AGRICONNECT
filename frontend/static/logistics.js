(() => {
    "use strict";

    const UPDATE_INTERVAL = 10000;
    const MAX_FIX_AGE = 30000;
    const byId = (id) => document.getElementById(id);
    let snapshot;
    let snapshotAt = 0;
    let revision = 0;
    let map = null;
    let marker = null;
    let accuracyCircle = null;
    let watchId = null;
    let heartbeat = null;
    let generation = 0;
    let saving = false;
    let locating = false;
    let changing = false;
    let polling = false;
    let lastSavedAt = 0;
    let lastFixTimestamp = 0;
    let trackingIssue = false;
    let pollTimer = null;
    let statusTimer = null;

    function message(text) {
        byId("locationStatus").textContent = text;
    }

    async function api(url, data) {
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 10000);
        try {
            const options = {signal: controller.signal, cache: "no-store"};
            if (data !== undefined) {
                options.method = "POST";
                options.headers = {"Content-Type": "application/json"};
                options.body = JSON.stringify(data);
            }
            const response = await fetch(url, options);
            const body = await response.json();
            if (!response.ok || !body.success) {
                if (response.status === 401 || response.status === 403) {
                    stopLocationTracking();
                    message(body.message || "Please sign in again to continue tracking.");
                }
                throw new Error(body.message || "Unable to update tracking. Please try again.");
            }
            return body;
        } catch (error) {
            if (error.name === "AbortError") {
                throw new Error("Connection timed out. Waiting to reconnect.");
            }
            throw error;
        } finally {
            clearTimeout(timeout);
        }
    }

    function showMarker(latitude, longitude, accuracy) {
        byId("currentLatitude").textContent = latitude.toFixed(7);
        byId("currentLongitude").textContent = longitude.toFixed(7);
        if (!map) return;
        const point = [latitude, longitude];
        if (marker) marker.setLatLng(point);
        else marker = L.marker(point).addTo(map).bindPopup("Driver's last saved location");
        if (Number.isFinite(accuracy) && accuracy >= 0) {
            if (!accuracyCircle) {
                accuracyCircle = L.circle(point, {radius: accuracy, color: "#2e7d32", fillOpacity: 0.12}).addTo(map);
            } else {
                accuracyCircle.setLatLng(point).setRadius(accuracy);
            }
        }
        if (byId("followVehicle").checked) map.setView(point, Math.max(map.getZoom(), 15));
    }

    function showFreshness() {
        if (!snapshot) return;
        const age = snapshot.last_seen_seconds === null ? null :
            snapshot.last_seen_seconds + Math.max(0, Math.floor((Date.now() - snapshotAt) / 1000));
        const fresh = age !== null && age <= snapshot.stale_after_seconds;
        const online = snapshot.availability === "ONLINE";
        const live = fresh && online && watchId !== null && !trackingIssue && lastSavedAt > 0;
        const elapsed = age < 60 ? age : Math.floor(age / 60);
        const unit = age < 60 ? "second" : "minute";
        byId("locationUpdatedAt").textContent = age === null ? "Last update: not available" :
            `Last update: ${elapsed} ${unit}${elapsed === 1 ? "" : "s"} ago`;
        byId("trackingStatus").textContent = !snapshot.location ? "No saved location" :
            !online ? "Offline — last saved location" :
            !fresh ? "Location stale — waiting for a fresh GPS update" :
            trackingIssue ? "Tracking interrupted — showing last saved location" :
            watchId === null ? "Tracking paused — showing last saved location" :
            !lastSavedAt ? "Connecting — waiting for a fresh device location" :
            "Live — receiving recent driver locations";
        byId("trackingStatus").style.color = live ? "#2e7d32" : "#76520b";
    }

    function applySnapshot(data, accuracy) {
        snapshot = data;
        snapshotAt = Date.now();
        revision += 1;
        const online = data.availability === "ONLINE";
        byId("availabilityToggle").checked = online;
        byId("availabilityStatus").textContent = online ? "🟢 ONLINE" : "⚪ OFFLINE";
        byId("availabilityStatus").className = `availability-status ${online ? "online" : "offline"}`;
        byId("trackingButton").textContent = online ? "Stop Live Tracking" : "Start Live Tracking";
        if (data.location) showMarker(data.location.latitude, data.location.longitude, accuracy);
        showFreshness();
    }

    function requireGPS() {
        if (!window.isSecureContext) throw new Error("Location access needs HTTPS or localhost.");
        if (!navigator.geolocation) throw new Error("This browser does not support GPS location.");
    }

    async function currentPosition(token) {
        requireGPS();
        const requestPosition = (enableHighAccuracy) => new Promise((resolve, reject) =>
            navigator.geolocation.getCurrentPosition(resolve, reject, {
                enableHighAccuracy, timeout: 15000, maximumAge: 0,
            }));
        try {
            return await requestPosition(true);
        } catch (error) {
            // Standard device positioning can work when a precise GPS fix is unavailable.
            // Never retry after permission denial or after this tracking session was stopped.
            if (token !== generation || (error.code !== 2 && error.code !== 3)) throw error;
            return requestPosition(false);
        }
    }

    function gpsMessage(error) {
        if (error.code === 1) return "Location permission denied. Allow location access to track the vehicle.";
        if (error.code === 2) return "Your device could not determine a location. Check that location services are enabled.";
        if (error.code === 3) return "Location request timed out. Check your device's location signal.";
        return error.message || "Unable to update location. Check your connection.";
    }

    async function savePosition(position, token) {
        if (saving || token !== generation) return false;
        const {latitude, longitude, accuracy} = position.coords;
        const fixAge = Date.now() - position.timestamp;
        if (!Number.isFinite(latitude) || !Number.isFinite(longitude) ||
            Math.abs(latitude) > 90 || Math.abs(longitude) > 180 ||
            !Number.isFinite(fixAge) || fixAge > MAX_FIX_AGE || fixAge < -5000) {
            throw new Error("Waiting for a fresh, valid GPS location.");
        }
        // Delayed watcher callbacks must not move the driver back to an older position.
        if (position.timestamp <= lastFixTimestamp) return false;
        saving = true;
        try {
            const data = await api("/logistics/update-location", {latitude, longitude});
            if (token !== generation) return false;
            lastSavedAt = Date.now();
            lastFixTimestamp = position.timestamp;
            trackingIssue = false;
            applySnapshot(data, accuracy);
            byId("locationAccuracy").textContent = Number.isFinite(accuracy) ?
                `GPS accuracy: approximately ${Math.round(accuracy)} metres` : "GPS accuracy: not available";
            message("Driver location saved.");
            return true;
        } finally {
            saving = false;
        }
    }

    function stopLocationTracking() {
        generation += 1;
        if (watchId !== null) navigator.geolocation.clearWatch(watchId);
        if (heartbeat !== null) clearInterval(heartbeat);
        watchId = null;
        heartbeat = null;
        locating = false;
        trackingIssue = false;
        showFreshness();
    }

    async function trackingError(error, token) {
        if (token !== generation) return;
        trackingIssue = true;
        if (error.code === 1) {
            stopLocationTracking();
            const stoppedToken = generation;
            try {
                const state = await api("/logistics/toggle-availability", {availability: "OFFLINE"});
                if (stoppedToken !== generation) return;
                applySnapshot(state);
            }
            catch (_) { /* Freshness still expires if the server is unreachable. */ }
        }
        message(gpsMessage(error) + (watchId !== null ? " Retrying automatically." : ""));
        showFreshness();
    }

    function startLocationTracking() {
        if (watchId !== null) return;
        requireGPS();
        const token = generation;
        const receive = async (position) => {
            if (token !== generation || saving || Date.now() - lastSavedAt < UPDATE_INTERVAL) return;
            try { await savePosition(position, token); }
            catch (error) { await trackingError(error, token); }
        };
        watchId = navigator.geolocation.watchPosition(receive,
            (error) => {
                // The watcher may time out while the independent heartbeat has just succeeded.
                if (error.code === 3 && Date.now() - lastSavedAt < UPDATE_INTERVAL) return;
                return trackingError(error, token);
            },
            {enableHighAccuracy: true, timeout: 15000, maximumAge: 5000});
        // Ask for a fresh fix even while stationary; never re-stamp an old fix as live.
        const updateFromGPS = async () => {
            if (token !== generation || locating || saving || Date.now() - lastSavedAt < UPDATE_INTERVAL) return;
            locating = true;
            const before = lastSavedAt;
            try { await receive(await currentPosition(token)); }
            catch (error) {
                if (error.code === 1 || lastSavedAt === before) await trackingError(error, token);
            }
            finally { if (token === generation) locating = false; }
        };
        heartbeat = setInterval(updateFromGPS, UPDATE_INTERVAL);
        message("Tracking started. Keep this page open on the driver's device.");
        showFreshness();
        updateFromGPS();
    }

    function setBusy(busy) {
        byId("availabilityToggle").disabled = busy;
        byId("trackingButton").disabled = busy;
        byId("updateLocationButton").disabled = busy;
    }

    async function changeAvailability() {
        const online = byId("availabilityToggle").checked;
        if (changing || (online && (saving || locating))) {
            byId("availabilityToggle").checked = snapshot.availability === "ONLINE";
            message("A location update is finishing. Please try again shortly.");
            return;
        }
        changing = true;
        // Display only the availability confirmed by the server while GPS/save is pending.
        byId("availabilityToggle").checked = snapshot.availability === "ONLINE";
        setBusy(true);
        try {
            if (online) {
                if (!snapshot.profile_exists) throw new Error("Save vehicle information before starting tracking.");
                message("Getting a fresh GPS location before going online...");
                const token = generation;
                const position = await currentPosition(token);
                if (!await savePosition(position, token)) throw new Error("Tracking was stopped. Please try again.");
                const state = await api("/logistics/toggle-availability", {availability: "ONLINE"});
                if (token !== generation) return;
                applySnapshot(state);
                startLocationTracking();
            } else {
                stopLocationTracking();
                applySnapshot(await api("/logistics/toggle-availability", {availability: "OFFLINE"}));
                message("Live tracking stopped. Your last saved location remains on the map.");
            }
        } catch (error) {
            byId("availabilityToggle").checked = snapshot.availability === "ONLINE";
            message(gpsMessage(error));
        } finally {
            changing = false;
            setBusy(false);
        }
    }

    async function refreshLocation() {
        if (polling || changing || saving) return;
        polling = true;
        const before = revision;
        const token = generation;
        try {
            const data = await api("/logistics/location");
            if (before !== revision || changing || saving) return;
            applySnapshot(data);
            if (data.availability !== "ONLINE" && watchId !== null) {
                stopLocationTracking();
                message("Tracking stopped because this vehicle is offline.");
            }
        } catch (error) {
            if (before === revision && !changing && !saving) await trackingError(error, token);
        }
        finally { polling = false; }
    }

    window.changeAvailability = changeAvailability;
    window.toggleLiveTracking = () => {
        byId("availabilityToggle").checked = snapshot.availability !== "ONLINE";
        return changeAvailability();
    };
    window.getCurrentLocation = async () => {
        if (changing || saving || locating) return;
        locating = true;
        byId("updateLocationButton").disabled = true;
        const token = generation;
        try {
            message("Getting the driver's current location...");
            await savePosition(await currentPosition(token), token);
        } catch (error) { await trackingError(error, token); }
        finally {
            locating = false;
            byId("updateLocationButton").disabled = changing;
        }
    };
    window.editVehicleInfo = () => {
        byId("vehicle-information").scrollIntoView({behavior: "smooth", block: "center"});
        byId("vehicleNumber").focus({preventScroll: true});
    };

    window.saveVehicleInfo = async (event) => {
        event.preventDefault();
        const output = byId("vehicleMessage");
        output.textContent = "Saving...";
        try {
            const submitted = new FormData(byId("vehicleForm"));
            const response = await fetch("/logistics/update-profile", {method: "POST", body: submitted});
            const data = await response.json();
            if (!response.ok || !data?.success) throw new Error(data?.message || "Unable to save vehicle information.");
            // Older running servers acknowledge the save without returning vehicle details.
            const vehicle = data.vehicle || {
                vehicle_number: submitted.get("vehicle_number").trim(),
                vehicle_type: submitted.get("vehicle_type").trim(),
                vehicle_capacity: Number(submitted.get("vehicle_capacity")),
            };
            for (const [id, value] of [
                ["summaryVehicleNumber", vehicle.vehicle_number],
                ["summaryVehicleType", vehicle.vehicle_type],
                ["summaryVehicleCapacity", `${vehicle.vehicle_capacity} kg`],
            ]) {
                const field = byId(id);
                // A previously loaded dashboard may not have the summary IDs yet.
                if (field) field.textContent = value;
            }
            output.textContent = data.message || "Vehicle information saved successfully.";
            output.style.color = "#2e7d32";
            await refreshLocation();
        } catch (error) {
            output.textContent = gpsMessage(error);
            output.style.color = "#d32f2f";
        }
    };

    document.addEventListener("DOMContentLoaded", () => {
        snapshot = JSON.parse(byId("logisticsInitialState").textContent);
        if (typeof L !== "undefined") {
            map = L.map("map").setView([22.7196, 75.8577], 10);
            L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
                maxZoom: 19, attribution: "&copy; OpenStreetMap contributors",
            }).addTo(map);
        } else {
            byId("map").textContent = "Map could not load. Coordinates and tracking are still available.";
        }
        applySnapshot(snapshot);
        setBusy(false);
        message(snapshot.location ? "Showing the driver's last saved location." : "Save vehicle information, then start live tracking.");
        refreshLocation();
        pollTimer = setInterval(refreshLocation, UPDATE_INTERVAL);
        statusTimer = setInterval(showFreshness, 1000);
        if (snapshot.availability === "ONLINE") {
            // Resume the watcher and retry loop even if the first fresh fix times out.
            try { startLocationTracking(); }
            catch (error) { message(gpsMessage(error)); showFreshness(); }
        }
    });
    window.addEventListener("pagehide", () => {
        stopLocationTracking();
        clearInterval(pollTimer);
        clearInterval(statusTimer);
    });
    window.addEventListener("pageshow", (event) => {
        // A restored back/forward page needs a new GPS request and timers.
        if (event.persisted) window.location.reload();
    });
})();
