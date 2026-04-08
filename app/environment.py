"""
Environment Manager — coordinates sessions, simulator, and grader.

Implements the OpenEnv Environment interface using openenv-core base classes.
Each session has its own isolated state dictionary.

New in v2:
- Incident journal: auto-appended to each observation with what the agent has found
- Second health check: 3 steps after /tmp is cleared, verifies cron is still gone
- Pre-log restart penalty: immediate -0.15 for 3+ restarts before reading logs
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

from openenv.core.env_server.types import Action, Observation, State

from app.models import SREAction, SREObservation, SREState
from app import simulator
from app import grader as grader_mod
from app.scenarios import base as scenario_base
from app.scenarios import task1_zombie, task2_config, task3_resource_leak

# Map task_id -> scenario module
TASK_MODULES = {
    1: task1_zombie,
    2: task2_config,
    3: task3_resource_leak,
}

TASK_NAMES = {
    1: "Zombie Process",
    2: "Config Failure",
    3: "Resource Leak",
}

MAX_STEPS = 30

# ---------------------------------------------------------------------------
# In-process session store
# ---------------------------------------------------------------------------
_sessions: dict[str, dict[str, Any]] = {}


class SREEnvironment:
    """
    SRE Incident Response environment.

    Manages multiple concurrent sessions (one per episode).
    Each call to reset() creates (or reinitialises) a session.
    """

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def reset(self, task_id: int = 1, session_id: Optional[str] = None) -> tuple[str, SREObservation]:
        if task_id not in TASK_MODULES:
            raise ValueError(f"task_id must be 1, 2, or 3; got {task_id}")

        if session_id is None:
            session_id = str(uuid.uuid4())

        task_module = TASK_MODULES[task_id]
        params, sim_state = scenario_base.build_initial_state(task_module, session_id)

        _sessions[session_id] = {
            "sim_state": sim_state,
            "grader_state": grader_mod.initial_grader_state(),
            "task_id": task_id,
            "params": params,
            "step": 0,
            "done": False,
            "final_score": None,
            # Incident journal — tracks what the agent has discovered
            "journal": [],
            # Second health check: after /tmp cleared, verify cron still gone N steps later
            "verify_at_step": None,   # step number to run second health check
            "verify_passed": False,
        }

        initial_service_status = self._surface_service_status(task_id, sim_state)

        TASK_MESSAGES = {
            1: (
                "[Incident Alert] The web service on port 8080 is down.\n"
                "Task: Zombie Process\n"
                "The service was working earlier but has become unresponsive.\n"
                "Your objective: diagnose the root cause and restore the service on port 8080.\n"
                "What is your first command?"
            ),
            2: (
                "[Incident Alert] The nginx service on port 8080 has failed to start.\n"
                "Task: Config Failure\n"
                "nginx was recently reconfigured and has stopped working.\n"
                "Your objective: check the nginx service logs and configuration, fix the error, and restart.\n"
                "What is your first command?"
            ),
            3: (
                "[Incident Alert] The web service on port 8080 is returning errors.\n"
                "Task: Resource Leak\n"
                "nginx appears to be running but requests are failing with 503 errors.\n"
                "Your objective: diagnose why requests are failing and fully resolve the issue.\n"
                "What is your first command?"
            ),
        }

        obs = SREObservation(
            done=False,
            reward=0.0,
            output=TASK_MESSAGES[task_id],
            step=0,
            service_status=initial_service_status,
            info={"session_id": session_id, "task_id": task_id, "milestones": {}},
        )
        return session_id, obs

    def step(self, session_id: str, action: SREAction) -> tuple[SREObservation, float, bool]:
        if session_id not in _sessions:
            raise KeyError(f"session_id '{session_id}' not found")

        sess = _sessions[session_id]

        if sess["done"]:
            obs = SREObservation(
                done=True,
                reward=0.0,
                output="Episode already complete.",
                step=sess["step"],
                service_status="up" if sess["sim_state"].get("service_healthy") else "down",
                info={
                    "session_id": session_id,
                    "final_score": sess["final_score"],
                    "milestones": grader_mod.milestone_summary(sess["grader_state"]),
                },
            )
            return obs, 0.0, True

        cmd = action.command.strip()
        task_id = sess["task_id"]
        sim_state = sess["sim_state"]
        params = sess["params"]
        gs = sess["grader_state"]
        journal: list[str] = sess["journal"]

        # ---- Execute command in simulator ----
        output = simulator.execute(cmd, sim_state, params, task_id)

        # ---- Task 3 time pressure escalation ----
        if task_id == 3:
            task3_resource_leak.apply_time_pressure(sim_state, sess["step"])
            tmp_cleared = sim_state["flags"].get("tmp_cleared", False)
            cron_removed = sim_state["flags"].get("cron_removed", False)
            if tmp_cleared and not sim_state.get("service_healthy"):
                sim_state["service_healthy"] = True
                if not cron_removed:
                    sim_state["service_status"]["nginx"] = (
                        "nginx.service - A high performance web server\n"
                        "   Active: active (running)\n"
                        "   Status: \"Requests succeeding — WARNING: cron job still active, /tmp will fill again\"\n"
                    )

        # ---- Increment step ----
        sess["step"] += 1
        step_number = sess["step"]

        # ---- Second health check for Task 3 ----
        # Schedule second check 3 steps after /tmp is first cleared
        if task_id == 3:
            if sim_state["flags"].get("tmp_cleared") and sess["verify_at_step"] is None:
                sess["verify_at_step"] = step_number + 3

            verify_at = sess["verify_at_step"]
            if verify_at is not None and step_number >= verify_at and not sess["verify_passed"]:
                cron_still_removed = sim_state["flags"].get("cron_removed", False)
                if cron_still_removed:
                    sess["verify_passed"] = True
                    # Bonus: no extra reward, but mark verified so final_score can trust it
                    output = (output or "(command completed silently)") + (
                        "\n\n[MONITORING] Second health check (T+3): /tmp usage nominal. "
                        "No cron activity detected. Fix verified stable."
                    )
                else:
                    # Cron not gone — add an ominous alert
                    output = (output or "(command completed silently)") + (
                        "\n\n[MONITORING] Second health check (T+3): WARNING — /tmp usage rising again. "
                        "Cron job still active. Disk will fill in ~60s."
                    )

        # ---- Grade ----
        step_reward = grader_mod.step_grade(
            task_id=task_id,
            cmd=cmd,
            output=output,
            state=sim_state,
            params=params,
            grader_state=gs,
            step=step_number,
        )

        # ---- Update incident journal ----
        self._update_journal(journal, task_id, cmd, output, gs, sim_state)

        # ---- Check done ----
        if task_id == 3:
            flags = sim_state.get("flags", {})
            fully_fixed = sim_state.get("service_healthy", False) and flags.get("cron_removed", False)
            done = fully_fixed or step_number >= MAX_STEPS
        else:
            done = sim_state.get("service_healthy", False) or step_number >= MAX_STEPS
        sess["done"] = done

        score = None
        if done:
            score = grader_mod.final_score(gs, sim_state, task_id, step_number)
            sess["final_score"] = score

        svc_status = self._surface_service_status(task_id, sim_state)

        # ---- Compose final output with incident journal appended ----
        final_output = output if output else "(command completed silently)"
        if journal:
            final_output = final_output + "\n\n" + self._format_journal(journal)

        obs = SREObservation(
            done=done,
            reward=float(step_reward),
            output=final_output,
            step=step_number,
            service_status=svc_status,
            info={
                "session_id": session_id,
                "cumulative_reward": sum([
                    grader_mod.M1_REWARD if gs["milestones"]["m1_fired"] else 0.0,
                    grader_mod.M2_REWARD if gs["milestones"]["m2_fired"] else 0.0,
                    grader_mod.M3_REWARD if gs["milestones"]["m3_fired"] else 0.0,
                ]),
                "milestones": grader_mod.milestone_summary(gs),
                "penalties": gs.get("penalties", 0.0),
                "escalation_level": sim_state["flags"].get("escalation_step", 0),
                "final_score": score,
                "journal": list(journal),
            },
        )
        return obs, float(step_reward), done

    def get_state(self, session_id: str) -> dict[str, Any]:
        if session_id not in _sessions:
            raise KeyError(f"session_id '{session_id}' not found")
        sess = _sessions[session_id]
        return {
            "session_id": session_id,
            "task_id": sess["task_id"],
            "step": sess["step"],
            "done": sess["done"],
            "final_score": sess["final_score"],
            "service_healthy": sess["sim_state"].get("service_healthy", False),
            "milestones": grader_mod.milestone_summary(sess["grader_state"]),
            "scenario_params": {k: v for k, v in sess["params"].items() if k != "seed"},
            "flags": sess["sim_state"].get("flags", {}),
            "journal": sess["journal"],
            "verify_passed": sess.get("verify_passed", False),
        }

    # ------------------------------------------------------------------ #
    # Incident Journal
    # ------------------------------------------------------------------ #

    @staticmethod
    def _update_journal(
        journal: list[str],
        task_id: int,
        cmd: str,
        output: str,
        gs: dict,
        sim_state: dict,
    ) -> None:
        """
        Auto-append discovered facts to the incident journal.
        Called after every step — journal accumulates the agent's findings.
        """
        cmd_l = cmd.lower()
        out_l = output.lower()
        flags = sim_state.get("flags", {})

        # M1 fired this step → record what was found
        if gs["milestones"].get("m1_fired") and len(journal) == 0:
            if task_id == 1:
                journal.append("[DISCOVERED] Port 8080 is held by a zombie process.")
            elif task_id == 2:
                journal.append("[DISCOVERED] nginx failed to start — config syntax error in /etc/nginx/nginx.conf.")
            elif task_id == 3:
                journal.append("[DISCOVERED] /tmp filesystem is 100% full — nginx cannot write temp files.")

        # M2 fired → record fix attempt
        if gs["milestones"].get("m2_fired") and not any("[FIX APPLIED]" in j for j in journal):
            if task_id == 1:
                journal.append("[FIX APPLIED] Zombie process killed.")
            elif task_id == 2:
                journal.append("[FIX APPLIED] nginx configuration file edited.")
            elif task_id == 3:
                if flags.get("tmp_cleared"):
                    journal.append("[FIX APPLIED] /tmp cleared — disk pressure relieved.")
                if flags.get("cron_removed"):
                    journal.append("[FIX APPLIED] Rogue cron job removed.")

        # Additional contextual discoveries
        if task_id == 3:
            if flags.get("cron_removed") and not any("[CRON REMOVED]" in j for j in journal):
                journal.append("[STATUS] Cron job removed — root cause eliminated.")
            # Track escalation in journal
            esc = sim_state["flags"].get("escalation_step", 0)
            if esc >= 2 and not any("[ESCALATED]" in j for j in journal):
                journal.append(f"[ESCALATED] Disk pressure at level {esc} — situation worsening.")

        if task_id == 2:
            if "no changes made" in out_l and not any("[PATTERN MISMATCH]" in j for j in journal):
                journal.append("[PATTERN MISMATCH] Last sed command did not match any line in nginx.conf.")
            # Clear mismatch note once config is fixed
            if sim_state.get("nginx_config_valid") and any("[PATTERN MISMATCH]" in j for j in journal):
                journal[:] = [j for j in journal if "[PATTERN MISMATCH]" not in j]
                journal.append("[CONFIG VALID] nginx.conf syntax is now correct.")

    @staticmethod
    def _format_journal(journal: list[str]) -> str:
        if not journal:
            return ""
        lines = ["--- Incident Journal ---"] + journal + ["------------------------"]
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _surface_service_status(task_id: int, sim_state: dict) -> str:
        healthy = sim_state.get("service_healthy", False)
        if healthy:
            return "up"
        if task_id == 3:
            return "degraded"
        return "down"


# Singleton instance
env = SREEnvironment()
