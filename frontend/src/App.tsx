import { useCallback, useEffect, useRef, useState } from "react";
import { createSession, fetchLiveKit, fetchMetrics, fetchSession, fetchSetup, sendAction, sendTurn, setCall, uploadPhotos, type Metrics, type SetupItem } from "./api";
import { CallPanel } from "./components/CallPanel";
import { ChatPanel } from "./components/ChatPanel";
import { RecordPanel } from "./components/RecordPanel";
import type { SessionState } from "./types";
import { connectLiveKit, type LiveCall } from "./voice";

const STORAGE_KEY = "alder.sessionId";

function liveUrl(sessionId: string): string {
  // The Vite proxy accepts this socket and then leaves it hanging. Talk to the API directly while developing.
  if (location.port === "5173") return `ws://127.0.0.1:8000/api/sessions/${sessionId}/ws`;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}//${location.host}/api/sessions/${sessionId}/ws`;
}

export function App() {
  const clientId = useRef(crypto.randomUUID());
  const liveRef = useRef<LiveCall | null>(null);
  const [session, setSession] = useState<SessionState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [callStatus, setCallStatus] = useState("idle");
  const [interim, setInterim] = useState("");
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const [missingSetup, setMissingSetup] = useState<SetupItem[]>([]);

  const take = useCallback((next: SessionState) => {
    setSession((current) => {
      if (!current || next.turns.length >= current.turns.length || next.revision !== current.revision) return next;
      return current;
    });
  }, []);

  useEffect(() => {
    fetchSetup()
      .then((body) => setMissingSetup([...body.requiredFromYou, ...body.included].filter((item) => !item.set)))
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    const saved = localStorage.getItem(STORAGE_KEY);
    const ready = saved
      ? fetchSession(saved).catch(() => createSession())
      : createSession();
    ready
      .then((next) => {
        localStorage.setItem(STORAGE_KEY, next.sessionId);
        setSession(next);
      })
      .catch((reason: Error) => setError(reason.message));
  }, []);

  useEffect(() => {
    if (!session?.sessionId) return;
    let closed = false;
    let socket: WebSocket | null = null;
    const connect = () => {
      socket = new WebSocket(liveUrl(session.sessionId));
      socket.onmessage = (event) => {
        const payload = JSON.parse(event.data) as { type?: string; state?: SessionState };
        if (!payload.state) return;
        take(payload.state);
        setInterim("");
      };
      socket.onclose = () => {
        if (!closed) window.setTimeout(connect, 1000);
      };
    };
    connect();
    return () => {
      closed = true;
      socket?.close();
    };
  }, [session?.sessionId, take]);

  useEffect(() => {
    if (!session?.callActive) return;
    const timer = window.setInterval(() => {
      fetchSession(session.sessionId).then(take).catch(() => undefined);
    }, 1200);
    return () => window.clearInterval(timer);
  }, [session?.callActive, session?.sessionId, take]);

  useEffect(() => {
    if (!session?.latestRecord) return;
    fetchMetrics().then(setMetrics).catch(() => undefined);
  }, [session?.latestRecord?.revision]);

  async function run(work: () => Promise<SessionState>) {
    setBusy(true);
    setError(null);
    try {
      const next = await work();
      take(next);
      return next;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Something went wrong.");
      return null;
    } finally {
      setBusy(false);
    }
  }

  async function startCall() {
    if (!session) return;
    setCallStatus("connecting");
    const next = await run(() => setCall(session.sessionId, true, clientId.current));
    if (!next) {
      setCallStatus("idle");
      return;
    }
    try {
      const creds = await fetchLiveKit(session.sessionId);
      liveRef.current = await connectLiveKit(creds.url, creds.token, {
        onStatus: setCallStatus,
        onInterim: setInterim,
      });
    } catch (reason) {
      setCallStatus("idle");
      setError(
        reason instanceof Error
          ? `${reason.message} You can still type on the call.`
          : "The call could not connect. You can still type on the call.",
      );
    }
  }

  function endCall() {
    liveRef.current?.stop();
    liveRef.current = null;
    setInterim("");
    setCallStatus("idle");
    if (!session) return;
    void run(() => setCall(session.sessionId, false, clientId.current));
  }

  async function newClaim() {
    liveRef.current?.stop();
    liveRef.current = null;
    setInterim("");
    setCallStatus("idle");
    const next = await run(() => createSession());
    if (next) localStorage.setItem(STORAGE_KEY, next.sessionId);
  }

  if (!session) {
    return (
      <main className="app">
        <p>{error || "Opening the claim desk…"}</p>
      </main>
    );
  }

  return (
    <main className="app">
      <header className="top">
        <div className="brand">
          <h1>Alder</h1>
          <p>Motor claim desk. The call and the chat share one claim.</p>
        </div>
        <div className="session-tools">
          <span className={`pill ${session.outcome || ""}`}>{outcomeLabel(session.outcome, session.revision)}</span>
          <span className="session-id">{session.sessionId}</span>
          <button className="ghost" type="button" onClick={() => void newClaim()}>
            New claim
          </button>
        </div>
      </header>
      {error && <div className="banner">{error}</div>}
      {missingSetup.length > 0 && (
        <div className="banner">
          {missingSetup.map((item) => (
            <p key={item.name}>
              <strong>{item.name}</strong> is not set. {item.why}
            </p>
          ))}
        </div>
      )}
      <div className="desk">
        <CallPanel
          turns={session.turns}
          active={session.callActive}
          status={callStatus}
          interim={interim}
          busy={busy}
          onStart={() => void startCall()}
          onEnd={endCall}
          onInterrupt={() => liveRef.current?.interrupt()}
          onSend={(message) => void run(() => sendTurn(session.sessionId, message, "call", clientId.current))}
        />
        <ChatPanel
          turns={session.turns}
          input={session.input}
          busy={busy}
          onSend={(message) => void run(() => sendTurn(session.sessionId, message, "chat", clientId.current))}
          onSkip={() => void run(() => sendTurn(session.sessionId, "", "chat", clientId.current, "contactPreference"))}
          onUpload={(files, message) => void run(() => uploadPhotos(session.sessionId, files, message, clientId.current))}
          onAction={(action) => void run(() => sendAction(session.sessionId, action, "chat", clientId.current))}
        />
      </div>
      <RecordPanel session={session} metrics={metrics} />
    </main>
  );
}

function outcomeLabel(outcome: string | null, revision: number): string {
  const name =
    outcome === "completed"
      ? "Filed"
      : outcome === "partial"
        ? "Partial"
        : outcome === "optedOut"
          ? "Opted out"
          : outcome === "needsReview"
            ? "Needs review"
            : "Collecting";
  return revision ? `${name} · rev ${revision}` : name;
}
