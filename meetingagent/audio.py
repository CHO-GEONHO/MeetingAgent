"""ffmpeg 기반 오디오 변환/정보/녹음."""
from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

AUDIO_EXTS = {
    ".m4a", ".mp3", ".wav", ".aac", ".caf", ".flac", ".ogg", ".opus",
    ".webm", ".mp4", ".mov", ".m4v", ".qta",
}


def is_audio(path: Path) -> bool:
    return path.suffix.lower() in AUDIO_EXTS and not path.name.startswith(".")


def require_ffmpeg() -> None:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg가 없습니다. `brew install ffmpeg` 또는 ./install.sh 를 실행하세요.")


def to_wav(src: Path, dst: Path) -> None:
    """Whisper/pyannote 공용 16kHz mono PCM wav."""
    require_ffmpeg()
    subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
         "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dst)],
        check=True,
    )


def probe(src: Path) -> dict:
    """길이(초)와 녹음 시각. 음성 메모 m4a는 creation_time 태그에 실제 녹음 시각이 들어있다."""
    info: dict = {"duration": None, "recorded_at": None}
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", str(src)],
            capture_output=True, text=True, check=True,
        )
        fmt = json.loads(r.stdout).get("format", {})
        if fmt.get("duration"):
            info["duration"] = float(fmt["duration"])
        ct = (fmt.get("tags") or {}).get("creation_time")
        if ct:
            info["recorded_at"] = datetime.fromisoformat(ct.replace("Z", "+00:00")).astimezone()
    except (subprocess.CalledProcessError, ValueError, FileNotFoundError):
        pass
    if info["recorded_at"] is None:
        st = src.stat()
        ts = getattr(st, "st_birthtime", st.st_mtime)
        info["recorded_at"] = datetime.fromtimestamp(ts).astimezone()
    return info


def list_devices() -> str:
    require_ffmpeg()
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
        capture_output=True, text=True,
    )
    lines = [l for l in r.stderr.splitlines() if "AVFoundation" in l]
    return "\n".join(l.split("] ", 1)[-1] for l in lines)


def record(dst: Path, device: str) -> Path:
    """마이크 녹음. q 또는 Ctrl+C로 종료하면 파일을 정상 마감한다."""
    require_ffmpeg()
    if not device.startswith(":"):
        device = f":{device}"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-stats",
           "-f", "avfoundation", "-i", device,
           "-ac", "1", "-c:a", "aac", "-b:a", "96k", str(dst)]
    proc = subprocess.Popen(cmd)
    try:
        proc.wait()
    except KeyboardInterrupt:
        # ffmpeg도 같은 프로세스 그룹이라 SIGINT를 받고 파일을 마감한다
        proc.wait()
    if not dst.exists() or dst.stat().st_size == 0:
        raise RuntimeError(
            "녹음 파일이 비어 있습니다. 시스템 설정 > 개인정보 보호 > 마이크에서 터미널 권한을 확인하세요."
        )
    return dst
