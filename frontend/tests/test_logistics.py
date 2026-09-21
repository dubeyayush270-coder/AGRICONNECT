"""Logistics HTTP regressions with real Flask and an isolated database double.

Run from frontend: python -B -m unittest discover -s tests -v
No live database, email, forecast training, or upload writes are performed.
"""
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

import mysql.connector
import flask  # Load Flask/Jinja before patch.dict restores the module registry.


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
forecast_stub = types.ModuleType("forecast")
forecast_stub.forecast_demand = MagicMock()
spec = importlib.util.spec_from_file_location("agriconnect_test_app", ROOT / "app.py")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
with patch.dict(sys.modules, {"forecast": forecast_stub}), \
        patch("mysql.connector.connect"), patch("os.makedirs"):
    spec.loader.exec_module(module)
module.app.config.update(TESTING=True, SECRET_KEY="logistics-tests-only")


def profile(**overrides):
    result = dict(logistics_id=4, vehicle_number="TEST 123", vehicle_type="Pickup",
                  vehicle_capacity=1000, availability="ONLINE", current_latitude=0,
                  current_longitude=0, location_age_seconds=0)
    result.update(overrides)
    return result


class LogisticsTests(unittest.TestCase):
    def setUp(self):
        self.client = module.app.test_client()
        with self.client.session_transaction() as session:
            session["user_id"] = 7
        self.connection = MagicMock()
        self.cursor = self.connection.cursor.return_value
        self.cursor.fetchone.side_effect = [{"role": "Logistics"}, profile()]
        self.connect = patch.object(module.mysql.connector, "connect", return_value=self.connection).start()
        self.addCleanup(patch.stopall)

    def test_location_is_scoped_to_signed_in_driver_and_not_cached(self):
        response = self.client.get("/logistics/location?user_id=99")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["location"], {"latitude": 0.0, "longitude": 0.0})
        self.assertTrue(response.json["is_live"])
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        for call in self.cursor.execute.call_args_list:
            self.assertEqual(call.args[1], (7,))
        self.connection.commit.assert_called_once()
        self.cursor.close.assert_called_once()
        self.connection.close.assert_called_once()

    def test_anonymous_requests_do_not_connect(self):
        client = module.app.test_client()
        for method, path in [("get", "/logistics/location"), ("post", "/logistics/update-location"),
                             ("post", "/logistics/toggle-availability"), ("post", "/logistics/update-profile")]:
            with self.subTest(path=path):
                response = getattr(client, method)(path)
                self.assertEqual(response.status_code, 401)
        self.connect.assert_not_called()

    def test_other_roles_cannot_read_or_change_location(self):
        for path, method in [("/logistics/location", "get"), ("/logistics/update-location", "post"),
                             ("/logistics/toggle-availability", "post")]:
            with self.subTest(path=path):
                self.cursor.fetchone.side_effect = [{"role": "buyer"}]
                response = getattr(self.client, method)(path, json={"latitude": 1, "longitude": 1})
                self.assertEqual(response.status_code, 403)

    def test_deleted_user_session_is_cleared(self):
        self.cursor.fetchone.side_effect = [None]
        self.assertEqual(self.client.get("/logistics/location").status_code, 401)
        with self.client.session_transaction() as session:
            self.assertNotIn("user_id", session)

    def test_no_profile_is_a_useful_empty_state(self):
        self.cursor.fetchone.side_effect = [{"role": "Logistics"}, None]
        result = self.client.get("/logistics/location").json
        self.assertFalse(result["profile_exists"])
        self.assertIsNone(result["location"])
        self.assertFalse(result["is_live"])

    def test_live_freshness_and_offline_states(self):
        for age, availability, latitude, expected in [
            (0, "ONLINE", 0, True), (60, "ONLINE", 0, True),
            (61, "ONLINE", 0, False), (None, "ONLINE", 0, False),
            (-1, "ONLINE", 0, False), (0, "OFFLINE", 0, False),
            (0, "ONLINE", None, False),
        ]:
            with self.subTest(age=age, availability=availability, latitude=latitude):
                self.cursor.fetchone.side_effect = [{"role": "Logistics"}, profile(
                    location_age_seconds=age, availability=availability, current_latitude=latitude)]
                self.assertEqual(self.client.get("/logistics/location").json["is_live"], expected)

    def test_location_rejects_bad_json_shapes_and_bad_coordinates(self):
        for payload in [[], [1], "text", True, {}, {"latitude": 1},
                        {"latitude": True, "longitude": 1},
                        {"latitude": "nan", "longitude": 1},
                        {"latitude": 1, "longitude": "inf"},
                        {"latitude": 91, "longitude": 1},
                        {"latitude": 1, "longitude": -181},
                        {"latitude": [], "longitude": 1}]:
            with self.subTest(payload=payload):
                self.cursor.reset_mock()
                self.cursor.fetchone.side_effect = [{"role": "Logistics"}]
                response = self.client.post("/logistics/update-location", json=payload)
                self.assertEqual(response.status_code, 400)
                self.assertFalse(any(call.args[0].lstrip().startswith("UPDATE") for call in self.cursor.execute.call_args_list))

    def test_malformed_json_is_400(self):
        self.cursor.fetchone.side_effect = [{"role": "Logistics"}]
        response = self.client.post("/logistics/update-location", data="{", content_type="application/json")
        self.assertEqual(response.status_code, 400)

    def test_stationary_vehicle_heartbeat_succeeds_when_changed_rows_is_zero(self):
        self.cursor.rowcount = 0
        response = self.client.post("/logistics/update-location", json={"latitude": 0, "longitude": 0})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["last_seen_seconds"], 0)
        self.assertTrue(response.json["is_live"])
        query, values = self.cursor.execute.call_args.args
        self.assertIn("location_updated_at = CURRENT_TIMESTAMP", query)
        self.assertEqual(values, (0.0, 0.0, 7))

    def test_missing_profile_cannot_receive_locations(self):
        self.cursor.fetchone.side_effect = [{"role": "Logistics"}, None]
        response = self.client.post("/logistics/update-location", json={"latitude": 1, "longitude": 1})
        self.assertEqual(response.status_code, 404)

    def test_online_requires_fresh_location(self):
        for stored in [profile(location_age_seconds=61), profile(current_latitude=None),
                       profile(location_age_seconds=None)]:
            with self.subTest(profile=stored):
                self.cursor.fetchone.side_effect = [{"role": "Logistics"}, stored]
                response = self.client.post("/logistics/toggle-availability", json={"availability": "ONLINE"})
                self.assertEqual(response.status_code, 400)

    def test_fresh_fix_can_go_online_and_stale_vehicle_can_go_offline(self):
        for target, age in [("ONLINE", 0), ("OFFLINE", 120)]:
            with self.subTest(target=target):
                self.cursor.fetchone.side_effect = [{"role": "Logistics"}, profile(location_age_seconds=age)]
                response = self.client.post("/logistics/toggle-availability", json={"availability": target})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json["availability"], target)

    def test_database_failure_rolls_back_closes_and_returns_json(self):
        self.cursor.execute.side_effect = mysql.connector.Error("simulated database failure")
        with self.assertLogs(module.app.logger, level="ERROR"):
            response = self.client.post("/logistics/update-location", json={"latitude": 1, "longitude": 1})
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json["success"])
        self.assertNotIn("simulated", response.json["message"])
        self.connection.rollback.assert_called_once()
        self.cursor.close.assert_called_once()
        self.connection.close.assert_called_once()

    def test_commit_failure_does_not_report_success(self):
        self.connection.commit.side_effect = mysql.connector.Error("commit failed")
        with self.assertLogs(module.app.logger, level="ERROR"):
            response = self.client.post("/logistics/update-location", json={"latitude": 1, "longitude": 1})
        self.assertEqual(response.status_code, 503)
        self.connection.rollback.assert_called_once()
        self.connection.close.assert_called_once()

    def test_each_request_gets_a_separate_connection(self):
        connections = [MagicMock(), MagicMock()]
        for connection in connections:
            connection.cursor.return_value.fetchone.side_effect = [{"role": "Logistics"}, profile()]
        self.connect.side_effect = connections
        self.assertEqual(self.client.get("/logistics/location").status_code, 200)
        self.assertEqual(self.client.get("/logistics/location").status_code, 200)
        for connection in connections:
            connection.close.assert_called_once()

    def test_vehicle_capacity_must_be_finite_and_positive(self):
        for capacity in ["nan", "inf", "0", "-1"]:
            with self.subTest(capacity=capacity):
                self.cursor.fetchone.side_effect = [{"role": "Logistics"}]
                response = self.client.post("/logistics/update-profile", data={
                    "vehicle_number": "TEST 123", "vehicle_type": "Pickup", "vehicle_capacity": capacity,
                })
                self.assertEqual(response.status_code, 400)

    def test_vehicle_create_and_edit_return_saved_details_after_commit(self):
        for existing_profile in (None, profile()):
            with self.subTest(existing_profile=existing_profile is not None):
                self.connection.commit.reset_mock()
                self.cursor.fetchone.side_effect = [{"role": "Logistics"}, existing_profile]
                response = self.client.post("/logistics/update-profile", data={
                    "vehicle_number": "  TEST 456  ", "vehicle_type": "Truck", "vehicle_capacity": "2000.50",
                })
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.json["success"])
                self.assertEqual(response.json["vehicle"], {
                    "vehicle_number": "TEST 456", "vehicle_type": "Truck", "vehicle_capacity": 2000.5,
                })
                self.connection.commit.assert_called_once()

    def test_dashboard_renders_serialized_initial_location_and_controls(self):
        self.cursor.fetchone.side_effect = [dict(id=7, name="Test Driver", role="Logistics"), profile()]
        response = self.client.get("/logistics")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'"latitude": 0.0', response.data)
        self.assertIn(b'id="trackingButton"', response.data)
        self.assertIn(b'/static/logistics.js', response.data)


if __name__ == "__main__":
    unittest.main()
