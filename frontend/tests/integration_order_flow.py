"""Real MariaDB checkout-to-delivery checks in an isolated temporary database.

Run from frontend: python -B tests/integration_order_flow.py --run-live
Only table definitions are read from the configured database. All fixtures and
orders live in a separate test schema, which is removed in finally.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import importlib.util
from pathlib import Path
import re
import secrets
import sys
import threading
import types
from unittest.mock import patch

import mysql.connector

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from migrations.add_delivery_timestamps import load_database_config, migrate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-live', action='store_true', required=True)
    parser.parse_args()
    source_config = load_database_config()
    source_db = source_config['database']
    test_db = 'agriconnect_flow_test_' + secrets.token_hex(8)
    if not re.fullmatch(r'agriconnect_flow_test_[0-9a-f]{16}', test_db) or test_db == source_db:
        raise RuntimeError('Invalid isolated test database name')
    admin = mysql.connector.connect(**source_config, autocommit=True)
    admin_cursor = admin.cursor()
    database = None
    app_connection = None
    created = False
    try:
        admin_cursor.execute(f'CREATE DATABASE `{test_db}`')
        created = True
        test_config = {**source_config, 'database': test_db}
        database = mysql.connector.connect(**test_config, autocommit=True)
        cursor = database.cursor(dictionary=True)
        tables = ('users', 'products', 'logistics_profiles', 'orders', 'logistics_order_requests')
        for table in tables:
            admin_cursor.execute('SHOW CREATE TABLE `' + source_db.replace('`', '``') + '`.`' + table + '`')
            ddl = admin_cursor.fetchone()[1]
            ddl = ddl.replace('REFERENCES `' + source_db.replace('`', '``') + '`.', 'REFERENCES `' + test_db + '`.')
            cursor.execute(ddl)
        migrate(database)
        assert migrate(database) == [], 'Migration must be safe to repeat'

        # Avoid forecast training, upload-directory writes and production connections on import.
        stub = types.ModuleType('forecast')
        stub.forecast_demand = lambda *args, **kwargs: None
        app_connection = mysql.connector.connect(**test_config)
        spec = importlib.util.spec_from_file_location('order_flow_app', ROOT / 'app.py')
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        with patch.dict(sys.modules, {'forecast': stub}), patch('os.makedirs'), \
                patch('mysql.connector.connect', return_value=app_connection):
            spec.loader.exec_module(module)
        module.DATABASE_CONFIG = test_config
        app = module.app
        app.config.update(TESTING=True, SECRET_KEY=secrets.token_hex(32))

        def sql(query, params=()):
            cursor.execute(query, params)
            return cursor.fetchall() if cursor.with_rows else cursor.lastrowid

        def new_user(name, role, index):
            return sql('''INSERT INTO users (name,email,phone,role,state,district,market,password)
                VALUES (%s,%s,%s,%s,'MP','Bhopal','Test market','not-a-login-password')''',
                       (name, f'fixture{index}@example.invalid', str(9000000000 + index), role))

        farmer = new_user('Fixture Farmer', 'Farmer', 1)
        buyer_id = new_user('Fixture Buyer', 'buyer', 2)
        other_buyer_id = new_user('Other Buyer', 'buyer', 3)
        driver_id = new_user('Assigned Driver', 'Logistics', 4)
        other_driver_id = new_user('Other Driver', 'Logistics', 5)
        product_id = sql('''INSERT INTO products
            (user_id,crop_name,quantity,quality,price_per_kg,state,district,
             pickup_address,pickup_village_city,pickup_state,pickup_district,pickup_latitude,pickup_longitude)
            VALUES (%s,'Fixture Tomato',10,'A',20,'MP','Bhopal','Fixture farm','Bhopal','MP','Bhopal',23,77)''', (farmer,))
        for user_id in (driver_id, other_driver_id):
            sql('''INSERT INTO logistics_profiles
                (user_id,vehicle_number,vehicle_type,vehicle_capacity,availability,
                 current_latitude,current_longitude,location_updated_at)
                VALUES (%s,%s,'Pickup',100,'ONLINE',23,77,CURRENT_TIMESTAMP)''', (user_id, f'TEST {user_id}'))

        def client(user_id):
            result = app.test_client()
            with result.session_transaction() as session:
                session.update(user_id=user_id, buyer_csrf='fixture-csrf')
            return result

        buyer, other_buyer = client(buyer_id), client(other_buyer_id)
        driver, other_driver = client(driver_id), client(other_driver_id)
        data = dict(csrf_token='fixture-csrf', quantity='5', delivery_latitude='23.1', delivery_longitude='77.1',
                    delivery_house_no='1', delivery_street='Fixture road', delivery_village_city='Bhopal',
                    delivery_state='MP', delivery_district='Bhopal', delivery_pincode='462001')

        def quote(client, quantity='5'):
            form = {**data, 'quantity': quantity}
            response = client.post(f'/buyer/quote/{product_id}', data=form)
            assert response.status_code == 200, response.get_data(as_text=True)
            return {**form, 'quote_token': response.json['quote_token']}

        def stock():
            return sql('SELECT quantity FROM products WHERE product_id=%s', (product_id,))[0]['quantity']

        assert buyer.get(f'/buy-product/{product_id}').status_code == 200
        placed = buyer.post(f'/place-order/{product_id}', data=quote(buyer))
        assert placed.status_code == 303, placed.get_data(as_text=True)
        order_id = int(placed.location.split('/orders/')[1].split('?')[0])
        assert stock() == 5
        order = sql('SELECT * FROM orders WHERE order_id=%s', (order_id,))[0]
        assert order['product_total'] == Decimal('100.00')
        assert order['estimated_logistics_cost'] == order['distance_km'] * 15
        assert len(sql('SELECT * FROM logistics_order_requests WHERE order_id=%s', (order_id,))) == 2
        assert buyer.get('/buyer/orders').status_code == 200
        assert buyer.get(placed.location).status_code == 200
        print('PASS checkout, real totals, stock reservation, driver invitations and buyer order views')

        accepted = driver.post(f'/logistics/available-requests/{order_id}/accept')
        assert 'accepted+successfully' in accepted.location
        listing = driver.get('/logistics/deliveries').json
        assert listing['deliveries'][0]['order_id'] == order_id
        assert listing['deliveries'][0]['next_status'] == 'PICKED_UP'
        token = listing['csrf_token']
        assert buyer.get(f'/buyer/orders/{order_id}/location').json['is_live'] is True
        assert b'Assigned Driver' in buyer.get(f'/buyer/orders/{order_id}').data
        assert other_buyer.get(f'/buyer/orders/{order_id}/location').status_code == 404
        wrong_token = other_driver.get('/logistics/deliveries').json['csrf_token']
        assert other_driver.post(f'/logistics/deliveries/{order_id}/status',
            json=dict(status='PICKED_UP', csrf_token=wrong_token)).status_code == 404
        assert driver.post(f'/logistics/deliveries/{order_id}/status',
            json=dict(status='DELIVERED', csrf_token=token)).status_code == 409
        print('PASS assignment, buyer GPS, privacy, driver ownership and step ordering')

        for status, column in (('PICKED_UP','picked_up_at'), ('IN_TRANSIT','in_transit_at'), ('DELIVERED','delivered_at')):
            response = driver.post(f'/logistics/deliveries/{order_id}/status', json=dict(status=status, csrf_token=token))
            assert response.status_code == 200 and response.json['changed']
            saved = sql('SELECT * FROM orders WHERE order_id=%s', (order_id,))[0]
            assert saved['status'] == status and saved[column] is not None
            retry = driver.post(f'/logistics/deliveries/{order_id}/status', json=dict(status=status, csrf_token=token))
            assert retry.status_code == 200 and retry.json['changed'] is False
            assert sql('SELECT * FROM orders WHERE order_id=%s', (order_id,))[0][column] == saved[column]
            assert stock() == 5, 'Status updates must not reserve stock again'
        assert buyer.get(f'/buyer/orders/{order_id}/location').json['location'] is None
        assert driver.get('/logistics/deliveries').json['deliveries'] == []
        history = driver.get('/logistics/deliveries?view=history').json
        assert history['deliveries'][0]['status'] == 'DELIVERED'
        assert history['summary']['completed_count'] == 1
        assert Decimal(history['summary']['completed_delivery_estimate']) == order['estimated_logistics_cost']
        assert driver.get('/logistics').status_code == 200
        # Inspect real template context so totals are checked independently of concurrent design changes.
        from flask import template_rendered
        contexts = []
        def capture(sender, template, context, **kwargs): contexts.append(context)
        with template_rendered.connected_to(capture, app):
            assert buyer.get('/buyer').status_code == 200
        assert contexts[-1]['stats']['pending_orders'] == 0
        assert contexts[-1]['stats']['spending'] == order['total_amount']
        print('PASS complete lifecycle, timestamps, safe retries, history, totals and tracking shutdown')

        # Competing buyers must not oversell the final five kilograms.
        race_clients = (client(buyer_id), client(other_buyer_id))
        forms = [quote(c, '4') for c in race_clients]
        barrier = threading.Barrier(2)
        def buy(index):
            barrier.wait(timeout=10)
            return race_clients[index].post(f'/place-order/{product_id}', data=forms[index]).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(buy, range(2)))
        assert sorted(results) == [303, 400], results
        assert stock() == 1
        print('PASS competing buyers cannot oversell inventory')
        cursor.close()
    finally:
        if app_connection is not None: app_connection.close()
        if database is not None: database.close()
        if created:
            # Only this invocation's fixed-prefix, random test schema can reach DROP.
            if re.fullmatch(r'agriconnect_flow_test_[0-9a-f]{16}', test_db) and test_db != source_db:
                admin_cursor.execute(f'DROP DATABASE `{test_db}`')
                print('Temporary test database removed; existing order data was not modified.')
        admin_cursor.close()
        admin.close()


if __name__ == '__main__':
    main()
