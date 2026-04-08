"""
Pydantic models for the SRE Incident Response OpenEnv environment.
All typed contracts: Action, Observation, Reward, Session.
"""

from datetime import datetime
from typing import Any, Optional
from uuid import uuid4

from openenv.core.env_server.types import Action, Observation, State


# ---------------------------------------------------------------------------
# Action
# ---------------------------------------------------------------------------

class SREAction(Action):
    """
    A single Linux terminal command string.

    The entire agent interface is a single command — no type field, no metadata.
    Examples: "ps aux", "kill -9 31337", "sed -i 's/auto2/auto/' /etc/nginx/nginx.conf"
    """
    command: str


# ---------------------------------------------------------------------------
# Observation
# ---------------------------------------------------------------------------

class SREObservation(Observation):
    """
    Terminal output from the last executed command plus episode metadata.

    service_status reflects only what the agent could infer from commands
    already run — it is a surface indicator, not internal ground truth.
    For Task 3 it starts as 'degraded' (nginx active but requests failing).
    """
    # 'done: bool' and 'reward: Optional[float]' inherited from Observation
    output: str = ""                          # raw terminal output
    step: int = 0                             # current step number
    service_status: str = "unknown"           # "up" | "down" | "degraded" | "unknown"
    info: dict[str, Any] = {}                 # milestones, cumulative_reward, penalties


# ---------------------------------------------------------------------------
# Session State
# ---------------------------------------------------------------------------

class SREState(State):
    """
    Full session state returned by GET /state.

    'episode_id' and 'step_count' are inherited from State.
    """
    # inherited: episode_id: str, step_count: int
    task_id: int = 1
    seed: int = 0
    created_at: datetime = None             # type: ignore[assignment]
    status: str = "active"                  # "active" | "complete"
    cumulative_reward: float = 0.0
    milestones: dict[str, Any] = {}
    penalties: float = 0.0
    escalation_level: int = 0               # Task 3 only

    def model_post_init(self, __context: Any) -> None:
        if self.created_at is None:
            self.created_at = datetime.utcnow()
