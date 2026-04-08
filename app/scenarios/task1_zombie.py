"""
Task 1 — Zombie Process

A zombie process is holding port 8080. Nginx cannot bind to the port and
fails on restart. The agent must identify the zombie PID, kill it, then
restart nginx.

Difficulty: Easy
"""

import random
from datetime import datetime, timedelta
from typing import Any


PARAM_SPACE = {
    "zombie_pid": (20000, 40000),
    "zombie_user": ["www-data", "nginx", "deploy", "app"],
}


def sample_params(seed: int) -> dict[str, Any]:
    rng = random.Random(seed)
    pid = rng.randint(20000, 40000)
    user = rng.choice(PARAM_SPACE["zombie_user"])
    # Randomise timestamp offsets so logs look different each session
    minutes_ago = rng.randint(3, 30)
    ts = (datetime.utcnow() - timedelta(minutes=minutes_ago)).strftime("%Y/%m/%d %H:%M:%S")
    return {"zombie_pid": pid, "zombie_user": user, "log_timestamp": ts}


def build_state(params: dict[str, Any]) -> dict[str, Any]:
    pid = params["zombie_pid"]
    user = params["zombie_user"]
    ts = params["log_timestamp"]

    nginx_conf = """\
user www-data;
worker_processes auto;
pid /run/nginx.pid;

events {
    worker_connections 768;
}

http {
    sendfile on;
    tcp_nopush on;
    types_hash_max_size 2048;

    include /etc/nginx/mime.types;
    default_type application/octet-stream;

    server {
        listen 8080 default_server;
        server_name localhost;

        location / {
            return 200 'OK';
            add_header Content-Type text/plain;
        }
    }
}
"""

    error_log = (
        f"{ts} [error] 12345#12345: bind() to 0.0.0.0:8080 failed "
        f"(98: Address already in use)\n"
        f"{ts} [emerg] 12345#12345: still could not bind\n"
        f"{ts} [error] 12345#12345: nginx: [emerg] bind() to [::]:8080 failed "
        f"(98: Address already in use)\n"
    )

    access_log = f"{ts} GET / HTTP/1.1 - -\n"

    processes = [
        # PID  USER      PR   VSZ    RSS  STAT  COMMAND
        {"pid": 1,    "user": "root",    "cpu": "0.0", "mem": "0.0", "vsz": "168380", "rss": "13396", "stat": "Ss", "start": "Apr07", "time": "0:01", "command": "/sbin/init"},
        {"pid": 512,  "user": "root",    "cpu": "0.0", "mem": "0.0", "vsz": "0",      "rss": "0",     "stat": "S<", "start": "Apr07", "time": "0:00", "command": "[kworker/0:1H]"},
        {"pid": 1104, "user": "root",    "cpu": "0.0", "mem": "0.1", "vsz": "72296",  "rss": "6132",  "stat": "Ss", "start": "Apr07", "time": "0:00", "command": "/usr/sbin/sshd -D"},
        {"pid": 2210, "user": "root",    "cpu": "0.0", "mem": "0.0", "vsz": "14440",  "rss": "3548",  "stat": "Ss", "start": "Apr07", "time": "0:00", "command": "/usr/sbin/cron -f"},
        {"pid": pid,  "user": user,      "cpu": "0.0", "mem": "0.0", "vsz": "0",      "rss": "0",     "stat": "Z",  "start": "Apr07", "time": "0:00", "command": f"[nginx] <defunct>"},
        {"pid": 31000,"user": "root",    "cpu": "0.0", "mem": "0.2", "vsz": "21932",  "rss": "3916",  "stat": "Ss", "start": "Apr07", "time": "0:00", "command": "/lib/systemd/systemd --user"},
    ]

    return {
        "processes": processes,
        "filesystem": {
            "/etc/nginx/nginx.conf": nginx_conf,
            "/var/log/nginx/error.log": error_log,
            "/var/log/nginx/access.log": access_log,
            "/tmp": {},
            "/var/run": {"nginx.pid": ""},
            "/etc/cron.d": {"cron.daily": "# standard cron"},
        },
        "ports": {8080: pid},
        "service_status": {
            "nginx": (
                f"● nginx.service - A high performance web server\n"
                f"   Loaded: loaded (/lib/systemd/system/nginx.service; enabled)\n"
                f"   Active: failed (Result: exit-code) since {ts}\n"
                f"  Process: 12345 ExecStart=/usr/sbin/nginx (code=exited, status=1/FAILURE)\n"
                f"Main PID: 12345 (code=exited, status=1/FAILURE)\n\n"
                f"Apr 07 12:00:01 server nginx[12345]: nginx: [emerg] bind() to 0.0.0.0:8080 failed (98: Address already in use)\n"
                f"Apr 07 12:00:01 server systemd[1]: nginx.service: Control process exited with error code.\n"
                f"Apr 07 12:00:01 server systemd[1]: Failed to start A high performance web server.\n"
            )
        },
        "disk_usage": {
            "/":     {"size": "50G",  "used": "12G",  "avail": "36G",  "use%": "25%", "mount": "/"},
            "/tmp":  {"size": "1.0G", "used": "128M", "avail": "896M", "use%": "13%", "mount": "/tmp"},
            "/var":  {"size": "20G",  "used": "3G",   "avail": "16G",  "use%": "16%", "mount": "/var"},
        },
        "memory_stats": {
            "total": "7.7G", "used": "1.2G", "free": "4.9G",
            "shared": "64M", "buff_cache": "1.6G", "available": "6.2G",
        },
        "crontab": [],
        "nginx_config_valid": True,
        "service_healthy": False,
        "flags": {
            "destructive_action_attempted": False,
            "zombie_pid_killed": False,
            "config_file_edited": False,
            "tmp_cleared": False,
            "cron_removed": False,
            "escalation_step": 0,
        },
        "restart_count": 0,
    }


def grade(params: dict[str, Any], state: dict[str, Any]) -> float:
    """
    Score the current state against Task 1 success criteria.

    Milestone weights (applied by grader.py per-step):
      M1 Diagnostic clue found : 0.20
      M2 Root cause targeted   : 0.30
      M3 Service restored      : 0.50

    This function returns a raw 0.0–1.0 based on flags only (no efficiency).
    The environment manager applies the efficiency multiplier and penalties on top.
    """
    flags = state.get("flags", {})
    healthy = state.get("service_healthy", False)
    zombie_killed = flags.get("zombie_pid_killed", False)

    if healthy and zombie_killed:
        return 1.0
    if zombie_killed and not healthy:
        return 0.50   # M1 + M2 earned, M3 not reached
    return 0.0
