from app.models import SREAction, SREObservation, SREState
print("models OK")

from app.scenarios.base import build_initial_state
from app.scenarios import task1_zombie, task2_config, task3_resource_leak

params, state = build_initial_state(task1_zombie, "test-session-123")
print(f"Task1 params: pid={params['zombie_pid']}, user={params['zombie_user']}")
print(f"Processes: {len(state['processes'])}")
print(f"Port 8080 -> PID {state['ports'].get(8080)}")
print("Task1 scenario OK")

params2, state2 = build_initial_state(task2_config, "test-session-456")
print(f"Task2 error_type: {params2['error_type']}")
print(f"Task2 config_valid: {state2['nginx_config_valid']}")
print("Task2 scenario OK")

params3, state3 = build_initial_state(task3_resource_leak, "test-session-789")
print(f"Task3 cron: {params3['cron_schedule']}")
print(f"Task3 red herrings: {params3['active_red_herrings']}")
print(f"Task3 /tmp use: {state3['disk_usage']['/tmp']['use%']}")
print("Task3 scenario OK")

from app import simulator
out = simulator.execute("ps aux", state, params, task_id=1)
print(f"\nps aux output ({len(out)} chars):")
print(out[:300])
print("\nsimulator OK")

from app import grader as grader_mod
gs = grader_mod.initial_grader_state()
# Simulate kill of zombie
out_kill = simulator.execute(f"kill -9 {params['zombie_pid']}", state, params, task_id=1)
step_reward = grader_mod.step_grade(1, f"kill -9 {params['zombie_pid']}", out_kill, state, params, gs, 2)
print(f"\nkill step_reward: {step_reward}")
print(f"zombie_pid_killed flag: {state['flags']['zombie_pid_killed']}")
print(f"port 8080 free: {8080 not in state['ports']}")
print("grader OK")
