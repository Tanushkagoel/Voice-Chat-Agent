import { useEffect, useRef, useState } from "react";
import type { InputSpec, Turn } from "../types";

export function ChatPanel(props: {
  turns: Turn[];
  input: InputSpec;
  busy: boolean;
  onSend: (message: string) => void;
  onSkip: () => void;
  onUpload: (files: File[], message: string) => void;
  onAction: (action: "submit" | "finish_later" | "opt_out") => void;
}) {
  const endRef = useRef<HTMLDivElement>(null);
  const [files, setFiles] = useState<File[]>([]);
  const turns = props.turns.filter((turn) => turn.channel === "chat" || turn.channel === "both");
  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" });
  }, [turns.length]);

  return (
    <section className="panel chat" aria-label="Chat">
      <header>
        <h2>Chat</h2>
        <span>Same claim as the call</span>
      </header>
      <div className="transcript">
        {turns.map((turn) => (
          <article key={turn.id} className={`bubble ${turn.role}`}>
            <span className="who">{turn.role === "assistant" ? "Alder" : "You"}</span>
            <p>{turn.content}</p>
          </article>
        ))}
        <div ref={endRef} />
      </div>
      <form
        className="composer"
        onSubmit={(event) => {
          event.preventDefault();
          const form = event.currentTarget;
          const input = form.elements.namedItem("message") as HTMLInputElement;
          const message = input.value.trim();
          if (props.input.type === "file" && files.length) {
            props.onUpload(files, message);
            setFiles([]);
            input.value = "";
            return;
          }
          if (!message) return;
          props.onSend(message);
          input.value = "";
        }}
      >
        {props.input.type === "choice" && props.input.choices && (
          <div className="choices">
            {props.input.choices.map((choice) => (
              <button
                key={choice.value}
                className="choice"
                type="button"
                disabled={props.busy}
                onClick={() => props.onSend(choice.label)}
              >
                {choice.label}
              </button>
            ))}
            {props.input.skippable && (
              <button className="ghost" type="button" disabled={props.busy} onClick={props.onSkip}>
                Skip
              </button>
            )}
          </div>
        )}
        {props.input.type === "file" && (
          <label className="drop">
            <span>{files.length ? files.map((file) => file.name).join(", ") : "Add JPEG, PNG, or WebP damage photos"}</span>
            <input
              type="file"
              accept={props.input.accept || "image/jpeg,image/png,image/webp"}
              multiple={props.input.multiple !== false}
              onChange={(event) => setFiles(Array.from(event.target.files ?? []))}
            />
          </label>
        )}
        <div className="row">
          <input
            name="message"
            type="text"
            placeholder={props.input.placeholder || "Message Alder"}
            aria-label="Chat message"
          />
          <button className="primary" type="submit" disabled={props.busy}>
            {props.input.type === "file" ? "Upload" : "Send"}
          </button>
        </div>
        <div className="actions">
          <button className="ghost" type="button" disabled={props.busy} onClick={() => props.onAction("submit")}>
            Submit claim
          </button>
          <button className="ghost" type="button" disabled={props.busy} onClick={() => props.onAction("finish_later")}>
            Finish later
          </button>
          <button className="danger" type="button" disabled={props.busy} onClick={() => props.onAction("opt_out")}>
            Opt out
          </button>
        </div>
      </form>
    </section>
  );
}
