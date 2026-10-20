"""Claude Code 헤드리스(claude -p)로 전사본 → 구조화된 회의록 JSON.

Claude는 '내용'만 JSON으로 만들고, Confluence XML은 render.py가 결정적으로 만든다.
(LLM이 매크로 XML을 직접 쓰면 깨지기 쉬움)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from datetime import date
from pathlib import Path

from .config import SKILL_PATH

_STR = {"type": "string"}
_STR_LIST = {"type": "array", "items": _STR}

MINUTES_SCHEMA = {
    "type": "object",
    "properties": {
        "title": _STR,
        "meeting_type": {"type": "string",
                         "enum": ["정기회의", "기술논의", "장애회고", "1on1", "외부미팅", "기타"]},
        "project": _STR,
        "attendees": _STR_LIST,
        # 회의 개요 표. 사용자가 시작할 때 입력한 값이 우선이고, 없으면 전사본에서 찾는다.
        "subject": _STR,
        "location": _STR,
        "started_at": _STR,
        "attendee_groups": {
            "type": "array",
            "items": {"type": "object",
                      "properties": {"org": _STR, "members": _STR_LIST},
                      "required": ["org", "members"]},
        },
        "background": _STR,
        "sequence": _STR_LIST,
        "summary": _STR,
        "summary_detail": _STR_LIST,
        "decisions": {
            "type": "array",
            "items": {"type": "object",
                      "properties": {"text": _STR, "status": _STR},
                      "required": ["text", "status"]},
        },
        "review_notes": {
            "type": "array",
            "items": {"type": "object",
                      "properties": {"topic": _STR, "points": _STR_LIST},
                      "required": ["topic", "points"]},
        },
        "action_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"owner": _STR, "task": _STR, "due": _STR, "evidence": _STR},
                "required": ["owner", "task", "due", "evidence"],
            },
        },
        "discussion": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"topic": _STR, "points": _STR_LIST},
                "required": ["topic", "points"],
            },
        },
        "open_questions": _STR_LIST,
        "speaker_map": {"type": "object", "additionalProperties": _STR},
        "corrections": {
            "type": "array",
            "items": {"type": "object", "properties": {"from": _STR, "to": _STR},
                      "required": ["from", "to"]},
        },
        "labels": _STR_LIST,
    },
    "required": ["title", "meeting_type", "project", "attendees", "subject", "location",
                 "started_at", "attendee_groups", "background", "sequence",
                 "summary", "summary_detail", "decisions",
                 "action_items", "discussion", "open_questions", "review_notes",
                 "speaker_map", "corrections", "labels"],
}

WEEKDAYS = "월화수목금토일"


def load_skill_prompt() -> str:
    text = SKILL_PATH.read_text(encoding="utf-8")
    if text.startswith("---"):
        text = text.split("---", 2)[2]
    # 대화형 사용법 섹션은 헤드리스 실행에 불필요
    return text.split("## 대화형으로 이 skill을 쓸 때", 1)[0].rstrip(" \n-") + "\n"


def build_input(*, meeting_date: date, duration_sec: float | None, hints: dict,
                glossary: list[dict], people: list[dict], transcript: str,
                notes: str = "") -> str:
    """Claude 에게 줄 입력. 수기 메모가 있으면 **전사본보다 먼저** 놓는다.

    전사본은 Whisper 가 만든 것이라 고유명사·수치가 틀어진다(`64EA`, `22대`, 제품명).
    사람이 회의 중에 적은 메모는 그 틀어짐이 없으므로, 둘이 어긋나면 메모를 따른다.
    """
    parts = ["# 회의 메타데이터",
             f"- 회의 날짜: {meeting_date.isoformat()} ({WEEKDAYS[meeting_date.weekday()]})"]
    if duration_sec:
        parts.append(f"- 녹음 길이: {max(1, round(duration_sec / 60))}분")
    labels = {"title": "사용자 지정 제목", "project": "프로젝트 힌트",
              "meeting_type": "회의 유형 힌트", "attendees": "참석자 힌트",
              "location": "회의 장소(확정값)", "started_at": "회의 시작 시각(확정값)",
              "note": "추가 메모"}
    for key, label in labels.items():
        if hints.get(key):
            val = hints[key]
            parts.append(f"- {label}: {', '.join(val) if isinstance(val, list) else val}")

    if glossary:
        parts.append("\n# 용어집 (정식 표기 ← 오인식 예)")
        for t in glossary:
            variants = ", ".join(t.get("variants") or [])
            parts.append(f"- {t['term']}" + (f" ← {variants}" if variants else ""))
    if people:
        parts.append("\n# 팀원 (정식 이름 ← 호칭)")
        for p in people:
            aliases = ", ".join(p.get("aliases") or [])
            parts.append(f"- {p['name']}" + (f" ← {aliases}" if aliases else ""))

    if notes.strip():
        parts.append("\n# 수기 회의록 (사람이 회의 중에 직접 적은 것 — 전사본보다 우선한다)\n")
        parts.append(notes.strip())

    parts.append("\n# 전사본\n")
    parts.append(transcript)
    return "\n".join(parts)


def find_claude(configured: str | None) -> str:
    if configured:
        return configured
    found = shutil.which("claude")
    if found:
        return found
    for cand in ("/opt/homebrew/bin/claude", "/usr/local/bin/claude",
                 os.path.expanduser("~/.local/bin/claude"),
                 os.path.expanduser("~/.claude/local/claude")):
        if os.path.exists(cand):
            return cand
    raise RuntimeError("claude CLI를 찾을 수 없습니다. config의 summarize.claude_bin을 지정하세요.")


def parse_claude_output(stdout: str) -> dict:
    """`--output-format json` 결과에서 회의록 JSON을 꺼낸다."""
    envelope = json.loads(stdout)
    if envelope.get("is_error"):
        raise RuntimeError(f"claude 실행 오류: {envelope.get('result') or envelope}")
    data = envelope.get("structured_output")
    if data is None:
        text = (envelope.get("result") or "").strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end < 0:
            raise RuntimeError(f"회의록 JSON을 찾을 수 없습니다: {text[:300]}")
        data = json.loads(text[start:end + 1])
    return normalize(data)


def normalize(data: dict) -> dict:
    """빠진 필드를 채우고 타입을 맞춘다. 업로드 단계가 항상 같은 모양을 받도록."""
    out = {
        "title": str(data.get("title") or "회의").strip(),
        "meeting_type": data.get("meeting_type") or "기타",
        "project": str(data.get("project") or "").strip(),
        "attendees": [str(a).strip() for a in data.get("attendees") or [] if str(a).strip()],
        "subject": str(data.get("subject") or "").strip(),
        "location": str(data.get("location") or "").strip(),
        "started_at": str(data.get("started_at") or "").strip(),
        "attendee_groups": [],
        "background": str(data.get("background") or "").strip(),
        "sequence": [str(s).strip() for s in data.get("sequence") or [] if str(s).strip()],
        "summary": str(data.get("summary") or "").strip(),
        "summary_detail": [str(s).strip() for s in data.get("summary_detail") or [] if str(s).strip()],
        "decisions": [],
        "action_items": [],
        "discussion": [],
        "open_questions": [str(q).strip() for q in data.get("open_questions") or [] if str(q).strip()],
        "review_notes": [],
        "speaker_map": {str(k): str(v) for k, v in (data.get("speaker_map") or {}).items()},
        "corrections": [c for c in data.get("corrections") or [] if c.get("from") and c.get("to")],
        "labels": [],
    }
    for g in data.get("attendee_groups") or []:
        members = [str(m).strip() for m in g.get("members") or [] if str(m).strip()]
        if members:
            out["attendee_groups"].append({"org": str(g.get("org") or "").strip(), "members": members})
    # decisions 는 예전엔 문자열 배열이었다. 옛 minutes.json 을 redo 할 때를 위해 둘 다 받는다.
    for d in data.get("decisions") or []:
        if isinstance(d, str):
            text, status = d.strip(), ""
        else:
            text, status = str(d.get("text") or "").strip(), str(d.get("status") or "").strip()
        if text:
            out["decisions"].append({"text": text, "status": status or "합의"})
    for n in data.get("review_notes") or []:
        points = [str(p).strip() for p in n.get("points") or [] if str(p).strip()]
        if points:
            out["review_notes"].append({"topic": str(n.get("topic") or "").strip(), "points": points})
    for a in data.get("action_items") or []:
        if str(a.get("task") or "").strip():
            out["action_items"].append({k: str(a.get(k) or "").strip()
                                        for k in ("owner", "task", "due", "evidence")})
    for d in data.get("discussion") or []:
        points = [str(p).strip() for p in d.get("points") or [] if str(p).strip()]
        if d.get("topic") or points:
            out["discussion"].append({"topic": str(d.get("topic") or "").strip(), "points": points})
    for label in data.get("labels") or []:
        clean = "".join(ch for ch in str(label).lower().replace(" ", "-")
                        if ch.isascii() and (ch.isalnum() or ch in "-_"))
        if clean and clean not in out["labels"]:
            out["labels"].append(clean)
    return out


def summarize(input_text: str, *, model: str, timeout: int, claude_bin: str | None) -> dict:
    cmd = [
        find_claude(claude_bin), "-p",
        "--output-format", "json",
        "--json-schema", json.dumps(MINUTES_SCHEMA, ensure_ascii=False),
        "--model", model,
        "--tools", "",
        "--no-session-persistence",
        "--system-prompt", load_skill_prompt(),
    ]
    # 레포/홈의 CLAUDE.md나 프로젝트 설정이 끼어들지 않도록 빈 임시 폴더에서 실행
    with tempfile.TemporaryDirectory(prefix="meetingagent-") as tmp:
        r = subprocess.run(cmd, input=input_text, capture_output=True, text=True,
                           timeout=timeout, cwd=tmp)
    if r.returncode != 0 and not r.stdout.strip():
        raise RuntimeError(f"claude -p 실패 (exit {r.returncode}): {r.stderr.strip()[:500]}")
    return parse_claude_output(r.stdout)


def save(minutes: dict, path: Path) -> None:
    path.write_text(json.dumps(minutes, ensure_ascii=False, indent=2), encoding="utf-8")
