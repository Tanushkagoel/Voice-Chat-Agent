import type { SessionState } from "./types";

async function parse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = body.detail || detail;
    } catch {
      /* The body was not JSON. */
    }
    throw new Error(typeof detail === "string" ? detail : "The request failed.");
  }
  return response.json() as Promise<T>;
}

export function createSession(): Promise<SessionState> {
  return fetch("/api/sessions", { method: "POST" }).then((response) => parse<SessionState>(response));
}

export function fetchSession(sessionId: string): Promise<SessionState> {
  return fetch(`/api/sessions/${sessionId}`).then((response) => parse<SessionState>(response));
}

export function sendTurn(sessionId: string, message: string, channel: "call" | "chat", clientId: string, skipField?: string) {
  return fetch(`/api/sessions/${sessionId}/turns`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, channel, clientId, skipField: skipField ?? null }),
  }).then((response) => parse<SessionState>(response));
}

export function sendAction(sessionId: string, action: "submit" | "finish_later" | "opt_out", channel: "call" | "chat", clientId: string) {
  return fetch(`/api/sessions/${sessionId}/actions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, channel, clientId }),
  }).then((response) => parse<SessionState>(response));
}

export function setCall(sessionId: string, active: boolean, clientId: string) {
  return fetch(`/api/sessions/${sessionId}/call`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ active, clientId }),
  }).then((response) => parse<SessionState>(response));
}

export function uploadPhotos(sessionId: string, files: File[], message: string, clientId: string) {
  const body = new FormData();
  for (const file of files) body.append("files", file);
  body.append("message", message);
  body.append("channel", "chat");
  body.append("clientId", clientId);
  return fetch(`/api/sessions/${sessionId}/uploads`, { method: "POST", body }).then((response) => parse<SessionState>(response));
}

export type Metrics = {
  assistantTurns: number;
  p50ServerLatencyMs: number | null;
  p95ServerLatencyMs: number | null;
  estimatedUsd: number;
  completedRecords: number;
  estimatedUsdPerCompletedRecord: number | null;
  promptTokens: number;
  completionTokens: number;
};

export function fetchLiveKit(sessionId: string): Promise<{ url: string; token: string; room: string }> {
  return fetch(`/api/sessions/${sessionId}/livekit`, { method: "POST" }).then((response) =>
    parse<{ url: string; token: string; room: string }>(response),
  );
}

export type SetupItem = { name: string; set: boolean; why: string };

export function fetchSetup(): Promise<{ requiredFromYou: SetupItem[]; included: SetupItem[] }> {
  return fetch("/api/setup").then((response) => parse<{ requiredFromYou: SetupItem[]; included: SetupItem[] }>(response));
}

export function fetchMetrics(): Promise<Metrics> {
  return fetch("/api/metrics").then((response) => parse<Metrics>(response));
}
