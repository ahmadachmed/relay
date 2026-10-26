"use client";
import { useEffect, useState } from "react";
import { api } from "../lib/api";

export type SourceEntry = { id: string; category: "automation" | "sut"; scope_id: string; environments: string[]; provider: string; format: string; revision: string };
export type SourceManifest = { id: string; identity: {mode:string;project_id:string;sut_id:string|null;environment:string;commit:string|null}; sources: {handle:string;source_id:string;category:string;path:string;selector:string;sha256:string;shared_sha256:string;commit:string|null;parser_version:string;freshness?:string;dependencies?:{path:string;sha256:string;commit:string|null}[]}[];diagnostics:string[];replay:string };
export function SourceControls({ enabled, sources, selected, consent, busy, onChange }: {enabled:boolean;sources:SourceEntry[];selected:string[];consent:boolean;busy:boolean;onChange:(ids:string[],consent:boolean)=>void}) {
  if (!enabled) return null;
  return <details className="ai-sharing"><summary>Original sources · planning and analysis</summary>
    <p>Choose up to eight approved sources for this project, SUT and environment. Their contents go to the configured AI provider; gateways may forward them upstream. Redaction is best-effort. This permission is separate from curated knowledge.</p>
    <label><input type="checkbox" checked={consent} disabled={busy} onChange={e=>onChange(selected,e.target.checked)}/>Share selected original sources with AI</label>
    {!sources.length && <p>No applicable original sources. Select an explicit environment and a declared SUT, or register sources in backend configuration.</p>}
    {sources.map(source=><label key={source.id}><input type="checkbox" checked={selected.includes(source.id)} disabled={busy || (selected.length >= 8 && !selected.includes(source.id))} onChange={e=>onChange(e.target.checked?[...selected,source.id]:selected.filter(id=>id!==source.id),consent)}/>{source.id} · {source.category}/{source.scope_id} · {source.format} · version: {source.revision}</label>)}
    <p>Local sources use a fresh hash snapshot per request. Automation repositories follow the selected branch for planning and the saved commit for run analysis. SUT repository references use their separately configured pinned revision; deployed version remains unknown.</p>
  </details>;
}
export function SourceReferences({ manifest, citations }: {manifest:SourceManifest;citations:{handle:string;claim:string}[]}) {
  const [fresh, setFresh] = useState<SourceManifest|null>(null);
  const [error,setError] = useState("");
  const [busy,setBusy] = useState(false);
  useEffect(()=>{setFresh(null);setError("");},[manifest.id]);
  return <section aria-label="Original source references"><h3>Original sources used</h3>
    <p>{manifest.identity.mode} · {manifest.identity.project_id} · SUT: {manifest.identity.sut_id || "none"} · {manifest.identity.environment}</p>
    <p>Authorized references, not verified runtime evidence. {manifest.replay}</p>
    {citations.map((c,i)=><p key={i}>Reference claim: {c.claim} · <a href={`#source-${c.handle}`}>section {c.handle.slice(0,10)}</a></p>)}
    <ul>{(fresh || manifest).sources.map(source=><li id={`source-${source.handle}`} key={source.handle}>Reference {source.handle.slice(0,10)} · {source.source_id} · {source.category} · {source.path} · {source.selector} · SHA256 {source.sha256} · shared SHA256 {source.shared_sha256} · {source.commit || "local snapshot"} · {source.parser_version}{source.freshness && <> · {source.freshness}</>}{source.dependencies?.map(dep=><p key={dep.path}>Dependency: {dep.path} · SHA256 {dep.sha256} · {dep.commit || "local snapshot"}</p>)}</li>)}</ul>
    <ul>{manifest.diagnostics.map((d,i)=><li key={i}>{d}</li>)}</ul>
    <button disabled={busy} onClick={async()=>{setBusy(true);setError("");try{setFresh(await api<SourceManifest>(`/api/source-manifests/${manifest.id}`));}catch{setError("References could not be rechecked; source may be unavailable or authorization changed.");}finally{setBusy(false);}}}>Check source freshness</button>
    {error && <p role="alert">{error}</p>}
  </section>;
}
