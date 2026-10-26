import json
from relay.source_registry import SourceRegistry
from relay.source_retrieval import SourceRetrieval


def test_retrieval_scope_hash_and_refresh(tmp_path):
    path=tmp_path/'rules.md'
    path.write_text('Voucher eligibility rules')
    registry=SourceRegistry.model_validate({'sources':{'rules':dict(category='sut',scope_id='service',environments=['uat'],provider='local',location=str(tmp_path),files=['rules.md'],format='markdown')}})
    retrieval=SourceRetrieval(registry)
    first=retrieval.retrieve('sut','service','uat','voucher')['sources'][0]
    assert retrieval.retrieve('sut','other','uat','voucher')['sources']==[]
    assert retrieval.retrieve('sut','service','prod','voucher')['sources']==[]
    path.write_text('Voucher eligibility changed')
    second=retrieval.retrieve('sut','service','uat','voucher')['sources'][0]
    assert first['sha256'] != second['sha256']
    assert first['id'] != second['id']
    assert retrieval.retrieve('sut','service','uat','voucher',budget=1)['sources']==[]
    registry.sources['rules'].enabled=False
    assert retrieval.retrieve('sut','service','uat','voucher')['sources']==[]


def test_explicit_operation(tmp_path):
    (tmp_path/'spec.json').write_text(json.dumps({'openapi':'3.0.0','paths':{'/voucher':{'get':{'responses':{'200':{'description':'OK'}}}}}}))
    registry=SourceRegistry.model_validate({'sources':{'api':dict(category='sut',scope_id='service',environments=['uat'],provider='local',location=str(tmp_path),files=['spec.json'],format='openapi')}})
    result=SourceRetrieval(registry).retrieve('sut','service','uat','GET /voucher')
    assert result['sources'][0]['selector']=='GET /voucher'
