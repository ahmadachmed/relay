from fastapi.testclient import TestClient
from relay.api import create_app


def test_conversation_attachment_scope_and_format(tmp_path):
    client = TestClient(create_app(data=tmp_path, monitoring=False))
    a = client.post('/api/conversations').json()['id']
    b = client.post('/api/conversations').json()['id']
    path = f'/api/conversations/{a}/attachments'
    result = client.post(path, params={'filename': 'output.xml'}, content=b'<robot/>')
    assert result.status_code == 200
    retry = client.post(path, params={'filename': 'output.xml'}, content=b'<robot/>')
    assert retry.status_code == 200
    assert retry.json()['id'] == result.json()['id']
    assert retry.json()['status'] == 'stored_format_unverified'
    assert client.get(f'/api/conversations/{b}/attachments').json() == []
    assert client.delete(f"/api/conversations/{b}/attachments/{result.json()['id']}").status_code == 404
    assert client.get('/api/conversations/' + 'f' * 32 + '/attachments').status_code == 404
    assert client.post(path, params={'filename': 'fake.pdf'}, content=b'not pdf').status_code == 415
    assert client.post(path, params={'filename': 'bad.docx'}, content=b'not zip').status_code == 415
    assert client.delete(path + '/' + result.json()['id']).status_code == 200
