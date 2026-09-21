# Driver road navigation

Open **Logistics dashboard → Route Optimization** after accepting a delivery.
Update GPS (or keep live tracking online), then choose **Next stop** or **All
remaining stops**. The map highlights the selected driving route, numbers pickups
and deliveries, shows distance and driving time, and lists turn instructions.
Where the routing provider returns alternatives, the driver can select one.
The next-stop button uses the existing pickup → in transit → delivered workflow.

## Routing configuration

`OSRM_BASE_URL` configures an OSRM-compatible driving service. Its development
default is `https://router.project-osrm.org`. No new package or database migration
is needed beyond the project's existing multi-order logistics schema.

For production, point `OSRM_BASE_URL` at a dedicated or self-hosted OSRM service
with current road coverage for the delivery area. The public demonstration server
has usage limits and no availability guarantee. Requests send coordinates to the
configured service; no names, phone numbers, addresses, or order IDs are sent.
Leaflet map tiles continue to use OpenStreetMap, as the existing location map does.

Example for a local dedicated routing server, before starting Flask:

```powershell
$env:OSRM_BASE_URL = 'http://127.0.0.1:5001'
```

This adds road navigation to the saved pickup/delivery order. It does not change
the existing capacity-aware order-insertion planner, delivery quotes, or prices.
OSRM's driving profile is not truck-specific: estimates exclude live traffic,
loading time, vehicle dimensions and road-access restrictions. Pins may snap to
roads within 250 metres; the final approach may be off-road. A missing road or a
provider failure displays an error and keeps the stop list; it never draws a
straight line and presents it as a driving route.

## Data and freshness

- `GET /logistics/route` returns only the signed-in driver's active route, version,
  pending stops, current/peak load, location freshness and delivery CSRF token.
- `GET /logistics/route/directions?route_id=…&route_version=…&scope=next|all`
  starts at the driver's saved GPS location and preserves stop order. Location
  must be no older than 60 seconds. A fresh offline position is also usable.
- Route version and ownership are checked before and after the provider request.
  Database connections are closed during external requests. Both APIs are
  read-only and return `Cache-Control: no-store`.
- Full-route directions support up to 24 pending stops; next-stop navigation
  remains available for larger routes.
- The page checks saved assignments every 30 seconds. Moving at least 100 metres
  triggers a route refresh, at most once per 30 seconds; manual refresh and stop
  changes can refresh sooner. This is a browser route overview with written
  instructions, not voice guidance or lane-level navigation.
- Each Python process caches at most 128 coordinate routes for 60 seconds and
  limits outgoing request starts to one per second. Multiple workers require a
  shared rate limiter or a routing service sized for their combined traffic.

## Checks

From `frontend`, with the project's virtual environment active:

```powershell
python -B -m unittest discover -s tests -p 'test_road_directions.py' -v
python -B -m unittest discover -s tests -p 'test_route_navigation.py' -v
node --test tests/route-navigation.test.js tests/logistics.test.js
```

The provider and API tests use synthetic coordinates and mock HTTP/database
connections. The separate MariaDB suite exercises navigation, authorization,
route versions, CSRF and the real delivery lifecycle:

```powershell
python -B tests/test_multi_order_integration.py --run-isolated
```

That suite connects only to its explicit `ROUTE_TEST_*` configuration (default
127.0.0.1:33317), creates a random disposable database and removes it afterward.
It does not connect to the configured application database.

Protocol reference: [OSRM Route service](https://project-osrm.org/docs/v5.24.0/api/#route-service).
