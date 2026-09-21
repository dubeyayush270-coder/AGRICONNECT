"""Assigned-route authorization, freshness and road-service failure regressions."""
from copy import deepcopy
import unittest
from unittest.mock import MagicMock, patch

from test_logistics import module, profile
from test_road_directions import road_payload, response


class RouteNavigationTests(unittest.TestCase):
    def setUp(self):
        self.client = module.app.test_client()
        with self.client.session_transaction() as session: session['user_id'] = 7
        self.driver = profile(current_latitude=23.2, current_longitude=77.4)
        self.user = {'role': 'Logistics'}
        self.route = dict(route_id=8, logistics_id=4, status='ACTIVE', current_load_kg=0, route_version=2)
        self.stops = [dict(stop_id=10, route_id=8, order_id=11, stop_type='PICKUP', sequence_no=1,
                           latitude=23.21, longitude=77.41, address='Farm', quantity_delta=50, status='PLANNED'),
                      dict(stop_id=12, route_id=8, order_id=11, stop_type='DELIVERY', sequence_no=2,
                           latitude=23.22, longitude=77.42, address='Market', quantity_delta=-50, status='PLANNED')]
        self.orders = [dict(order_id=11, status='LOGISTICS_ASSIGNED', assigned_logistics_id=4)]
        self.connection = MagicMock()
        self.cursor = self.connection.cursor.return_value
        self.cursor.execute.side_effect = self.execute
        patcher = patch.object(module.mysql.connector, 'connect', return_value=self.connection)
        patcher.start(); self.addCleanup(patcher.stop)
        self.router = module.app.extensions['road_directions']
        self.router.cache.clear()
        self.router.next_request_at = 0
        patcher = patch.object(self.router, 'http_get', return_value=response())
        self.http = patcher.start(); self.addCleanup(patcher.stop)

    def execute(self, sql, params=()):
        if 'SELECT role FROM users' in sql:
            self.assertEqual(params, (7,)); self.cursor.fetchone.return_value = self.user
        elif 'FROM logistics_profiles' in sql:
            self.assertEqual(params, (7,)); self.cursor.fetchone.return_value = self.driver
        elif 'FROM logistics_routes WHERE' in sql:
            self.assertEqual(params, (4,)); self.cursor.fetchall.return_value = [deepcopy(self.route)] if self.route else []
        elif 'FROM logistics_route_stops' in sql:
            self.assertEqual(params, (8,)); self.cursor.fetchall.return_value = deepcopy(self.stops)
        elif 'FROM orders' in sql:
            self.assertEqual(params, (8,)); self.cursor.fetchall.return_value = deepcopy(self.orders)
        else: self.fail('Unexpected database statement')

    def directions(self, query=''):
        return self.client.get('/logistics/route/directions?route_id=8&route_version=2' + query)

    def test_snapshot_is_assigned_scoped_and_contains_ordered_stops(self):
        result = self.client.get('/logistics/route?logistics_id=999')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.headers['Cache-Control'], 'no-store')
        self.assertEqual([s['stop_id'] for s in result.json['route']['stops']], [10, 12])
        self.assertEqual([s['load_after_kg'] for s in result.json['route']['stops']], [50, 0])
        self.assertTrue(result.json['csrf_token'])
        self.http.assert_not_called()

    def test_anonymous_other_roles_and_deleted_users_cannot_read_routes(self):
        for path in ('/logistics/route', '/logistics/route/directions'):
            self.assertEqual(module.app.test_client().get(path).status_code, 401)
            self.user = {'role': 'buyer'}
            self.assertEqual(self.client.get(path).status_code, 403)
        self.user = None
        self.assertEqual(self.client.get('/logistics/route').status_code, 401)
        with self.client.session_transaction() as session: self.assertNotIn('user_id', session)
        self.http.assert_not_called()

    def test_empty_profile_and_empty_route_are_useful_states(self):
        self.route = None
        self.assertIsNone(self.client.get('/logistics/route').json['route'])
        self.assertEqual(self.directions().status_code, 404)
        self.driver = None
        self.assertIsNone(self.client.get('/logistics/route').json['route'])
        self.http.assert_not_called()

    def test_only_fresh_saved_gps_may_request_directions(self):
        for changes in ({'location_age_seconds': 61}, {'location_age_seconds': None}, {'current_latitude': None}):
            self.driver = profile(current_latitude=23.2, current_longitude=77.4, **changes) if 'current_latitude' not in changes else profile(**changes)
            self.assertEqual(self.directions().json['code'], 'LOCATION_STALE')
        self.http.assert_not_called()

    def test_provider_runs_after_database_connection_closes(self):
        def provider(*args, **kwargs):
            self.connection.close.assert_called()
            return response()
        self.http.side_effect = provider
        result = self.directions()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json['scope'], 'next')
        self.assertEqual(result.json['stop_ids'], [10])
        self.assertEqual(result.json['origin'], {'latitude': 23.2, 'longitude': 77.4})
        self.assertEqual(self.connection.close.call_count, 2)

    def test_full_route_includes_each_planned_stop_in_saved_order(self):
        self.http.return_value = response(road_payload(2))
        result = self.directions('&scope=all')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json['stop_ids'], [10, 12])
        self.assertTrue(self.http.call_args.args[0].endswith('77.400000,23.200000;77.410000,23.210000;77.420000,23.220000'))

    def test_completed_pickup_is_not_routed_again(self):
        self.stops[0]['status'] = 'COMPLETED'
        self.route['current_load_kg'] = 50
        self.orders[0]['status'] = 'IN_TRANSIT'
        result = self.directions()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json['stop_ids'], [12])

    def test_stale_versions_and_foreign_route_ids_are_rejected_before_network(self):
        for query in ('route_id=999&route_version=2', 'route_id=8&route_version=1', ''):
            result = self.client.get('/logistics/route/directions?' + query)
            self.assertEqual(result.status_code, 409)
            self.assertEqual(result.json['code'], 'ROUTE_CHANGED')
        self.http.assert_not_called()

    def test_route_change_during_provider_request_discards_directions(self):
        def provider(*args, **kwargs):
            self.route['route_version'] += 1
            return response()
        self.http.side_effect = provider
        self.assertEqual(self.directions().json['code'], 'ROUTE_CHANGED')

    def test_assignment_status_and_capacity_corruption_rejects_route(self):
        self.orders[0]['assigned_logistics_id'] = 999
        self.assertEqual(self.client.get('/logistics/route').status_code, 409)
        self.orders[0]['assigned_logistics_id'] = 4
        self.orders[0]['status'] = 'CANCELLED'
        self.assertEqual(self.client.get('/logistics/route').status_code, 409)
        self.orders[0]['status'] = 'LOGISTICS_ASSIGNED'
        self.driver['vehicle_capacity'] = 20
        self.assertEqual(self.client.get('/logistics/route').status_code, 409)
        self.http.assert_not_called()

    def test_provider_failure_does_not_modify_orders_or_invent_geometry(self):
        self.http.return_value = response({'code': 'NoRoute'}, 400)
        result = self.directions()
        self.assertEqual(result.status_code, 422)
        self.assertNotIn('routes', result.json)
        self.assertTrue(all(call.args[0].strip().startswith('SELECT') for call in self.cursor.execute.call_args_list))


if __name__ == '__main__': unittest.main()
