const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const {distance, duration, separation} = require("../static/route-navigation.js");
const source = fs.readFileSync(path.join(__dirname, "../static/route-navigation.js"), "utf8");
const flush = async () => { for (let i = 0; i < 70; i++) await Promise.resolve(); };

class Node {
    constructor() { this.children = []; this.events = {}; this.value = "next"; this.dataset = {}; this.textContent = "";
        this.classList = {toggle() {}}; this.style = {}; this.attributes = {}; }
    append(...nodes) { this.children.push(...nodes); }
    prepend(...nodes) { this.children.unshift(...nodes); }
    replaceChildren(...nodes) { this.children = nodes; }
    setAttribute(name, value) { this.attributes[name] = value; }
    addEventListener(name, fn) { this.events[name] = fn; }
    querySelector() { return this.option || (this.option = new Node()); }
    set innerHTML(_) { throw new Error("Route data must be rendered as text, not HTML"); }
}
const text = (node) => node.textContent + node.children.map(text).join(" ");
function fixture() {
    return {success: true, csrf_token: "test-token", location_state: {location: {latitude: 23.2, longitude: 77.4},
        last_seen_seconds: 0, stale_after_seconds: 60}, route: {route_id: 8, route_version: 2, completed_stops: 0,
        current_load_kg: 0, vehicle_capacity_kg: 100, stops: [
            {stop_id: 10, order_id: 11, stop_type: "PICKUP", latitude: 23.21, longitude: 77.41,
                address: "Farm <img src=x onerror=alert(1)>", quantity_kg: 50, load_after_kg: 50, next_status: "PICKED_UP"},
            {stop_id: 12, order_id: 11, stop_type: "DELIVERY", latitude: 23.22, longitude: 77.42,
                address: "Market", quantity_kg: 50, load_after_kg: 0, next_status: "DELIVERED"}]}};
}
function directions() {
    return {success: true, route_id: 8, route_version: 2, scope: "next", origin: {latitude: 23.2, longitude: 77.4},
        stop_ids: [10], routes: [{distance_m: 1200, duration_s: 240,
            geometry: {type: "LineString", coordinates: [[77.4, 23.2], [77.41, 23.21]]},
            legs: [{steps: [{instruction: "Turn left onto Market Road", distance_m: 200}]}]}]};
}
async function browser(options = {}) {
    const nodes = new Map();
    const byId = (id) => { if (!nodes.has(id)) nodes.set(id, new Node()); return nodes.get(id); };
    byId("route-optimization").dataset = {routeUrl: "/logistics/route", directionsUrl: "/logistics/route/directions",
        statusUrl: "/logistics/deliveries/0/status"};
    const events = {}, windowEvents = {};
    const env = {byId, calls: [], snapshot: fixture(), roads: directions(), snapshotStatus: 200, roadStatus: 200, ...options};
    const timers = new Map(); let timerId = 0;
    const window = {addEventListener: (name, fn) => { windowEvents[name] = fn; }};
    const document = {hidden: false, getElementById: byId, createElement: () => new Node(),
        addEventListener: (name, fn) => { events[name] = fn; }};
    const context = {window, document, AbortController, URLSearchParams, console,
        setTimeout: (fn) => { timers.set(++timerId, fn); return timerId; }, clearTimeout: (id) => timers.delete(id),
        setInterval: () => ++timerId, clearInterval() {},
        async fetch(url, options = {}) {
            env.calls.push({url, options});
            if (url.includes("/deliveries/")) {
                env.snapshot = {...env.snapshot, route: null, message: "No active route."};
                return {ok: true, status: 200, json: async () => ({success: true})};
            }
            const road = url.includes("/directions?");
            const body = JSON.parse(JSON.stringify(road ? env.roads : env.snapshot));
            const status = road ? env.roadStatus : env.snapshotStatus;
            if (road && env.holdDirections) await new Promise(resolve => { env.releaseDirections = resolve; });
            return {ok: status === 200, status, json: async () => body};
        },
    };
    vm.runInNewContext(source, context);
    events.DOMContentLoaded(); await flush();
    env.windowEvents = windowEvents;
    return env;
}

test("route distances, driving times and reroute threshold are readable", () => {
    assert.equal(distance(850), "850 m");
    assert.equal(distance(1234), "1.2 km");
    assert.equal(duration(660), "11 min");
    assert.equal(duration(3720), "1 hr 2 min");
    assert.equal(separation({latitude: 0, longitude: 0}, {latitude: 0, longitude: 0}), 0);
    assert.ok(separation({latitude: 0, longitude: 0}, {latitude: 0, longitude: .002}) > 100);
});
test("renders assigned stops, road instructions and a useful map fallback", async () => {
    const env = await browser();
    assert.equal(env.byId("routeDistance").textContent, "1.2 km");
    assert.equal(env.byId("routeDuration").textContent, "4 min");
    assert.match(text(env.byId("routeStops")), /Farm <img src=x onerror=alert\(1\)>/);
    assert.match(text(env.byId("routeInstructions")), /Turn left onto Market Road/);
    assert.match(env.byId("routeMap").textContent, /map could not load/);
});
test("scope controls request the full saved route", async () => {
    const env = await browser();
    env.byId("routeScope").value = "all";
    await env.byId("routeScope").events.change();
    assert.ok(env.calls.at(-1).url.includes("scope=all"));
});
test("stale GPS keeps stops visible without requesting a road line", async () => {
    const snapshot = fixture(); snapshot.location_state.last_seen_seconds = 90;
    const env = await browser({snapshot});
    assert.equal(env.calls.filter(c => c.url.includes("directions")).length, 0);
    assert.match(env.byId("routeMessage").textContent, /Update GPS/);
    assert.equal(env.byId("routeDistance").textContent, "—");
    assert.equal(env.byId("routeContent").hidden, false);
});
test("no road route clears estimates and never replaces it with a line", async () => {
    const env = await browser({roadStatus: 422, roads: {success: false, message: "No driving route", code: "NO_ROAD_ROUTE"}});
    assert.equal(env.byId("routeDistance").textContent, "—");
    assert.match(env.byId("routeMessage").textContent, /No driving route/);
    assert.equal(env.byId("routeStops").children.length, 2);
    assert.equal(env.byId("routeAlternatives").children.length, 0);
});
test("late directions cannot replace a newer route snapshot", async () => {
    const env = await browser({holdDirections: true});
    env.holdDirections = false;
    env.snapshot = {...fixture(), route: null, message: "All deliveries completed."};
    await env.byId("refreshRoute").events.click();
    env.releaseDirections(); await flush();
    assert.equal(env.byId("routeContent").hidden, true);
    assert.equal(env.byId("routeMessage").textContent, "All deliveries completed.");
    assert.equal(env.byId("routeAlternatives").children.length, 0);
});
test("expired sign-in clears private route content", async () => {
    const env = await browser();
    env.snapshotStatus = 401; env.snapshot = {success: false, message: "Sign in again"};
    await env.byId("refreshRoute").events.click();
    assert.equal(env.byId("routeContent").hidden, true);
    assert.equal(env.byId("routeMessage").textContent, "Sign in again");
    assert.equal(env.byId("completeRouteStop").disabled, true);
});
test("unverified assignments disable progress updates until the route is refreshed", async () => {
    const env = await browser();
    env.snapshotStatus = 503;
    env.snapshot = {success: false, message: "Unable to load your route"};
    await env.byId("refreshRoute").events.click();
    assert.equal(env.byId("completeRouteStop").disabled, true);
    await env.byId("completeRouteStop").events.click();
    assert.equal(env.calls.filter(c => c.options.method === "POST").length, 0);
    env.snapshotStatus = 200; env.snapshot = fixture();
    await env.byId("refreshRoute").events.click();
    assert.equal(env.byId("completeRouteStop").disabled, false);
});
test("next-stop action sends CSRF and prevents double submission", async () => {
    const env = await browser();
    const first = env.byId("completeRouteStop").events.click();
    const second = env.byId("completeRouteStop").events.click();
    await Promise.all([first, second]);
    const writes = env.calls.filter(c => c.options.method === "POST");
    assert.equal(writes.length, 1);
    assert.equal(writes[0].url, "/logistics/deliveries/11/status");
    assert.equal(writes[0].options.headers["X-CSRF-Token"], "test-token");
    assert.deepEqual(JSON.parse(writes[0].options.body), {status: "PICKED_UP"});
    assert.equal(env.byId("routeContent").hidden, true);
});
