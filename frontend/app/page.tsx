"use client";
import ThemePicker from "../components/theme-picker";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowUp,
  ArrowUpRight,
  Check,
  ChevronRight,
  CircleHelp,
  FileText,
  FlaskConical,
  History,
  LayoutGrid,
  LoaderCircle,
  PanelRight,
  Paperclip,
  Play,
  Plus,
  RefreshCw,
  Settings2,
  ShieldCheck,
  Sparkles,
  Square,
  Upload,
  X,
} from "lucide-react";
import {
  api,
  ApiError,
  post,
  safeBitbucketUrl,
  type Evidence,
  type Plan,
  type Project,
  type Registry,
  type Run,
} from "../lib/api";

import { SourceControls, SourceReferences, type SourceEntry, type SourceManifest } from "./SourceContext";
import KnowledgeReview from "./KnowledgeReview";

type Knowledge = {sources:{id:string;source_repository?:string;source_path?:string;source_commit?:string;category?:string;scope_id?:string;document_id:string;path:string;revision:string;sha256:string;reviewed_at:string}[];diagnostics:string[]};
type Context = {
  sut_id?: string | null;
  project: string;
  pipeline: string;
  branch: string;
  parameters: Record<string, string>;
};
type AiReply = {
  source_manifest?: SourceManifest | null;
  planning_knowledge?: {automation:Knowledge|null;sut:Knowledge|null} | null;
  run_id: string | null;
  answer: {
    source_citations?: {handle:string;claim:string}[];
    missing_context?: string[];
    classification?: { category: "suspected_product_bug" | "suspected_automation_bug" | "environment_infrastructure" | "inconclusive"; reason: string; artifact_ids: string[]; verification: string } | null;
    action: "clarify" | "propose" | "analyze" | "cancel" | "answer";
    message: string;
    selection: {
      project_id: string;
      pipeline_id: string;
      branch: string;
      parameters: Record<string, string>;
    } | null;
    hypotheses: { text: string; artifact_ids: string[] }[];
    bug_report_draft: string | null;
  };
  facts: {
    pipeline_status: string;
    test_status: string;
    counts: Record<string, number>;
    attempts_known: boolean;
    identity?: {sut_id?:string|null;project_id:string; framework:string; repository:string; branch:string; commit:string};
    knowledge?: Knowledge | null;
    sut_knowledge?: Knowledge | null;
    analysis_limitations?: string[];
    can_analyze_failure_details?: boolean;
  } | null;
  limitations: string[];
};
type Message = { id?: string; role: "user" | "relay"; text: string };
const initialMessage: Message = {
  id: "initial",
  role: "relay",
  text: "Choose a project and its parameters, then preview your execution plan. I’ll keep the run and its evidence together.",
};
const storage = {
  get(key: string) {
    try {
      return localStorage.getItem(`relay.ui.${key}`);
    } catch {
      return null;
    }
  },
  set(key: string, value: string) {
    try {
      localStorage.setItem(`relay.ui.${key}`, value);
    } catch {
      /* Storage may be disabled. */
    }
  },
};
function defaults(project: Project, pipeline: string): Record<string, string> {
  return Object.fromEntries(
    Object.entries(project.pipelines[pipeline]?.parameters || {})
      .filter(([, p]) => p.default !== null)
      .map(([name, p]) => [name, p.default!]),
  );
}
function Badge({ value }: { value: string }) {
  return (
    <span
      className={`badge ${["pass", "passed", "successful", "confirmed", "available"].includes(value) ? "good" : ["fail", "failed", "error", "rejected"].includes(value) ? "bad" : ["running", "pending", "active", "preview", "submitted"].includes(value) ? "warm" : ""}`}
    >
      <span />
      {value.replaceAll("_", " ")}
    </span>
  );
}
function errorText(error: unknown) {
  return error instanceof Error ? error.message : "Request failed";
}

export default function Workspace() {
  const [sourceCatalog,setSourceCatalog] = useState<SourceEntry[]>([]);
  const [sourceEnabled,setSourceEnabled] = useState(false);
  const [sourceIds,setSourceIds] = useState<string[]>([]);
  const [shareSources,setShareSources] = useState(false);
  const [savedSourceIdentity,setSavedSourceIdentity] = useState<{project:string;sut:string|null;environment:string|null}|null>(null);
  useEffect(()=>{void api<{enabled:boolean;sources:SourceEntry[]}>("/api/source-registry").then(value=>{setSourceEnabled(value.enabled);setSourceCatalog(value.sources);}).catch(()=>setSourceEnabled(false));},[]);
  const [knowledgeRefresh, setKnowledgeRefresh] = useState(0);
  const [aiEnabled, setAiEnabled] = useState(false);
  const [shareAi, setShareAi] = useState(true);
  const [shareFailures, setShareFailures] = useState(true);
  const [shareSutKnowledge, setShareSutKnowledge] = useState(false);
  const [savedSut, setSavedSut] = useState<string | null | undefined>(undefined);
  const [shareKnowledge, setShareKnowledge] = useState(false);
  const [preferencesReady, setPreferencesReady] = useState(false);
  const [replies, setReplies] = useState<Record<string, AiReply | null>>({});

  function setAiReply(value: AiReply | null) {
    setReplies(old => ({...old, [selected || "planning"]: value}));
  }
  const aiLock = useRef(false);
  const proposals = useRef<Record<string, AiReply["answer"]["selection"]>>({});
  useEffect(() => {
    void api<{ enabled: boolean; configured: boolean }>("/api/assistant")
      .then((s) => setAiEnabled(s.enabled && s.configured))
      .catch(() => setAiEnabled(false));
  }, []);
  useEffect(() => {
    setShareAi(storage.get("shareAi") !== "false");
    setShareFailures(storage.get("shareFailures") !== "false");
    setShareKnowledge(storage.get("shareKnowledge") === "true");
    setShareSutKnowledge(storage.get("shareSutKnowledge") === "true");
    setPreferencesReady(true);
  }, []);
  useEffect(() => {
    if (preferencesReady) {
      storage.set("shareSutKnowledge", String(shareSutKnowledge));
      storage.set("shareKnowledge", String(shareKnowledge));
      storage.set("shareAi", String(shareAi));
      storage.set("shareFailures", String(shareFailures));
    }
  }, [shareAi, shareFailures, shareKnowledge, shareSutKnowledge, preferencesReady]);
  const [registry, setRegistry] = useState<Registry | null>(null);
  const [context, setContext] = useState<Context>({
    project: "",
    pipeline: "",
    branch: "",
    parameters: {},
  });
  const [runs, setRuns] = useState<Run[]>([]);
  const [selected, setSelected] = useState("");
  type AnalysisInput = {expected_behavior: string; test_code: string; environment_notes: string; share: boolean};
  const [analysisInputs, setAnalysisInputs] = useState<Record<string, AnalysisInput>>({});
  const analysisInput = analysisInputs[selected] || {expected_behavior:"", test_code:"", environment_notes:"", share:false};
  function updateAnalysisInput(next: Partial<AnalysisInput>) {
    setAnalysisInputs(old => ({...old, [selected]: {...analysisInput, ...next}}));
  }
  const aiReply = replies[selected || "planning"] || null;
  const [run, setRun] = useState<Run | null>(null);
  const [evidence, setEvidence] = useState<Evidence | null>(null);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [recoverId, setRecoverId] = useState("");
  const [historyBoundary,setHistoryBoundary] = useState<Record<string,string>>({});
  const [conversations, setConversations] = useState<Record<string, Message[]>>({});
  const conversationKey = selected || "planning";
  const messages = conversations[conversationKey] || (selected ? [] : [initialMessage]);
  type ConversationFile = {id:string;filename:string;size:number;status:string};
  const [attachmentScope,setAttachmentScope]=useState("");
  const [scopeGeneration,setScopeGeneration]=useState(0);
  const [attachmentMap,setAttachmentMap]=useState<Record<string,ConversationFile[]>>({});
  const attachments=attachmentMap[attachmentScope] || [];
  const [attachmentError,setAttachmentError]=useState("");
  const [attachmentBusy,setAttachmentBusy]=useState(false);
  const [uploadRetry,setUploadRetry]=useState<{scope:string;files:File[]}|null>(null);
  const attachmentInput=useRef<HTMLInputElement>(null);
  useEffect(()=>{
    let live=true;setAttachmentScope("");setAttachmentError("");
    async function load(){
      try {
        let scope=storage.get(`attachment-scope-${conversationKey}`);
        let files:ConversationFile[]=[];
        if(scope){try{files=await api<ConversationFile[]>(`/api/conversations/${scope}/attachments`);}catch(e){if(e instanceof ApiError && e.status===404 && e.code==="not_found")scope=null;else throw e;}}
        if(!scope){const created=await api<{id:string}>("/api/conversations",{method:"POST"});scope=created.id;}
        if(live){storage.set(`attachment-scope-${conversationKey}`,scope);setAttachmentMap(old=>({...old,[scope!]:files}));setAttachmentScope(scope);}
      }catch(e){if(live)setAttachmentError(errorText(e));}
    }
    void load();return()=>{live=false;};
  },[conversationKey,scopeGeneration]);
  async function attachFiles(files:File[], scope=attachmentScope){
    if(!files.length||!scope)return;
    setAttachmentBusy(true);setAttachmentError("");setUploadRetry(null);
    let index=0;
    try {
      for(;index<files.length;index++){
        const file=files[index];
        const item=await api<ConversationFile>(`/api/conversations/${scope}/attachments?filename=${encodeURIComponent(file.name)}`,{method:"POST",headers:{"Content-Type":file.type||"application/octet-stream"},body:file});
        setAttachmentMap(old=>({...old,[scope]:(old[scope]||[]).some(x=>x.id===item.id)?old[scope]:[...(old[scope]||[]),item]}));
      }
    } catch(e) {setUploadRetry({scope,files:files.slice(index)});setAttachmentError(errorText(e));}
    finally {setAttachmentBusy(false);if(attachmentInput.current)attachmentInput.current.value="";}
  }
  async function removeAttachment(id:string){const scope=attachmentScope;try{await api(`/api/conversations/${scope}/attachments/${id}`,{method:"DELETE"});setAttachmentMap(old=>({...old,[scope]:(old[scope]||[]).filter(x=>x.id!==id)}));}catch(e){setAttachmentError(errorText(e));}}
  function excludePriorHistory() {
    const last = messages.at(-1);
    if (last?.id) setHistoryBoundary(old=>({...old,[conversationKey]:last.id!}));
  }
  const setMessages = useCallback((value: Message[] | ((old: Message[]) => Message[])) => {
    setConversations(old => ({...old, [conversationKey]: typeof value === "function" ? value(old[conversationKey] || (selected ? [] : [initialMessage])) : value}));
  }, [conversationKey, selected]);
  const [command, setCommand] = useState("");
  const [error, setError] = useState("");
  const [aiError, setAiError] = useState<{message: string; text: string; proposeKnowledge?: boolean} | null>(null);
  const [historyError, setHistoryError] = useState("");
  const [runError, setRunError] = useState("");
  const [evidenceError, setEvidenceError] = useState("");
  const [busy, setBusy] = useState("");
  const [aiPendingConversation, setAiPendingConversation] = useState<string | null>(null);
  const thinkingRef = useRef<HTMLDivElement>(null);
  const showThinking = busy === "ai" && aiPendingConversation === conversationKey;
  useEffect(() => {
    if (showThinking) thinkingRef.current?.scrollIntoView({ block: "center" });
  }, [showThinking]);
  const [online, setOnline] = useState(false);
  const [contextOpen, setContextOpen] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [cancelConfirm, setCancelConfirm] = useState(false);
  const [reportPath, setReportPath] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [associationConfirmed, setAssociationConfirmed] = useState(false);
  const [page, setPage] = useState(0);
  const [more, setMore] = useState(false);
  const [clock, setClock] = useState(Date.now());
  const revision = useRef(0);
  const activeRun = useRef("");
  const previewLock = useRef(false);
  const executed = useRef(new Set<string>());
  const actionLock = useRef(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const project = registry?.projects[context.project];
  const pipeline = project?.pipelines[context.pipeline];
  const validContext = Boolean(
    project?.enabled &&
      pipeline &&
      context.branch &&
      Object.entries(pipeline.parameters).every(
        ([key, p]) =>
          (!p.required && !context.parameters[key]) ||
          p.allowed_values.includes(context.parameters[key] || ""),
      ),
  );
  const addMessage = useCallback(
    (message: Message) => setMessages((old) => [...old.slice(-39), {...message,id:crypto.randomUUID()}]),
    [setMessages],
  );

  const loadHistory = useCallback(async (offset = 0, append = false) => {
    try {
      const list = await api<Run[]>(
        `/api/remote-runs?limit=30&offset=${offset}`,
      );
      setRuns((old) => {
        const incoming = new Set(list.map((r) => r.id));
        return append
          ? [...old.filter((r) => !incoming.has(r.id)), ...list]
          : [...list, ...old.filter((r) => !incoming.has(r.id))];
      });
      if (append) setPage(offset / 30);
      setMore(list.length === 30);
      setHistoryError("");
      setOnline(true);
    } catch (e) {
      setHistoryError(errorText(e));
      setOnline(false);
    }
  }, []);

  const recoverPlan = useCallback(async (id: string) => {
    setRecoverId(id);
    try {
      const saved = await api<Plan>(`/api/plans/${encodeURIComponent(id)}`);
      if (saved.status === "submitted") {
        setSelected(id);
        storage.set("selected", id);
        setRecoverId("");
        storage.set("attemptedPlan", "");
      } else if (
        saved.status === "preview" &&
        saved.expired_unclaimed === true
      ) {
        setRecoverId("");
        storage.set("attemptedPlan", "");
        setPlan(null);
        setError("");
      } else if (saved.status === "rejected") {
        setRecoverId("");
        storage.set("attemptedPlan", "");
        setPlan(saved);
      } else {
        setRecoverId(id);
        setPlan(saved);
      }
    } catch (e) {
      setError(errorText(e));
    }
  }, []);

  useEffect(() => {
    let live = true;
    async function init() {
      try {
        const data = await api<Registry>("/api/project-registry");
        if (!live) return;
        setRegistry(data);
        setOnline(true);
        const first =
          Object.keys(data.projects).find((id) => data.projects[id].enabled) ||
          "";
        let restored: Context | null = null;
        try {
          restored = JSON.parse(storage.get("context") || "null");
        } catch {
          /* Reset invalid local data. */
        }
        const p = data.projects[restored?.project || ""];
        if (
          p?.enabled &&
          restored &&
          p.pipelines[restored.pipeline] &&
          p.allowed_branches.includes(restored.branch)
        ) {
          const parameters = Object.fromEntries(
            Object.entries(restored.parameters || {}).filter(([key, value]) =>
              p.pipelines[restored!.pipeline].parameters[
                key
              ]?.allowed_values.includes(value),
            ),
          );
          setContext({
            ...restored,
            sut_id: p.pipelines[restored.pipeline].sut_ids?.includes(restored.sut_id || "") ? restored.sut_id : null,
            parameters: { ...defaults(p, restored.pipeline), ...parameters },
          });
        } else if (first) {
          const p = data.projects[first];
          const key = Object.keys(p.pipelines)[0];
          setContext({
            project: first,
            pipeline: key,
            branch: p.default_branch,
            parameters: defaults(p, key),
          });
        }
        setSelected(storage.get("selected") || "");
        try {
          const saved = JSON.parse(storage.get("conversations-v1") || "{}");
          const clean: Record<string, Message[]> = {};
          if (saved && typeof saved === "object" && !Array.isArray(saved)) {
            for (const [key, value] of Object.entries(saved)) {
              if ((key === "planning" || /^[a-f0-9]{32}$/.test(key)) && Array.isArray(value))
                clean[key] = value.filter(m => m && ["user", "relay"].includes(m.role) && typeof m.text === "string").slice(-40).map(m=>({...m,id:m.id || crypto.randomUUID()}));
            }
          }
          setConversations(clean);
        } catch { /* Ignore invalid stored conversations. */ }
        const pending = storage.get("attemptedPlan");
        if (pending) {
          executed.current.add(pending);
          await recoverPlan(pending);
        }
        await loadHistory();
      } catch (e) {
        if (live) {
          setOnline(false);
          setError(errorText(e));
        }
      }
    }
    void init();
    return () => {
      live = false;
    };
  }, [loadHistory, recoverPlan]);

  useEffect(() => {
    if (context.project) storage.set("context", JSON.stringify(context));
  }, [context]);
  useEffect(() => {
    if (registry) storage.set("conversations-v1", JSON.stringify(conversations));
  }, [conversations, registry]);
  useEffect(() => {
    const timer = setInterval(() => setClock(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  useEffect(() => {
    const timer = setInterval(() => void loadHistory(), 15000);
    return () => clearInterval(timer);
  }, [loadHistory]);
  useEffect(() => {
    setAiError(null);
    activeRun.current = selected;
    setShareSources(false); setSourceIds([]);
    revision.current++;
    setPlan(null);
    setError("");
    setRun(null);
    setSavedSut(undefined);
    setSavedSourceIdentity(null);
    setEvidence(null);
    setRunError("");
    setEvidenceError("");
    setCancelConfirm(false);
    setFile(null);
    setAssociationConfirmed(false);
    setReportPath("");
    setUploadReports([]);
    if (fileRef.current) fileRef.current.value = "";
    if (!selected) return;
    storage.set("selected", selected);
    let stopped = false,
      inFlight = false;
    async function poll() {
      if (inFlight) return;
      inFlight = true;
      try {
        const value = await api<Run>(
          `/api/remote-runs/${encodeURIComponent(selected)}`,
        );
        if (!stopped) {
          setRun(value);
          setRunError("");
          setOnline(true);
        }
      } catch (e) {
        if (!stopped) setRunError(errorText(e));
      } finally {
        inFlight = false;
      }
    }
    void poll();
    const timer = setInterval(() => void poll(), 5000);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [selected]);

  const loadEvidence = useCallback(async (id: string) => {
    try {
      const [evidenceResult, planResult] = await Promise.allSettled([
        api<Evidence>(`/api/remote-runs/${encodeURIComponent(id)}/evidence`),
        api<Plan>(`/api/plans/${encodeURIComponent(id)}`),
      ]);
      if (activeRun.current !== id) return;
      if (planResult.status === "fulfilled") {
        setSavedSut(planResult.value.plan.sut_id || null);
        const p = planResult.value.plan;
        const keys = Object.entries(p.project.pipelines[p.pipeline_id].parameters).filter(([,spec])=>spec.kind === "environment").map(([key])=>key);
        setSavedSourceIdentity({project:p.project_id,sut:p.sut_id || null,environment:keys.length===1?p.parameters[keys[0]] || null:null});
      }
      if (evidenceResult.status === "rejected") throw evidenceResult.reason;
      if (planResult.status === "rejected") throw planResult.reason;
      const value = evidenceResult.value;
      const savedPlan = planResult.value;
      if (activeRun.current === id) {
        setSavedSut(savedPlan.plan.sut_id || null);
        setEvidence(value);
        setEvidenceError("");
        setReportPath((old) =>
          savedPlan.plan.project.reports.some((r) => r.path === old)
            ? old
            : savedPlan.plan.project.reports[0]?.path || "",
        );
        setUploadReports(savedPlan.plan.project.reports);
      }
    } catch (e) {
      if (activeRun.current === id) setEvidenceError(errorText(e));
    }
  }, []);
  const [uploadReports, setUploadReports] = useState<
    { format: string; path: string }[]
  >([]);
  useEffect(() => {
    if (selected) void loadEvidence(selected);
  }, [selected, loadEvidence]);

  function changeContext(next: Context) {
    if (shareSources) {
      setShareSources(false); setSourceIds([]);
      excludePriorHistory();
    }
    setAiError(null);
    revision.current++;
    proposals.current[selected || "planning"] = null;
    setContext(next);
    setAiReply(null);
    setPlan(null);
    setError("");
  }
  function chooseProject(id: string) {
    const p = registry?.projects[id];
    if (!p) return;
    const key = Object.keys(p.pipelines)[0];
    changeContext({
      project: id,
      pipeline: key,
      branch: p.default_branch,
      parameters: defaults(p, key),
    });
  }
  async function preview() {
    if (
      !validContext ||
      recoverId ||
      storage.get("attemptedPlan") ||
      previewLock.current ||
      actionLock.current
    )
      return;
    previewLock.current = true;
    setBusy("preview");
    setError("");
    setPlan(null);
    const version = revision.current;
    try {
      const value = await post<Plan>("/api/plans/preview", {
        project_id: context.project,
        pipeline_id: context.pipeline,
        sut_id: context.sut_id || null,
        branch: context.branch,
        parameters: context.parameters,
      });
      if (version === revision.current) {
        setPlan(value);
        addMessage({
          role: "relay",
          text: `Plan ready for ${value.plan.project_id} / ${value.plan.pipeline_id}. Review the pinned commit and parameters before running.`,
        });
      }
    } catch (e) {
      if (version === revision.current) setError(errorText(e));
    } finally {
      previewLock.current = false;
      setBusy("");
    }
  }
  async function execute() {
    if (
      !plan ||
      recoverId ||
      storage.get("attemptedPlan") ||
      busy ||
      actionLock.current ||
      executed.current.has(plan.id) ||
      clock >= plan.expires_at * 1000
    )
      return;
    const value = plan;
    executed.current.add(value.id);
    actionLock.current = true;
    setBusy("execute");
    setError("");
    // Persist ownership before POST: the response can be lost after acceptance.
    const owned = {...conversations, [value.id]: conversations[value.id] || messages};
    storage.set("conversations-v1", JSON.stringify(owned));
    setConversations(owned);
    storage.set("attemptedPlan", value.id);
    setRecoverId(value.id);
    try {
      const result = await post<Plan>(`/api/plans/${value.id}/execute`, {
        confirmed: true,
      });
      setPlan(result);
      if (result.status === "submitted") {
        setConversations(old => ({...old, [result.id]: [...(old[result.id] || messages), {role: "relay", text: "Bitbucket accepted the pipeline."}]}));
        setSelected(result.id);
        setRecoverId("");
        storage.set("attemptedPlan", "");
        addMessage({
          role: "relay",
          text: "Bitbucket accepted the pipeline. Pipeline progress and test evidence are tracked separately below.",
        });
        await loadHistory();
      } else if (result.status === "rejected") {
        setRecoverId("");
        storage.set("attemptedPlan", "");
        setError(
          result.error || "Submission rejected. Preview a new plan to retry.",
        );
      } else {
        setRecoverId(result.id);
        setError(
          result.error ||
            `Submission state: ${result.status}. Check the saved plan before starting another run.`,
        );
      }
    } catch (e) {
      if (
        e instanceof ApiError &&
        e.status === 409 &&
        ["stale_plan", "expired_plan"].includes(e.code || "")
      ) {
        setRecoverId("");
        storage.set("attemptedPlan", "");
        setPlan(null);
      } else {
        setRecoverId(value.id);
      }
      setError(errorText(e));
    } finally {
      actionLock.current = false;
      setBusy("");
    }
  }
  async function runAction(kind: "refresh" | "cancel") {
    if (!selected || actionLock.current) return;
    const id = selected;
    actionLock.current = true;
    setBusy(kind);
    setRunError("");
    try {
      const value = await post<Run>(
        `/api/remote-runs/${id}/${kind}`,
        kind === "cancel" ? { confirmed: true } : undefined,
      );
      if (activeRun.current === id) {
        setRun(value);
        setCancelConfirm(false);
      }
      await loadHistory();
    } catch (e) {
      if (activeRun.current === id) setRunError(errorText(e));
    } finally {
      actionLock.current = false;
      setBusy("");
    }
  }
  async function upload() {
    if (
      !file ||
      !reportPath ||
      !associationConfirmed ||
      !selected ||
      actionLock.current
    )
      return;
    if (file.size > 10_000_000) {
      setEvidenceError("Report exceeds the 10 MB limit.");
      return;
    }
    const id = selected;
    actionLock.current = true;
    setBusy("upload");
    setEvidenceError("");
    try {
      await api(
        `/api/remote-runs/${id}/evidence?report_path=${encodeURIComponent(reportPath)}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/octet-stream" },
          body: file,
        },
      );
      await loadEvidence(id);
      if (activeRun.current === id) {
        setFile(null);
        setAssociationConfirmed(false);
        if (fileRef.current) fileRef.current.value = "";
      }
    } catch (e) {
      if (activeRun.current === id) setEvidenceError(errorText(e));
    } finally {
      actionLock.current = false;
      setBusy("");
    }
  }
  async function askAi(text: string, proposeKnowledge = false) {
    if (
      !shareAi ||
      !aiEnabled ||
      aiLock.current ||
      actionLock.current ||
      previewLock.current
    ) {
      addMessage({
        role: "relay",
        text: "Enable AI sharing first, or use /preview, /status, or /help.",
      });
      return;
    }
    aiLock.current = true;
    actionLock.current = true;
    setAiError(null);
    setAiPendingConversation(conversationKey);
    setBusy("ai");
    setError("");
    setAiReply(null);
    // A follow-up may change the intended target; never leave an old plan runnable.
    setPlan(null);
    const version = revision.current;
    try {
      const value = await post<AiReply>("/api/assistant/chat", {
        message: text,
        propose_knowledge: proposeKnowledge,
        share_context: true,
        include_source_context: shareSources,
        source_ids: sourceIds,

        include_failure_details: shareFailures,
        include_automation_knowledge: shareKnowledge,
        include_sut_knowledge: shareSutKnowledge,
        analysis_context: selected && analysisInput.share ? {expected_behavior:analysisInput.expected_behavior, test_code:analysisInput.test_code, environment_notes:analysisInput.environment_notes} : null,
        previous_proposal: proposals.current[conversationKey] || null,
        context: project?.enabled && pipeline && context.branch
          ? {
              sut_id: context.sut_id || null,
              project_id: context.project,
              pipeline_id: context.pipeline,
              branch: context.branch,
              parameters: context.parameters,
            }
          : null,
        run_id: selected || null,
        history: messages
          .slice(historyBoundary[conversationKey] ? messages.findIndex(m=>m.id===historyBoundary[conversationKey])+1 : 0)
          .slice(-8)
          .map((m) => ({ role: m.role, text: m.text.slice(0, 4000) })),
      });
      if (version === revision.current) {
        if (value.answer.action === "propose") proposals.current[conversationKey] = value.answer.selection;
        setAiReply(value);
        if (proposeKnowledge) setKnowledgeRefresh(old=>old+1);
        addMessage({
          role: "relay",
          text: `AI suggestion: ${value.answer.message}`,
        });
      }
    } catch (e) {
      if (version === revision.current) setAiError({message: errorText(e), text, proposeKnowledge});
    } finally {
      aiLock.current = false;
      actionLock.current = false;
      setAiPendingConversation(null);
      setBusy("");
    }
  }
  function sendCommand(event: React.FormEvent) {
    event.preventDefault();
    if (busy || attachmentBusy || aiLock.current || actionLock.current || previewLock.current) return;
    const text = command.trim();
    if (!text) return;
    setCommand("");
    addMessage({ role: "user", text });
    if (text === "/preview") void preview();
    else if (text === "/status") {
      if (selected) void runAction("refresh");
      else
        addMessage({
          role: "relay",
          text: "Choose a run from the sidebar first.",
        });
    } else if (!text.startsWith("/")) void askAi(text);
    else
      addMessage({
        role: "relay",
        text: "Available commands: /preview for your selected context, /status for the selected run, and /help. Running a pipeline requires the Confirm & run button on its reviewed plan.",
      });
  }
  const expired = Boolean(plan && clock >= plan.expires_at * 1000);
  const pipelineLink = safeBitbucketUrl(run?.pipeline.url);

  return (
    <div className="workspace">
      <aside
        className={`sidebar ${historyOpen ? "mobile-open" : ""}`}
        aria-label="Workspace navigation"
      >
        <a className="brand" href="/ui/">
          <span className="brand-mark">»</span>
          <span>
            relay <small>QA</small>
          </span>
        </a>
        <div className="workspace-label">
          <span className="workspace-avatar">QA</span>
          <div>
            Your workspace<small>Local development</small>
          </div>
          <ShieldCheck size={16} />
        </div>
        <button
          className="nav-item selected"
          onClick={() => {
            setHistoryOpen(false);
            document.getElementById("conversation")?.focus();
          }}
        >
          <LayoutGrid size={17} />
          QA conversation
        </button>
        <button
          className="new-check"
          disabled={Boolean(busy)}
          onClick={() => {
            setAiError(null);
            storage.set("attachment-scope-planning", "");
            setScopeGeneration(old=>old+1);
            setSelected("");
            storage.set("selected", "");
            setRun(null);
            setEvidence(null);
            setPlan(null);
            setConversations(old => ({...old, planning: [initialMessage]}));
            proposals.current.planning = null;
            setReplies(old => ({...old, planning: null}));
            setHistoryOpen(false);
          }}
        >
          <Plus size={17} />
          New check
        </button>
        <div className="section-label">
          RUN HISTORY
          <button
            aria-label="Refresh run history"
            onClick={() => void loadHistory()}
          >
            <RefreshCw size={13} />
          </button>
        </div>
        {historyError && (
          <p className="small-error" role="status">
            {historyError}
          </p>
        )}
        <div className="history-list">
          {runs.length === 0 ? (
            <div className="empty-history">
              <History size={20} />
              <p>No remote runs yet.</p>
              <small>Your first run will appear here.</small>
            </div>
          ) : (
            runs.map((r) => (
              <button
                className={`history-item ${selected === r.id ? "current" : ""}`}
                key={r.id}
                disabled={Boolean(busy) && busy !== "ai"}
                onClick={() => {
                  setSelected(r.id);
                  setHistoryOpen(false);
                }}
              >
                <div>
                  <span className={`status-dot ${r.pipeline_status}`} />
                  <strong>{r.project_id}</strong>
                  <ChevronRight size={13} />
                </div>
                <small>
                  {r.id.slice(0, 10)} · {r.pipeline_status} · SUT: {r.sut_id || "None"}
                </small>
              </button>
            ))
          )}
        </div>
        {more && (
          <button
            className="text-button older"
            onClick={() => {
              const next = page + 1;
              void loadHistory(next * 30, true);
            }}
          >
            Load older runs
          </button>
        )}
        <div className="sidebar-footer">
          <span className={`connection-dot ${online ? "connected" : ""}`} />
          {online ? "Backend connected" : "Connecting to backend"}
          <small>Private credentials stay on the backend</small>
        </div>
      </aside>

      <main className="main">
        <header className="topbar">
          <button
            className="mobile-control"
            aria-label="Open run history"
            onClick={() => setHistoryOpen(!historyOpen)}
          >
            <History size={19} />
          </button>
          <div>
            <span className="breadcrumb">Workspace</span>
            <ChevronRight size={13} />
            <strong>QA conversation</strong>
          </div>
          <div className="header-actions">
            <ThemePicker />
            <span className="mode-label">
              <span />
              {aiEnabled ? "QA assistant" : "Structured commands"}
            </span>
            <button
              aria-label="Toggle execution context"
              onClick={() => setContextOpen(!contextOpen)}
            >
              <PanelRight size={19} />
            </button>
          </div>
        </header>
        <div className="conversation" id="conversation" tabIndex={-1}>
          <div className="eyebrow">YOUR QA WORKSPACE</div>
          <h1>Let’s check it together.</h1>
          <p className="intro">
            Choose your context. Review the plan. Follow the evidence.
          </p>
          <div className="context-chips">
            <span>
              <FlaskConical size={14} />
              {context.project || "No project selected"}
            </span>
            <span>{context.branch || "Choose a branch"}</span>
            <span className="muted-chip">
              {aiEnabled ? "AI available · opt-in" : "AI not connected"}
            </span>
          </div>
          {!plan && !selected && messages.length <= 1 && (
            <section className="welcome">
              <span className="welcome-icon">
                <Sparkles size={27} />
              </span>
              <h2>What are we testing today?</h2>
              <p>
                Start with your configured tests, or pick up a previous run.
                <br />
                We’ll keep the plan and its evidence in view.
              </p>
              <button
                className="welcome-action"
                disabled={!validContext || Boolean(busy)}
                onClick={() => void preview()}
              >
                <FlaskConical size={16} />
                <span>
                  <strong>Run a configured check</strong>
                  <small>
                    Review your selected project, branch and parameters
                  </small>
                </span>
                <ArrowUpRight size={14} />
              </button>
              <button
                className="welcome-action"
                onClick={() => {
                  setHistoryOpen(true);
                  document.querySelector(".history-list")?.scrollIntoView();
                }}
              >
                <History size={16} />
                <span>
                  <strong>Inspect a previous run</strong>
                  <small>
                    Follow pipeline progress and inspect test evidence
                  </small>
                </span>
                <ArrowUpRight size={14} />
              </button>
            </section>
          )}
          <div
            className="conversation-thread"
            aria-label="QA conversation history"
          >
            {selected && messages.length === 0 && <p>No conversation for this run yet.</p>}
          {messages.map((m, i) => (
              <div className={`message ${m.role}`} key={i}>
                <span className="message-avatar">
                  {m.role === "relay" ? "r" : "Y"}
                </span>
                <div>
                  <span className="message-author">
                    {m.role === "relay" ? "Relay" : "You"}
                  </span>
                  <p>{m.text}</p>
                </div>
              </div>
            ))}
            {showThinking && (
              <div className="message relay" ref={thinkingRef} role="status" aria-live="polite" aria-atomic="true">
                <span className="message-avatar" aria-hidden="true">r</span>
                <div>
                  <span className="message-author">Relay</span>
                  <p className="ai-thinking">
                    AI is thinking
                    <span className="thinking-dots" aria-hidden="true"><span /><span /><span /></span>
                  </p>
                </div>
              </div>
            )}
          </div>
          {aiError && (
            <div className="error-banner" role="alert">
              {aiError.message} Retry sends this message to AI again; review sharing settings if needed.
              <button className="text-button" disabled={Boolean(busy) || !shareAi || !aiEnabled}
                onClick={() => void askAi(aiError.text, aiError.proposeKnowledge)}>Retry AI message</button>
              <button className="text-button" onClick={() => setAiError(null)}>Dismiss</button>
            </div>
          )}
          {error && (
            <div className="error-banner" role="alert">
              {error}
              <button
                className="text-button"
                onClick={() => window.location.reload()}
              >
                Reconnect
              </button>
            </div>
          )}
          {recoverId && (
            <div className="recovery">
              <CircleHelp size={18} />
              <div>
                <strong>Check the saved submission</strong>
                <p>
                  Request {recoverId.slice(0, 12)}. Recovery only reads the
                  plan; it never submits again.
                </p>
              </div>
              <button onClick={() => void recoverPlan(recoverId)}>
                Check status
              </button>
            </div>
          )}
          {aiReply && (
            <section className="plan-card ai-card" aria-label="AI suggestion">
              <div className="card-heading">
                <Sparkles size={18} />
                <div>
                  <h2>AI suggestion</h2>
                  <p>No pipeline was started or stopped by AI.</p>
                </div>
              </div>
              <div className="ai-body">
                {aiReply.source_manifest && <SourceReferences key={aiReply.source_manifest.id} manifest={aiReply.source_manifest} citations={aiReply.answer.source_citations || []}/>}
                {aiReply.planning_knowledge && <section aria-label="Planning knowledge"><h3>Planning references</h3><p>Reference knowledge, not observed run evidence.</p>{Object.entries(aiReply.planning_knowledge).map(([category,bundle])=>bundle && <div key={category}><strong>{category}</strong><ul>{bundle.sources.map(source=><li key={source.id}>{source.path} · SHA256 {source.sha256}</li>)}{bundle.diagnostics.map((d,i)=><li key={i}>{d}</li>)}</ul></div>)}</section>}

                {aiReply.answer.action === "propose" &&
                  aiReply.answer.selection && (
                    <>
                      <p>
                        Proposed context: {aiReply.answer.selection.project_id}{" "}
                        / {aiReply.answer.selection.pipeline_id} /{" "}
                        {aiReply.answer.selection.branch}
                      </p>
                      <pre>
                        {JSON.stringify(
                          aiReply.answer.selection.parameters,
                          null,
                          2,
                        )}
                      </pre>
                      <button
                        disabled={Boolean(busy) || Boolean(recoverId)}
                        onClick={() => {
                          const s = aiReply.answer.selection!;
                          changeContext({
                            project: s.project_id,
                            pipeline: s.pipeline_id,
                            sut_id: s.project_id === context.project && registry?.projects[s.project_id]?.pipelines[s.pipeline_id]?.sut_ids?.includes(context.sut_id || "") ? context.sut_id : null,
                            branch: s.branch,
                            parameters: s.parameters,
                          });
                        }}
                      >
                        Use proposed context
                      </button>
                      <p>
                        Then preview the pinned plan and confirm it explicitly.
                      </p>
                    </>
                  )}
                {aiReply.facts && (
                  <details open>
                    <summary>Observed report summary</summary>
                    {aiReply.facts.identity && <p>Analyzed run: {aiReply.facts.identity.project_id} · {aiReply.facts.identity.framework} · {aiReply.facts.identity.branch} · commit {aiReply.facts.identity.commit}</p>}
                    {([['Automation', aiReply.facts.knowledge], ['SUT', aiReply.facts.sut_knowledge]] as const).map(([category, bundle]) => bundle && <section key={category} aria-label={`${category} knowledge used`}><h3>{category} knowledge used</h3><p>Operator-maintained references, not runtime evidence.</p><ul>{bundle.sources.map(source => <li key={source.id}>{source.category || category} · {source.scope_id || aiReply.facts?.identity?.project_id} · {source.document_id} · revision {source.revision} · SHA256 {source.sha256} · reviewed {source.reviewed_at}{source.source_repository && <> · source {source.source_repository} / {source.source_path} @ {source.source_commit || "unknown commit"}</>}</li>)}</ul><ul>{bundle.diagnostics.map((item,i) => <li key={i}>{item}</li>)}</ul></section>)}
                    {aiReply.facts.analysis_limitations && <>
                      <p>{aiReply.facts.can_analyze_failure_details ? "Available failure details can be analyzed with these limits." : "Analysis uses only available shared report data."}</p>
                      <ul>{aiReply.facts.analysis_limitations.map((limit, i) => <li key={i}>{limit}</li>)}</ul>
                    </>}
                    <p>
                      Pipeline: {aiReply.facts.pipeline_status} · Tests:{" "}
                      {aiReply.facts.test_status}
                    </p>
                    <p>
                      {Object.entries(aiReply.facts.counts)
                        .map(([s, n]) => `${s}: ${n}`)
                        .join(" · ") || "No tests collected"}
                    </p>
                    <p>
                      Attempt counts:{" "}
                      {aiReply.facts.attempts_known
                        ? "reported"
                        : "unknown or incomplete"}
                    </p>
                  </details>
                )}
                {Boolean(aiReply.answer.missing_context?.length) && <section aria-label="Missing analysis context"><h3>Context needed</h3><ul>{aiReply.answer.missing_context!.map((item,i)=><li key={i}>{item}</li>)}</ul></section>}
                {aiReply.answer.classification && <section aria-label="Failure classification">
                  <h3>Failure classification — unverified</h3>
                  <strong>{{suspected_product_bug: "Suspected product bug", suspected_automation_bug: "Suspected automation bug", environment_infrastructure: "Environment / infrastructure", inconclusive: "Inconclusive"}[aiReply.answer.classification.category]}</strong>
                  <p>{aiReply.answer.classification.reason}</p>
                  <p>Verification: {aiReply.answer.classification.verification}</p>
                  {aiReply.answer.classification.artifact_ids.map(id => <a key={id} href={`/api/remote-runs/${encodeURIComponent(aiReply.run_id!)}/evidence/${encodeURIComponent(id)}`}>Evidence {id.slice(0,10)} </a>)}
                </section>}
                {aiReply.answer.hypotheses.map((h, i) => (
                  <div key={i}>
                    <h3>Hypothesis — unverified</h3>
                    <p>{h.text}</p>
                    {h.artifact_ids.map((id) => (
                      <a
                        key={id}
                        href={`/api/remote-runs/${encodeURIComponent(aiReply.run_id!)}/evidence/${encodeURIComponent(id)}`}
                      >
                        Evidence {id.slice(0, 10)}{" "}
                      </a>
                    ))}
                  </div>
                ))}
                {aiReply.answer.bug_report_draft && (
                  <details>
                    <summary>Bug report draft — review before sharing</summary>
                    <pre className="bug-report-draft" aria-label="Bug report draft text">{aiReply.answer.bug_report_draft}</pre>
                  </details>
                )}
                {aiReply.answer.action === "cancel" &&
                  aiReply.run_id === selected &&
                  run &&
                  !run.terminal &&
                  run.cancel_status === "none" && (
                    <button
                      disabled={Boolean(busy)}
                      onClick={() => {
                        setCancelConfirm(true);
                        document
                          .querySelector('[aria-label="Selected run"]')
                          ?.scrollIntoView({ behavior: "smooth" });
                      }}
                    >
                      Review cancellation
                    </button>
                  )}
                {aiReply.limitations.map((text, i) => (
                  <p key={i}>
                    <small>{text}</small>
                  </p>
                ))}
              </div>
            </section>
          )}
          {plan && (
            <section className="plan-card" aria-label="Execution plan">
              <div className="card-heading">
                <span className="icon-box">
                  <Play size={16} />
                </span>
                <div>
                  <h2>Execution plan</h2>
                  <p>Review exactly what will run.</p>
                </div>
                <Badge value={plan.status} />
              </div>
              <dl className="plan-grid">
                <div>
                  <dt>PROJECT / PIPELINE</dt>
                  <dd>
                    {plan.plan.project_id} / {plan.plan.pipeline_id} · SUT: {plan.plan.sut_id || "None"}
                  </dd>
                </div>
                <div>
                  <dt>BRANCH</dt>
                  <dd>{plan.plan.branch}</dd>
                </div>
                <div className="wide">
                  <dt>REPOSITORY</dt>
                  <dd>{plan.plan.project.repository}</dd>
                </div>
                <div>
                  <dt>SELECTOR</dt>
                  <dd>{plan.selector}</dd>
                </div>
                <div>
                  <dt>PINNED COMMIT</dt>
                  <dd>
                    <code title={plan.commit}>{plan.commit.slice(0, 12)}</code>
                  </dd>
                </div>
              </dl>
              <div className="parameter-review">
                {Object.entries(plan.plan.parameters).map(([key, value]) => (
                  <span key={key}>
                    <small>{key}</small>
                    <b>{value}</b>
                  </span>
                ))}
              </div>
              <div className="plan-footer">
                <span>
                  <ShieldCheck size={15} />
                  {plan.status !== "preview"
                    ? `Submission: ${plan.status}`
                    : expired
                      ? "Preview expired. Create a new plan."
                      : "Only runs after your confirmation."}
                </span>
                <button
                  className="primary"
                  disabled={
                    Boolean(busy) ||
                    expired ||
                    plan.status !== "preview" ||
                    executed.current.has(plan.id)
                  }
                  onClick={() => void execute()}
                >
                  {busy === "execute" ? (
                    <LoaderCircle className="spin" size={16} />
                  ) : (
                    <Play size={15} />
                  )}
                  Confirm & run
                </button>
              </div>
            </section>
          )}

          {selected && (
            <section className="run-card" aria-label="Selected run"><details open key={`progress-${selected}`}>
              <summary className="card-heading">
                <span className="icon-box">
                  <History size={16} />
                </span>
                <div>
                  <h2>Run progress</h2>
                  <p className="mono">{selected}</p>
                </div>
                {run && <Badge value={run.pipeline_status} />}
              </summary>
              {runError && (
                <p role="alert" className="small-error">
                  {runError}
                </p>
              )}
              {!run && !runError && (
                <p className="loading">
                  <LoaderCircle size={16} className="spin" />
                  Loading run…
                </p>
              )}
              {run && (
                <>
                  <div className="run-info">
                    <span>
                      Monitoring{" "}
                      <b>{run.monitoring_status.replaceAll("_", " ")}</b>
                    </span>
                    <span>
                      Last checked{" "}
                      <b>
                        {run.last_checked_at
                          ? new Date(
                              run.last_checked_at * 1000,
                            ).toLocaleTimeString()
                          : "Not yet"}
                      </b>
                    </span>
                    {pipelineLink && (
                      <a href={pipelineLink} target="_blank" rel="noreferrer">
                        Open in Bitbucket
                        <ArrowUpRight size={14} />
                      </a>
                    )}
                  </div>
                  {run.error && (
                    <div role="status" className="error-banner">
                      {run.error.message} Showing the last saved observation.
                    </div>
                  )}
                  <div className="steps">
                    {run.steps.length ? (
                      run.steps.map((step, i) => (
                        <div className="step" key={step.id}>
                          <span className="step-index">
                            {step.status === "successful" ? (
                              <Check size={14} />
                            ) : (
                              i + 1
                            )}
                          </span>
                          <span>
                            {step.name}
                            <small>
                              {step.result || "Waiting for a remote result"}
                            </small>
                          </span>
                          <Badge value={step.status} />
                        </div>
                      ))
                    ) : (
                      <p className="empty-inline">
                        No steps have been observed yet.
                      </p>
                    )}
                  </div>
                  {run.cancel_status !== "none" && (
                    <p className="cancel-note">
                      Cancellation: <b>{run.cancel_status}</b>.{" "}
                      {run.cancel_error?.message ||
                        (run.cancel_status === "acknowledged"
                          ? "Waiting for Bitbucket to confirm the pipeline stopped."
                          : "")}
                    </p>
                  )}
                  <div className="run-controls">
                    <button
                      disabled={Boolean(busy)}
                      onClick={() => void runAction("refresh")}
                    >
                      <RefreshCw
                        size={14}
                        className={busy === "refresh" ? "spin" : ""}
                      />
                      Refresh remote status
                    </button>
                    {!run.terminal && run.cancel_status === "none" && (
                      <button
                        className="danger-text"
                        disabled={Boolean(busy)}
                        onClick={() => setCancelConfirm(true)}
                      >
                        <Square size={13} />
                        Cancel pipeline
                      </button>
                    )}
                  </div>
                  {cancelConfirm && (
                    <div
                      className="cancel-confirm"
                      role="group"
                      aria-label="Confirm cancellation"
                    >
                      <p>
                        Stop this pipeline in Bitbucket? A stop request remains
                        pending until its remote status is verified.
                      </p>
                      <button
                        disabled={Boolean(busy)}
                        onClick={() => void runAction("cancel")}
                      >
                        Confirm cancellation
                      </button>
                      <button onClick={() => setCancelConfirm(false)}>
                        Keep running
                      </button>
                    </div>
                  )}
                </>
              )}
            </details></section>
          )}

          {selected && (
            <section
              className="evidence-card"
              aria-label="Test evidence"
              id="test-evidence"
            ><details open key={`evidence-${selected}`}>
              <summary className="card-heading">
                <span className="icon-box">
                  <FileText size={16} />
                </span>
                <div>
                  <h2>Test evidence</h2>
                  <p>Actual report results, separate from pipeline status.</p>
                </div>
                {evidence && <Badge value={evidence.test_status} />}
              </summary>
              {evidenceError && (
                <p className="small-error" role="alert">
                  {evidenceError}
                </p>
              )}
              <button
                className="text-button"
                onClick={() => void loadEvidence(selected)}
              >
                <RefreshCw size={13} />
                Refresh evidence
              </button>
              {evidence && (
                <>
                  <div className="artifacts">
                    {evidence.artifacts.map((a, i) => (
                      <div key={a.id || `${a.name}-${i}`}>
                        <FileText size={15} />
                        <span>
                          {a.name}
                          <small>
                            {a.availability} · {a.parse_status}
                            {a.source ? ` · ${a.source}` : ""}
                          </small>
                          {a.error && (
                            <small className="small-error">{a.error}</small>
                          )}
                        </span>
                        {a.id && a.availability === "available" && (
                          <a
                            href={`/api/remote-runs/${encodeURIComponent(selected)}/evidence/${encodeURIComponent(a.id)}`}
                          >
                            Download
                          </a>
                        )}
                      </div>
                    ))}
                  </div>
                  {!evidence.artifacts.length && (
                    <p className="empty-inline">
                      No report paths are configured for this run.
                    </p>
                  )}
                  <div className="test-list">
                    {evidence.tests.map((test) => (
                      <details key={test.id}>
                        <summary>
                          <Badge value={test.status} />
                          <span>{test.name}</span>
                          <ChevronRight size={13} />
                        </summary>
                        {test.message && <pre>{test.message}</pre>}
                        <small>
                          Attempts: {test.attempts ?? "unknown"} · Evidence{" "}
                        </small>
                        {test.artifact_ids.map((id) => (
                          <a
                            key={id}
                            href={`/api/remote-runs/${encodeURIComponent(selected)}/evidence/${encodeURIComponent(id)}`}
                          >
                            {id.slice(0, 10)}{" "}
                          </a>
                        ))}
                      </details>
                    ))}
                  </div>
                </>
              )}
              <div className="upload-panel">
                <h3>
                  <Upload size={15} />
                  Upload a report
                </h3>
                <p>
                  Download the file from the pipeline’s Artifacts tab. Extract
                  archives locally; upload the report itself (max 10 MB).
                </p>
                <label>
                  Configured report path
                  <select
                    value={reportPath}
                    onChange={(e) => {
                      setReportPath(e.target.value);
                      setAssociationConfirmed(false);
                    }}
                    disabled={!uploadReports.length || Boolean(busy)}
                  >
                    {uploadReports.map((r) => (
                      <option key={r.path} value={r.path}>
                        {r.path} · {r.format}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="file-label">
                  Report file
                  <input
                    ref={fileRef}
                    type="file"
                    disabled={Boolean(busy)}
                    onChange={(e) => {
                      setFile(e.target.files?.[0] || null);
                      setAssociationConfirmed(false);
                    }}
                  />
                </label>
                <label className="attestation">
                  <input
                    type="checkbox"
                    checked={associationConfirmed}
                    onChange={(e) => setAssociationConfirmed(e.target.checked)}
                  />
                  I verified that this file belongs to the selected run.
                  Association is recorded as a manual assertion.
                </label>
                <button
                  disabled={
                    !file ||
                    !reportPath ||
                    !associationConfirmed ||
                    Boolean(busy)
                  }
                  onClick={() => void upload()}
                >
                  <Upload size={14} />
                  {busy === "upload" ? "Uploading…" : "Upload evidence"}
                </button>
                <small>
                  Report slots are immutable. An identical retry is safe; a
                  different file for an occupied slot will be rejected.
                </small>
              </div>
            </details></section>
          )}
        </div>
        <div className="composer-wrap">

          <form className="composer" onSubmit={sendCommand}>
            <div className="composer-files" aria-live="polite">{attachments.map(file=><span key={file.id}>{file.filename} · {Math.ceil(file.size/1024)} KB · Stored; format unverified, not analyzed <button type="button" aria-label={`Remove ${file.filename}`} onClick={()=>void removeAttachment(file.id)}><X size={13}/></button></span>)}{attachmentBusy&&<span role="status">Uploading…</span>}{attachmentError&&<span role="alert">{attachmentError} <button type="button" onClick={()=>{if(uploadRetry?.scope===attachmentScope)void attachFiles(uploadRetry.files,uploadRetry.scope);else setScopeGeneration(old=>old+1);}}>Retry</button></span>}</div>
            <input ref={attachmentInput} className="sr-only" type="file" multiple accept=".xml,.txt,.md,.pdf,.docx" onChange={e=>void attachFiles(Array.from(e.target.files||[]))}/>
            <button type="button" aria-label="Attach files" disabled={Boolean(busy)||attachmentBusy||!attachmentScope} onClick={()=>attachmentInput.current?.click()}><Paperclip size={18}/></button>
            <label className="sr-only" htmlFor="command">
              Structured command
            </label>
            <textarea
              id="command"
              rows={1}
              value={command}
              onInput={e=>{const el=e.currentTarget;el.style.height="auto";el.style.height=`${Math.min(el.scrollHeight,160)}px`;}}
              onKeyDown={e=>{if(e.key==="Enter"&&!e.shiftKey&&!e.nativeEvent.isComposing){e.preventDefault();e.currentTarget.form?.requestSubmit();}}}
              onChange={(e) => setCommand(e.target.value)}
              maxLength={4000}
              placeholder={
                aiEnabled && shareAi
                  ? "Ask about your tests or suggest a check…"
                  : "Try /preview or /status…"
              }
            />
            <button
              type="submit"
              aria-label="Send command"
              disabled={!command.trim() || Boolean(busy)}
            >
              <ArrowUp size={20} />
            </button>
          </form>
          <p>
            <span>
              Explicit commands. Every run starts with a reviewed plan.
            </span>
            <kbd>/help</kbd>
          </p>
        </div>
      </main>

      <aside
        className={`context-panel ${contextOpen ? "mobile-open" : ""}`}
        aria-label="Execution context"
      >
        <div className="context-heading">
          <span>
            <Settings2 size={16} />
            Execution context
          </span>
          <button
            aria-label="Close execution context"
            onClick={() => setContextOpen(false)}
          >
            <X size={16} />
          </button>
        </div>
          {aiEnabled && selected && <details className="analysis-context" key={selected}>
            <summary>Analysis context for this run</summary>
            <p>Optional operator-provided context, not verified report evidence. These inputs stay in page memory and reset on reload. Included only when sharing below is enabled; existing AI sharing settings also apply.</p>
            {([['expected_behavior','Expected behavior / acceptance criteria',4000],['test_code','Relevant test / keyword / assertion code',6000],['environment_notes','Environment notes',2000]] as const).map(([field,label,limit])=><label key={field}>{label}<textarea value={analysisInput[field]} maxLength={limit} disabled={Boolean(busy)} onChange={e=>updateAnalysisInput({[field]:e.target.value})}/></label>)}
            <label><input type="checkbox" checked={analysisInput.share} disabled={Boolean(busy)} onChange={e=>updateAnalysisInput({share:e.target.checked})}/>Include this run’s additional context in AI requests</label>
            <button disabled={Boolean(busy)} onClick={()=>updateAnalysisInput({expected_behavior:"",test_code:"",environment_notes:"",share:false})}>Clear analysis context</button>
          </details>}
          {aiEnabled && <SourceControls enabled={sourceEnabled} sources={sourceCatalog.filter(source=>{
            const identity = selected ? savedSourceIdentity : {project:context.project,sut:context.sut_id || null,environment:Object.entries(pipeline?.parameters || {}).filter(([,p])=>p.kind==="environment").length===1?context.parameters[Object.entries(pipeline?.parameters || {}).find(([,p])=>p.kind==="environment")![0]]:null};
            return identity && source.scope_id === (source.category==="sut"?identity.sut:identity.project) && source.environments.includes(identity.environment || "");
          })} selected={sourceIds} consent={shareSources} busy={Boolean(busy)} onChange={(ids,consent)=>{revision.current++;setSourceIds(ids);setShareSources(consent);setReplies({});excludePriorHistory();setAiError(null);}}/>}
          {aiEnabled && (
            <details className="ai-sharing"><summary>Settings · AI context sharing</summary><label><input type="checkbox" checked={shareKnowledge} disabled={Boolean(busy)} onChange={e=>{revision.current++;setShareKnowledge(e.target.checked);setReplies({});setAiError(null);}}/>Include approved automation knowledge for planning or the selected run</label><label><input type="checkbox" checked={shareSutKnowledge} disabled={Boolean(busy)} onChange={e=>{revision.current++;setShareSutKnowledge(e.target.checked);setReplies({});setAiError(null);}}/>Include approved SUT knowledge for planning or the selected run</label><p>Off by default. When enabled, applicable private knowledge documents are redacted and sent to the configured AI provider, which may be upstream of 9router.</p><p>Chat, context, run summaries and optional failure details go to the configured model provider. Local gateways may forward data upstream. Redaction is best-effort; raw artifacts are not sent.</p>
              <label>
                <input
                  type="checkbox"
                  checked={shareAi}
                  disabled={Boolean(busy)}
                  onChange={(e) => {
                    revision.current++; setReplies({}); excludePriorHistory(); setAiError(null);
                    setShareAi(e.target.checked);

                  }}
                />
                Send chat, recent conversation, configured context and selected
                run summary to AI
              </label>
              <label>
                <input
                  type="checkbox"
                  checked={shareFailures}
                  disabled={Boolean(busy)}
                  onChange={(e) => {revision.current++;setReplies({});excludePriorHistory();setAiError(null);setShareFailures(e.target.checked);}}
                />
                Include failure names/messages (best-effort redaction; verify
                sensitive content first)
              </label>
            </details>
          )}

        {selected && <>
          {aiEnabled && <button disabled={Boolean(busy)||!shareAi||(!shareKnowledge&&!shareSutKnowledge)} onClick={()=>void askAi("Propose concise reusable knowledge from this run's shared failure evidence. Keep hypotheses unverified and use only the selected sharing categories.", true)}>Suggest knowledge from this run</button>}
          <KnowledgeReview key={selected} runId={selected} refresh={knowledgeRefresh} busy={Boolean(busy)} onBusy={value=>{actionLock.current=value;setBusy(value?"knowledge":"");}} onChanged={()=>{revision.current++;setReplies({});setKnowledgeRefresh(old=>old+1);}}/>
        </>}
        {selected && <p aria-label="Selected run SUT">Selected run SUT: {savedSut === undefined ? "Unknown" : savedSut || "None"}</p>}
        <p className="context-description">
          Execution selection for a new plan. Analysis uses the selected run’s saved project and commit.
        </p>
        <fieldset disabled={!registry || Boolean(busy)}>
          <label>
            Project
            <select
              aria-label="Project"
              value={context.project}
              onChange={(e) => chooseProject(e.target.value)}
            >
              <option value="" disabled>
                Select project
              </option>
              {Object.entries(registry?.projects || {}).map(([id, p]) => (
                <option key={id} value={id} disabled={!p.enabled}>
                  {id}
                  {p.enabled ? "" : " (disabled)"}
                </option>
              ))}
            </select>
          </label>
          {project && (
            <div className="repo-note">
              <span>{project.framework || project.provider}</span>
              <code>{project.repository}</code>
            </div>
          )}
          <label>
            Branch
            <select
              aria-label="Branch"
              value={context.branch}
              onChange={(e) =>
                changeContext({ ...context, branch: e.target.value })
              }
            >
              {project?.allowed_branches.map((branch) => (
                <option key={branch}>{branch}</option>
              ))}
            </select>
          </label>
          <label>
            Pipeline
            <select
              aria-label="Pipeline"
              value={context.pipeline}
              onChange={(e) => {
                const key = e.target.value;
                if (project)
                  changeContext({
                    ...context,
                    pipeline: key,
                    sut_id: null,
                    parameters: defaults(project, key),
                  });
              }}
            >
              {Object.keys(project?.pipelines || {}).map((key) => (
                <option key={key}>{key}</option>
              ))}
            </select>
          </label>
          {Boolean(pipeline?.sut_ids?.length) && <label>System under test<select aria-label="System under test" value={context.sut_id || ""} onChange={e=>changeContext({...context,sut_id:e.target.value || null})}><option value="">Without SUT context</option>{pipeline?.sut_ids?.map(id=><option key={id} value={id}>{id}</option>)}</select></label>}
          <div className="parameter-heading">
            PARAMETERS<span>From project configuration</span>
          </div>
          {Object.entries(pipeline?.parameters || {}).map(([key, p]) => (
            <label key={key}>
              {key}
              {p.required && <span className="required">*</span>}
              <select
                aria-label={key}
                value={context.parameters[key] || ""}
                onChange={(e) => {
                  const parameters = { ...context.parameters };
                  if (e.target.value) parameters[key] = e.target.value;
                  else delete parameters[key];
                  changeContext({ ...context, parameters });
                }}
              >
                <option value="">
                  {p.required ? "Choose a value" : "Not supplied"}
                </option>
                {p.allowed_values.map((value) => (
                  <option key={value}>{value}</option>
                ))}
              </select>
              <small>{p.kind.replaceAll("_", " ")}</small>
            </label>
          ))}
        </fieldset>
        <button
          className="primary preview-button"
          disabled={!validContext || Boolean(busy) || !online}
          onClick={() => void preview()}
        >
          {busy === "preview" ? (
            <LoaderCircle className="spin" size={15} />
          ) : (
            <Play size={15} />
          )}
          Preview plan
        </button>
        <div className="context-tip">
          <ShieldCheck size={18} />
          <p>
            Preview checks your selection and pins the commit.
            <strong>It doesn’t start a pipeline.</strong>
          </p>
        </div>
        <div className="context-bottom">
          <CircleHelp size={15} />
          <span>
            Run and test outcomes are tracked separately. Missing evidence stays
            visible.
          </span>
        </div>
      </aside>
    </div>
  );
}
