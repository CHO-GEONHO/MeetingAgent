"""회의 1건 = 작업 폴더 1개. 단계별 결과를 캐시해서 실패 지점부터 재개한다.

~/MeetingAgent-data/meetings/2026-10-19_1430_주간회의/
  source.m4a        원본 (설정에 따라 업로드 후 삭제)
  audio.wav         16k mono (전사 후 삭제)
  transcript.json   세그먼트 [{start,end,text,speaker}]
  transcript.txt    사람이 읽는 전사본
  minutes.json      Claude 결과 (구조화)
  minutes.md        로컬 사본
  page.xml          업로드한 Confluence storage format
  state.json        메타데이터/진행 상태/페이지 링크
"""
from __future__ import annotations

import fcntl
import json
import re
import shutil
import subprocess
import time
import traceback
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

from . import audio, keychain, render, summarize, transcribe
from .config import Settings
from .confluence import ConfluenceConflict

STAGES = ["convert", "transcribe", "diarize", "summarize", "upload"]


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def slugify(text: str, max_len: int = 40) -> str:
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE).strip()
    return re.sub(r"[\s_]+", "-", text)[:max_len].strip("-") or "meeting"


class Job:
    def __init__(self, path: Path):
        self.dir = path
        self.state_path = path / "state.json"
        self.state: dict = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}

    def save(self) -> None:
        self.state_path.write_text(json.dumps(self.state, ensure_ascii=False, indent=2, default=str))

    def f(self, name: str) -> Path:
        return self.dir / name

    @property
    def name(self) -> str:
        return self.dir.name

    @property
    def source(self) -> Path | None:
        src = self.state.get("source_file")
        return self.dir / src if src else None

    @property
    def meeting_date(self) -> date:
        return date.fromisoformat(self.state["meeting_date"])

    @property
    def status(self) -> str:
        return self.state.get("status", "new")

    def mark(self, stage: str) -> None:
        self.state.setdefault("stages", {})[stage] = datetime.now().isoformat(timespec="seconds")
        self.save()

    def done(self, stage: str) -> bool:
        return stage in self.state.get("stages", {})

    def reset_from(self, stage: str) -> None:
        stages = self.state.get("stages", {})
        for s in STAGES[STAGES.index(stage):]:
            stages.pop(s, None)
        self.save()


# --- 작업 생성/조회 ---

def create_job(src: Path, cfg: Settings, *, move: bool, hints: dict | None = None,
               meeting_dt: datetime | None = None) -> Job:
    info = audio.probe(src)
    dt = meeting_dt or info["recorded_at"]
    hints = {k: v for k, v in (hints or {}).items() if v}
    base = f"{dt:%Y-%m-%d_%H%M}_{slugify(hints.get('title') or src.stem)}"
    cfg.jobs_dir.mkdir(parents=True, exist_ok=True)
    path, n = cfg.jobs_dir / base, 2
    while path.exists():
        path, n = cfg.jobs_dir / f"{base}-{n}", n + 1
    path.mkdir()

    dst = path / f"source{src.suffix.lower()}"
    (shutil.move if move else shutil.copy2)(str(src), str(dst))
    job = Job(path)
    job.state = {
        "source_name": src.name,
        "source_file": dst.name,
        "meeting_date": dt.date().isoformat(),
        "meeting_time": f"{dt:%H:%M}",
        "duration_sec": info["duration"],
        "hints": hints,
        "status": "new",
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    job.save()
    return job


def list_jobs(cfg: Settings) -> list[Job]:
    if not cfg.jobs_dir.exists():
        return []
    return [Job(p) for p in sorted(cfg.jobs_dir.iterdir(), reverse=True)
            if (p / "state.json").exists()]


def find_job(cfg: Settings, ref: str) -> Job:
    p = Path(ref).expanduser()
    if (p / "state.json").exists():
        return Job(p)
    jobs = list_jobs(cfg)
    if ref in ("last", "latest", "") and jobs:
        return jobs[0]
    matches = [j for j in jobs if j.name.startswith(ref) or ref in j.name]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise SystemExit(f"작업을 찾을 수 없습니다: {ref}  (`meet list`로 확인)")
    raise SystemExit(f"여러 작업이 일치합니다: {', '.join(j.name for j in matches[:5])}")


# --- 동시 실행 방지 (16GB에서 Whisper 두 개는 무리) ---

@contextmanager
def global_lock(cfg: Settings, *, blocking: bool = True):
    cfg.workdir.mkdir(parents=True, exist_ok=True)
    fh = open(cfg.workdir / ".lock", "w")
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if not blocking:
                yield False
                return
            log("다른 작업이 처리 중이라 대기합니다…")
            fcntl.flock(fh, fcntl.LOCK_EX)
        yield True
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


# --- 단계 ---

def _stage_convert(job: Job, cfg: Settings) -> None:
    if job.done("convert") and job.f("audio.wav").exists():
        return
    if job.done("transcribe"):
        return  # 이미 전사가 끝났으면 wav가 없어도 됨
    if not job.source or not job.source.exists():
        raise RuntimeError("원본 음성 파일이 없습니다.")
    log("오디오 변환 (16kHz mono)")
    audio.to_wav(job.source, job.f("audio.wav"))
    job.mark("convert")


def _stage_transcribe(job: Job, cfg: Settings) -> None:
    if job.done("transcribe") and job.f("transcript.json").exists():
        return
    mins = int((job.state.get("duration_sec") or 0) // 60)
    log(f"전사 시작: {cfg.get('whisper.model')} (녹음 {mins}분)")
    t0 = time.time()
    segments = transcribe.transcribe(
        job.f("audio.wav"),
        model=cfg.get("whisper.model"),
        language=cfg.get("language"),
        initial_prompt=transcribe.build_initial_prompt(cfg.glossary, cfg.people),
        condition_on_previous_text=bool(cfg.get("whisper.condition_on_previous_text")),
    )
    if not segments:
        raise RuntimeError("전사 결과가 비어 있습니다. 녹음에 음성이 있는지 확인하세요.")
    job.f("transcript.json").write_text(json.dumps(segments, ensure_ascii=False, indent=1))
    job.state["transcribe_sec"] = round(time.time() - t0)
    job.mark("transcribe")
    log(f"전사 완료: {len(segments)}개 구간, {job.state['transcribe_sec']}초 소요")


def _stage_diarize(job: Job, cfg: Settings) -> None:
    if not cfg.get("diarization.enabled") or job.done("diarize"):
        return
    from . import diarize

    if not job.f("audio.wav").exists():
        log("화자 분리 건너뜀: wav가 이미 삭제됨")
        return
    token = keychain.get("huggingface")
    if not token:
        raise RuntimeError("Hugging Face 토큰이 없습니다: meet setup --hf-token")
    log("화자 분리 중 (pyannote)")
    turns = diarize.diarize(job.f("audio.wav"), model=cfg.get("diarization.model"), token=token,
                            device=cfg.get("diarization.device"),
                            num_speakers=job.state["hints"].get("speakers")
                            or cfg.get("diarization.num_speakers"))
    segments = json.loads(job.f("transcript.json").read_text())
    segments = diarize.relabel(diarize.assign_speakers(segments, turns))
    job.f("transcript.json").write_text(json.dumps(segments, ensure_ascii=False, indent=1))
    n = len({s["speaker"] for s in segments if s.get("speaker")})
    job.mark("diarize")
    log(f"화자 분리 완료: {n}명")


def _write_transcript_txt(job: Job) -> str:
    segments = json.loads(job.f("transcript.json").read_text())
    text = transcribe.format_transcript(segments)
    job.f("transcript.txt").write_text(text, encoding="utf-8")
    return text


def _stage_summarize(job: Job, cfg: Settings, transcript_text: str) -> dict:
    if job.done("summarize") and job.f("minutes.json").exists():
        return summarize.normalize(json.loads(job.f("minutes.json").read_text()))
    log(f"회의록 작성 중 (claude -p, {cfg.get('summarize.model')})")
    payload = summarize.build_input(
        meeting_date=job.meeting_date, duration_sec=job.state.get("duration_sec"),
        hints=job.state.get("hints", {}), glossary=cfg.glossary, people=cfg.people,
        transcript=transcript_text,
    )
    minutes = summarize.summarize(payload, model=cfg.get("summarize.model"),
                                  timeout=int(cfg.get("summarize.timeout_sec")),
                                  claude_bin=cfg.get("summarize.claude_bin"))
    if job.state["hints"].get("title"):
        minutes["title"] = job.state["hints"]["title"]
    summarize.save(minutes, job.f("minutes.json"))
    job.f("minutes.md").write_text(
        render.markdown(minutes, meeting_date=job.meeting_date,
                        duration_sec=job.state.get("duration_sec")), encoding="utf-8")
    job.state["title"] = minutes["title"]
    job.mark("summarize")
    log(f"회의록 완료: \"{minutes['title']}\" (결정 {len(minutes['decisions'])}, "
        f"액션 {len(minutes['action_items'])})")
    return minutes


def confluence_client(cfg: Settings):
    from .confluence import Confluence

    token = keychain.get("confluence")
    if not token:
        raise RuntimeError("Confluence 토큰이 없습니다: meet setup --token")
    return Confluence(cfg.get("confluence.base_url"), token, cfg.get("confluence.space_key"),
                      verify_ssl=cfg.get("confluence.verify_ssl"),
                      userkey_cache=cfg.workdir / ".userkeys.json")


def _parent_id(cf, cfg: Settings, d: date) -> str:
    from .confluence import CHILDREN_BODY

    # 상위 페이지를 ID 로 고정한 경우. 제목이 바뀌어도 따라가고, 연/월 묶음도 만들지 않는다 —
    # 회의록이 전부 그 페이지 바로 밑에 `[날짜] 제목` 으로 쌓인다.
    fixed = str(cfg.get("confluence.parent_id") or "").strip()
    if fixed:
        return fixed

    root_title = cfg.get("confluence.parent_title")
    root = cf.find_page(root_title)
    if not root:
        raise RuntimeError(f"상위 페이지 '{root_title}'가 없습니다. `meet confluence-init`을 먼저 실행하세요.")
    parent = root["id"]
    hierarchy = cfg.get("confluence.hierarchy")
    if hierarchy in ("year", "year_month"):
        parent = cf.get_or_create_page(f"{root_title} {d:%Y}", CHILDREN_BODY, parent)["id"]
    if hierarchy == "year_month":
        parent = cf.get_or_create_page(f"{root_title} {d:%Y-%m}", CHILDREN_BODY, parent)["id"]
    return parent


def _stage_upload(job: Job, cfg: Settings, minutes: dict, transcript_text: str, *,
                  force: bool = False) -> None:
    if not cfg.get("confluence.enabled"):
        return
    cf = confluence_client(cfg)
    r = render.Renderer(cfg.people, cf.userkey)
    body = r.storage(
        minutes, meeting_date=job.meeting_date, duration_sec=job.state.get("duration_sec"),
        transcript_lines=transcript_text.splitlines() if cfg.get("confluence.attach_transcript") else None,
        source_name=job.state.get("source_name", ""),
    )
    job.f("page.xml").write_text(body, encoding="utf-8")

    title = f"[{job.meeting_date.isoformat()}] {minutes['title']}"
    prev = job.state.get("page") or {}
    page_id = prev.get("id")
    if page_id:
        log("Confluence 페이지 갱신")
        # known_version 을 넘겨 사람이 고친 본문을 말없이 덮지 않게 한다.
        # 충돌하면 ConfluenceConflict 가 올라오고, --force 로만 넘어간다.
        page = cf.update_page(
            page_id, prev["title"], body,
            known_version=prev.get("version"),
            message=f"MeetingAgent 재생성 ({job.name})",
            force=force,
        )
    else:
        if cf.find_page(title):  # 같은 날 같은 제목이면 시각을 붙인다 (스페이스 내 제목은 유일해야 함)
            title = f"{title} ({job.state.get('meeting_time', '')})"
        log(f"Confluence 업로드: {title}")
        page = cf.create_page(title, body, _parent_id(cf, cfg, job.meeting_date))
    labels = list(dict.fromkeys(list(cfg.get("confluence.labels") or []) + minutes["labels"]))
    labels = cf.sync_labels(page["id"], labels, prev.get("labels"))
    # 버전을 꼭 남긴다 — 비면 다음 갱신에서 충돌 검사가 조용히 꺼진다.
    version = (page.get("version") or {}).get("number") or cf.page_version(page["id"])
    job.state["page"] = {"id": page["id"], "title": page.get("title", title), "url": cf.url(page),
                         "version": version, "labels": labels}
    job.mark("upload")
    log(f"업로드 완료: {job.state['page']['url']}")


def _cleanup(job: Job, cfg: Settings) -> None:
    policy = cfg.get("retention.delete_audio")
    if job.done("transcribe") and (not cfg.get("diarization.enabled") or job.done("diarize")):
        job.f("audio.wav").unlink(missing_ok=True)
    finished = job.done("upload") or not cfg.get("confluence.enabled")
    if job.source and job.source.exists() and (
        policy == "after_transcribe" and job.done("transcribe")
        or policy == "after_upload" and finished and job.done("summarize")
    ):
        job.source.unlink()
        job.state["source_deleted"] = True
        job.save()


def notify(cfg: Settings, title: str, message: str) -> None:
    if not cfg.get("notify"):
        return
    script = f'display notification {json.dumps(message)} with title {json.dumps(title)}'
    subprocess.run(["osascript", "-e", script], capture_output=True)


def run(job: Job, cfg: Settings, *, upload: bool = True, notify_failure: bool = True,
        force_upload: bool = False) -> bool:
    """전 단계 실행. 성공 True. 실패해도 예외를 삼키고 state에 기록 → `meet retry`.

    `force_upload`는 이번 실행에만 적용하고 state.json에 남기지 않는다. 남겨두면 이번 시도가
    실패했을 때(VPN 끊김 등) 나중의 자동 재시도가 그 사이 생긴 수정까지 확인 없이 덮는다.
    """
    job.state.pop("error", None)
    job.state.pop("force_upload", None)  # 예전 버전이 남긴 플래그는 무시하고 지운다
    try:
        _stage_convert(job, cfg)
        _stage_transcribe(job, cfg)
        _stage_diarize(job, cfg)
        text = _write_transcript_txt(job)
        minutes = _stage_summarize(job, cfg, text)
        if upload:
            _stage_upload(job, cfg, minutes, text, force=force_upload)
        job.state["status"] = "done" if job.done("upload") or not cfg.get("confluence.enabled") \
            else ("summarized" if job.done("summarize") else job.status)
        _cleanup(job, cfg)
        job.save()
    except ConfluenceConflict as e:
        # 재시도로 풀리는 문제가 아니다. 사람이 페이지를 보고 판단해야 한다.
        # status 를 conflict 로 두어 `meet retry` 가 매번 다시 부딪히지 않게 한다.
        job.state["status"] = "conflict"
        job.state["error"] = str(e)
        job.save()
        log(f"업로드 중단\n{e}")
        if notify_failure:
            notify(cfg, "MeetingAgent 업로드 중단", f"{job.name}: 페이지가 그 사이 수정됨")
        return False
    except Exception as e:  # noqa: BLE001 - 파이프라인 경계
        job.state["status"] = "failed"
        job.state["error"] = f"{e.__class__.__name__}: {e}"
        job.save()
        (job.f("error.log")).write_text(traceback.format_exc())
        log(f"실패: {e}")
        log(f"이어서 하려면: meet retry   (상세: {job.f('error.log')})")
        if notify_failure:
            notify(cfg, "MeetingAgent 실패", f"{job.name}: {str(e)[:120]}")
        return False

    page = job.state.get("page")
    msg = page["url"] if page else str(job.f("minutes.md"))
    notify(cfg, "회의록 완료", f"{job.state.get('title', job.name)}")
    if page and cfg.get("open_page_after_upload"):
        subprocess.run(["open", page["url"]])
    log(f"완료 → {msg}")
    return True


def pending(cfg: Settings) -> list[Job]:
    # conflict 는 자동 재시도 대상이 아니다 — `meet redo <작업> --from upload [--force]` 로 사람이 푼다.
    return [j for j in list_jobs(cfg) if j.status not in ("done", "conflict")]
