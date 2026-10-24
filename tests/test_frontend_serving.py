from fastapi.testclient import TestClient
from relay import api


def test_optional_frontend_export_served_at_same_origin(tmp_path, monkeypatch):
    root = tmp_path / 'source'
    ui = root / 'frontend' / 'out'
    ui.mkdir(parents=True)
    (ui / 'index.html').write_text('<html>Relay workspace fixture</html>')
    (root / 'web').mkdir()
    (root / 'web' / 'index.html').write_text('Legacy runner fixture')
    real_root = api.ROOT
    monkeypatch.setattr(api, 'ROOT', root)
    app = api.create_app(real_root / 'relay.example.json', tmp_path / 'data', real_root / 'projects.example.json')
    with TestClient(app) as client:
        assert client.get('/', follow_redirects=False).headers['location'] == '/ui/'
        assert 'Relay workspace fixture' in client.get('/ui/').text
        assert client.get('/legacy').text == 'Legacy runner fixture'
        assert client.get('/api/project-registry').status_code == 200
        assert client.get('/ui/', headers={'Origin': 'https://other.example'}).status_code == 403
