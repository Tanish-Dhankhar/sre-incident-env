"""Full integration tests for all 3 tasks and the shotgun penalty."""
import requests

BASE = "http://localhost:7860"


def run_task(task_id, commands, label=""):
    r = requests.post(f"{BASE}/reset", json={"task_id": task_id})
    d = r.json()
    sid = d["session_id"]
    print(f"\n=== Task {task_id}: {d['task_name']} {label} ===")

    for cmd in commands:
        r = requests.post(f"{BASE}/step", json={"session_id": sid, "command": cmd})
        s = r.json()
        out = s["observation"]["output"]
        encoded_out = out.encode("ascii", errors="replace").decode("ascii")[:80]
        print(f"  $ {cmd}")
        print(f"    reward={s['reward']}  done={s['done']}  svc={s['observation']['service_status']}")
        print(f"    out: {encoded_out}")
        if s["done"]:
            ms = s["info"]["milestones"]
            print(f"  >> FINAL SCORE: {s['info'].get('final_score')}")
            print(f"  >> M1={ms['m1_diagnostic_clue']['earned']} M2={ms['m2_root_cause_targeted']['earned']} M3={ms['m3_service_restored']['earned']}")
            print(f"  >> Penalties: {ms['penalties']}")
            return True
    return False


# ===== Task 1 — Zombie Process =====
run_task(1, [
    "lsof -i :8080",
    "kill -9 {AUTO_ZOMBIE_PID}",  # will be replaced below
    "systemctl restart nginx",
])

# Proper Task 1 test — extract PID from lsof output
import re
r = requests.post(f"{BASE}/reset", json={"task_id": 1})
d = r.json(); sid = d["session_id"]
print(f"\n=== Task 1: Zombie Process (PID-aware) ===")
r2 = requests.post(f"{BASE}/step", json={"session_id": sid, "command": "lsof -i :8080"})
s2 = r2.json()
out2 = s2["observation"]["output"]
pid_m = re.search(r"\s(\d{4,6})\s", out2)
zombie_pid = pid_m.group(1) if pid_m else "99999"
print(f"  lsof found zombie PID: {zombie_pid}  reward={s2['reward']}")
r3 = requests.post(f"{BASE}/step", json={"session_id": sid, "command": f"kill -9 {zombie_pid}"})
s3 = r3.json()
print(f"  kill reward={s3['reward']}")
r4 = requests.post(f"{BASE}/step", json={"session_id": sid, "command": "systemctl restart nginx"})
s4 = r4.json()
print(f"  restart reward={s4['reward']} done={s4['done']} final_score={s4['info'].get('final_score')}")

# ===== Task 2 — Config Failure (test all 3 error types) =====
# The session_id determines error type (SHA-256 of session_id -> seed -> error_type)
# We run 3 sessions and they'll each get different error types based on their UUID

good_t2 = False
for attempt in range(6):
    r = requests.post(f"{BASE}/reset", json={"task_id": 2})
    d = r.json(); sid = d["session_id"]

    # Read what error we have
    r_j = requests.post(f"{BASE}/step", json={"session_id": sid, "command": "journalctl -u nginx -n 5"})
    journal = r_j.json()["observation"]["output"]
    r_c = requests.post(f"{BASE}/step", json={"session_id": sid, "command": "cat /etc/nginx/nginx.conf"})
    config = r_c.json()["observation"]["output"]

    # Determine error type and fix
    if "worker_processes auto2" in config:
        fix_cmd = "sed -i 's/worker_processes auto2/worker_processes auto/' /etc/nginx/nginx.conf"
        etype = "invalid_directive"
    elif config.count("}") > config.count("{"):
        # Extra brace — rewrite config inline (simplified valid config)
        fix_cmd = (
            "echo 'user www-data;\\nworker_processes auto;\\npid /run/nginx.pid;\\n\\n"
            "events {\\n    worker_connections 768;\\n}\\n\\nhttp {\\n    sendfile on;\\n\\n"
            "    server {\\n        listen 8080 default_server;\\n        server_name localhost;\\n\\n"
            "        location / {\\n            return 200 OK;\\n        }\\n    }\\n}' > /etc/nginx/nginx.conf"
        )
        etype = "extra_brace"
    else:
        fix_cmd = "sed -i 's/server_name localhost$/server_name localhost;/' /etc/nginx/nginx.conf"
        etype = "missing_semicolon"

    print(f"\n=== Task 2 attempt {attempt+1}: {etype} ===")

    r_fix = requests.post(f"{BASE}/step", json={"session_id": sid, "command": fix_cmd})
    s_fix = r_fix.json()
    print(f"  fix: reward={s_fix['reward']}")

    r_t = requests.post(f"{BASE}/step", json={"session_id": sid, "command": "nginx -t"})
    s_t = r_t.json()
    print(f"  nginx -t: {s_t['observation']['output'][:80]}")

    r_rs = requests.post(f"{BASE}/step", json={"session_id": sid, "command": "systemctl restart nginx"})
    s_rs = r_rs.json()
    print(f"  restart: reward={s_rs['reward']} done={s_rs['done']} score={s_rs['info'].get('final_score')}")

    if s_rs["done"]:
        good_t2 = True
        print(f"  >> Task 2 ({etype}) PASSED with score={s_rs['info'].get('final_score')}")
        break

# ===== Task 3 — Resource Leak =====
run_task(3, [
    "systemctl status nginx",
    "curl http://localhost:8080",
    "df -h",
    "ls /tmp",
    "crontab -l",
    "rm -rf /tmp/*",
    "crontab -r",
])

# ===== Shotgun Penalty =====
print("\n=== Shotgun Restart Penalty (Task 1) ===")
r = requests.post(f"{BASE}/reset", json={"task_id": 1})
d = r.json(); sid = d["session_id"]
for i in range(6):
    r2 = requests.post(f"{BASE}/step", json={"session_id": sid, "command": "systemctl restart nginx"})
    s = r2.json()
    print(f"  restart #{i+1}: reward={s['reward']}  penalties={s['info'].get('milestones',{}).get('penalties',0)}")

print("\nAll tests complete.")
