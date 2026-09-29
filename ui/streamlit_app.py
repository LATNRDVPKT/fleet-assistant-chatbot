"""
Fleet Copilot — Streamlit chat UI.

The UI only talks to the FastAPI backend over HTTP (it never imports the pipeline), exactly like a
mobile app would.

Run:   streamlit run ui/streamlit_app.py
Env:   API_URL (default http://localhost:8000)   API_KEY (default dev-key-123)
"""
import logging
import os
import sys
import uuid

import requests
import streamlit as st

logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                    format="%(asctime)s | %(levelname)-5s | ui | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("ui")

API_URL = os.getenv("API_URL", "http://localhost:8000").rstrip("/")
API_KEY = os.getenv("API_KEY", "dev-key-123")
TIMEOUT_SECONDS = 60

STATUS_BADGE = {"answered": "✅ Answered", "clarification_needed": "❓ Needs a vehicle",
                "not_found": "🔍 Not in documents", "blocked": "⛔ Blocked"}

st.set_page_config(page_title="Fleet Copilot", page_icon="🚚", layout="wide")


# ------------------------------------------------------------------ API helpers
def call_api(method: str, path: str, **kwargs) -> tuple[int, dict]:
    """Call the backend and ALWAYS return (status_code, json). Network errors become a fake 503."""
    request_id = uuid.uuid4().hex[:12]
    headers = {"X-API-Key": st.session_state.get("api_key", API_KEY), "X-Request-ID": request_id}
    log.info(f"→ {method} {path} request_id={request_id}")
    try:
        r = requests.request(method, f"{st.session_state.get('api_url', API_URL)}{path}",
                             headers=headers, timeout=TIMEOUT_SECONDS, **kwargs)
        log.info(f"← {r.status_code} {path} request_id={request_id} in {r.elapsed.total_seconds():.2f}s")
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {"error": {"code": "UI", "message": r.text[:300], "request_id": request_id}}
    except requests.exceptions.Timeout:
        log.warning(f"✗ timeout after {TIMEOUT_SECONDS}s request_id={request_id}")
        return 504, {"error": {"code": "UI-TIMEOUT", "message": "The server took too long to answer.",
                               "retryable": True, "request_id": request_id}}
    except requests.exceptions.ConnectionError:
        log.warning(f"✗ cannot reach {API_URL} request_id={request_id}")
        return 503, {"error": {"code": "UI-OFFLINE", "message": f"Cannot reach the API at {API_URL}. Is it running?",
                               "retryable": True, "request_id": request_id}}


def load_vehicles() -> list[dict]:
    """Vehicle list for the dropdown, fetched once per browser session."""
    if not st.session_state.get("vehicles"):
        status, data = call_api("GET", "/v1/vehicles")
        st.session_state.vehicles = data if status == 200 else []
    return st.session_state.vehicles


def render_answer(msg: dict, idx: int):
    data = msg["data"]
    if "error" in data:
        err = data["error"]
        st.error(f"**{err.get('message')}**  \nCode: `{err.get('code')}` · request id: `{err.get('request_id')}`")
        return
    st.markdown(data["answer"])
    meta = [STATUS_BADGE.get(data["status"], data["status"])]
    if data.get("confidence") not in (None, "n/a"):
        meta.append(f"confidence: **{data['confidence']}**")
    if data.get("vehicle_model"):
        meta.append(f"vehicle: **{data['vehicle_model']}**")
    meta.append(f"{data['latency_ms']:.0f} ms")
    meta.append(f"id `{data['request_id']}`")
    st.caption(" · ".join(meta))
    if data.get("degraded"):
        st.warning("Simplified answer (fallback mode). Please check the cited page.")
    if data.get("citations"):
        with st.expander(f"📚 Sources ({len(data['citations'])})"):
            for c in data["citations"]:
                page = f"page {c['page']}" if c["page"] != "n/a (docx)" else "section only"
                st.markdown(f"**[{c['id']}] {c['doc_title']}** (v{c['doc_version']}) — {c['section']} — {page} "
                            f"· {c['content_type']} · score {c['score']}")
                st.code(c["snippet"], language="markdown", wrap_lines=True)
    if data.get("trace"):
        with st.expander("🔎 Pipeline trace (debug)"):
            st.dataframe([{k: v for k, v in s.items() if k in ("step", "name", "status", "ms")} for s in data["trace"]],
                         hide_index=True, use_container_width=True)
    c1, c2, _ = st.columns([1, 1, 10])
    if c1.button("👍", key=f"up{idx}"):
        call_api("POST", "/v1/feedback", json={"request_id": data["request_id"], "rating": "up"})
        st.toast("Thanks for the feedback!")
    if c2.button("👎", key=f"down{idx}"):
        call_api("POST", "/v1/feedback", json={"request_id": data["request_id"], "rating": "down"})
        st.toast("Thanks — we'll review this answer.")


# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.title("🚚 Fleet Copilot")
    st.session_state.api_url = st.text_input("API URL", st.session_state.get("api_url", API_URL))
    st.session_state.api_key = st.text_input("API key", st.session_state.get("api_key", API_KEY), type="password")

    status, health = call_api("GET", "/health")
    if status == 200:
        st.success(f"API online · index {health.get('index_version')} · {health.get('chunks')} chunks")
    else:
        st.error(f"API not ready: {health.get('error') or health}")

    vehicles = load_vehicles()
    labels = ["(not selected)"] + [f"{v['model']} — {v['name']}" for v in vehicles]
    choice = st.selectbox("Vehicle", labels)
    vehicle = None if choice == "(not selected)" else choice.split(" — ")[0]
    variants = next((v["variants"] for v in vehicles if v["model"] == vehicle), [])
    variant = st.selectbox("Variant", ["(any)"] + variants) if variants else "(any)"
    debug = st.toggle("Show pipeline trace", value=False)
    if st.button("Clear chat"):
        st.session_state.messages = []

    st.markdown("**Try asking**")
    for q in ["How often should I change the engine oil?", "What is the harsh braking threshold?",
              "What does DTC P2463 mean?", "How many hours can a driver work per day?"]:
        if st.button(q, use_container_width=True):
            st.session_state.pending = q

# ------------------------------------------------------------------ chat
st.header("Ask about your vehicle")
st.caption("Answers come only from approved manuals and the fleet handbook, with page citations. "
           "Do not use while driving.")

if "messages" not in st.session_state:
    st.session_state.messages = []

for i, msg in enumerate(st.session_state.messages):
    with st.chat_message(msg["role"]):
        if msg["role"] == "user":
            st.markdown(msg["content"])
        else:
            render_answer(msg, i)

question = st.chat_input("e.g. What is the tyre pressure for the rear axle?") or st.session_state.pop("pending", None)
if question:
    st.session_state.messages.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)
    with st.chat_message("assistant"):
        with st.spinner("Searching manuals and checking the answer…"):
            payload = {"question": question, "vehicle_model": vehicle, "debug": debug,
                       "variant": None if variant == "(any)" else variant}
            status, data = call_api("POST", "/v1/chat", json=payload)
        msg = {"role": "assistant", "data": data}
        st.session_state.messages.append(msg)
        render_answer(msg, len(st.session_state.messages) - 1)
