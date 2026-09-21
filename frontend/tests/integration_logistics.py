"""Opt-in HTTP + MariaDB checks using temporary records, with exact-row cleanup.

Run: python -B tests/integration_logistics.py --run-live --artifacts <scratch-directory>
Existing users/products are read only. Temporary orders reference the existing
buyer/product without changing inventory. AUTO_INCREMENT counters may advance.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout, redirect_stderr
import hashlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import traceback

import mysql.connector
import requests
from werkzeug.serving import make_server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-live', action='store_true', required=True)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--artifacts', type=Path, required=True)
    parser.add_argument('--browser-script', type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    artifacts = args.artifacts.resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    os.chdir(root)
    sys.path.insert(0, str(root))
    spec = importlib.util.spec_from_file_location('logistics_live_app', root / 'app.py')
    module = importlib.util.module_from_spec(spec)
    with (artifacts / 'startup.log').open('w', encoding='utf-8') as log, redirect_stdout(log), redirect_stderr(log):
        spec.loader.exec_module(module)
    app = module.app
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    db = mysql.connector.connect(**module.DATABASE_CONFIG, autocommit=True)
    cursor = db.cursor(dictionary=True)
    users, drivers, orders = [], [], []
    report = {'root': str(root), 'checks': [], 'fixture_ids': {}, 'cleanup': None}
    server = None
    auth_file = artifacts / 'browser-auth.json'
    tables = ('users', 'products', 'orders', 'logistics_profiles', 'logistics_order_requests')

    def query(sql, params=()):
        cursor.execute(sql, params)
        return cursor.fetchall() if cursor.with_rows else cursor.lastrowid

    def snapshot():
        return {table: hashlib.sha256(json.dumps(query('SELECT * FROM ' + table + ' ORDER BY 1'), default=str, sort_keys=True).encode()).hexdigest() for table in tables}

    baseline = snapshot()
    baseline_counts = {t: query('SELECT COUNT(*) AS n FROM ' + t)[0]['n'] for t in tables}
    report['baseline_counts'] = baseline_counts

    def check(name, function):
        try:
            detail = function()
            result = {'name': name, 'passed': True, 'detail': detail}
        except Exception as exc:
            result = {'name': name, 'passed': False, 'error': str(exc), 'traceback': traceback.format_exc()}
        report['checks'].append(result)
        print(('PASS ' if result['passed'] else 'FAIL ') + name + (': ' + result.get('error', '') if not result['passed'] else ''), flush=True)

    def expect(condition, message):
        if not condition:
            raise AssertionError(message)

    def session_for(user_id):
        client = requests.Session()
        cookie = app.session_interface.get_signing_serializer(app).dumps({'user_id': user_id})
        client.cookies.set(app.config['SESSION_COOKIE_NAME'], cookie, domain='127.0.0.1', path='/')
        return client, cookie

    def get(client, path):
        return client.get(base + path, timeout=15, allow_redirects=False)

    def post(client, path, **kwargs):
        return client.post(base + path, timeout=20, allow_redirects=False, **kwargs)

    def order_state(order_id):
        return query('SELECT * FROM orders WHERE order_id=%s', (order_id,))[0]

    def invitations(order_id):
        return {r['logistics_id']: r for r in query('SELECT * FROM logistics_order_requests WHERE order_id=%s ORDER BY logistics_id', (order_id,))}

    def visible(client, order_id):
        response = get(client, '/logistics/available-requests')
        expect(response.status_code == 200, 'Request page did not render: ' + str(response.status_code))
        expect(response.headers.get('Cache-Control') == 'no-store', 'Request page was cacheable')
        return f'/logistics/available-requests/{order_id}/accept' in response.text

    def new_order(invite_indices=(0, 1, 2)):
        order_id = query('''INSERT INTO orders
            (product_id,buyer_id,farmer_id,quantity,product_price,product_total,
             pickup_village_city,pickup_district,pickup_state,pickup_address,pickup_latitude,pickup_longitude,
             delivery_house_no,delivery_street,delivery_village_city,delivery_pincode,delivery_district,
             delivery_state,delivery_address,delivery_latitude,delivery_longitude,distance_km,
             estimated_logistics_cost,total_amount,status)
             VALUES (%s,%s,%s,100,%s,%s,'Test Pickup','Test District','Test State',%s,0,0,
                     'TEST','Test Street','Test Delivery','000000','Test District','Test State',%s,
                     0.01,0.01,2,50,%s,'PENDING_LOGISTICS')''',
             (product['product_id'], buyer['id'], product['user_id'], product['price_per_kg'],
              product['price_per_kg'] * 100, marker, marker, product['price_per_kg'] * 100 + 50))
        orders.append(order_id)
        for index in invite_indices:
            query("INSERT INTO logistics_order_requests(order_id,logistics_id,status) VALUES(%s,%s,'PENDING')", (order_id, drivers[index]))
        return order_id

    try:
        product = query('SELECT product_id,user_id,crop_name,price_per_kg FROM products ORDER BY product_id LIMIT 1')[0]
        buyer = query("SELECT id FROM users WHERE role='buyer' ORDER BY id LIMIT 1")[0]
        report['reference_records'] = {'product_id': product['product_id'], 'crop_name': product['crop_name'], 'farmer_id': product['user_id'], 'buyer_id': buyer['id']}
        marker = 'AGRICONNECT CONTROLLED TEST ' + secrets.token_hex(8)
        clients, cookies = [], []
        for index in range(4):
            uid = query('''INSERT INTO users(name,email,phone,role,state,district,market,password)
                VALUES(%s,%s,%s,'Logistics','Test State','Test District','Test Market',%s)''',
                (f'Controlled Test Driver {index+1}', secrets.token_hex(12)+'@example.invalid',
                 str(secrets.randbelow(9000000000)+1000000000), secrets.token_urlsafe(40)))
            users.append(uid)
            drivers.append(query('''INSERT INTO logistics_profiles
                (user_id,vehicle_number,vehicle_type,vehicle_capacity,availability)
                VALUES(%s,%s,'Pickup',200,'OFFLINE')''', (uid, 'TEST-' + secrets.token_hex(5))))
            client, cookie = session_for(uid)
            clients.append(client)
            cookies.append(cookie)
        report['fixture_ids'] = {'users': users, 'drivers': drivers, 'orders': orders}
        server = make_server('127.0.0.1', 5055, app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = 'http://127.0.0.1:5055'

        def startup():
            expect(get(clients[0], '/logistics').status_code == 200, 'Dashboard startup failed')
            expect(get(clients[0], '/static/logistics.css').status_code == 200, 'CSS missing')
            expect(get(clients[0], '/static/logistics.js').status_code == 200, 'JavaScript missing (404)')
        check('Clean Flask startup and dashboard/static HTTP responses', startup)

        def access():
            anon = requests.Session()
            buyer_client, _ = session_for(buyer['id'])
            for path in ('/logistics', '/logistics/available-requests'):
                expect(get(anon, path).status_code == 302, 'Anonymous page access allowed')
                expect(get(buyer_client, path).status_code == 403, 'Buyer page access allowed')
            for action in ('accept','reject'):
                expect(post(anon, '/logistics/available-requests/0/'+action).headers['Location'] == '/login', 'Anonymous action allowed')
                expect(get(clients[0], '/logistics/available-requests/0/'+action).status_code == 405, 'GET mutated order')
        check('Login/role protection and POST-only Accept/Reject', access)

        def gps():
            for client in clients:
                response = post(client, '/logistics/toggle-availability', json={'availability':'ONLINE'})
                expect(response.status_code == 400, 'ONLINE allowed without GPS')
                expect(post(client, '/logistics/update-location', json={'latitude':91,'longitude':0}).status_code == 400, 'Invalid GPS accepted')
                expect(post(client, '/logistics/update-location', json={'latitude':0,'longitude':0}).status_code == 200, 'Valid zero GPS rejected')
                response = post(client, '/logistics/toggle-availability', json={'availability':'ONLINE'})
                expect(response.json()['is_live'] is True, 'Fresh GPS did not enable live ONLINE')
            query('UPDATE logistics_profiles SET location_updated_at=NOW()-INTERVAL 61 SECOND WHERE logistics_id=%s', (drivers[0],))
            expect(get(clients[0], '/logistics/location').json()['is_live'] is False, 'Stale GPS reported live')
            expect(post(clients[0], '/logistics/toggle-availability', json={'availability':'ONLINE'}).status_code == 400, 'Stale GPS allowed going online')
            post(clients[0], '/logistics/update-location', json={'latitude':0,'longitude':0})
        check('Fresh/missing/invalid/stale GPS and availability persistence', gps)

        core_order = new_order()
        def visibility():
            expect(all(visible(c, core_order) for c in clients[:3]), 'Eligible invited driver cannot see request')
            expect(not visible(clients[3], core_order), 'Uninvited driver sees request')
        check('Eligible ONLINE drivers see only their invitations', visibility)

        def uninvited():
            before = order_state(core_order), invitations(core_order)
            post(clients[3], f'/logistics/available-requests/{core_order}/accept')
            response = post(clients[3], f'/logistics/available-requests/{core_order}/reject')
            expect(before == (order_state(core_order), invitations(core_order)), 'Uninvited driver modified records')
            expect('rejected' not in response.headers['Location'], 'No-op Reject incorrectly reported success')
        check('Uninvited actions do not mutate data or claim rejection', uninvited)

        def reject():
            before = order_state(core_order), invitations(core_order)
            response = post(clients[0], f'/logistics/available-requests/{core_order}/reject')
            expect('rejected' in response.headers['Location'], 'Reject did not succeed')
            after = invitations(core_order)
            expect(order_state(core_order) == before[0], 'Reject modified the order')
            expect(after[drivers[0]]['status'] == 'REJECTED' and after[drivers[0]]['responded_at'] is not None, 'Reject did not update invitation')
            for d in drivers[1:3]:
                expect(after[d] == before[1][d], 'Reject touched another driver')
            expect(not visible(clients[0], core_order) and visible(clients[1], core_order), 'Reject visibility incorrect')
            post(clients[0], f'/logistics/available-requests/{core_order}/accept')
            expect(order_state(core_order) == before[0], 'Rejected invitation was accepted')
        check('Reject changes only one invitation; order stays PENDING_LOGISTICS', reject)

        def accept():
            response = post(clients[1], f'/logistics/available-requests/{core_order}/accept')
            expect('accepted+successfully' in response.headers['Location'], 'Accept failed: '+response.headers['Location'])
            order, invites = order_state(core_order), invitations(core_order)
            expect(order['status'] == 'LOGISTICS_ASSIGNED', 'Order status incorrect')
            expect(order['assigned_logistics_id'] == drivers[1] and order['logistics_assigned_at'] is not None, 'Assignment fields missing')
            expect(invites[drivers[1]]['status'] == 'ACCEPTED' and invites[drivers[1]]['responded_at'] is not None, 'Winning invitation incorrect')
            expect(invites[drivers[2]]['status'] == 'EXPIRED' and invites[drivers[2]]['responded_at'] is not None, 'Pending invitation not expired')
            expect(invites[drivers[0]]['status'] == 'REJECTED', 'Previously rejected invitation changed')
            expect(not any(visible(c, core_order) for c in clients), 'Assigned request still visible')
            report['confirmed_core_state'] = {'order_id': core_order, 'status': order['status'], 'assigned_logistics_id': order['assigned_logistics_id'], 'logistics_assigned_at': str(order['logistics_assigned_at']), 'invitations': {str(k):v['status'] for k,v in invites.items()}}
        check('Accept assigns atomically, expires competitors, and disappears for all drivers', accept)

        def replay():
            before = order_state(core_order), invitations(core_order)
            for client in clients[:3]:
                for action in ('accept','reject'):
                    post(client, f'/logistics/available-requests/{core_order}/{action}')
            expect(before == (order_state(core_order), invitations(core_order)), 'Repeated actions changed final state')
        check('Repeated/stale actions preserve assignment and invitation timestamps', replay)

        def eligibility(kind):
            order_id = new_order((3,))
            before = order_state(order_id), invitations(order_id)
            if kind == 'offline':
                post(clients[3], '/logistics/toggle-availability', json={'availability':'OFFLINE'})
            else:
                query('UPDATE logistics_profiles SET vehicle_capacity=50 WHERE logistics_id=%s', (drivers[3],))
            shown = visible(clients[3], order_id)
            post(clients[3], f'/logistics/available-requests/{order_id}/accept')
            unchanged = before == (order_state(order_id), invitations(order_id))
            query("UPDATE logistics_profiles SET availability='ONLINE',vehicle_capacity=200 WHERE logistics_id=%s", (drivers[3],))
            expect(not shown and unchanged, f'{kind} driver can see or accept request (visible={shown}, unchanged={unchanged})')
        check('OFFLINE driver cannot see or accept requests', lambda: eligibility('offline'))
        check('Undersized vehicle cannot see or accept requests', lambda: eligibility('capacity'))

        def race():
            for _ in range(10):
                order_id = new_order((1, 2))
                barrier = threading.Barrier(2)
                def racing_accept(index):
                    barrier.wait()
                    return post(clients[index], f'/logistics/available-requests/{order_id}/accept')
                with ThreadPoolExecutor(max_workers=2) as pool:
                    responses = list(pool.map(racing_accept, (1, 2)))
                expect(sum('accepted+successfully' in r.headers.get('Location','') for r in responses) == 1, 'Race did not have exactly one winner')
                expect(all('Unable+to+accept' not in r.headers.get('Location', '') for r in responses), 'Losing accept returned a database error instead of no-longer-available')
                order, invites = order_state(order_id), invitations(order_id)
                expect(sorted(r['status'] for r in invites.values()) == ['ACCEPTED','EXPIRED'], 'Race invitation states incorrect')
                expect(invites[order['assigned_logistics_id']]['status'] == 'ACCEPTED', 'Race winner mismatch')
        check('Simultaneous accepts produce exactly one winner and a clean loser response (10 races)', race)

        if args.browser_script:
            browser_order = new_order()
            for client in clients:
                post(client, '/logistics/toggle-availability', json={'availability':'OFFLINE'})
            auth_file.write_text(json.dumps({'base': base, 'cookies': cookies, 'order_id': browser_order, 'artifacts': str(artifacts)}), encoding='utf-8')
            def browser_check():
                result = subprocess.run(['node', str(args.browser_script.resolve()), str(auth_file)], capture_output=True, text=True, timeout=150)
                (artifacts / 'browser.log').write_text(result.stdout + result.stderr, encoding='utf-8')
                expect(result.returncode == 0, 'Browser tests failed: ' + result.stdout + result.stderr)
                state = order_state(browser_order), invitations(browser_order)
                expect(state[0]['status'] == 'LOGISTICS_ASSIGNED' and state[0]['assigned_logistics_id'] == drivers[1], 'Browser Accept did not persist assignment')
                expect([state[1][d]['status'] for d in drivers[:3]] == ['REJECTED','ACCEPTED','EXPIRED'], 'Browser invitation results incorrect')
                return result.stdout.strip()
            check('Real browser dashboard/GPS/Reject/Accept/other-driver refresh', browser_check)
    finally:
        if server:
            server.shutdown()
            server.server_close()
        # Delete only primary keys created by this run, in foreign-key order.
        if orders:
            query('DELETE FROM orders WHERE order_id IN (' + ','.join(['%s']*len(orders)) + ')', tuple(orders))
        if drivers:
            query('DELETE FROM logistics_profiles WHERE logistics_id IN (' + ','.join(['%s']*len(drivers)) + ')', tuple(drivers))
        if users:
            query('DELETE FROM users WHERE id IN (' + ','.join(['%s']*len(users)) + ')', tuple(users))
        auth_file.unlink(missing_ok=True)
        report['cleanup'] = {'all_original_rows_unchanged': snapshot() == baseline, 'final_counts': {t: query('SELECT COUNT(*) AS n FROM '+t)[0]['n'] for t in tables}}
        (artifacts / 'integration-results.json').write_text(json.dumps(report, indent=2, default=str), encoding='utf-8')
        print('CLEANUP', json.dumps(report['cleanup']), flush=True)
        cursor.close()
        db.close()
        module.db.close()
    return 0 if all(c['passed'] for c in report['checks']) and report['cleanup']['all_original_rows_unchanged'] else 1


if __name__ == '__main__':
    sys.exit(main())
