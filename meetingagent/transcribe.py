"""로컬 STT (mlx-whisper) + 전사본 정리/포맷."""
from __future__ import annotations

import inspect
import re
from pathlib import Path

# 무음/잡음 구간에서 한국어 Whisper가 자주 지어내는 문장 (유튜브 자막 학습 흔적)
HALLUCINATION_PATTERNS = [
    r"시청해\s*주셔서\s*감사합니다",
    r"구독(과|하고)?\s*좋아요",
    r"좋아요(와|랑)?\s*구독",
    r"자막\s*(제공|by|제작)",
    r"다음\s*영상에서\s*(만나요|뵙겠습니다)",
    r"MBC\s*뉴스",
    r"^\s*\.+\s*$",
]
_HALLU_RE = re.compile("|".join(HALLUCINATION_PATTERNS))


def build_initial_prompt(glossary: list[dict], people: list[dict], max_chars: int = 350) -> str:
    """Whisper initial_prompt. 너무 길면 디코더 컨텍스트(224 토큰)를 넘으므로 자른다."""
    words: list[str] = []
    for t in glossary:
        if t.get("term"):
            words.append(str(t["term"]))
    for p in people:
        if p.get("name"):
            words.append(str(p["name"]))
    prompt = "다음은 사내 업무 회의 녹음입니다."
    if not words:
        return prompt
    prompt += " 주요 용어: "
    used: list[str] = []
    for w in words:
        if len(prompt) + len(", ".join(used + [w])) > max_chars:
            break
        used.append(w)
    return prompt + ", ".join(used) + "."


def clean_segments(segments: list[dict]) -> list[dict]:
    """환각 문장과 반복 루프(같은 문장 3회 이상 연속)를 제거."""
    out: list[dict] = []
    repeat = 0
    for seg in segments:
        text = seg["text"].strip()
        if not text or _HALLU_RE.search(text):
            continue
        if out and out[-1]["text"] == text:
            repeat += 1
            if repeat >= 2:
                continue
        else:
            repeat = 0
        out.append({**seg, "text": text})
    return out


def transcribe(wav: Path, *, model: str, language: str, initial_prompt: str,
               condition_on_previous_text: bool = False) -> list[dict]:
    import mlx_whisper

    kwargs = dict(
        path_or_hf_repo=model,
        language=language,
        initial_prompt=initial_prompt,
        condition_on_previous_text=condition_on_previous_text,
        verbose=False,  # tqdm 진행률 표시
        no_speech_threshold=0.6,
        compression_ratio_threshold=2.4,
        logprob_threshold=-1.0,
    )
    # 버전에 따라 지원: 이전 문맥 없이도 매 윈도우에 용어집 프롬프트를 유지
    if "carry_initial_prompt" in inspect.signature(mlx_whisper.transcribe).parameters:
        kwargs["carry_initial_prompt"] = True

    result = mlx_whisper.transcribe(str(wav), **kwargs)
    segments = [
        {"start": round(float(s["start"]), 2), "end": round(float(s["end"]), 2), "text": s["text"]}
        for s in result.get("segments", [])
    ]
    return clean_segments(segments)


def fmt_ts(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def format_transcript(segments: list[dict], speaker_names: dict[str, str] | None = None,
                      max_span: float = 45.0) -> str:
    """같은 화자의 연속 발화를 한 줄로 묶어 `[HH:MM:SS] 화자: 내용` 형태로.
    타임스탬프가 Action Item 근거로 쓰이므로 화자가 없거나 길어지면 max_span 초마다 줄을 나눈다."""
    names = speaker_names or {}
    lines: list[str] = []
    cur_spk: object = object()
    buf: list[str] = []
    start = 0.0

    def flush() -> None:
        if not buf:
            return
        prefix = f"[{fmt_ts(start)}] "
        if cur_spk:
            prefix += f"{names.get(cur_spk, cur_spk)}: "
        lines.append(prefix + " ".join(buf))

    for seg in segments:
        spk = seg.get("speaker")
        if spk != cur_spk or not spk or seg["start"] - start > max_span:
            flush()
            cur_spk, buf, start = spk, [], seg["start"]
        buf.append(seg["text"])
    flush()
    return "\n".join(lines) + "\n"
