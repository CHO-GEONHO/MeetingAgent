"""안건을 템플릿처럼 자세히: 소제목·표·색 라벨·강조 박스·안건별 결론, 표 안의 Action Item 체크박스."""
import re
import unittest
import xml.dom.minidom
from datetime import date

from meetingagent import render, summarize

TABLE = {"columns": ["KT 요청", "우리 답변", "상태"],
         "rows": [["IP 자산 200만 개 등록", "비용 산정 후 임계치 전달", "확인 필요"],
                  ["배너 없는 포트 보존", "KT 자산 분기 추가 개발", "진행 중"],
                  ["도메인 자체 등록", "2~3일 내 오픈", "합의"]],
         "status_column": "상태"}


def detailed():
    return summarize.normalize({
        "title": "KT 아젠다", "meeting_type": "기술논의", "attendees": ["조건호"],
        "summary": "요약", "decisions": [{"text": "임계치는 계약 금액 기준", "status": "합의"}],
        "action_items": [{"owner": "", "task": "(개발팀) 분기 로직 개발", "due": "2026-10-31",
                          "evidence": "00:15:30"}],
        "discussion": [{
            "topic": "KT 자산 스캔 임계치", "conclusion": "기술 이슈 없음, 비용 이슈로 전달", "status": "합의",
            "sections": [
                {"heading": "현황", "points": ["KT 자산 약 200만 개", "일 12시간 스캔 시 일 200만 원"]},
                {"heading": "KT 요청과 답변", "points": [], "table": TABLE},
                {"heading": "주의", "points": [],
                 "callout": {"type": "warning", "title": "전제", "text": "임대 기반 스캔 환경이라 IDC 협력이 관건"}},
                {"heading": "빈 소절", "points": []},          # 내용 없으면 빠진다
            ]}],
        "review_notes": [{"topic": "비용 구조", "conclusion": "", "status": "",
                          "sections": [{"heading": "판단", "points": ["월 8천만 원 수준"]}]}],
    })


class TestNormalizeBlocks(unittest.TestCase):
    def test_legacy_points_become_a_section(self):
        d = summarize.normalize({"discussion": [{"topic": "일정", "points": ["a", " "]}]})["discussion"][0]
        self.assertEqual(d["sections"], [{"heading": "", "points": ["a"], "table": None, "callout": None}])

    def test_table_rows_padded_and_bad_status_column_dropped(self):
        t = summarize.normalize_table({"columns": ["a", "b"], "rows": [["1"], ["2", "3", "4"], ["", ""]],
                                       "status_column": "없는열"})
        self.assertEqual(t, {"columns": ["a", "b"], "rows": [["1", ""], ["2", "3"]], "status_column": ""})
        self.assertIsNone(summarize.normalize_table({"columns": [], "rows": [["x"]]}))

    def test_empty_sections_and_callouts_dropped(self):
        d = detailed()["discussion"][0]
        self.assertEqual([s["heading"] for s in d["sections"]], ["현황", "KT 요청과 답변", "주의"])
        self.assertIsNone(summarize.normalize_callout({"type": "warning", "text": " "}))
        self.assertEqual(summarize.normalize_callout({"type": "이상함", "text": "x"})["type"], "note")


class TestDetailedStorage(unittest.TestCase):
    def setUp(self):
        self.xml = render.Renderer([]).storage(detailed(), meeting_date=date(2026, 10, 20),
                                               duration_sec=None, transcript_lines=None)

    def test_well_formed(self):
        xml.dom.minidom.parseString('<root xmlns:ac="a" xmlns:ri="r">' + self.xml + "</root>")

    def test_subsections_numbered_under_agenda(self):
        self.assertIn("<h2>2. 안건 1 — KT 자산 스캔 임계치</h2>", self.xml)
        self.assertIn("<h3>2.1 현황</h3>", self.xml)
        self.assertIn("<h3>2.2 KT 요청과 답변</h3>", self.xml)
        self.assertNotIn("빈 소절", self.xml)

    def test_table_with_status_lozenges(self):
        self.assertIn("<th>KT 요청</th><th>우리 답변</th><th>상태</th>", self.xml)
        for title, colour in (("확인 필요", "Yellow"), ("진행 중", "Blue"), ("합의", "Green")):
            self.assertIn(f'<ac:parameter ac:name="colour">{colour}</ac:parameter>'
                          f'<ac:parameter ac:name="title">{title}</ac:parameter>', self.xml)

    def test_callout_and_conclusion(self):
        self.assertIn('<ac:structured-macro ac:name="warning" ac:schema-version="1">'
                      '<ac:parameter ac:name="title">전제</ac:parameter>', self.xml)
        self.assertRegex(self.xml, r"<p><strong>결론</strong> <ac:structured-macro ac:name=\"status\".*?"
                                   r"기술 이슈 없음, 비용 이슈로 전달</p>")

    def test_agenda_conclusion_table_in_overview(self):
        self.assertIn("<h3>1.1 안건별 결론</h3>", self.xml)
        self.assertIn("<th>#</th><th>안건</th><th>결론</th><th>상태</th>", self.xml)

    def test_action_items_table_keeps_task_checkbox_and_due_inside(self):
        """상위 페이지 미완료 리포트가 Task 와 Task 본문 안 날짜를 읽는다."""
        row = re.search(r"<tr><td>1</td><td>(<ac:task-list>.*?</ac:task-list>)</td><td>(.*?)</td>", self.xml)
        self.assertIsNotNone(row)
        self.assertIn('<time datetime="2026-10-31" />', row.group(1))
        self.assertEqual(row.group(2), "<em>담당 미정</em>")

    def test_review_note_sections_use_h4(self):
        self.assertIn("<h4>판단</h4>", self.xml)


class TestStatusColour(unittest.TestCase):
    def test_priority(self):
        cases = {"합의": "Green", "확정 대기": "Yellow", "결정 보류": "Grey", "불가능": "Red",
                 "회신 예정": "Blue", "기준 협의 필요": "Yellow", "아무거나": "Grey"}
        for text, colour in cases.items():
            self.assertEqual(render.status_colour(text), colour, text)


class TestDetailedMarkdown(unittest.TestCase):
    def test_markdown_tables_and_callouts(self):
        md = render.markdown(detailed(), meeting_date=date(2026, 10, 20), duration_sec=None)
        self.assertIn("### 2.2 KT 요청과 답변", md)
        self.assertIn("| IP 자산 200만 개 등록 | 비용 산정 후 임계치 전달 | [확인 필요] |", md)
        self.assertIn("> **[주의] 전제** 임대 기반 스캔 환경이라 IDC 협력이 관건", md)
        self.assertIn("**결론** [합의] 기술 이슈 없음, 비용 이슈로 전달", md)
        self.assertIn("### 1.1 안건별 결론", md)


if __name__ == "__main__":
    unittest.main()
