"""Writes one JSON line per Ansible event to $B86_PROGRESS_FILE.

lib/ui.sh reads these lines to draw the progress display. Without the
environment variable this plugin does nothing.
"""
import json
import os

from ansible.plugins.callback import CallbackBase


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "notification"
    CALLBACK_NAME = "progress"
    CALLBACK_NEEDS_ENABLED = True

    def __init__(self):
        super().__init__()
        path = os.environ.get("B86_PROGRESS_FILE")
        self._out = open(path, "a", buffering=1) if path else None

    def _emit(self, event):
        if self._out:
            self._out.write(json.dumps(event) + "\n")

    def _failed(self, result):
        self._emit({
            "ev": "failed",
            "task": result._task.get_name(),
            "host": result._host.get_name(),
            "msg": str(result._result.get("msg", ""))[:500],
        })

    def v2_playbook_on_play_start(self, play):
        self._emit({"ev": "play", "name": play.get_name()})

    def v2_playbook_on_task_start(self, task, is_conditional):
        self._emit({"ev": "task", "name": task.get_name()})

    def v2_runner_on_ok(self, result):
        self._emit({"ev": "ok", "changed": bool(result._result.get("changed"))})

    def v2_runner_on_failed(self, result, ignore_errors=False):
        if not ignore_errors:
            self._failed(result)

    def v2_runner_on_unreachable(self, result):
        self._failed(result)

    def v2_playbook_on_stats(self, stats):
        failed = sum(stats.failures.values()) + sum(stats.dark.values())
        self._emit({"ev": "end", "failed": failed})
