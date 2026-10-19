"""`meet` CLI."""
from __future__ import annotations

import argparse
import re
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

STATUS_ICON = {"done": "✅", "summarized": "📝", "failed": "❌", "new": "⏳"}


def _add_hint_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("-t", "--title", help="회의 제목 (없으면 Claude가 생성)")
    p.add_argument("-p", "--project", help="프로젝트 힌트")
    p.add_argument("-a", "--attendees", help="참석자, 쉼표 구분")
    p.add_argument("--type", dest="meeting_type", help="정기회의|기술논의|장애회고|1on1|외부미팅")
    p.add_argument("-n", "--speakers", type=int, help="화자 수 (화자 분리 정확도 향상)")
    p.add_argument("--note", help="Claude에게 줄 추가 메모 (예: '보안 이슈 위주로')")
    p.add_argument("--date", help="회의 일시 덮어쓰기 'YYYY-MM-DD HH:MM'")
    p.add_argument("--no-upload", action="store_true", help="Confluence 업로드 생략 (로컬 md만)")


def _hints(args) -> dict:
    return {
        "title": args.title,
        "project": args.project,
        "attendees": [a.strip() for a in args.attendees.split(",")] if args.attendees else None,
        "meeting_type": args.meeting_type,
        "speakers": args.speakers,
        "note": args.note,
    }


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
            ok &= pipeline.run(job, cfg, upload=not args.no_upload)
    return 0 if ok else 1


def cmd_rec(args) -> int:
    cfg = _require_setup()
    if args.list_devices:
        print(audio.list_devices())
        return 0
    rec_dir = cfg.workdir / "recordings"
    rec_dir.mkdir(parents=True, exist_ok=True)
    dst = rec_dir / f"{datetime.now():%Y-%m-%d_%H%M}.m4a"
    print(f"● 녹음 중… 종료하려면 q 또는 Ctrl+C   ({dst})")
    audio.record(dst, args.device or cfg.get("recording.device"))
    print(f"\n녹음 저장: {dst}")
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


def cmd_redo(args) -> int:
    cfg = _require_setup()
    job = pipeline.find_job(cfg, args.job)
    if args.from_stage in ("transcribe", "convert") and not (job.source and job.source.exists()):
        print("원본 음성이 이미 삭제되어 재전사할 수 없습니다. --from summarize 는 가능합니다.")
        return 1
    if args.title:
        job.state.setdefault("hints", {})["title"] = args.title
    if args.note:
        job.state.setdefault("hints", {})["note"] = args.note
    job.reset_from(args.from_stage)
    with pipeline.global_lock(cfg):
        return 0 if pipeline.run(job, cfg) else 1


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


COMMANDS = {"rec", "list", "ls", "open", "retry", "redo", "watch", "setup", "doctor",
            "confluence-init", "process", "help"}


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

    p = sub.add_parser("redo")
    p.add_argument("job")
    p.add_argument("--from", dest="from_stage", default="summarize",
                   choices=["convert", "transcribe", "diarize", "summarize", "upload"])
    p.add_argument("-t", "--title")
    p.add_argument("--note")
    p.set_defaults(func=cmd_redo)

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
