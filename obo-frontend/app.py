"""OBO (On-Behalf-Of) flow tester — a local debugging UI, not a deployable agent.

Walks the real flow end to end, with every step visible:

  1. Sign in at a real external IdP (Okta) via a genuine Authorization Code +
     PKCE redirect — this app's own URL is the redirect_uri, so the OAuth
     callback lands back here automatically.
  2. Decode the caller's access token and show exactly what's in it,
     including the `typ` header, since that header's absence/shape is what
     actually gates whether ThunderID will accept the token at all (see
     scout-agent's own obo_identity.py docstring for why).
  3. Optionally run the RFC 8693 token-exchange call *directly from this
     app*, authenticating as the AgentID, so you can watch the literal
     request/response ThunderID sees — this mirrors obo_identity.py's
     _exchange() exactly, it does not call into Scout's code, it duplicates
     the same wire call so the exchange is visible independently of
     whatever Scout itself logs.
  4. Call the deployed Scout agent's real /chat endpoint with the caller's
     token, so Scout performs its own internal exchange exactly as it would
     for a real caller, and shows the actual response.

Every action appends to a single running log so the whole session reads
back as one narrated trace.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests
import streamlit as st

st.set_page_config(page_title="OBO Flow Tester", layout="wide")

SUBJECT_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:jwt"
TOKEN_EXCHANGE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:token-exchange"

# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------
_DEFAULTS = {
    "log": [],
    "caller_token": None,
    "caller_token_response": None,
    "pkce_verifier": None,
    "oauth_state": None,
    "delegated_token": None,
    "delegated_token_response": None,
    "chat_response": None,
    "session_id": str(uuid.uuid4()),
}
for key, default in _DEFAULTS.items():
    if key not in st.session_state:
        st.session_state[key] = default() if callable(default) else default


def log(level: str, message: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    st.session_state["log"].append(f"[{ts}] [{level}] {message}")


def log_request(method: str, url: str, headers: dict, data: dict | None = None) -> None:
    safe_headers = dict(headers)
    if "Authorization" in safe_headers:
        scheme = safe_headers["Authorization"].split(" ")[0]
        safe_headers["Authorization"] = f"{scheme} <redacted>"
    log("REQ", f"{method} {url}")
    log("REQ", f"  headers: {json.dumps(safe_headers)}")
    if data:
        safe_data = dict(data)
        for sensitive in ("subject_token", "client_secret", "code_verifier"):
            if sensitive in safe_data:
                safe_data[sensitive] = safe_data[sensitive][:12] + "...<redacted>"
        log("REQ", f"  body: {json.dumps(safe_data)}")


def log_response(resp: requests.Response) -> None:
    log("RESP", f"status={resp.status_code}")
    try:
        body = resp.json()
        log("RESP", f"  body: {json.dumps(body)[:800]}")
    except ValueError:
        log("RESP", f"  body (non-JSON): {resp.text[:400]}")


def decode_jwt(token: str) -> tuple[dict, dict]:
    header_b64, payload_b64, *_ = token.split(".")

    def pad(s: str) -> str:
        return s + "=" * (-len(s) % 4)

    header = json.loads(base64.urlsafe_b64decode(pad(header_b64)))
    payload = json.loads(base64.urlsafe_b64decode(pad(payload_b64)))
    return header, payload


def make_pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:64]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


# ---------------------------------------------------------------------------
# Sidebar — configuration (nothing here is persisted to disk)
# ---------------------------------------------------------------------------
st.sidebar.header("Configuration")
st.sidebar.caption("Local testing tool. Secrets stay in this browser session only, never written to disk.")

st.sidebar.subheader("1. External IdP (Okta)")
okta_issuer = st.sidebar.text_input(
    "Issuer", placeholder="https://your-domain.okta.com/oauth2/default", key="okta_issuer"
)
okta_client_id = st.sidebar.text_input("Client ID", key="okta_client_id")
okta_redirect_uri = st.sidebar.text_input(
    "Redirect URI (must be registered on the Okta app, same as this app's own URL)",
    value="http://localhost:8501",
    key="okta_redirect_uri",
)
okta_scope = st.sidebar.text_input("Scope", value="openid profile", key="okta_scope")
okta_audience = st.sidebar.text_input(
    "Audience (only for a custom authorization server, e.g. api://default)",
    key="okta_audience",
)

st.sidebar.subheader("2. AgentID (for the exchange demo)")
agentid_token_endpoint = st.sidebar.text_input(
    "Thunder token endpoint", placeholder="https://.../oauth2/token", key="agentid_token_endpoint"
)
agentid_client_id = st.sidebar.text_input("AgentID client ID", key="agentid_client_id")
agentid_client_secret = st.sidebar.text_input(
    "AgentID client secret", type="password", key="agentid_client_secret"
)
agentid_resource = st.sidebar.text_input(
    "Resource (the MCP proxy URL this agent's own client_credentials auth already uses)",
    key="agentid_resource",
)
agentid_scopes = st.sidebar.text_input(
    "Scopes to request (space-separated, e.g. test:read)", key="agentid_scopes"
)

st.sidebar.subheader("3. Scout agent")
scout_chat_url = st.sidebar.text_input(
    "Chat endpoint", placeholder="https://your-scout-agent/chat", key="scout_chat_url"
)

if st.sidebar.button("Reset everything"):
    for key in list(st.session_state.keys()):
        del st.session_state[key]
    st.rerun()

# ---------------------------------------------------------------------------
# Catch the OAuth redirect back from Okta, if this load carries one
# ---------------------------------------------------------------------------
params = st.query_params
if "code" in params and st.session_state["caller_token"] is None:
    code = params["code"]
    returned_state = params.get("state")
    if returned_state != st.session_state["oauth_state"]:
        log("FAIL", f"state mismatch on callback: expected {st.session_state['oauth_state']!r}, got {returned_state!r}")
    elif not st.session_state["pkce_verifier"]:
        log("FAIL", "no code_verifier in session — did sign-in happen in a different tab/session?")
    else:
        token_url = f"{okta_issuer}/v1/token"
        form = {
            "grant_type": "authorization_code",
            "client_id": okta_client_id,
            "redirect_uri": okta_redirect_uri,
            "code": code,
            "code_verifier": st.session_state["pkce_verifier"],
        }
        log("INFO", "Authorization code received — exchanging it for a caller access token")
        log_request("POST", token_url, {"content-type": "application/x-www-form-urlencoded"}, form)
        try:
            resp = requests.post(token_url, data=form, timeout=10)
            log_response(resp)
            resp.raise_for_status()
            body = resp.json()
            st.session_state["caller_token"] = body["access_token"]
            st.session_state["caller_token_response"] = body
            log("OK", "Caller token minted")
        except Exception as exc:
            log("FAIL", f"code exchange failed: {exc}")
    st.query_params.clear()
    st.rerun()

# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------
st.title("OBO Flow Tester")
st.caption("Drives the real external sign-in, shows the real RFC 8693 exchange, calls the real Scout agent.")

col_main, col_log = st.columns([3, 2])

with col_main:
    st.header("1. Sign in at the external IdP")
    missing_okta = not (okta_issuer and okta_client_id and okta_redirect_uri)
    if st.session_state["caller_token"]:
        st.success("Caller token acquired.")
    elif missing_okta:
        st.info("Fill in the Okta issuer, client ID and redirect URI in the sidebar first.")
    else:
        if st.button("Sign in with Okta", type="primary"):
            verifier, challenge = make_pkce_pair()
            state = secrets.token_urlsafe(16)
            st.session_state["pkce_verifier"] = verifier
            st.session_state["oauth_state"] = state
            query = {
                "client_id": okta_client_id,
                "response_type": "code",
                "scope": okta_scope,
                "redirect_uri": okta_redirect_uri,
                "state": state,
                "code_challenge_method": "S256",
                "code_challenge": challenge,
            }
            if okta_audience:
                query["audience"] = okta_audience
            authorize_url = f"{okta_issuer}/v1/authorize?{urlencode(query)}"
            log("INFO", f"Redirecting to Okta: {authorize_url}")
            st.markdown(
                f'<meta http-equiv="refresh" content="0; url={authorize_url}">',
                unsafe_allow_html=True,
            )
            st.write(f"Redirecting... if nothing happens, [click here]({authorize_url}).")

    st.divider()
    st.header("2. Decoded caller token")
    if st.session_state["caller_token"]:
        header, payload = decode_jwt(st.session_state["caller_token"])
        c1, c2 = st.columns(2)
        with c1:
            st.caption("Header")
            st.json(header)
            typ = header.get("typ")
            if typ in (None, ""):
                st.warning("No `typ` header — ThunderID will still accept this if the exchange "
                           "declares subject_token_type as the generic `jwt` type, not `access_token`.")
            elif typ not in ("at+jwt", "application/at+jwt"):
                st.warning(f"typ is {typ!r}, not the RFC 9068 `at+jwt` — same situation as above.")
            else:
                st.success("typ is RFC 9068-compliant — would pass even if declared as access_token.")
        with c2:
            st.caption("Payload")
            st.json(payload)
        remaining = payload.get("exp", 0) - time.time()
        st.caption(f"Expires in {max(0, int(remaining))}s" if remaining > 0 else "Expired")
        with st.expander("Raw token"):
            st.code(st.session_state["caller_token"], language=None)
    else:
        st.info("Sign in above to get a token, or paste one you already have:")
        pasted = st.text_area("Paste an access token", height=100, key="pasted_token")
        if st.button("Use pasted token"):
            if pasted.strip():
                st.session_state["caller_token"] = pasted.strip()
                log("OK", "Using a manually-pasted caller token")
                st.rerun()

    st.divider()
    st.header("3. Watch the token exchange (RFC 8693)")
    st.caption(
        "Calls Thunder's token endpoint directly with the AgentID's own credentials, "
        "exactly the same wire call Scout's own code makes internally — purely so you "
        "can see the request/response, not required before step 4."
    )
    can_exchange = bool(
        st.session_state["caller_token"] and agentid_token_endpoint and agentid_client_id
        and agentid_client_secret and agentid_resource
    )
    if not can_exchange:
        st.info("Need a caller token plus the AgentID fields in the sidebar.")
    if st.button("Run token exchange", disabled=not can_exchange):
        form = {
            "grant_type": TOKEN_EXCHANGE_GRANT_TYPE,
            "subject_token": st.session_state["caller_token"],
            "subject_token_type": SUBJECT_TOKEN_TYPE,
            "resource": agentid_resource,
        }
        if agentid_scopes.strip():
            form["scope"] = agentid_scopes.strip()
        log("INFO", "Running RFC 8693 token exchange against Thunder")
        log_request(
            "POST", agentid_token_endpoint,
            {"content-type": "application/x-www-form-urlencoded", "Authorization": "Basic <agentid-creds>"},
            form,
        )
        try:
            resp = requests.post(
                agentid_token_endpoint, data=form,
                auth=(agentid_client_id, agentid_client_secret), timeout=10,
            )
            log_response(resp)
            body = resp.json()
            if resp.status_code == 200:
                st.session_state["delegated_token"] = body.get("access_token")
                st.session_state["delegated_token_response"] = body
                log("OK", "Delegated token minted")
            else:
                st.session_state["delegated_token_response"] = body
                error = body.get("error")
                hint = {
                    "access_denied": "No matching authorization mapping for this caller's claims.",
                    "invalid_target": "The `resource` value doesn't match a registered resource server.",
                    "invalid_scope": "The requested scope isn't granted by the mapped role.",
                    "invalid_request": "Check subject_token_type and that all required fields are present.",
                }.get(error, "")
                log("FAIL", f"exchange denied: {error} — {body.get('error_description', '')}. {hint}")
        except Exception as exc:
            log("FAIL", f"exchange request failed: {exc}")

    if st.session_state["delegated_token_response"]:
        st.caption("Response")
        st.json(st.session_state["delegated_token_response"])
        if st.session_state["delegated_token"]:
            d_header, d_payload = decode_jwt(st.session_state["delegated_token"])
            st.caption("Decoded delegated token — look for sub=caller, act=AgentID, aud=resource")
            st.json({"header": d_header, "payload": d_payload})

    st.divider()
    st.header("4. Call Scout agent for real")
    st.caption("Scout performs its own internal exchange here — this is the real end-to-end path.")
    message = st.text_input("Message", value="Fetch https://example.com and summarize it.")
    session_id = st.text_input("Session ID", value=st.session_state["session_id"])
    can_chat = bool(st.session_state["caller_token"] and scout_chat_url)
    if not can_chat:
        st.info("Need a caller token plus the Scout chat endpoint in the sidebar.")
    if st.button("Send chat message", disabled=not can_chat, type="primary"):
        headers = {"Authorization": f"Bearer {st.session_state['caller_token']}", "Content-Type": "application/json"}
        body = {"message": message, "session_id": session_id, "context": {}}
        log("INFO", "Calling Scout's /chat with the caller token")
        log_request("POST", scout_chat_url, headers, body)
        try:
            resp = requests.post(scout_chat_url, headers=headers, json=body, timeout=60)
            log_response(resp)
            st.session_state["chat_response"] = {
                "status": resp.status_code,
                "body": resp.json() if resp.headers.get("content-type", "").startswith("application/json") else resp.text,
            }
            if resp.status_code == 200:
                log("OK", "Scout responded successfully")
            else:
                log("FAIL", f"Scout returned {resp.status_code}")
        except Exception as exc:
            log("FAIL", f"chat request failed: {exc}")

    if st.session_state["chat_response"]:
        resp_data = st.session_state["chat_response"]
        if resp_data["status"] == 200:
            st.success(f"HTTP {resp_data['status']}")
        else:
            st.error(f"HTTP {resp_data['status']}")
        st.json(resp_data["body"])

with col_log:
    st.header("Live log")
    if st.button("Clear log"):
        st.session_state["log"] = []
        st.rerun()
    st.code("\n".join(st.session_state["log"]) or "(nothing logged yet)", language=None)
