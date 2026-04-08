"""
Task 2 — Config Failure

The nginx config file has a syntax error. Nginx fails on start with a clear
error in the logs pointing to a specific line. The agent must read the config,
fix the error with sed or echo, verify with nginx -t, and restart.

Difficulty: Medium
"""

import random
from datetime import datetime, timedelta
from typing import Any


PARAM_SPACE = {
    "error_type": ["missing_semicolon", "extra_brace", "invalid_directive"],
    "line_number": [8, 12, 18],
}


def sample_params(seed: int) -> dict[str, Any]:
    rng = random.Random(seed)
    error_type = rng.choice(PARAM_SPACE["error_type"])
    minutes_ago = rng.randint(2, 25)
    ts = (datetime.utcnow() - timedelta(minutes=minutes_ago)).strftime("%Y/%m/%d %H:%M:%S")
    return {"error_type": error_type, "log_timestamp": ts}


def _build_broken_config(error_type: str) -> tuple[str, int, str]:
    """Return (broken_config, error_line_number, error_description)."""
    if error_type == "missing_semicolon":
        # Missing semicolon after server_name
        config = """\
user www-data;
worker_processes auto;
pid /run/nginx.pid;

events {
    worker_connections 768;
}

http {
    sendfile on;
    tcp_nopush on;

    server {
        listen 8080 default_server;
        server_name localhost
        root /var/www/html;

        location / {
            return 200 'OK';
            add_header Content-Type text/plain;
        }
    }
}
"""
        return config, 15, "unexpected '}' in /etc/nginx/nginx.conf:15"

    elif error_type == "extra_brace":
        config = """\
user www-data;
worker_processes auto;
pid /run/nginx.pid;

events {
    worker_connections 768;
}

http {
    sendfile on;
    tcp_nopush on;

    server {
        listen 8080 default_server;
        server_name localhost;
        root /var/www/html;

        location / {
            return 200 'OK';
            add_header Content-Type text/plain;
        }
    }
}
}
"""
        return config, 24, "unexpected '}' in /etc/nginx/nginx.conf:24"

    else:  # invalid_directive
        config = """\
user www-data;
worker_processes auto2;
pid /run/nginx.pid;

events {
    worker_connections 768;
}

http {
    sendfile on;
    tcp_nopush on;

    server {
        listen 8080 default_server;
        server_name localhost;
        root /var/www/html;

        location / {
            return 200 'OK';
            add_header Content-Type text/plain;
        }
    }
}
"""
        return config, 2, "invalid value \"auto2\" in /etc/nginx/nginx.conf:2"


def build_state(params: dict[str, Any]) -> dict[str, Any]:
    error_type = params["error_type"]
    ts = params["log_timestamp"]

    broken_config, error_line, error_desc = _build_broken_config(error_type)

    error_log = (
        f"{ts} [emerg] 5678#5678: {error_desc}\n"
        f"{ts} [error] 5678#5678: nginx: configuration file /etc/nginx/nginx.conf test failed\n"
    )

    processes = [
        {"pid": 1,    "user": "root",    "cpu": "0.0", "mem": "0.0", "vsz": "168380", "rss": "13396", "stat": "Ss", "start": "Apr07", "time": "0:01", "command": "/sbin/init"},
        {"pid": 512,  "user": "root",    "cpu": "0.0", "mem": "0.0", "vsz": "0",      "rss": "0",     "stat": "S<", "start": "Apr07", "time": "0:00", "command": "[kworker/0:1H]"},
        {"pid": 1104, "user": "root",    "cpu": "0.0", "mem": "0.1", "vsz": "72296",  "rss": "6132",  "stat": "Ss", "start": "Apr07", "time": "0:00", "command": "/usr/sbin/sshd -D"},
        {"pid": 2210, "user": "root",    "cpu": "0.0", "mem": "0.0", "vsz": "14440",  "rss": "3548",  "stat": "Ss", "start": "Apr07", "time": "0:00", "command": "/usr/sbin/cron -f"},
        {"pid": 31000,"user": "root",    "cpu": "0.0", "mem": "0.2", "vsz": "21932",  "rss": "3916",  "stat": "Ss", "start": "Apr07", "time": "0:00", "command": "/lib/systemd/systemd --user"},
    ]

    return {
        "processes": processes,
        "filesystem": {
            "/etc/nginx/nginx.conf": broken_config,
            "/var/log/nginx/error.log": error_log,
            "/var/log/nginx/access.log": "",
            "/tmp": {},
            "/var/run": {},
            "/etc/cron.d": {"cron.daily": "# standard cron"},
        },
        "ports": {},  # port 8080 is FREE — nginx just fails to start
        "service_status": {
            "nginx": (
                f"● nginx.service - A high performance web server\n"
                f"   Loaded: loaded (/lib/systemd/system/nginx.service; enabled)\n"
                f"   Active: failed (Result: exit-code) since {ts}\n"
                f"  Process: 5678 ExecStartPre=/usr/sbin/nginx -t (code=exited, status=1/FAILURE)\n"
                f"Main PID: 5678 (code=exited, status=1/FAILURE)\n\n"
                f"nginx[5678]: nginx: {error_desc}\n"
                f"nginx[5678]: nginx: configuration file /etc/nginx/nginx.conf test failed\n"
                f"systemd[1]: nginx.service: Control process exited with error code.\n"
                f"systemd[1]: Failed to start A high performance web server.\n"
            )
        },
        "disk_usage": {
            "/":    {"size": "50G",  "used": "12G",  "avail": "36G",  "use%": "25%", "mount": "/"},
            "/tmp": {"size": "1.0G", "used": "64M",  "avail": "960M", "use%": "6%",  "mount": "/tmp"},
        },
        "memory_stats": {
            "total": "7.7G", "used": "1.1G", "free": "5.2G",
            "shared": "48M", "buff_cache": "1.4G", "available": "6.4G",
        },
        "crontab": [],
        "nginx_config_valid": False,
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
    flags = state.get("flags", {})
    healthy = state.get("service_healthy", False)
    config_ok = state.get("nginx_config_valid", False)
    config_edited = flags.get("config_file_edited", False)

    if healthy and config_ok:
        return 1.0
    if config_ok and not healthy:
        return 0.50   # M1 + M2 earned (config fixed), M3 not reached
    if config_edited and not config_ok:
        return 0.20   # M2 triggered (wrote to config) but fix was wrong
    return 0.0
