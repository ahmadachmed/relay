"""Conservative local comparison of uploaded Robot results, never a flaky verdict."""
from collections import Counter
import io
from xml.etree.ElementTree import ParseError
from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException
from .execution import PlanError
from .report_evidence import MAX_REPORT_BYTES
from .report_sections import MAX_NODES, MAX_DEPTH


def _read(body):
    if len(body)>MAX_REPORT_BYTES:
        raise PlanError('report_limit','Report exceeds 10 MB.',413)
    stack, suites, tests = [], [], []
    current=None
    nodes=0
    try:
        for event,element in ET.iterparse(io.BytesIO(body),events=('start','end'),forbid_dtd=True):
            if event=='start':
                nodes+=1
                stack.append(element.tag)
                if nodes>MAX_NODES or len(stack)>MAX_DEPTH:
                    raise PlanError('report_limit','Report exceeds node or nesting limits.',413)
                if nodes==1 and element.tag!='robot':
                    raise PlanError('unsupported_report','Expected a Robot Framework output XML.',415)
                if element.tag=='suite':
                    suites.append(element.get('name',''))
                elif element.tag=='test':
                    if current is not None:
                        raise PlanError('invalid_report','Nested tests are unsupported.',422)
                    current={'id':str(len(tests)),'identity':(*suites,element.get('name','')),'status':'UNKNOWN'}
            else:
                if current is not None and element.tag=='status' and len(stack)>1 and stack[-2]=='test':
                    current['status']=element.get('status','UNKNOWN')
                if element.tag=='test' and current is not None:
                    tests.append(current)
                    current=None
                if element.tag=='suite': suites.pop()
                element.clear()
                stack.pop()
    except (ParseError,DefusedXmlException,LookupError,ValueError):
        raise PlanError('invalid_report','Malformed or unsafe XML report.',422) from None
    return tests


def compare_robot(first, second, first_sha, second_sha, offset=0, limit=20, sanitize=None):
    if first_sha==second_sha:
        raise PlanError('comparison_same_report','Choose two different reports.',422)
    if offset<0 or offset>100000 or not 1<=limit<=50:
        raise PlanError('comparison_page','Invalid comparison page.',422)
    clean=sanitize or (lambda value:value)
    before,after=_read(first),_read(second)
    before_count=Counter(t['identity'] for t in before)
    after_count=Counter(t['identity'] for t in after)
    # Refuse to pair unnamed or duplicate paths; no position-based guessing.
    after_index={t['identity']:t for t in after if after_count[t['identity']]==1 and len(t['identity']) >= 2 and all(t['identity'])}
    paired=[]
    ambiguous=0
    for old in before:
        key=old['identity']
        if len(key)<2 or not all(key) or before_count[key]>1 or after_count[key]>1:
            ambiguous+=1
            continue
        new=after_index.get(key)
        if not new: continue
        prior,later=old['status'],new['status']
        transition=('fail_to_pass' if prior=='FAIL' and later=='PASS' else
                    'pass_to_fail' if prior=='PASS' and later=='FAIL' else
                    'unchanged' if prior==later else 'other')
        suite_path=' / '.join(clean(x) for x in key[:-1])
        test_name=clean(key[-1])
        paired.append({'pair_id':str(len(paired)), 'first_test_id':old['id'],'second_test_id':new['id'],
                       'suite_path':suite_path[:600], 'suite_path_truncated':len(suite_path)>600,
                       'test_name':test_name[:300], 'test_name_truncated':len(test_name)>300, 'first_status':clean(prior)[:40],
                       'second_status':clean(later)[:40], 'transition':transition})
    counts=Counter(item['transition'] for item in paired)
    return {'first_sha256':first_sha,'second_sha256':second_sha,'items':paired[offset:offset+limit],
            'total':len(paired),'offset':offset,'next_offset':offset+limit if offset+limit<len(paired) else None,
            'counts':dict(counts),'first_total_tests':len(before),'second_total_tests':len(after),
            'ambiguous_first_tests':ambiguous,
            'limitations':['Uploaded reports have unverified run, environment and revision associations.',
                'Matching suite and test names does not prove identical code or comparable execution.',
                'Fail-to-pass is an observed difference, not proof of flakiness or root cause.',
                'Duplicate and unnamed identities are not paired; unmatched tests are omitted.']}
