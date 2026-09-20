"""Checkout and buyer-access regressions; no live database is modified."""
from decimal import Decimal
import unittest
from unittest.mock import MagicMock, patch

import mysql.connector
from test_logistics import module


BUYER = dict(id=7, name="Buyer Test", role="buyer", district="Bhopal", state="MP")
PRODUCT = dict(product_id=3, user_id=9, crop_name="Tomato", quantity=Decimal("100"),
               price_per_kg=Decimal("20"), quality="A", description="", product_image=None,
               pickup_latitude=23, pickup_longitude=77, pickup_address="Farm, Bhopal",
               pickup_village_city="Bhopal", pickup_district="Bhopal", pickup_state="MP")
FORM = dict(csrf_token="test-csrf", quantity="5.00", delivery_latitude="23.1", delivery_longitude="77.1",
            delivery_house_no="1", delivery_street="Market Road", delivery_village_city="Bhopal",
            delivery_state="MP", delivery_district="Bhopal", delivery_pincode="462001")


class BuyerTests(unittest.TestCase):
    def setUp(self):
        self.client = module.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user_id=7, buyer_csrf="test-csrf")
        self.connection = MagicMock()
        self.cursor = self.connection.cursor.return_value
        self.cursor.lastrowid = 42
        self.connect = patch.object(module.mysql.connector, "connect", return_value=self.connection).start()
        self.addCleanup(patch.stopall)

    def ready(self, product=None):
        self.cursor.fetchone.side_effect = [BUYER.copy(), (product or PRODUCT).copy()]

    def quoted_form(self):
        self.ready()
        response = self.client.post('/buyer/quote/3', data=FORM)
        self.assertEqual(response.status_code, 200)
        self.connection.reset_mock()
        self.ready()
        return {**FORM, "quote_token": response.json["quote_token"]}

    def mutations(self):
        return [call for call in self.cursor.execute.call_args_list
                if call.args[0].strip().startswith(("INSERT", "UPDATE", "DELETE"))]

    def test_buyer_pages_require_login_and_buyer_role(self):
        for path in ('/buyer', '/browse-products', '/buy-product/3', '/buyer/orders',
                     '/buyer/orders/42', '/buyer/orders/42/location'):
            self.assertEqual(module.app.test_client().get(path).status_code, 302)
            self.cursor.fetchone.side_effect = [{**BUYER, 'role': 'Logistics'}]
            self.assertEqual(self.client.get(path).status_code, 403)
        self.assertEqual(self.client.get('/place-order/3').status_code, 405)

    def test_order_and_quote_reject_missing_csrf(self):
        for path in ('/place-order/3', '/buyer/quote/3'):
            self.ready()
            self.assertEqual(self.client.post(path, data={**FORM, 'csrf_token': ''}).status_code, 400)
        self.assertEqual(self.mutations(), [])

    def test_quote_uses_server_price_and_configured_rate(self):
        self.ready()
        response = self.client.post('/buyer/quote/3', data={**FORM, 'product_price': '0.01'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['product_total'], '100.00')
        self.assertEqual(Decimal(response.json['estimated_logistics_cost']),
                         Decimal(response.json['distance_km']) * 15)
        self.assertEqual(self.mutations(), [])

    def test_order_saves_snapshots_reserves_stock_and_invites_drivers(self):
        data = self.quoted_form()
        response = self.client.post('/place-order/3', data={**data, 'total_amount': '0.01', 'buyer_id': 99})
        self.assertEqual(response.status_code, 303)
        self.assertIn('/buyer/orders/42', response.location)
        writes = self.mutations()
        self.assertEqual(len(writes), 3)
        values = writes[0].args[1]
        self.assertEqual(writes[0].args[0].count('%s'), len(values))
        self.assertEqual(values[:6], (3, 7, 9, Decimal('5'), Decimal('20'), Decimal('100')))
        self.assertIn('Market Road', values[20])
        self.assertGreater(values[-1], Decimal('100'))
        self.assertEqual(writes[1].args[1], (Decimal('5'), 3))
        self.assertEqual(writes[2].args[1], (42, Decimal('5')))
        self.assertTrue(any('products WHERE product_id = %s FOR UPDATE' in c.args[0]
                            for c in self.cursor.execute.call_args_list))
        self.connection.commit.assert_called_once()

    def test_invalid_quantities_locations_and_addresses_do_not_write(self):
        invalid = [{'quantity': v} for v in ('0', '-1', 'NaN', 'Infinity', '101', '1.001', 'no')]
        invalid += [{'delivery_latitude': '91'}, {'delivery_longitude': 'NaN'},
                    {'delivery_state': ''}, {'delivery_pincode': '123'}, {'delivery_street': 'x' * 256}]
        for override in invalid:
            with self.subTest(override=override):
                self.cursor.reset_mock()
                self.ready()
                response = self.client.post('/place-order/3', data={**FORM, **override})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.mutations(), [])

    def test_quote_must_be_reviewed_again_when_price_or_quantity_changes(self):
        data = self.quoted_form()
        for override in ({'price_per_kg': Decimal('25')}, {}):
            self.cursor.reset_mock()
            self.ready({**PRODUCT, **override})
            form = data if override else {**data, 'quantity': '6'}
            self.assertEqual(self.client.post('/place-order/3', data=form).status_code, 400)
            self.assertEqual(self.mutations(), [])

    def test_invitation_or_commit_failure_rolls_back_and_never_reports_success(self):
        for failure in ('invitation', 'commit'):
            with self.subTest(failure=failure):
                self.connection.reset_mock(side_effect=True)
                data = self.quoted_form()
                def execute(sql, params):
                    if failure == 'invitation' and 'INSERT INTO logistics_order_requests' in sql:
                        raise mysql.connector.Error('test write failure')
                self.cursor.execute.side_effect = execute
                if failure == 'commit':
                    self.connection.commit.side_effect = mysql.connector.Error('test commit failure')
                with self.assertLogs(module.app.logger, level='ERROR'):
                    response = self.client.post('/place-order/3', data=data)
                self.assertEqual(response.status_code, 503)
                self.connection.rollback.assert_called_once()

    def test_other_buyers_cannot_read_order_or_driver_location(self):
        for path in ('/buyer/orders/42', '/buyer/orders/42/location'):
            self.cursor.fetchone.side_effect = [BUYER.copy(), None]
            response = self.client.get(path)
            self.assertEqual(response.status_code, 404)
            self.assertIn('o.buyer_id = %s', self.cursor.execute.call_args.args[0])
            self.assertEqual(self.cursor.execute.call_args.args[1], (42, 7))

    def test_completed_orders_do_not_expose_driver_location(self):
        for state in ('DELIVERED', 'CANCELLED'):
            self.cursor.fetchone.side_effect = [BUYER.copy(), dict(status=state, current_latitude=23,
                current_longitude=77, availability='ONLINE', location_age_seconds=0)]
            response = self.client.get('/buyer/orders/42/location')
            self.assertEqual(response.status_code, 200)
            self.assertIsNone(response.json['location'])
            self.assertEqual(response.headers['Cache-Control'], 'no-store')

    def test_browse_search_and_supplier_are_bound_parameters(self):
        self.cursor.fetchone.side_effect = [BUYER.copy()]
        self.cursor.fetchall.return_value = []
        response = self.client.get('/browse-products?q=Tomato&supplier=9')
        self.assertEqual(response.status_code, 200)
        query, params = self.cursor.execute.call_args.args
        self.assertNotIn('Tomato', query)
        self.assertIn('%Tomato%', params)
        self.assertIn(9, params)

    def test_dashboard_and_orders_render_real_records_and_empty_states(self):
        stats = dict(total_orders=1, pending_orders=1, spending=0, order_value=150,
                     product_value=100, delivery_value=50, active_suppliers=1)
        order = dict(order_id=42, crop_name='Test produce', quantity=5, farmer_name='Test supplier',
                     total_amount=150, status='PENDING_LOGISTICS')
        self.cursor.fetchone.side_effect = [BUYER.copy(), stats]
        self.cursor.fetchall.side_effect = [[order], [], [], []]
        response = self.client.get('/buyer')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Test produce', response.data)
        self.assertNotIn(b'Green Valley FPO', response.data)
        self.cursor.fetchone.side_effect = [BUYER.copy()]
        self.cursor.fetchall.side_effect = [[]]
        response = self.client.get('/buyer/orders')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'No orders yet', response.data)


if __name__ == '__main__':
    unittest.main()
