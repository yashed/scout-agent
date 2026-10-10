# OBO Flow Tester

A local Streamlit app for exercising Scout's delegated-access (OBO) flow end
to end, with every step visible — not a deployable agent, a debugging tool.

It does three things, in order:

1. **Signs you in at a real external IdP** (e.g. Okta) via an actual
   Authorization Code + PKCE redirect. This app's own URL is the OAuth
   `redirect_uri`, so the callback lands back here automatically and the
   code-for-token exchange happens right in front of you.
2. **Decodes the resulting caller token** and shows its header and payload,
   flagging whether its `typ` header would satisfy ThunderID's RFC 9068
   check if declared as `access_token` (most real IdPs' tokens won't — see
   `../obo_identity.py`'s module docstring for why that's fine).
3. **Calls the deployed Scout agent's real `/chat` endpoint** with that
   caller token. Scout performs its own internal RFC 8693 token exchange
   against ThunderID exactly as it would for any real caller — this app
   never touches that exchange directly, it only calls the public endpoint,
   the same way any real application would.

A status row at the top tracks progress through sign-in, token, call, and
response, and every action is appended to a running log panel so the whole
session reads back as one trace.

## Run it

```bash
cd obo-frontend
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

Streamlit opens at `http://localhost:8501` by default — use that exact URL
as the registered **Sign-in redirect URI** on your Okta app (or whatever
port you actually run it on).

## What you need in the sidebar

- **External IdP**: issuer, client ID, redirect URI (this app's own URL),
  scope, and — only if you're using a custom authorization server rather
  than the org default — its audience.
- **Scout agent**: the deployed agent's `/chat` endpoint URL (e.g. the
  **Invoke URL** shown on the agent's Overview page, plus `/chat`).

Nothing entered here is written to disk — it lives only in this browser
session's state for as long as the app is running.

## Before testing, check on the agent's own console pages

This app only calls the public `/chat` endpoint — it can't see or fix
anything inside Scout's own configuration. Before running a test, confirm:

- **Agent ID → Delegated Access (OBO)**: readiness checklist all green
  (except "Gateway challenge," which stays Unknown by design) and the
  **Enable Delegated Access** toggle switched on.
- **The target role's External identities tab** actually has a mapping rule
  for your caller (the console's OBO toggle being on does *not* imply any
  particular user is mapped to the role this agent needs — check this
  specifically for the role tied to this agent).
- **That same role's Permissions tab** includes every scope you intend to
  request — a resource/scope being listed in the Delegation Ceiling picker
  does not mean the mapped role actually grants it.
- **The deployed agent's environment variables** include `AMP_OBO_ENABLED=true`
  plus the matching `<PROXY>_OBO_SCOPES` for each MCP proxy you're testing.
  The console's OBO toggle only configures Thunder's side (the grant type);
  it does **not** set these on the running agent, and without them Scout
  silently falls back to its own client_credentials identity instead of
  doing OBO at all.

## Notes

- If step 1's callback fails with "no code_verifier in session," you likely
  opened the Okta sign-in link in a new tab rather than letting it navigate
  the current one — the PKCE verifier lives in this app's session state and
  needs the same browser tab to come back to.
- A token minted at step 1 is typically only valid for about an hour — if
  step 3 fails in a way that looks unrelated to the OBO setup, check whether
  the token simply expired (its remaining lifetime is shown under step 2)
  and sign in again.
