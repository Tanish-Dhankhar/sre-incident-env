"""
Task 3 — Resource Leak with Time Pressure and Red Herrings

A rogue cron job fills /tmp with large files. Nginx reports as 'active (running)'
but fails to write temporary files for proxied requests — failures are at the
application layer, not the service layer.

The deception: nginx status says active. curl returns 503, not connection refused.
The time pressure: every 5 steps, if /tmp is not cleared, disk usage escalates further.
Red herrings: 2 of 3 distracting symptoms are active per session.

Difficulty: Hard
"""

import random
import string
from datetime import datetime, timedelta
from typing import Any


PARAM_SPACE = {
    "cron_schedule": ["* * * * *", "*/2 * * * *"],
    "red_herrings": ["A", "B", "C"],  # pick 2
}


def _random_suffix(rng: random.Random, length: int = 6) -> str:
    return "".join(rng.choices(string.ascii_uppercase + string.digits, k=length))


def sample_params(seed: int) -> dict[str, Any]:
    rng = random.Random(seed)
    cron_schedule = rng.choice(PARAM_SPACE["cron_schedule"])
    suffix = _random_suffix(rng)
    junk_filename = f"cache_dump_{suffix}.tmp"
    # Pick 2 of 3 red herrings
    active_red_herrings = rng.sample(["A", "B", "C"], 2)
    minutes_ago = rng.randint(5, 45)
    ts = (datetime.utcnow() - timedelta(minutes=minutes_ago)).strftime("%Y/%m/%d %H:%M:%S")
    return {
        "cron_schedule": cron_schedule,
        "junk_filename": junk_filename,
        "active_red_herrings": active_red_herrings,
        "log_timestamp": ts,
    }


def build_state(params: dict[str, Any]) -> dict[str, Any]:
    cron_schedule = params["cron_schedule"]
    junk_filename = params["junk_filename"]
    active_rh = params["active_red_herrings"]
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
    proxy_temp_path /tmp/nginx_temp;
    client_body_temp_path /tmp/nginx_body;

    server {
        listen 8080 default_server;
        server_name localhost;

        location / {
            proxy_pass http://127.0.0.1:8081;
            proxy_read_timeout 5s;
        }
    }
}
"""

    error_log = (
        f"{ts} [error] 7788#7788: *1 upstream timed out (110: Connection timed out) "
        f"while reading response header from upstream, client: 127.0.0.1, "
        f"server: localhost, request: \"GET / HTTP/1.1\"\n"
        f"{ts} [crit] 7788#7788: *2 open() \"/tmp/nginx_temp/1/00/0000000001\" "
        f"failed (28: No space left on device) while reading upstream\n"
        f"{ts} [error] 7788#7788: *3 failed to open temp file: No space left on device\n"
        f"{ts} [error] 7788#7788: *4 upstream timed out (110: Connection timed out)\n"
    )

    # Base processes — normal system
    processes = [
        {"pid": 1,    "user": "root",    "cpu": "0.0", "mem": "0.0", "vsz": "168380", "rss": "13396", "stat": "Ss", "start": "Apr07", "time": "0:01", "command": "/sbin/init"},
        {"pid": 512,  "user": "root",    "cpu": "0.0", "mem": "0.0", "vsz": "0",      "rss": "0",     "stat": "S<", "start": "Apr07", "time": "0:00", "command": "[kworker/0:1H]"},
        {"pid": 1104, "user": "root",    "cpu": "0.0", "mem": "0.1", "vsz": "72296",  "rss": "6132",  "stat": "Ss", "start": "Apr07", "time": "0:00", "command": "/usr/sbin/sshd -D"},
        {"pid": 2210, "user": "root",    "cpu": "0.0", "mem": "0.0", "vsz": "14440",  "rss": "3548",  "stat": "Ss", "start": "Apr07", "time": "0:00", "command": "/usr/sbin/cron -f"},
        # Nginx master + worker (running! this is the deception)
        {"pid": 7788, "user": "root",    "cpu": "0.1", "mem": "0.1", "vsz": "55680",  "rss": "4312",  "stat": "Ss", "start": "Apr07", "time": "0:02", "command": "nginx: master process /usr/sbin/nginx"},
        {"pid": 7790, "user": "www-data","cpu": "0.0", "mem": "0.1", "vsz": "56048",  "rss": "2108",  "stat": "S",  "start": "Apr07", "time": "0:00", "command": "nginx: worker process"},
    ]

    # Red herring A: high memory usage
    mem_stats = {
        "total": "7.7G", "used": "1.2G", "free": "4.9G",
        "shared": "64M", "buff_cache": "1.6G", "available": "6.2G",
    }
    if "A" in active_rh:
        mem_stats = {
            "total": "7.7G", "used": "6.7G", "free": "0.4G",
            "shared": "512M", "buff_cache": "0.6G", "available": "0.7G",
        }

    # Red herring B: suspicious process
    if "B" in active_rh:
        processes.append({
            "pid": 9432, "user": "root", "cpu": "14.9", "mem": "1.2",
            "vsz": "43200", "rss": "9800", "stat": "R", "start": "Apr07",
            "time": "2:17", "command": f"python3 /tmp/monitor.py",
        })

    # Red herring C: stale lock file
    var_run = {"nginx.pid": "7788"}
    if "C" in active_rh:
        var_run["nginx.pid.lock"] = ""

    cron_entry = f"{cron_schedule} root /bin/bash -c 'dd if=/dev/urandom of=/tmp/{junk_filename} bs=1M count=100 2>/dev/null'"

    return {
        "processes": processes,
        "filesystem": {
            "/etc/nginx/nginx.conf": nginx_conf,
            "/var/log/nginx/error.log": error_log,
            "/var/log/nginx/access.log": "",
            "/tmp": {junk_filename: "[binary data: 1.0G]"},
            "/var/run": var_run,
            "/etc/cron.d": {"rogue": cron_entry},
        },
        "ports": {8080: 7788},
        "service_status": {
            # DECEPTION: nginx shows active even though requests fail
            "nginx": (
                f"● nginx.service - A high performance web server\n"
                f"   Loaded: loaded (/lib/systemd/system/nginx.service; enabled)\n"
                f"   Active: active (running) since {ts}\n"
                f" Main PID: 7788 (nginx)\n"
                f"   Status: \"Worker process\"\n\n"
                f"nginx[7788]: upstream timed out while reading response header\n"
                f"nginx[7788]: failed to open temp file: No space left on device\n"
            )
        },
        "disk_usage": {
            "/":    {"size": "50G",  "used": "30G",  "avail": "18G",  "use%": "63%", "mount": "/"},
            "/tmp": {"size": "1.0G", "used": "1.0G", "avail": "0",    "use%": "100%","mount": "/tmp"},
        },
        "memory_stats": mem_stats,
        "crontab": [cron_entry],
        "nginx_config_valid": True,
        "service_healthy": False,   # requests fail despite nginx being "up"
        "flags": {
            "destructive_action_attempted": False,
            "zombie_pid_killed": False,
            "config_file_edited": False,
            "tmp_cleared": False,
            "cron_removed": False,
            "escalation_step": 0,
        },
        "restart_count": 0,
        # Task 3 specific: killing monitor.py has no effect
        "_rh_active": active_rh,
    }


def apply_time_pressure(state: dict[str, Any], step: int) -> None:
    """
    Called every step. If /tmp is not cleared, incrementally worsen the situation.
    Modeled as 4 escalation levels, each triggering every 5 steps.
    """
    if state["flags"].get("tmp_cleared", False):
        return  # escalation stopped

    escalation = step // 5  # 0,1,2,3+
    state["flags"]["escalation_step"] = escalation

    disk = state["disk_usage"]["/tmp"]
    error_log = state["filesystem"]["/var/log/nginx/error.log"]

    import datetime
    ts = datetime.datetime.utcnow().strftime("%Y/%m/%d %H:%M:%S")

    if escalation == 1 and disk["use%"] == "100%":
        disk.update({"used": "1.05G", "avail": "0", "use%": "105%"})
        state["filesystem"]["/var/log/nginx/error.log"] += (
            f"{ts} [crit] 7788#7788: *8 open() \"/tmp/nginx_temp/2/00/0000000002\" "
            f"failed (28: No space left on device)\n"
        )

    elif escalation == 2 and "105%" in disk.get("use%", ""):
        disk.update({"used": "1.10G", "use%": "110%"})
        state["filesystem"]["/var/log/nginx/error.log"] += (
            f"{ts} [crit] 7788#7788: *15 failed to open temp file: No space left on device (repeated)\n"
            f"{ts} [error] 7788#7788: *16 upstream timed out (repeating every 30s)\n"
        )

    elif escalation >= 3 and "110%" in disk.get("use%", ""):
        disk.update({"used": "1.15G", "use%": "115%"})
        # Nginx status degrades to warning
        state["service_status"]["nginx"] = (
            "● nginx.service - A high performance web server\n"
            "   Loaded: loaded (/lib/systemd/system/nginx.service; enabled)\n"
            "   Active: active (running) since earlier\n"
            " Main PID: 7788 (nginx)\n"
            "   Status: \"Worker degraded — disk full, 502 errors\"\n\n"
            f"{ts} [alert] 7788#7788: *20 no live upstreams — switching to backup\n"
        )


def grade(params: dict[str, Any], state: dict[str, Any]) -> float:
    """
    Task 3 grader — strictest.

    Requires:
      - service_healthy=True  (requests succeed after fix)
      - tmp_cleared=True      (disk freed)
      - cron_removed=True     (root cause eliminated)

    Partial credit is given for fixing symptoms without root cause.
    """
    flags = state.get("flags", {})
    healthy = state.get("service_healthy", False)
    tmp_cleared = flags.get("tmp_cleared", False)
    cron_removed = flags.get("cron_removed", False)

    if healthy and tmp_cleared and cron_removed:
        return 1.0
    if healthy and tmp_cleared and not cron_removed:
        # Fixed symptoms, not cause — cron will fill /tmp again next minute
        return 0.65
    if tmp_cleared and not healthy:
        return 0.50  # cleared disk, service not confirmed restored
    if cron_removed and not tmp_cleared:
        return 0.30  # removed cron job but didn't clear existing junk
    return 0.0
