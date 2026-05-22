"""
app/routes/log_stream.py
=========================
GET /log-stream  — exposes recent structured Cloud Run logs as JSON.

How it works end-to-end:
  1. Your agent tools.py calls logging.info("[Step X] ... | key=value | ...")
  2. Cloud Run captures those lines in Cloud Logging automatically.
  3. The GUI calls GET /log-stream?event_id=EV-xxx every 3 seconds.
  4. This endpoint queries Cloud Logging API for this service's recent logs,
     parses each line into {step, event, key: value, ...}, and returns JSON.
  5. The GUI parser reads step + key=value fields to drive the status cards.

Natural language log lines are parsed the same way — the [Step X] prefix
is the classifier key, and key=value pairs at the end of the line provide
the structured fields for the GUI pills display.

Prerequisites:
  pip install google-cloud-logging
  In GCP IAM: add roles/logging.viewer to the Cloud Run service account.

Registration in your FastAPI app:
  from app.routes.log_stream import router as log_router
  app.include_router(log_router)
"""

import os
import re
import json
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

router = APIRouter()

_PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT")
_SERVICE = os.environ.get("K_SERVICE", "ran-engineer-test-v2")


# ── Parser ─────────────────────────────────────────────────────────────────────

def _parse(text: str) -> dict:
    """
    Parse one structured log line into a flat dict.
    Handles all log line formats produced by the three agents:
    """
    result = {"raw": text}

    # [Step X] or [Step X OUT] — the primary classifier for the GUI
    step_m = re.match(r"\[Step\s*([\w\s]+)\]", text)
    if step_m:
        result["step"] = step_m.group(1).strip()

    # [Word] prefix for MCP/agent lines that have no Step tag
    prefix_m = re.match(r"\[(\w+)\]", text)
    if prefix_m and not step_m:
        result["prefix"] = prefix_m.group(1)

    # Human-readable event description — text inside [...] up to first |
    parts = text.split("|")
    label_m = re.search(r"\]\s*(.+)$", parts[0])
    if label_m:
        result["event"] = label_m.group(1).strip()

    # key=value pairs from all pipe-separated segments after the first
    for part in parts[1:]:
        part = part.strip()
        if "=" in part:
            k, _, v = part.partition("=")
            key = k.strip().replace(" ", "_")
            result[key] = v.strip()

    return result


# ── Cloud Logging query ────────────────────────────────────────────────────────

def _fetch_logs(
    event_id:      str | None,
    limit:         int,
    since_minutes: int,
) -> list[dict]:
    """
    Query Cloud Logging for recent log entries from this service only.

    Filter chain:
      resource.type = cloud_run_revision        → Cloud Run logs only
      resource.labels.service_name = K_SERVICE  → this service only
      severity >= INFO                           → skip DEBUG
      timestamp >= now - since_minutes           → recency window
      textPayload =~ event_id                    → optional per-run filter
    """
    try:
        from google.cloud import logging_v2
    except ImportError:
        # google-cloud-logging not installed — return empty (local dev without GCP)
        return []

    client    = logging_v2.Client(project=_PROJECT)
    since     = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
    since_str = since.strftime("%Y-%m-%dT%H:%M:%SZ")

    filter_parts = [
        'resource.type="cloud_run_revision"',
        f'resource.labels.service_name="{_SERVICE}"',
        "severity>=INFO",
        f'timestamp>="{since_str}"',
    ]
    if event_id:
        # Filter to lines that contain the eventId — lets the GUI track one run
        filter_parts.append(f'textPayload=~"{re.escape(event_id)}"')

    try:
        entries = list(
            client.list_entries(
                filter_="\n".join(filter_parts),
                order_by=logging_v2.DESCENDING,
                max_results=limit,
                page_size=min(limit, 100),
            )
        )
    except Exception as e:
        return [{"raw": f"[log_stream] Cloud Logging query error: {e}", "step": None}]

    results = []
    for entry in reversed(entries):   # oldest-first for timeline display in GUI
        payload = entry.payload
        if isinstance(payload, str):
            text = payload
        elif isinstance(payload, dict):
            # Structured JSON log — try to extract textPayload or message field
            text = (
                payload.get("textPayload")
                or payload.get("message")
                or json.dumps(payload)
            )
        else:
            text = str(payload)

        parsed = _parse(text)
        parsed["timestamp"] = entry.timestamp.isoformat() if entry.timestamp else ""
        parsed["severity"]  = str(entry.severity) if entry.severity else "INFO"
        parsed["insert_id"] = entry.insert_id or ""
        results.append(parsed)

    return results


# ── Route ──────────────────────────────────────────────────────────────────────

@router.get("/log-stream")
async def log_stream(
    event_id: str | None = Query(
        default=None,
        description=(
            "Optional eventId string to filter logs for one specific pipeline run. "
            "E.g. EV-dekfn_efjnf_gege. Matches any log line containing this string."
        ),
    ),
    limit: int = Query(
        default=40,
        ge=1,
        le=200,
        description="Max number of log entries to return (oldest-first within window).",
    ),
    since_minutes: int = Query(
        default=10,
        ge=1,
        le=60,
        description="How far back to look in Cloud Logging (minutes). Default: 10.",
    ),
):
    """
    Returns recent structured log entries for this Cloud Run service.

    The GUI polls this endpoint every 3 seconds and uses the response to:
      - Drive step-progress dots on the agent status cards
      - Populate key=value pills (domain, priority, impact_score, etc.)
      - Render the MCP sub-block (connection source, tool rows, domain badge)
      - Feed the live log tail at the bottom

    """
    logs = _fetch_logs(
        event_id=event_id,
        limit=limit,
        since_minutes=since_minutes,
    )
    return JSONResponse({
        "service": _SERVICE,
        "project": _PROJECT,
        "count":   len(logs),
        "logs":    logs,
    })
