"""
Linux Simulator â€” the fake terminal.

Takes a raw command string, reads/mutates the per-session state dictionary,
and returns output that looks exactly like a real Linux server terminal.

The agent never knows it's fake.
"""

from __future__ import annotations

import re
import shlex
from typing import Any

# ---------------------------------------------------------------------------
# Safety patterns â€” these commands are blocked regardless of task
# ---------------------------------------------------------------------------

_DESTRUCTIVE_PATTERNS = [
    r"^rm\s+-rf\s+/$",
    r"^rm\s+-rf\s+/etc",
    r"^rm\s+-rf\s+/usr",
    r"^rm\s+-rf\s+/var$",
    r"^rm\s+-rf\s+/bin",
    r"^rm\s+-rf\s+/sbin",
    r"^kill\s+(-\d+\s+)?1$",
    r"^kill\s+-9\s+1$",
    r"^:\(\)\s*\{",            # fork bomb
    r"dd\s+.*of=/dev/",
    r"mkfs\.",
]


def _is_destructive(cmd: str) -> bool:
    for pattern in _DESTRUCTIVE_PATTERNS:
        if re.search(pattern, cmd.strip(), re.IGNORECASE):
            return True
    return False


# ---------------------------------------------------------------------------
# PS AUX formatter
# ---------------------------------------------------------------------------

def _format_ps_aux(processes: list[dict]) -> str:
    header = "USER         PID %CPU %MEM    VSZ   RSS TTY      STAT START   TIME COMMAND"
    lines = [header]
    for p in processes:
        lines.append(
            f"{p['user']:<12} {p['pid']:>5} {p['cpu']:>4} {p['mem']:>4} "
            f"{p['vsz']:>6} {p['rss']:>5} ?        {p['stat']:<4} {p['start']:<7} {p['time']:<7} {p['command']}"
        )
    return "\n".join(lines)


def _format_ps_ef(processes: list[dict]) -> str:
    header = "UID          PID    PPID  C STIME TTY          TIME CMD"
    lines = [header]
    for p in processes:
        lines.append(
            f"{p['user']:<12} {p['pid']:>5}       1  0 {p['start']} ?        {p['time']} {p['command']}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Individual command handlers
# ---------------------------------------------------------------------------

def _handle_ps(args: list[str], state: dict) -> str:
    processes = state["processes"]
    raw = " ".join(args)
    if "-ef" in raw or "-e" in raw:
        return _format_ps_ef(processes)
    return _format_ps_aux(processes)


def _handle_netstat(state: dict) -> str:
    ports = state["ports"]
    lines = [
        "Active Internet connections (only servers)",
        "Proto Recv-Q Send-Q Local Address           Foreign Address         State       PID/Program name",
    ]
    for port, pid in ports.items():
        # find process name
        name = "unknown"
        for p in state["processes"]:
            if p["pid"] == pid:
                name = p["command"].split()[0].split("/")[-1]
                break
        lines.append(f"tcp        0      0 0.0.0.0:{port}          0.0.0.0:*               LISTEN      {pid}/{name}")
        lines.append(f"tcp6       0      0 :::{port}               :::*                    LISTEN      {pid}/{name}")
    return "\n".join(lines)


def _handle_ss(state: dict) -> str:
    ports = state["ports"]
    lines = ["Netid  State   Recv-Q Send-Q      Local Address:Port         Peer Address:Port  Process"]
    for port, pid in ports.items():
        name = "unknown"
        for p in state["processes"]:
            if p["pid"] == pid:
                name = p["command"].split()[0].split("/")[-1]
                break
        lines.append(f"tcp    LISTEN  0      128               0.0.0.0:{port}             0.0.0.0:*     users:((\"{name}\",pid={pid},fd=6))")
    return "\n".join(lines)


def _handle_lsof(args: list[str], state: dict) -> str:
    # lsof -i :8080
    port = None
    raw = " ".join(args)
    m = re.search(r":(\d+)", raw)
    if m:
        port = int(m.group(1))

    if port is None:
        return "lsof: missing port argument"

    pid = state["ports"].get(port)
    if pid is None:
        return ""  # nothing holding that port

    name = "unknown"
    for p in state["processes"]:
        if p["pid"] == pid:
            name = p["command"].split()[0].split("/")[-1]
            break

    return (
        f"COMMAND     PID     USER   FD   TYPE DEVICE SIZE/OFF NODE NAME\n"
        f"{name:<10} {pid:>5}  {state['processes'][0]['user']:<6}   6u  IPv4  12345      0t0  TCP *:{port} (LISTEN)\n"
        f"{name:<10} {pid:>5}  {state['processes'][0]['user']:<6}   7u  IPv6  12346      0t0  TCP *:{port} (LISTEN)"
    )


def _handle_systemctl_status(service: str, state: dict) -> str:
    svc = state["service_status"].get(service)
    if svc is None:
        return f"Unit {service}.service could not be found."
    return svc


def _handle_systemctl_restart(service: str, state: dict, task_id: int) -> str:
    if service != "nginx":
        return f"Failed to restart {service}.service: Unit not found."

    state["restart_count"] = state.get("restart_count", 0) + 1

    port_free = 8080 not in state["ports"]
    config_ok = state.get("nginx_config_valid", True)

    if port_free and config_ok:
        # ---- success ----
        state["service_healthy"] = True
        state["service_status"]["nginx"] = (
            "â— nginx.service - A high performance web server\n"
            "   Loaded: loaded (/lib/systemd/system/nginx.service; enabled)\n"
            "   Active: active (running) since just now\n"
            " Main PID: 9999 (nginx)\n"
            "   Status: \"Starting worker process\"\n"
        )
        state["ports"][8080] = 9999
        # add nginx worker to process list
        state["processes"].append({
            "pid": 9999, "user": "www-data", "cpu": "0.0", "mem": "0.1",
            "vsz": "55680", "rss": "2108", "stat": "S", "start": "now",
            "time": "0:00", "command": "nginx: worker process",
        })
        return ""  # systemctl restart silent on success
    else:
        msg_parts = []
        if not port_free:
            msg_parts.append("bind() to 0.0.0.0:8080 failed (98: Address already in use)")
        if not config_ok:
            msg_parts.append("nginx: configuration file /etc/nginx/nginx.conf test failed")
        err_detail = "; ".join(msg_parts)
        # append to error log
        import datetime
        ts = datetime.datetime.utcnow().strftime("%Y/%m/%d %H:%M:%S")
        state["filesystem"]["/var/log/nginx/error.log"] += f"{ts} [emerg] 0#0: {err_detail}\n"
        state["service_status"]["nginx"] = (
            "â— nginx.service - A high performance web server\n"
            "   Loaded: loaded (/lib/systemd/system/nginx.service; enabled)\n"
            f"   Active: failed (Result: exit-code)\n"
            f"  Process: ExecStart=/usr/sbin/nginx (code=exited, status=1/FAILURE)\n\n"
            f"nginx: [emerg] {err_detail}\n"
        )
        return (
            "Job for nginx.service failed because the control process exited with error code.\n"
            f"nginx: [emerg] {err_detail}\n"
            "See \"journalctl -xe\" for details."
        )


def _handle_systemctl_stop(service: str, state: dict) -> str:
    if service != "nginx":
        return f"Failed to stop {service}.service: Unit not found."
    state["service_healthy"] = False
    state["service_status"]["nginx"] = (
        "â— nginx.service - A high performance web server\n"
        "   Loaded: loaded (/lib/systemd/system/nginx.service; enabled)\n"
        "   Active: inactive (dead)\n"
    )
    # Remove nginx worker from ports if running
    if 9999 in state["ports"]:
        del state["ports"][9999]
    return ""


def _handle_systemctl_start(service: str, state: dict, task_id: int) -> str:
    return _handle_systemctl_restart(service, state, task_id)


def _handle_journalctl(args: list[str], state: dict) -> str:
    raw = " ".join(args)
    # Figure out which service
    service = "nginx"
    m = re.search(r"-u\s+(\S+)", raw)
    if m:
        service = m.group(1)

    lines_limit = None
    m = re.search(r"-n\s+(\d+)", raw)
    if m:
        lines_limit = int(m.group(1))

    if service == "nginx":
        log = state["filesystem"].get("/var/log/nginx/error.log", "-- No entries --")
        # Also show systemctl context
        svc_status = state["service_status"].get("nginx", "")
        full_log = f"-- Logs begin at Apr 07 00:00:00 UTC --\n{log}\n{svc_status}"
        lines = full_log.strip().split("\n")
        if lines_limit:
            lines = lines[-lines_limit:]
        return "\n".join(lines)
    return f"-- No journal entries for {service} --"


def _handle_cat(path: str, state: dict) -> str:
    fs = state["filesystem"]
    if path in fs:
        content = fs[path]
        if isinstance(content, str):
            return content
        return "\n".join(str(v) for v in content.values())
    # Check parent directories
    for key in fs:
        if path.startswith(key + "/"):
            child = path[len(key) + 1:]
            parent = fs[key]
            if isinstance(parent, dict) and child in parent:
                return str(parent[child])
    return f"cat: {path}: No such file or directory"


def _handle_ls(path: str, state: dict) -> str:
    fs = state["filesystem"]
    # Direct key match
    if path in fs:
        entry = fs[path]
        if isinstance(entry, dict):
            if not entry:
                return ""
            return "  ".join(entry.keys())
        return path.split("/")[-1]
    # Parent directory listing
    prefix = path.rstrip("/") + "/"
    children = []
    for key in fs:
        if key.startswith(prefix):
            remainder = key[len(prefix):]
            if "/" not in remainder:
                children.append(remainder)
    if children:
        return "  ".join(children)
    return f"ls: cannot access '{path}': No such file or directory"


def _handle_df(args: list[str], state: dict) -> str:
    disk = state["disk_usage"]
    header = "Filesystem      Size  Used Avail Use% Mounted on"
    lines = [header]
    raw = " ".join(args)
    # If specific path requested
    target = None
    for a in args:
        if a.startswith("/") and a not in ("-h",):
            target = a
            break
    for mount, info in disk.items():
        if target and not mount.startswith(target.rstrip("/")):
            continue
        lines.append(
            f"overlay         {info['size']:<5} {info['used']:<5} {info['avail']:<5} {info['use%']} {info['mount']}"
        )
    return "\n".join(lines)


def _handle_free(state: dict) -> str:
    m = state["memory_stats"]
    return (
        f"               total        used        free      shared  buff/cache   available\n"
        f"Mem:           {m['total']:>6}      {m['used']:>6}      {m['free']:>6}      {m['shared']:>5}      {m['buff_cache']:>6}      {m['available']:>6}\n"
        f"Swap:           512M          0B       512M"
    )


def _handle_top(state: dict) -> str:
    processes = state["processes"]
    mem = state["memory_stats"]
    lines = [
        "top - 12:00:00 up 1 day,  3:00,  1 user,  load average: 0.08, 0.05, 0.01",
        f"Tasks: {len(processes)} total,   1 running, {len(processes)-1} sleeping,   0 stopped,   0 zombie",
        f"%Cpu(s):  2.3 us,  0.7 sy,  0.0 ni, 96.5 id",
        f"MiB Mem :   {mem['total']} total,   {mem['free']} free,   {mem['used']} used,   {mem['buff_cache']} buff/cache",
        "",
        "  PID USER      PR  NI    VIRT    RES    SHR S  %CPU  %MEM     TIME+ COMMAND",
    ]
    for p in processes[:12]:
        lines.append(
            f"{p['pid']:>5} {p['user']:<9}  20   0 {p['vsz']:>7} {p['rss']:>6}   1024 {p['stat'][0]}  {p['cpu']:>5}  {p['mem']:>5}  {p['time']:<8} {p['command'].split()[0]}"
        )
    return "\n".join(lines)


def _handle_kill(args: list[str], state: dict, params: dict) -> str:
    raw = " ".join(args)
    # Extract numeric PID
    pids = []
    for a in args:
        a_clean = a.lstrip("-")
        if a_clean.isdigit():
            pids.append(int(a_clean))

    # Filter out signal numbers (9, 15, etc.)
    target_pids = [p for p in pids if p > 100]

    if not target_pids:
        return "kill: usage: kill [-s sigspec | -n signum | -sigspec] pid | jobspec ... or kill -l [sigspec]"

    messages = []
    zombie_pid = params.get("zombie_pid")

    for pid in target_pids:
        # Find and remove process
        before = len(state["processes"])
        state["processes"] = [p for p in state["processes"] if p["pid"] != pid]
        after = len(state["processes"])

        if after < before:
            # Process was removed â€” free any ports it held
            for port, holder in list(state["ports"].items()):
                if holder == pid:
                    del state["ports"][port]
                    if port == 8080 and pid == zombie_pid:
                        state["flags"]["zombie_pid_killed"] = True
        else:
            messages.append(f"bash: kill: ({pid}) - No such process")

    return "\n".join(messages)


def _handle_crontab(args: list[str], state: dict) -> str:
    if "-l" in args:
        entries = state.get("crontab", [])
        if not entries:
            return "no crontab for root"
        return "\n".join(entries)
    if "-r" in args:
        state["crontab"] = []
        state["flags"]["cron_removed"] = True
        return ""
    return "crontab: invalid option"


def _handle_rm(args: list[str], state: dict, task_id: int = 0) -> str:
    # Reconstruct path(s) from args, skip flags
    paths = [a for a in args if not a.startswith("-")]
    if not paths:
        return "rm: missing operand"

    messages = []
    tmp_was_cleared = state["flags"].get("tmp_cleared", False)

    for path in paths:
        # Find in filesystem
        fs = state["filesystem"]
        # Direct key
        if path in fs:
            del fs[path]
            if "/tmp" in path or path == "/tmp":
                state["flags"]["tmp_cleared"] = True
                state["disk_usage"]["/tmp"] = {
                    "size": "1.0G", "used": "0",
                    "avail": "1.0G", "use%": "0%", "mount": "/tmp",
                }
            continue
        # File inside a directory
        found = False
        for key in list(fs.keys()):
            if isinstance(fs[key], dict) and path.startswith(key + "/"):
                child = path[len(key) + 1:]
                if child in fs[key]:
                    del fs[key][child]
                    found = True
                    if key == "/tmp":
                        state["flags"]["tmp_cleared"] = True
                        state["disk_usage"]["/tmp"] = {
                            "size": "1.0G", "used": "0",
                            "avail": "1.0G", "use%": "0%", "mount": "/tmp",
                        }
                    break
        if not found:
            # Check if path is /tmp directory itself
            if path.startswith("/tmp"):
                state["filesystem"]["/tmp"] = {}
                state["flags"]["tmp_cleared"] = True
                state["disk_usage"]["/tmp"] = {
                    "size": "1.0G", "used": "0",
                    "avail": "1.0G", "use%": "0%", "mount": "/tmp",
                }
            else:
                messages.append(f"rm: cannot remove '{path}': No such file or directory")

    # Fix 2: Task 3 â€” emit a prominent warning when /tmp is newly cleared but cron is still alive.
    # This is realistic: monitoring would alert again in ~60s when cron refills disk.
    tmp_now_cleared = state["flags"].get("tmp_cleared", False)
    cron_removed = state["flags"].get("cron_removed", False)
    if task_id == 3 and tmp_now_cleared and not tmp_was_cleared and not cron_removed:
        warning = (
            "WARNING: Disk space restored but a cron job is still active\n"
            "and will refill /tmp. The episode is not complete until the\n"
            "rogue cron job is removed.\n"
            "Hint: check 'crontab -l' and remove with 'crontab -r'."
        )
        messages.insert(0, warning)

    return "\n".join(messages)


def _evaluate_nginx_config(content: str) -> bool:
    """Return True if content looks like a valid nginx config.

    Requires positive structure (not just absence of known errors).
    Short strings like 'broken config' return False immediately.
    """
    if not content or len(content.strip()) < 20:
        return False
    if "{" not in content or "}" not in content:
        return False
    # Must reference at least one nginx concept
    import re as _re
    if not _re.search(r"\b(server|listen|http|events)\b", content):
        return False
    # Invalid directive value
    if _re.search(r"worker_processes\s+auto\d+", content):
        return False
    # Unbalanced braces
    if content.count("{") != content.count("}"):
        return False
    # Extra closing brace at end
    if content.strip().endswith("}\n}") or content.strip().endswith("}\r\n}"):
        return False
    # Missing semicolon on server_name line
    if _re.search(r"^\s*server_name\s+\S+\s*$", content, _re.MULTILINE):
        return False
    return True


def _handle_echo_redirect(cmd: str, state: dict) -> str:
    """Handle: echo 'content' > /path/to/file or echo 'content' >> /path

    Task 2 truncation mechanic:
    Using echo > /etc/nginx/nginx.conf with a short/incomplete config replaces
    the file with a broken stub. The agent sees a warning and must recover.
    """
    m = re.match(r"echo\s+(.+?)\s*>{1,2}\s*(\S+)", cmd, re.DOTALL)
    if not m:
        return ""
    content_raw = m.group(1).strip("'\"")
    path = m.group(2)
    is_append = ">>" in cmd.split(path)[0]

    if is_append:
        existing = state["filesystem"].get(path, "")
        state["filesystem"][path] = existing + "\n" + content_raw
    else:
        state["filesystem"][path] = content_raw

    if "nginx" in path:
        state["flags"]["config_file_edited"] = True
        state["nginx_config_valid"] = _evaluate_nginx_config(state["filesystem"][path])
        # Truncation detection: short overwrite that produces invalid config
        if not state["nginx_config_valid"] and len(content_raw) < 200:
            return (
                "[WARNING] /etc/nginx/nginx.conf overwritten with incomplete configuration "
                f"({len(content_raw)} bytes).\n"
                f"File now contains:\n{content_raw}\n\n"
                "nginx -t will fail. You must write a complete valid nginx configuration.\n"
                "Tip: echo a full config block with events{{}} and http{{server{{}}}} sections."
            )
    return ""


def _handle_sed(cmd: str, state: dict) -> str:
    """Handle: sed -i 's/old/new/[flags]' /path â€” supports basic regex anchors."""
    m = re.match(r"sed\s+.*?'s/(.*?)/(.*?)/[gi]*'\s+(\S+)", cmd)
    if not m:
        m = re.match(r"sed\s+.*?'s/(.*?)/(.*?)/'\s+(\S+)", cmd)
    if not m:
        m = re.match(r'sed\s+.*?"s/(.*?)/(.*?)/[gi]*"\s+(\S+)', cmd)
    if not m:
        m = re.match(r'sed\s+.*?"s/(.*?)/(.*?)/"\s+(\S+)', cmd)
    if not m:
        return "sed: invalid expression"

    old, new, path = m.group(1), m.group(2), m.group(3)
    fs = state["filesystem"]
    if path not in fs:
        return f"sed: can't read {path}: No such file or directory"

    content = fs[path]
    if isinstance(content, str):
        content_before = content
        try:
            # Use re.sub with MULTILINE so ^ and $ behave as line anchors (like real sed)
            fs[path] = re.sub(old, new, content, flags=re.MULTILINE)
        except re.error:
            # Pattern is not valid regex â€” fall back to literal string replace
            fs[path] = content.replace(old, new)

        if fs[path] == content_before:
            # Pattern matched nothing â€” tell the agent explicitly so it tries a different fix
            return f"sed: no changes made to {path}"

        if "nginx" in path:
            state["flags"]["config_file_edited"] = True
            state["nginx_config_valid"] = _evaluate_nginx_config(fs[path])
    return ""


def _handle_nginx_t(state: dict) -> str:
    if state.get("nginx_config_valid", True):
        return (
            "nginx: the configuration file /etc/nginx/nginx.conf syntax is ok\n"
            "nginx: configuration file /etc/nginx/nginx.conf test is successful"
        )
    return (
        "nginx: [emerg] invalid parameter \"/etc/nginx/nginx.conf\" in /etc/nginx/nginx.conf\n"
        "nginx: configuration file /etc/nginx/nginx.conf test failed"
    )


def _handle_curl(url: str, state: dict, task_id: int) -> str:
    if "localhost:8080" not in url and "127.0.0.1:8080" not in url:
        return f"curl: (7) Failed to connect to host: Connection refused"

    escalation = state["flags"].get("escalation_step", 0)

    if state.get("service_healthy", False):
        if task_id == 3 and not state["flags"].get("tmp_cleared", False):
            # Even if healthy flag not cleared, task3 before fix
            return "HTTP/1.1 503 Service Unavailable\n\n503 Service Temporarily Unavailable\nupstream timed out"
        return "HTTP/1.1 200 OK\n\nOK"
    else:
        if task_id == 3:
            # Nginx is active but requests fail
            if escalation >= 3:
                return "HTTP/1.1 502 Bad Gateway\n\n502 Bad Gateway\nnginx"
            return "HTTP/1.1 503 Service Unavailable\n\n503 Service Temporarily Unavailable\nupstream timed out"
        # port not bound at all
        return "curl: (7) Failed to connect to 0.0.0.0 port 8080 after 0 ms: Connection refused"


def _handle_wget(url: str, state: dict, task_id: int) -> str:
    result = _handle_curl(url, state, task_id)
    if "200" in result:
        return f"--2026-04-08 12:00:00--  {url}\nHTTP request sent, awaiting response... 200 OK\nLength: 2 [text/plain]\nOK"
    return f"--2026-04-08 12:00:00--  {url}\nHTTP request sent, awaiting response... connecting... failed: Connection refused."


# ---------------------------------------------------------------------------
# Main dispatcher
# ---------------------------------------------------------------------------

def execute(cmd_raw: str, state: dict, params: dict, task_id: int) -> str:
    """
    Execute a command string against the session state.

    Returns the terminal output string.
    State is mutated in-place.
    """
    cmd = cmd_raw.strip()

    # Strip sudo prefix
    if cmd.startswith("sudo "):
        cmd = cmd[5:].strip()

    if not cmd:
        return ""

    # Safety check first
    if _is_destructive(cmd):
        state["flags"]["destructive_action_attempted"] = True
        return "bash: Permission denied (simulated safety block)"

    # Interactive editors
    if re.match(r"^(nano|vi|vim|emacs|pico)\b", cmd):
        return (
            "Interactive editors are not available in this environment.\n"
            "Use 'echo' with output redirect (>) or 'sed -i' for in-place file editing."
        )

    # Parse first word
    try:
        parts = shlex.split(cmd)
    except ValueError:
        parts = cmd.split()

    if not parts:
        return ""

    verb = parts[0]
    args = parts[1:]

    # ---- Route ----
    if verb == "ps":
        return _handle_ps(args, state)

    if verb in ("netstat",):
        return _handle_netstat(state)

    if verb == "ss":
        return _handle_ss(state)

    if verb == "lsof":
        return _handle_lsof(args, state)

    if verb == "systemctl":
        if not args:
            return "systemctl: missing sub-command"
        sub = args[0]
        service = args[1] if len(args) > 1 else "nginx"
        if sub == "status":
            return _handle_systemctl_status(service, state)
        if sub in ("restart",):
            return _handle_systemctl_restart(service, state, task_id)
        if sub == "start":
            return _handle_systemctl_start(service, state, task_id)
        if sub == "stop":
            return _handle_systemctl_stop(service, state)
        if sub in ("enable", "disable", "daemon-reload"):
            return ""  # silently succeed
        return f"Unknown systemctl sub-command: {sub}"

    if verb == "journalctl":
        return _handle_journalctl(args, state)

    if verb == "cat":
        path = args[0] if args else ""
        return _handle_cat(path, state)

    if verb == "ls":
        path = args[-1] if args and not args[-1].startswith("-") else "."
        if path in ("-l", "-la", "-lh", "-a"):
            path = "."
        return _handle_ls(path, state)

    if verb == "df":
        return _handle_df(args, state)

    if verb == "free":
        return _handle_free(state)

    if verb == "top":
        return _handle_top(state)

    if verb == "kill":
        return _handle_kill(args, state, params)

    if verb == "crontab":
        return _handle_crontab(args, state)

    if verb == "rm":
        return _handle_rm(args, state, task_id)

    if verb == "echo" and (">" in cmd):
        return _handle_echo_redirect(cmd, state)

    if verb == "echo":
        # Plain echo â€” just return the text
        return " ".join(args).strip("'\"")

    if verb == "sed":
        return _handle_sed(cmd, state)

    if verb == "nginx":
        if "-t" in args or "-T" in args:
            return _handle_nginx_t(state)
        return "nginx: [alert] could not open error log file: open() \"/var/log/nginx/error.log\" failed"

    if verb == "curl":
        url = args[0] if args else ""
        return _handle_curl(url, state, task_id)

    if verb == "wget":
        url = args[0] if args else ""
        return _handle_wget(url, state, task_id)

    if verb in ("which", "whereis"):
        binary = args[0] if args else ""
        known = {"nginx": "/usr/sbin/nginx", "python3": "/usr/bin/python3",
                 "systemctl": "/usr/bin/systemctl", "curl": "/usr/bin/curl"}
        return known.get(binary, f"{verb}: {binary}: not found")

    if verb == "hostname":
        return "sre-server-01"

    if verb == "whoami":
        return "root"

    if verb == "date":
        import datetime
        return datetime.datetime.utcnow().strftime("%a %b %d %H:%M:%S UTC %Y")

    if verb == "uptime":
        return " 12:00:00 up 1 day,  3:00,  1 user,  load average: 0.08, 0.05, 0.01"

    if verb == "uname":
        return "Linux sre-server-01 5.15.0-91-generic #101-Ubuntu SMP x86_64 GNU/Linux"

    if verb in ("clear", "reset", "history"):
        return ""

    if verb == "exit":
        return ""

    # Unknown command
    return f"bash: {verb}: command not found"
