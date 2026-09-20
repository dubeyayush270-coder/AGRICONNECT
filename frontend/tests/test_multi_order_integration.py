"""Real Flask + MariaDB regressions, using only a newly created test database.

Run: python -B tests/test_multi_order_integration.py --run-isolated
Connection options: ROUTE_TEST_HOST (127.0.0.1), ROUTE_TEST_PORT (33317),
ROUTE_TEST_USER (root), ROUTE_TEST_PASSWORD (empty). The account needs CREATE
and DROP DATABASE. The configured application database is NEVER connected to.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import importlib.util
import os
from pathlib import Path
import re
import secrets
import sys
import threading
import types
import unittest
from unittest.mock import patch

import mysql.connector

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@unittest.skipUnless('--run-isolated' in sys.argv, 'Run this file directly with --run-isolated.')
class MultiOrderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = dict(host=os.getenv('ROUTE_TEST_HOST', '127.0.0.1'),
                          port=int(os.getenv('ROUTE_TEST_PORT', '33317')),
                          user=os.getenv('ROUTE_TEST_USER', 'root'),
                          password=os.getenv('ROUTE_TEST_PASSWORD', ''), connection_timeout=5)
        cls.database_name = 'agriconnect_route_test_' + secrets.token_hex(8)
        assert re.fullmatch(r'agriconnect_route_test_[0-9a-f]{16}', cls.database_name)
        cls.admin = mysql.connector.connect(**cls.config, autocommit=True)
        cls.addClassCleanup(cls.admin.close)
        with cls.admin.cursor() as cursor:
            cursor.execute('CREATE DATABASE `' + cls.database_name + '`')
        cls.addClassCleanup(cls.drop_database)
        cls.config['database'] = cls.database_name
        cls.database = mysql.connector.connect(**cls.config, autocommit=True)
        cls.addClassCleanup(cls.database.close)
        cls.cursor = cls.database.cursor(dictionary=True)
        cls.addClassCleanup(cls.cursor.close)
        for ddl in (ROOT / 'tests/multi_order_schema.sql').read_text(encoding='utf-8').split(';'):
            if ddl.strip():
                cls.cursor.execute(ddl)
        from migrations.add_multi_order_logistics import apply_migration
        apply_migration(cls.database)
        apply_migration(cls.database)
        forecast = types.ModuleType('forecast')
        forecast.forecast_demand = lambda *a, **kw: None
        locations = types.ModuleType('locations')
        locations.LOCATIONS = {}
        cls.import_connection = mysql.connector.connect(**cls.config)
        cls.addClassCleanup(cls.import_connection.close)
        spec = importlib.util.spec_from_file_location('route_integration_app', ROOT / 'app.py')
        cls.module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.module
        with patch.dict(sys.modules, {'forecast': forecast, 'locations': locations}), \
                patch('mysql.connector.connect', return_value=cls.import_connection), patch('os.makedirs'):
            spec.loader.exec_module(cls.module)
        cls.module.DATABASE_CONFIG = cls.config.copy()
        cls.app = cls.module.app
        cls.app.config.update(TESTING=True, SECRET_KEY=secrets.token_hex(32))

    @classmethod
    def drop_database(cls):
        with cls.admin.cursor() as cursor:
            cursor.execute('DROP DATABASE `' + cls.database_name + '`')
        print('Removed isolated test database:', cls.database_name)

    def sql(self, query, params=()):
        self.cursor.execute(query, params)
        return self.cursor.fetchall() if self.cursor.with_rows else self.cursor.lastrowid

    def setUp(self):
        self.users = []
        for role in ('Farmer', 'buyer', 'Logistics', 'Logistics'):
            self.users.append(self.sql('''INSERT INTO users
                (name,email,phone,role,state,district,market,password)
                VALUES('Fixture',%s,%s,%s,'Test','Test','Test','not-a-real-password')''',
                (secrets.token_hex(12) + '@example.invalid', str(secrets.randbelow(9_000_000_000)+1_000_000_000), role)))
        self.farmer, self.buyer, self.driver, self.other_driver = self.users
        self.drivers = []
        for uid in (self.driver, self.other_driver):
            self.drivers.append(self.sql('''INSERT INTO logistics_profiles
                (user_id,vehicle_number,vehicle_type,vehicle_capacity,availability,
                 current_latitude,current_longitude,location_updated_at)
                VALUES(%s,'TEST','Pickup',100,'ONLINE',0,0,CURRENT_TIMESTAMP)''', (uid,)))
        self.logistics, self.other_logistics = self.drivers
        self.product = self.sql('''INSERT INTO products
            (user_id,crop_name,quantity,quality,price_per_kg,state,district,
             pickup_address,pickup_latitude,pickup_longitude)
            VALUES(%s,'Fixture',1000,'A',20,'Test','Test','Fixture pickup',0,0)''', (self.farmer,))
        self.client = self.client_for(self.driver)

    def client_for(self, uid):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session.update(user_id=uid, delivery_csrf='test-token', buyer_csrf='buyer-token')
        return client

    def new_order(self, quantity=60, pickup=(0, 0), delivery=(0, .3), invites=None):
        order_id = self.sql('''INSERT INTO orders
            (product_id,buyer_id,farmer_id,quantity,product_price,product_total,
             pickup_address,pickup_latitude,pickup_longitude,
             delivery_house_no,delivery_street,delivery_village_city,delivery_pincode,
             delivery_district,delivery_state,delivery_address,delivery_latitude,delivery_longitude,
             distance_km,estimated_logistics_cost,total_amount,status)
            VALUES(%s,%s,%s,%s,20,%s,'Pickup',%s,%s,'1','Road','Town','123456','Test','Test',
                   'Delivery',%s,%s,30,450,%s,'PENDING_LOGISTICS')''',
            (self.product, self.buyer, self.farmer, quantity, Decimal(str(quantity))*20,
             *pickup, *delivery, Decimal(str(quantity))*20 + 450))
        for lid in self.drivers if invites is None else invites:
            self.sql("INSERT INTO logistics_order_requests(order_id,logistics_id,status) VALUES(%s,%s,'PENDING')",
                     (order_id, lid))
        return order_id

    def order(self, order_id):
        return self.sql('SELECT * FROM orders WHERE order_id=%s', (order_id,))[0]

    def route(self, order_id):
        return self.sql('SELECT * FROM logistics_routes WHERE route_id=%s',
                        (self.order(order_id)['logistics_route_id'],))[0]

    def stops(self, order_id):
        return self.sql('SELECT * FROM logistics_route_stops WHERE route_id=%s ORDER BY sequence_no',
                        (self.order(order_id)['logistics_route_id'],))

    def accept(self, order_id, client=None, success=True):
        result = (client or self.client).post(f'/logistics/available-requests/{order_id}/accept')
        self.assertEqual(result.status_code, 302)
        if success:
            self.assertIn('accepted+successfully', result.location)
        else:
            self.assertNotIn('accepted+successfully', result.location)
        return result

    def status(self, order_id, target, client=None, code=200):
        result = (client or self.client).post(f'/logistics/deliveries/{order_id}/status',
                          json=dict(status=target, csrf_token='test-token'))
        self.assertEqual(result.status_code, code, result.get_data(as_text=True))
        return result

    def visible(self, order_id, client=None):
        result = (client or self.client).get('/logistics/available-requests')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.headers['Cache-Control'], 'no-store')
        return f'/logistics/available-requests/{order_id}/accept' in result.get_data(as_text=True)

    def complete_route(self, order_id):
        while True:
            pending = [s for s in self.stops(order_id) if s['status'] == 'PLANNED']
            if not pending:
                break
            stop = pending[0]
            oid = stop['order_id']
            if stop['stop_type'] == 'PICKUP':
                self.status(oid, 'PICKED_UP')
            else:
                if self.order(oid)['status'] == 'PICKED_UP':
                    self.status(oid, 'IN_TRANSIT')
                self.status(oid, 'DELIVERED')

    def state_snapshot(self, order_id):
        return self.order(order_id), self.route(order_id), self.stops(order_id)

    def parallel(self, actions):
        barrier = threading.Barrier(len(actions))
        def run(action):
            barrier.wait(timeout=10)
            return action()
        with ThreadPoolExecutor(max_workers=len(actions)) as executor:
            futures = [executor.submit(run, action) for action in actions]
            return [future.result(timeout=20) for future in futures]

    def test_first_accept_creates_route_and_expires_other_invitations(self):
        a = self.new_order()
        self.assertTrue(self.visible(a))
        self.accept(a)
        order, route, stops = self.state_snapshot(a)
        self.assertEqual(order['status'], 'LOGISTICS_ASSIGNED')
        self.assertIsNotNone(order['logistics_assigned_at'])
        self.assertEqual(order['assigned_logistics_id'], self.logistics)
        self.assertEqual(route['current_load_kg'], 0)
        self.assertEqual(route['route_version'], 1)
        self.assertEqual([(s['sequence_no'], s['stop_type'], s['quantity_delta'], s['status']) for s in stops],
                         [(1, 'PICKUP', 60, 'PLANNED'), (2, 'DELIVERY', -60, 'PLANNED')])
        invites = self.sql('SELECT logistics_id,status FROM logistics_order_requests WHERE order_id=%s', (a,))
        self.assertEqual({i['logistics_id']: i['status'] for i in invites},
                         {self.logistics: 'ACCEPTED', self.other_logistics: 'EXPIRED'})
        before = self.state_snapshot(a)
        self.accept(a, success=False)
        self.assertEqual(before, self.state_snapshot(a))

    def test_shared_route_small_extension_and_full_lifecycle(self):
        a = self.new_order()
        self.accept(a)
        b = self.new_order(30, pickup=(0, .1), delivery=(0, .32))
        self.assertTrue(self.visible(b))
        self.accept(b)
        self.assertEqual(self.order(a)['logistics_route_id'], self.order(b)['logistics_route_id'])
        self.status(b, 'PICKED_UP', code=409)
        self.status(a, 'PICKED_UP')
        self.assertEqual(self.route(a)['current_load_kg'], 60)
        self.status(a, 'IN_TRANSIT')
        before = self.state_snapshot(a)
        self.status(a, 'DELIVERED', code=409)
        self.assertEqual(before, self.state_snapshot(a))
        self.status(b, 'PICKED_UP')
        self.assertEqual(self.route(a)['current_load_kg'], 90)
        self.complete_route(a)
        self.assertEqual(self.route(a)['current_load_kg'], 0)
        self.assertEqual(self.route(a)['status'], 'COMPLETED')
        self.assertEqual(self.route(a)['planned_distance_km'], 0)
        self.assertIsNotNone(self.route(a)['completed_at'])
        for oid in (a, b):
            order = self.order(oid)
            self.assertEqual(order['status'], 'DELIVERED')
            for name in ('logistics_assigned_at', 'picked_up_at', 'in_transit_at', 'delivered_at'):
                self.assertIsNotNone(order[name])
        for stop in self.stops(a):
            self.assertEqual(stop['status'], 'COMPLETED')
            self.assertIsNotNone(stop['completed_at'])
            self.assertIsNotNone(stop['actual_arrival_at'])
        c = self.new_order(10)
        self.accept(c)
        self.assertNotEqual(self.order(a)['logistics_route_id'], self.order(c)['logistics_route_id'])

    def test_far_pickup_and_excessive_detour_hidden_and_rejected(self):
        a = self.new_order()
        self.accept(a)
        for pickup, delivery in (((1, 1), (0, .3)), ((0, .1), (1, 1))):
            b = self.new_order(20, pickup, delivery)
            self.assertFalse(self.visible(b))
            before = self.state_snapshot(a)
            self.accept(b, success=False)
            self.assertEqual(before, self.state_snapshot(a))
            self.assertEqual(self.order(b)['status'], 'PENDING_LOGISTICS')

    def test_capacity_reused_after_delivery_at_same_location(self):
        a = self.new_order(80, delivery=(0, .1))
        self.accept(a)
        b = self.new_order(50, pickup=(0, .1), delivery=(0, .14))
        self.assertTrue(self.visible(b))
        self.accept(b)
        self.assertEqual([(s['order_id'], s['stop_type']) for s in self.stops(a)],
                         [(a, 'PICKUP'), (a, 'DELIVERY'), (b, 'PICKUP'), (b, 'DELIVERY')])
        self.complete_route(a)

    def test_insertion_after_pickup_preserves_completed_stop_and_load(self):
        a = self.new_order()
        self.accept(a)
        self.status(a, 'PICKED_UP')
        completed = self.stops(a)[0]
        b = self.new_order(30, pickup=(0, .1), delivery=(0, .32))
        self.accept(b)
        self.assertEqual(completed, self.stops(a)[0])
        self.assertEqual(self.route(a)['current_load_kg'], 60)
        self.assertEqual([s['sequence_no'] for s in self.stops(a)], [1, 2, 3, 4])
        self.complete_route(a)

    def test_retries_are_idempotent_for_every_lifecycle_step(self):
        a = self.new_order('0.10')
        self.accept(a)
        for target in ('PICKED_UP', 'IN_TRANSIT', 'DELIVERED'):
            self.status(a, target)
            before = self.state_snapshot(a)
            response = self.status(a, target)
            self.assertFalse(response.json['changed'])
            self.assertEqual(before, self.state_snapshot(a))
        self.assertEqual(self.route(a)['current_load_kg'], Decimal('0.00'))

    def test_invalid_status_auth_csrf_and_ownership(self):
        a = self.new_order()
        self.accept(a)
        before = self.state_snapshot(a)
        self.status(a, 'DELIVERED', code=409)
        self.status(a, 'PICKED_UP', client=self.client_for(self.other_driver), code=404)
        self.status(a, 'PICKED_UP', client=self.app.test_client(), code=401)
        self.status(a, 'PICKED_UP', client=self.client_for(self.buyer), code=403)
        result = self.client.post(f'/logistics/deliveries/{a}/status', json=dict(status='PICKED_UP'))
        self.assertEqual(result.status_code, 403)
        self.assertEqual(before, self.state_snapshot(a))

    def test_accept_checks_online_invitation_capacity_and_location(self):
        a = self.new_order(10, invites=[])
        self.accept(a, success=False)
        b = self.new_order(101)
        self.assertFalse(self.visible(b))
        self.accept(b, success=False)
        c = self.new_order(10)
        self.sql("UPDATE logistics_profiles SET availability='OFFLINE' WHERE logistics_id=%s", (self.logistics,))
        self.assertFalse(self.visible(c))
        self.accept(c, success=False)
        self.sql("UPDATE logistics_profiles SET availability='ONLINE' WHERE logistics_id=%s", (self.logistics,))
        self.sql('UPDATE orders SET pickup_latitude=NULL WHERE order_id=%s', (c,))
        self.assertFalse(self.visible(c))
        self.accept(c, success=False)
        self.assertIsNone(self.order(c)['logistics_route_id'])

    def test_missing_gps_uses_pickup_then_last_completed_stop(self):
        self.sql('UPDATE logistics_profiles SET current_latitude=NULL,current_longitude=NULL WHERE logistics_id=%s',
                 (self.logistics,))
        a = self.new_order()
        self.accept(a)
        self.status(a, 'PICKED_UP')
        b = self.new_order(30, pickup=(0, .1), delivery=(0, .32))
        self.assertTrue(self.visible(b))
        self.accept(b)
        self.complete_route(a)

    def test_legacy_delivery_blocks_new_route_until_finished(self):
        a = self.new_order()
        self.sql("UPDATE orders SET assigned_logistics_id=%s,status='LOGISTICS_ASSIGNED' WHERE order_id=%s",
                 (self.logistics, a))
        b = self.new_order(10)
        self.assertFalse(self.visible(b))
        self.accept(b, success=False)
        for target in ('PICKED_UP', 'IN_TRANSIT', 'DELIVERED'):
            self.status(a, target)
        self.assertIsNone(self.order(a)['logistics_route_id'])
        self.assertTrue(self.visible(b))
        self.accept(b)

    def test_profile_cannot_shrink_below_planned_peak(self):
        a = self.new_order()
        self.accept(a)
        response = self.client.post('/logistics/update-profile', data=dict(
            vehicle_number='TEST', vehicle_type='Pickup', vehicle_capacity=50))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.sql('SELECT vehicle_capacity FROM logistics_profiles WHERE logistics_id=%s',
                                 (self.logistics,))[0]['vehicle_capacity'], 100)

    def test_two_drivers_competing_for_one_order(self):
        a = self.new_order()
        def action(uid):
            return lambda: self.client_for(uid).post(f'/logistics/available-requests/{a}/accept')
        results = self.parallel([action(self.driver), action(self.other_driver)])
        self.assertEqual(sum('accepted+successfully' in r.location for r in results), 1)
        self.assertEqual(len(self.stops(a)), 2)
        self.assertEqual(self.sql('SELECT COUNT(*) AS n FROM logistics_routes WHERE logistics_id IN (%s,%s)',
                                  tuple(self.drivers))[0]['n'], 1)

    def test_parallel_first_accepts_create_only_one_active_route(self):
        a, b = self.new_order(30), self.new_order(30, pickup=(0, .1), delivery=(0, .32))
        def action(oid):
            return lambda: self.client_for(self.driver).post(f'/logistics/available-requests/{oid}/accept')
        results = self.parallel([action(a), action(b)])
        self.assertTrue(all('accepted+successfully' in r.location for r in results))
        self.assertEqual(self.order(a)['logistics_route_id'], self.order(b)['logistics_route_id'])
        self.assertEqual(len(self.stops(a)), 4)

    def test_stale_offers_revalidated_during_parallel_accepts(self):
        a = self.new_order()
        self.accept(a)
        self.status(a, 'PICKED_UP')
        b = self.new_order(30, pickup=(0, .1))
        c = self.new_order(30, pickup=(0, .1))
        self.assertTrue(self.visible(b))
        self.assertTrue(self.visible(c))
        def action(oid):
            return lambda: self.client_for(self.driver).post(f'/logistics/available-requests/{oid}/accept')
        results = self.parallel([action(b), action(c)])
        self.assertEqual(sum('accepted+successfully' in r.location for r in results), 1)
        self.assertEqual(len(self.stops(a)), 4)
        self.assertEqual(self.route(a)['current_load_kg'], 60)

    def test_parallel_duplicate_pickups_change_load_once(self):
        a = self.new_order()
        self.accept(a)
        def pickup():
            return self.client_for(self.driver).post(f'/logistics/deliveries/{a}/status',
                json=dict(status='PICKED_UP', csrf_token='test-token'))
        results = self.parallel([pickup, pickup])
        self.assertTrue(all(r.status_code == 200 for r in results))
        self.assertEqual(sum(r.json['changed'] for r in results), 1)
        self.assertEqual(self.route(a)['current_load_kg'], 60)
        self.assertEqual(self.route(a)['route_version'], 2)

    def test_accept_and_pickup_serialize_without_losing_load(self):
        a = self.new_order()
        self.accept(a)
        b = self.new_order(30, pickup=(0, .1), delivery=(0, .32))
        results = self.parallel([
            lambda: self.client_for(self.driver).post(f'/logistics/available-requests/{b}/accept'),
            lambda: self.client_for(self.driver).post(f'/logistics/deliveries/{a}/status',
                         json=dict(status='PICKED_UP', csrf_token='test-token'))])
        self.assertIn('accepted+successfully', results[0].location)
        self.assertEqual(results[1].status_code, 200)
        self.assertEqual(self.route(a)['current_load_kg'], 60)
        self.assertEqual(len(self.stops(a)), 4)
        self.complete_route(a)

    def test_accept_failure_rolls_back_route_assignment_and_invitations(self):
        a = self.new_order()
        real_save = self.module.save_logistics_route_plan
        def fail_after_save(*args, **kwargs):
            real_save(*args, **kwargs)
            raise mysql.connector.Error('Injected after stop insertion')
        with patch.object(self.module, 'save_logistics_route_plan', side_effect=fail_after_save), \
                self.assertLogs(self.app.logger, level='ERROR'):
            self.accept(a, success=False)
        self.assertIsNone(self.order(a)['logistics_route_id'])
        self.assertEqual(self.order(a)['status'], 'PENDING_LOGISTICS')
        self.assertEqual(self.sql('SELECT COUNT(*) AS n FROM logistics_routes WHERE logistics_id=%s',
                                  (self.logistics,))[0]['n'], 0)
        self.assertTrue(all(r['status'] == 'PENDING' for r in self.sql(
            'SELECT status FROM logistics_order_requests WHERE order_id=%s', (a,))))

    def test_status_database_error_rolls_back_stop_and_load(self):
        a = self.new_order()
        self.accept(a)
        before = self.state_snapshot(a)
        self.sql("""CREATE TRIGGER route_test_fail_status BEFORE UPDATE ON orders FOR EACH ROW
            BEGIN IF NEW.order_id = %s AND NEW.status='PICKED_UP' THEN
                SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Injected failure'; END IF; END""" % a)
        try:
            with self.assertLogs(self.app.logger, level='ERROR'):
                self.status(a, 'PICKED_UP', code=503)
        finally:
            self.sql('DROP TRIGGER route_test_fail_status')
        self.assertEqual(before, self.state_snapshot(a))

    def test_invitation_failure_rolls_back_entire_acceptance(self):
        a = self.new_order()
        self.sql("""CREATE TRIGGER route_test_fail_invitation BEFORE UPDATE ON logistics_order_requests
            FOR EACH ROW BEGIN IF NEW.order_id = %s AND NEW.status='ACCEPTED' THEN
                SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Injected invitation failure'; END IF; END""" % a)
        try:
            with self.assertLogs(self.app.logger, level='ERROR'):
                self.accept(a, success=False)
        finally:
            self.sql('DROP TRIGGER route_test_fail_invitation')
        self.assertIsNone(self.order(a)['logistics_route_id'])
        self.assertIsNone(self.order(a)['assigned_logistics_id'])
        self.assertIsNone(self.order(a)['logistics_assigned_at'])
        self.assertEqual(self.sql('SELECT COUNT(*) AS n FROM logistics_routes WHERE logistics_id=%s',
                                  (self.logistics,))[0]['n'], 0)
        self.assertTrue(all(r['status'] == 'PENDING' for r in self.sql(
            'SELECT status FROM logistics_order_requests WHERE order_id=%s', (a,))))

    def test_final_delivery_failure_does_not_complete_route(self):
        a = self.new_order()
        self.accept(a)
        self.status(a, 'PICKED_UP')
        self.status(a, 'IN_TRANSIT')
        before = self.state_snapshot(a)
        self.sql("""CREATE TRIGGER route_test_fail_final BEFORE UPDATE ON orders FOR EACH ROW
            BEGIN IF NEW.order_id = %s AND NEW.status='DELIVERED' THEN
                SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Injected final failure'; END IF; END""" % a)
        try:
            with self.assertLogs(self.app.logger, level='ERROR'):
                self.status(a, 'DELIVERED', code=503)
        finally:
            self.sql('DROP TRIGGER route_test_fail_final')
        self.assertEqual(before, self.state_snapshot(a))

    def test_parallel_duplicate_deliveries_decrement_load_once(self):
        a = self.new_order()
        self.accept(a)
        self.status(a, 'PICKED_UP')
        self.status(a, 'IN_TRANSIT')
        def deliver():
            return self.client_for(self.driver).post(f'/logistics/deliveries/{a}/status',
                json=dict(status='DELIVERED', csrf_token='test-token'))
        results = self.parallel([deliver, deliver])
        self.assertTrue(all(r.status_code == 200 for r in results))
        self.assertEqual(sum(r.json['changed'] for r in results), 1)
        self.assertEqual(self.route(a)['current_load_kg'], 0)
        self.assertEqual(self.route(a)['status'], 'COMPLETED')

    def test_completed_route_history_and_active_list_keep_api_shape(self):
        a = self.new_order()
        self.accept(a)
        result = self.client.get('/logistics/deliveries')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json['deliveries'][0]['next_status'], 'PICKED_UP')
        self.assertEqual(result.json['deliveries'][0]['logistics_route_id'], self.route(a)['route_id'])
        self.assertEqual(result.json['summary']['active_count'], '1')
        self.complete_route(a)
        result = self.client.get('/logistics/deliveries?view=history')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json['deliveries'][0]['status'], 'DELIVERED')
        self.assertEqual(result.json['deliveries'][0]['next_status'], None)

    def test_corrupt_route_load_and_order_quantity_fail_without_mutation(self):
        a = self.new_order()
        self.accept(a)
        self.sql('UPDATE logistics_routes SET current_load_kg=7 WHERE route_id=%s',
                 (self.order(a)['logistics_route_id'],))
        before = self.state_snapshot(a)
        self.status(a, 'PICKED_UP', code=409)
        self.assertEqual(before, self.state_snapshot(a))
        self.sql('UPDATE logistics_routes SET current_load_kg=0 WHERE route_id=%s',
                 (self.order(a)['logistics_route_id'],))
        self.sql('UPDATE orders SET quantity=61 WHERE order_id=%s', (a,))
        before = self.state_snapshot(a)
        self.status(a, 'PICKED_UP', code=409)
        self.assertEqual(before, self.state_snapshot(a))

    def test_buyer_checkout_still_quotes_reserves_stock_and_creates_invitations(self):
        buyer = self.client_for(self.buyer)
        form = dict(csrf_token='buyer-token', quantity='5', delivery_latitude='0', delivery_longitude='.3',
                    delivery_house_no='1', delivery_street='Road', delivery_village_city='Town',
                    delivery_pincode='123456', delivery_district='Test', delivery_state='Test')
        response = buyer.post(f'/buyer/quote/{self.product}', data=form)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['product_total'], '100.00')
        quote = response.json
        response = buyer.post(f'/place-order/{self.product}', data={**form, 'quote_token': quote['quote_token']})
        self.assertEqual(response.status_code, 303)
        oid = int(response.location.split('/orders/')[1].split('?')[0])
        order = self.order(oid)
        self.assertEqual(order['status'], 'PENDING_LOGISTICS')
        self.assertIsNone(order['logistics_route_id'])
        self.assertEqual(order['total_amount'], Decimal(quote['total_amount']))
        self.assertEqual(self.sql('SELECT quantity FROM products WHERE product_id=%s',
                                 (self.product,))[0]['quantity'], 995)
        self.assertTrue(self.sql('SELECT request_id FROM logistics_order_requests WHERE order_id=%s AND logistics_id=%s',
                                (oid, self.logistics)))
        self.accept(oid)
        self.complete_route(oid)
        self.assertEqual(self.order(oid)['total_amount'], Decimal(quote['total_amount']))
        self.assertEqual(self.sql('SELECT quantity FROM products WHERE product_id=%s',
                                 (self.product,))[0]['quantity'], 995)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-isolated', action='store_true', required=True)
    parser.parse_args()
    unittest.main(argv=[sys.argv[0]], verbosity=2)
