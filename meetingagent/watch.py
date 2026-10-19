"""launchd로 inbox 폴더 감시. 파일이 들어오면 처리, 15분마다 미완료 업로드 재시도(VPN 꺼져 있던 경우)."""
from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import time
from pathlib import Path

from . import audio, pipeline
from .config import REPO_ROOT, Settings

LABEL = "com.meetingagent.watch"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _path_env() -> str:
    dirs = [str(REPO_ROOT / ".venv" / "bin"), "/opt/homebrew/bin", "/usr/local/bin",
            str(Path.home() / ".local" / "bin")]
    claude = shutil.which("claude")
    if claude:
        dirs.append(str(Path(claude).resolve().parent))
        dirs.append(str(Path(claude).parent))
    dirs += ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    return ":".join(dict.fromkeys(dirs))


def install(cfg: Settings) -> None:
    cfg.inbox.mkdir(parents=True, exist_ok=True)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": LABEL,
        "ProgramArguments": [str(REPO_ROOT / "bin" / "meet"), "watch", "run"],
        "WatchPaths": [str(cfg.inbox)],
        "StartInterval": 900,
        "RunAtLoad": True,
        "ThrottleInterval": 20,
        "ProcessType": "Background",
        "EnvironmentVariables": {"PATH": _path_env(), "PYTHONUNBUFFERED": "1",
                                 "LANG": "ko_KR.UTF-8"},
        "StandardOutPath": str(cfg.logs_dir / "watch.log"),
        "StandardErrorPath": str(cfg.logs_dir / "watch.log"),
    }
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    uninstall(quiet=True)
    with PLIST.open("wb") as f:
        plistlib.dump(plist, f)
    subprocess.run(["launchctl", "bootstrap", _domain(), str(PLIST)], check=True)


def uninstall(quiet: bool = False) -> None:
    subprocess.run(["launchctl", "bootout", f"{_domain()}/{LABEL}"], capture_output=True)
    if PLIST.exists():
        PLIST.unlink()
    elif not quiet:
        print("감시가 설치되어 있지 않습니다.")


def is_installed() -> bool:
    r = subprocess.run(["launchctl", "print", f"{_domain()}/{LABEL}"], capture_output=True)
    return r.returncode == 0


def _stable_files(inbox: Path) -> list[Path]:
    """복사/녹음 중인 파일은 건너뛴다: 크기 변화가 없고 최소 5초 지난 파일만."""
    files = [p for p in inbox.iterdir() if p.is_file() and audio.is_audio(p)]
    sizes = {p: p.stat().st_size for p in files}
    time.sleep(3)
    now = time.time()
    return sorted(
        (p for p in files if p.exists() and p.stat().st_size == sizes[p] and sizes[p] > 0
         and now - p.stat().st_mtime > 5),
        key=lambda p: p.stat().st_mtime,
    )


def run_once(cfg: Settings) -> None:
    """launchd가 호출. 이미 처리 중이면 바로 종료(처리 중인 쪽이 끝나기 전 inbox를 다시 훑음)."""
    cfg.inbox.mkdir(parents=True, exist_ok=True)
    with pipeline.global_lock(cfg, blocking=False) as acquired:
        if not acquired:
            return
        while True:
            files = _stable_files(cfg.inbox)
            if not files:
                break
            for f in files:
                pipeline.log(f"새 녹음: {f.name}")
                job = pipeline.create_job(f, cfg, move=True)
                pipeline.run(job, cfg)
        # 업로드만 남은 작업(VPN 꺼져 있었던 경우 등) 재시도. 전사 실패 건은 수동 retry로.
        for job in pipeline.pending(cfg):
            if job.done("summarize") and not job.done("upload"):
                pipeline.log(f"업로드 재시도: {job.name}")
                pipeline.run(job, cfg, notify_failure=False)  # VPN 꺼짐 알림이 15분마다 뜨지 않게
