# MeetingAgent

회의 녹음 파일 하나만 넣으면 **로컬 전사 → Claude 회의록 → Confluence 업로드**까지 자동으로 끝내는 맥용 도구입니다.

```
녹음(m4a) ─▶ mlx-whisper 전사 ─▶ (pyannote 화자 분리) ─▶ claude -p + 회의록 skill ─▶ Confluence DC
            ── 로컬, 음성은 맥 밖으로 안 나감 ──        ── 텍스트만 전송 ──           ── VPN ──
```

- **비용 0원, 무제한**: 전사는 Apple Silicon에서 로컬로 돌립니다. 35초 음성은 3초 정도 걸리고, 2시간 회의는 대략 10분 안팎이에요.
- **사내 회의록 템플릿 고정**: 회의 개요(주제·일시·장소·참석) → 안건별 정리 → 결정·합의(상태 태그) → Action Items → 미결 → 담당자 검토 의견 순서로 절 번호까지 붙여 만듭니다. **근거가 없는 절은 넣지 않아요** — 빈 절을 채우려고 지어내는 것보다 낫기 때문입니다.
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
meet rec
```
녹음을 시작하기 **전에** 회의 정보를 물어봅니다. Enter로 건너뛰면 전사본에서 추정해요.
```
회의 정보 — Enter 로 건너뛰면 전사본에서 추정합니다
  회의 제목: 주간 인프라 회의
  참석자 (쉼표 구분): 홍길동, 김철수
  회의 장소: 본사 9층 회의실
  회의 시작 시각 [14:03]:
● 녹음 중… q 또는 Ctrl+C로 끝내면 → 전사 → 회의록 → 업로드
```

미리 알고 있으면 플래그로 줘도 되고, 그러면 그 항목은 묻지 않아요.
```bash
meet rec -t "주간 인프라 회의" -a "홍길동,김철수" -l "본사 9층 회의실" --time 14:00
meet rec -y                      # 아무것도 묻지 않고 바로 녹음
```

### 3. 파일 지정
```bash
meet ~/Downloads/회의.m4a        # 이때도 회의 정보를 물어봅니다
meet 회의.m4a -p "AKS 업그레이드" --type 기술논의 -n 4 --note "보안 이슈 위주로"
meet 회의.m4a --no-upload        # 로컬 minutes.md만
```

> `~/MeetingInbox` 자동 감시로 처리될 때는 **묻지 않습니다**(백그라운드라 물으면 멈춰요).
> 장소·시각이 필요하면 나중에 `meet redo <작업> --from summarize -l "..." ` 로 다시 만들면 됩니다.

## 수기 회의록을 같이 반영하기

회의 중에 직접 적은 메모가 있으면 **그걸 전사본보다 우선**해서 회의록을 만듭니다.
Whisper는 숫자와 고유명사를 자주 틀리는데(`64EA`→"육십사이에이", `22대`, 제품명),
사람이 적은 메모에는 그런 왜곡이 없기 때문이에요.

```bash
meet rec --notes ~/notes.md       # 파일로 미리 주기
meet 회의.m4a --notes ~/notes.md
```

`meet rec` 는 녹음이 끝나면 물어봅니다. 세 가지 방법 중 편한 걸로 넣으면 돼요.
```
회의 중 적은 수기 회의록이 있나요? [y/N]: y
  수기 회의록 넣는 방법 (셋 중 하나)
    · 내용 붙여넣기 → 다 붙였으면 Enter, 그다음 Ctrl+D
    · 파일을 이 창으로 끌어다 놓기 → Enter
    · 편집기에서 쓰기 → 그냥 Enter
```

이미 만들어진 회의록에 나중에 붙일 수도 있어요. **같은 Confluence 페이지가 갱신**됩니다.
```bash
meet notes 2026-10-19             # 편집기로 작성/수정 → 회의록 재생성 여부를 물음
meet notes 2026-10-19 -f ~/notes.md
meet redo 2026-10-19 --from summarize --notes ~/notes.md
```

메모는 문장이 아니어도 됩니다. 적힌 대로 두면 Claude가 전사본 맥락과 합쳐 정리해요.

- 메모와 전사본이 어긋나면 **메모를 따릅니다** (숫자·제품명·날짜)
- **메모에만 있는 내용도 들어갑니다** — 녹음이 못 잡았거나 끝나고 적은 것
- 메모의 우선순위·데드라인은 결정사항이나 Action Item으로 올라갑니다

## 회의 중 띄운 자료(워드·PDF·PPT) 같이 주기

회의 때 화면에 띄운 안건지·보고서·발표자료도 넣을 수 있어요. `meet rec`는 녹음이 끝나면
"자료가 있으면 파일을 끌어다 놓으세요"라고 묻고, 나중에 붙일 수도 있어요 (같은 페이지가 갱신됨).

```bash
meet docs 2026-10-20_1702 ~/Downloads/KT\ 아젠다.docx      # 붙이고 → 회의록 재생성 여부를 물음
meet rec --doc 안건.docx --doc 보고서.pdf                   # 미리 주기 (여러 개 가능)
```

자료는 수기 메모와 다르게 **참고용**이에요. 숫자·제품명·고객사명 표기를 바로잡고 안건을 나누는 데만 쓰고,
자료에만 있고 회의에서 다루지 않은 내용은 결정사항이나 Action Item에 넣지 않아요.
지원 형식은 `.docx .doc .rtf .odt .html .pdf .pptx .hwpx .txt .md`예요. 한글 `.hwp`는 PDF나 `.hwpx`로 저장해서 넣으세요.
페이지 하단에 "참고 자료: 파일명"이 붙고, 원본은 작업 폴더의 `materials/`에 보관돼요(컨플에 올리지는 않아요).

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
회의록 정리               ← meet confluence-init: 전체 회의 표 + 미완료 Action Item 리포트
└ [2026-10-19] AKS 1.30 업그레이드 일정 논의
     핵심 요약(패널)
     1. 회의 개요        Page Properties(주제·날짜·장소·유형·프로젝트·참석·Action Items)
        1.1 배경 / 1.2 진행 순서      ← 전사본에 근거가 있을 때만
        1.3 안건별 결론               표: 안건 | 결론 | 상태(색 라벨)
     2~N. 안건 N — <주제>
        2.1 현황 / 2.2 논의 / 2.3 요청과 답변 …   소제목·불릿·표·주의/판단 박스
        결론 [합의]                    안건마다 결론 한 줄 + 상태
     N+1. 결정·합의 사항   표 + 색 라벨(합의=초록, 진행 중=파랑, 확인 필요=노랑, 보류=회색, 불가=빨강)
     N+2. Action Items    표(할 일 | 담당 | 근거), 할 일 칸은 Confluence Task(체크박스 + 기한)
     N+3. 미결 / 다음에 다룰 것
     N+4. 담당자 검토 의견 및 후속 논의 안건
     ─ 화자 추정 · 전사 보정 · 원문 전사본(펼치기)
     라벨: meeting, proj-aks-upgrade, kubernetes …
```

절 번호는 **있는 절만** 세어서 붙습니다. `1. 회의 개요`의 Page Properties 표가 상위
페이지의 목록 표를 채우므로, 행 이름(날짜·유형·프로젝트·참석자·Action Items)은 바꾸지 마세요.

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
  transcript.txt   transcript.json   notes.md   minutes.json   minutes.md   page.xml   state.json   (error.log)
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
