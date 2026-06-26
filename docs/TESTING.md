# Testing the Property Agent App

Covers: starting the server, automated smoke tests, the restart-resilience
check, and how to inspect persisted state.

## 1. Start the server

```bash
cd property-agent-app
source .venv/bin/activate
uvicorn server:app --reload --port 8000
```

Confirm it's up:

```bash
curl -s http://127.0.0.1:8000/health
# {"status": "healthy", "engine": "workflow_agent_loaded"}
```

## 2. Run the automated smoke test

```bash
python3 scripts/smoke_test.py
# or against a different host/port:
python3 scripts/smoke_test.py http://127.0.0.1:8000
```

This checks three things end-to-end:

- `/health` responds.
- A single session can run several turns back-to-back (turn 1 → turn 2 →
  turn 3) without the original `"Unexpected content type while awaiting
  request info responses."` error. Every turn after the first resumes a
  paused (`request_info`) workflow, which is exactly where that bug fired.
- Two different `session_id`s interleaved (A → B → A → B) don't contaminate
  each other. This is the shared-singleton-workflow bug — under the old
  design, a second, unrelated session's first message would crash as soon
  as any other session was mid-conversation.

All three passing means the original architectural bugs are fixed.

## 3. Restart-resilience test (manual, do this by hand)

This validates the checkpoint upgrade — that conversation state survives a
process restart instead of living only in memory.

```bash
# 1. Start a conversation
curl -s -X POST http://127.0.0.1:8000/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"session_id": "restart-test", "message": "Hi, show me 1 bedroom places."}'

# 2. Stop the server (Ctrl+C), then start it again:
uvicorn server:app --reload --port 8000

# 3. Resume the SAME session_id
curl -s -X POST http://127.0.0.1:8000/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"session_id": "restart-test", "message": "Yes, the first one looks good."}'
```

Step 3 should reply normally (no 500). If it does, the conversation state
survived the restart via the checkpoint files on disk — this would NOT have
worked with the old in-memory-only design.

If step 3 instead returns a 500 with `"Checkpoint deserialization
blocked for type '...'"`, a custom type used internally by the workflow
(e.g. something from `agent_framework_orchestrations`) isn't in
`allowed_checkpoint_types` in `app.py` yet — add its `module:qualname` to
the list passed to `FileCheckpointStorage(...)`.

## 4. Inspecting persisted state

Two files/folders hold all durable conversation state. Both are
git-ignored (runtime data, not source):

- `checkpoints/` — one JSON file per checkpoint, written by
  `FileCheckpointStorage`. Safe to delete entirely between test runs if you
  want a clean slate (`rm -rf checkpoints/`).
- `session_state.db` — a small SQLite file mapping each `session_id` to its
  latest `checkpoint_id` and any `pending_request_id`. Inspect it directly:

```bash
sqlite3 session_state.db "SELECT * FROM sessions;"
```

If you wipe `checkpoints/`, also delete `session_state.db` (or at least the
rows in it) — otherwise sessions will point at checkpoint IDs that no
longer exist on disk.

## 5. What's still NOT covered by these tests

- Concurrency at the process level: the smoke test sends requests
  sequentially, not truly in parallel. If you need to validate behavior
  under real concurrent load, use a tool like `hey` or `locust` against
  `/v1/chat` with distinct `session_id`s.
- Multi-worker / multi-machine deployments: `session_state.db` is a local
  SQLite file. Running multiple `uvicorn` workers or replicas against the
  same file works for light concurrency but isn't a substitute for a real
  shared database (Postgres, etc.) at higher scale.
