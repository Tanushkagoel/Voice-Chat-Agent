"""One LiveKit room per claim. Gemini transcribes and speaks. LangGraph decides the words."""

from __future__ import annotations

import array
import asyncio
import audioop
import json
import logging

import httpx
import redis.asyncio as redis
from livekit import rtc

from app.config import Settings, get_settings
from app.voice.gemini_audio import resample, synthesize, transcribe_wav
from app.voice.tokens import agent_token

log = logging.getLogger(__name__)

SAMPLE_RATE = 24000
SPEECH_RMS = 450
BARGE_RMS = 1600
SILENCE_FRAMES = 18
MIN_SPEECH_FRAMES = 8


class Playback:
    def __init__(self, source: rtc.AudioSource, notify):
        self.source = source
        self.notify = notify
        self.generation = 0
        self.playing = False
        self._lock = asyncio.Lock()

    def cancel(self) -> None:
        self.generation += 1
        self.playing = False
        asyncio.create_task(self._clear())

    async def _clear(self) -> None:
        try:
            await self.source.clear_queue()
        except Exception:
            log.debug("Could not clear the playback queue", exc_info=True)

    async def speak(self, settings: Settings, text: str) -> None:
        spoken = " ".join(text.split())
        if not spoken:
            return
        self.generation += 1
        token = self.generation
        await self._clear()
        audio = await synthesize(settings, spoken)
        if audio is None or token != self.generation:
            return
        pcm, rate = audio
        pcm = resample(pcm, rate, SAMPLE_RATE)
        async with self._lock:
            if token != self.generation:
                return
            self.playing = True
            await self.notify("speaking")
            try:
                await self._play(pcm, token)
            finally:
                if token == self.generation:
                    self.playing = False
                    await self.notify("listening")

    async def _play(self, pcm: bytes, token: int) -> None:
        samples_per_frame = SAMPLE_RATE // 50
        step = samples_per_frame * 2
        for offset in range(0, len(pcm) - step + 1, step):
            if token != self.generation:
                await self._clear()
                return
            chunk = pcm[offset : offset + step]
            samples = array.array("h")
            samples.frombytes(chunk)
            frame = rtc.AudioFrame.create(SAMPLE_RATE, 1, samples_per_frame)
            frame.data[:] = samples
            await self.source.capture_frame(frame)


async def run_room(session_id: str, opening: str | None, stop: asyncio.Event) -> None:
    settings = get_settings()
    if not settings.livekit_url or not settings.livekit_api_key:
        log.error("LiveKit is not configured")
        return
    room = rtc.Room()
    try:
        await room.connect(settings.livekit_url, agent_token(settings, session_id))
        source = rtc.AudioSource(SAMPLE_RATE, 1)
        track = rtc.LocalAudioTrack.create_audio_track("alder-voice", source)
        options = rtc.TrackPublishOptions()
        options.source = rtc.TrackSource.SOURCE_MICROPHONE
        await room.local_participant.publish_track(track, options)

        async def notify(status: str) -> None:
            try:
                await room.local_participant.publish_data(
                    json.dumps({"type": "status", "status": status}).encode(),
                    reliable=True,
                )
            except Exception:
                log.debug("Could not publish call status", exc_info=True)

        playback = Playback(source, notify)

        @room.on("data_received")
        def on_data(packet: rtc.DataPacket) -> None:
            try:
                body = json.loads(packet.data.decode())
            except (UnicodeDecodeError, json.JSONDecodeError):
                return
            if body.get("type") == "interrupt":
                playback.cancel()

        notices = asyncio.create_task(_speak_remote_notices(settings, session_id, playback, stop))
        try:
            await _wait_for_caller(room, stop)
            if opening and not stop.is_set():
                await playback.speak(settings, opening)
            await _listen(room, settings, session_id, playback, stop)
        finally:
            notices.cancel()
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Voice room failed for %s", session_id)
    finally:
        await room.disconnect()


async def _wait_for_caller(room: rtc.Room, stop: asyncio.Event) -> None:
    if room.remote_participants:
        return
    joined = asyncio.Event()

    @room.on("participant_connected")
    def _on_join(_participant: rtc.RemoteParticipant) -> None:
        joined.set()

    while not stop.is_set() and not joined.is_set():
        try:
            await asyncio.wait_for(joined.wait(), timeout=0.5)
        except TimeoutError:
            if room.remote_participants:
                return


async def _listen(
    room: rtc.Room,
    settings: Settings,
    session_id: str,
    playback: Playback,
    stop: asyncio.Event,
) -> None:
    track = await _wait_for_microphone(room, stop)
    if track is None:
        return
    stream = rtc.AudioStream(track, sample_rate=16000, num_channels=1, frame_size_ms=20)
    buffer = bytearray()
    speech_frames = 0
    silence_frames = 0
    in_speech = False
    barge_frames = 0
    async for event in stream:
        if stop.is_set():
            break
        frame = event.frame
        energy = audioop.rms(frame.data.tobytes(), 2) if frame.data.nbytes else 0
        if playback.playing and energy >= BARGE_RMS:
            barge_frames += 1
            if barge_frames >= 4:
                playback.cancel()
                in_speech = True
                buffer.clear()
                speech_frames = 0
                silence_frames = 0
                barge_frames = 0
            continue
        barge_frames = 0
        if playback.playing:
            continue
        if energy >= SPEECH_RMS:
            in_speech = True
            silence_frames = 0
            speech_frames += 1
            buffer.extend(frame.data.tobytes())
            continue
        if not in_speech:
            continue
        silence_frames += 1
        buffer.extend(frame.data.tobytes())
        if silence_frames < SILENCE_FRAMES:
            continue
        captured = bytes(buffer)
        buffer.clear()
        in_speech = False
        heard = speech_frames
        speech_frames = 0
        silence_frames = 0
        if heard < MIN_SPEECH_FRAMES:
            continue
        await _handle_utterance(room, settings, session_id, playback, captured)

    await stream.aclose()


async def _wait_for_microphone(room: rtc.Room, stop: asyncio.Event) -> rtc.Track | None:
    found: asyncio.Queue[rtc.Track] = asyncio.Queue()

    @room.on("track_subscribed")
    def on_track(track: rtc.Track, _publication: rtc.RemoteTrackPublication, _participant: rtc.RemoteParticipant) -> None:
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            found.put_nowait(track)

    for participant in room.remote_participants.values():
        for publication in participant.track_publications.values():
            if publication.track and publication.track.kind == rtc.TrackKind.KIND_AUDIO:
                return publication.track
    while not stop.is_set():
        try:
            return await asyncio.wait_for(found.get(), timeout=0.5)
        except TimeoutError:
            continue
    return None


async def _handle_utterance(
    room: rtc.Room,
    settings: Settings,
    session_id: str,
    playback: Playback,
    pcm: bytes,
) -> None:
    wav = _wav(pcm, 16000)
    transcript, _prompt, _completion = await transcribe_wav(settings, wav)
    if not transcript:
        return
    await _publish(room, {"type": "interim", "text": transcript})
    reply = await _post_turn(settings, session_id, transcript)
    await _publish(room, {"type": "interim", "text": ""})
    if reply:
        await playback.speak(settings, reply)


async def _publish(room: rtc.Room, payload: dict) -> None:
    try:
        await room.local_participant.publish_data(json.dumps(payload).encode(), reliable=True)
    except Exception:
        log.debug("Could not publish the live transcript", exc_info=True)


async def _post_turn(settings: Settings, session_id: str, message: str) -> str:
    url = f"{settings.api_base_url.rstrip('/')}/api/sessions/{session_id}/turns"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                url,
                json={"message": message, "channel": "call", "clientId": "livekit-agent"},
            )
            response.raise_for_status()
            return str(response.json().get("reply") or "")
    except httpx.HTTPError:
        log.exception("Could not post the call transcript")
        return ""


async def _speak_remote_notices(settings: Settings, session_id: str, playback: Playback, stop: asyncio.Event) -> None:
    if not settings.redis_url:
        return
    client = redis.from_url(settings.redis_url)
    pubsub = client.pubsub()
    await pubsub.subscribe(f"session:{session_id}")
    try:
        while not stop.is_set():
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.5)
            if not message or message.get("type") != "message":
                await asyncio.sleep(0.05)
                continue
            raw = message["data"]
            if isinstance(raw, bytes):
                raw = raw.decode()
            data = json.loads(raw)
            if data.get("origin") == "livekit-agent":
                continue
            text = data.get("speak")
            if text:
                await playback.speak(settings, str(text))
    finally:
        await pubsub.aclose()
        await client.aclose()


def _wav(pcm: bytes, sample_rate: int) -> bytes:
    samples = len(pcm) // 2
    if samples <= 0:
        return b""
    frame = rtc.AudioFrame.create(sample_rate, 1, samples)
    values = array.array("h")
    values.frombytes(pcm[: samples * 2])
    frame.data[:] = values
    return frame.to_wav_bytes()
