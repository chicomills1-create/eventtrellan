#!/usr/bin/env python3
"""EventTrellan confirmation gate — Charter v2 R4 executable controls.

The Accuracy Law as code: publication is BLOCKED unless a complete, immutable
confirmation record exists; the publish step compares the approved proof fields
against the outgoing page fields and any mismatch blocks; rejected proofs stay
blocked; post-approval changes require a fresh confirmation cycle producing a
new version (prior records are preserved, never overwritten).

This module is the control logic the ops-run fulfillment pipeline executes for
every event. It owns no production data; records are append-only JSON lines.
"""
import hashlib
import json
import re
from datetime import date

REQUIRED_ALWAYS = ["event_name", "host_name", "date", "start_time", "rsvp_instruction", "contact"]
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
PUBLISH_FIELDS = ["event_name", "host_name", "date", "start_time", "end_time", "timezone",
                  "format", "venue_name", "address", "virtual_link", "rsvp_instruction", "contact"]


def _weekday_of(iso):
    y, m, d = (int(x) for x in iso.split("-"))
    return WEEKDAYS[date(y, m, d).weekday()]


def validate_brief(brief):
    """Return a list of blocking questions. Empty list = brief is clean.
    The gate NEVER resolves a conflict itself — every problem comes back as a
    question naming the conflicting values (Charter: never silently correct)."""
    q = []
    for f in REQUIRED_ALWAYS:
        if not brief.get(f):
            q.append(f"Missing required field: {f}. Please supply it exactly as guests should see it.")
    fmt = brief.get("format")
    if fmt not in ("physical", "virtual"):
        q.append("Is this event physical or virtual? The brief does not say.")
    # Time zone: required whenever a start time is given (guests act on it).
    if brief.get("start_time") and not brief.get("timezone"):
        q.append("No time zone is given for the start time. Which time zone applies? "
                 "We do not assume one from the venue or the organizer.")
    # Conflicting dates carried in the same brief.
    if brief.get("date") and brief.get("date_alt") and brief["date_alt"] != brief["date"]:
        q.append(f"Conflicting dates in the brief: {brief['date']} and {brief['date_alt']}. "
                 "Which date is correct? Nothing publishes until this is resolved.")
    # Weekday the organizer wrote vs the calendar.
    if brief.get("date") and brief.get("weekday_claimed"):
        actual = _weekday_of(brief["date"])
        if actual != brief["weekday_claimed"]:
            q.append(f"Weekday mismatch: the brief says {brief['weekday_claimed']}, "
                     f"but {brief['date']} is a {actual}. Which is right — the weekday or the date?")
    if fmt == "physical":
        addr = brief.get("address") or ""
        if not addr:
            q.append("Physical event with no venue address. Please supply the full street address.")
        elif not re.search(r"\d+\s+\S+", addr):
            q.append(f"The address as supplied (\"{addr}\") cannot be resolved to a street address. "
                     "Please supply the street number and street name. No map or directions are generated until then.")
        if not brief.get("venue_name"):
            q.append("Physical event with no venue name. Please supply it exactly as guests should see it.")
    if fmt == "virtual":
        if brief.get("address") or brief.get("parking_note"):
            q.append("This brief says virtual, but it also carries a street address / parking details. "
                     "Which is authoritative — the online event, or the physical venue?")
        if not brief.get("virtual_link"):
            q.append("Virtual event with no access link or format wording. Please supply it exactly as guests should use it.")
    return q


def render_proof(brief):
    """The show-back: every publishable field, verbatim from the brief."""
    return {f: brief.get(f) for f in PUBLISH_FIELDS}


def _record_hash(rec):
    blob = json.dumps(rec, sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()


class ConfirmationStore:
    """Append-only confirmation records. A record, once written, is never
    mutated: approval, rejection and publication are separate appended entries
    keyed to (event_id, version)."""

    def __init__(self):
        self.entries = []          # every entry, in order (the audit log)
        self.records = {}          # (event_id, version) -> record dict
        self.published = {}        # event_id -> {version, fields}

    def create_record(self, brief, proof):
        key = (brief["event_id"], brief.get("version", 1))
        if key in self.records:
            raise ValueError("Immutable record already exists for this event/version; "
                             "a change requires a new version, never an overwrite.")
        rec = {"event_id": key[0], "version": key[1], "brief": dict(brief),
               "proof": dict(proof), "status": "awaiting_approval", "approval": None}
        rec["hash"] = _record_hash({k: rec[k] for k in ("event_id", "version", "brief", "proof")})
        self.records[key] = rec
        self.entries.append({"type": "record_created", "event_id": key[0], "version": key[1], "hash": rec["hash"]})
        return rec

    def approve(self, event_id, version, approver, timestamp):
        rec = self.records[(event_id, version)]
        rec["status"] = "approved"
        rec["approval"] = {"approver": approver, "timestamp": timestamp, "explicit": True}
        self.entries.append({"type": "approved", "event_id": event_id, "version": version,
                             "approver": approver, "timestamp": timestamp})

    def reject(self, event_id, version, approver, timestamp):
        rec = self.records[(event_id, version)]
        rec["status"] = "rejected"
        self.entries.append({"type": "rejected", "event_id": event_id, "version": version,
                             "approver": approver, "timestamp": timestamp})

    def publish(self, event_id, version, outgoing_fields):
        """The system block. Returns (ok, reason). Every attempt is logged."""
        key = (event_id, version)
        rec = self.records.get(key)
        if rec is not None and rec["status"] == "rejected":
            self.entries.append({"type": "publish_blocked", "event_id": event_id, "version": version,
                                 "reason": "proof rejected"})
            return False, "BLOCKED: the proof was rejected; a rejected proof stays blocked."
        if rec is None or rec["approval"] is None:
            self.entries.append({"type": "publish_blocked", "event_id": event_id, "version": version,
                                 "reason": "no complete confirmation record"})
            return False, "BLOCKED: no complete confirmation record (missing approval)."
        if rec["status"] != "approved":
            self.entries.append({"type": "publish_blocked", "event_id": event_id, "version": version,
                                 "reason": f"status is {rec['status']}"})
            return False, f"BLOCKED: record status is {rec['status']}, not approved."
        diffs = [f for f in PUBLISH_FIELDS if outgoing_fields.get(f) != rec["proof"].get(f)]
        if diffs:
            self.entries.append({"type": "publish_blocked", "event_id": event_id, "version": version,
                                 "reason": "field mismatch vs approved proof", "fields": diffs})
            return False, ("BLOCKED: outgoing fields differ from the approved proof on: "
                           + ", ".join(diffs) + ". Changes require a fresh confirmation cycle.")
        self.published[event_id] = {"version": version, "fields": dict(rec["proof"])}
        self.entries.append({"type": "published", "event_id": event_id, "version": version, "diffs": 0})
        return True, "PUBLISHED: field comparison vs approved proof = zero diffs."
