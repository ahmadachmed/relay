"""Durable, metadata-first approval for conversation attachment investigations.

Task approvals cover attachment, saved-run and registered source capabilities. Source
content requires a separate exact-snapshot sharing confirmation after local preparation.
"""
import asyncio
import json
import os
import time
from uuid import uuid4
from typing import Literal
from pydantic import Field, ValidationError, model_validator
from .assistant import Strict, Turn, ChatRequest, Answer, ai_configuration, redact, redact_source_metadata, model_input_size
from .contracts import PlanRequest
from .execution import PlanError
from .source_runtime import digest, encoded

TASK_TTL = 900
RESULT_RETENTION = 7 * 86400
WORK_LEASE = 120


class TaskStart(Strict):
    message: str = Field(min_length=1, max_length=4000)
    share_context: Literal[True]
    run_id: str | None = Field(default=None, pattern=r'^[a-f0-9]{32}$')
    context: PlanRequest | None = None
    history: list[Turn] = Field(default_factory=list, max_length=8)

    @model_validator(mode='before')
    @classmethod
    def explicit_consent(cls, value):
        if not isinstance(value, dict) or value.get('share_context') is not True:
            raise ValueError('Explicit consent to share the message and attachment metadata is required')
        return value


class TaskDecision(Strict):
    approval_hash: str = Field(pattern=r'^[a-f0-9]{64}$')


def recipient():
    base, protocol, mode, key, model, local = ai_configuration()
    public = {'base_url': base, 'protocol': protocol, 'output_format':mode, 'model':model,
              'local_gateway_may_forward': local}
    # Bind account credentials without persisting the key itself.
    return public, digest(encoded({'recipient':public, 'credential':key or ''}))


def source_snapshot(session):
    return {'identity':session.identity, 'configs':dict(sorted(session.configs.items())),
            'files':[{'source_id':source_id,'path':path,'sha256':value['sha256'],
                      'commit':value.get('commit'),'repository':value.get('repository')}
                     for (source_id,path),value in sorted(session.snapshots.items())]}


class InvestigationTasks:
    def __init__(self, execution, assistant):
        self.execution, self.assistant = execution, assistant
        # Task records contain user messages and redacted responses.
        os.chmod(execution.path, 0o600)
        with execution.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS investigation_tasks (id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, record TEXT NOT NULL)')

    def _get(self, scope, task_id, db=None):
        if db is None:
            self.assistant.conversation_files.validate_scope(scope)
            with self.execution.connect() as connection:
                return self._get(scope, task_id, connection)
        row = db.execute('SELECT record FROM investigation_tasks WHERE id=? AND conversation_id=?', (task_id, scope)).fetchone()
        if not row:
            raise PlanError('not_found', 'Task not found in this conversation.', 404)
        return json.loads(row[0])

    @staticmethod
    def _save(db, record):
        db.execute('UPDATE investigation_tasks SET record=? WHERE id=? AND conversation_id=?',
                   (json.dumps(record, ensure_ascii=False), record['id'], record['conversation_id']))

    @staticmethod
    def _public(record):
        return {k:record.get(k) for k in ('id','conversation_id','status','created_at','expires_at','message',
                    'capabilities','recipient','approval_hash','answer','error','limitations','run_id','run_fingerprint','context','planner_message','lease_until','source_snapshot','source_diagnostics')}

    def create(self, scope, request):
        self.assistant.conversation_files.validate_scope(scope)
        run_fingerprint = self._run_fingerprint(request.run_id) if request.run_id else None
        public, fingerprint = recipient()
        now=time.time()
        record={'id':uuid4().hex,'conversation_id':scope,'status':'draft','created_at':now,'expires_at':now+TASK_TTL,
                'message':request.message,'run_id':request.run_id,'run_fingerprint':run_fingerprint,'context':request.context.model_dump(mode='json') if request.context else None,
                'request':request.model_dump(mode='json'),'recipient':public,
                'recipient_fingerprint':fingerprint,'capabilities':[],'approval_hash':None,'answer':None,'error':None,
                'limitations':['Registered sources require an initial read approval and a second approval of exact source hashes before AI sharing.',
                               'A local gateway may forward data to an upstream model provider.',
                               'Selected-run status and artifact metadata may change; after approval the current run metadata is shared.']}
        with self.execution.connect() as db:
            db.execute('DELETE FROM investigation_tasks WHERE json_extract(record, "$.expires_at") < ?', (now-RESULT_RETENTION,))
            db.execute('INSERT INTO investigation_tasks VALUES (?,?,?)', (record['id'],scope,json.dumps(record,ensure_ascii=False)))
        return self._public(record)

    def get(self, scope, task_id):
        self.assistant.conversation_files.validate_scope(scope)
        with self.execution.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            record=self._get(scope,task_id,db)
            now=time.time()
            if record['status'] in ('draft','awaiting_approval','awaiting_source_share','planning','preparing_sources','running') and now >= record['expires_at']:
                record['status']='expired';record['answer']=None;self._save(db,record)
            elif record['status'] in ('planning','preparing_sources','running') and now >= record.get('lease_until',0):
                record['status']='interrupted';record['answer']=None
                record['error']={'code':'task_interrupted','message':'The task did not finish. Create a new task and approve it again.'}
                self._save(db,record)
            return self._public(record)

    def _run_fingerprint(self, run_id, db=None):
        if db is None:
            self.assistant.monitor.get(run_id)
            with self.execution.connect() as connection:
                return self._run_fingerprint(run_id, connection)
        row=db.execute('SELECT record FROM plans WHERE id=?',(run_id,)).fetchone()
        if row is None:
            raise PlanError('task_stale', 'Selected run is unavailable; create a new task.', 409)
        saved=json.loads(row[0])
        return digest(encoded({'plan':saved.get('plan'), 'commit':saved.get('commit')}))

    def _check_run(self, record, db=None):
        if record.get('run_id') and self._run_fingerprint(record['run_id'],db) != record.get('run_fingerprint'):
            raise PlanError('task_stale', 'Selected run identity changed; create a new task.', 409)

    def _related_saved_candidates(self, record):
        if not record.get('run_id'):
            return {}
        selected=self.execution.get(record['run_id'])['plan']
        with self.execution.connect() as db:
            rows=db.execute('SELECT e.run_id,e.id,e.metadata,p.record FROM evidence e JOIN plans p ON p.id=e.run_id JOIN remote_runs r ON r.id=e.run_id WHERE e.body IS NOT NULL AND e.expires_at>? ORDER BY e.rowid DESC LIMIT 100', (time.time(),)).fetchall()
        values={}
        for run_id, artifact_id, raw, plan_raw in rows:
            metadata=json.loads(raw);saved=json.loads(plan_raw);plan=saved['plan']
            if (metadata.get('format')!='robot_xml' or not metadata.get('sha256') or
                    plan.get('project_id')!=selected.get('project_id') or
                    plan.get('project',{}).get('repository')!=selected.get('project',{}).get('repository')):
                continue
            specs=plan['project']['pipelines'][plan['pipeline_id']]['parameters']
            env_keys=[key for key,spec in specs.items() if spec.get('kind')=='environment']
            environment=plan.get('parameters',{}).get(env_keys[0]) if len(env_keys)==1 else None
            values[artifact_id]={'id':artifact_id,'run_id':run_id,'sha256':metadata['sha256'],
                'name':metadata['name'],'association':metadata.get('association','unverified'),
                'run_fingerprint':digest(encoded({'plan':plan,'commit':saved.get('commit')})),
                'branch':redact(plan.get('branch')), 'commit':saved.get('commit'), 'environment':redact(environment)}
        selected_items=[x for x in values.values() if x['run_id']==record['run_id']]
        other_items=[x for x in values.values() if x['run_id']!=record['run_id']]
        return {x['id']:x for x in (selected_items+other_items)[:20]}

    def _source_candidates(self, request):
        try:
            identity=self.assistant.sources.identity(request)
            registry=self.assistant.sources.registry()
        except PlanError:
            return {}, None
        candidates={}
        for key in sorted(registry.sources):
            try:
                source=self.assistant.sources.authorize(registry,key,identity)
            except PlanError:
                continue
            candidates[key]={'id':key,'category':source.category,'scope_id':source.scope_id,
                'provider':source.provider,'revision':source.revision,'format':source.format,
                'config_sha256':self.assistant.sources.fingerprint(source)}
        return candidates, identity

    def _check_sources(self, record):
        source_caps=[cap for cap in record['capabilities'] if cap['kind']=='source']
        if not source_caps:
            return
        identity=record.get('source_identity')
        if not identity:
            raise PlanError('task_stale', 'Source identity is missing.', 409)
        registry=self.assistant.sources.registry()
        for capability in source_caps:
            for resource in capability['resources']:
                source=self.assistant.sources.authorize(registry, resource['id'], identity)
                if self.assistant.sources.fingerprint(source) != resource['config_sha256']:
                    raise PlanError('task_stale', 'Approved source configuration changed.', 409)

    def _check_recipient(self, record):
        public, fingerprint=recipient()
        if fingerprint != record['recipient_fingerprint'] or public != record['recipient']:
            raise PlanError('task_stale', 'Model provider or credentials changed; create a new task.', 409)

    def _check_resources(self, record, db=None):
        if db is None:
            with self.execution.connect() as connection:
                return self._check_resources(record, connection)
        for capability in record['capabilities']:
            if capability['kind']=='source':
                continue
            for resource in capability['resources']:
                if capability['kind'] in ('saved_report','saved_comparison'):
                    resource_run=resource.get('run_id') or record['run_id']
                    if resource.get('run_fingerprint') and self._run_fingerprint(resource_run,db) != resource['run_fingerprint']:
                        raise PlanError('task_stale', 'A compared run identity changed.', 409)
                    row=db.execute('SELECT metadata,body,expires_at FROM evidence WHERE run_id=? AND id=?',
                        (resource_run,resource['id'])).fetchone()
                    if not row or row[1] is None or row[2] <= time.time():
                        raise PlanError('task_stale', 'Approved saved-run evidence expired or disappeared.', 409)
                    metadata=json.loads(row[0])
                    if metadata.get('format') != 'robot_xml' or metadata.get('sha256') != resource['sha256'] or digest(row[1]) != resource['sha256']:
                        raise PlanError('task_stale', 'Approved saved-run evidence changed.', 409)
                    continue
                row=db.execute('SELECT sha256 FROM conversation_files WHERE conversation_id=? AND id=? AND created_at>=?',
                    (record['conversation_id'],resource['id'],time.time()-7*86400)).fetchone()
                if row is None or row[0] != resource['sha256']:
                    raise PlanError('task_stale', 'An approved attachment changed or was deleted; create a new task.', 409)

    def guard(self, scope, task_id, expected_status='running'):
        record=self._get(scope,task_id)
        if record['status'] != expected_status:
            raise PlanError('task_revoked', 'Task access was denied, revoked or interrupted.', 409)
        if time.time() >= record['expires_at'] or time.time() >= record.get('lease_until',0):
            raise PlanError('task_expired', 'Task approval expired; create a new task.', 409)
        self._check_recipient(record)
        self._check_run(record)
        self._check_sources(record)
        self._check_resources(record)

    def _guard_planning(self, scope, task_id):
        record=self._get(scope,task_id)
        if record['status'] != 'planning':
            raise PlanError('task_cancelled', 'Task planning was cancelled or interrupted.', 409)
        if time.time() >= record['expires_at'] or time.time() >= record.get('lease_until',0):
            raise PlanError('task_expired', 'Task planning expired; create a new task.', 409)
        self._check_recipient(record)
        self._check_run(record)

    async def plan(self, scope, task_id):
        self.assistant.conversation_files.validate_scope(scope)
        with self.execution.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            record=self._get(scope,task_id,db)
            if record['status'] != 'draft':
                return self._public(record)
            if time.time() >= record['expires_at']:
                record['status']='expired';self._save(db,record);return self._public(record)
            self._check_recipient(record)
            self._check_run(record, db)
            record['status']='planning';record['lease_until']=time.time()+WORK_LEASE;self._save(db,record)
        try:
            attachments=self.assistant.conversation_files.list(scope)
            candidates={item['id']:item for item in attachments}
            saved_candidates={}
            if record.get('run_id'):
                summary=await asyncio.to_thread(self.assistant.evidence.summary,record['run_id'])
                saved_candidates={item['id']:item for item in summary['artifacts'] if item.get('id') and item.get('format')=='robot_xml' and item.get('availability')=='available' and item.get('sha256')}
            saved_metadata=[{'id':item['id'],'sha256':item['sha256'],'filename':redact(item['name']), 'association':item.get('association','unverified')} for item in list(saved_candidates.values())[:20]]
            saved_candidates={item['id']:saved_candidates[item['id']] for item in saved_metadata}
            related_candidates=self._related_saved_candidates(record)
            related_metadata=[{'id':item['id'],'run_id':item['run_id'],'sha256':item['sha256'], 'filename':redact(item['name']),
                'association':item['association'],'branch':item['branch'],'commit':item['commit'],'environment':item['environment']} for item in related_candidates.values()]
            planning_request=ChatRequest.model_validate({'message':record['message'], 'share_context':True,
                'run_id':record['run_id'], 'context':record['context']})
            source_candidates, source_identity=self._source_candidates(planning_request)
            source_metadata=list(source_candidates.values())[:8]
            source_candidates={item['id']:item for item in source_metadata}
            metadata=[{'id':item['id'],'sha256':item['sha256'],'filename':redact(item['filename']),'size':item['size'],
                       'status':'user_uploaded_unverified'} for item in attachments]
            context={'task_planning':{'attachments':metadata,'saved_reports':saved_metadata,'related_saved_reports':related_metadata,'run_id':record.get('run_id'), 'sources':source_metadata, 'source_capabilities_available':bool(source_metadata)},
                     'message':redact(record['message']),'history':redact(record['request']['history'])}
            if model_input_size(context) > 64000:
                raise PlanError('ai_context_limit', 'Task planning context exceeds the AI input budget.', 413)
            if os.getenv('RELAY_AI_ENABLED') != '1':
                raise PlanError('ai_disabled','AI is disabled.',503)
            if self.assistant.lock.locked():
                raise PlanError('ai_busy','Another AI request is active.',429)
            async with self.assistant.lock:
                await asyncio.to_thread(self._guard_planning,scope,task_id)
                async with asyncio.timeout(35):
                    response=await self.assistant.provider.answer(context)
            response=Answer.model_validate(response.model_dump())
            if (response.action not in ('answer','clarify') or response.selection is not None or response.hypotheses
                    or response.bug_report_draft is not None or response.classification is not None or response.knowledge_suggestion is not None
                    or response.report_operations or response.retrieval_requests or response.report_citations or response.suite_citations
                    or response.reference_citations or response.source_citations or response.comparison_citations or response.evidence_history_citations):
                raise PlanError('task_plan_invalid','Model returned unsupported planning output.',502)
            capabilities=[]
            kinds=set()
            for proposal in response.access_requests:
                ids=proposal.resource_ids
                valid_count = (len(ids)==2 if proposal.kind in ('comparison','saved_comparison') else 1<=len(ids)<=8 if proposal.kind=='source' else len(ids)==1)
                if proposal.kind in kinds or not valid_count or len(set(ids)) != len(ids):
                    raise PlanError('task_plan_invalid','Model requested an invalid or duplicate capability.',502)
                pool = source_candidates if proposal.kind=='source' else saved_candidates if proposal.kind=='saved_report' else related_candidates if proposal.kind=='saved_comparison' else candidates
                if any(item not in pool for item in ids):
                    raise PlanError('task_plan_invalid','Model requested an attachment outside this conversation.',502)
                resources=[pool[item] for item in ids]
                formats=[item.get('filename', item.get('name','')).rsplit('.',1)[-1].lower() for item in resources]
                if ((proposal.kind in ('report','saved_report','comparison','saved_comparison') and any(fmt!='xml' for fmt in formats))
                    or (proposal.kind=='reference' and formats[0] not in ('txt','md','pdf','docx'))
                    or (proposal.kind not in ('report','saved_report') and (proposal.search_all or proposal.include_suites))
                    or (proposal.kind in ('report','saved_report') and ({'report','saved_report'} & kinds))
                    or (proposal.kind=='comparison' and resources[0]['sha256']==resources[1]['sha256'])
                    or (proposal.kind=='saved_comparison' and (resources[0]['run_id']==resources[1]['run_id'] or record['run_id'] not in (resources[0]['run_id'],resources[1]['run_id'])))
                    or (proposal.kind in ('comparison','saved_comparison') and ({'comparison','saved_comparison'} & kinds))):
                    raise PlanError('task_plan_invalid','Model requested an unsupported attachment operation.',502)
                kinds.add(proposal.kind)
                if proposal.kind=='saved_comparison':
                    capabilities.append({'kind':'saved_comparison','purpose':redact(proposal.purpose),'search_all':False,'include_suites':False,
                        'resources':[{'id':item['id'],'sha256':item['sha256'],'filename':redact(item['name']),'run_id':item['run_id'],
                                      'run_fingerprint':item['run_fingerprint'],'association':item['association']} for item in resources]})
                    continue
                if proposal.kind=='source':
                    capabilities.append({'kind':'source','purpose':redact(proposal.purpose),'search_all':False,'include_suites':False,
                        'resources':[dict(item) for item in resources]})
                    continue
                capabilities.append({'kind':proposal.kind,'purpose':redact(proposal.purpose),
                    'search_all':proposal.search_all,'include_suites':proposal.include_suites,
                    'resources':[{'id':item['id'],'sha256':item['sha256'],'filename':redact(item.get('filename', item.get('name',''))),
                                  'association':item.get('association','unverified')} for item in resources]})
            with self.execution.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                current=self._get(scope,task_id,db)
                if current['status'] != 'planning':
                    return self._public(current)
                self._check_recipient(current)
                self._check_run(current, db)
                current['capabilities']=capabilities
                current['source_identity']=source_identity if any(cap['kind']=='source' for cap in capabilities) else None
                self._check_sources(current)
                self._check_resources(current, db)
                current['planner_message']=redact(response.message)
                if time.time() >= current['expires_at']:
                    current['status']='expired'
                elif capabilities:
                    current['approval_hash']=digest(encoded({'task_id':task_id,'conversation_id':scope,'capabilities':capabilities,
                        'recipient_fingerprint':current['recipient_fingerprint'],'run_fingerprint':current['run_fingerprint'],'source_identity':current['source_identity'],'request':current['request'],'expires_at':current['expires_at']}))
                    current['status']='awaiting_approval'
                else:
                    current['status']='completed'
                    current['answer']={'answer':redact_source_metadata(response.model_dump()),'facts':None,'run_id':None,'limitations':current['limitations']}
                self._save(db,current)
                return self._public(current)
        except TimeoutError:
            return self._fail(scope,task_id,'task_timeout','Task planning timed out.')
        except PlanError as error:
            return self._fail(scope,task_id,error.code,str(error))
        except ValidationError:
            return self._fail(scope,task_id,'task_plan_invalid','Model returned invalid planning output.')
        except Exception:
            self._fail(scope,task_id,'task_plan_failed','Task planning failed.')
            raise

    def _fail(self, scope, task_id, code, message):
        with self.execution.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            record=self._get(scope,task_id,db)
            if record['status'] in ('planning','preparing_sources','running'):
                record['status']='failed';record['error']={'code':code,'message':message};self._save(db,record)
            return self._public(record)

    def _request(self, record):
        base=record['request']
        values={'message':base['message'],'share_context':True,'history':base['history'],'run_id':base['run_id'],'context':base['context']}
        scope=record['conversation_id']
        for capability in record['capabilities']:
            resources=capability['resources']
            if capability['kind']=='report':
                values['auto_report']={'conversation_id':scope,'attachment_id':resources[0]['id'],'sha256':resources[0]['sha256'],
                    'share':True,'search_all':capability['search_all'],'include_suites':capability['include_suites']}
            elif capability['kind']=='saved_report':
                values['auto_report']={'conversation_id':scope,'run_id':record['run_id'],'artifact_id':resources[0]['id'],
                    'sha256':resources[0]['sha256'],'share':True,'search_all':capability['search_all'],'include_suites':capability['include_suites']}
            elif capability['kind']=='saved_comparison':
                values['comparison']={'conversation_id':scope,'first_run_id':resources[0]['run_id'],
                    'first_id':resources[0]['id'],'first_sha256':resources[0]['sha256'],
                    'second_run_id':resources[1]['run_id'],'second_id':resources[1]['id'],
                    'second_sha256':resources[1]['sha256'],'share':True}
            elif capability['kind']=='source':
                values['include_source_context']=True
                values['source_ids']=[resource['id'] for resource in resources]
            elif capability['kind']=='reference':
                values['reference_attachment']={'conversation_id':scope,'attachment_id':resources[0]['id'],'sha256':resources[0]['sha256'],'share':True}
            elif capability['kind']=='comparison':
                values['comparison']={'conversation_id':scope,'first_id':resources[0]['id'],'first_sha256':resources[0]['sha256'],
                    'second_id':resources[1]['id'],'second_sha256':resources[1]['sha256'],'share':True}
        return ChatRequest.model_validate(values)

    async def approve(self, scope, task_id, decision):
        self.assistant.conversation_files.validate_scope(scope)
        with self.execution.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            record=self._get(scope,task_id,db)
            if record['status'] not in ('awaiting_approval','awaiting_source_share'):
                return self._public(record)
            if decision.approval_hash != record['approval_hash']:
                raise PlanError('task_stale','Approval does not match the current task.',409)
            if time.time() >= record['expires_at']:
                record['status']='expired';self._save(db,record);return self._public(record)
            self._check_recipient(record)
            self._check_run(record, db)
            self._check_sources(record)
            self._check_resources(record, db)
            prepare = record['status']=='awaiting_approval' and any(cap['kind']=='source' for cap in record['capabilities'])
            record['status']='preparing_sources' if prepare else 'running'
            record['lease_until']=time.time()+WORK_LEASE
            self._save(db,record)
        try:
            if prepare:
                await asyncio.to_thread(self.guard,scope,task_id,'preparing_sources')
                session=await self.assistant.sources.start(self._request(record))
                await asyncio.to_thread(self.guard,scope,task_id,'preparing_sources')
                snapshot=source_snapshot(session)
                if not snapshot['files']:
                    raise PlanError('task_source_unavailable','No eligible source files could be prepared.',422)
                with self.execution.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    current=self._get(scope,task_id,db)
                    if current['status']!='preparing_sources':
                        return self._public(current)
                    if time.time()>=current['expires_at']:
                        current['status']='expired';self._save(db,current);return self._public(current)
                    self._check_recipient(current)
                    self._check_run(current,db)
                    self._check_sources(current)
                    self._check_resources(current,db)
                    current['source_snapshot']=snapshot
                    current['source_diagnostics']=redact(session.diagnostics)
                    current['approval_hash']=digest(encoded({'task_id':task_id,'conversation_id':scope,
                        'capabilities':current['capabilities'], 'source_snapshot':snapshot,
                        'recipient_fingerprint':current['recipient_fingerprint'], 'run_fingerprint':current['run_fingerprint'],
                        'source_identity':current['source_identity'], 'request':current['request'], 'expires_at':current['expires_at']}))
                    current['status']='awaiting_source_share'
                    self._save(db,current)
                    return self._public(current)
            result=await self.assistant.chat(self._request(record),task_guard=lambda:self.guard(scope,task_id),
                expected_source_snapshot=record.get('source_snapshot'))
            with self.execution.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                current=self._get(scope,task_id,db)
                if current['status']=='running':
                    if time.time() >= current['expires_at']:
                        current['status']='expired'
                    else:
                        self._check_recipient(current)
                        self._check_run(current, db)
                        self._check_sources(current)
                        self._check_resources(current, db)
                        current['status']='completed';current['answer']=result
                    self._save(db,current)
                return self._public(current)
        except TimeoutError:
            return self._fail(scope,task_id,'task_timeout','Source preparation timed out.')
        except PlanError as error:
            return self._fail(scope,task_id,error.code,str(error))
        except Exception:
            self._fail(scope,task_id,'task_failed','Task execution failed.')
            raise

    def cancel_unapproved(self, scope, task_id):
        self.assistant.conversation_files.validate_scope(scope)
        with self.execution.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            record=self._get(scope,task_id,db)
            if record['status'] in ('draft','planning'):
                record['status']='cancelled'
                self._save(db,record)
            return self._public(record)

    def deny(self, scope, task_id, decision):
        self.assistant.conversation_files.validate_scope(scope)
        with self.execution.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            record=self._get(scope,task_id,db)
            if record['status'] in ('awaiting_approval','awaiting_source_share','preparing_sources','running'):
                if decision.approval_hash != record['approval_hash']:
                    raise PlanError('task_stale','Approval does not match the current task.',409)
                record['status']='revoked' if record['status'] in ('preparing_sources','running') else 'denied'
                record['error']={'code':'task_'+record['status'], 'message':('Access was revoked; the answer will be discarded.' if record['status']=='revoked' else 'Access was denied; attachment contents were not sent.')}
                self._save(db,record)
            return self._public(record)
