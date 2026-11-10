export type FieldState = {
  value: unknown;
  source: string | null;
  status: string;
  message?: string | null;
};

export type Turn = {
  id: number;
  channel: "call" | "chat" | "both";
  role: "user" | "assistant";
  content: string;
  createdAt: string | null;
};

export type Choice = { value: string; label: string };

export type InputSpec = {
  type: "text" | "choice" | "file";
  field?: string | null;
  placeholder?: string;
  choices?: Choice[];
  skippable?: boolean;
  accept?: string;
  multiple?: boolean;
};

export type ClaimRecord = {
  sessionId: string;
  revision: number;
  outcome: string;
  fields: Record<string, { value: unknown; source: string | null; status: string }>;
  conflicts: { field: string; values: { value: unknown; source: string | null }[] }[];
  issues: { field: string; status: string; message: string }[];
  deliveryStatus?: string;
};

export type SessionState = {
  sessionId: string;
  callActive: boolean;
  outcome: string | null;
  revision: number;
  pendingAction: string | null;
  fields: Record<string, FieldState>;
  missing: string[];
  conflicts: { field: string; options: { value: unknown; source: string; label: string }[] }[];
  input: InputSpec;
  turns: Turn[];
  records: ClaimRecord[];
  latestRecord: ClaimRecord | null;
  reply?: string;
  voiceNotice?: string | null;
  speak?: string;
  latencyMs?: number;
};

export const FIELD_LABELS: Record<string, string> = {
  incidentDescription: "What happened",
  incidentDate: "Date",
  incidentLocation: "Location",
  anyoneInjured: "Anyone injured",
  injuryDetails: "Injury details",
  policyNumber: "Policy number",
  vehicleRegistration: "Registration",
  contactEmail: "Email",
  damagePhotos: "Damage photos",
  contactPreference: "Follow-up",
};
