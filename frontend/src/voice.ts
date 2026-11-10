import { Room, RoomEvent, Track, type RemoteTrack } from "livekit-client";

type Status = "idle" | "connecting" | "listening" | "speaking" | "blocked";

export type LiveCall = {
  interrupt: () => void;
  stop: () => void;
};

export async function connectLiveKit(
  url: string,
  token: string,
  handlers: {
    onStatus: (status: Status) => void;
    onInterim: (text: string) => void;
  },
): Promise<LiveCall> {
  const room = new Room();
  const attached: HTMLMediaElement[] = [];

  room.on(RoomEvent.TrackSubscribed, (track: RemoteTrack) => {
    if (track.kind !== Track.Kind.Audio) return;
    const element = track.attach();
    element.autoplay = true;
    document.body.appendChild(element);
    attached.push(element);
  });
  room.on(RoomEvent.TrackUnsubscribed, (track: RemoteTrack) => {
    for (const element of track.detach()) element.remove();
  });
  room.on(RoomEvent.DataReceived, (payload: Uint8Array) => {
    try {
      const message = JSON.parse(new TextDecoder().decode(payload)) as { type?: string; text?: string; status?: Status };
      if (message.type === "status" && message.status) handlers.onStatus(message.status);
      if (message.type === "interim") handlers.onInterim(message.text || "");
    } catch {
      /* Ignore a data packet that is not ours. */
    }
  });
  room.on(RoomEvent.Disconnected, () => handlers.onStatus("idle"));

  handlers.onStatus("connecting");
  await room.connect(url, token);
  try {
    await room.localParticipant.setMicrophoneEnabled(true);
    handlers.onStatus("listening");
  } catch {
    handlers.onStatus("blocked");
  }

  return {
    interrupt() {
      const payload = new TextEncoder().encode(JSON.stringify({ type: "interrupt" }));
      void room.localParticipant.publishData(payload, { reliable: true });
      handlers.onStatus("listening");
    },
    stop() {
      for (const element of attached) element.remove();
      void room.disconnect();
      handlers.onStatus("idle");
    },
  };
}
