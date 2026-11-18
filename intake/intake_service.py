#!/usr/bin/env python3
"""EventTrellan brief intake service — server-side capture per the factory
standard: every submission is STORED as a record on submit and answered with
an on-page confirmation carrying a reference.

- POST /brief    — an organizer's event brief. Runs the Charter v2 R4
  confirmation gate (confirmation_gate.py, vendored byte-identical from the
  seat pipeline): validation questions come back as questions (never silently
  resolved); a clean brief creates an immutable confirmation record in
  'awaiting_approval' — publication stays system-blocked until the organizer
  explicitly approves the proof shown back (the ops-run cycle).
- POST /interest — early-access capture (name + email), stored with a reference.
- GET /record/<ref> — ops read-back, requires the X-Ops-Token header
  (OPS_TOKEN env var). No token set = read-back disabled.

Storage: append-only JSONL files under DATA_DIR (env, default ./data).
Stdlib only — no dependencies.
"""
import json
import os
import re
import secrets
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from confirmation_gate import ConfirmationStore, render_proof, validate_brief

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
OPS_TOKEN = os.environ.get("OPS_TOKEN", "")
ALLOWED_ORIGINS = {
    "https://eventtrellan.com",
    "https://www.eventtrellan.com",
    "https://eventtrellan.onrender.com",
}

store = ConfirmationStore()
_submissions = {}   # reference -> submission dict (briefs and interest)


def _path(name):
    return os.path.join(DATA_DIR, name)


def _append(name, obj):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(_path(name), "a", encoding="utf-8") as f:
        f.write(json.dumps(obj) + "\n")


def _load():
    """Rebuild in-memory state from the append-only logs (restart-safe)."""
    sub_file = _path("submissions.jsonl")
    if os.path.exists(sub_file):
        with open(sub_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    s = json.loads(line)
                    _submissions[s["reference"]] = s
    audit_file = _path("gate-audit.jsonl")
    if os.path.exists(audit_file):
        with open(audit_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                e = json.loads(line)
                if e.get("type") == "record_created" and "record" in e:
                    rec = e["record"]
                    store.records[(rec["event_id"], rec["version"])] = rec
                store.entries.append(e)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _new_ref(prefix):
    return f"{prefix}-{datetime.now(timezone.utc):%Y%m%d}-{secrets.token_hex(3).upper()}"


def handle_brief(body):
    brief = {k: (v.strip() if isinstance(v, str) else v) for k, v in body.items()}
    questions = validate_brief(brief)
    ref = _new_ref("ET")
    brief["event_id"] = ref
    brief.setdefault("version", 1)
    submission = {
        "reference": ref, "kind": "brief", "received_at": _now(),
        "status": "needs_answers" if questions else "awaiting_approval",
        "questions": questions, "brief": brief,
        "organizer_email": brief.get("contact"),
        "tier": brief.get("tier"),
    }
    if not questions:
        rec = store.create_record(brief, render_proof(brief))
        submission["proof"] = rec["proof"]
        submission["record_hash"] = rec["hash"]
        _append("gate-audit.jsonl", {"type": "record_created", "event_id": ref,
                                     "version": rec["version"], "hash": rec["hash"],
                                     "record": rec})
    _append("submissions.jsonl", submission)
    _submissions[ref] = submission
    return (422 if questions else 201), {
        "reference": ref, "status": submission["status"],
        "questions": questions, "proof": submission.get("proof"),
        "message": ("We stored your brief. Before anything is built, please answer "
                    "the questions below — we never guess at event details."
                    if questions else
                    "Brief received and stored. Your proof is shown below — nothing "
                    "publishes until you approve it exactly as shown."),
    }


def handle_interest(body):
    name = (body.get("name") or "").strip()
    email = (body.get("email") or "").strip()
    if not name or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return 422, {"error": "A name and a valid email are required."}
    ref = _new_ref("INT")
    submission = {"reference": ref, "kind": "interest", "received_at": _now(),
                  "status": "stored", "name": name, "email": email,
                  "note": (body.get("note") or "").strip()}
    _append("submissions.jsonl", submission)
    _submissions[ref] = submission
    return 201, {"reference": ref, "status": "stored",
                 "message": "You're on the list. Reference: " + ref}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, obj):
        payload = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        origin = self.headers.get("Origin", "")
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_OPTIONS(self):
        self.send_response(204)
        origin = self.headers.get("Origin", "")
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Ops-Token")
        self.end_headers()

    def do_GET(self):
        if self.path == "/":
            return self._send(200, {"ok": True, "service": "eventtrellan-intake"})
        m = re.match(r"^/record/([A-Z]+-\d{8}-[0-9A-F]{6})$", self.path)
        if m:
            if not OPS_TOKEN or self.headers.get("X-Ops-Token") != OPS_TOKEN:
                return self._send(403, {"error": "ops token required"})
            rec = _submissions.get(m.group(1))
            if rec is None:
                return self._send(404, {"error": "no such reference"})
            return self._send(200, rec)
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._send(400, {"error": "invalid JSON body"})
        if self.path == "/brief":
            code, obj = handle_brief(body)
            return self._send(code, obj)
        if self.path == "/interest":
            code, obj = handle_interest(body)
            return self._send(code, obj)
        return self._send(404, {"error": "not found"})


if __name__ == "__main__":
    _load()
    port = int(os.environ.get("PORT", "8080"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
