"""Focused driver lifecycle and access checks; database is mocked."""
import unittest
from unittest.mock import MagicMock, patch

import mysql.connector
from test_logistics import module, profile


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.client = module.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user_id=7, delivery_csrf="delivery-test-token")
        self.connection = MagicMock()
        self.cursor = self.connection.cursor.return_value
        self.connect = patch.object(module.mysql.connector, "connect", return_value=self.connection).start()
        self.addCleanup(patch.stopall)

    def ready(self, status="LOGISTICS_ASSIGNED", **overrides):
        self.cursor.fetchone.side_effect = [dict(role="Logistics"), profile(**overrides),
                                           dict(order_id=42, status=status)]

    def post(self, status, **extra):
        return self.client.post('/logistics/deliveries/42/status',
                                json=dict(status=status, csrf_token="delivery-test-token", **extra))

    def writes(self):
        return [call for call in self.cursor.execute.call_args_list if call.args[0].strip().startswith("UPDATE")]

    def test_authentication_role_csrf_and_method_are_required(self):
        self.assertEqual(module.app.test_client().get('/logistics/deliveries').status_code, 401)
        self.assertEqual(self.client.get('/logistics/deliveries/42/status').status_code, 405)
        self.cursor.fetchone.side_effect = [dict(role="buyer")]
        self.assertEqual(self.post('PICKED_UP').status_code, 403)
        for token in (None, "wrong", []):
            self.cursor.fetchone.side_effect = [dict(role="Logistics")]
            response = self.client.post('/logistics/deliveries/42/status', json=dict(status='PICKED_UP', csrf_token=token))
            self.assertEqual(response.status_code, 403)
        self.assertEqual(self.writes(), [])

    def test_only_assigned_driver_can_update(self):
        self.cursor.fetchone.side_effect = [dict(role="Logistics"), profile(), None]
        self.assertEqual(self.post('PICKED_UP').status_code, 404)
        self.assertEqual(self.cursor.execute.call_args.args[1], (42, 4))
        self.assertIn('assigned_logistics_id = %s FOR UPDATE', self.cursor.execute.call_args.args[0])
        self.assertEqual(self.writes(), [])

    def test_valid_steps_commit_without_changing_stock_or_charges(self):
        for current, target, column in [('LOGISTICS_ASSIGNED','PICKED_UP','picked_up_at'),
                                        ('PICKED_UP','IN_TRANSIT','in_transit_at'),
                                        ('IN_TRANSIT','DELIVERED','delivered_at')]:
            self.connection.reset_mock()
            self.ready(current, availability='OFFLINE')
            response = self.post(target)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json['changed'])
            self.assertEqual(len(self.writes()), 1)
            sql, params = self.writes()[0].args
            self.assertIn(column + ' = CURRENT_TIMESTAMP', sql)
            self.assertEqual(params, (target, 42, 4))
            self.connection.commit.assert_called_once()
            self.assertEqual(response.headers['Cache-Control'], 'no-store')

    def test_skipping_reversing_or_reopening_steps_is_rejected(self):
        for current, target in [('LOGISTICS_ASSIGNED','DELIVERED'), ('PICKED_UP','DELIVERED'),
                                ('IN_TRANSIT','PICKED_UP'), ('DELIVERED','IN_TRANSIT'),
                                ('CANCELLED','PICKED_UP'), ('PENDING_LOGISTICS','PICKED_UP')]:
            self.cursor.reset_mock()
            self.ready(current)
            self.assertEqual(self.post(target).status_code, 409)
            self.assertEqual(self.writes(), [])

    def test_repeated_status_does_not_change_timestamp(self):
        for status in ('PICKED_UP','IN_TRANSIT','DELIVERED'):
            self.cursor.reset_mock()
            self.ready(status)
            response = self.post(status)
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json['changed'])
            self.assertEqual(self.writes(), [])

    def test_malformed_json_and_status_are_rejected(self):
        for data in ([], None, {'status': []}, {'status':'CANCELLED'}):
            self.cursor.fetchone.side_effect = [dict(role='Logistics')]
            if isinstance(data, dict): data['csrf_token'] = 'delivery-test-token'
            self.assertEqual(self.client.post('/logistics/deliveries/42/status', json=data).status_code, 400)
        self.assertEqual(self.writes(), [])

    def test_commit_failure_rolls_back_without_claiming_success(self):
        self.ready()
        self.connection.commit.side_effect = mysql.connector.Error('test commit failure')
        with self.assertLogs(module.app.logger, level='ERROR'):
            response = self.post('PICKED_UP')
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json['success'])
        self.connection.rollback.assert_called_once()

    def test_active_list_has_next_actions_pagination_and_csrf(self):
        self.cursor.fetchone.side_effect = [dict(role='Logistics'), profile(),
                                          dict(active_count=21, completed_count=0, completed_delivery_estimate=0)]
        self.cursor.fetchall.return_value = [dict(order_id=i, status='LOGISTICS_ASSIGNED') for i in range(21)]
        response = self.client.get('/logistics/deliveries')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json['deliveries']), 20)
        self.assertTrue(response.json['has_next'])
        self.assertEqual(response.json['deliveries'][0]['next_status'], 'PICKED_UP')
        self.assertEqual(response.json['csrf_token'], 'delivery-test-token')

    def test_history_and_empty_profile_are_supported(self):
        self.cursor.fetchone.side_effect = [dict(role='Logistics'), None]
        response = self.client.get('/logistics/deliveries?view=history')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['deliveries'], [])
        self.assertEqual(response.json['summary']['completed_count'], 0)


if __name__ == '__main__':
    unittest.main()
