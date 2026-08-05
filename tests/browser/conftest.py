"""A real dashboard process, with no SereneDB behind it.

See `harness.py` for what is canned and why it runs as a separate process. `Dash` can be stopped
and started on the same port, because "restart the dashboard under an open tab" is the bug this
suite exists for.
"""
import os
import socket
import subprocess
import sys
import time

import pytest

pytest.importorskip("playwright.sync_api", reason="browser tests need playwright")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Dash:
    """The dashboard as a process: start it, kill it, start it again on the same port."""

    def __init__(self, port=None):
        self.port, self.proc = port or free_port(), None

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def start(self):
        env = {**os.environ, "PYTHONPATH": ROOT, "PYTHONUNBUFFERED": "1"}
        self.proc = subprocess.Popen([sys.executable, "-m", "tests.browser.harness",
                                      str(self.port)], cwd=ROOT, env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for _ in range(200):
            if self.proc.poll() is not None:
                raise RuntimeError(f"the harness died: {self.proc.stdout.read()}")
            try:
                with socket.create_connection(("127.0.0.1", self.port), 0.2):
                    return self
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("the test dashboard never came up")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        self.proc = None


@pytest.fixture
def dash():
    d = Dash().start()
    try:
        yield d
    finally:
        d.stop()


@pytest.fixture(autouse=True)
def _fail_on_console_errors(page):
    """A JS exception fails the test rather than quietly leaving the page half-built.

    The reconnect bug rendered nothing and threw nothing, but the next one might well throw, and a
    browser test that ignores the console reports green on a broken page.
    """
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    yield
    assert not errors, f"the page threw: {errors}"
