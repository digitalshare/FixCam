"""Sidecar inside the MatrAIx trial network: forwards persona chat to FixCam on the host.

Injects the trial's caseId (which sample video the persona "sent") and a stable
sessionId so every trial is one FixCam conversation.
"""

import os
import uuid

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)
UPSTREAM = os.environ.get("FIXCAM_UPSTREAM", "http://host.docker.internal:8000").rstrip("/")
CASE_ID = os.environ["FIXCAM_CASE_ID"]
SESSION_ID = f"matraix-{CASE_ID}-{uuid.uuid4().hex[:8]}"


@app.get("/health")
@app.get("/v1/health")
def health():
    return jsonify(ok=True, session=SESSION_ID)


@app.post("/v1/messages")
def messages():
    body = request.get_json(silent=True) or {}
    payload = {"message": str(body.get("message", "")), "sessionId": SESSION_ID, "caseId": CASE_ID}
    r = requests.post(f"{UPSTREAM}/v1/messages", json=payload, timeout=300)
    data = r.json()
    return jsonify(sessionId=SESSION_ID, reply=data.get("reply", "")), r.status_code


@app.get("/v1/conversation")
def conversation():
    r = requests.get(f"{UPSTREAM}/v1/conversation", params={"sessionId": SESSION_ID}, timeout=30)
    return jsonify(r.json()), r.status_code


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, threaded=True)
