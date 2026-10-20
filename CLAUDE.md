# MeetingAgent

회의 녹음을 로컬 mlx-whisper로 전사하고(pyannote 화자 분리는 선택), `claude -p`로 회의록 JSON을 만든 뒤 Confluence DC에 업로드하는 맥용 CLI(`meet`)다.

- 진입점: `bin/meet` → `meetingagent/cli.py`
- 파이프라인: `meetingagent/pipeline.py`. 회의 1건이 작업 폴더 1개이고, `state.json`의 `stages`로 단계별 캐시와 재개를 관리한다.
- 회의록 규칙: `.claude/skills/meeting-minutes/SKILL.md`. 이 파일은 헤드리스 실행의 시스템 프롬프트이기도 해서, 고치면 다음 실행부터 바로 반영된다.
- Claude는 JSON(`MINUTES_SCHEMA`, summarize.py)만 만든다. Confluence storage XML은 `render.py`가 결정적으로 만든다. 프롬프트로 XML을 만들게 하지 말 것.
- 작업 폴더의 `notes.md`(수기 회의록)가 있으면 `build_input`이 **전사본 앞에** 넣고 "전사본보다 우선한다"고 명시한다. 순서와 그 문구를 바꾸면 모델이 전사본을 더 믿어 숫자·고유명사가 틀어진다(`tests/test_core.py::TestSummarize::test_build_input_puts_notes_before_transcript`). `reset_from`은 stage 표시만 지우므로 redo 해도 notes.md는 남는다.
- 회의록 골격은 사내 템플릿(wiki pageId=329155833)을 따른다. 안건·검토 의견은 `sections`(소제목·불릿·표·강조 박스) + `conclusion`/`status` 블록이고, 표의 `status_column` 과 결정 상태는 색 라벨(status 매크로)로, Action Items 는 표 안 Task 로 그린다(`tests/test_detail_layout.py`). Action Item 기한은 Task 본문 안에 둔다 — 상위 페이지 미완료 리포트가 거기서 읽는다. 절 순서·번호와 "빈 절은 빼기"는 `tests/test_core.py::TestTemplateLayout`이 고정한다. 절을 추가/변경하면 `render.storage`·`render.markdown`·SKILL.md "결과물의 모양"·그 테스트를 **같이** 고친다.
- `1. 회의 개요`의 Page Properties(`details`) 행 이름은 `confluence.index_page_body`의 `headings`와 짝이다. 한쪽만 바꾸면 상위 페이지 목록 표의 칸이 빈다.
- 회의 자료(`materials/`, `meet docs`, `--doc`)는 수기 메모와 달리 **참고용**이다. `build_input`에서 메모 다음·전사본 앞에 "논의·결정의 근거가 아니다"와 함께 넣고, SKILL.md "회의 자료가 함께 오면"과 짝이다. 자료만 보고 결정사항을 만들게 바꾸지 말 것(`tests/test_materials.py`).
- 시작 시 질문(`_prompt_meeting_info`)은 `sys.stdin.isatty()`일 때만 한다. 자동 감시는 launchd라 물으면 거기서 멈춘다.
- 터미널 입력: 한글 자유 입력은 `_input_line`(readline, ASCII 프롬프트) + 받은 값 확인 단계로 받는다(기본 줄 편집에서 글자가 빠진 채 들어온 적이 있다). 여러 줄 붙여넣기는 `_classify_notes_input`이 Ctrl+D까지 읽고, 대화형 명령은 끝날 때 `_flush_tty_input`으로 남은 입력을 버린다 — 남은 줄이 셸에서 명령으로 실행됐던 사고 때문이다.
- 재업로드는 `state.json`의 `page.version`과 서버 버전이 같을 때만 덮어쓴다. 다르면 `ConfluenceConflict`로 멈추고 status 가 `conflict` 가 된다(= `meet retry` 대상에서 빠진다). 덮어쓰려면 `meet redo … --from upload --force`. `--force`는 그 실행에만 적용하고 state.json에 저장하지 않는다(저장하면 실패 후 자동 재시도가 확인 없이 덮는다, `tests/test_confluence_guard.py`). **이 가드를 끄거나 우회하는 코드를 넣지 말 것** — 회의록은 사람이 고치는 문서다.
- 라벨은 `sync_labels`로 맞춘다. 지우는 대상은 `state.json`의 `page.labels`(=지난번에 우리가 붙인 것) 중 빠진 것뿐이다. 사람이 단 라벨은 건드리지 않는다.
- 상위 페이지는 `confluence.parent_id`(pageId 고정)가 있으면 그것만 쓰고 `parent_title`·`hierarchy`는 무시한다.
- `config/*.yaml`은 사내 정보라 커밋 금지다. `*.example.yaml`만 커밋한다.
- 테스트: `PYTHONPATH=. .venv/bin/python -m unittest discover -s tests`
- 격리 테스트: `MEETINGAGENT_CONFIG_DIR=<dir>`, `MEETINGAGENT_CONFLUENCE_TOKEN=<tok>` 환경변수
