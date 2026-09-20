// Run with: node --test tests/logistics.test.js
// GPS, network, map and timers are simulated; no real location is requested.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../static/logistics.js"), "utf8");

const flush = async () => { for (let i = 0; i < 30; i++) await Promise.resolve(); };

async function browser(options = {}) {
    let now = 1700000000000;
    let timerId = 0;
    let availability = options.availability || "OFFLINE";
    let savedAt = availability === "ONLINE" ? now : null;
    let location = savedAt === null ? null : {latitude: 0, longitude: 0};
    const nodes = new Map();
    const intervals = new Map();
    const watches = new Map();
    const events = {};
    const calls = [];
    const mapPoints = [];
    const env = {nodes, intervals, watches, events, calls, mapPoints, failLocation: false,
        geoError: options.geoError || null, fixAge: 0, geoRequests: []};
    function state() {
        const age = savedAt === null ? null : Math.floor((now - savedAt) / 1000);
        return {success: true, profile_exists: true, availability, location,
            last_seen_seconds: age, stale_after_seconds: 60,
            is_live: availability === "ONLINE" && age !== null && age <= 60};
    }
    function node(id) {
        if (!nodes.has(id)) nodes.set(id, {textContent: "", style: {}, checked: id === "followVehicle", disabled: false});
        return nodes.get(id);
    }
    node("logisticsInitialState").textContent = JSON.stringify(state());
    node("followVehicle");
    node("vehicleNumber").value = "TEST 123";
    node("vehicleType").value = "Pickup";
    node("vehicleCapacity").value = "1000";
    class VehicleFormData {
        constructor() {
            this.values = {
                vehicle_number: node("vehicleNumber").value,
                vehicle_type: node("vehicleType").value,
                vehicle_capacity: node("vehicleCapacity").value,
            };
        }
        get(name) { return this.values[name]; }
    }
    env.position = () => ({coords: {latitude: 0, longitude: 0, accuracy: 12}, timestamp: now - env.fixAge});
    env.advance = (ms) => { now += ms; };
    const window = {isSecureContext: true, addEventListener: (name, fn) => { events[name] = fn; }, location: {reload() {}}};
    const document = {getElementById: node, addEventListener: (name, fn) => { events[name] = fn; }};
    const navigator = {geolocation: {
        getCurrentPosition(resolve, reject, options) {
            env.geoRequests.push(options);
            if (env.delayGPS) { env.resolveGPS = resolve; env.rejectGPS = reject; return; }
            const error = env.geoError || (options.enableHighAccuracy && env.highAccuracyError);
            error ? reject(error) : resolve(env.position());
        },
        watchPosition(receive, reject) { const id = ++timerId; watches.set(id, {receive, reject}); return id; },
        clearWatch(id) { watches.delete(id); },
    }};
    const leafletMap = {setView(point) { mapPoints.push(point); return this; }, getZoom() {return 15;}};
    const leafletLayer = () => ({addTo() {return this;}, bindPopup() {return this;}, setLatLng() {return this;}, setRadius() {return this;}});
    const context = {
        window, document, navigator, console, AbortController, FormData: VehicleFormData,
        Date: {now: () => now},
        L: {map: () => leafletMap, marker: leafletLayer, circle: leafletLayer, tileLayer: leafletLayer},
        setTimeout: () => ++timerId, clearTimeout() {},
        setInterval(fn, delay) {const id = ++timerId; intervals.set(id, {fn, delay}); return id;},
        clearInterval(id) {intervals.delete(id);},
        async fetch(url, options = {}) {
            const data = options.body instanceof VehicleFormData ? options.body.values :
                options.body ? JSON.parse(options.body) : null;
            calls.push({url, data});
            if (url.endsWith("update-profile")) {
                if (env.delayProfile) await new Promise(resolve => { env.releaseProfile = resolve; });
                const status = env.profileStatus || 200;
                return {ok: status === 200, status, json: async () => env.profileResponse};
            }
            if (url === "/logistics/location") {
                if (env.delayPoll) await new Promise(resolve => { env.releasePoll = resolve; });
                if (env.failPoll) return {ok: false, status: 503, json: async () => ({success: false, message: "Connection unavailable"})};
            }
            if (url.endsWith("update-location")) {
                if (env.failLocation) return {ok: false, status: 503, json: async () => ({success: false, message: "Connection unavailable"})};
                if (env.delayLocation) await new Promise(resolve => { env.releaseLocation = resolve; });
                location = {latitude: data.latitude, longitude: data.longitude}; savedAt = now;
            }
            if (url.endsWith("toggle-availability")) {
                if (env.delayAvailability) await new Promise(resolve => { env.releaseAvailability = resolve; });
                availability = data.availability;
            }
            return {ok: true, status: 200, json: async () => state()};
        },
    };
    vm.runInNewContext(source, context, {filename: "logistics.js"});
    events.DOMContentLoaded();
    await flush();
    env.window = window;
    env.tick = async (delay) => {
        for (const timer of [...intervals.values()].filter(t => t.delay === delay)) { timer.fn(); await flush(); }
    };
    return env;
}

test("saving vehicle info displays the normalized details returned by the server", async () => {
    const env = await browser();
    env.profileResponse = {success: true, message: "Vehicle information saved successfully.",
        vehicle: {vehicle_number: "SERVER 456", vehicle_type: "Truck", vehicle_capacity: 2000}};
    await env.window.saveVehicleInfo({preventDefault() {}});
    assert.equal(env.nodes.get("summaryVehicleNumber").textContent, "SERVER 456");
    assert.equal(env.nodes.get("summaryVehicleType").textContent, "Truck");
    assert.equal(env.nodes.get("summaryVehicleCapacity").textContent, "2000 kg");
    assert.equal(env.nodes.get("vehicleMessage").textContent, env.profileResponse.message);
});

test("an older successful save response without vehicle details still updates the summary", async () => {
    const env = await browser();
    env.profileResponse = {success: true, message: "Vehicle information saved successfully."};
    env.nodes.get("vehicleNumber").value = "  MP 04 AB 1234  ";
    await env.window.saveVehicleInfo({preventDefault() {}});
    assert.equal(env.nodes.get("summaryVehicleNumber").textContent, "MP 04 AB 1234");
    assert.equal(env.nodes.get("summaryVehicleType").textContent, "Pickup");
    assert.equal(env.nodes.get("summaryVehicleCapacity").textContent, "1000 kg");
    assert.equal(env.nodes.get("vehicleMessage").textContent, env.profileResponse.message);
    assert.equal(env.nodes.get("vehicleMessage").style.color, "#2e7d32");
});

test("the older response uses submitted values, not edits made while the save was pending", async () => {
    const env = await browser();
    env.profileResponse = {success: true};
    env.delayProfile = true;
    const saving = env.window.saveVehicleInfo({preventDefault() {}});
    env.nodes.get("vehicleNumber").value = "UNSAVED 789";
    env.releaseProfile();
    await saving;
    assert.equal(env.nodes.get("summaryVehicleNumber").textContent, "TEST 123");
    assert.equal(env.nodes.get("vehicleNumber").value, "UNSAVED 789");
    assert.equal(env.nodes.get("vehicleMessage").textContent, "Vehicle information saved successfully.");
});

test("a failed vehicle save preserves the existing summary and shows the server error", async () => {
    const env = await browser();
    env.nodes.set("summaryVehicleNumber", {textContent: "SAVED 123"});
    env.nodes.set("summaryVehicleType", {textContent: "Truck"});
    env.nodes.set("summaryVehicleCapacity", {textContent: "2000 kg"});
    env.profileStatus = 503;
    env.profileResponse = {success: false, message: "Unable to save vehicle information."};
    await env.window.saveVehicleInfo({preventDefault() {}});
    assert.equal(env.nodes.get("summaryVehicleNumber").textContent, "SAVED 123");
    assert.equal(env.nodes.get("summaryVehicleType").textContent, "Truck");
    assert.equal(env.nodes.get("summaryVehicleCapacity").textContent, "2000 kg");
    assert.equal(env.nodes.get("vehicleMessage").textContent, env.profileResponse.message);
    assert.equal(env.nodes.get("vehicleMessage").style.color, "#d32f2f");
});

test("an older dashboard without summary IDs does not report a successful save as an error", async () => {
    const env = await browser();
    env.profileResponse = {success: true};
    for (const id of ["summaryVehicleNumber", "summaryVehicleType", "summaryVehicleCapacity"]) {
        env.nodes.set(id, null);
    }
    await env.window.saveVehicleInfo({preventDefault() {}});
    assert.equal(env.nodes.get("vehicleMessage").textContent, "Vehicle information saved successfully.");
    assert.equal(env.nodes.get("vehicleMessage").style.color, "#2e7d32");
});

test("GPS is saved before going online, then the map and watch start", async () => {
    const env = await browser();
    await env.window.toggleLiveTracking();
    const writes = env.calls.filter(call => call.data);
    assert.equal(writes[0].url, "/logistics/update-location");
    assert.equal(writes[1].url, "/logistics/toggle-availability");
    assert.equal(writes[1].data.availability, "ONLINE");
    assert.equal(env.watches.size, 1);
    assert.equal(env.nodes.get("currentLatitude").textContent, "0.0000000");
    assert.match(env.nodes.get("trackingStatus").textContent, /^Live/);
    assert.match(env.nodes.get("locationAccuracy").textContent, /12 metres/);
});

test("a failed GPS save never sets ONLINE or starts tracking", async () => {
    const env = await browser(); env.failLocation = true;
    await env.window.toggleLiveTracking();
    assert.equal(env.calls.filter(call => call.url.endsWith("toggle-availability")).length, 0);
    assert.equal(env.watches.size, 0);
    assert.equal(env.nodes.get("availabilityToggle").checked, false);
    assert.match(env.nodes.get("locationStatus").textContent, /Connection unavailable/);
});

test("permission denial leaves the driver offline with a clear explanation", async () => {
    const env = await browser(); env.geoError = {code: 1};
    await env.window.toggleLiveTracking();
    assert.equal(env.calls.filter(call => call.data).length, 0);
    assert.equal(env.watches.size, 0);
    assert.match(env.nodes.get("locationStatus").textContent, /permission denied/);
});

test("going offline cancels tracking and ignores a queued GPS callback", async () => {
    const env = await browser();
    await env.window.toggleLiveTracking();
    const receive = [...env.watches.values()][0].receive;
    await env.window.toggleLiveTracking();
    const before = env.calls.length;
    env.advance(10000); receive(env.position()); await flush();
    assert.equal(env.watches.size, 0);
    assert.equal(env.calls.length, before);
    assert.equal(env.nodes.get("availabilityToggle").checked, false);
});

test("GPS writes are throttled while the map keeps following saved updates", async () => {
    const env = await browser(); await env.window.toggleLiveTracking();
    const receive = [...env.watches.values()][0].receive;
    env.advance(1000); receive(env.position()); await flush();
    env.advance(1000); receive(env.position()); await flush();
    assert.equal(env.calls.filter(call => call.url.endsWith("update-location")).length, 1);
    env.advance(8000); receive(env.position()); await flush();
    assert.equal(env.calls.filter(call => call.url.endsWith("update-location")).length, 2);
});

test("stationary vehicles obtain a fresh fix for each heartbeat", async () => {
    const env = await browser(); await env.window.toggleLiveTracking();
    env.advance(10000); await env.tick(10000);
    assert.equal(env.calls.filter(call => call.url.endsWith("update-location")).length, 2);
    assert.match(env.nodes.get("locationUpdatedAt").textContent, /0 seconds/);
});

test("stale GPS fixes are not resent as live coordinates", async () => {
    const env = await browser(); env.fixAge = 31000;
    await env.window.toggleLiveTracking();
    assert.equal(env.calls.filter(call => call.data).length, 0);
    assert.match(env.nodes.get("locationStatus").textContent, /fresh, valid/);
});

test("failed updates visibly become stale after a minute", async () => {
    const env = await browser(); await env.window.toggleLiveTracking();
    env.failLocation = true;
    env.advance(61000); await env.tick(1000);
    assert.match(env.nodes.get("trackingStatus").textContent, /stale/);
});

test("turning off follow keeps the user's map position", async () => {
    const env = await browser();
    env.nodes.get("followVehicle").checked = false;
    const before = env.mapPoints.length;
    await env.window.getCurrentLocation();
    assert.equal(env.mapPoints.length, before);
    assert.equal(env.nodes.get("currentLongitude").textContent, "0.0000000");
});

test("closing the page stops GPS and all recurring timers", async () => {
    const env = await browser(); await env.window.toggleLiveTracking();
    env.events.pagehide();
    assert.equal(env.watches.size, 0);
    assert.equal(env.intervals.size, 0);
});

test("the driver can stop immediately while a location save is in flight", async () => {
    const env = await browser(); await env.window.toggleLiveTracking();
    env.delayLocation = true;
    env.advance(10000);
    [...env.watches.values()][0].receive(env.position());
    await flush();
    assert.equal(typeof env.releaseLocation, "function");
    await env.window.toggleLiveTracking();
    assert.equal(env.watches.size, 0);
    assert.equal(env.nodes.get("availabilityToggle").checked, false);
    env.releaseLocation(); await flush();
    assert.equal(env.nodes.get("availabilityToggle").checked, false);
});

test("closing the page during start never creates a late GPS watcher", async () => {
    const env = await browser(); env.delayAvailability = true;
    const starting = env.window.toggleLiveTracking();
    await flush();
    assert.equal(typeof env.releaseAvailability, "function");
    env.events.pagehide();
    env.releaseAvailability(); await starting;
    assert.equal(env.watches.size, 0);
    assert.equal(env.intervals.size, 0);
});

test("a high accuracy timeout retries with standard device location and no cached fix", async () => {
    const env = await browser();
    env.highAccuracyError = {code: 3};
    await env.window.toggleLiveTracking();
    assert.deepEqual(env.geoRequests.map(options => options.enableHighAccuracy), [true, false]);
    assert.ok(env.geoRequests.every(options => options.maximumAge === 0));
    assert.equal(env.watches.size, 1);
    assert.match(env.nodes.get("trackingStatus").textContent, /^Live/);
});

test("reloading online keeps retrying after GPS times out and recovers on a fresh fix", async () => {
    const env = await browser({availability: "ONLINE", geoError: {code: 3}});
    assert.equal(env.watches.size, 1);
    assert.match(env.nodes.get("locationStatus").textContent, /Retrying automatically/);
    assert.doesNotMatch(env.nodes.get("trackingStatus").textContent, /^Live/);
    assert.equal(env.nodes.get("currentLatitude").textContent, "0.0000000");
    env.geoError = null;
    env.advance(10000); await env.tick(10000);
    assert.match(env.nodes.get("trackingStatus").textContent, /^Live/);
    assert.match(env.nodes.get("locationStatus").textContent, /location saved/);
});

test("a GPS interruption stays visible across polling until a new fix is saved", async () => {
    const env = await browser(); await env.window.toggleLiveTracking();
    env.advance(20000);
    await [...env.watches.values()][0].reject({code: 3});
    assert.match(env.nodes.get("trackingStatus").textContent, /interrupted/);
    env.geoError = {code: 3};
    await env.tick(10000);
    assert.doesNotMatch(env.nodes.get("trackingStatus").textContent, /^Live/);
    env.geoError = null;
    env.advance(10000); await env.tick(10000);
    assert.match(env.nodes.get("trackingStatus").textContent, /^Live/);
});

test("a watcher timeout cannot replace a location just saved by the heartbeat", async () => {
    const env = await browser(); await env.window.toggleLiveTracking();
    env.advance(10000); await env.tick(10000);
    await [...env.watches.values()][0].reject({code: 3});
    assert.match(env.nodes.get("locationStatus").textContent, /location saved/);
    assert.match(env.nodes.get("trackingStatus").textContent, /^Live/);
});

test("stopping during a GPS request prevents fallback requests and later writes", async () => {
    const env = await browser(); await env.window.toggleLiveTracking();
    env.delayGPS = true;
    env.advance(10000); await env.tick(10000);
    assert.equal(typeof env.rejectGPS, "function");
    await env.window.toggleLiveTracking();
    const before = env.geoRequests.length;
    env.rejectGPS({code: 3}); await flush();
    assert.equal(env.geoRequests.length, before);
    assert.equal(env.watches.size, 0);
    assert.match(env.nodes.get("trackingStatus").textContent, /^Offline/);
});

test("permission denial never attempts a fallback and stops active tracking", async () => {
    const env = await browser(); await env.window.toggleLiveTracking();
    const before = env.geoRequests.length;
    await [...env.watches.values()][0].reject({code: 1});
    assert.equal(env.geoRequests.length, before);
    assert.equal(env.watches.size, 0);
    assert.equal(env.nodes.get("availabilityToggle").checked, false);
    assert.match(env.nodes.get("locationStatus").textContent, /permission denied/);
    assert.match(env.nodes.get("trackingStatus").textContent, /^Offline/);
});

test("an out of order GPS callback never overwrites the driver's newer position", async () => {
    const env = await browser(); await env.window.toggleLiveTracking();
    const receive = [...env.watches.values()][0].receive;
    env.advance(10000);
    const newer = env.position();
    newer.coords.latitude = 1;
    await receive(newer);
    const before = env.calls.filter(call => call.url.endsWith("update-location")).length;
    env.advance(10000);
    const older = env.position();
    older.timestamp = newer.timestamp - 1000;
    await receive(older);
    assert.equal(env.calls.filter(call => call.url.endsWith("update-location")).length, before);
    assert.equal(env.nodes.get("currentLatitude").textContent, "1.0000000");
});

test("a late heartbeat failure does not replace a newer successful watcher update", async () => {
    const env = await browser(); await env.window.toggleLiveTracking();
    env.delayGPS = true;
    env.advance(10000); await env.tick(10000);
    await [...env.watches.values()][0].receive(env.position());
    env.geoError = {code: 3};
    env.delayGPS = false;
    env.rejectGPS({code: 3}); await flush();
    assert.match(env.nodes.get("trackingStatus").textContent, /^Live/);
    assert.match(env.nodes.get("locationStatus").textContent, /location saved/);
});

test("a late polling failure does not replace a newer successfully saved location", async () => {
    const env = await browser(); await env.window.toggleLiveTracking();
    env.delayPoll = true;
    env.failPoll = true;
    env.advance(10000); await env.tick(10000);
    assert.equal(typeof env.releasePoll, "function");
    env.releasePoll(); await flush();
    assert.match(env.nodes.get("trackingStatus").textContent, /^Live/);
    assert.match(env.nodes.get("locationStatus").textContent, /location saved/);
});

test("network failures stop the live indicator until a retry saves a fresh position", async () => {
    const env = await browser(); await env.window.toggleLiveTracking();
    env.failLocation = true;
    env.advance(10000); await env.tick(10000);
    assert.match(env.nodes.get("trackingStatus").textContent, /interrupted/);
    assert.match(env.nodes.get("locationStatus").textContent, /Retrying automatically/);
    env.failLocation = false;
    env.advance(10000); await env.tick(10000);
    assert.match(env.nodes.get("trackingStatus").textContent, /^Live/);
});

test("the availability switch stays at the server state while GPS is pending", async () => {
    const env = await browser();
    env.delayGPS = true;
    const starting = env.window.toggleLiveTracking();
    await flush();
    assert.equal(env.nodes.get("availabilityToggle").checked, false);
    assert.equal(env.nodes.get("availabilityToggle").disabled, true);
    assert.match(env.nodes.get("availabilityStatus").textContent, /OFFLINE/);
    env.resolveGPS(env.position());
    await starting;
    assert.equal(env.nodes.get("availabilityToggle").checked, true);
    assert.match(env.nodes.get("availabilityStatus").textContent, /ONLINE/);
});
