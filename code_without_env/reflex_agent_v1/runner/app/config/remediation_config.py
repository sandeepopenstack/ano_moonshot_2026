"""
app/config/remediation_config.py
==================================
Single source of truth for ALL domain knowledge.

"""

from __future__ import annotations


# ── Composite anomaly impact score thresholds ──────────────────────────────────
COMPOSITE_ANOMALY_IMPACT_SCORE_THRESHOLDS: dict[str, float] = {
    "P1": 8.0,
    "P2": 5.0,
    "P3": 3.0,
}

PRIORITY_THRESHOLDS        = COMPOSITE_ANOMALY_IMPACT_SCORE_THRESHOLDS
COMPOSITE_SCORE_THRESHOLDS = COMPOSITE_ANOMALY_IMPACT_SCORE_THRESHOLDS

BASELINE_Z_SCORE: float = 2.0

# ── GNN businessPriority → internal flag ──────────────────────────────────────
GNN_BUSINESS_PRIORITY_TO_FLAG: dict[str, str] = {
    "CRITICAL": "P1",
    "HIGH":     "P2",
    "MEDIUM":   "P3",
    "LOW":      "P3",
}

# ── Priority flag → external priority string ───────────────────────────────────
PRIORITY_FLAG_TO_EXTERNAL: dict[str, str] = {
    "P1": "CRITICAL",
    "P2": "HIGH",
    "P3": "MEDIUM",
}

# ── Domain node patterns (telecom knowledge — not entity IDs) ──────────────────
DOMAIN_NODE_PATTERNS: dict[str, list[str]] = {
    "RAN":       ["RAN", "ENB", "GNB", "ENODEB", "GNODEB", "CELL", "SECTOR", "NR"],
    "CORE":      ["HSS", "CORE", "MME", "AMF", "UPF", "SMF"],
    "TRANSPORT": ["TRANSPORT", "AGG", "CSR", "FIBER", "BACKHAUL", "LINK"],
}

# ── Root cause string → HEALING_ACTIONS key ───────────────────────────────────
# Accepts both YAML root_cause values and Detective Agent strings (case-insensitive).
ROOT_CAUSE_MAP: dict[str, str] = {
    # RAN
    "antenna_tilt_misconfiguration":  "BAD_ANTENNA_TILT_PUSH",
    "ran_parameter_push":             "BAD_ANTENNA_TILT_PUSH",
    "bad_antenna_tilt_push":          "BAD_ANTENNA_TILT_PUSH",
    # CORE
    "hss_session_saturation":         "HSS_STALE_SESSION_LOOP",
    "hss_stale_sessions":             "HSS_STALE_SESSION_LOOP",
    "hss_overload":                   "HSS_SATURATION",
    "hss_stale_session_loop":         "HSS_STALE_SESSION_LOOP",
    "hss_session_table_overflow":     "HSS_STALE_SESSION_LOOP",
    "hss_saturation":                 "HSS_SATURATION",
    "hss_subscriber_db_saturation":   "HSS_SATURATION",     # UC2 YAML root_cause
    # TRANSPORT
    "fiber_cut":                      "FIBER_CUT",
    "physical_fiber_cut_backhaul":    "FIBER_CUT",           # UC3 YAML root_cause
    "backhaul_degradation":           "PATH_DEGRADATION",
    "transport_path_failure":         "FIBER_CUT",
    "transport_path_degradation":     "PATH_DEGRADATION",
    "path_degradation":               "PATH_DEGRADATION",
    # CROSS
    "multi_domain_service_degradation": "MULTI_DOMAIN_SERVICE_DEGRADATION",
    "multi_domain_degradation":         "MULTI_DOMAIN_SERVICE_DEGRADATION",
}

# ── Healing actions ────────────────────────────────────────────────────────────
# yaml_action: matches scenario YAML resolution.action = ACTIVATIONS.csv action field
#              This is what the ExecutorAgent actually executes.
# ranked_healing_actions: ordered list of specific sub-actions within the healing
#                         Used as branch_a_actions when Detective sends no suggestions.
# synth_signal: which ml_* signals from features.csv are elevated for this scenario.
#               Aligns with signal_injection in scenario YAML.
# performance_kpis: which PERFORMANCE.csv KPIs to monitor for recovery.

HEALING_ACTIONS: dict[str, dict] = {

    "BAD_ANTENNA_TILT_PUSH": {
        # Synth data alignment:
        #   YAML: action=RAN_PARAM_ROLLBACK, type=RAN_PARAMETER_PUSH
        #   ACTIVATIONS.csv: action=RAN_PARAM_ROLLBACK
        #   PERFORMANCE.csv: dl_throughput_mbps 50->8, rrc_setup_success_rate 99.5->60
        #   signal_injection: ml_ran, ml_fault, ml_nrb, ml_cns, ml_sqes
        "yaml_action":   "RAN_PARAM_ROLLBACK",       # ACTIVATIONS.csv action field
        "domain":        "RAN",
        "risk_level":    "LOW",
        "reversible":    True,
        "synth_signal":  ["ml_ran", "ml_fault", "ml_nrb", "ml_cns", "ml_sqes"],
        "performance_kpis": [
            "dl_throughput_mbps",       # 50.0 -> 8.0 (PERFORMANCE.csv RAN)
            "rrc_setup_success_rate",   # 99.5 -> 60.0
            "handover_success_rate",    # 98.0 -> 55.0
        ],
        "ranked_healing_actions": [
            "ROLLBACK_TILT_TO_BASELINE",
            "REDUCE_TILT_BY_2_DEGREES",
            "REBUILD_NEIGHBOR_RELATIONS",
        ],
        "secondary_healing_actions": ["REBUILD_NEIGHBOR_RELATIONS"],
        "tmf915_parameter_bounds": {
            "parameter":       "antenna_tilt_degrees",
            "current_value":   None,
            "baseline_value":  3.0,
            "min_delta":       -5.0,
            "max_delta":        5.0,
            "safe_profile":    "baseline_profile_v1",
            "rollback_action": "SET_TILT_TO_BASELINE",
            "unit":            "degrees",
        },
        "expected_recovery_minutes": 10,   # YAML ttr_minutes=130, recovery effect ~10
    },

    "HSS_STALE_SESSION_LOOP": {
        "yaml_action":   "MZ_SESSION_CLEAR",          # ACTIVATIONS.csv action field
        "domain":        "CORE",
        "risk_level":    "MEDIUM",
        "reversible":    True,
        "synth_signal":  ["ml_nmc", "ml_fault", "ml_core"],
        "performance_kpis": [
            "attach_success_rate",     # 99.8 -> 72.0 (PERFORMANCE.csv CORE)
            "active_session_count",    # 5000 -> 8500
        ],
        "ranked_healing_actions": [
            "CLEAR_STALE_HSS_SESSIONS",
            "SHIFT_TRAFFIC_TO_SECONDARY_HSS",
            "REDUCE_REATTACH_RATE_LIMIT",
        ],
        "secondary_healing_actions": ["SHIFT_TRAFFIC_TO_SECONDARY_HSS"],
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

    "HSS_SATURATION": {
        # UC2: hss_subscriber_db_saturation -> HSS_SATURATION
        # YAML: action=MZ_SESSION_CLEAR, type=FAILOVER_MIGRATION
        # ACTIVATIONS.csv: action=MZ_SESSION_CLEAR, parameter_name=ClearStaleSessions
        # PERFORMANCE.csv: attach_success_rate 99.8->72, cpu_utilization_pct 35->92
        # signal_injection: ml_core, ml_nmc, ml_fault, ml_moutage
        "yaml_action":   "MZ_SESSION_CLEAR",          # ACTIVATIONS.csv action field
        "domain":        "CORE",
        "risk_level":    "MEDIUM",
        "reversible":    True,
        "synth_signal":  ["ml_core", "ml_nmc", "ml_fault", "ml_moutage"],
        "performance_kpis": [
            "attach_success_rate",     # 99.8 -> 72.0
            "cpu_utilization_pct",     # 35.0 -> 92.0
        ],
        "ranked_healing_actions": [
            "CLEAR_STALE_HSS_SESSIONS",
            "SHIFT_TRAFFIC_TO_SECONDARY_HSS",
            "REDUCE_REATTACH_RATE_LIMIT",
        ],
        "secondary_healing_actions": ["SHIFT_TRAFFIC_TO_SECONDARY_HSS"],
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

    "FIBER_CUT": {
        # UC3: physical_fiber_cut_backhaul -> FIBER_CUT
        # YAML: action=BACKHAUL_REROUTE, type=FIBER_CUT
        # ACTIVATIONS.csv: action=BACKHAUL_REROUTE, parameter_name=RerouteBackhaulTraffic
        # PERFORMANCE.csv: link_utilization_pct 40->100, packet_loss_rate 0.01->45
        # signal_injection: ml_ebh, ml_fault, ml_wb, ml_nmc
        "yaml_action":   "BACKHAUL_REROUTE",           # ACTIVATIONS.csv action field
        "domain":        "TRANSPORT",
        "risk_level":    "HIGH",
        "reversible":    False,
        "synth_signal":  ["ml_ebh", "ml_fault", "ml_wb", "ml_nmc"],
        "performance_kpis": [
            "link_utilization_pct",  # 40.0 -> 100.0
            "packet_loss_rate",      # 0.01 -> 45.0
            "latency_ms",            # 5.0  -> 320.0
        ],
        "ranked_healing_actions": [
            "FAILOVER_TO_REDUNDANT_FIBER_PATH",
            "REROUTE_TRAFFIC_VIA_BACKUP_AGG",
            "ISOLATE_FAILED_AGG_NODE",
        ],
        "secondary_healing_actions": [],
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

    "PATH_DEGRADATION": {
        "yaml_action":   "BACKHAUL_REROUTE",
        "domain":        "TRANSPORT",
        "risk_level":    "MEDIUM",
        "reversible":    True,
        "synth_signal":  ["ml_fault", "ml_wb", "ml_ebh"],
        "performance_kpis": ["link_utilization_pct", "latency_ms"],
        "ranked_healing_actions": [
            "FAILOVER_TO_BACKUP_PATH",
            "RESET_TRANSPORT_PATH",
            "REROUTE_TRAFFIC_VIA_BACKUP_AGG",
        ],
        "secondary_healing_actions": [],
        "tmf915_parameter_bounds": {
            "parameter":       "transport_path",
            "safe_profile":    "backup_path_v1",
            "rollback_action": "RESTORE_PRIMARY_PATH",
            "unit":            "path_id",
        },
        "expected_recovery_minutes": 20,
    },

    "MULTI_DOMAIN_SERVICE_DEGRADATION": {
        "yaml_action":   "MULTI_DOMAIN_SEQUENCE",
        "domain":        "CROSS_DOMAIN",
        "risk_level":    "LOW",
        "reversible":    True,
        "synth_signal":  ["ml_ebh", "ml_nmc", "ml_fault", "ml_wb", "ml_ran"],
        "performance_kpis": [
            "dl_throughput_mbps", "attach_success_rate", "link_utilization_pct",
        ],
        "ranked_healing_actions": [
            "ROLLBACK_TILT_TO_BASELINE",
            "CLEAR_STALE_HSS_SESSIONS",
            "FAILOVER_TO_BACKUP_PATH",
        ],
        "secondary_healing_actions": [],
        "tmf915_parameter_bounds": {
            "parameter":       "multi_domain",
            "safe_profile":    "cross_domain_v1",
            "rollback_action": "RESTORE_ALL_BASELINES",
            "unit":            "composite",
        },
        "expected_recovery_minutes": 25,
    },
}

# ── Reflection config ──────────────────────────────────────────────────────────
REFLECTION_CONFIG: dict = {
    "resolved_z_threshold":    BASELINE_Z_SCORE,
    "execution_success_state": "completed",
    "max_retrigger_attempts":  3,
    "topology_stable_states":  [
        "STABLE_GRAPH_V2",
        "STABLE_GRAPH_CORE_V2",
        "STABLE_GRAPH_TRANSPORT_V2",
    ],
    "kpi_normal_states":      ["KEI_NORMAL"],
    "business_normal_states": ["UTILITY_SCORE_NORMAL"],
}

# ── Utility scoring weights ────────────────────────────────────────────────────
UTILITY_SCORING: dict = {
    "risk_penalty": {
        "LOW":    0.1,
        "MEDIUM": 0.4,
        "HIGH":   0.7,
    },
    "tier_weight": {
        "Tier1": 1.0,
        "Tier2": 0.7,
        "Tier3": 0.4,
    },
    "reversibility_multiplier": {
        "reversible":     1.0,
        "not_reversible": 0.5,
    },
    "active_traffic_boost":        1.2,
    "active_threshold_pct":        10.0,
    "secondary_branch_kpi_weight": 0.4,
    "default_kpi_delta_pct":       30.0,
    "priority_thresholds": {
        "CRITICAL": 0.6,
        "HIGH":     0.3,
        "MEDIUM":   0.15,
    },
    "fallback_metadata": {
        "biz_traffic_density":          0.5,
        "biz_service_criticality_tier": "Tier2",
    },
}


# ── Helper functions ───────────────────────────────────────────────────────────

def infer_domain(nodes: list[str]) -> str:
    """Derive domain from GNN anomalous subgraph node names."""
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


def get_priority_flag(score: float) -> str:
    """Map composite anomaly impact score → internal priority flag."""
    if score >= COMPOSITE_ANOMALY_IMPACT_SCORE_THRESHOLDS["P1"]: return "P1"
    if score >= COMPOSITE_ANOMALY_IMPACT_SCORE_THRESHOLDS["P2"]: return "P2"
    if score >= COMPOSITE_ANOMALY_IMPACT_SCORE_THRESHOLDS["P3"]: return "P3"
    return "NORMAL"


def get_priority_from_gnn(gnn_inference: dict) -> tuple[str, str]:
    """Primary priority resolver. Reads businessPriority first, falls back to compositeScore."""
    biz_priority = gnn_inference.get("businessPriority", "")
    if biz_priority in GNN_BUSINESS_PRIORITY_TO_FLAG:
        flag     = GNN_BUSINESS_PRIORITY_TO_FLAG[biz_priority]
        external = PRIORITY_FLAG_TO_EXTERNAL.get(flag, "CRITICAL")
        return flag, external

    anomaly = gnn_inference.get("anomalyScore", {})
    score   = float(anomaly.get("compositeScore") or anomaly.get("zScore") or 0.0)
    flag     = get_priority_flag(score)
    external = PRIORITY_FLAG_TO_EXTERNAL.get(flag, "CRITICAL")
    return flag, external


def normalize_root_cause(root_cause_str: str) -> str:
    """Map external root_cause string → HEALING_ACTIONS key. Case-insensitive."""
    return ROOT_CAUSE_MAP.get(root_cause_str.lower().strip(), root_cause_str.lower().strip())


def get_healing_actions(root_cause: str) -> dict:
    """Return healing action definition. Accepts external or internal keys."""
    if root_cause in HEALING_ACTIONS:
        return HEALING_ACTIONS[root_cause]
    mapped = normalize_root_cause(root_cause)
    return HEALING_ACTIONS.get(mapped, {
        "yaml_action":               "MANUAL_INVESTIGATION_REQUIRED",
        "domain":                    "UNKNOWN",
        "risk_level":                "HIGH",
        "reversible":                False,
        "synth_signal":              [],
        "performance_kpis":          [],
        "ranked_healing_actions":    ["MANUAL_INVESTIGATION_REQUIRED"],
        "secondary_healing_actions": [],
        "tmf915_parameter_bounds":   {},
        "expected_recovery_minutes": 60,
    })


def get_tilt_correction(current_tilt: float, baseline_tilt: float | None = None) -> dict:
    """Compute exact antenna tilt correction. Used by EngineerAgent."""
    bounds = HEALING_ACTIONS["BAD_ANTENNA_TILT_PUSH"]["tmf915_parameter_bounds"]
    if baseline_tilt is None:
        baseline_tilt = bounds["baseline_value"]
    raw_delta     = baseline_tilt - current_tilt
    clamped_delta = max(bounds["min_delta"], min(bounds["max_delta"], raw_delta))
    return {
        "current_tilt_degrees":  current_tilt,
        "baseline_tilt_degrees": baseline_tilt,
        "correction_delta":      round(clamped_delta, 2),
        "target_tilt_degrees":   round(current_tilt + clamped_delta, 2),
        "within_safe_bounds":    (bounds["min_delta"] <= raw_delta <= bounds["max_delta"]),
        "clamped":               raw_delta != clamped_delta,
    }