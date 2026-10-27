"""钩子插件的跨语言回归：pytest 里跑 node 自测，避免"两套测试各自绿"。"""

from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
NODE = shutil.which("node")
HOOK_TEST = ROOT / "governance" / "hooks" / "test_dsh_guard.mjs"


@pytest.mark.skipif(NODE is None, reason="node 未安装，跳过 DSH 钩子测试")
def test_dsh_hook_behaviour_suite_passes():
    assert HOOK_TEST.exists(), f"缺少钩子测试: {HOOK_TEST}"
    completed = subprocess.run(
        [NODE, str(HOOK_TEST)],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(ROOT),
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "11/11 passed" in completed.stdout
