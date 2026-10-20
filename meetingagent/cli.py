"""`meet` CLI."""
from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from . import audio, config, keychain, pipeline

USAGE = """\
meet — 회의 녹음 → 로컬 전사 → Claude 회의록 → Confluence

  meet rec                     지금부터 녹음 (q 또는 Ctrl+C로 종료) → 자동 처리
  meet <파일.m4a> [...]        녹음 파일 처리
  meet notes <작업>            수기 회의록을 붙이고 회의록을 다시 만든다
  meet docs <작업> <파일...>   회의 중 띄운 자료(워드·PDF·PPT)를 붙이고 다시 만든다
  meet watch on                ~/MeetingInbox 자동 감시 (파일만 넣으면 끝)

  meet list                    최근 회의록과 상태
  meet open [작업]             Confluence 페이지 열기 (기본: 최근)
  meet retry                   실패/미업로드 작업 이어서 처리
  meet redo <작업> --from summarize|upload|transcribe

  meet setup                   최초 설정 (Confluence, 토큰, 모델 다운로드)
  meet doctor                  환경 점검
  meet confluence-init         Confluence에 '회의록' 홈 페이지 만들기

공통 옵션: -t 제목  -p 프로젝트  -a "참석자1,참석자2"  --type 장애회고  -n 화자수  --no-upload
"""

STATUS_ICON = {"done": "✅", "summarized": "📝", "failed": "❌", "new": "⏳", "conflict": "⚠️"}


def _add_hint_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("-t", "--title", help="회의 제목 (없으면 Claude가 생성)")
    p.add_argument("-p", "--project", help="프로젝트 힌트")
    p.add_argument("-a", "--attendees", help="참석자, 쉼표 구분")
    p.add_argument("-l", "--location", help="회의 장소 (예: 본사 9층 회의실)")
    p.add_argument("--time", dest="started_at", help="회의 시작 시각 (예: 14:00)")
    p.add_argument("--type", dest="meeting_type", help="정기회의|기술논의|장애회고|1on1|외부미팅")
    p.add_argument("-n", "--speakers", type=int, help="화자 수 (화자 분리 정확도 향상)")
    p.add_argument("--note", help="Claude에게 줄 추가 메모 (예: '보안 이슈 위주로')")
    p.add_argument("--notes", metavar="파일",
                   help="회의 중 직접 적은 수기 회의록 (.md/.txt). 전사본보다 우선합니다")
    p.add_argument("--doc", action="append", metavar="파일",
                   help="회의 중 띄운 자료 (워드·PDF·PPT·hwpx). 여러 번 지정 가능. 참고용으로만 씁니다")
    p.add_argument("--date", help="회의 일시 덮어쓰기 'YYYY-MM-DD HH:MM'")
    p.add_argument("--no-upload", action="store_true", help="Confluence 업로드 생략 (로컬 md만)")
    p.add_argument("-y", "--yes", action="store_true", help="시작 질문 없이 바로 진행")


def _hints(args) -> dict:
    return {
        "title": args.title,
        "project": args.project,
        "attendees": [a.strip() for a in args.attendees.split(",")] if args.attendees else None,
        "location": getattr(args, "location", None),
        "started_at": getattr(args, "started_at", None),
        "meeting_type": args.meeting_type,
        "speakers": args.speakers,
        "note": args.note,
    }


NOTES_TEMPLATE = """\
# 수기 회의록

회의 중에 적은 것을 그대로 두세요. 문장이 아니어도 됩니다.
Whisper 가 틀리는 숫자·제품명·대수는 여기 적힌 것을 따릅니다.
전사본에 없는 내용(녹음이 못 잡았거나 끝나고 정리한 것)도 회의록에 들어갑니다.

"""


def _notes_path(job):
    return job.f("notes.md")


def _attach_notes(job, src: str | None) -> bool:
    """`--notes <파일>` 을 작업 폴더의 notes.md 로 넣는다.

    src 가 "-" 면 편집기를 열어 그 자리에서 작성하게 한다.
    """
    if not src:
        return False
    path = _notes_path(job)
    if src == "-":
        if not path.exists():
            path.write_text(NOTES_TEMPLATE, encoding="utf-8")
        _open_editor(path)
        body = [l for l in path.read_text(encoding="utf-8").splitlines()
                if l.strip() and not l.startswith("#") and "Whisper" not in l]
        print(f"  수기 회의록 작성: {len(body)}줄" if body else "  수기 회의록 비어 있음 — 건너뜁니다")
        return bool(body)
    p = Path(src).expanduser()
    if not p.exists():
        print(f"수기 회의록 파일이 없습니다: {p}")
        sys.exit(1)
    text = p.read_text(encoding="utf-8")
    _notes_path(job).write_text(text, encoding="utf-8")
    print(f"  수기 회의록 반영: {p.name} ({len(text.splitlines())}줄)")
    return True


def _open_editor(path: Path) -> None:
    """$EDITOR 로 열고 닫힐 때까지 기다린다. 없으면 맥 기본 편집기를 -W 로 띄운다."""
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL")
    cmd = ([*editor.split(), str(path)] if editor
           else ["open", "-W", "-t", str(path)])  # -W: 닫을 때까지 대기
    subprocess.run(cmd)


MEETING_FIELDS = [("title", "회의 제목"), ("attendees", "참석자 (쉼표 구분)"),
                  ("location", "회의 장소"), ("started_at", "회의 시작 시각")]


def _prompt_meeting_info(args) -> None:
    """시작할 때 회의 개요에 들어갈 값을 묻는다 (제목·참석자·장소·시작 시각).

    - 자동 감시(launchd)처럼 **터미널이 없으면 묻지 않는다.** 물으면 데몬이 거기서 멈춘다.
    - 플래그로 이미 준 값은 다시 묻지 않는다. `-y` 로 전부 건너뛸 수 있다.
    - 빈칸으로 두면 Claude 가 전사본에서 추정한다. 틀리게 지어내는 것보다 낫도록
      SKILL.md 가 "확인 안 되면 비워 둔다" 로 지시한다.
    - 받은 값을 다시 보여주고 확인받는다. 터미널 한글 입력에서 화면에는 다 보이는데 실제로는
      글자가 빠진 채 들어오는 일이 있었다 ("화상미팅 5층 무스타파" → "화상미팅 5층 파").
    """
    if getattr(args, "yes", False) or not sys.stdin.isatty():
        return
    print("\n회의 정보 — Enter 로 건너뛰면 전사본에서 추정합니다")
    asked = []
    for key, label in MEETING_FIELDS:
        if getattr(args, key, None):
            continue
        default = datetime.now().strftime("%H:%M") if key == "started_at" else ""
        setattr(args, key, _input_line(label, default=default) or None)
        asked.append((key, label))
    while asked:
        print("  ── 받은 값 ──")
        for key, label in asked:
            print(f"    {label}: {getattr(args, key) or '(비움 — 전사본에서 추정)'}")
        if _yes("이대로 진행할까요? 글자가 빠졌으면 n", True):
            break
        for key, label in asked:
            setattr(args, key, _input_line(f"{label} — 고친 뒤 Enter",
                                           prefill=getattr(args, key) or "") or None)
    print()


def _readline():
    try:
        import readline
        return readline
    except ImportError:
        return None


def _input_line(label: str, *, default: str = "", prefill: str = "") -> str:
    """자유 입력 한 줄. 질문은 따로 찍고 입력 칸은 '>' 만 둔다.

    readline(libedit)으로 받는다 — 터미널 기본 줄 편집보다 한글 IME 입력이 덜 깨진다.
    libedit 는 프롬프트에 한글이 있으면 커서 위치를 잘못 계산하므로 프롬프트 자체는 ASCII 로 둔다.
    `prefill` 은 고칠 때 기존 값을 입력 칸에 미리 넣어 둔다.
    """
    rl = _readline()
    print(f"  {label}" + (f" [{default}]" if default else ""))
    hooked = False
    if rl and prefill:
        try:
            rl.set_startup_hook(lambda: rl.insert_text(prefill))
            hooked = True
        except Exception:  # noqa: BLE001 - 미리 채우기는 편의 기능
            pass
    try:
        val = input("  > ").strip()
    finally:
        if hooked:
            rl.set_startup_hook(None)
    return val or default


def _parse_paths(line: str) -> list[Path]:
    """터미널에 파일을 끌어다 놓으면 들어오는 경로들. 공백은 `\\ ` 로 이스케이프되고 여러 개면 공백 구분."""
    try:
        tokens = shlex.split(line)
    except ValueError:
        return []
    return [Path(t).expanduser() for t in tokens]


def _looks_like_path(line: str) -> bool:
    s = line.strip().strip("'\"")
    return s.startswith(("/", "~/", "./"))


def _classify_notes_input(first: str, stream) -> tuple[str, str]:
    """수기 메모 입력의 첫 줄을 보고 (종류, 값) 을 돌려준다.

    - 빈 줄            → ("editor", "")
    - 있는 파일 경로   → ("file", 경로)        ← 끌어다 놓기
    - 없는 경로처럼 보임 → ("missing", 입력값)
    - 그 밖            → ("text", 본문)        ← 붙여넣기. 나머지 줄은 Ctrl+D 까지 여기서 읽는다.
    붙여넣은 나머지 줄을 여기서 다 읽어야 셸로 넘어가 명령으로 실행되지 않는다.
    """
    if not first.strip():
        return ("editor", "")
    paths = _parse_paths(first)
    if len(paths) == 1 and paths[0].is_file():
        return ("file", str(paths[0]))
    if _looks_like_path(first):
        return ("missing", first.strip())
    return ("text", first + stream.read())


def _ask_notes(args) -> None:
    print("  수기 회의록 넣는 방법 (셋 중 하나)")
    print("    · 내용 붙여넣기 → 다 붙였으면 Enter, 그다음 Ctrl+D")
    print("    · 파일을 이 창으로 끌어다 놓기 → Enter")
    print("    · 편집기에서 쓰기 → 그냥 Enter")
    while True:
        print("  > ", end="", flush=True)
        kind, val = _classify_notes_input(sys.stdin.readline(), sys.stdin)
        if kind != "missing":
            break
        print(f"  파일이 없습니다: {val}\n  다시 끌어다 놓거나, 편집기로 쓰려면 그냥 Enter")
    if kind == "editor":
        args.notes = "-"
    elif kind == "file":
        args.notes = val
    else:
        args.notes_text = val
        print(f"\n  수기 회의록 {len([l for l in val.splitlines() if l.strip()])}줄 받음")


def _ask_docs() -> list[str]:
    print("  회의 중 띄운 자료(워드·PDF·PPT·hwpx)가 있으면 파일을 이 창으로 끌어다 놓고 Enter")
    print("  (여러 개면 한꺼번에 끌어다 놓기, 없으면 그냥 Enter)")
    while True:
        print("  > ", end="", flush=True)
        line = sys.stdin.readline()
        if not line.strip():
            return []
        paths = _parse_paths(line)
        missing = [str(p) for p in paths if not p.is_file()]
        if paths and not missing:
            return [str(p) for p in paths]
        _flush_tty_input()  # 메모를 잘못 붙여넣은 경우 나머지 줄을 버린다
        print(f"  파일을 찾을 수 없습니다: {', '.join(missing) or line.strip()[:60]}")
        print("  다시 끌어다 놓거나, 없으면 그냥 Enter")


def _attach_docs(job, files) -> int:
    from . import materials

    n = 0
    for f in files or []:
        try:
            name, chars = materials.attach(job.dir, Path(f).expanduser())
            print(f"  회의 자료 반영: {name} ({chars:,}자)")
            n += 1
        except materials.MaterialError as e:
            print(f"  ! 회의 자료 건너뜀 — {e}")
    return n


def _flush_tty_input() -> None:
    """읽지 않고 남은 입력을 버린다. 붙여넣기 잔여분이 셸로 넘어가 명령으로 실행되는 걸 막는다.

    (실제로 수기 메모를 경로 칸에 붙여넣었다가 나머지 줄이 셸에서 실행되고,
    `->` 가 리다이렉션으로 해석돼 빈 파일이 생긴 적이 있다.)
    """
    if not sys.stdin.isatty():
        return
    try:
        import termios
        termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except Exception:  # noqa: BLE001 - 정리 단계, 실패해도 무방
        pass


def _meeting_dt(args) -> datetime | None:
    if not args.date:
        return None
    return datetime.fromisoformat(args.date if " " in args.date else f"{args.date} 09:00").astimezone()


def _require_setup() -> config.Settings:
    if not config.config_exists():
        print("설정이 없습니다. 먼저 `meet setup`을 실행하세요.")
        sys.exit(1)
    return config.load()


# --- 명령 ---

def cmd_process(args) -> int:
    cfg = _require_setup()
    notes = getattr(args, "notes", None)
    if notes and notes != "-" and not Path(notes).expanduser().is_file():
        # 작업 폴더를 만들기 전에 확인한다 (만든 뒤 멈추면 반쯤 만든 작업이 남는다)
        print(f"수기 회의록 파일이 없습니다: {notes}")
        return 1
    _prompt_meeting_info(args)
    ok = True
    with pipeline.global_lock(cfg):
        for f in args.files:
            src = Path(f).expanduser()
            if not src.exists():
                print(f"파일 없음: {src}")
                ok = False
                continue
            job = pipeline.create_job(src, cfg, move=args.move, hints=_hints(args),
                                      meeting_dt=_meeting_dt(args))
            pipeline.log(f"작업 폴더: {job.dir}")
            _attach_notes(job, notes)
            if getattr(args, "notes_text", None):
                _notes_path(job).write_text(args.notes_text.strip() + "\n", encoding="utf-8")
                print(f"  수기 회의록 반영: 붙여넣은 내용 ({len(args.notes_text.splitlines())}줄)")
            _attach_docs(job, getattr(args, "doc", None))
            ok &= pipeline.run(job, cfg, upload=not args.no_upload)
    return 0 if ok else 1


def cmd_rec(args) -> int:
    cfg = _require_setup()
    if args.list_devices:
        print(audio.list_devices())
        return 0
    # 녹음 전에 묻는다 — 끝난 뒤에 물으면 회의 끝나고 자리를 뜬 상태에서 멈춰 있게 된다.
    _prompt_meeting_info(args)
    args.yes = True  # cmd_process 에서 다시 묻지 않게
    rec_dir = cfg.workdir / "recordings"
    rec_dir.mkdir(parents=True, exist_ok=True)
    dst = rec_dir / f"{datetime.now():%Y-%m-%d_%H%M}.m4a"
    print(f"● 녹음 중… 종료하려면 q 또는 Ctrl+C   ({dst})")
    audio.record(dst, args.device or cfg.get("recording.device"))
    print(f"\n녹음 저장: {dst}")
    # 회의 중 적은 메모와 띄운 자료를 지금 붙인다. 전사가 끝난 뒤가 아니라 여기서 묻는 이유는,
    # 이 뒤 단계(전사·요약)가 몇 분씩 걸려서 그때 물으면 자리를 비운 사이 멈춰 있기 때문이다.
    # 자료를 먼저 묻는다 — 메모 붙여넣기는 Ctrl+D 로 끝나서 그 뒤에 다른 질문을 두지 않는다.
    if sys.stdin.isatty():
        if not args.doc:
            args.doc = _ask_docs()
        if not args.notes and _yes("회의 중 적은 수기 회의록이 있나요?", False):
            _ask_notes(args)
    args.files, args.move = [str(dst)], True
    return cmd_process(args)


def cmd_list(args) -> int:
    cfg = _require_setup()
    jobs = pipeline.list_jobs(cfg)[: args.n]
    if not jobs:
        print("아직 처리한 회의가 없습니다.")
        return 0
    for j in jobs:
        icon = STATUS_ICON.get(j.status, "·")
        title = j.state.get("title") or j.state.get("source_name")
        print(f"{icon} {j.name}  {title}")
        if j.state.get("page"):
            print(f"     {j.state['page']['url']}")
        elif j.state.get("error"):
            print(f"     {j.state['error'][:150]}")
    return 0


def cmd_open(args) -> int:
    cfg = _require_setup()
    job = pipeline.find_job(cfg, args.job)
    page = job.state.get("page")
    target = page["url"] if page and not args.folder else str(job.dir)
    subprocess.run(["open", target])
    return 0


def cmd_retry(args) -> int:
    cfg = _require_setup()
    jobs = pipeline.pending(cfg)
    if not jobs:
        print("이어서 처리할 작업이 없습니다.")
        return 0
    ok = True
    with pipeline.global_lock(cfg):
        for job in jobs:
            pipeline.log(f"재시도: {job.name}")
            ok &= pipeline.run(job, cfg)
    return 0 if ok else 1


def cmd_notes(args) -> int:
    """수기 회의록을 붙이거나 고치고, 그 내용으로 회의록을 다시 만든다."""
    cfg = _require_setup()
    job = pipeline.find_job(cfg, args.job)
    _attach_notes(job, args.file or "-")
    if not _notes_path(job).exists():
        return 1
    print(f"  저장: {_notes_path(job)}")
    if not args.no_redo and _yes("이 메모를 반영해 회의록을 다시 만들까요?"):
        args.from_stage, args.title, args.note = "summarize", None, None
        args.attendees = args.location = args.started_at = None
        args.force = False
        return cmd_redo(args)
    print(f"  나중에 반영하려면: meet redo {job.name} --from summarize")
    return 0


def cmd_docs(args) -> int:
    """회의 자료(회의 중 화면에 띄운 문서)를 붙이고, 그 내용을 참고해 회의록을 다시 만든다."""
    cfg = _require_setup()
    job = pipeline.find_job(cfg, args.job)
    if not _attach_docs(job, args.files):
        return 1
    if not args.no_redo and _yes("이 자료를 참고해 회의록을 다시 만들까요?"):
        args.from_stage, args.title, args.note, args.notes, args.doc = "summarize", None, None, None, None
        args.attendees = args.location = args.started_at = None
        args.force = False
        return cmd_redo(args)
    print(f"  나중에 반영하려면: meet redo {job.name} --from summarize")
    return 0


def cmd_redo(args) -> int:
    cfg = _require_setup()
    job = pipeline.find_job(cfg, args.job)
    if args.from_stage in ("transcribe", "convert") and not (job.source and job.source.exists()):
        print("원본 음성이 이미 삭제되어 재전사할 수 없습니다. --from summarize 는 가능합니다.")
        return 1
    _attach_notes(job, getattr(args, "notes", None))
    _attach_docs(job, getattr(args, "doc", None))
    for key in ("title", "note", "location", "started_at"):
        val = getattr(args, key, None)
        if val:
            job.state.setdefault("hints", {})[key] = val
    if getattr(args, "attendees", None):
        job.state.setdefault("hints", {})["attendees"] = [
            a.strip() for a in args.attendees.split(",") if a.strip()]
    job.reset_from(args.from_stage)
    with pipeline.global_lock(cfg):
        # --force: 업로드 이후 사람이 고친 페이지를 덮어쓴다. 이번 실행에만 적용된다.
        return 0 if pipeline.run(job, cfg, force_upload=args.force) else 1


def cmd_watch(args) -> int:
    from . import watch

    cfg = _require_setup()
    if args.action == "on":
        watch.install(cfg)
        print(f"감시 시작: {cfg.inbox}")
        print("  → 이 폴더에 녹음 파일(m4a 등)을 넣으면 자동으로 전사·요약·업로드합니다.")
        print("  → 음성 메모 앱: 녹음 우클릭 > 공유 > '파일에 저장' 또는 Finder로 드래그")
        print(f"  로그: meet watch log")
        subprocess.run(["open", str(cfg.inbox)])
    elif args.action == "off":
        watch.uninstall()
        print("감시 중지")
    elif args.action == "status":
        print(f"감시: {'켜짐' if watch.is_installed() else '꺼짐'}   폴더: {cfg.inbox}")
    elif args.action == "log":
        log = cfg.logs_dir / "watch.log"
        if log.exists():
            subprocess.run(["tail", "-n", "60", "-f", str(log)])
        else:
            print("아직 로그가 없습니다.")
    elif args.action == "run":
        watch.run_once(cfg)
    return 0


def cmd_confluence_init(args) -> int:
    from .confluence import index_page_body

    cfg = _require_setup()
    cf = pipeline.confluence_client(cfg)
    title = cfg.get("confluence.parent_title")
    if cf.find_page(title):
        print(f"이미 있습니다: {title}")
        return 0
    parent = None
    if args.under:
        p = cf.find_page(args.under)
        if not p:
            print(f"상위로 지정한 페이지를 찾을 수 없습니다: {args.under}")
            return 1
        parent = p["id"]
    labels = cfg.get("confluence.labels") or ["meeting"]
    page = cf.create_page(title, index_page_body(cfg.get("confluence.space_key"), labels[0]), parent)
    print(f"생성 완료: {cf.url(page)}")
    return 0


# --- setup / doctor ---

def _ask(prompt: str, default: str = "") -> str:
    val = input(f"  {prompt}" + (f" [{default}]" if default else "") + ": ").strip()
    return val or default


def _yes(prompt: str, default: bool = True) -> bool:
    val = input(f"  {prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    return default if not val else val.startswith("y")


def _set_yaml_value(text: str, section: str, key: str, value: str) -> str:
    """주석을 보존하려고 yaml을 다시 쓰지 않고 `section:` 블록 안의 한 줄만 바꾼다."""
    m = re.search(rf"^{re.escape(section)}:\s*\n((?:[ \t]+.*\n?|\s*\n)*)", text, flags=re.M)
    if not m:
        return text
    block = re.sub(rf"^(\s+{re.escape(key)}:\s*)[^#\n]*?(\s*(#.*)?)$",
                   lambda k: f"{k.group(1)}{value}{k.group(2)}", m.group(1), count=1, flags=re.M)
    return text[:m.start(1)] + block + text[m.end(1):]


def cmd_setup(args) -> int:
    cdir = config.CONFIG_DIR
    if args.token:
        return 0 if keychain.set_interactive("confluence") else 1
    if args.hf_token:
        return 0 if keychain.set_interactive("huggingface") else 1

    print("== MeetingAgent 설정 ==\n")
    for name in ("config", "glossary", "people"):
        dst = cdir / f"{name}.yaml"
        if not dst.exists():
            shutil.copy(cdir / f"{name}.example.yaml", dst)
            print(f"  생성: {dst}")

    cfg = config.load()
    print("\n[1/4] Confluence (Data Center)")
    text = (cdir / "config.yaml").read_text()
    if _yes("Confluence 업로드를 사용할까요?", True):
        base = _ask("Base URL (예: https://wiki.company.com)", cfg.get("confluence.base_url"))
        space = _ask("Space key", cfg.get("confluence.space_key"))
        parent = _ask("회의록 상위 페이지 제목", cfg.get("confluence.parent_title"))
        for k, v in (("enabled", "true"), ("base_url", base), ("space_key", space),
                     ("parent_title", parent)):
            text = _set_yaml_value(text, "confluence", k, v)
        (cdir / "config.yaml").write_text(text)
        if not keychain.get("confluence") or _yes("PAT(개인 액세스 토큰)를 새로 등록할까요?", False):
            print("  Confluence > 프로필 > 개인 액세스 토큰 에서 발급")
            keychain.set_interactive("confluence")
        cfg = config.load()
        try:
            space_info = pipeline.confluence_client(cfg).ping()
            print(f"  ✓ 연결 확인: {space_info.get('name', space)}")
            if _yes(f"'{parent}' 홈 페이지(목록/Action Item 리포트)를 만들까요?", True):
                cmd_confluence_init(argparse.Namespace(under=None))
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ 연결 실패: {e}\n    VPN 연결 후 `meet doctor`로 다시 확인하세요.")
    else:
        (cdir / "config.yaml").write_text(_set_yaml_value(text, "confluence", "enabled", "false"))

    print("\n[2/4] 화자 분리 (선택)")
    try:
        import pyannote.audio  # noqa: F401
        has_pyannote = True
    except ImportError:
        has_pyannote = False
    if not has_pyannote:
        print("  pyannote 미설치 → 건너뜀. 쓰려면 ./install.sh --with-diarization 후 다시 setup")
    elif _yes("화자 분리를 켤까요? (Hugging Face 토큰 필요)", True):
        print("  1) https://hf.co/pyannote/speaker-diarization-community-1 에서 약관 동의")
        print("  2) https://hf.co/settings/tokens 에서 read 토큰 발급")
        keychain.set_interactive("huggingface")
        text = (cdir / "config.yaml").read_text()
        text = _set_yaml_value(text, "diarization", "enabled", "true")
        (cdir / "config.yaml").write_text(text)

    print("\n[3/4] Whisper 모델 다운로드 (최초 1회, 약 1.6GB)")
    cfg = config.load()
    try:
        from huggingface_hub import snapshot_download

        snapshot_download(cfg.get("whisper.model"))
        print("  ✓ 완료")
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ 실패: {e} (첫 전사 때 다시 시도합니다)")

    print("\n[4/4] 자동 감시")
    if _yes(f"{cfg.inbox} 폴더를 감시해서 자동 처리할까요?", True):
        cmd_watch(argparse.Namespace(action="on"))

    print("\n완료! 다음으로 해볼 것:")
    print(f"  · 용어집/팀원 채우기: {cdir/'glossary.yaml'}, {cdir/'people.yaml'}")
    print("  · 테스트: meet rec  (10초 말하고 q)")
    return 0


def cmd_doctor(args) -> int:
    ok = True

    def check(label: str, passed: bool, hint: str = "") -> None:
        nonlocal ok
        ok &= passed
        print(f"  {'✓' if passed else '✗'} {label}" + ("" if passed or not hint else f"  → {hint}"))

    print("MeetingAgent 점검")
    check("설정 파일", config.config_exists(), "meet setup")
    cfg = config.load()
    check("ffmpeg", bool(shutil.which("ffmpeg")), "brew install ffmpeg")
    try:
        from .summarize import find_claude
        claude = find_claude(cfg.get("summarize.claude_bin"))
        check(f"claude CLI ({claude})", True)
    except RuntimeError as e:
        check("claude CLI", False, str(e))
    try:
        import mlx_whisper  # noqa: F401
        check("mlx-whisper", True)
    except ImportError:
        check("mlx-whisper", False, "./install.sh")
    try:
        from huggingface_hub import try_to_load_from_cache
        cached = try_to_load_from_cache(cfg.get("whisper.model"), "config.json")
        check(f"Whisper 모델 캐시 ({cfg.get('whisper.model')})", isinstance(cached, str),
              "첫 전사 때 자동 다운로드 (또는 meet setup)")
    except ImportError:
        pass
    if cfg.get("diarization.enabled"):
        try:
            import pyannote.audio  # noqa: F401
            check("pyannote", True)
        except ImportError:
            check("pyannote", False, "./install.sh --with-diarization")
        check("Hugging Face 토큰", bool(keychain.get("huggingface")), "meet setup --hf-token")
    print(f"  · 용어집 {len(cfg.glossary)}개, 팀원 {len(cfg.people)}명")
    if cfg.get("confluence.enabled"):
        has_token = bool(keychain.get("confluence"))
        check("Confluence 토큰 (Keychain)", has_token, "meet setup --token")
        if has_token:
            try:
                cf = pipeline.confluence_client(cfg)
                cf.ping()
                check(f"Confluence 연결 ({cfg.get('confluence.base_url')})", True)
                fixed = str(cfg.get("confluence.parent_id") or "").strip()
                if fixed:
                    try:
                        p = cf._req("GET", f"/content/{fixed}", params={"expand": "space"})
                        # 다른 스페이스의 페이지 밑에는 만들 수 없다 (업로드 때 400)
                        space = (p.get("space") or {}).get("key")
                        want = cfg.get("confluence.space_key")
                        check(f"상위 페이지 (ID {fixed}) '{p.get('title', '')}'",
                              not space or space == want,
                              f"{space} 스페이스의 페이지입니다. confluence.space_key({want})와 같은 스페이스여야 합니다")
                    except Exception as e:  # noqa: BLE001
                        check(f"상위 페이지 (ID {fixed})", False, f"ID 를 확인하세요 — {e}")
                else:
                    check(f"상위 페이지 '{cfg.get('confluence.parent_title')}'",
                          bool(cf.find_page(cfg.get("confluence.parent_title"))), "meet confluence-init")
            except Exception as e:  # noqa: BLE001
                check("Confluence 연결", False, str(e))
    else:
        print("  · Confluence 업로드 꺼짐 (로컬 minutes.md만 생성)")
    from . import watch
    print(f"  · 자동 감시: {'켜짐' if watch.is_installed() else '꺼짐'} ({cfg.inbox})")
    pend = pipeline.pending(cfg)
    if pend:
        print(f"  · 미완료 작업 {len(pend)}건 → meet retry")
    return 0 if ok else 1


COMMANDS = {"rec", "list", "ls", "open", "retry", "redo", "notes", "docs", "watch", "setup", "doctor",
            "confluence-init", "process", "help"}
INTERACTIVE = {"rec", "process", "notes", "docs", "redo", "setup"}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("help", "-h", "--help"):
        print(USAGE)
        return 0
    if argv[0] not in COMMANDS:
        argv.insert(0, "process")

    ap = argparse.ArgumentParser(prog="meet", usage=USAGE)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("process")
    p.add_argument("files", nargs="+")
    p.add_argument("--move", action="store_true", help="원본을 작업 폴더로 이동 (기본: 복사)")
    _add_hint_args(p)
    p.set_defaults(func=cmd_process)

    p = sub.add_parser("rec")
    p.add_argument("-d", "--device", help="입력 장치 (예: :1). 목록은 --list-devices")
    p.add_argument("--list-devices", action="store_true")
    _add_hint_args(p)
    p.set_defaults(func=cmd_rec)

    for name in ("list", "ls"):
        p = sub.add_parser(name)
        p.add_argument("-n", type=int, default=15)
        p.set_defaults(func=cmd_list)

    p = sub.add_parser("open")
    p.add_argument("job", nargs="?", default="last")
    p.add_argument("-f", "--folder", action="store_true", help="작업 폴더 열기")
    p.set_defaults(func=cmd_open)

    sub.add_parser("retry").set_defaults(func=cmd_retry)

    p = sub.add_parser("notes")
    p.add_argument("job")
    p.add_argument("-f", "--file", help="붙일 파일 (없으면 편집기에서 작성)")
    p.add_argument("--no-redo", action="store_true", help="회의록 재생성 없이 저장만")
    p.set_defaults(func=cmd_notes)

    p = sub.add_parser("redo")
    p.add_argument("job")
    p.add_argument("--from", dest="from_stage", default="summarize",
                   choices=["convert", "transcribe", "diarize", "summarize", "upload"])
    p.add_argument("-t", "--title")
    p.add_argument("-a", "--attendees", help="참석자, 쉼표 구분")
    p.add_argument("-l", "--location", help="회의 장소")
    p.add_argument("--time", dest="started_at", help="회의 시작 시각 (예: 14:00)")
    p.add_argument("--note")
    p.add_argument("--notes", metavar="파일", help="수기 회의록 파일을 붙이고 다시 생성")
    p.add_argument("--doc", action="append", metavar="파일", help="회의 자료를 붙이고 다시 생성")
    p.add_argument("--force", action="store_true",
                   help="업로드 이후 사람이 고친 Confluence 페이지도 덮어쓴다")
    p.set_defaults(func=cmd_redo)

    p = sub.add_parser("docs")
    p.add_argument("job")
    p.add_argument("files", nargs="+", help="워드·PDF·PPT·hwpx·txt 파일")
    p.add_argument("--no-redo", action="store_true", help="회의록 재생성 없이 붙이기만")
    p.set_defaults(func=cmd_docs)

    p = sub.add_parser("watch")
    p.add_argument("action", choices=["on", "off", "status", "log", "run"])
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("setup")
    p.add_argument("--token", action="store_true", help="Confluence PAT만 다시 등록")
    p.add_argument("--hf-token", action="store_true", help="Hugging Face 토큰만 다시 등록")
    p.set_defaults(func=cmd_setup)

    sub.add_parser("doctor").set_defaults(func=cmd_doctor)

    p = sub.add_parser("confluence-init")
    p.add_argument("--under", help="이 제목의 페이지 아래에 생성 (기본: 스페이스 최상위)")
    p.set_defaults(func=cmd_confluence_init)

    args = ap.parse_args(argv)
    try:
        return args.func(args) or 0
    except KeyboardInterrupt:
        print("\n중단됨")
        return 130
    finally:
        if args.cmd in INTERACTIVE:
            _flush_tty_input()
