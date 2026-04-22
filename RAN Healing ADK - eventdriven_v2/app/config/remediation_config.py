"""
app/config/remediation_config.py
==================================
Single source of truth for ALL domain knowledge used by the agents.

tools.py files import from here — they contain ZERO hardcoded values.
mock_api.py files import from here — scenario names come from here too.

Alignment with synthetic data pipeline
---------------------------------------
Signal names     → features.py  SIGNAL_TYPES + SIGNAL_SOURCE
Z-score fields   → anomaly.py   Z_SCORE_MAP keys
Priority gates   → anomaly.py   GATE_A/B/C thresholds + COMPOSITE_ANOMALY_THRESHOLD
Node naming      → topology.py  eNB-SYN-*, gNB-SYN-*, HSS-SYN-*, AGG-SYN-*, CSR-SYN-*
KPI names        → graph_tables.py  kpi_profiles keys
Scenario names   → scenarios/*.yaml  (UC1 tilt, UC2 HSS, UC3 fiber cut)
Validation       → execution_mock_output.py  postActionZScore / expectedBaseline
"""

from __future__ import annotations

# ── Priority thresholds ────────────────────────────────────────────────────────
# Aligned with anomaly.py: COMPOSITE_ANOMALY_THRESHOLD = 3.0
# GATE_A_THRESHOLD = 5.0 (weighted_severity_impact)
# We use Z-score from GNN (derived from anomaly engine composite score)
PRIORITY_THRESHOLDS: dict[str, float] = {
    "P1": 8.0,   # CRITICAL  — z >= 8.0  (triggers GATE_A in anomaly.py)
    "P2": 5.0,   # HIGH      — z >= 5.0
    "P3": 3.0,   # MEDIUM    — z >= 3.0  (COMPOSITE_ANOMALY_THRESHOLD)
    # z < 3.0 → NORMAL, no healing action triggered
}

# ── Baseline Z-score ───────────────────────────────────────────────────────────
# Aligned with scorecard.py: 30-day rolling mean + stddev per hex bin
# post-action z must fall at or below this for RESOLVED verdict
BASELINE_Z_SCORE: float = 2.0

# ── Domain node patterns ───────────────────────────────────────────────────────
# Aligned with topology.py naming conventions:
#   eNodeB  → eNB-SYN-NNN    gNodeB  → gNB-SYN-NNN
#   MME     → MME-SYN-NN     HSS     → HSS-SYN-01
#   AMF     → AMF-SYN-NN     UPF/SMF → UPF-SYN-01 / SMF-SYN-01
#   CSR     → CSR-SYN-NNN    AGG     → AGG-SYN-NN
# GNN inference provider uses short names (RAN_CELL_101 etc.) that map to these.
DOMAIN_NODE_PATTERNS: dict[str, list[str]] = {
    "RAN":       ["RAN", "ENB", "GNB", "ENODEB", "GNODEB", "CELL", "SECTOR"],
    "CORE":      ["HSS", "CORE", "MME", "AMF", "UPF", "SMF"],
    "TRANSPORT": ["TRANSPORT", "AGG", "CSR", "FIBER", "BACKHAUL", "LINK"],
}

# ── GNN node name → synth_gen EID ────────────────────────────────────────────
# GNN provider uses short names; downstream needs full EIDs for CSV lookup
GNN_NODE_TO_EID: dict[str, str] = {
    "RAN_CELL_101":      "eNB-SYN-001",
    "RAN_CELL_102":      "eNB-SYN-002",
    "RAN_CELL_103":      "eNB-SYN-003",
    "HSS_CORE_01":       "HSS-SYN-01",
    "HSS_CORE_02":       "HSS-SYN-01",   # single HSS in default topology
    "TRANSPORT_LINK_01": "CSR-SYN-001",
    "TRANSPORT_LINK_02": "AGG-SYN-01",
}

# ── Healing actions ────────────────────────────────────────────────────────────
# Keyed by root_cause string from InvestigationAgent confirmed RCA output.
# synth_signal: the ml_* feature names from features.py that fire for this cause
# ranked_healing_actions: ordered best-first (ExecutionAgent picks top)
# tmf915_parameter_bounds: safe envelope; current_value filled at runtime
# expected_recovery_minutes: used by ValidationAgent to set re-check window
HEALING_ACTIONS: dict[str, dict] = {

    # ── UC1: RAN Antenna Tilt Push ─────────────────────────────────────────
    # Synth signals: ml_ebh (EBH degradation), ml_wb (wideband)
    # PERFORMANCE.csv: dl_throughput_mbps drops, handover_success_rate drops
    # Topology: eNB-SYN-* / gNB-SYN-* nodes
    "BAD_ANTENNA_TILT_PUSH": {
        "domain": "RAN",
        "synth_signal": ["ml_ebh", "ml_wb"],                 # features.py SIGNAL_TYPES
        "performance_kpis": ["dl_throughput_mbps", "handover_success_rate"],
        "ranked_healing_actions": [
            "ROLLBACK_TILT_TO_BASELINE",
            "REDUCE_TILT_BY_2_DEGREES",
            "REBUILD_NEIGHBOR_RELATIONS",
        ],
        "tmf915_parameter_bounds": {
            "parameter":       "antenna_tilt_degrees",
            "current_value":   None,          # filled at runtime from RCA payload
            "baseline_value":  3.0,           # nominal tilt (degrees) from topology YAML
            "min_delta":       -5.0,          # max downward correction
            "max_delta":        5.0,          # max upward correction
            "safe_profile":    "baseline_profile_v1",
            "rollback_action": "SET_TILT_TO_BASELINE",
            "unit":            "degrees",
        },
        "expected_recovery_minutes": 10,
    },

    # ── UC2: Core — HSS Stale Session Loop ────────────────────────────────
    # Synth signals: ml_nmc (NMC alert), ml_fault
    # PERFORMANCE.csv: attach_success_rate drops, active_session_count spikes
    # NG1: initial_registration_failure_rate spikes (from ng1.py CORE_KPIS)
    # Topology: HSS-SYN-01 → MME-SYN-* cascade
    "HSS_STALE_SESSION_LOOP": {
        "domain": "CORE",
        "synth_signal": ["ml_nmc", "ml_fault"],
        "performance_kpis": ["attach_success_rate", "active_session_count"],
        "ranked_healing_actions": [
            "CLEAR_STALE_HSS_SESSIONS",
            "SHIFT_TRAFFIC_TO_SECONDARY_HSS",
            "REDUCE_REATTACH_RATE_LIMIT",
        ],
        "tmf915_parameter_bounds": {
            "parameter":       "stale_sessions",
            "max_clear":       10000,          # safety cap per clear command
            "target_capacity_pct": 80,         # restore to <80% per TMF921 intent
            "reattach_limit":  50,             # max re-attach attempts per minute
            "safe_profile":    "clear_looped_503_sessions",
            "rollback_action": "RESTORE_DEFAULT_SESSION_LIMITS",
            "unit":            "sessions",
        },
        "expected_recovery_minutes": 15,
    },

    # alias — investigation mock sometimes produces this label
    "HSS_SATURATION": {
        "domain": "CORE",
        "synth_signal": ["ml_nmc"],
        "performance_kpis": ["attach_success_rate", "cpu_utilization_pct"],
        "ranked_healing_actions": [
            "CLEAR_STALE_HSS_SESSIONS",
            "SHIFT_TRAFFIC_TO_SECONDARY_HSS",
            "REDUCE_REATTACH_RATE_LIMIT",
        ],
        "tmf915_parameter_bounds": {
            "parameter":           "stale_sessions",
            "max_clear":           10000,
            "target_capacity_pct": 80,
            "reattach_limit":      50,
            "safe_profile":        "clear_looped_503_sessions",
            "rollback_action":     "RESTORE_DEFAULT_SESSION_LIMITS",
            "unit":                "sessions",
        },
        "expected_recovery_minutes": 15,
    },

    # ── UC3: Transport — Backhaul Fiber Cut ───────────────────────────────
    # Synth signals: ml_ebh (EBH), ml_fault
    # EDGE_EntityToEdgePerformance.csv: link_utilization_pct → 100%, latency → 320ms
    # Topology: AGG-SYN-NN → CSR-SYN-NNN → eNodeB/gNodeB cascade
    "FIBER_CUT": {
        "domain": "TRANSPORT",
        "synth_signal": ["ml_ebh", "ml_fault"],
        "performance_kpis": ["link_utilization_pct", "latency_ms", "packet_loss_rate"],
        "ranked_healing_actions": [
            "FAILOVER_TO_REDUNDANT_FIBER_PATH",
            "REROUTE_TRAFFIC_VIA_BACKUP_AGG",
            "ISOLATE_FAILED_AGG_NODE",
        ],
        "tmf915_parameter_bounds": {
            "parameter":       "transport_path",
            "primary_path":    "AGG_PRIMARY",
            "backup_path":     "AGG_REDUNDANT",
            "safe_profile":    "backup_path_v1",
            "rollback_action": "RESTORE_PRIMARY_FIBER_PATH",
            "unit":            "path_id",
        },
        "expected_recovery_minutes": 20,
    },

    # alias
    "PATH_DEGRADATION": {
        "domain": "TRANSPORT",
        "synth_signal": ["ml_fault", "ml_wb"],
        "performance_kpis": ["link_utilization_pct", "latency_ms"],
        "ranked_healing_actions": [
            "FAILOVER_TO_BACKUP_PATH",
            "RESET_TRANSPORT_PATH",
            "REROUTE_TRAFFIC_VIA_BACKUP_AGG",
        ],
        "tmf915_parameter_bounds": {
            "parameter":       "transport_path",
            "safe_profile":    "backup_path_v1",
            "rollback_action": "RESTORE_PRIMARY_PATH",
            "unit":            "path_id",
        },
        "expected_recovery_minutes": 20,
    },

    # ── Cross-domain (UC1 + UC2 combined) ─────────────────────────────────
    "MULTI_DOMAIN_SERVICE_DEGRADATION": {
        "domain": "CROSS_DOMAIN",
        "synth_signal": ["ml_ebh", "ml_nmc", "ml_fault", "ml_wb"],
        "performance_kpis": [
            "dl_throughput_mbps", "attach_success_rate",
            "link_utilization_pct",
        ],
        "ranked_healing_actions": [
            "ROLLBACK_TILT_TO_BASELINE",
            "CLEAR_STALE_HSS_SESSIONS",
            "FAILOVER_TO_BACKUP_PATH",
        ],
        "tmf915_parameter_bounds": {
            "parameter":       "multi_domain",
            "safe_profile":    "cross_domain_v1",
            "rollback_action": "RESTORE_ALL_BASELINES",
            "unit":            "composite",
        },
        "expected_recovery_minutes": 25,
    },
}

# ── Validation config ──────────────────────────────────────────────────────────
# Aligned with execution_mock_output.py postActionValidation field names
VALIDATION_CONFIG: dict = {
    "resolved_z_threshold":   BASELINE_Z_SCORE,        # post_z <= 2.0 → RESOLVED
    "topology_stable_states": [                         # topologySnapshot values
        "STABLE_GRAPH_V2",
        "STABLE_GRAPH_CORE_V2",
        "STABLE_GRAPH_TRANSPORT_V2",
    ],
    "kpi_normal_states":      ["KEI_NORMAL"],           # serviceKPI values
    "business_normal_states": ["UTILITY_SCORE_NORMAL"], # businessUtility values
    "max_retrigger_attempts": 3,
}

# ── Scenario → mock scenario key mapping ──────────────────────────────────────
# Maps domain/root-cause to the scenario key used by mock providers.
# mock_api.py files read this so they never hard-code scenario strings.
DOMAIN_TO_INVESTIGATION_SCENARIO: dict[str, str] = {
    "CROSS_DOMAIN": "UC_MULTI_DOMAIN_RCA",
    "CORE":         "UC2_CORE_CONGESTION",
    "TRANSPORT":    "UC3_TRANSPORT_FIBER_CUT",
    "RAN":          "UC_SINGLE_RAN",
}

DOMAIN_TO_EXECUTION_SCENARIO: dict[str, str] = {
    "CROSS_DOMAIN": "UC1_SUCCESSFUL_REMEDIATION",
    "RAN":          "UC1_SUCCESSFUL_REMEDIATION",
    "CORE":         "UC2_CORE_REMEDIATION",
    "TRANSPORT":    "UC3_TRANSPORT_REMEDIATION",
}

# ── Helper functions ───────────────────────────────────────────────────────────

def infer_domain(nodes: list[str]) -> str:
    """
    Derive domain from anomalous subgraph node names.
    Aligned with topology.py naming: eNB-SYN-*, gNB-*, HSS-*, AGG-*, CSR-*
    Also handles GNN short names: RAN_CELL_*, HSS_CORE_*, TRANSPORT_LINK_*
    """
    nodes_upper = [n.upper() for n in nodes]
    hits = {
        domain: any(
            any(pattern in node for pattern in patterns)
            for node in nodes_upper
        )
        for domain, patterns in DOMAIN_NODE_PATTERNS.items()
    }
    active = [d for d, hit in hits.items() if hit]
    if len(active) > 1:
        return "CROSS_DOMAIN"
    return active[0] if active else "UNKNOWN"


def get_priority_flag(z_score: float) -> str:
    """
    Map Z-score → priority flag.
    Aligned with anomaly.py COMPOSITE_ANOMALY_THRESHOLD and gate logic.
    Returns "NORMAL" if below P3 threshold — MonitoringAgent skips action.
    """
    if z_score >= PRIORITY_THRESHOLDS["P1"]:
        return "P1"
    if z_score >= PRIORITY_THRESHOLDS["P2"]:
        return "P2"
    if z_score >= PRIORITY_THRESHOLDS["P3"]:
        return "P3"
    return "NORMAL"


def get_healing_actions(root_cause: str) -> dict:
    """
    Return healing action definition for a root cause.
    Falls back gracefully if root cause is not in config.
    """
    return HEALING_ACTIONS.get(root_cause, {
        "domain":                  "UNKNOWN",
        "synth_signal":            [],
        "performance_kpis":        [],
        "ranked_healing_actions":  ["MANUAL_INVESTIGATION_REQUIRED"],
        "tmf915_parameter_bounds": {},
        "expected_recovery_minutes": 60,
    })


def get_tilt_correction(current_tilt: float, baseline_tilt: float | None = None) -> dict:
    """
    Compute exact antenna tilt correction.
    Called by SolutionPlanningAgent for BAD_ANTENNA_TILT_PUSH.

    baseline_tilt defaults to the value in HEALING_ACTIONS config
    (3.0 degrees — the nominal tilt from synth topology YAML).
    current_tilt comes from the RCA payload (observed by InvestigationAgent).
    """
    bounds = HEALING_ACTIONS["BAD_ANTENNA_TILT_PUSH"]["tmf915_parameter_bounds"]
    if baseline_tilt is None:
        baseline_tilt = bounds["baseline_value"]

    raw_delta = baseline_tilt - current_tilt
    # Clamp to safe operating bounds
    clamped_delta = max(bounds["min_delta"], min(bounds["max_delta"], raw_delta))

    return {
        "current_tilt_degrees":  current_tilt,
        "baseline_tilt_degrees": baseline_tilt,
        "correction_delta":      round(clamped_delta, 2),
        "target_tilt_degrees":   round(current_tilt + clamped_delta, 2),
        "action":                f"SET_TILT_TO_{baseline_tilt}_DEGREES",
        "within_safe_bounds":    (bounds["min_delta"] <= raw_delta <= bounds["max_delta"]),
        "clamped":               raw_delta != clamped_delta,
    }


def get_investigation_scenario(domain: str) -> str:
    """Return investigation mock scenario key for a given domain."""
    return DOMAIN_TO_INVESTIGATION_SCENARIO.get(domain, "UC_SINGLE_RAN")


def get_execution_scenario(domains: set[str]) -> str:
    """
    Return execution mock scenario key for the set of domains in a plan.
    Highest-specificity match wins: CROSS_DOMAIN > single domain.
    """
    if len(domains) > 1 or "CROSS_DOMAIN" in domains:
        return DOMAIN_TO_EXECUTION_SCENARIO["CROSS_DOMAIN"]
    domain = next(iter(domains), "RAN")
    return DOMAIN_TO_EXECUTION_SCENARIO.get(domain, "UC1_SUCCESSFUL_REMEDIATION")
