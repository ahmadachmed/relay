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

type Context = {
  project: string;
  pipeline: string;
  branch: string;
  parameters: Record<string, string>;
};
type AiReply = {
  run_id: string | null;
  answer: {
    action: "clarify" | "propose" | "analyze" | "cancel";
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
  } | null;
  limitations: string[];
};
type Message = { role: "user" | "relay"; text: string };
const initialMessage: Message = {
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
  const [aiEnabled, setAiEnabled] = useState(false);
  const [shareAi, setShareAi] = useState(false);
  const [shareFailures, setShareFailures] = useState(false);
  const [aiReply, setAiReply] = useState<AiReply | null>(null);
  const aiLock = useRef(false);
  useEffect(() => {
    void api<{ enabled: boolean; configured: boolean }>("/api/assistant")
      .then((s) => setAiEnabled(s.enabled && s.configured))
      .catch(() => setAiEnabled(false));
  }, []);
  const [registry, setRegistry] = useState<Registry | null>(null);
  const [context, setContext] = useState<Context>({
    project: "",
    pipeline: "",
    branch: "",
    parameters: {},
  });
  const [runs, setRuns] = useState<Run[]>([]);
  const [selected, setSelected] = useState("");
  const [run, setRun] = useState<Run | null>(null);
  const [evidence, setEvidence] = useState<Evidence | null>(null);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [recoverId, setRecoverId] = useState("");
  const [messages, setMessages] = useState<Message[]>([initialMessage]);
  const [command, setCommand] = useState("");
  const [error, setError] = useState("");
  const [historyError, setHistoryError] = useState("");
  const [runError, setRunError] = useState("");
  const [evidenceError, setEvidenceError] = useState("");
  const [busy, setBusy] = useState("");
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
    (message: Message) => setMessages((old) => [...old.slice(-39), message]),
    [],
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
          const saved = JSON.parse(storage.get("messages") || "null");
          if (Array.isArray(saved))
            setMessages(
              saved
                .filter(
                  (m: Message) =>
                    ["user", "relay"].includes(m.role) &&
                    typeof m.text === "string",
                )
                .slice(-40),
            );
        } catch {
          /* Ignore invalid session history. */
        }
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
    if (registry) storage.set("messages", JSON.stringify(messages));
  }, [messages, registry]);
  useEffect(() => {
    const timer = setInterval(() => setClock(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  useEffect(() => {
    const timer = setInterval(() => void loadHistory(), 15000);
    return () => clearInterval(timer);
  }, [loadHistory]);
  useEffect(() => {
    activeRun.current = selected;
    setAiReply(null);
    setRun(null);
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
      const [value, savedPlan] = await Promise.all([
        api<Evidence>(`/api/remote-runs/${encodeURIComponent(id)}/evidence`),
        api<Plan>(`/api/plans/${encodeURIComponent(id)}`),
      ]);
      if (activeRun.current === id) {
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
    revision.current++;
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
    storage.set("attemptedPlan", value.id);
    setRecoverId(value.id);
    try {
      const result = await post<Plan>(`/api/plans/${value.id}/execute`, {
        confirmed: true,
      });
      setPlan(result);
      if (result.status === "submitted") {
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
  async function askAi(text: string) {
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
    setBusy("ai");
    setError("");
    setAiReply(null);
    // A follow-up may change the intended target; never leave an old plan runnable.
    setPlan(null);
    const version = revision.current;
    try {
      const value = await post<AiReply>("/api/assistant/chat", {
        message: text,
        share_context: true,
        include_failure_details: shareFailures,
        context: validContext
          ? {
              project_id: context.project,
              pipeline_id: context.pipeline,
              branch: context.branch,
              parameters: context.parameters,
            }
          : null,
        run_id: selected || null,
        history: messages
          .slice(-8)
          .map((m) => ({ role: m.role, text: m.text.slice(0, 4000) })),
      });
      if (version === revision.current) {
        setAiReply(value);
        addMessage({
          role: "relay",
          text: `AI suggestion: ${value.answer.message}`,
        });
      }
    } catch (e) {
      if (version === revision.current) setError(errorText(e));
    } finally {
      aiLock.current = false;
      actionLock.current = false;
      setBusy("");
    }
  }
  function sendCommand(event: React.FormEvent) {
    event.preventDefault();
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
            setSelected("");
            storage.set("selected", "");
            setRun(null);
            setEvidence(null);
            setPlan(null);
            setMessages([initialMessage]);
            setAiReply(null);
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
                disabled={Boolean(busy)}
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
                  {r.id.slice(0, 10)} · {r.pipeline_status}
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
          </div>
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
                  <div>
                    <h3>Observed report summary</h3>
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
                  </div>
                )}
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
                    <pre>{aiReply.answer.bug_report_draft}</pre>
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
                    {plan.plan.project_id} / {plan.plan.pipeline_id}
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
            <section className="run-card" aria-label="Selected run">
              <div className="card-heading">
                <span className="icon-box">
                  <History size={16} />
                </span>
                <div>
                  <h2>Run progress</h2>
                  <p className="mono">{selected}</p>
                </div>
                {run && <Badge value={run.pipeline_status} />}
              </div>
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
            </section>
          )}

          {selected && (
            <section
              className="evidence-card"
              aria-label="Test evidence"
              id="test-evidence"
            >
              <div className="card-heading">
                <span className="icon-box">
                  <FileText size={16} />
                </span>
                <div>
                  <h2>Test evidence</h2>
                  <p>Actual report results, separate from pipeline status.</p>
                </div>
                {evidence && <Badge value={evidence.test_status} />}
              </div>
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
            </section>
          )}
        </div>
        <div className="composer-wrap">
          {aiEnabled && (
            <div className="ai-sharing">
              <label>
                <input
                  type="checkbox"
                  checked={shareAi}
                  disabled={Boolean(busy)}
                  onChange={(e) => {
                    setShareAi(e.target.checked);
                    if (!e.target.checked) setShareFailures(false);
                  }}
                />
                Send chat, recent conversation, configured context and selected
                run summary to AI
              </label>
              <label>
                <input
                  type="checkbox"
                  checked={shareFailures}
                  disabled={!shareAi || Boolean(busy)}
                  onChange={(e) => setShareFailures(e.target.checked)}
                />
                Include failure names/messages (best-effort redaction; verify
                sensitive content first)
              </label>
            </div>
          )}

          <form className="composer" onSubmit={sendCommand}>
            <label className="sr-only" htmlFor="command">
              Structured command
            </label>
            <input
              id="command"
              value={command}
              onChange={(e) => setCommand(e.target.value)}
              maxLength={500}
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
        <p className="context-description">
          A clear target makes a useful check.
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
                    parameters: defaults(project, key),
                  });
              }}
            >
              {Object.keys(project?.pipelines || {}).map((key) => (
                <option key={key}>{key}</option>
              ))}
            </select>
          </label>
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
