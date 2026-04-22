"""
healing_knowledge_base.py
==========================
Single source of truth for ALL hard-coded healing parameters.

Rules:
  - No magic numbers anywhere in tools.py files.
  - Every threshold, degree, action name, and KPI bound lives here.
  - tools.py files import what they need — nothing else.

Sections
--------
  1. Domain node detection keywords
  2. Priority thresholds (Z-score → P1/P2/P3)
  3. Healing actions per root cause
     - Antenna tilt: exact degrees to roll back, per-node override map
     - HSS sessions: clear count limits
     - Transport: failover parameters
  4. Validation thresholds (pre vs post Z-score)
  5. GNN node name → synth_gen EID mapping
"""

# ─────────────────────────────────────────────────────────────────────────────
# 1. DOMAIN KEYWORDS  (used by MonitoringAgent to infer domain from node names)
# ─────────────────────────────────────────────────────────────────────────────

DOMAIN_KEYWORDS = {
    "RAN":       ["RAN", "eNB", "gNB", "CELL", "SECTOR"],
    "CORE":      ["HSS", "MME", "AMF", "UPF", "SMF", "CORE"],
    "TRANSPORT": ["TRANSPORT", "CSR", "AGG", "LINK", "BACKHAUL"],
}

# ─────────────────────────────────────────────────────────────────────────────
# 2. PRIORITY THRESHOLDS
# ─────────────────────────────────────────────────────────────────────────────

Z_SCORE_PRIORITY = {
    "P1": 8.0,   # z >= 8.0  → P1 CRITICAL
    "P2": 5.0,   # z >= 5.0  → P2 HIGH
    "P3": 0.0,   # z <  5.0  → P3 MEDIUM
}

# ─────────────────────────────────────────────────────────────────────────────
# 3. HEALING ACTIONS PER ROOT CAUSE
# ─────────────────────────────────────────────────────────────────────────────

# --- 3a. Antenna Tilt (UC1 / BAD_ANTENNA_TILT_PUSH) ---

# Default tilt rollback when GNN does not carry a per-node delta.
# Positive = uptilt (less coverage), negative = downtilt (more coverage).
# The investigation agent from Ericsson team should provide the actual
# observed_tilt_degrees in the RCA payload when available.
ANTENNA_TILT_DEFAULT_ROLLBACK_DEGREES = -2   # roll back 2° from the bad push

# How much tolerance to allow before declaring a tilt mismatch (degrees)
ANTENNA_TILT_TOLERANCE_DEGREES = 0.5

# Safe operating bounds (TMF915 parameter bounds)
ANTENNA_TILT_MIN_DEGREES = -10
ANTENNA_TILT_MAX_DEGREES =  10

# Per-node tilt override: if the Ericsson investigation agent identifies a
# specific node + exact bad tilt, those values go here for the demo.
# Format: { "node_eid": {"bad_tilt": <float>, "target_tilt": <float>} }
ANTENNA_TILT_NODE_OVERRIDES: dict[str, dict] = {
    # populated from synth_gen scenario YAML — empty means use default
    # Example: "eNB-SYN-003": {"bad_tilt": 4.0, "target_tilt": 2.0},
}

ANTENNA_TILT_ACTIONS = {
    "domain": "RAN",
    "ranked_healing_actions": [
        "ROLLBACK_TILT_TO_BASELINE",
        "REDUCE_TILT_BY_2_DEGREES",
        "REBUILD_NEIGHBOR_RELATIONS",
    ],
    "tmf915_parameter_bounds": {
        "parameter":              "antenna_tilt",
        "unit":                   "degrees",
        "rollback_delta_degrees": ANTENNA_TILT_DEFAULT_ROLLBACK_DEGREES,
        "min_degrees":            ANTENNA_TILT_MIN_DEGREES,
        "max_degrees":            ANTENNA_TILT_MAX_DEGREES,
        "tolerance_degrees":      ANTENNA_TILT_TOLERANCE_DEGREES,
        "safe_profile":           "baseline_profile_v1",
    },
}

# --- 3b. HSS Stale Sessions (UC2 / HSS_STALE_SESSION_LOOP) ---

HSS_MAX_SESSIONS_TO_CLEAR     = 10_000   # safety cap per clear command
HSS_TARGET_CAPACITY_PCT       = 80       # "Restore HSS capacity to <80%"
HSS_SESSION_CLEAR_BATCH_SIZE  = 1_000    # clear in batches of 1000
HSS_STALE_SESSION_ACTIONS = {
    "domain": "CORE",
    "ranked_healing_actions": [
        "CLEAR_STALE_HSS_SESSIONS",
        "SHIFT_TRAFFIC_TO_SECONDARY_HSS",
        "REDUCE_REATTACH_RATE_LIMIT",
    ],
    "tmf915_parameter_bounds": {
        "parameter":            "stale_sessions",
        "max_clear":            HSS_MAX_SESSIONS_TO_CLEAR,
        "target_capacity_pct":  HSS_TARGET_CAPACITY_PCT,
        "batch_size":           HSS_SESSION_CLEAR_BATCH_SIZE,
        "safe_profile":         "clear_looped_503_sessions",
    },
}

# --- 3c. Transport Path Degradation (UC3 / PATH_DEGRADATION) ---

TRANSPORT_REROUTE_TIMEOUT_SEC  = 30    # seconds to wait for reroute confirmation
TRANSPORT_PATH_DEGRADATION_ACTIONS = {
    "domain": "TRANSPORT",
    "ranked_healing_actions": [
        "FAILOVER_TO_BACKUP_PATH",
        "RESET_TRANSPORT_PATH",
    ],
    "tmf915_parameter_bounds": {
        "parameter":             "transport_path",
        "reroute_timeout_sec":   TRANSPORT_REROUTE_TIMEOUT_SEC,
        "safe_profile":          "backup_path_v1",
    },
}

# --- 3d. Multi-domain (cross-domain cascade) ---

MULTI_DOMAIN_ACTIONS = {
    "domain": "CROSS_DOMAIN",
    "ranked_healing_actions": [
        "ROLLBACK_TILT_TO_BASELINE",
        "CLEAR_STALE_HSS_SESSIONS",
        "FAILOVER_TO_BACKUP_PATH",
    ],
    "tmf915_parameter_bounds": {
        "parameter":    "multi_domain",
        "safe_profile": "cross_domain_v1",
    },
}

# Master lookup — root_cause string → healing action config
HEALING_ACTIONS: dict[str, dict] = {
    "BAD_ANTENNA_TILT_PUSH":           ANTENNA_TILT_ACTIONS,
    "HSS_STALE_SESSION_LOOP":          HSS_STALE_SESSION_ACTIONS,
    "HSS_SATURATION":                  HSS_STALE_SESSION_ACTIONS,   # alias
    "PATH_DEGRADATION":                TRANSPORT_PATH_DEGRADATION_ACTIONS,
    "FIBER_CUT":                       TRANSPORT_PATH_DEGRADATION_ACTIONS,  # alias
    "MULTI_DOMAIN_SERVICE_DEGRADATION": MULTI_DOMAIN_ACTIONS,
}

# ─────────────────────────────────────────────────────────────────────────────
# 4. VALIDATION THRESHOLDS
# ─────────────────────────────────────────────────────────────────────────────

# Post-action Z-score must be at or below this to count as RESOLVED.
# Matches synth_gen execution_mock_output: postActionZScore=1.2, expectedBaseline=2.0
VALIDATION_Z_SCORE_BASELINE   = 2.0

# If post-Z is within this band of baseline, still pass (floating point safety)
VALIDATION_Z_SCORE_TOLERANCE  = 0.05

# Minimum % of branches that must succeed for RESOLVED verdict
VALIDATION_MIN_SUCCESS_BRANCH_PCT = 1.0   # 100% — all branches must succeed

# How many retrigger loops before we give up and escalate
VALIDATION_MAX_RETRIGGER_LOOPS = 3

# ─────────────────────────────────────────────────────────────────────────────
# 5. GNN NODE NAME → SYNTH_GEN EID MAPPING
#    The GNN inference provider uses short names like "RAN_CELL_101".
#    The synth_gen pipeline uses full EIDs like "eNB-SYN-001".
#    This mapping lets agents translate between the two without hard-coding
#    the translation inside tools.py.
# ─────────────────────────────────────────────────────────────────────────────

GNN_NODE_TO_SYNTH_EID: dict[str, str] = {
    "RAN_CELL_101":    "eNB-SYN-001",
    "RAN_CELL_102":    "eNB-SYN-002",
    "RAN_CELL_103":    "eNB-SYN-003",
    "HSS_CORE_01":     "HSS-SYN-01",
    "HSS_CORE_02":     "HSS-SYN-02",
    "TRANSPORT_LINK_01": "CSR-SYN-001",
    "TRANSPORT_LINK_02": "AGG-SYN-01",
}

# Reverse map (synth EID → GNN node name)
SYNTH_EID_TO_GNN_NODE: dict[str, str] = {v: k for k, v in GNN_NODE_TO_SYNTH_EID.items()}
