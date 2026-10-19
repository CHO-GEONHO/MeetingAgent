# MeetingAgent

회의 녹음을 로컬 mlx-whisper로 전사하고(pyannote 화자 분리는 선택), `claude -p`로 회의록 JSON을 만든 뒤 Confluence DC에 업로드하는 맥용 CLI(`meet`)다.

- 진입점: `bin/meet` → `meetingagent/cli.py`
- 파이프라인: `meetingagent/pipeline.py`. 회의 1건이 작업 폴더 1개이고, `state.json`의 `stages`로 단계별 캐시와 재개를 관리한다.
- 회의록 규칙: `.claude/skills/meeting-minutes/SKILL.md`. 이 파일은 헤드리스 실행의 시스템 프롬프트이기도 해서, 고치면 다음 실행부터 바로 반영된다.
- Claude는 JSON(`MINUTES_SCHEMA`, summarize.py)만 만든다. Confluence storage XML은 `render.py`가 결정적으로 만든다. 프롬프트로 XML을 만들게 하지 말 것.
- `config/*.yaml`은 사내 정보라 커밋 금지다. `*.example.yaml`만 커밋한다.
- 테스트: `PYTHONPATH=. .venv/bin/python -m unittest discover -s tests`
- 격리 테스트: `MEETINGAGENT_CONFIG_DIR=<dir>`, `MEETINGAGENT_CONFLUENCE_TOKEN=<tok>` 환경변수
