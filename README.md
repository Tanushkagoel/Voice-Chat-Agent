# Alder

A motor-claim desk with one session on two channels. The caller can talk, and the same claim accepts a policy number, a registration, an email, and damage photos in chat. Both sides read and write the same fields. The business result is one sanitized JSON record per revision.

## Credentials

The only secret you create is a Gemini key. Everything else in the local stack starts from Docker with no accounts.

| Piece | What you set | Account |
| --- | --- | --- |
| Gemini | `GEMINI_API_KEY` in `backend/.env`, or in the shell before `docker compose up` | [Google AI Studio](https://aistudio.google.com/apikey) |
| PostgreSQL | Nothing. Compose creates user `alder`, database `alder` | None |
| Redis | Nothing. Compose runs it for events and the session lock | None |
| LiveKit on this machine | Nothing. `deploy/livekit.yaml` uses key `devkey` and secret `secret` | None |
| LiveKit on the public internet | `LIVEKIT_URL`, `LIVEKIT_PUBLIC_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | [LiveKit Cloud](https://cloud.livekit.io), or a host that can open UDP 7882 |

Chat, photos, and the typed call line work with the Gemini key unset. A spoken call does not: the token endpoint returns 503 and the page says why. This repository is not deployed to a public HTTPS host. Compose is the deployment shape; a public URL still needs a host you control, and spoken audio on that host needs the LiveKit Cloud values or open UDP.

## Run it

Python 3.11 and Node 20 or newer.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt -r backend/requirements-dev.txt
cd backend
uvicorn app.main:app --reload --port 8000
```

In another terminal:

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:5173. Start the call and speak, or type in the call panel if the microphone is blocked. The chat panel is the same claim. The page lists any missing credential.

Copy `backend/.env.example` to `backend/.env` and set `GEMINI_API_KEY`. Validators, not the model, accept policy numbers, registrations, emails, and dates.

Postgres, Redis, LiveKit, the API, and the voice worker:

```bash
export GEMINI_API_KEY=your-key
docker compose up --build
```

Open http://localhost:8000. Compose points the API at Postgres. Without Compose, the API uses a SQLite file at `backend/data/alder.db`.

`deploy/livekit.yaml` advertises `127.0.0.1`, which is the right media address for a browser on this machine. If a call joins and you hear nothing while the worker is inside Docker, run the worker on the host instead so it shares that address:

```bash
cd backend
REDIS_URL=redis://127.0.0.1:6379/0 \
LIVEKIT_URL=ws://127.0.0.1:7880 \
LIVEKIT_API_KEY=devkey \
LIVEKIT_API_SECRET=secret \
API_BASE_URL=http://127.0.0.1:8000 \
GEMINI_API_KEY=your-key \
python -m app.voice.worker
```

Tests:

```bash
cd backend && pytest
```

## Try one claim

1. On the call: “I had an accident yesterday near my office. Nobody was injured.”
2. End the call. The claim stays open.
3. In chat: `POL123456`, `MH12AB1234`, `ada@example.com`, then one or more JPEG, PNG, or WebP photos.
4. Submit. Revision 1 is `completed`.
5. Finish later before the photos exist. Come back and submit. Revision 2 is new; revision 1 stays `partial`.
6. Give a second date without saying “actually”, then submit. The outcome is `needsReview`.
7. “I don’t want to continue” records `optedOut`.

## Architecture

```mermaid
flowchart LR
  browser[React call and chat] --> turn[POST /turns]
  browser --> ws[WebSocket]
  livekit[LiveKit room] --> worker[Voice worker]
  worker --> turn
  turn --> graph[LangGraph]
  graph --> gemini[Gemini]
  graph --> db[(Postgres)]
  turn --> redis[(Redis)]
  redis --> worker
  redis --> ws
```

`TurnService.handle_turn` is the only place a user message changes the claim. The call, the chat, the upload, and the submit, finish-later, and opt-out actions all enter there. The graph has three nodes: interpret the utterance, validate and merge fields, then choose the next question or an outcome. A Redis lock wraps that write so the call and the chat cannot merge at the same time. Without Redis the lock is skipped, which is how the tests run on SQLite.

The browser subscribes to `/api/sessions/{id}/ws`. Redis also publishes `session:{id}` and, when a call starts or stops, `voice:control`. The worker joins the LiveKit room, transcribes with Gemini, posts the turn, and speaks the reply. A chat update during a live call is spoken too, unless the worker itself wrote it. Ending the call sets `call_active` to false and does not file a record. The older SSE stream is still available.

## Final record and sanitization

Each filed revision is one row, unique on `(session_id, revision)`. The payload is:

- `sessionId`, `revision`, `outcome`
- `fields`: every collected field as `value`, `source` (`call` or `chat`), and `status`
- `conflicts`: fields with two answers
- `issues`: missing, invalid, conflicting, or ambiguous items

Normalization happens before the value is stored. Policy numbers become `POL` plus 6 to 12 letters or digits. Registrations lose spaces (`MH 12 AB 1234` becomes `MH12AB1234`). Emails are lowercased. Dates become `YYYY-MM-DD`. Photos are stored as id, file name, and content type, never as a filesystem path. Descriptions and locations are trimmed and length-limited.

A record is delivered after the row commits. The local inbox uses the idempotency key `sessionId:revision`. A configured `RECORD_WEBHOOK_URL` receives the same key. Startup retries rows that are still `pending` and does not insert another revision. Sending the same revision again is a no-op.

## Outcomes and revisions

| Outcome | When |
| --- | --- |
| `completed` | Every required field is valid, nothing conflicts, and the user submits |
| `partial` | The user finishes later, or leaves required fields unresolved on purpose |
| `optedOut` | The user says they do not want to continue, or uses Opt out |
| `needsReview` | They submit while a field conflicts, or the story and the injury answer disagree, including a mention of a death |

Required on the call: what happened, the date, the place, and whether anyone was injured. Injury details become required only if someone was injured. Required in chat: policy number, registration, email, and at least one damage photo. Follow-up by phone, email, or either is optional and can be skipped.

A submit with nothing new does not write another row. A later correction sets the session dirty and the next submit writes the next revision. The older record stays.

## Stack, in under 150 words

React and TypeScript render the call, transcript, chat, and uploads. LiveKit carries the microphone and the spoken reply. A Python worker transcribes, interrupts on barge-in, and posts into the same turn path. FastAPI serves REST, the session WebSocket, and session state. LangGraph interprets, validates, and responds for both channels. Gemini extracts answers, phrases the next question, and handles speech. Postgres stores sessions, messages, and revisions. Redis carries events, the speak cue, and the session lock. Docker Compose starts Postgres, Redis, LiveKit, the API, and the worker. SQLite is only for tests and a local API. You supply one credential, `GEMINI_API_KEY`. Local LiveKit uses a built-in dev key. Public voice needs LiveKit Cloud or a host with UDP open. Cost is Gemini tokens, including audio, times the configured rates. With no key, chat still files a claim and model cost is zero.

## Product decisions

The claim is a car accident because speech fits the story and chat fits exact strings and photos. The agent does not file on its own. It asks for a submit once the required fields are valid. A repeated different answer is a conflict unless the person says “actually” or a similar correction, in which case the new value replaces the old one. Submit during an unresolved conflict files `needsReview` instead of guessing. Invalid input keeps the last valid value and explains the rule. Photos that are not JPEG, PNG, or WebP are refused, including by file signature rather than the browser’s content type.

## Edge cases

Handled: invalid policy, email, registration, and date; future dates and dates older than a year; a bad photo; a conflict versus a correction; ending the call without filing; continuing in chat; a partial record followed by a new revision; a second submit with no edits; opt-out; an injury story that contradicts the yes/no answer; server restart without a second record.

Not handled: HEIC photos, more than one vehicle, identity checks, malware scanning, phone-network calls, languages other than English, and two people editing one session at once. The session lock only holds when Redis is configured.

## Least trusted part

Gemini speech-to-text and speech synthesis. A noisy room can be transcribed as a turn, and the energy detector can miss a quiet interruption or treat a loud noise as one. Interrupt still clears the playback queue. A parsed policy number, plate, email, or date still overrides the model, including the phrased reply: if a rewrite drops one of those facts, the template sentence is spoken instead.

## Cost and latency

`GET /api/metrics` reports p50 and p95 of server turn time, token counts, and an estimated dollar cost once the Gemini rates are set. Those times cover the graph and the database write. Transcription and speech add further Gemini tokens that are not all included in that counter. The local LiveKit server has no vendor fee. LiveKit Cloud, if you use it for a public deployment, is a separate bill. A completed chat session with no Gemini key has a model cost of zero.

## With another week

Check damage photos with a vision model. Add Hindi. Require a signed session link so claims are not guessable. Show delivery failures in the desk instead of only in logs. Put the voice worker on the same network path as the browser so a public host does not depend on `node_ip: 127.0.0.1`.

## How AI tools were used

The first implementation was drafted in Cursor with an AI coding agent from the written assignment, then checked against the outcome, revision, and restart tests. The parts to re-read by hand are the date and registration parsers and the voice activity thresholds. Those are where a generated first pass is most likely to be subtly wrong. The tests in `backend/tests/test_flow.py` are the check that the shared session, the four outcomes, and the single delivery actually hold.
