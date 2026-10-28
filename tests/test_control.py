import os
from threading import Thread

import pytest
from werkzeug.serving import make_server
from wordpress_manager import create_app


class Boundary:
    def __init__(self):
        self.calls = []
    def call(self, op, **fields):
        self.calls.append((op, fields))
        if op == 'login':
            if fields['password'] != 'private-password':
                raise ValueError('Invalid credentials')
            return {'capability': 'session-capability'}
        if op == 'status':
            return {'jobs': []}
        if op == 'plan':
            return {'id': 'plan-1', 'digest': 'a' * 64, 'packages': {'nginx': '1.24'}, 'artifacts': [],
                    'preserved_php': ['php8.5-fpm'], 'notice': 'Preserve configuration'}
        return {'job': 'job-1'}


@pytest.fixture
def secured(observer):
    boundary = Boundary()
    app = create_app({'TESTING': True, 'AUTH_SETTINGS': {'secret_key': 'a' * 64, 'provisioning': True}, 'CONTROLLER': boundary}, observer)
    return app.test_client(), boundary, app


def csrf(client, **kwargs):
    with client.session_transaction(**kwargs) as state:
        return state['csrf']


def login(client):
    client.get('/login')
    return client.post('/login', data={'csrf': csrf(client), 'username': 'admin', 'password': 'private-password'})


def test_authentication_and_private_reads(secured):
    client, boundary, app = secured
    for path in ('/', '/api/snapshot', '/view/setup', '/setup/control'):
        assert client.get(path).status_code == 302
    assert client.get('/healthz').status_code == 200
    response = login(client)
    assert response.status_code == 302
    assert 'HttpOnly' in response.headers['Set-Cookie'] and 'SameSite=Strict' in response.headers['Set-Cookie']
    assert client.get('/setup/control').status_code == 200
    assert client.get('/').status_code == 200
    assert client.post('/logout', data={'csrf': csrf(client)}).status_code == 302
    assert client.get('/api/snapshot').status_code == 302


@pytest.mark.parametrize('origin', ['http://localhost:8080', 'http://127.0.0.1:8080'])
def test_same_origin_login_and_session_persistence(secured, origin):
    client, boundary, app = secured
    page = client.get('/login', base_url=origin)
    assert page.headers['Referrer-Policy'] == 'same-origin'
    previous_csrf = csrf(client, base_url=origin)
    response = client.post('/login', base_url=origin, headers={'Origin': origin},
                           data={'csrf': previous_csrf, 'username': 'admin', 'password': 'private-password'})
    assert response.status_code == 302 and response.headers['Location'] == '/'
    assert boundary.calls == [('login', {'password': 'private-password'})]
    with client.session_transaction(base_url=origin) as state:
        assert state['capability'] == 'session-capability'
        assert state.permanent
        assert state['csrf'] != previous_csrf
    cookie = client.get_cookie('wm_session', domain=origin.split('://')[1].split(':')[0])
    assert cookie.http_only and cookie.same_site == 'Strict'
    assert cookie.expires is not None
    # A new request context must authenticate from the signed cookie alone.
    resumed = app.test_client()
    resumed.set_cookie('wm_session', cookie.value, domain=cookie.domain)
    for path in ('/', '/api/snapshot', '/view/setup', '/setup/control'):
        assert resumed.get(path, base_url=origin).status_code == 200


@pytest.mark.parametrize('path', ['/login', '/logout', '/setup/control'])
@pytest.mark.parametrize('origin', [
    'null', 'https://evil.example', 'https://localhost:8080',
    'http://localhost:8081', 'http://127.0.0.1:8080',
    'http://localhost:8080.evil.example', 'http://localhost:8080/',
])
def test_post_requires_exact_origin_even_with_valid_csrf(secured, path, origin):
    client, boundary, app = secured
    client.get('/login', base_url='http://localhost:8080')
    if path != '/login':
        client.post('/login', base_url='http://localhost:8080', headers={'Origin': 'http://localhost:8080'},
                    data={'csrf': csrf(client), 'username': 'admin', 'password': 'private-password'})
    before = list(boundary.calls)
    token = csrf(client)
    response = client.post(path, base_url='http://localhost:8080', headers={'Origin': origin},
                           data={'csrf': token, 'username': 'admin', 'password': 'private-password',
                                 'action': 'plan', 'php': '8.3'})
    assert response.status_code == 403
    assert boundary.calls == before
    with client.session_transaction() as state:
        assert state['csrf'] == token
        assert state.get('capability') == (None if path == '/login' else 'session-capability')


@pytest.mark.parametrize('token', ['', 'incorrect-token'])
def test_same_origin_does_not_bypass_csrf(secured, token):
    client, boundary, app = secured
    client.get('/login')
    response = client.post('/login', headers={'Origin': 'http://localhost'},
                           data={'csrf': token, 'username': 'admin', 'password': 'private-password'})
    assert response.status_code == 403
    assert not boundary.calls


def test_browser_login_form_origin_and_session(secured):
    channel = os.environ.get('WM_TEST_BROWSER')
    if not channel:
        pytest.skip('Set WM_TEST_BROWSER=chrome (installed Chrome) or chromium; install .[test,browser]')
    from playwright.sync_api import sync_playwright

    client, boundary, app = secured
    # Use an ephemeral loopback listener and the real template/response headers.
    server = make_server('127.0.0.1', 0, app, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f'http://127.0.0.1:{server.server_port}'
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=channel)
            try:
                page = browser.new_page(color_scheme='light')
                login_page = page.goto(origin + '/login')
                assert page.locator('html').get_attribute('data-theme') == 'light'
                assert page.get_by_role('button', name='Switch to dark mode').count() == 1
                page.get_by_role('button', name='Switch to dark mode').click()
                assert page.locator('html').get_attribute('data-theme') == 'dark'
                assert page.evaluate("localStorage.getItem('wm-theme')") == 'dark'
                assert page.evaluate('getComputedStyle(document.documentElement).backgroundColor') == 'rgb(17, 28, 33)'
                page.get_by_label('Password', exact=True).fill('private-password')
                with page.expect_response(lambda response: response.url == origin + '/login'
                                          and response.request.method == 'POST') as submitted:
                    page.get_by_role('button', name='Sign in', exact=True).click()
                response = submitted.value
                # Chrome must generate this header from a native form submission;
                # no fetch, request interception, or explicit Origin header is used.
                assert response.request.all_headers()['origin'] == origin
                assert response.status == 302
                assert login_page.headers['referrer-policy'] == 'same-origin'
                page.wait_for_url(origin + '/')
                assert page.locator('html').get_attribute('data-theme') == 'dark'
                assert page.goto(origin + '/api/snapshot').status == 200
                assert page.reload().status == 200
                assert page.locator('html').get_attribute('data-theme') == 'dark'
                assert page.locator('#theme-toggle').get_attribute('aria-pressed') == 'true'
                page.get_by_role('button', name='Switch to light mode').click()
                assert page.locator('html').get_attribute('data-theme') == 'light'
                assert page.evaluate("localStorage.getItem('wm-theme')") == 'light'
                assert page.reload().status == 200
                assert page.locator('html').get_attribute('data-theme') == 'light'
                assert boundary.calls == [('login', {'password': 'private-password'})]
            finally:
                browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_csrf_origin_and_method_boundaries(secured):
    client, boundary, app = secured
    client.get('/login')
    assert client.post('/login', data={'password': 'private-password'}).status_code == 403
    assert not boundary.calls
    login(client)
    before = list(boundary.calls)
    assert client.post('/setup/control', data={'csrf': csrf(client)}, headers={'Origin': 'https://evil.example'}).status_code == 403
    assert client.post('/setup/control', data={'action': 'apply'}).status_code == 403
    assert client.put('/setup/control').status_code == 405
    assert boundary.calls == before


def test_plan_preview_requires_separate_confirmation(secured):
    client, boundary, app = secured
    login(client)
    response = client.post('/setup/control', data={'csrf': csrf(client), 'action': 'plan', 'php': '8.3'})
    assert response.status_code == 200 and b'INSTALL MISSING COMPONENTS' in response.data
    assert b'php8.5-fpm' in response.data and b'1.24' in response.data
    assert not any(op == 'apply' for op, _ in boundary.calls)
    app.config['PROVISIONING_ENABLED'] = False
    assert client.post('/setup/control', data={'csrf': csrf(client), 'action': 'apply'}).status_code == 403


def test_no_credentials_in_access_log(secured):
    client, boundary, app = secured
    login(client)
    assert b'private-password' not in client.get('/view/security').data
    assert all('password' not in str(event) for event in app.extensions['audit_events'])
