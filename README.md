# MeetingAgent

회의 녹음 파일 하나만 넣으면 **로컬 전사 → Claude 회의록 → Confluence 업로드**까지 자동으로 끝내는 맥용 도구입니다.

```
녹음(m4a) ─▶ mlx-whisper 전사 ─▶ (pyannote 화자 분리) ─▶ claude -p + 회의록 skill ─▶ Confluence DC
            ── 로컬, 음성은 맥 밖으로 안 나감 ──        ── 텍스트만 전송 ──           ── VPN ──
```

- **비용 0원, 무제한**: 전사는 Apple Silicon에서 로컬로 돌립니다. 35초 음성은 3초 정도 걸리고, 2시간 회의는 대략 10분 안팎이에요.
- **회의록 형식 고정**: 결정사항, Action Items(담당/기한/근거 타임스탬프), 논의 내용, 미결 순서예요. 없는 담당자나 기한은 지어내지 않습니다.
- **Confluence를 DB처럼**: Page Properties와 라벨로 회의 목록 표를 만들고, Task로 "내 미완료 액션"을 모아볼 수 있어요.
- **중간에 실패해도 이어서**: VPN이 꺼져 있어도 회의록까지는 만들어두고, 나중에 `meet retry`로 업로드만 하면 돼요.

## 설치 (최초 1회, 5분)

```bash
git clone https://github.com/CHO-GEONHO/MeetingAgent.git ~/MeetingAgent
cd ~/MeetingAgent
./install.sh                    # 화자 분리까지: ./install.sh --with-diarization
meet setup                      # Confluence 주소/스페이스, PAT(Keychain 저장), 모델 다운로드, 자동 감시
```

> `meet: command not found`가 나오면 `echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc && exec zsh`

필요한 것: Apple Silicon 맥, Homebrew, 로그인된 [Claude Code](https://claude.com/claude-code) CLI, 그리고 Confluence 개인 액세스 토큰(프로필 > 개인 액세스 토큰).

## 쓰는 법

### 1. 제일 쉬운 방법: 폴더에 넣기
`meet setup`에서 자동 감시를 켰다면 녹음 파일을 **`~/MeetingInbox`에 넣기만** 하면 됩니다. 끝나면 macOS 알림이 와요.

- 음성 메모 앱에서는 녹음을 Finder의 `~/MeetingInbox`로 드래그하거나, 공유 > 파일에 저장을 쓰면 돼요.
- VPN이 꺼져 있으면 업로드만 대기 상태로 남고, 15분마다 자동으로 다시 시도해요.

### 2. 터미널에서 바로 녹음
```bash
meet rec -t "주간 인프라 회의" -a "홍길동,김철수"
# ● 녹음 중… q 또는 Ctrl+C로 끝내면 → 전사 → 회의록 → 업로드
```

### 3. 파일 지정
```bash
meet ~/Downloads/회의.m4a
meet 회의.m4a -p "AKS 업그레이드" --type 기술논의 -n 4 --note "보안 이슈 위주로"
meet 회의.m4a --no-upload        # 로컬 minutes.md만
```

### 관리
```bash
meet list                         # 최근 회의와 상태 (✅ 완료 📝 업로드 대기 ❌ 실패)
meet open                         # 최근 회의록 페이지 열기  (meet open -f: 작업 폴더)
meet retry                        # 실패/미업로드 건 이어서 처리
meet redo 2026-10-19 --from summarize   # 용어집 고친 뒤 회의록만 다시 생성 (같은 페이지 갱신)
meet redo 2026-10-19 --from upload --force   # 사람이 고친 페이지를 덮어쓸 때만
meet doctor                       # 환경 점검
meet watch on|off|status|log
```

회의록 내용을 대화로 고치고 싶으면, 이 레포에서 Claude Code를 열고 이렇게 요청하면 돼요.
> 마지막 회의록에서 공지 작성 담당을 홍길동으로 바꾸고 다시 올려줘

`.claude/skills/meeting-minutes` skill이 `minutes.json`을 고치고 `meet redo … --from upload`로 같은 페이지를 갱신합니다.

## 정확도 올리기: 용어집과 팀원

정확도에 제일 큰 영향을 주는 건 `config/glossary.yaml`과 `config/people.yaml`이에요. 둘 다 gitignore 대상이라 사내 정보는 커밋되지 않습니다.

```yaml
# glossary.yaml: Whisper 프롬프트로 인식 단계에서 유도하고, Claude가 한 번 더 보정
terms:
  - term: FluxCD
    variants: [플럭스 시디, 플럭스CD]

# people.yaml: 호칭을 정식 이름으로 정규화하고, username이 있으면 @멘션
people:
  - name: 홍길동
    aliases: [길동님, 홍 매니저]
    username: gdhong
```

회의록 하단 "전사 보정" 줄에 자주 나오는 오인식이 보이면 `variants`에 추가하세요.

## Confluence 구조

```
회의록                    ← meet confluence-init: 전체 회의 표 + 미완료 Action Item 리포트
└ 회의록 2026
  └ 회의록 2026-10
    └ [2026-10-19] AKS 1.30 업그레이드 일정 논의
        Page Properties(날짜/유형/프로젝트/참석자) · 요약 · 결정사항 · Task(@담당 + 기한)
        · 논의 · 미결 · 원문 전사본(펼치기)
        라벨: meeting, proj-aks-upgrade, kubernetes …
```

- 프로젝트별로 모아보려면 라벨 `proj-*`를 쓰세요. CQL 예시는 `label = "proj-sbom"`이에요.
- 계층은 `config.yaml`의 `confluence.hierarchy`(`none` | `year` | `year_month`)로 바꿀 수 있어요.

**상위 페이지를 하나로 고정하고 싶다면** `confluence.parent_id`에 그 페이지의 `pageId`를 넣으세요.
연/월 묶음 없이 전부 그 페이지 바로 밑에 `[2026-10-19] 회의 제목`으로 쌓이고, 상위 페이지 제목이
바뀌어도 따라갑니다. (`parent_title`과 `hierarchy`는 무시됩니다.)

```yaml
confluence:
  parent_id: "329166233"
```

### 사람이 고친 회의록은 덮어쓰지 않습니다

업로드한 뒤 페이지를 직접 고쳤다면, `meet redo … --from upload`는 **멈추고 알려줍니다.**
회의록은 고쳐 쓰라고 만든 문서라 자동 재생성이 사람 수정을 지우면 안 되기 때문이에요.

```
업로드 중단
업로드 이후 페이지가 수정되었습니다 (v3 → v5, 수정: 홍길동).
  덮어쓰지 않았습니다. 먼저 내용을 확인하세요:
    https://wiki.example.com/pages/viewpage.action?pageId=12345
  확인 후 그래도 이쪽으로 덮어쓰려면: meet redo <작업> --from upload --force
```

이 상태는 `meet list`에 ⚠️로 보이고, `meet retry`가 자동으로 다시 시도하지 않습니다.
라벨도 **MeetingAgent가 붙였던 것만** 정리하고, 사람이 손으로 단 라벨은 건드리지 않아요.

## 화자 분리 (선택)

```bash
./install.sh --with-diarization
meet setup                   # HF 토큰 입력 (모델 페이지에서 약관 동의 필요)
```
대면 회의에서 마이크 하나로 녹음하면 화자 분리가 완벽하지 않아요. 그래서 라벨은 "화자A/B"로 붙이고, 이름은 Claude가 문맥(호명, 자기소개)으로 추정합니다. 확실하지 않으면 "(추정)"으로 표시해요. 참석자 수를 `-n 4`처럼 알려주면 더 정확해져요.

## 데이터와 보안

| 데이터 | 위치 | 비고 |
|---|---|---|
| 음성 | 로컬만 | 업로드가 끝나면 원본 삭제 (`retention.delete_audio`) |
| 전사본/회의록 | 로컬 `~/MeetingAgent-data/meetings/*`, Confluence | Claude로는 **텍스트만** 전송 |
| 토큰 | macOS Keychain (`meetingagent.*`) | 평문 파일 없음 |

## 작업 폴더

```
~/MeetingAgent-data/meetings/2026-10-19_1430_주간-인프라-회의/
  transcript.txt   transcript.json   minutes.json   minutes.md   page.xml   state.json   (error.log)
```

## 문제 해결

| 증상 | 해결 |
|---|---|
| `Confluence 접속 실패` | VPN을 켜고 `meet retry` |
| `인증 실패(401)` | `meet setup --token` |
| `업로드 이후 페이지가 수정되었습니다` | 페이지를 열어 확인 후, 덮어써도 되면 `meet redo <작업> --from upload --force` |
| `상위 페이지 … ID 를 확인하세요` | `confluence.parent_id`의 pageId가 맞는지 확인 (`meet doctor`) |
| 녹음 파일이 비어 있음 | 시스템 설정 > 개인정보 보호 > 마이크에서 터미널 허용 |
| 같은 문장이 반복 전사됨 | Whisper 환각이에요. 기본으로 반복을 필터링하고, 계속되면 `whisper.condition_on_previous_text: false`인지 확인 |
| 감시가 동작 안 함 | `meet watch log`, `meet doctor` |
| 온라인 회의 소리 녹음 | [BlackHole](https://github.com/ExistentialAudio/BlackHole)로 다중 출력 장치를 만들고 `meet rec --list-devices`, `meet rec -d :2` |

## 개발

```bash
PYTHONPATH=. .venv/bin/python -m unittest discover -s tests
```
