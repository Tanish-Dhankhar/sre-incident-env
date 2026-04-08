"""
Gradio demo UI for the SRE Incident Response OpenEnv.

Mounted at /demo on the FastAPI app -- same container, same port (7860).
Gives judges an interactive terminal to play any task without writing code.
"""

from __future__ import annotations

import html as _html
import gradio as gr

from app.environment import env
from app.models import SREAction

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TASK_META = {
    1: {
        "label": "Task 1 - Zombie Process [Easy]",
        "incident": "Port 8080 is held by a zombie process. nginx cannot bind and fails on restart.",
        "hint": "Recommended first command: netstat -tulpn | grep 8080",
        "color": "#22c55e",
    },
    2: {
        "label": "Task 2 - Config Failure [Medium]",
        "incident": "nginx fails to start -- a syntax error was injected into /etc/nginx/nginx.conf.",
        "hint": "Recommended first command: journalctl -u nginx --no-pager -n 50",
        "color": "#f59e0b",
    },
    3: {
        "label": "Task 3 - Resource Leak [Hard]",
        "incident": "nginx is 'active (running)' but every request returns 503. Something is wrong -- find it.",
        "hint": "Recommended first command: df -h",
        "color": "#ef4444",
    },
}

TASK_CHOICES = [m["label"] for _, m in TASK_META.items()]

WELCOME_HTML = """
<div style="background:#0d1117;border:1px solid #30363d;border-radius:8px;
            padding:20px;font-family:'JetBrains Mono',Consolas,monospace;color:#e6edf3">
  <div style="color:#58a6ff;font-size:13px;margin-bottom:8px">
    ============================================================<br>
    &nbsp;&nbsp;&nbsp;SRE Incident Response &mdash; OpenEnv Interactive Demo<br>
    ============================================================
  </div>
  <div style="color:#8b949e;font-size:12px;margin-top:12px">
    Select a task above and click <b style="color:#e6edf3">Start Incident</b> to begin.<br>
    Type Linux commands in the input box and press <b style="color:#e6edf3">Run</b> or Enter.<br><br>
    <span style="color:#3fb950">+</span> Task 1 [Easy]   &mdash; zombie process holds port 8080<br>
    <span style="color:#d29922">+</span> Task 2 [Medium] &mdash; nginx config syntax error<br>
    <span style="color:#f85149">+</span> Task 3 [Hard]   &mdash; /tmp full; nginx looks healthy but fails<br>
  </div>
</div>
"""

COMMON_COMMANDS = [
    "systemctl status nginx",
    "journalctl -u nginx --no-pager -n 30",
    "cat /etc/nginx/nginx.conf",
    "nginx -t",
    "netstat -tulpn | grep 8080",
    "lsof -i :8080",
    "df -h",
    "free -h",
    "ps aux",
    "crontab -l",
    "curl http://localhost:8080",
    "ls /tmp",
    "systemctl restart nginx",
    "crontab -r",
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _task_id_from_label(label: str) -> int:
    for tid, meta in TASK_META.items():
        if meta["label"] == label:
            return tid
    return 1


def _severity_color(reward: float) -> str:
    if reward >= 0.40:
        return "#3fb950"   # green -- milestone hit
    if reward >= 0.15:
        return "#d29922"   # amber
    if reward < 0:
        return "#f85149"   # red -- penalty
    return "#8b949e"       # grey -- no reward


def _milestone_html(milestones: dict) -> str:
    """Render milestone status badges."""
    def badge(key: str, label: str) -> str:
        info  = milestones.get(key, {})
        fired = info.get("earned", False)
        step  = info.get("step", None)
        bg     = "#1a7f37" if fired else "#21262d"
        border = "#238636" if fired else "#30363d"
        status = "[DONE]" if fired else "[    ]"
        color  = "#3fb950" if fired else "#8b949e"
        step_s = f"&nbsp;<small>step&nbsp;{step}</small>" if fired and step else ""
        return (
            f'<span style="background:{bg};border:1px solid {border};'
            f'border-radius:4px;padding:3px 10px;margin:3px;display:inline-block;'
            f'font-size:12px;color:{color}">'
            f'{status} {label}{step_s}'
            f'</span>'
        )

    m1 = badge("m1_diagnostic_clue",     "M1 +0.20")
    m2 = badge("m2_root_cause_targeted",  "M2 +0.30")
    m3 = badge("m3_service_restored",     "M3 +0.50")
    return f'<div style="margin:8px 0">{m1}&nbsp;{m2}&nbsp;{m3}</div>'


def _build_output_html(lines: list) -> str:
    body = "\n".join(lines)
    return (
        '<div style="background:#0d1117;border:1px solid #30363d;border-radius:8px;'
        'padding:16px;font-family:\'JetBrains Mono\',Consolas,monospace;'
        'font-size:12px;line-height:1.6;max-height:500px;overflow-y:auto">'
        + body
        + "</div>"
    )


def _score_bar_html(score, step: int, done: bool) -> str:
    if score is not None:
        pct   = int(score * 100)
        color = "#3fb950" if score >= 0.8 else "#d29922" if score >= 0.5 else "#f85149"
        label = f"FINAL SCORE: {score:.3f}"
        done_tag = "&ensp;<b style='color:#3fb950'>EPISODE COMPLETE</b>" if done else ""
    else:
        pct      = 0
        color    = "#58a6ff"
        label    = f"Step {step} / 30"
        done_tag = ""

    return (
        f'<div style="font-family:\'JetBrains Mono\',Consolas,monospace;font-size:12px;'
        f'color:#8b949e;margin-bottom:4px">{label}{done_tag}</div>'
        f'<div style="background:#21262d;border-radius:4px;height:8px;overflow:hidden">'
        f'<div style="background:{color};width:{pct}%;height:100%;transition:width 0.3s"></div>'
        f'</div>'
    )


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

def _empty_session() -> dict:
    return {
        "session_id": None,
        "task_id": 1,
        "step": 0,
        "done": False,
        "final_score": None,
        "milestones": {},
        "penalties": 0.0,
        "terminal_lines": [],
    }


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------

def handle_start(task_label: str):
    """Reset environment and display the initial incident alert."""
    task_id = _task_id_from_label(task_label)
    meta    = TASK_META[task_id]

    session_id, obs = env.reset(task_id=task_id)

    state = _empty_session()
    state["session_id"] = session_id
    state["task_id"]    = task_id

    lines = []
    lines.append(
        f'<div style="color:{meta["color"]};font-weight:bold;font-size:13px;margin-bottom:8px">'
        f'======================================================<br>'
        f'{_html.escape(meta["label"].upper())}<br>'
        f'======================================================</div>'
    )
    lines.append(
        f'<div style="margin:4px 0"><span style="color:#f0b429;font-weight:bold">INCIDENT</span>'
        f'&nbsp;<span style="color:#e6edf3">{_html.escape(meta["incident"])}</span></div>'
    )
    lines.append(
        f'<div style="margin:4px 0"><span style="color:#8b949e">HINT</span>'
        f'&nbsp;<span style="color:#8b949e">{_html.escape(meta["hint"])}</span></div>'
    )
    lines.append('<hr style="border:none;border-top:1px solid #30363d;margin:10px 0">')
    lines.append(
        f'<div style="color:#e6edf3">{obs.output.replace(chr(10), "<br>")}</div>'
    )
    state["terminal_lines"] = lines

    return (
        state,
        _build_output_html(lines),
        _milestone_html({}),
        _score_bar_html(None, 0, False),
        gr.update(interactive=True, value=""),
        gr.update(interactive=True),
        gr.update(interactive=True),
    )


def handle_command(command: str, state: dict):
    """Execute one terminal command in the environment."""
    if not state.get("session_id"):
        return (
            state,
            _build_output_html(['<span style="color:#f85149">No active session. Click Start Incident first.</span>']),
            _milestone_html({}),
            _score_bar_html(None, 0, False),
            "",
        )

    if state["done"]:
        lines = state["terminal_lines"] + [
            '<span style="color:#8b949e">Episode complete. Start a new incident to play again.</span>'
        ]
        return (
            state,
            _build_output_html(lines),
            _milestone_html(state["milestones"]),
            _score_bar_html(state["final_score"], state["step"], True),
            "",
        )

    command = command.strip()
    if not command:
        return (
            state,
            _build_output_html(state["terminal_lines"]),
            _milestone_html(state["milestones"]),
            _score_bar_html(state["final_score"], state["step"], state["done"]),
            "",
        )

    obs, reward, done = env.step(state["session_id"], SREAction(command=command))

    state["step"]      = obs.step
    state["done"]      = done
    state["penalties"] = obs.info.get("penalties", 0.0)

    # Normalise milestone keys
    raw_m = obs.info.get("milestones", {})
    norm  = {}
    for k, v in raw_m.items():
        if isinstance(v, dict):
            norm[k] = {
                "earned": v.get("fired", v.get("earned", False)),
                "step":   v.get("step"),
                "reward": v.get("reward", 0.0),
            }
    state["milestones"] = norm

    if done:
        state["final_score"] = obs.info.get("final_score")

    lines = state["terminal_lines"]

    # -- Prompt line --
    rc    = _severity_color(reward)
    rs    = f"+{reward:.2f}" if reward > 0 else f"{reward:.2f}"
    lines.append(
        f'<div style="margin-top:10px;margin-bottom:2px">'
        f'<span style="color:#58a6ff">user@sre-env</span>'
        f'<span style="color:#8b949e">:~$</span> '
        f'<span style="color:#e6edf3;font-weight:bold">{_html.escape(command)}</span>'
        f'&ensp;<span style="color:{rc};font-size:11px">[{rs}]</span>'
        f'</div>'
    )

    # -- Command output --
    fmt = _html.escape(obs.output)
    fmt = fmt.replace("WARNING",  '<span style="color:#f85149;font-weight:bold">WARNING</span>')
    fmt = fmt.replace("DISCOVERED", '<span style="color:#d29922;font-weight:bold">DISCOVERED</span>')
    fmt = fmt.replace("FIX APPLIED", '<span style="color:#3fb950;font-weight:bold">FIX APPLIED</span>')
    fmt = fmt.replace("MONITORING", '<span style="color:#58a6ff;font-weight:bold">MONITORING</span>')
    fmt = fmt.replace("--- Incident Journal ---", '<span style="color:#58a6ff">--- Incident Journal ---</span>')
    fmt = fmt.replace("------------------------", '<span style="color:#8b949e">------------------------</span>')
    fmt = fmt.replace("\n", "<br>")
    lines.append(f'<div style="color:#c9d1d9;margin-left:16px;margin-bottom:4px">{fmt}</div>')

    # -- Episode complete banner --
    if done and state["final_score"] is not None:
        sc  = state["final_score"]
        col = "#3fb950" if sc >= 0.8 else "#d29922" if sc >= 0.5 else "#f85149"
        rating = "EXCELLENT" if sc >= 0.9 else "PARTIAL" if sc >= 0.5 else "FAILED"
        lines.append(
            f'<div style="border:1px solid {col};border-radius:6px;padding:12px;'
            f'margin-top:14px;background:#0d1117;text-align:center">'
            f'<div style="color:{col};font-size:14px;font-weight:bold;letter-spacing:2px">'
            f'EPISODE COMPLETE &mdash; {rating}</div>'
            f'<div style="color:#e6edf3;font-size:28px;font-weight:bold;margin:8px 0">'
            f'{sc:.3f}</div>'
            f'<div style="color:#8b949e;font-size:11px">Steps: {state["step"]} / 30</div>'
            f'</div>'
        )

    state["terminal_lines"] = lines
    return (
        state,
        _build_output_html(lines),
        _milestone_html(state["milestones"]),
        _score_bar_html(state["final_score"], state["step"], done),
        "",
    )


def handle_quick_cmd(cmd: str, state: dict):
    """Run a command from the quick-commands dropdown."""
    if not cmd or not state.get("session_id"):
        return (
            state,
            _build_output_html(state.get("terminal_lines", [])),
            _milestone_html(state.get("milestones", {})),
            _score_bar_html(state.get("final_score"), state.get("step", 0), state.get("done", False)),
            cmd,
        )
    return handle_command(cmd, state)


# ---------------------------------------------------------------------------
# Build Gradio layout
# ---------------------------------------------------------------------------

def build_demo() -> gr.Blocks:
    with gr.Blocks(title="SRE Incident Response -- OpenEnv Demo") as demo:

        session_state = gr.State(_empty_session())

        # -- Header --
        gr.HTML("""
        <div style="background:#0d1117;border:1px solid #30363d;border-radius:8px;
                    padding:20px 28px;margin-bottom:12px">
          <div style="display:flex;align-items:center;gap:16px">
            <div>
              <h1 style="color:#e6edf3;margin:0;font-size:20px;font-weight:700;
                         font-family:'JetBrains Mono',Consolas,monospace">
                SRE Incident Response
              </h1>
              <p style="color:#8b949e;margin:4px 0 0;font-size:12px;
                        font-family:'JetBrains Mono',Consolas,monospace">
                OpenEnv &nbsp;|&nbsp; Interactive Demo &nbsp;|&nbsp; Pure-Python Linux Simulator
              </p>
            </div>
            <div style="margin-left:auto;text-align:right;font-family:'JetBrains Mono',Consolas,monospace">
              <div style="color:#3fb950;font-size:12px">STATUS: LIVE</div>
              <div style="color:#8b949e;font-size:11px">Max 30 steps / episode</div>
            </div>
          </div>
        </div>
        """)

        with gr.Row():
            # -- Left: terminal --
            with gr.Column(scale=3):
                with gr.Row():
                    task_dd = gr.Dropdown(
                        choices=TASK_CHOICES,
                        value=TASK_CHOICES[0],
                        label="Select Task",
                        scale=3,
                    )
                    start_btn = gr.Button("Start Incident", variant="primary", scale=1)

                terminal_out = gr.HTML(value=WELCOME_HTML)

                with gr.Row():
                    cmd_input = gr.Textbox(
                        placeholder="Enter a Linux command and press Enter...",
                        label="Command",
                        scale=4,
                        interactive=False,
                        container=True,
                    )
                    run_btn = gr.Button("Run", variant="secondary", scale=1, interactive=False)

                quick_dd = gr.Dropdown(
                    choices=[""] + COMMON_COMMANDS,
                    value="",
                    label="Quick commands (click to run)",
                    interactive=False,
                )

            # -- Right: scoring panel --
            with gr.Column(scale=1, min_width=230):
                gr.HTML('<div style="color:#58a6ff;font-size:12px;font-weight:bold;'
                        'font-family:monospace;margin-bottom:4px">MILESTONES</div>')
                milestone_html = gr.HTML(_milestone_html({}))

                gr.HTML('<hr style="border:none;border-top:1px solid #30363d;margin:10px 0">')

                gr.HTML('<div style="color:#58a6ff;font-size:12px;font-weight:bold;'
                        'font-family:monospace;margin-bottom:4px">PROGRESS</div>')
                score_html = gr.HTML(_score_bar_html(None, 0, False))

                gr.HTML('<hr style="border:none;border-top:1px solid #30363d;margin:10px 0">')

                gr.HTML("""
                <div style="font-family:'JetBrains Mono',Consolas,monospace;font-size:11px;
                            color:#8b949e;line-height:1.8">
                  <b style="color:#c9d1d9">Reward</b><br>
                  M1 Diagnostic clue &nbsp; +0.20<br>
                  M2 Root cause fix  &nbsp;&nbsp; +0.30<br>
                  M3 Service restored &nbsp; +0.50<br>
                  <br>
                  <b style="color:#c9d1d9">Efficiency</b><br>
                  &lt;= 10 steps &nbsp; x1.00<br>
                  &lt;= 16 steps &nbsp; x0.90<br>
                  &lt;= 22 steps &nbsp; x0.80<br>
                  &lt;= 30 steps &nbsp; x0.70<br>
                  <br>
                  <b style="color:#f85149">Penalties</b><br>
                  Pre-log restart &nbsp; -0.15<br>
                  Shotgun restart &nbsp; -0.10<br>
                  Destructive cmd &nbsp; -0.15<br>
                </div>
                """)

        # -- Event wiring --
        start_out = [session_state, terminal_out, milestone_html, score_html,
                     cmd_input, run_btn, quick_dd]
        step_out  = [session_state, terminal_out, milestone_html, score_html, cmd_input]

        start_btn.click(fn=handle_start, inputs=[task_dd], outputs=start_out)
        run_btn.click(fn=handle_command, inputs=[cmd_input, session_state], outputs=step_out)
        cmd_input.submit(fn=handle_command, inputs=[cmd_input, session_state], outputs=step_out)
        quick_dd.change(fn=handle_quick_cmd, inputs=[quick_dd, session_state], outputs=step_out)

    return demo


demo = build_demo()
