"""
app/routes/log_stream.py
=========================
GET /log-stream  — exposes recent structured Cloud Run logs as JSON.

Same file used by all three agents. Cloud Run sets K_SERVICE automatically.

Returns ALL agent log lines — both natural language sentences and JSON
payload lines — so the GUI team can use whatever they need.

Filters OUT HTTP access logs (GET/POST lines) — only agent logs returned.
"""

import os
import re
import json
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

router = APIRouter()

_PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "poc-z-in2300756")
_SERVICE = os.environ.get("K_SERVICE", "ran-engineer-test-v2")


# ── Parser ─────────────────────────────────────────────────────────────────────

def _parse(text: str) -> dict:
    """
    Parse one log line into a flat dict.

    [Step X] natural sentence | key=value | key=value
      → { step, event, key: value, ... }

    [Step X] JSON Payload | {...}
      → { step, event: "JSON Payload", raw: full line }

    [MCP] query_entity_domains → 5 rows
      → { prefix: "MCP", event: "query_entity_domains → 5 rows" }
    """
    result = {"raw": text}

    # Extract [Step X] or [Step X OUT] tag
    step_m = re.match(r"\[Step\s*([^\]]+)\]", text)
    if step_m:
        result["step"] = step_m.group(1).strip()

    # Extract [Prefix] for MCP/agent lines without a Step tag
    prefix_m = re.match(r"\[(\w+)\]", text)
    if prefix_m and not step_m:
        result["prefix"] = prefix_m.group(1)

    # Extract human-readable event description (text after ] up to first |)
    parts = text.split("|")
    label_m = re.search(r"\]\s*(.+)$", parts[0])
    if label_m:
        result["event"] = label_m.group(1).strip()

    # Extract key=value pairs from all pipe-separated segments after the first
    for part in parts[1:]:
        part = part.strip()
        if "=" in part:
            k, _, v = part.partition("=")
            key = k.strip().replace(" ", "_")
            result[key] = v.strip()

    return result


def _extract_text(entry) -> str | None:
    """
    Extract plain text from a Cloud Logging entry.

    Uses entry.to_api_repr() which returns the raw Cloud Logging API dict
    — works regardless of SDK version or entry type.

    Cloud Run logging.info() calls appear as textPayload in the API repr.
    Structured logs appear as jsonPayload.message.
    """
    try:
        # Most reliable: use the raw API representation
        api_repr = entry.to_api_repr()

        # 1. textPayload — where logging.info("[Step X]...") lands in Cloud Run
        if api_repr.get("textPayload"):
            return api_repr["textPayload"]

        # 2. jsonPayload — structured JSON logs
        json_payload = api_repr.get("jsonPayload", {})
        if json_payload:
            return (
                json_payload.get("message")
                or json_payload.get("textPayload")
                or json_payload.get("msg")
                or json.dumps(json_payload)
            )

        # 3. protoPayload — admin/audit logs (rarely agent logs)
        proto = api_repr.get("protoPayload", {})
        if proto:
            return proto.get("status", {}).get("message") or json.dumps(proto)

    except Exception:
        pass

    # 4. Fallback: try SDK attributes directly
    for attr in ("text_payload", "payload"):
        val = getattr(entry, attr, None)
        if val and isinstance(val, str):
            return val

    return None


# ── Cloud Logging query ────────────────────────────────────────────────────────

def _fetch_logs(
    event_id:      str | None,
    limit:         int,
    since_minutes: int,
) -> list[dict]:
    """
    Query Cloud Logging for recent agent log entries from this service.
    HTTP access logs (GET /openapi.json, POST /trigger_event) are excluded.
    """
    try:
        from google.cloud import logging_v2
    except ImportError:
        return []

    client    = logging_v2.Client(project=_PROJECT)
    since     = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
    since_str = since.strftime("%Y-%m-%dT%H:%M:%SZ")

    filter_parts = [
        'resource.type="cloud_run_revision"',
        f'resource.labels.service_name="{_SERVICE}"',
        f'timestamp>="{since_str}"',
        # Only fetch lines that contain our structured log prefixes
        # This excludes HTTP access logs at the Cloud Logging level
        '(textPayload=~"\\[Step" OR textPayload=~"\\[MCP\\]" OR '
        'textPayload=~"\\[ReflexAgent\\]" OR textPayload=~"\\[EngineerAgent\\]" OR '
        'textPayload=~"\\[ReflectionAgent\\]" OR textPayload=~"\\[GNN\\]" OR '
        'jsonPayload.message=~"\\[Step")',
    ]

    if event_id:
        filter_parts.append(
            f'(textPayload=~"{re.escape(event_id)}" OR '
            f'jsonPayload.message=~"{re.escape(event_id)}")'
        )

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
    for entry in reversed(entries):   # oldest-first for timeline display
        text = _extract_text(entry)
        if not text:
            continue
        text = text.strip()

        # Strip Python logging prefix (INFO:root:, WARNING:root:, ERROR:root:)
        for prefix in ("INFO:root:", "WARNING:root:", "ERROR:root:",
                       "INFO:uvicorn:", "INFO:"):
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
                break

        # Only keep agent log lines — skip anything that slipped through
        if not (
            text.startswith("[Step")
            or text.startswith("[MCP]")
            or text.startswith("[ReflexAgent]")
            or text.startswith("[EngineerAgent]")
            or text.startswith("[ReflectionAgent]")
            or text.startswith("[GNN]")
        ):
            continue

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
        description="Filter to one specific pipeline run by eventId. Optional.",
    ),
    limit: int = Query(
        default=50,
        ge=1,
        le=200,
        description="Max log entries to return. Default 50.",
    ),
    since_minutes: int = Query(
        default=60,
        ge=1,
        le=1440,
        description="How far back to look in minutes. Default 60.",
    ),
):
    """
    Returns recent agent log entries for this Cloud Run service.

    Every entry contains:
      raw       → full original log line (natural sentence or JSON payload)
      step      → pipeline step e.g. "2B", "4A", "4B", "5", "5 OUT", "6", "7", "10"
      event     → clean display text (natural sentence or payload label)
      timestamp → ISO timestamp
      severity  → INFO / WARNING / ERROR
      + any key=value fields extracted from the log line
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