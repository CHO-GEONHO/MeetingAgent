"""화자 분리 (pyannote, 선택). 결과를 Whisper 세그먼트에 겹치는 시간 기준으로 붙인다."""
from __future__ import annotations

import wave
from pathlib import Path


def _load_wav(path: Path):
    """pyannote 4.x의 파일 디코딩(torchcodec) 의존을 피하려고 직접 텐서로 넘긴다."""
    import numpy as np
    import torch

    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    waveform = torch.from_numpy(data.astype(np.float32) / 32768.0).unsqueeze(0)
    return {"waveform": waveform, "sample_rate": sr}


def diarize(wav: Path, *, model: str, token: str, device: str = "mps",
            num_speakers: int | None = None) -> list[dict]:
    import torch
    from pyannote.audio import Pipeline

    try:
        pipeline = Pipeline.from_pretrained(model, token=token)  # pyannote.audio 4.x
    except TypeError:
        pipeline = Pipeline.from_pretrained(model, use_auth_token=token)  # 3.x
    if pipeline is None:
        raise RuntimeError(
            f"{model} 로드 실패. Hugging Face에서 모델 약관 동의 여부와 토큰을 확인하세요."
        )

    audio = _load_wav(wav)
    kwargs = {"num_speakers": num_speakers} if num_speakers else {}

    def run(dev: str):
        pipeline.to(torch.device(dev))
        return pipeline(audio, **kwargs)

    try:
        output = run(device)
    except Exception:
        if device == "cpu":
            raise
        output = run("cpu")

    annotation = getattr(output, "speaker_diarization", output)  # 4.x: DiarizeOutput
    return [
        {"start": round(turn.start, 2), "end": round(turn.end, 2), "speaker": str(label)}
        for turn, _, label in annotation.itertracks(yield_label=True)
    ]


def assign_speakers(segments: list[dict], turns: list[dict]) -> list[dict]:
    """각 전사 세그먼트에 가장 많이 겹치는 화자를, 겹침이 없으면 가장 가까운 화자를 붙인다."""
    if not turns:
        return segments
    turns = sorted(turns, key=lambda t: t["start"])
    out = []
    for seg in segments:
        best, best_overlap, best_dist = None, 0.0, float("inf")
        for t in turns:
            if t["start"] > seg["end"] + 30:
                break
            overlap = min(seg["end"], t["end"]) - max(seg["start"], t["start"])
            if overlap > best_overlap:
                best, best_overlap = t["speaker"], overlap
            elif best_overlap == 0:
                dist = max(t["start"] - seg["end"], seg["start"] - t["end"], 0)
                if dist < best_dist:
                    best, best_dist = t["speaker"], dist
        out.append({**seg, "speaker": best})
    return out


def relabel(segments: list[dict]) -> list[dict]:
    """SPEAKER_00 → 화자A 처럼 등장 순서대로 읽기 쉬운 라벨로."""
    mapping: dict[str, str] = {}
    out = []
    for seg in segments:
        spk = seg.get("speaker")
        if spk and spk not in mapping:
            idx = len(mapping)
            mapping[spk] = "화자" + (chr(ord("A") + idx) if idx < 26 else str(idx + 1))
        out.append({**seg, "speaker": mapping.get(spk) if spk else None})
    return out
