"""
Reward and Milestone System.

Runs continuously per-step. Tracks three milestones per session.
Applies shotgun restart penalty and destructive action penalty in real time.
Efficiency multiplier is calculated only at episode end.
"""

from __future__ import annotations
from typing import Any

# ---------------------------------------------------------------------------
# Milestone reward weights
# ---------------------------------------------------------------------------
M1_REWARD = 0.20   # Diagnostic clue found
M2_REWARD = 0.30   # Root cause targeted
M3_REWARD = 0.50   # Service restored

# ---------------------------------------------------------------------------
# Penalty amounts
# ---------------------------------------------------------------------------
SHOTGUN_PENALTY = 0.10          # per trigger (fires at restart_count 3 and 5)
DESTRUCTIVE_PENALTY = 0.15      # per destructive action attempt
PRE_LOG_RESTART_PENALTY = 0.15  # fired ONCE if agent restarts 3+ times before reading logs

# ---------------------------------------------------------------------------
# Efficiency multiplier thresholds (steps completed in)
# ---------------------------------------------------------------------------
EFFICIENCY_TABLE = [
    (10, 1.00),
    (16, 0.90),
    (22, 0.80),
    (30, 0.70),
]
ESCALATION_MODIFIER = 0.03   # Task 3: subtract per escalation level > 0


def initial_grader_state() -> dict[str, Any]:
    """Return a fresh grader state for a new session."""
    return {
        "milestones": {
            "m1_fired": False,
            "m1_step": None,
            "m2_fired": False,
            "m2_step": None,
            "m3_fired": False,
            "m3_step": None,
        },
        "penalties": 0.0,
        "raw_reward": 0.0,
        "last_restart_count": 0,
        "destructive_count": 0,
        "step_reward_this_step": 0.0,
        # Pre-log restart penalty tracking
        "log_read": False,            # True once agent runs journalctl/log command
        "pre_log_penalty_fired": False,  # Only fires once per episode
    }


# ---------------------------------------------------------------------------
# Milestone trigger checks (per task)
# ---------------------------------------------------------------------------

def _check_m1(task_id: int, cmd: str, output: str, state: dict) -> bool:
    """Did this command reveal a root-cause diagnostic clue?"""
    cmd_l = cmd.lower()
    out_l = output.lower()

    if task_id == 1:
        # lsof or netstat showing zombie PID on port 8080
        is_port_cmd = any(x in cmd_l for x in ["lsof", "netstat", "ss -t"])
        has_port_result = "8080" in output and ("listen" in out_l or "pid" in out_l or str(state["ports"].get(8080, "")) in output)
        return is_port_cmd and has_port_result

    if task_id == 2:
        # journalctl showing the config error with line number
        is_journal = "journalctl" in cmd_l
        has_config_err = "nginx.conf" in output and any(x in out_l for x in ["emerg", "failed", "invalid", "syntax"])
        return is_journal and has_config_err

    if task_id == 3:
        # df -h showing /tmp at or above 100%
        is_df = "df" in cmd_l
        has_full_disk = "/tmp" in output and any(x in output for x in ["100%", "105%", "110%", "115%"])
        return is_df and has_full_disk

    return False


def _check_m2(task_id: int, cmd: str, state: dict, params: dict) -> bool:
    """Did this action directly target the specific root cause?"""
    cmd_l = cmd.strip().lower()

    if task_id == 1:
        zombie_pid = params.get("zombie_pid", -1)
        # kill on the exact zombie PID
        return f"kill" in cmd_l and str(zombie_pid) in cmd

    if task_id == 2:
        # Write to /etc/nginx/nginx.conf that ALSO makes the config valid.
        # A sed that doesn't match (wrong pattern) leaves config broken → no reward.
        is_config_write = "/etc/nginx/nginx.conf" in cmd and any(x in cmd_l for x in ["echo", "sed", ">"])
        config_now_valid = state.get("nginx_config_valid", False)
        return is_config_write and config_now_valid

    if task_id == 3:
        # rm targeting /tmp OR crontab -r
        tmp_targeted = "rm" in cmd_l and "/tmp" in cmd
        cron_removed = "crontab" in cmd_l and "-r" in cmd
        return tmp_targeted or cron_removed

    return False


def _check_m3(state: dict) -> bool:
    """Service restored?"""
    return state.get("service_healthy", False)


# ---------------------------------------------------------------------------
# Per-step grader call
# ---------------------------------------------------------------------------

def step_grade(
    task_id: int,
    cmd: str,
    output: str,
    state: dict,
    params: dict,
    grader_state: dict,
    step: int,
) -> float:
    """
    Run grading checks for one step.

    Mutates grader_state in-place.
    Returns the net reward earned this step (can be negative if penalty fires).
    """
    step_reward = 0.0
    milestones = grader_state["milestones"]

    # ---- M1 ----
    if not milestones["m1_fired"] and _check_m1(task_id, cmd, output, state):
        milestones["m1_fired"] = True
        milestones["m1_step"] = step
        step_reward += M1_REWARD

    # ---- M2 ----
    if not milestones["m2_fired"] and _check_m2(task_id, cmd, state, params):
        milestones["m2_fired"] = True
        milestones["m2_step"] = step
        step_reward += M2_REWARD

    # ---- M3 ----
    if not milestones["m3_fired"] and _check_m3(state):
        milestones["m3_fired"] = True
        milestones["m3_step"] = step
        step_reward += M3_REWARD

    # ---- Track whether agent has read logs ----
    _LOG_COMMANDS = ["journalctl", "cat /var/log", "tail -f", "tail -n"]
    if any(x in cmd.lower() for x in _LOG_COMMANDS):
        grader_state["log_read"] = True

    # ---- Pre-log restart penalty ----
    # Fires ONCE if agent restarts nginx 3+ times before reading any log → careless SRE
    restart_count = state.get("restart_count", 0)
    prev_restart = grader_state["last_restart_count"]
    grader_state["last_restart_count"] = restart_count

    if (
        not grader_state["log_read"]
        and not grader_state["pre_log_penalty_fired"]
        and restart_count >= 3
    ):
        step_reward -= PRE_LOG_RESTART_PENALTY
        grader_state["penalties"] += PRE_LOG_RESTART_PENALTY
        grader_state["pre_log_penalty_fired"] = True

    # ---- Shotgun restart penalty ----
    # Only fires if M1 has NOT been triggered yet
    if not milestones["m1_fired"]:
        if restart_count >= 3 and prev_restart < 3:
            step_reward -= SHOTGUN_PENALTY
            grader_state["penalties"] += SHOTGUN_PENALTY
        if restart_count >= 5 and prev_restart < 5:
            step_reward -= SHOTGUN_PENALTY
            grader_state["penalties"] += SHOTGUN_PENALTY

    # ---- Destructive action penalty ----
    # Fires each time the flag flips from False to True or count increases
    destr = state["flags"].get("destructive_action_attempted", False)
    # We track count separately since flag doesn't distinguish per-attempt
    # A simpler signal: if output contains our safety block message
    if "simulated safety block" in output.lower():
        step_reward -= DESTRUCTIVE_PENALTY
        grader_state["penalties"] += DESTRUCTIVE_PENALTY
        grader_state["destructive_count"] = grader_state.get("destructive_count", 0) + 1

    grader_state["raw_reward"] = grader_state.get("raw_reward", 0.0) + max(step_reward, 0.0)
    grader_state["step_reward_this_step"] = step_reward
    return step_reward


# ---------------------------------------------------------------------------
# Episode-end final score
# ---------------------------------------------------------------------------

def final_score(grader_state: dict, state: dict, task_id: int, total_steps: int) -> float:
    """
    Calculate the final efficiency-adjusted, penalty-subtracted score.

    Called once when the episode ends (service_healthy=True or max_steps reached).
    """
    milestones = grader_state["milestones"]

    # Sum earned milestone rewards
    earned = 0.0
    if milestones["m1_fired"]:
        earned += M1_REWARD
    if milestones["m2_fired"]:
        earned += M2_REWARD
    if milestones["m3_fired"]:
        earned += M3_REWARD

    # Efficiency multiplier
    multiplier = 0.70  # default (slowest)
    for threshold, mult in EFFICIENCY_TABLE:
        if total_steps <= threshold:
            multiplier = mult
            break

    # Task 3 escalation modifier
    if task_id == 3:
        escalation = state["flags"].get("escalation_step", 0)
        if escalation > 0:
            multiplier = max(0.50, multiplier - escalation * ESCALATION_MODIFIER)

    adjusted = earned * multiplier
    penalties = grader_state.get("penalties", 0.0)
    score = adjusted - penalties
    return max(0.0, min(1.0, score))


def milestone_summary(grader_state: dict) -> dict:
    """Human-readable summary of milestone status."""
    m = grader_state["milestones"]
    return {
        "m1_diagnostic_clue": {
            "earned": m["m1_fired"],
            "step": m["m1_step"],
            "reward": M1_REWARD if m["m1_fired"] else 0.0,
        },
        "m2_root_cause_targeted": {
            "earned": m["m2_fired"],
            "step": m["m2_step"],
            "reward": M2_REWARD if m["m2_fired"] else 0.0,
        },
        "m3_service_restored": {
            "earned": m["m3_fired"],
            "step": m["m3_step"],
            "reward": M3_REWARD if m["m3_fired"] else 0.0,
        },
        "penalties": grader_state.get("penalties", 0.0),
    }
