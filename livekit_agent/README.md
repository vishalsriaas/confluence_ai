# ShipKia LiveKit voice worker

This worker runs the Confluence-managed ShipKia calling prompt with Gemini Live.
It does not import, call, or modify WA Chat Hub.

Local setup:

1. Run `confluence_ai.shipkia_setup.configure_shipkia_voice` on the development site.
2. Create `/home/harsh/.local/share/shipkia-livekit/.venv` with `uv venv` and
   install this directory's `requirements.txt`.
3. From WSL, run `bash apps/confluence_ai_upstream/livekit_agent/run-local.sh`.
4. As an authenticated System Manager, call
   `confluence_ai.api.shipkia_voice.create_local_test_session` with `customer_phone`.
5. Open LiveKit Meet or Agent Console and enter the returned server URL and participant token.

Real credentials are stored under the site's private directory and must never be
copied into this directory or committed.
