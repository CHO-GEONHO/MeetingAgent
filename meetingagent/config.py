"""설정 로드. config/*.yaml (없으면 *.example.yaml 기본값)."""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.environ.get("MEETINGAGENT_CONFIG_DIR", REPO_ROOT / "config"))
SKILL_PATH = REPO_ROOT / ".claude" / "skills" / "meeting-minutes" / "SKILL.md"

DEFAULTS: dict[str, Any] = {
    "language": "ko",
    "paths": {"inbox": "~/MeetingInbox", "workdir": "~/MeetingAgent-data"},
    "recording": {"device": ":default"},
    "whisper": {
        "model": "mlx-community/whisper-large-v3-turbo",
        "condition_on_previous_text": False,
    },
    "diarization": {
        "enabled": False,
        "model": "pyannote/speaker-diarization-community-1",
        "device": "mps",
        "num_speakers": None,
    },
    "summarize": {"model": "sonnet", "timeout_sec": 900, "claude_bin": None},
    "confluence": {
        "enabled": False,
        "base_url": "",
        "space_key": "",
        "parent_id": "",          # 고정 상위 페이지 ID. 넣으면 parent_title·hierarchy 를 무시한다
        "parent_title": "회의록",
        "hierarchy": "year_month",
        "labels": ["meeting"],
        "attach_transcript": True,
        "verify_ssl": True,
    },
    "retention": {"delete_audio": "after_upload"},
    "notify": True,
    "open_page_after_upload": False,
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _load_yaml(name: str) -> dict:
    path = CONFIG_DIR / f"{name}.yaml"
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class Settings:
    def __init__(self, raw: dict, glossary: list[dict], people: list[dict]):
        self.raw = raw
        self.glossary = glossary
        self.people = people

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    @property
    def inbox(self) -> Path:
        return Path(self.get("paths.inbox")).expanduser()

    @property
    def workdir(self) -> Path:
        return Path(self.get("paths.workdir")).expanduser()

    @property
    def jobs_dir(self) -> Path:
        return self.workdir / "meetings"

    @property
    def logs_dir(self) -> Path:
        return self.workdir / "logs"


def config_exists() -> bool:
    return (CONFIG_DIR / "config.yaml").exists()


def load() -> Settings:
    raw = _deep_merge(DEFAULTS, _load_yaml("config"))
    glossary = _load_yaml("glossary").get("terms") or []
    people = _load_yaml("people").get("people") or []
    return Settings(raw, glossary, people)
