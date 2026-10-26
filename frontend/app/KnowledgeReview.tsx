"use client";
import { useEffect, useRef, useState } from 'react';
import { api, post } from '../lib/api';

type Proposal = {
 id:string;run_id:string;category:string;scope_id:string;environment:string;
 path:string|null;document_id:string|null;text:string;rationale:string;diff:string;
 hash:string;base_hash:string;base_revision:string|null;status:string;verification:string;
 artifact_ids:string[];operation:string;applied_revision?:string;
};
type History = {current_hash:string;revisions:{revision:string;hash:string;previous:string|null;actor:string;operation:string;created_at:string;provenance:unknown}[]};

export default function KnowledgeReview({runId,refresh,busy,onChanged,onBusy}:{runId:string;refresh:number;busy:boolean;onChanged:()=>void;onBusy:(value:boolean)=>void}) {
 const [proposals,setProposals]=useState<Proposal[]>([]);
 const [error,setError]=useState('');
 const [notice,setNotice]=useState('');
 const [loading,setLoading]=useState(false);
 const [selected,setSelected]=useState<Proposal|null>(null);
 const [text,setText]=useState('');
 const [rationale,setRationale]=useState('');
 const [actor,setActor]=useState('');
 const [confirmed,setConfirmed]=useState(false);
 const [verified,setVerified]=useState(false);
 const [category,setCategory]=useState('automation');
 const [history,setHistory]=useState<History|null>(null);
 const live=useRef(true);
 const actionLock=useRef(false);
 useEffect(()=>{live.current=true;return ()=>{live.current=false;};},[]);
 async function load(){
  try { const value=await api<Proposal[]>(`/api/remote-runs/${runId}/knowledge-proposals`); if(live.current){setProposals(value);setError('');} }
  catch(e){if(live.current)setError(e instanceof Error?e.message:'Cannot load proposals');}
 }
 useEffect(()=>{void load();},[runId,refresh]); // component is keyed by immutable run ID
 function choose(value:Proposal, clearNotice=true){setSelected(value);setText(value.text);setRationale(value.rationale);setConfirmed(false);setVerified(false);if(clearNotice)setNotice('');}
 async function action(callback:()=>Promise<Proposal|void>){
  if(actionLock.current||busy)return;
  actionLock.current=true;setLoading(true);setError('');setNotice('');onBusy(true);
  try {const result=await callback();if(live.current){if(result){choose(result,false);if(result.status==='applied'){onChanged();setHistory(null);setNotice('Knowledge revision applied or recovered. Run a new analysis to use it.');}}await load();}}
  catch(e){if(live.current)setError(e instanceof Error?e.message:'Knowledge action failed');}
  finally{actionLock.current=false;if(live.current)setLoading(false);onBusy(false);}
 }
 const dirty=selected && (text!==selected.text||rationale!==selected.rationale);
 const pending=selected && ['draft','stored_unverified'].includes(selected.status);
 const locked=busy||loading;
 return <details className="analysis-inputs"><summary>Knowledge proposals and history</summary>
 <p>Drafts are private. Saving a hypothesis does not verify it. Only explicitly verified approval updates active knowledge. Scope and environment come from this run.</p>
 {error&&<p role="alert">{error}</p>}{notice&&<p role="status">{notice}</p>}
 <button disabled={locked} onClick={()=>void load()}>Refresh knowledge proposals</button>
 <ul>{proposals.map(item=><li key={item.id}><button disabled={locked} onClick={()=>choose(item)}>{item.category} / {item.scope_id} / {item.path||'bundle rollback'} · {item.status}</button></li>)}</ul>
 {selected&&<section aria-label="Review knowledge proposal">
 <p>Run {selected.run_id} · {selected.category} / {selected.scope_id} · environment {selected.environment} · {selected.operation}</p>
 <p>Document ID: {selected.document_id||'bundle'} · Base revision: {selected.base_revision||'unmanaged'} · Base hash: {selected.base_hash}</p>
 <p>Proposal hash: {selected.hash} · {selected.status} · {selected.verification}</p>
 <p>Evidence: {selected.artifact_ids.join(', ')||'Existing bundle revision (rollback)'}</p>
 <label>Proposed knowledge<textarea aria-label="Proposed knowledge" value={text} disabled={locked||!pending||selected.operation==='rollback'} onChange={e=>{setText(e.target.value);setConfirmed(false);setVerified(false);}} maxLength={8000}/></label>
 <label>Knowledge rationale<textarea aria-label="Knowledge rationale" value={rationale} disabled={locked||!pending||selected.operation==='rollback'} onChange={e=>{setRationale(e.target.value);setConfirmed(false);setVerified(false);}} maxLength={1500}/></label>
 <pre style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}}>{selected.diff}</pre>
 {pending&&<>
 {selected.operation!=='rollback'&&<button disabled={locked||!dirty} onClick={()=>void action(()=>post<Proposal>(`/api/remote-runs/${runId}/knowledge-proposals/${selected.id}/edit`,{expected_hash:selected.hash,text,rationale}))}>Save edited draft for review</button>}
 <button disabled={locked} onClick={()=>void action(()=>post<Proposal>(`/api/remote-runs/${runId}/knowledge-proposals/${selected.id}/reject`,{expected_hash:selected.hash}))}>Reject knowledge proposal</button>
 <label>Reviewer name<input aria-label="Reviewer name" value={actor} maxLength={100} disabled={locked} onChange={e=>{setActor(e.target.value);setConfirmed(false);}}/></label>
 <label><input type="checkbox" checked={confirmed} disabled={locked||Boolean(dirty)} onChange={e=>setConfirmed(e.target.checked)}/>I reviewed this exact proposal and approve saving it.</label>
 <label><input type="checkbox" checked={verified} disabled={locked||Boolean(dirty)} onChange={e=>setVerified(e.target.checked)}/>I independently verified the claims for this environment. Activate approved knowledge.</label>
 <button disabled={locked||Boolean(dirty)||!confirmed||!actor.trim()} onClick={()=>void action(async()=>{
   const value=await post<Proposal>(`/api/remote-runs/${runId}/knowledge-proposals/${selected.id}/approve`,{expected_hash:selected.hash,confirmed:true,verified,actor});
   if(live.current && value.status!=='applied'){setNotice('Stored as unverified; active knowledge was not changed.');}
   return value;
 })}>{verified?'Approve and activate knowledge':'Store unverified draft'}</button>
 </>}
 {selected.status==='applied'&&<p>Applied revision: {selected.applied_revision}. Earlier analysis provenance remains unchanged.</p>}
 </section>}
 <label>History category<select aria-label="Knowledge history category" value={category} disabled={locked} onChange={e=>{setCategory(e.target.value);setHistory(null);}}><option value="automation">Automation</option><option value="sut">SUT</option></select></label>
 <button disabled={locked} onClick={()=>void action(async()=>{const value=await api<History>(`/api/remote-runs/${runId}/knowledge-history?category=${category}`);if(live.current)setHistory(value);})}>Load knowledge history</button>
 {history&&<><p>Recent 30 revisions; older immutable snapshots remain on disk.</p><ul>{history.revisions.map(item=><li key={item.revision}>{item.revision} · {item.operation} · {item.actor} · {item.created_at} · SHA256 {item.hash}
 <button disabled={locked} onClick={()=>void action(()=>post<Proposal>(`/api/remote-runs/${runId}/knowledge-rollback`,{category,revision:item.revision,expected_hash:history.current_hash}))}>Review rollback to {item.revision.slice(0,8)}</button>
 </li>)}</ul></>}
 </details>;
}
