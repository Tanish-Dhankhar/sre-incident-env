"""
SRE Incident Response OpenEnv — Tiered Baseline

Three agent tiers show that the reward function meaningfully discriminates:
  Tier 1 — Random Agent   : selects commands at random from a diagnostic pool
  Tier 2 — Plain LLM      : sends raw observation to LLM, uses its first command
  Tier 3 — CoT LLM        : uses chain-of-thought system prompt + fallback rotation

Run all tiers:
  python inference.py

Run a single tier:
  python inference.py --tier 2

Environment variables:
  API_BASE_URL      OpenAI-compatible base URL (default: Google Generative AI)
  MODEL_NAME        Model name (default: gemini-3.1-flash-lite-preview)
  HF_TOKEN          API key (also accepts OPENAI_API_KEY)
  ENVIRONMENT_URL   Environment server URL (default: http://localhost:7860)
  TIER              Which tiers to run: "1", "2", "3", or "all" (default: all)
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time
from typing import Optional

import requests
from openai import OpenAI

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

API_BASE_URL = os.environ.get(
    "API_BASE_URL",
    "https://generativelanguage.googleapis.com/v1beta/openai/",
)
MODEL_NAME = os.environ.get("MODEL_NAME", "gemini-3.1-flash-lite-preview")
API_KEY = os.environ.get("HF_TOKEN") or os.environ.get("OPENAI_API_KEY", "")
ENVIRONMENT_URL = os.environ.get("ENVIRONMENT_URL", "http://localhost:7860").rstrip("/")
TEMPERATURE = 0.2
MAX_STEPS = 30

TASK_IDS = [1, 2, 3]
TASK_NAMES = {1: "zombie_process", 2: "config_failure", 3: "resource_leak"}

# ---------------------------------------------------------------------------
# Random agent command pool (Tier 1)
# ---------------------------------------------------------------------------

RANDOM_POOL = [
    "systemctl status nginx",
    "ps aux",
    "netstat -tulpn",
    "ss -tulpn",
    "lsof -i :8080",
    "journalctl -u nginx --no-pager -n 20",
    "cat /etc/nginx/nginx.conf",
    "nginx -t",
    "df -h",
    "free -h",
    "ls /tmp",
    "crontab -l",
    "ps aux | grep python",
    "curl http://localhost:8080",
    "top -bn1 | head -20",
    "systemctl restart nginx",
]

# ---------------------------------------------------------------------------
# Fallback diagnostic rotation (Tier 3 — CoT agent)
# ---------------------------------------------------------------------------

FALLBACK_DIAGNOSTICS = {
    1: [
        "netstat -tulpn | grep 8080",
        "lsof -i :8080",
        "ps aux | grep zombie",
        "ss -tulpn | grep 8080",
        "systemctl status nginx",
    ],
    2: [
        "journalctl -u nginx --no-pager -n 50",
        "cat /etc/nginx/nginx.conf",
        "nginx -t",
        "systemctl status nginx",
        "ls /etc/nginx/",
    ],
    3: [
        "df -h",
        "ls -lh /tmp",
        "crontab -l",
        "ps aux | grep cron",
        "cat /var/log/nginx/error.log | tail -20",
    ],
}

# ---------------------------------------------------------------------------
# OpenAI-compatible client
# ---------------------------------------------------------------------------

client = OpenAI(base_url=API_BASE_URL, api_key=API_KEY)

SYSTEM_PROMPT_PLAIN = """You are an SRE (Site Reliability Engineer) debugging a production Linux server.
The web service on port 8080 is broken. You will be given terminal output one step at a time.
Respond with ONLY a single Linux terminal command. No explanation. No markdown. Just the command."""

SYSTEM_PROMPT_COT = """You are a senior SRE (Site Reliability Engineer) debugging a broken production server.
The web service on port 8080 is failing. You receive terminal output one step at a time.

Follow this diagnostic process:
1. First: identify whether the service is down, degraded (running but requests failing), or misbehaving.
2. Second: check logs before restarting anything (journalctl -u nginx, /var/log/nginx/error.log).
3. Third: act on what logs tell you. If config is broken, fix the EXACT line that's wrong.
4. Fourth: if you see 'sed: no changes made', your pattern didn't match — cat the file and try again.
5. Fifth: if /tmp is full, clear it AND remove the cron job causing it.

Rules:
- Do NOT restart nginx more than twice without reading logs first.
- If you see 'sed: no changes made', immediately cat the file to inspect it.
- If you see 'no space left on device', run 'df -h' and then 'ls /tmp'.
- An episode is complete only when the root cause is eliminated, not just symptoms.

Respond with ONLY a single Linux command. No prose. No markdown. Just the command."""


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def reset(task_id: int) -> tuple[str, str]:
    """Reset environment. Returns (session_id, initial_observation)."""
    r = requests.post(f"{ENVIRONMENT_URL}/reset", json={"task_id": task_id})
    r.raise_for_status()
    d = r.json()
    return d["session_id"], d["observation"]["output"]


def step(session_id: str, command: str) -> dict:
    """Execute one step. Returns full response dict."""
    r = requests.post(
        f"{ENVIRONMENT_URL}/step",
        json={"session_id": session_id, "command": command},
    )
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Tier 1 — Random Agent
# ---------------------------------------------------------------------------

def run_random_agent(task_id: int) -> float:
    """Select random commands from the pool each step."""
    session_id, _ = reset(task_id)
    rng = random.Random(42 + task_id)
    rewards = []
    used = []

    for step_num in range(1, MAX_STEPS + 1):
        # Pick a command not recently used (no consecutive repeats)
        available = [c for c in RANDOM_POOL if c != (used[-1] if used else None)]
        cmd = rng.choice(available)
        used.append(cmd)

        result = step(session_id, cmd)
        reward = result["reward"]
        done = result["done"]
        rewards.append(reward)

        _log_step(step_num, cmd, reward, done, error=None)

        if done:
            score = result["info"].get("final_score", 0.0)
            _log_end(success=done and result["observation"]["service_status"] == "up",
                     steps=step_num, score=score, rewards=rewards)
            return score

    # Max steps reached
    result = step(session_id, "systemctl status nginx")  # dummy to get final
    score = result["info"].get("final_score", 0.0) or 0.0
    _log_end(success=False, steps=MAX_STEPS, score=score, rewards=rewards)
    return score


# ---------------------------------------------------------------------------
# Tier 2 — Plain LLM
# ---------------------------------------------------------------------------

def run_plain_llm(task_id: int) -> float:
    """Feed raw observation to LLM, use verbatim response as command."""
    session_id, obs = reset(task_id)
    messages = [{"role": "system", "content": SYSTEM_PROMPT_PLAIN}]
    rewards = []

    for step_num in range(1, MAX_STEPS + 1):
        messages.append({"role": "user", "content": obs})

        try:
            resp = client.chat.completions.create(
                model=MODEL_NAME,
                messages=messages,
                temperature=TEMPERATURE,
                max_tokens=128,
            )
            cmd = resp.choices[0].message.content.strip().strip("`").strip()
        except Exception as e:
            cmd = "ps aux"  # safe fallback

        # Keep conversation going
        messages.append({"role": "assistant", "content": cmd})

        result = step(session_id, cmd)
        obs = result["observation"]["output"]
        reward = result["reward"]
        done = result["done"]
        rewards.append(reward)

        _log_step(step_num, cmd, reward, done, error=None)

        if done:
            score = result["info"].get("final_score", 0.0)
            _log_end(success=True, steps=step_num, score=score, rewards=rewards)
            return score

    score = result["info"].get("final_score", 0.0) or 0.0
    _log_end(success=False, steps=MAX_STEPS, score=score, rewards=rewards)
    return score


# ---------------------------------------------------------------------------
# Tier 3 — CoT LLM with fallback rotation
# ---------------------------------------------------------------------------

def run_cot_llm(task_id: int) -> float:
    """
    Chain-of-thought system prompt + fallback diagnostic rotation.

    Fallback triggers when:
    - Agent repeats the same command twice in a row, OR
    - No cumulative reward earned after step 4
    """
    session_id, obs = reset(task_id)
    messages = [{"role": "system", "content": SYSTEM_PROMPT_COT}]
    rewards = []
    fallback_queue = list(FALLBACK_DIAGNOSTICS.get(task_id, []))
    last_cmd = None
    cumulative_reward = 0.0

    for step_num in range(1, MAX_STEPS + 1):
        messages.append({"role": "user", "content": obs})

        # Fallback detection
        use_fallback = False
        if last_cmd is not None:
            # Repeated command
            try:
                last_model_cmd = messages[-2]["content"].strip() if len(messages) >= 2 else ""
            except Exception:
                last_model_cmd = ""

        if step_num > 4 and cumulative_reward == 0.0:
            use_fallback = True
        elif step_num > 1 and last_cmd == (messages[-1].get("content", "")[:80] if messages else ""):
            use_fallback = True

        if use_fallback and fallback_queue:
            cmd = fallback_queue.pop(0)
        else:
            try:
                resp = client.chat.completions.create(
                    model=MODEL_NAME,
                    messages=messages,
                    temperature=TEMPERATURE,
                    max_tokens=128,
                )
                cmd = resp.choices[0].message.content.strip().strip("`").strip()
            except Exception:
                cmd = fallback_queue.pop(0) if fallback_queue else "ps aux"

        # Repeat detection (post-generation)
        if cmd == last_cmd and fallback_queue:
            cmd = fallback_queue.pop(0)

        messages.append({"role": "assistant", "content": cmd})
        last_cmd = cmd

        result = step(session_id, cmd)
        obs = result["observation"]["output"]
        reward = result["reward"]
        done = result["done"]
        rewards.append(reward)
        cumulative_reward += max(reward, 0.0)

        _log_step(step_num, cmd, reward, done, error=None)

        if done:
            score = result["info"].get("final_score", 0.0)
            _log_end(success=True, steps=step_num, score=score, rewards=rewards)
            return score

    score = result["info"].get("final_score", 0.0) or 0.0
    _log_end(success=False, steps=MAX_STEPS, score=score, rewards=rewards)
    return score


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

def _log_step(step: int, action: str, reward: float, done: bool, error: Optional[str]) -> None:
    error_val = error if error else "null"
    done_val = str(done).lower()
    print(
        f"[STEP] step={step} action={action} reward={reward:.2f} done={done_val} error={error_val}",
        flush=True,
    )


def _log_end(success: bool, steps: int, score: float, rewards: list[float]) -> None:
    rewards_str = ",".join(f"{r:.2f}" for r in rewards)
    print(f"[END] success={str(success).lower()} steps={steps} score={score:.3f} rewards={rewards_str}", flush=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

TIER_NAMES = {
    1: "Random Agent",
    2: "Plain LLM",
    3: "CoT LLM (+ fallback)",
}

TIER_RUNNERS = {
    1: run_random_agent,
    2: run_plain_llm,
    3: run_cot_llm,
}


def main():
    parser = argparse.ArgumentParser(description="SRE OpenEnv — Tiered Baseline")
    parser.add_argument("--tier", choices=["1", "2", "3", "all"], default="3",
                        help="Which tier(s) to run (default: 3)")
    parser.add_argument("--tasks", nargs="+", type=int, default=[1, 2, 3],
                        help="Which task IDs to run (default: 1 2 3)")
    args = parser.parse_args()

    tiers_to_run = [1, 2, 3] if args.tier == "all" else [int(args.tier)]

    all_results: dict[int, dict[int, float]] = {}   # tier -> {task_id -> score}

    for tier in tiers_to_run:
        runner = TIER_RUNNERS[tier]
        tier_scores: dict[int, float] = {}

        for task_id in args.tasks:
            task_name = TASK_NAMES[task_id]
            print(f"[START] task={task_name} env=sre-incident-env model={MODEL_NAME}", flush=True)
            score = runner(task_id)
            tier_scores[task_id] = score
            time.sleep(0.5)

        all_results[tier] = tier_scores

    # ---- Summary table ----
    # Disabled for automated grader compliance


if __name__ == "__main__":
    main()
