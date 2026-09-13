"""Exercise Windows venv launchers and actual descendant PIDs, not just Popen.pid."""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from job_radar.processes import ProcessTree
from job_radar.settings import SettingsStore, init_config
from job_radar.tasks import TaskManager

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows Job Object ownership")


def wait_file(path):
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            time.sleep(0.05)
    pytest.fail(f"Worker did not become ready: {path.name}")


def test_venv_launcher_is_suspended_until_ownership_is_registered(tmp_path, monkeypatch):
    marker = tmp_path / "executed.json"
    initialize = ProcessTree.__init__

    def delayed_registration(self, process):
        time.sleep(0.3)
        assert not marker.exists(), "Child code ran before its process tree was owned"
        initialize(self, process)

    monkeypatch.setattr(ProcessTree, "__init__", delayed_registration)
    tree = ProcessTree.spawn([sys.executable, "-c",
        "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('true')", str(marker)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert tree.process.wait(timeout=8) == 0
        assert wait_file(marker) is True
    finally:
        tree.close()


def test_deadline_kills_real_worker_and_descendants_only(tmp_path, monkeypatch):
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]

    child_script = tmp_path / "child.py"
    child_script.write_text("import os,time,pathlib,sys\npathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\ntime.sleep(60)\n", encoding="utf-8")
    worker_script = tmp_path / "worker.py"
    worker_script.write_text("""import json,os,pathlib,subprocess,sys,time
root = pathlib.Path(sys.argv[1])
child = subprocess.Popen([sys.executable, str(root/'child.py'), str(root/'child-pid.json')])
while not (root/'child-pid.json').exists():
    time.sleep(0.01)
payload = {'worker': os.getpid(), 'child_launcher': child.pid}
(root/'worker-pids.json').write_text(json.dumps(payload))
time.sleep(60)
""", encoding="utf-8")
    spawn = ProcessTree.spawn
    independent = spawn([sys.executable, "-c", "import time; time.sleep(60)"],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    handles = []
    try:
        def unresponsive_worker(_command, **kwargs):
            return spawn([sys.executable, str(worker_script), str(tmp_path)], **kwargs)

        monkeypatch.setattr(ProcessTree, "spawn", unresponsive_worker)
        settings = SettingsStore(init_config(tmp_path / "config.local.yaml", str(tmp_path / "jobs.sqlite3")))
        cfg, revision = settings.read()
        cfg.service.stop_timeout_seconds = 1
        settings.save(cfg.model_dump(mode="json"), revision)
        with TaskManager(settings) as manager:
            task = manager.start("collect", {"dry_run": True})
            process, tree = manager._workers[task["id"]]
            pids = wait_file(tmp_path / "worker-pids.json")
            pids["child"] = wait_file(tmp_path / "child-pid.json")
            pids["launcher"] = process.pid
            for pid in set(pids.values()):
                handle = kernel.OpenProcess(0x1000 | 0x100000, False, pid)  # QUERY_LIMITED_INFORMATION | SYNCHRONIZE
                assert handle, f"PID {pid} is not alive"
                handles.append(handle)
                owned = wintypes.BOOL()
                assert kernel.IsProcessInJob(handle, tree.job, ctypes.byref(owned))
                assert owned.value, f"PID {pid} escaped the task's Job Object"
            manager.stop(task["id"])
            process.wait(timeout=8)
            for handle in handles:
                assert kernel.WaitForSingleObject(handle, 5000) == 0, "A real worker or descendant survived stop"
            deadline = time.monotonic() + 5
            while manager.store.get(task["id"])["status"] == "stopping" and time.monotonic() < deadline:
                time.sleep(0.05)
            assert manager.store.get(task["id"])["status"] == "cancelled"
            assert independent.process.poll() is None
    finally:
        for handle in handles:
            kernel.CloseHandle(handle)
        independent.close()
        independent.process.wait(timeout=5)
