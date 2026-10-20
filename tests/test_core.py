import json
import unittest
from datetime import date

from meetingagent import diarize, render, summarize, transcribe

PEOPLE = [{"name": "홍길동", "aliases": ["길동님"], "username": "gdhong"},
          {"name": "김철수", "aliases": ["철수"]}]


def minutes(**over):
    base = summarize.normalize({
        "title": "AKS 업그레이드", "meeting_type": "기술논의", "project": "AKS",
        "attendees": ["홍길동", "김철수"], "summary": "1.30으로 올리기로 함 <확정>",
        "decisions": ["다음 주 화요일에 스테이징부터 진행하기로 함"],
        "action_items": [
            {"owner": "길동님", "task": "노드풀 & 애드온 호환성 확인", "due": "2026-10-23", "evidence": "00:03:10"},
            {"owner": "", "task": "공지 작성", "due": "다음주", "evidence": ""},
        ],
        "discussion": [{"topic": "일정", "points": ["업무 시간 외 진행"]}],
        "open_questions": [], "speaker_map": {"화자A": "홍길동(추정)"},
        "corrections": [{"from": "에이케이에스", "to": "AKS"}], "labels": ["proj-aks-upgrade", "쿠버", "K8s Ops"],
    })
    base.update(over)
    return base


class TestTranscribe(unittest.TestCase):
    def test_clean_removes_hallucinations_and_loops(self):
        segs = [{"start": i, "end": i + 1, "text": t} for i, t in enumerate(
            ["안녕하세요", "시청해주셔서 감사합니다.", "네", "네", "네", "네", "다음 안건"])]
        texts = [s["text"] for s in transcribe.clean_segments(segs)]
        self.assertEqual(texts, ["안녕하세요", "네", "네", "다음 안건"])

    def test_initial_prompt_is_bounded(self):
        glossary = [{"term": f"TERM{i}"} for i in range(200)]
        p = transcribe.build_initial_prompt(glossary, PEOPLE, max_chars=120)
        self.assertLessEqual(len(p), 125)
        self.assertIn("TERM0", p)

    def test_format_groups_same_speaker(self):
        segs = [{"start": 0, "end": 2, "text": "a", "speaker": "화자A"},
                {"start": 2, "end": 4, "text": "b", "speaker": "화자A"},
                {"start": 3725, "end": 3726, "text": "c", "speaker": "화자B"}]
        self.assertEqual(transcribe.format_transcript(segs),
                         "[00:00:00] 화자A: a b\n[01:02:05] 화자B: c\n")

    def test_format_splits_without_speaker_and_long_turns(self):
        no_spk = [{"start": 0, "end": 1, "text": "a"}, {"start": 1, "end": 2, "text": "b"}]
        self.assertEqual(transcribe.format_transcript(no_spk), "[00:00:00] a\n[00:00:01] b\n")
        long = [{"start": t, "end": t + 1, "text": str(t), "speaker": "화자A"} for t in (0, 30, 60)]
        self.assertEqual(transcribe.format_transcript(long).count("\n"), 2)


class TestDiarize(unittest.TestCase):
    def test_assign_by_overlap_and_nearest(self):
        turns = [{"start": 0, "end": 5, "speaker": "S1"}, {"start": 5, "end": 10, "speaker": "S2"}]
        segs = [{"start": 1, "end": 6, "text": "x"}, {"start": 6, "end": 9, "text": "y"},
                {"start": 12, "end": 13, "text": "z"}]
        out = diarize.relabel(diarize.assign_speakers(segs, turns))
        self.assertEqual([s["speaker"] for s in out], ["화자A", "화자B", "화자B"])


class TestSummarize(unittest.TestCase):
    def test_parse_structured_output(self):
        env = {"type": "result", "is_error": False, "result": "",
               "structured_output": {"title": "t", "action_items": [{"task": "do"}]}}
        m = summarize.parse_claude_output(json.dumps(env))
        self.assertEqual(m["action_items"], [{"owner": "", "task": "do", "due": "", "evidence": ""}])
        self.assertEqual(m["meeting_type"], "기타")

    def test_parse_fenced_result_fallback(self):
        env = {"is_error": False, "result": '```json\n{"title": "x"}\n```'}
        self.assertEqual(summarize.parse_claude_output(json.dumps(env))["title"], "x")

    def test_labels_sanitized(self):
        self.assertEqual(minutes()["labels"], ["proj-aks-upgrade", "k8s-ops"])

    def test_skill_prompt_strips_frontmatter_and_interactive_section(self):
        p = summarize.load_skill_prompt()
        self.assertNotIn("name: meeting-minutes", p)
        self.assertNotIn("대화형으로", p)
        self.assertIn("지어내지 않는다", p)

    def test_build_input(self):
        text = summarize.build_input(meeting_date=date(2026, 10, 19), duration_sec=3600,
                                     hints={"attendees": ["a", "b"]}, glossary=[{"term": "AKS", "variants": ["에이케이에스"]}],
                                     people=PEOPLE, transcript="[00:00:00] hi")
        self.assertIn("2026-10-19 (월)", text)
        self.assertIn("참석자 힌트: a, b", text)
        self.assertIn("AKS ← 에이케이에스", text)

    def test_build_input_puts_notes_before_transcript(self):
        """수기 메모가 전사본보다 앞에 오고 우선한다고 적혀야 한다.

        Whisper 는 숫자·고유명사를 틀린다(64EA, 22대). 사람이 적은 메모가 그 교정본이라,
        순서와 문구가 바뀌면 모델이 전사본을 더 믿게 된다.
        """
        text = summarize.build_input(
            meeting_date=date(2026, 10, 19), duration_sec=None, hints={},
            glossary=[], people=[], transcript="[00:00:00] 육십사이에이",
            notes="노보가라 64EA 스캐너를 미리 오픈한다")
        self.assertLess(text.index("# 수기 회의록"), text.index("# 전사본"))
        self.assertIn("전사본보다 우선한다", text)
        self.assertIn("64EA", text)

    def test_build_input_without_notes_has_no_notes_section(self):
        text = summarize.build_input(
            meeting_date=date(2026, 10, 19), duration_sec=None, hints={},
            glossary=[], people=[], transcript="hi", notes="   \n  ")
        self.assertNotIn("수기 회의록", text)


class TestRender(unittest.TestCase):
    def test_storage_mentions_escaping_and_dates(self):
        r = render.Renderer(PEOPLE, lambda u: "KEY-" + u if u == "gdhong" else None)
        xml = r.storage(minutes(), meeting_date=date(2026, 10, 19), duration_sec=1800,
                        transcript_lines=["[00:00:00] 화자A: <script>"], source_name="a.m4a")
        self.assertIn('<ri:user ri:userkey="KEY-gdhong" />', xml)  # 별칭 → 멘션
        self.assertIn("김철수", xml)                                  # username 없음 → 텍스트
        self.assertIn('<time datetime="2026-10-23" />', xml)
        self.assertIn("기한 미정", xml)                               # '다음주'는 날짜 아님
        self.assertIn("담당 미정", xml)
        self.assertIn("&lt;script&gt;", xml)
        self.assertIn("노드풀 &amp; 애드온", xml)
        self.assertNotIn("<script>", xml)
        self.assertIn('ac:name="details"', xml)
        self.assertIn('ac:name="expand"', xml)

    def test_storage_is_well_formed(self):
        import xml.dom.minidom

        xml_body = render.Renderer(PEOPLE).storage(minutes(), meeting_date=date(2026, 10, 19),
                                                   duration_sec=None, transcript_lines=None)
        wrapped = ('<root xmlns:ac="http://atlassian.com/content" '
                   'xmlns:ri="http://atlassian.com/resource/identifier">' + xml_body + "</root>")
        xml.dom.minidom.parseString(wrapped)  # 파싱 실패 시 예외

    def test_markdown(self):
        md = render.markdown(minutes(), meeting_date=date(2026, 10, 19), duration_sec=None)
        self.assertTrue(md.startswith("# [2026-10-19] AKS 업그레이드"))
        self.assertIn("- [ ] **길동님** 노드풀 & 애드온 호환성 확인 (2026-10-23)", md)

    def test_find_person_strips_honorifics(self):
        self.assertEqual(render.find_person("홍길동(추정)", PEOPLE)["name"], "홍길동")
        self.assertEqual(render.find_person("철수님", PEOPLE)["name"], "김철수")
        self.assertIsNone(render.find_person("", PEOPLE))


class TestTemplateLayout(unittest.TestCase):
    """사내 회의록 템플릿(wiki pageId=329155833) 골격을 고정한다."""

    def _headings(self, m):
        import re
        xml = render.Renderer(PEOPLE).storage(m, meeting_date=date(2026, 9, 23),
                                              duration_sec=None, transcript_lines=None)
        return re.findall(r"<h2>([^<]+)</h2>", xml), xml

    def test_sections_are_numbered_in_template_order(self):
        m = minutes(
            location="KCB 사옥 12층", started_at="10:00",
            background="1차 협의 후속", sequence=["설명", "질의응답"],
            discussion=[{"topic": "기능 설명", "points": ["a"]}, {"topic": "인프라", "points": ["b"]}],
            open_questions=["부하 기준"],
            review_notes=[{"topic": "현장 수정", "points": ["왕복 소요가 관건"]}],
        )
        heads, xml = self._headings(m)
        self.assertEqual(heads, [
            "1. 회의 개요",
            "2. 안건 1 — 기능 설명",
            "3. 안건 2 — 인프라",
            "4. 결정·합의 사항",
            "5. Action Items",
            "6. 미결 / 다음에 다룰 것",
            "7. 담당자 검토 의견 및 후속 논의 안건",
        ])
        self.assertIn("<h3>1.1 배경</h3>", xml)
        self.assertIn("<h3>1.2 진행 순서</h3>", xml)
        self.assertIn("KCB 사옥 12층", xml)
        self.assertIn("10:00", xml)

    def test_empty_sections_are_omitted(self):
        """근거가 없으면 절이 통째로 빠져야 한다. 빈 절을 채우려는 유혹을 막는 장치다."""
        heads, xml = self._headings(minutes(open_questions=[], review_notes=[],
                                            background="", sequence=[]))
        self.assertEqual(heads, ["1. 회의 개요", "2. 안건 1 — 일정",
                                 "3. 결정·합의 사항", "4. Action Items"])
        self.assertNotIn("배경", xml)
        self.assertNotIn("진행 순서", xml)

    def test_attendee_groups_render_per_org(self):
        m = minutes(attendee_groups=[{"org": "KCB", "members": ["신민준"]},
                                     {"org": "AISpera", "members": ["홍길동"]}])
        _, xml = self._headings(m)
        self.assertIn("<th>참석 (KCB)</th>", xml)
        self.assertIn("<th>참석 (AISpera)</th>", xml)
        # Page Properties Report 가 읽는 '참석자' 행은 합쳐서 그대로 남아야 한다
        self.assertIn("<th>참석자</th>", xml)

    def test_decisions_accept_old_string_form(self):
        """예전 minutes.json(문자열 배열)을 redo 해도 깨지지 않는다."""
        d = summarize.normalize({"decisions": ["옛 형식"]})["decisions"]
        self.assertEqual(d, [{"text": "옛 형식", "status": "합의"}])

    def test_decision_status_shown(self):
        m = minutes(decisions=[{"text": "범위 전환", "status": "확정 대기"}])
        _, xml = self._headings(m)
        # 템플릿처럼 색 라벨(status 매크로)로: 확정 대기 = 노랑
        self.assertIn('<ac:parameter ac:name="colour">Yellow</ac:parameter>'
                      '<ac:parameter ac:name="title">확정 대기</ac:parameter>', xml)

    def test_markdown_mirrors_storage_sections(self):
        m = minutes(location="본사 9층", started_at="14:00")
        md = render.markdown(m, meeting_date=date(2026, 9, 23), duration_sec=None)
        for s in ("## 1. 회의 개요", "| 장소 | 본사 9층 |", "| 일시 | 2026-09-23 14:00 |",
                  "## 3. 결정·합의 사항", "## 4. Action Items"):
            self.assertIn(s, md)


class TestSetup(unittest.TestCase):
    def test_set_yaml_value_is_section_scoped(self):
        from pathlib import Path

        from meetingagent import cli
        from meetingagent.config import REPO_ROOT, CONFIG_DIR

        text = (REPO_ROOT / "config" / "config.example.yaml").read_text()
        out = cli._set_yaml_value(text, "confluence", "enabled", "false")
        out = cli._set_yaml_value(out, "confluence", "base_url", "https://wiki.corp")
        out = cli._set_yaml_value(out, "diarization", "enabled", "true")
        import yaml
        d = yaml.safe_load(out)
        self.assertFalse(d["confluence"]["enabled"])
        self.assertTrue(d["diarization"]["enabled"])
        self.assertEqual(d["confluence"]["base_url"], "https://wiki.corp")
        self.assertIn("# Data Center 주소", out)  # 주석 보존


if __name__ == "__main__":
    unittest.main()
