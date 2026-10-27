import io
import json
import zipfile
import pytest
from pypdf import PdfWriter
from pypdf.generic import NameObject, DictionaryObject, DecodedStreamObject
from fastapi.testclient import TestClient
from relay.api import create_app
from relay.assistant import Answer


def response(**kw):
    return Answer.model_validate(dict(action='answer', message='Review evidence.', selection=None,
        hypotheses=[], bug_report_draft=None) | kw)


class Fake:
    def __init__(self): self.calls=[]
    async def answer(self, context):
        self.calls.append(json.loads(json.dumps(context)))
        if context.get('auto_report') and not context.get('report_attachment'):
            return response(report_operations=[{'operation':'read_test','test_id':'0','offset':0}])
        refs=context.get('reference_attachment',{}).get('sections',[])
        return response(reference_citations=[{'section_id':refs[0]['id'],'claim':'Expected behavior'}] if refs else [],
                        report_citations=[{'section_id':'0','claim':'Failure'}] if context.get('report_attachment') else [])


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('RELAY_AI_ENABLED','1')
    app=create_app(data=tmp_path,monitoring=False); fake=Fake(); app.state.assistant.provider=fake
    with TestClient(app) as client:
        scope=client.post('/api/conversations').json()['id']
        yield app,client,scope,fake


def upload(c,scope,name,body):
    r=c.post(f'/api/conversations/{scope}/attachments',params={'filename':name},content=body)
    assert r.status_code==200, r.text
    return r.json()


def ref(scope,item):
    return {'conversation_id':scope,'attachment_id':item['id'],'sha256':item['sha256'],'share':True}


def ask(c,**kw):
    return c.post('/api/assistant/chat',json={'message':'Expected login response', 'share_context':True, **kw})


def make_pdf(with_text=True, encrypted=False, extra_blank=False):
    writer=PdfWriter();page=writer.add_blank_page(width=300,height=300)
    if with_text:
        font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
        page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
        stream=DecodedStreamObject();stream.set_data(b'BT /F1 12 Tf 20 100 Td (Expected login 200) Tj ET')
        page[NameObject('/Contents')]=writer._add_object(stream)
    if extra_blank: writer.add_blank_page(width=300,height=300)
    if encrypted: writer.encrypt('password')
    result=io.BytesIO();writer.write(result);return result.getvalue()


def make_docx(xml=None):
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml','<Types/>')
        z.writestr('word/document.xml',xml or '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Expected login 200</w:t></w:r></w:p></w:body></w:document>')
    return out.getvalue()


@pytest.mark.parametrize('name,body', [('req.txt',b'Expected login 200'),('req.md',b'## Expected login 200'),('req.pdf',make_pdf()),('req.docx',make_docx())])
def test_reference_formats_share_only_with_approval(setup,name,body):
    app,c,scope,fake=setup;item=upload(c,scope,name,body)
    assert ask(c).status_code==200 and 'reference_attachment' not in fake.calls[-1]
    result=ask(c,reference_attachment=ref(scope,item))
    assert result.status_code==200, result.text
    shared=result.json()['reference_shared']
    assert shared['sha256']==item['sha256'] and 'Expected login 200' in shared['sections'][0]['text']
    assert result.json()['answer']['reference_citations'][0]['section_id']==shared['sections'][0]['id']
    assert ask(c,reference_attachment={**ref(scope,item),'share':False}).status_code==422
    assert ask(c,reference_attachment={**ref(scope,item),'sha256':'0'*64}).status_code==409
    other=c.post('/api/conversations').json()['id']
    assert ask(c,reference_attachment={**ref(scope,item),'conversation_id':other}).status_code==404


def test_optional_reference_can_be_combined_with_auto_report(setup):
    app,c,scope,fake=setup
    doc=upload(c,scope,'req.txt',b'Login must return 200')
    report=upload(c,scope,'output.xml',b'<robot><suite><test name="login"><msg>HTTP 500</msg><status status="FAIL"/></test></suite></robot>')
    result=ask(c,reference_attachment=ref(scope,doc),auto_report=ref(scope,report))
    assert result.status_code==200, result.text
    assert result.json()['reference_shared']['source']=='user_uploaded_unverified'
    assert result.json()['report_shared']['items'][0]['text']=='HTTP 500'
    assert result.json()['answer']['reference_citations'] and result.json()['answer']['report_citations']
    other=c.post('/api/conversations').json()['id']
    assert ask(c,reference_attachment={**ref(scope,doc),'conversation_id':other},auto_report=ref(scope,report)).status_code==422


def test_redaction_precedes_reference_truncation_and_invalid_citations_fail(setup,monkeypatch):
    app,c,scope,fake=setup
    secret='super-secret-value-0987654321';monkeypatch.setenv('RELAY_TEST_SECRET',secret)
    item=upload(c,scope,'req.txt',('x'*1490+secret).encode())
    result=ask(c,reference_attachment=ref(scope,item))
    assert result.status_code==200
    assert secret[:10] not in json.dumps(fake.calls[-1]['reference_attachment'])
    class Bad:
        async def answer(self,context): return response(reference_citations=[{'section_id':'999','claim':'invented'}])
    app.state.assistant.provider=Bad()
    assert ask(c,reference_attachment=ref(scope,item)).status_code==502
    assert ask(c).status_code==502


def test_scanned_encrypted_corrupt_and_unsafe_docs(setup):
    app,c,scope,fake=setup
    for name,body,code in [('scan.pdf',make_pdf(False),'document_no_text'),('protected.pdf',make_pdf(encrypted=True),'document_encrypted'),('broken.pdf',b'%PDF-invalid','document_invalid'),('evil.docx',make_docx('<!DOCTYPE x [<!ENTITY e "x">]><x>&e;</x>'),'document_invalid')]:
        item=upload(c,scope,name,body)
        r=ask(c,reference_attachment=ref(scope,item))
        assert r.status_code==422 and r.json()['detail']['code']==code, r.text


def test_ranking_and_partial_coverage_are_visible(setup):
    app,c,scope,fake=setup
    item=upload(c,scope,'req.md',b'Overview\nGeneral rules\nLimits\nNotes\nExpected login response 200\nOther text')
    result=ask(c,reference_attachment=ref(scope,item))
    assert result.status_code==200
    shared=result.json()['reference_shared']
    assert shared['sections'][0]['id']=='4' and shared['sections'][0]['location']=='line 5'
    assert shared['total_sections']==6 and shared['omitted_sections']==3


def test_deleted_reference_during_model_call_discards_answer(setup):
    app,c,scope,fake=setup; item=upload(c,scope,'req.txt',b'Expected login 200')
    class Delete:
        async def answer(self,context):
            app.state.assistant.conversation_files.delete(scope,item['id'])
            return response()
    app.state.assistant.provider=Delete()
    assert ask(c,reference_attachment=ref(scope,item)).status_code==404


def test_reference_redaction_change_discards_answer(setup,monkeypatch):
    app,c,scope,fake=setup;item=upload(c,scope,'req.txt',b'Expected private-example-secret')
    class Change:
        async def answer(self,context):
            monkeypatch.setenv('RELAY_NEW_SECRET','private-example-secret')
            return response()
    app.state.assistant.provider=Change()
    assert ask(c,reference_attachment=ref(scope,item)).status_code==409


def test_document_limits_and_mislabeled_text(setup):
    app,c,scope,fake=setup
    item=upload(c,scope,'large.txt',b'a'*5_000_001)
    result=ask(c,reference_attachment=ref(scope,item))
    assert result.status_code==422 and result.json()['detail']['code']=='document_too_large'
    item=upload(c,scope,'wrong.txt',b'%PDF-fake')
    result=ask(c,reference_attachment=ref(scope,item))
    assert result.status_code==422 and result.json()['detail']['code']=='document_format'
    item=upload(c,scope,'huge.docx',make_docx('<x>'+'a'*2_000_001+'</x>'))
    result=ask(c,reference_attachment=ref(scope,item))
    assert result.status_code==422 and result.json()['detail']['code']=='document_limit'


def test_mixed_pdf_discloses_pages_without_text(setup):
    app,c,scope,fake=setup
    item=upload(c,scope,'mixed.pdf',make_pdf(extra_blank=True))
    result=ask(c,reference_attachment=ref(scope,item))
    assert result.status_code==200
    assert result.json()['reference_shared']['non_text_pages']==[2]
    assert fake.calls[-1]['reference_attachment']['non_text_pages']==[2]


@pytest.mark.parametrize('mode', ['json_schema', 'json_object'])
def test_report_and_reference_through_chat_gateway_transport(setup, monkeypatch, mode):
    import httpx
    from relay.assistant import OpenAIProvider
    app,c,scope,fake=setup
    monkeypatch.setenv('RELAY_AI_BASE_URL','http://localhost:20128/v1')
    monkeypatch.setenv('RELAY_AI_API','chat_completions')
    monkeypatch.setenv('RELAY_AI_OUTPUT_FORMAT',mode)
    monkeypatch.setenv('RELAY_AI_MODEL','synthetic-model')
    doc=upload(c,scope,'req.txt',b'Login returns 200')
    report=upload(c,scope,'output.xml',b'<robot><suite><test name="login"><msg>HTTP 500</msg><status status="FAIL"/></test></suite></robot>')
    seen=[]
    def handle(request):
        payload=json.loads(request.content)
        context=json.loads(payload['messages'][-1]['content'])
        seen.append(context)
        if len(seen)==1:
            value=response(report_operations=[{'operation':'read_test','test_id':'0','offset':0}])
        else:
            value=response(reference_citations=[{'section_id':'0','claim':'Expected 200'}],report_citations=[{'section_id':'0','claim':'Observed 500'}])
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(value.model_dump())}}]})
    original=httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(handle),**kwargs))
    app.state.assistant.provider=OpenAIProvider()
    result=ask(c,reference_attachment=ref(scope,doc),auto_report=ref(scope,report))
    assert result.status_code==200, result.text
    assert len(seen)==2 and seen[1]['reference_attachment']['sections'][0]['text']=='Login returns 200'
    assert seen[1]['report_attachment']['items'][0]['text']=='HTTP 500'


def test_long_reference_selects_window_near_relevant_terms(setup):
    app,c,scope,fake=setup
    item=upload(c,scope,'req.txt',('A'*2500+' expected login 200').encode())
    result=ask(c,reference_attachment=ref(scope,item))
    assert result.status_code==200
    section=result.json()['reference_shared']['sections'][0]
    assert section['text_offset'] > 0 and 'expected login 200' in section['text']
    assert section['truncated'] is True


def test_reference_window_uses_original_unicode_offsets(setup):
    app,c,scope,fake=setup
    text='İ'*1800+' expected login 200'
    item=upload(c,scope,'unicode.txt',text.encode())
    result=ask(c,reference_attachment=ref(scope,item))
    assert result.status_code==200
    section=result.json()['reference_shared']['sections'][0]
    assert section['text_offset'] < 1800 and 'expected login 200' in section['text']
    assert section['text']==text[section['text_offset']:section['text_offset']+1500]


def test_unicode_expansion_preserves_unique_section_ids(setup):
    app,c,scope,fake=setup
    text=('İ'*1800+' expected login 200\n'+'İ'*1800+' expected login 400')
    item=upload(c,scope,'unicode-sections.txt',text.encode())
    result=ask(c,reference_attachment=ref(scope,item))
    assert result.status_code==200, result.text
    sections=result.json()['reference_shared']['sections']
    assert [(s['id'],s['location']) for s in sections]==[('0','line 1'),('1','line 2')]


def test_large_unicode_reference_within_limits(setup):
    app,c,scope,fake=setup
    item=upload(c,scope,'emoji.txt',('😀'*130000).encode())
    result=ask(c,reference_attachment=ref(scope,item))
    assert result.status_code==200, result.text
    assert result.json()['reference_shared']['sections'][0]['truncated']
