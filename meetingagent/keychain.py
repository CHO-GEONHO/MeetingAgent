"""macOS Keychain에서 토큰 읽기/쓰기. 평문 파일에 키를 두지 않기 위함.

환경변수 MEETINGAGENT_<NAME>_TOKEN 이 있으면 우선 사용 (테스트/임시용).
"""
from __future__ import annotations

import getpass
import os
import subprocess

SERVICES = {
    "confluence": "meetingagent.confluence",
    "huggingface": "meetingagent.huggingface",
}


def _account() -> str:
    return getpass.getuser()


def get(name: str) -> str | None:
    env = os.environ.get(f"MEETINGAGENT_{name.upper()}_TOKEN")
    if env:
        return env
    r = subprocess.run(
        ["security", "find-generic-password", "-s", SERVICES[name], "-a", _account(), "-w"],
        capture_output=True,
        text=True,
    )
    return r.stdout.strip() or None if r.returncode == 0 else None


def set_interactive(name: str) -> bool:
    """`security`가 직접 입력을 받게 해서 토큰이 argv/셸 히스토리에 남지 않게 한다."""
    print(f"  {name} 토큰을 붙여넣고 Enter (화면에 표시되지 않음, 확인용으로 한 번 더 입력)")
    r = subprocess.run(
        ["security", "add-generic-password", "-U", "-s", SERVICES[name], "-a", _account(), "-w"]
    )
    return r.returncode == 0
