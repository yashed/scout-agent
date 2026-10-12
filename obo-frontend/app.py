"""OBO (On-Behalf-Of) flow tester — a local debugging UI, not a deployable agent.

Does exactly three things, visibly:

  1. Signs you in at a real external IdP (e.g. Okta) via an actual
     Authorization Code + PKCE redirect — this app's own URL is the OAuth
     redirect_uri, so the callback lands back here automatically.
  2. Decodes the resulting caller token and shows its header and payload,
     flagging whether its `typ` header would satisfy ThunderID's RFC 9068
     check if declared as `access_token` (most real IdPs' tokens won't —
     see ../obo_identity.py's module docstring for why that's fine).
  3. Calls the deployed Scout agent's real `/chat` endpoint with that
     caller token. Scout performs its own internal RFC 8693 token exchange
     against ThunderID exactly as it would for any real caller — this app
     never touches that exchange directly, it only calls the public
     endpoint, the same way any real application would.

Every action is appended to a running log panel so the whole session reads
back as one trace.
"""

from __future__ import annotations

import base64
import json
import secrets
import hashlib
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests
import streamlit as st

st.set_page_config(page_title="OBO Flow Tester", layout="wide")

# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------
_DEFAULTS = {
    "log": [],
    "caller_token": None,
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
                safe_data[sensitive] = str(safe_data[sensitive])[:12] + "...<redacted>"
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


def discover_oidc_endpoints(issuer: str) -> tuple[str, str]:
    """Fetches {issuer}/.well-known/openid-configuration and returns its
    (authorization_endpoint, token_endpoint).

    This used to be `f"{issuer}/v1/authorize"` / `f"{issuer}/v1/token"`,
    hardcoded — which happens to match Okta's own path convention
    (.../oauth2/default/v1/authorize) but isn't part of the OIDC spec at
    all. Auth0's real endpoints are just {issuer}/authorize and
    {issuer}/oauth/token, no "v1" segment, so that hardcoding silently
    produced a 404 for any non-Okta issuer. Real discovery is the only way
    this tool is actually provider-agnostic, which is the whole point of
    letting the sidebar's issuer field be freeform.
    """
    discovery_url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
    resp = requests.get(discovery_url, timeout=10)
    resp.raise_for_status()
    doc = resp.json()
    return doc["authorization_endpoint"], doc["token_endpoint"]


def encode_state(data: dict) -> str:
    """Pack everything the callback will need into the OAuth `state` value
    itself, rather than st.session_state.

    Streamlit starts a brand-new session on every full browser navigation —
    including the external redirect to the IdP and back, even in the same
    tab — so anything stashed in session_state before the redirect (the PKCE
    verifier, the sidebar's issuer/client_id/redirect_uri) is already gone by
    the time the callback arrives. `state` is the one piece of this request
    the IdP is contractually required to echo back unchanged, so it's the
    only reliable place to carry state across the round-trip here.
    """
    raw = json.dumps(data).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_state(value: str) -> dict:
    def pad(s: str) -> str:
        return s + "=" * (-len(s) % 4)

    return json.loads(base64.urlsafe_b64decode(pad(value)))


# ---------------------------------------------------------------------------
# Sidebar — configuration (nothing here is persisted to disk)
# ---------------------------------------------------------------------------
st.sidebar.header("Configuration")
st.sidebar.caption("Local testing tool. Nothing entered here is written to disk.")

st.sidebar.subheader("1. External IdP")
st.sidebar.caption("Any OIDC-compliant provider — endpoints are discovered from the issuer's own .well-known document.")
okta_issuer = st.sidebar.text_input(
    "Issuer", placeholder="https://your-domain.okta.com/oauth2/default or https://your-tenant.us.auth0.com/", key="okta_issuer"
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

st.sidebar.subheader("2. Scout agent")
scout_chat_url = st.sidebar.text_input(
    "Chat endpoint", placeholder="http://<gateway-host>/<agent>/chat", key="scout_chat_url"
)

if st.sidebar.button("Reset everything"):
    for key in list(st.session_state.keys()):
        del st.session_state[key]
    st.rerun()

# ---------------------------------------------------------------------------
# Catch the OAuth redirect back from Okta, if this load carries one.
#
# This is necessarily a fresh Streamlit session (see encode_state's
# docstring), so nothing here can read st.session_state from before the
# redirect — everything needed is decoded from `state` itself instead.
# ---------------------------------------------------------------------------
params = st.query_params
if "code" in params and st.session_state["caller_token"] is None:
    code = params["code"]
    raw_state = params.get("state", "")
    try:
        state_payload = decode_state(raw_state)
    except Exception as exc:
        log("FAIL", f"could not decode state parameter ({exc}) — the callback URL may be stale or malformed")
        state_payload = None

    if state_payload:
        token_url = state_payload["token_endpoint"]
        form = {
            "grant_type": "authorization_code",
            "client_id": state_payload["client_id"],
            "redirect_uri": state_payload["redirect_uri"],
            "code": code,
            "code_verifier": state_payload["verifier"],
        }
        log("INFO", "Authorization code received — exchanging it for a caller access token")
        log_request("POST", token_url, {"content-type": "application/x-www-form-urlencoded"}, form)
        try:
            resp = requests.post(token_url, data=form, timeout=10)
            log_response(resp)
            resp.raise_for_status()
            body = resp.json()
            st.session_state["caller_token"] = body["access_token"]
            log("OK", "Caller token minted")
        except Exception as exc:
            log("FAIL", f"code exchange failed: {exc}")
    st.query_params.clear()
    st.rerun()

# ---------------------------------------------------------------------------
# Flow status tracker
# ---------------------------------------------------------------------------
st.title("OBO Flow Tester")
st.caption("Signs in at the real IdP, calls the real deployed Scout agent, visualizes every step.")

step1_done = bool(st.session_state["caller_token"])
step2_done = step1_done
step3_done = bool(st.session_state["chat_response"] and st.session_state["chat_response"]["status"] == 200)

cols = st.columns(4)
steps = [
    ("1. Sign in", step1_done),
    ("2. Decode token", step2_done),
    ("3. Call Scout", step3_done),
    ("4. Response", step3_done),
]
for col, (label, done) in zip(cols, steps):
    with col:
        st.markdown(f"{'🟢' if done else '⚪'} **{label}**")

st.divider()

col_main, col_log = st.columns([3, 2])

with col_main:
    st.header("1. Sign in at the external IdP")
    missing_okta = not (okta_issuer and okta_client_id and okta_redirect_uri)
    if st.session_state["caller_token"]:
        st.success("Caller token acquired.")
    elif missing_okta:
        st.info("Fill in the external IdP's issuer, client ID and redirect URI in the sidebar first.")
    else:
        if st.button("Sign in", type="primary"):
            try:
                authorization_endpoint, token_endpoint = discover_oidc_endpoints(okta_issuer)
            except Exception as exc:
                log("FAIL", f"OIDC discovery failed for issuer {okta_issuer!r}: {exc}")
                st.error(
                    f"Couldn't fetch {okta_issuer.rstrip('/')}/.well-known/openid-configuration ({exc}). "
                    "Check the issuer URL — it should be the base issuer, not an endpoint path."
                )
                authorization_endpoint = token_endpoint = None

            if authorization_endpoint and token_endpoint:
                verifier, challenge = make_pkce_pair()
                state = encode_state({
                    "verifier": verifier,
                    "nonce": secrets.token_urlsafe(8),
                    "issuer": okta_issuer,
                    "client_id": okta_client_id,
                    "redirect_uri": okta_redirect_uri,
                    "token_endpoint": token_endpoint,
                })
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
                authorize_url = f"{authorization_endpoint}?{urlencode(query)}"
                log("INFO", f"Discovered endpoints — authorize: {authorization_endpoint} | token: {token_endpoint}")
                log("INFO", f"Redirecting to IdP: {authorize_url}")
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
                st.warning("No `typ` header — Scout's own exchange code declares the subject token as the "
                           "generic `jwt` type specifically to handle this, so this is expected and fine.")
            elif typ not in ("at+jwt", "application/at+jwt"):
                st.warning(f"typ is {typ!r}, not the RFC 9068 `at+jwt` — same situation as above, still fine.")
            else:
                st.success("typ is RFC 9068-compliant.")
        with c2:
            st.caption("Payload")
            st.json(payload)
        remaining = payload.get("exp", 0) - time.time()
        st.caption(f"Expires in {max(0, int(remaining))}s" if remaining > 0 else "Expired — sign in again")
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
    st.header("3. Call Scout agent for real")
    st.caption("Scout performs its own internal RFC 8693 exchange here — this is the real end-to-end test.")
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
            st.session_state["chat_response"] = {"status": 0, "body": str(exc)}

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
