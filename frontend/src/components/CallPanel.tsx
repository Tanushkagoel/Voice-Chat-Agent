import { useEffect, useRef } from "react";
import type { Turn } from "../types";

export function CallPanel(props: {
  turns: Turn[];
  active: boolean;
  status: string;
  interim: string;
  busy: boolean;
  onStart: () => void;
  onEnd: () => void;
  onInterrupt: () => void;
  onSend: (message: string) => void;
}) {
  const endRef = useRef<HTMLDivElement>(null);
  const turns = props.turns.filter((turn) => turn.channel === "call" || turn.channel === "both");
  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" });
  }, [turns.length, props.interim]);

  return (
    <section className="panel call" aria-label="Voice call">
      <header>
        <h2>Call</h2>
        <span>{labelFor(props.status, props.active)}</span>
      </header>
      <div className="transcript" aria-live="polite">
        {turns.map((turn) => (
          <article key={turn.id} className={`bubble ${turn.role}`}>
            <span className="who">{turn.role === "assistant" ? "Alder" : "You"}</span>
            <p>{turn.content}</p>
          </article>
        ))}
        {props.interim && (
          <article className="bubble user interim">
            <span className="who">You</span>
            <p>{props.interim}</p>
          </article>
        )}
        <div ref={endRef} />
      </div>
      <form
        className="composer"
        onSubmit={(event) => {
          event.preventDefault();
          const form = event.currentTarget;
          const input = form.elements.namedItem("utterance") as HTMLInputElement;
          const message = input.value.trim();
          if (!message) return;
          props.onSend(message);
          input.value = "";
        }}
      >
        <div className="row">
          {!props.active ? (
            <button className="primary" type="button" onClick={props.onStart} disabled={props.busy}>
              Start call
            </button>
          ) : (
            <>
              <button className="primary live" type="button" onClick={props.onEnd} disabled={props.busy}>
                End call
              </button>
              <button className="ghost" type="button" onClick={props.onInterrupt} disabled={props.status !== "speaking"}>
                Interrupt
              </button>
            </>
          )}
        </div>
        <div className="row">
          <input name="utterance" type="text" placeholder="Or type on the call if the mic is blocked" aria-label="Call message" />
          <button className="ghost" type="submit" disabled={props.busy}>
            Send
          </button>
        </div>
      </form>
    </section>
  );
}

function labelFor(status: string, active: boolean): string {
  if (!active) return "Not connected";
  if (status === "connecting") return "Connecting";
  if (status === "speaking") return "Alder is speaking";
  if (status === "listening") return "Listening";
  if (status === "blocked" || status === "idle") return "Mic unavailable — type instead";
  return "Connected";
}
