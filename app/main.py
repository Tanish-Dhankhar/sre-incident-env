"""
FastAPI application for SRE Incident Response OpenEnv environment.

Exposes HTTP endpoints compatible with the OpenEnv spec:
  POST /reset   — start new episode
  POST /step    — send terminal command, get observation
  GET  /state   — get full session state
  GET  /health  — liveness probe
  GET  /tasks   — list available tasks
"""

from __future__ import annotations

import datetime
from typing import Optional

import gradio as gr
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app.environment import env
from app.models import SREAction, SREObservation

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="SRE Incident Response OpenEnv",
    description=(
        "An OpenEnv-compliant environment that simulates a broken Linux server. "
        "An AI agent diagnoses and fixes a downed web service by running terminal commands."
    ),
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount Gradio demo at /
from app.demo import demo as gradio_demo  # noqa: E402 (after app created to avoid circular)
gr.mount_gradio_app(app, gradio_demo, path="/")

# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

class ResetRequest(BaseModel):
    task_id: int = 1
    session_id: Optional[str] = None


class ResetResponse(BaseModel):
    session_id: str
    observation: dict
    task_name: str
    task_description: str


class StepRequest(BaseModel):
    session_id: str
    command: str


class StepResponse(BaseModel):
    observation: dict
    reward: float
    done: bool
    info: dict


# ---------------------------------------------------------------------------
# Static task catalogue
# ---------------------------------------------------------------------------

TASKS = [
    {
        "id": 1,
        "name": "Zombie Process",
        "difficulty": "easy",
        "description": (
            "A zombie process is holding port 8080. Nginx cannot bind to the port "
            "and fails on restart. Identify the zombie PID, kill it, and restart nginx."
        ),
    },
    {
        "id": 2,
        "name": "Config Failure",
        "difficulty": "medium",
        "description": (
            "The nginx configuration file has a syntax error. Nginx fails on start "
            "with a clear error in the logs. Read the config, fix the error, and restart."
        ),
    },
    {
        "id": 3,
        "name": "Resource Leak",
        "difficulty": "hard",
        "description": (
            "A rogue cron job is filling /tmp with large files. Nginx reports as active "
            "but requests fail silently with 503 errors. Identify and fix both the "
            "immediate symptom (full disk) and the root cause (rogue cron job)."
        ),
    },
]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    """Liveness probe — must return 200 within 100ms."""
    return {"status": "ok", "timestamp": datetime.datetime.utcnow().isoformat()}


@app.get("/tasks")
def list_tasks():
    """Return static list of all available tasks."""
    return {"tasks": TASKS}


@app.post("/reset", response_model=ResetResponse)
def reset(req: ResetRequest):
    """
    Start a new episode.

    Args:
        task_id:    1 (easy), 2 (medium), or 3 (hard)
        session_id: optional; a new UUID is generated if not provided
    """
    if req.task_id not in (1, 2, 3):
        raise HTTPException(status_code=422, detail="task_id must be 1, 2, or 3")

    try:
        session_id, obs = env.reset(task_id=req.task_id, session_id=req.session_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    task = next(t for t in TASKS if t["id"] == req.task_id)
    return ResetResponse(
        session_id=session_id,
        observation=obs.model_dump(),
        task_name=task["name"],
        task_description=task["description"],
    )


@app.post("/step", response_model=StepResponse)
def step(req: StepRequest):
    """
    Execute a terminal command in the environment.

    Args:
        session_id: from /reset response
        command:    a Linux terminal command string
    """
    try:
        action = SREAction(command=req.command)
        obs, reward, done = env.step(req.session_id, action)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return StepResponse(
        observation=obs.model_dump(),
        reward=reward,
        done=done,
        info=obs.info,
    )


@app.get("/state")
def get_state(session_id: str):
    """Return full session state for debugging."""
    try:
        return env.get_state(session_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
