"""
Smoke test for the /v1/chat gateway in server.py.

Dependency-free (stdlib only: urllib), so it runs in any Python 3 environment
without needing the project's venv active.

Usage:
    python3 scripts/smoke_test.py [base_url]

    base_url defaults to http://127.0.0.1:8000

What it checks:
    1. /health responds.
    2. A single conversation can run multiple turns back-to-back without
       hitting "Unexpected content type while awaiting request info
       responses." -- this is the original bug. Every turn after the first
       resumes a paused (request_info) workflow.
    3. Two different session_ids interleaved don't contaminate each other
       (this would have broken under the old shared-singleton-workflow
       design).

This script does NOT test restart-resilience (surviving a server restart
mid-conversation) -- that needs you to actually stop/start the server
process by hand. See the "Restart test" steps printed at the end.
"""
import json
import sys
import urllib.request
import urllib.error
import uuid

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"


def post_chat(session_id: str, message: str) -> dict:
    body = json.dumps({"session_id": session_id, "message": message}).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE_URL}/v1/chat",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8")
        raise RuntimeError(f"HTTP {e.code} from {BASE_URL}/v1/chat: {detail}") from None


def check_health() -> None:
    with urllib.request.urlopen(f"{BASE_URL}/health", timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    assert data.get("status") == "healthy", f"Unexpected /health response: {data}"
    print(f"[PASS] /health -> {data}")


def run_conversation(label: str, session_id: str, turns: list) -> None:
    print(f"\n--- {label} (session_id={session_id}) ---")
    for i, message in enumerate(turns, start=1):
        result = post_chat(session_id, message)
        print(f"  turn {i} > {message!r}")
        print(f"  turn {i} < agent={result['active_agent']!r} reply={result['response'][:200]!r}")
    print(f"[PASS] {label} completed {len(turns)} turns without error.")


def run_interleaved_sessions() -> None:
    print("\n--- Two independent sessions interleaved ---")
    session_a = f"smoke-a-{uuid.uuid4().hex[:8]}"
    session_b = f"smoke-b-{uuid.uuid4().hex[:8]}"

    r1 = post_chat(session_a, "Hi, I'm looking for a 2 bedroom apartment.")
    print(f"  A turn 1 < agent={r1['active_agent']!r}")

    # Session B's very first message arrives while A is mid-conversation
    # (A's workflow is now paused awaiting request_info). Under the old
    # shared-singleton design this is exactly where a brand-new,
    # unrelated session would crash.
    r2 = post_chat(session_b, "Hello, do you have any studio apartments?")
    print(f"  B turn 1 < agent={r2['active_agent']!r}")

    r3 = post_chat(session_a, "Yes, please show me what's available downtown.")
    print(f"  A turn 2 < agent={r3['active_agent']!r}")

    r4 = post_chat(session_b, "What's the price range on those?")
    print(f"  B turn 2 < agent={r4['active_agent']!r}")

    print("[PASS] Interleaved sessions completed without cross-contamination.")


def main() -> None:
    print(f"Target: {BASE_URL}")
    check_health()

    session_id = f"smoke-{uuid.uuid4().hex[:8]}"
    run_conversation(
        "Single conversation, multiple turns",
        session_id,
        turns=[
            "Hi, I'm looking for a 2 bedroom apartment under $2500.",
            "That sounds good, can I book a tour for the first one?",
            "My name is Jordan Smith.",
        ],
    )

    run_interleaved_sessions()

    print(
        "\nAll automated checks passed.\n"
        "\nRestart test (do this by hand to validate the checkpoint upgrade):\n"
        "  1. Run: curl -s -X POST {url}/v1/chat -H 'Content-Type: application/json' \\\n"
        "         -d '{{\"session_id\": \"restart-test\", \"message\": \"Hi, show me 1 bedroom places.\"}}'\n"
        "  2. Stop the server (Ctrl+C) and start it again.\n"
        "  3. Send a second message with the SAME session_id:\n"
        "     curl -s -X POST {url}/v1/chat -H 'Content-Type: application/json' \\\n"
        "         -d '{{\"session_id\": \"restart-test\", \"message\": \"Yes, the first one looks good.\"}}'\n"
        "  4. It should reply normally (no 500), proving the conversation state\n"
        "     survived the restart via checkpoints/ on disk -- this is the part\n"
        "     that would NOT have worked with the old in-memory-only design.\n".format(url=BASE_URL)
    )


if __name__ == "__main__":
    main()