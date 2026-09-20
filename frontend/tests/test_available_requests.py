"""Focused route regressions; the companion MariaDB integration test uses real transactions."""
import unittest
from unittest.mock import MagicMock, patch
import mysql.connector
from test_logistics import module, profile


class AvailableRequestTests(unittest.TestCase):
    def setUp(self):
        self.client = module.app.test_client()
        with self.client.session_transaction() as session:
            session['user_id'] = 7
        self.connection = MagicMock()
        self.cursor = self.connection.cursor.return_value
        self.connect = patch.object(module.mysql.connector, 'connect', return_value=self.connection).start()
        self.addCleanup(patch.stopall)

    def ready(self, **overrides):
        self.cursor.fetchone.side_effect = [
            {'role': 'Logistics'}, profile(**overrides),
            dict(order_id=1, status='PENDING_LOGISTICS', assigned_logistics_id=None, quantity=100),
            dict(request_id=1, status='PENDING'),
        ]

    def test_anonymous_order_actions_do_not_connect(self):
        client = module.app.test_client()
        for action in ('accept', 'reject'):
            self.assertEqual(client.post(f'/logistics/available-requests/1/{action}').location, '/')
            self.assertEqual(client.get(f'/logistics/available-requests/1/{action}').status_code, 405)
        self.connect.assert_not_called()

    def test_accept_connection_failure_is_handled(self):
        self.connect.side_effect = mysql.connector.Error('test connection failure')
        with self.assertLogs(module.app.logger, level='ERROR'):
            response = self.client.post('/logistics/available-requests/1/accept')
        self.assertEqual(response.status_code, 302)
        self.assertIn('Unable+to+accept', response.location)

    def test_accept_commit_failure_never_reports_success(self):
        self.ready()
        self.connection.commit.side_effect = mysql.connector.Error('test commit failure')
        with self.assertLogs(module.app.logger, level='ERROR'):
            response = self.client.post('/logistics/available-requests/1/accept')
        self.assertIn('Unable+to+accept', response.location)
        self.connection.rollback.assert_called_once()
        self.connection.close.assert_called_once()

    def test_accept_write_failure_rolls_back_entire_assignment(self):
        self.ready()
        def execute(query, params):
            if "status = 'EXPIRED'" in query:
                raise mysql.connector.Error('test invitation write failure')
        self.cursor.execute.side_effect = execute
        with self.assertLogs(module.app.logger, level='ERROR'):
            response = self.client.post('/logistics/available-requests/1/accept')
        self.assertIn('Unable+to+accept', response.location)
        self.connection.commit.assert_not_called()
        self.connection.rollback.assert_called_once()

    def test_ineligible_accept_does_not_write(self):
        for overrides in ({'availability': 'OFFLINE'}, {'vehicle_capacity': 99}):
            self.cursor.reset_mock()
            self.ready(**overrides)
            response = self.client.post('/logistics/available-requests/1/accept')
            self.assertNotIn('successfully', response.location)
            self.assertFalse(any(call.args[0].lstrip().startswith('UPDATE') for call in self.cursor.execute.call_args_list))

    def test_buyer_cannot_accept(self):
        self.cursor.fetchone.side_effect = [{'role': 'buyer'}]
        self.assertEqual(self.client.post('/logistics/available-requests/1/accept').status_code, 403)

    def test_reject_noop_does_not_report_success(self):
        self.cursor.fetchone.side_effect = [{'logistics_id': 4}]
        self.cursor.rowcount = 0
        response = self.client.post('/logistics/available-requests/1/reject')
        self.assertIn('no+longer+available', response.location)

    def test_dashboard_script_exists_and_is_served(self):
        response = self.client.get('/static/logistics.js')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'changeAvailability', response.data)
        response.close()


if __name__ == '__main__':
    unittest.main()
