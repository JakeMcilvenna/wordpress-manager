import pytest
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


def csrf(client):
    with client.session_transaction() as state:
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
