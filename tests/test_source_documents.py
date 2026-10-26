import hashlib
import json
import pytest
from relay.source_registry import Source
from relay.source_documents import read_local, extract_operation


def source(root):
    return Source(category='sut',scope_id='service',environments=['uat'],provider='local',location=str(root),files=['spec.json'],format='openapi')


def test_local_snapshot_and_symlink_rejection(tmp_path):
    body=b'{"openapi":"3.0.0"}'
    (tmp_path/'spec.json').write_bytes(body)
    assert read_local(source(tmp_path),'spec.json')['sha256'] == hashlib.sha256(body).hexdigest()
    with pytest.raises(ValueError):
        read_local(source(tmp_path),'other.json')
    (tmp_path/'spec.json').unlink()
    (tmp_path/'spec.json').symlink_to('/etc/passwd')
    with pytest.raises(OSError):
        read_local(source(tmp_path),'spec.json')


def snapshot(ref='#/components/schemas/Voucher'):
    return {'path':'spec.json','sha256':'hash','text':json.dumps({'openapi':'3.0.0','paths':{'/voucher':{'get':{'responses':{'200':{'schema':{'$ref':ref}}}}}},'components':{'schemas':{'Voucher':{'type':'object','properties':{'parent':{'$ref':'#/components/schemas/Voucher'}}}}}})}


def test_dependencies_and_cycle():
    result=extract_operation(snapshot(),'get','/voucher')
    assert '#/components/schemas/Voucher' in json.loads(result['text'])['references']
    assert result['sha256']=='hash'


@pytest.mark.parametrize('ref',['https://example.org/spec.json','#/missing'])
def test_invalid_dependencies(ref):
    with pytest.raises(ValueError):
        extract_operation(snapshot(ref),'get','/voucher')


def test_budget_and_unknown_endpoint():
    with pytest.raises(ValueError):
        extract_operation(snapshot(),'get','/voucher',max_bytes=10)
    with pytest.raises(ValueError):
        extract_operation(snapshot(),'get','/unknown')
