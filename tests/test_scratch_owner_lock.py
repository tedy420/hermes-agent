"""Scratch prune ownership comes from OS-held locks, not from the spawn ledger's records."""
import contextlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from hermes_constants import get_scratch_dir, prune_scratch_dir
from tests.waiting import wait_until

REPO = str(Path(__file__).resolve().parents[1])
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _age(*paths: Path) -> None:
    ancient = time.time() - 30 * 3600
    for path in paths:
        os.utime(path, (ancient, ancient))


def _alive(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


@pytest.mark.platforms("any")
def test_a_running_gateway_keeps_its_shells_entry_when_the_spawn_ledger_is_lost(tmp_path):
    """A gateway registered at start runs a shell inside an idle scratch entry; the spawn ledger is
    then lost (corrupt file, quarantined on the next read). The gateway is alive, so the prune
    must still leave its shell and that entry alone."""
    scratch = get_scratch_dir(tmp_path, prune=False)
    lane = scratch / "gateway-lane"
    lane.mkdir()
    ready = tmp_path / "gateway.json"
    gateway = subprocess.Popen(
        [sys.executable, "-c",
         f"import json, os, subprocess, sys, time; sys.path.insert(0, {REPO!r}); "
         "from hermes_cli.process_identity import register_self; register_self('gateway'); "
         f"sh = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'], cwd={str(lane)!r}, "
         f"stdin=subprocess.DEVNULL, creationflags={_NO_WINDOW}); "
         f"open({str(ready)!r}, 'w', encoding='utf-8').write(json.dumps([os.getpid(), sh.pid])); time.sleep(120)"],  # windows-footgun: ok — a write
        stdin=subprocess.DEVNULL, env={**os.environ, "HERMES_HOME": str(tmp_path)}, creationflags=_NO_WINDOW)
    try:
        _, shell = wait_until(lambda: ready.exists() and json.loads(ready.read_text(encoding="utf-8-sig") or "null"),
                              timeout=60, what="the registered gateway started its shell")
        (tmp_path / "spawn-ledger.json").write_text("{not json", encoding="utf-8")
        _age(lane)

        prune_scratch_dir(scratch)

        assert _alive(shell), "the live gateway's shell was killed"
        assert lane.exists(), "the entry the live gateway's shell works in was deleted"
    finally:
        with contextlib.suppress(psutil.Error):
            for child in psutil.Process(gateway.pid).children(recursive=True):
                child.kill()
        gateway.kill()
        gateway.wait(timeout=30)


@pytest.mark.platforms("windows")
def test_an_idle_entry_with_a_file_held_open_is_kept_whole(tmp_path):
    """Another process holds a file open inside an idle entry (a log tail; its cwd is elsewhere, so
    it is not ours to kill). Windows cannot delete that file, so the entry must be kept whole, not
    half-deleted around it."""
    scratch = get_scratch_dir(tmp_path, prune=False)
    entry = scratch / "held"
    entry.mkdir()
    held, sibling = entry / "held.log", entry / "sibling.txt"
    held.write_text("tail me", encoding="utf-8")
    sibling.write_text("keep me", encoding="utf-8")
    ready = tmp_path / "holder.ready"
    holder = subprocess.Popen(
        [sys.executable, "-c",
         f"import time; f = open({str(held)!r}, 'ab'); open({str(ready)!r}, 'wb').close(); time.sleep(120)"],  # windows-footgun: ok — binary
        cwd=str(tmp_path), stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    try:
        wait_until(ready.exists, timeout=60, what="the holder opened the file")
        _age(held, sibling, entry)

        assert prune_scratch_dir(scratch) == 0

        assert sibling.exists() and held.exists(), "the entry was half-deleted"
        assert holder.poll() is None, "a process that only holds a file open was killed"
    finally:
        holder.kill()
        holder.wait(timeout=30)


def _registered_gateway(tmp_path, lane, then=""):
    """A process that calls ``register_self`` (as the gateway does), runs a shell inside *lane*,
    then runs *then* in-process. Returns (process, shell pid)."""
    ready = tmp_path / "gateway.json"
    home = tmp_path / ".hermes"
    gateway = subprocess.Popen(
        [sys.executable, "-c",
         f"import json, os, subprocess, sys, time; sys.path.insert(0, {REPO!r}); "
         "from hermes_cli.process_identity import register_self; register_self('gateway'); "
         f"sh = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'], cwd={str(lane)!r}, "
         f"stdin=subprocess.DEVNULL); {then + '; ' if then else ''}"
         f"open({str(ready)!r}, 'w', encoding='utf-8').write(json.dumps(sh.pid)); time.sleep(120)"],  # windows-footgun: ok — a write
        stdin=subprocess.DEVNULL, env={**os.environ, "HOME": str(tmp_path), "HERMES_HOME": str(home)})
    shell = wait_until(lambda: ready.exists() and json.loads(ready.read_text(encoding="utf-8-sig") or "null"),
                       timeout=60, what="the registered gateway started its shell")
    return gateway, shell


def _stop(gateway):
    with contextlib.suppress(psutil.Error):
        for child in psutil.Process(gateway.pid).children(recursive=True):
            child.kill()
    gateway.kill()
    gateway.wait(timeout=30)


@pytest.mark.platforms("posix")
def test_a_gateway_that_clones_its_profile_and_reads_its_own_lock_stays_an_owner(tmp_path):
    """POSIX record locks (lockf) drop when the holder closes ANY descriptor of the file. A gateway
    that clones the default profile with clone_all (serve/TUI do it in-process) or reads its own
    lock file must stay an owner: its shell and idle entry are kept, and the clone carries no
    locks of the source's processes."""
    home = tmp_path / ".hermes"
    (home / "cache" / "scratch").mkdir(parents=True)
    (home / "config.yaml").write_text("model: {}\n", encoding="utf-8")
    lane = get_scratch_dir(home, prune=False) / "gateway-lane"
    lane.mkdir()
    gateway, shell = _registered_gateway(tmp_path, lane, then=(
        "from hermes_cli import profiles; profiles.create_profile(name='clonex', clone_from='default', clone_all=True); "
        "import glob; [open(p, 'rb').read() for p in glob.glob(os.path.join(os.environ['HERMES_HOME'], 'locks', '*', '*'))]"))
    try:
        _age(lane)

        prune_scratch_dir(lane.parent)

        assert _alive(shell), "the live gateway's shell was killed"
        assert lane.exists(), "the entry the live gateway's shell works in was deleted"
        assert not (home / "profiles" / "clonex" / "locks").exists(), "clone_all copied the source's locks"
    finally:
        _stop(gateway)


@pytest.mark.platforms("posix")
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores directory permissions")
def test_owner_locks_that_cannot_be_taken_make_the_prune_reap_nothing(tmp_path):
    """The gateway could not take its owner lock (``locks/`` not writable), so the owners dir holds
    nothing. That is \"unknown\", not \"nobody\": the prune must leave the gateway's shell and its
    idle entry alone and say why in the audit log."""
    home = tmp_path / ".hermes"
    lane = get_scratch_dir(home, prune=False) / "gateway-lane"
    lane.mkdir(parents=True)
    (home / "locks").mkdir()
    os.chmod(home / "locks", 0o555)
    try:
        gateway, shell = _registered_gateway(tmp_path, lane)
        try:
            _age(lane)

            prune_scratch_dir(lane.parent)

            assert _alive(shell), "the gateway's shell was killed although ownership was unknown"
            assert lane.exists()
            log = (home / "logs" / "scratch-prune.log").read_text(encoding="utf-8-sig")
            assert "scratch owner locks unavailable" in log, log
        finally:
            _stop(gateway)
    finally:
        os.chmod(home / "locks", 0o755)
