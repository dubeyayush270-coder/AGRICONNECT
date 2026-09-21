"""Public-page and sign-in integration checks with an isolated database double."""
from html.parser import HTMLParser
import unittest
from unittest.mock import MagicMock, patch

from test_logistics import module


class PageElements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.elements = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


class PublicPageTests(unittest.TestCase):
    def setUp(self):
        self.client = module.app.test_client()
        self.database = MagicMock()
        self.cursor = self.database.cursor.return_value
        self.cursor.fetchone.return_value = None
        patcher = patch.object(module, 'db', self.database)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_home_and_login_render_without_database_queries(self):
        for path, page_class in (('/', 'annasetu-home'), ('/login', 'annasetu-login')):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn(f'class="{page_class}"', response.text)
        self.database.cursor.assert_not_called()

    def test_public_navigation_and_assets_resolve(self):
        for path in ('/', '/login'):
            page = PageElements(self.client.get(path).text)
            ids = {attrs['id'] for _, attrs in page.elements if 'id' in attrs}
            links = {attrs['href'] for _, attrs in page.elements if 'href' in attrs}
            self.assertIn('/register', links)
            self.assertIn('/login' if path == '/' else '/', links)
            for tag, attrs in page.elements:
                target = attrs.get('src') or attrs.get('href', '')
                if target.startswith('/static/'):
                    with self.subTest(asset=target):
                        self.assertEqual(self.client.get(target).status_code, 200)
                elif target.startswith('#'):
                    self.assertIn(target[1:], ids)
        login = PageElements(self.client.get('/login').text)
        self.assertTrue(any(tag == 'form' and attrs.get('action') == '/login'
                            and attrs.get('method') == 'post' for tag, attrs in login.elements))
        self.assertIn('/forgot-password', {attrs.get('href') for _, attrs in login.elements})

    def test_valid_sign_in_preserves_sessions_and_role_destinations(self):
        for role, destination in (('farmer', '/seller'), ('buyer', '/buyer'),
                                  ('Logistics', '/logistics'), ('Admin', '/admin')):
            with self.subTest(role=role):
                client = module.app.test_client()
                self.cursor.fetchone.return_value = {'id': 7, 'role': role}
                response = client.post('/login', data={'login_id': 'user@example.test',
                                                       'password': 'test-password'})
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.location, destination)
                with client.session_transaction() as session:
                    self.assertEqual(session['user_id'], 7)
                    self.assertEqual(session['role'], role)
                self.assertEqual(self.cursor.execute.call_args.args[1],
                                 ('user@example.test', 'test-password'))

    def test_failed_sign_in_keeps_identifier_escaped_and_password_empty(self):
        identifier = '\"><script>alert(1)</script>@example.test'
        response = self.client.post('/login', data={'login_id': identifier,
                                                    'password': 'never-render-this-password'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('Wrong email or password', response.text)
        self.assertIn('role="alert"', response.text)
        self.assertNotIn('<script>alert(1)</script>', response.text)
        self.assertNotIn('never-render-this-password', response.text)
        inputs = {attrs['name']: attrs for tag, attrs in PageElements(response.text).elements
                  if tag == 'input' and 'name' in attrs}
        self.assertEqual(inputs['login_id']['value'], identifier)
        self.assertNotIn('value', inputs['password'])
        with self.client.session_transaction() as session:
            self.assertNotIn('user_id', session)

    def test_legacy_root_sign_in_submissions_still_work(self):
        self.cursor.fetchone.return_value = {'id': 7, 'role': 'buyer'}
        response = self.client.post('/', data={'login_id': 'buyer@example.test', 'password': 'test'})
        self.assertEqual(response.location, '/buyer')
        self.cursor.fetchone.return_value = None
        response = self.client.post('/', data={'login_id': 'wrong@example.test', 'password': 'test'})
        self.assertIn('class="annasetu-login"', response.text)
        self.assertIn('Wrong email or password', response.text)

    def test_protected_pages_send_anonymous_visitors_to_login(self):
        for path in ('/seller', '/seller/orders', '/my-products', '/add-product',
                     '/demand-forecast', '/government-schemes', '/buyer', '/buyer/orders',
                     '/logistics', '/logistics/available-requests'):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.location, '/login')
        self.database.cursor.assert_not_called()

    def test_registration_and_recovery_link_back_to_login(self):
        for path in ('/register', '/forgot-password'):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                links = {attrs.get('href') for _, attrs in PageElements(response.text).elements}
                self.assertIn('/login', links)


if __name__ == '__main__':
    unittest.main()
