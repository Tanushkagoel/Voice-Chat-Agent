import type { Metrics } from "../api";
import { FIELD_LABELS, type SessionState } from "../types";

export function RecordPanel(props: { session: SessionState; metrics: Metrics | null }) {
  const record = props.session.latestRecord;
  const fields = props.session.fields;
  const keys = Object.keys(fields);
  return (
    <section className="record" aria-label="Claim record">
      <h2>Claim record</h2>
      <p className="lede">
        {record
          ? `Revision ${record.revision} is ${record.outcome}. Delivery is ${record.deliveryStatus || "pending"}.`
          : "Nothing is filed yet. Ending the call keeps the claim open."}
      </p>
      {keys.length > 0 && (
        <div className="field-grid">
          {keys.map((key) => {
            const field = fields[key];
            return (
              <article key={key} className="field">
                <b>{FIELD_LABELS[key] || key}</b>
                <div>{shown(field.value)}</div>
                <em>
                  <span className={`status ${field.status}`}>{field.status}</span>
                  {field.source ? ` · ${field.source}` : ""}
                </em>
              </article>
            );
          })}
        </div>
      )}
      {props.session.missing.length > 0 && (
        <p className="lede">Still needed: {props.session.missing.map((key) => FIELD_LABELS[key] || key).join(", ")}.</p>
      )}
      {record && (
        <details>
          <summary>Structured JSON</summary>
          <pre>{JSON.stringify(record, null, 2)}</pre>
        </details>
      )}
      {props.metrics && (
        <p className="metrics">
          Server turn latency p50 {props.metrics.p50ServerLatencyMs ?? "—"} ms, p95 {props.metrics.p95ServerLatencyMs ?? "—"} ms.
          {props.metrics.completedRecords
            ? ` Estimated model cost per completed record: ${
                props.metrics.estimatedUsdPerCompletedRecord === null
                  ? "set GEMINI_INPUT_USD_PER_MILLION to price the token counts"
                  : `$${props.metrics.estimatedUsdPerCompletedRecord.toFixed(4)}`
              }.`
            : " No completed record yet."}
        </p>
      )}
    </section>
  );
}

function shown(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (Array.isArray(value)) {
    return value
      .map((item) => (item && typeof item === "object" && "name" in item ? String(item.name) : String(item)))
      .join(", ");
  }
  return String(value);
}
