"""Real-code A/B: who macOS holds responsible for the gateway Hermes starts, before and after the
starting app quits. TEMPORARY probe, never merge.

Terminal.app stands in for Hermes Desktop (a runner cannot grant Local Network to an arbitrary app,
and it does not enforce Local Network at all: run 40263330950). Legs, each started from a
Terminal-run script exactly like Desktop runs its bounded CLI clients:
  ensure   `hermes gateway ensure --json` (Desktop's launch path; spawn_unmanaged_gateway)
  restart  the update-time watcher (gateway.launch_detached_profile_gateway_restart)

argv: outdir checkout
"""
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)
REPO = Path(sys.argv[2]).resolve()
PY = sys.executable
UHOME = Path(os.environ["RUNNER_TEMP"]) / "u"
HHOME = UHOME / ".hermes"
HHOME.mkdir(parents=True, exist_ok=True)
(HHOME / "config.yaml").write_text("gateway:\n  multiplex_profiles: false\n", encoding="utf-8")
ENV = {**os.environ, "HOME": str(UHOME), "HERMES_HOME": str(HHOME)}


def sh(cmd, timeout=60, env=None):
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, check=False, stdin=subprocess.DEVNULL, env=env)
        return proc.returncode, (proc.stdout + proc.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, "TIMEOUT"


def log(msg):
    print(msg, flush=True)
    with (OUT / "driver.log").open("a", encoding="utf-8") as fh:
        fh.write(msg + "\n")


def responsible(pid):
    _, text = sh(["sudo", "-n", "launchctl", "procinfo", str(pid)])
    return [ln.strip() for ln in text.splitlines() if "responsible" in ln.lower()] or [text[-200:]]


def proc_line(pid):
    return sh(["ps", "-o", "pid=,ppid=,command=", "-p", str(pid)])[1][:220]


def gateway_pid():
    for name in ("gateway.pid", "gateway_state.json"):
        try:
            text = (HHOME / name).read_text(encoding="utf-8")
        except OSError:
            continue
        match = re.search(r'"pid"\s*:\s*(\d+)', text) or re.match(r"\s*(\d+)", text)
        if match:
            pid = int(match.group(1))
            if sh(["kill", "-0", str(pid)])[0] == 0:
                return pid
    return None


def wait(pred, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = pred()
        if value:
            return value
        time.sleep(0.5)
    return None


def in_terminal(leg, body):
    marker = OUT / f"{leg}.done"
    script = OUT / f"{leg}.command"
    exports = " ".join(f"{k}={shlex.quote(v)}" for k, v in (("HOME", str(UHOME)), ("HERMES_HOME", str(HHOME))))
    script.write_text(f"#!/bin/bash\ncd {shlex.quote(str(REPO))}\nexport {exports}\n{body}\n"
                      f"echo $? > {shlex.quote(str(marker))}\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    sh(["killall", "Terminal"])
    sh(["open", "-a", "Terminal", str(script)])
    return wait(lambda: marker.exists() and marker.stat().st_size, 180) and marker.read_text().strip()


def quit_terminal():
    sh(["killall", "-TERM", "Terminal"])
    if not wait(lambda: sh(["pgrep", "-x", "Terminal"])[0] != 0, 15):
        sh(["killall", "-KILL", "Terminal"])
    time.sleep(2)


def leg(name, body, *, previous=None):
    status = in_terminal(name, body)
    pid = wait(lambda: (p := gateway_pid()) and p != previous and p, 120)
    if not pid:
        log(f"RESULT {json.dumps({'leg': name, 'error': 'no gateway', 'script_exit': status})}")
        for f in ("ensure.json", "ensure.err", "restart.out"):
            if (OUT / f).exists():
                log(f"--- {f}\n{(OUT / f).read_text(errors='replace')[-2000:]}")
        log("--- gateway-stdio.log tail\n" + sh(["tail", "-40", str(HHOME / "logs" / "gateway-stdio.log")])[1])
        return None
    parent = int(sh(["ps", "-o", "ppid=", "-p", str(pid)])[1] or 0)
    before = responsible(pid)
    quit_terminal()
    alive = sh(["kill", "-0", str(pid)])[0] == 0
    log("RESULT " + json.dumps({
        "leg": name, "script_exit": status, "gateway": proc_line(pid), "parent": proc_line(parent),
        "responsible_before_quit": before, "responsible_after_quit": responsible(pid),
        "gateway_alive_after_quit": alive,
        "xpc_service_name": sh(["/bin/sh", "-c", f"ps eww -o command= -p {pid} | tr ' ' '\\n' | grep '^XPC_SERVICE_NAME=' || true"])[1],
    }))
    return pid


log(f"FACT {sh(['sw_vers'])[1]}")
log(f"FACT checkout {sh(['git', '-C', str(REPO), 'log', '--oneline', '-1'])[1]}")
first = leg("ensure", f"{shlex.quote(PY)} -m hermes_cli.main gateway ensure --json "
                      f"> {shlex.quote(str(OUT / 'ensure.json'))} 2> {shlex.quote(str(OUT / 'ensure.err'))}")
if first:
    snippet = (f"import os, signal; from hermes_cli import gateway; "
               f"print(gateway.launch_detached_profile_gateway_restart('default', {first})); "
               f"os.kill({first}, signal.SIGTERM)")
    leg("restart", f"{shlex.quote(PY)} -c {shlex.quote(snippet)} > {shlex.quote(str(OUT / 'restart.out'))} 2>&1",
        previous=first)
sh([PY, "-m", "hermes_cli.main", "gateway", "stop"], timeout=90, env=ENV)
log(f"FACT launchd runtime jobs left: {sh(['/bin/sh', '-c', 'launchctl list | grep com.nousresearch.hermes.runtime || true'])[1]!r}")
log("DONE")
