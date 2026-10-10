# OBO Flow Tester

A local Streamlit app for exercising Scout's delegated-access (OBO) flow end
to end, with every step visible — not a deployable agent, a debugging tool.

It does four things, in order:

1. **Signs you in at a real external IdP** (e.g. Okta) via an actual
   Authorization Code + PKCE redirect. This app's own URL is the OAuth
   `redirect_uri`, so the callback lands back here automatically and the
   code-for-token exchange happens right in front of you.
2. **Decodes the resulting caller token** and shows its header and payload,
   flagging whether its `typ` header would satisfy ThunderID's RFC 9068
   check if declared as `access_token` (most real IdPs' tokens won't — see
   `../obo_identity.py`'s module docstring for why that's fine).
3. **Optionally runs the RFC 8693 token exchange directly from this app**,
   authenticating as the AgentID, so you can see the literal request and
   response ThunderID produces — independent of whatever Scout itself logs.
4. **Calls Scout's real `/chat` endpoint** with the caller token, so Scout
   performs its own internal exchange exactly as it would for a real caller.

Every action is appended to a running log panel so the whole session reads
back as one trace.

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
- **AgentID** (for step 3 only): the same `AMP_AGENTID_TOKEN_ENDPOINT` /
  `AMP_AGENTID_CLIENT_ID` / `AMP_AGENTID_CLIENT_SECRET` values Agent Manager
  injects into the deployed Scout agent, plus the MCP resource and scopes
  you want to request.
- **Scout agent**: the deployed agent's `/chat` endpoint URL.

Nothing entered here is written to disk — it lives only in this browser
session's state for as long as the app is running.

## Notes

- Step 3 is a standalone demonstration of the exchange call — it does not
  read anything from Scout's own process, and the token it mints is not
  reused in step 4. Step 4 always sends the original caller token and lets
  Scout do its own exchange, which is the actual thing being tested.
- If step 1's callback fails with "no code_verifier in session," you likely
  opened the Okta sign-in link in a new tab rather than letting it navigate
  the current one — the PKCE verifier lives in this app's session state and
  needs the same browser tab to come back to.
