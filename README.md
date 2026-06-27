# Property Agent App

A multi-agent real estate assistant built with Microsoft Agent Framework, FastAPI, and Supabase.

It supports:

- Property discovery and filtering
- Tour booking
- Booking management (list, cancel, reschedule)
- Durable workflow resume via checkpoint storage

## Tech Stack

- Python 3.11+
- FastAPI
- Microsoft Agent Framework (`agent-framework`, `agent-framework-openai`)
- Supabase (Postgres)
- YAML-driven agent definitions
- SQLite (lightweight session metadata)

## Repository Structure

- `app.py`: Agent client/bootstrap and workflow graph factory
- `server.py`: FastAPI gateway with per-session workflow resume
- `config/*.yaml`: Agent prompts/tool contracts
- `tools/property_tools.py`: Property search tool
- `tools/scheduling_tools.py`: Booking tools
- `utils/agent_config.py`: YAML to agent builder
- `utils/logger.py`: Central logger setup
- `seeder.txt`: Supabase SQL schema and seed data

## Architecture Overview

```mermaid
flowchart LR
  Client["Client App"] --> API["FastAPI server.py"]
  API --> WF["Fresh workflow per request"]
  WF --> CP["FileCheckpointStorage"]
  API --> META["session_state.db"]
  WF --> PF["PropertyFinderAgent"]
  WF --> TS["TourSchedulerAgent"]
  WF --> CPAG["CustomerPortalAgent"]
  PF --> PT["search_properties_api"]
  TS --> ST["confirm_tour_booking"]
  CPAG --> ST2["list/cancel/reschedule tools"]
  PT --> SB["Supabase"]
  ST --> SB
  ST2 --> SB
```

## Workflow Routing

```mermaid
flowchart TD
  U["User Message"] --> PF["PropertyFinderAgent"]
  PF -->|property search| PF
  PF -->|book or schedule tour| TS["TourSchedulerAgent"]
  PF -->|manage existing bookings| CP["CustomerPortalAgent"]
  TS -->|booking complete| PF
  CP -->|actions complete| PF
```

## Request Lifecycle

```mermaid
sequenceDiagram
  participant C as Client
  participant S as FastAPI /v1/chat
  participant W as Fresh workflow
  participant CK as FileCheckpointStorage
  participant DB as session_state.db

  C->>S: POST /v1/chat {session_id, message}
  S->>DB: Load checkpoint_id + pending_request_id
  S->>W: build_workflow_agent()
  S->>W: run(messages, checkpoint_id, checkpoint_storage)
  W->>CK: read/write checkpoints
  W-->>S: result (text, active_agent, messages)
  S->>DB: Save new checkpoint_id + pending_request_id
  S-->>C: {session_id, active_agent, response}
```

## Prerequisites

- Python 3.11+
- Supabase project with SQL from `seeder.txt` executed
- OpenAI API key

## Environment Variables

Copy `env.example` to `.env` and set values:

- `OPENAI_API_KEY`: OpenAI key
- `AGENT_MODEL`: Model name (default `gpt-4o-mini`)
- `SUPABASE_URL`: Supabase URL
- `SUPABASE_SERVICE_ROLE_KEY`: Supabase service role key
- `SUPABASE_KEY`: Optional alternative to service role key
- `DEVUI_AUTH_TOKEN`: Optional token for dev UI
- `APP_PORT`: Optional dev UI port (default `8000`)

Note: tool modules accept either `SUPABASE_KEY` or `SUPABASE_SERVICE_ROLE_KEY`.

## Setup

1. Create and activate a virtual environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

For dev UI extras:

```bash
pip install -r requirements-dev.txt
```

## Run API Gateway

```bash
uvicorn server:app --host 0.0.0.0 --port 8000 --reload
```

Health check:

```bash
curl http://localhost:8000/health
```

## Run Dev UI (Optional)

```bash
python app.py
```

## API Contract

### POST `/v1/chat`

Request body:

```json
{
  "session_id": "optional-uuid",
  "message": "I need a 2-bedroom under 3000 with gym"
}
```

Response body:

```json
{
  "session_id": "same-or-generated-id",
  "active_agent": "Master_Property_Workflow",
  "response": "...assistant response..."
}
```

## Logging and Debugging

Logs are written to:

- Console
- `logs/app_execution.log`

Current logging coverage includes:

- Incoming chat request metadata (`session_id`, message length)
- Session metadata load/save (`checkpoint_id`, `pending_request_id`)
- Checkpoint resolution after each run
- Final active agent per response
- Tool-level DB operations (search, booking, cancel, reschedule)
- Error stack traces on failures

## Troubleshooting

- `Agent Execution Failure: Unexpected content type while awaiting request info responses.`
  - Ensure resume payload is sent as a `tool` role `function_result` (implemented in `server.py`).
  - Ensure checkpoint storage is durable and valid (`FileCheckpointStorage` in `app.py`).

- `Checkpoint deserialization blocked for type ...`
  - Ensure `allowed_checkpoint_types` in `app.py` includes the required handoff/request-info types.

- Supabase auth errors
  - Verify `SUPABASE_URL` and one of `SUPABASE_KEY` / `SUPABASE_SERVICE_ROLE_KEY`.

## Notes

- FastAPI path uses a fresh workflow instance per request for isolation and safe resume behavior.
- Session continuity is persisted via `checkpoint_id` and `session_state.db`, not in-process memory.
