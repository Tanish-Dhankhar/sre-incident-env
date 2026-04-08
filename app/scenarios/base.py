"""
Shared parameterized scenario loader.

Every scenario module must expose:
  - PARAM_SPACE: dict describing the parameter space
  - sample_params(seed: int) -> dict   — deterministic from seed
  - build_state(params: dict) -> dict  — full simulator state dict
  - grade(params: dict, state: dict) -> float  — score 0.0–1.0
"""

import hashlib
import random
from types import ModuleType
from typing import Any


def _seed_from_session(session_id: str) -> int:
    """Derive a stable integer seed from a session UUID string."""
    digest = hashlib.sha256(session_id.encode()).hexdigest()
    return int(digest[:8], 16)


def build_initial_state(task_module: ModuleType, session_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    Given a task module and session_id, return (params, state).

    The seed is derived from session_id so:
      - Every session_id gives a unique scenario
      - The same session_id always gives the same scenario (reproducible)
    """
    seed = _seed_from_session(session_id)
    params = task_module.sample_params(seed)
    state = task_module.build_state(params)
    return params, state
