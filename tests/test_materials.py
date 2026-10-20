"""회의 자료(화면에 띄운 문서) 붙이기와, 터미널 입력을 안전하게 받는 부분."""
import io
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path

from meetingagent import cli, materials, render, summarize


class TestExtract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_plain_text(self):
        f = self.d / "메모.md"
        f.write_text("ASM 스캔 비용\r\n\r\n\r\n\r\n월 8천만원  \n")
        self.assertEqual(materials.clean(materials.extract_text(f)), "ASM 스캔 비용\n\n월 8천만원")

    @unittest.skipUnless(shutil.which("textutil"), "macOS textutil 필요")
    def test_docx_via_textutil(self):
        src = self.d / "a.txt"
        src.write_text("KT 아젠다\n전체 자산 200만개\n", encoding="utf-8")
        subprocess.run(["textutil", "-convert", "docx", "-output", str(self.d / "KT 아젠다.docx"), str(src)],
                       check=True, capture_output=True)
        text = materials.extract_text(self.d / "KT 아젠다.docx")
        self.assertIn("전체 자산 200만개", text)

    def _zip(self, name, members: dict[str, str]) -> Path:
        f = self.d / name
        with zipfile.ZipFile(f, "w") as z:
            for k, v in members.items():
                z.writestr(k, v)
        return f

    def test_pptx_slides_in_order(self):
        slide = '<p:sld><a:p><a:r><a:t>{}</a:t></a:r><a:r><a:t xml:space="preserve"> &amp; 비용</a:t></a:r></a:p></p:sld>'
        f = self._zip("발표.pptx", {"ppt/slides/slide10.xml": slide.format("열번째"),
                                    "ppt/slides/slide2.xml": slide.format("두번째"),
                                    "ppt/slides/_rels/slide2.xml.rels": "<x/>"})
        self.assertEqual(materials.extract_text(f),
                         "[슬라이드 2]\n두번째 & 비용\n[슬라이드 10]\n열번째 & 비용")

    def test_hwpx(self):
        f = self._zip("보고서.hwpx", {"Contents/section0.xml":
                                      "<hs:sec><hp:p><hp:run><hp:t>IDC 협력 필요</hp:t></hp:run></hp:p></hs:sec>"})
        self.assertEqual(materials.extract_text(f), "IDC 협력 필요")

    def test_hwp_and_unknown_are_rejected_with_hint(self):
        (self.d / "a.hwp").write_bytes(b"\xd0\xcf")
        with self.assertRaisesRegex(materials.MaterialError, "PDF나 .hwpx"):
            materials.extract_text(self.d / "a.hwp")
        (self.d / "a.xyz").write_text("x")
        with self.assertRaises(materials.MaterialError):
            materials.extract_text(self.d / "a.xyz")

    @unittest.skipUnless(shutil.which("cupsfilter") and shutil.which("osascript"), "macOS 필요")
    def test_pdf_via_pdfkit(self):
        src = self.d / "p.txt"
        src.write_text("ASM schedule scan 12 hours\n")
        r = subprocess.run(["cupsfilter", "-m", "application/pdf", str(src)], capture_output=True)
        if r.returncode != 0 or not r.stdout.startswith(b"%PDF"):
            self.skipTest("cupsfilter 로 PDF 를 만들 수 없음")
        (self.d / "p.pdf").write_bytes(r.stdout)
        self.assertIn("schedule scan", materials.extract_text(self.d / "p.pdf"))

    def test_attach_and_load_roundtrip(self):
        job = self.d / "job"
        job.mkdir()
        (self.d / "안건.txt").write_text("1. 비용 산정\n2. IDC 협력\n")
        self.assertEqual(materials.attach(job, self.d / "안건.txt"),
                         ("안건.txt", len("1. 비용 산정\n2. IDC 협력")))
        self.assertTrue((job / "materials" / "안건.txt").exists())   # 원본 보관
        self.assertEqual(materials.load(job), [("안건.txt", "1. 비용 산정\n2. IDC 협력")])
        with self.assertRaises(materials.MaterialError):
            materials.attach(job, self.d / "없는파일.docx")


class TestPrompt(unittest.TestCase):
    def build(self, **kw):
        return summarize.build_input(meeting_date=date(2026, 10, 20), duration_sec=None, hints={},
                                     glossary=[], people=[], transcript="[00:00:00] 안녕", **kw)

    def test_materials_between_notes_and_transcript_as_reference(self):
        text = self.build(notes="임계치 정해서 전달", materials=[("KT 아젠다.docx", "자산 200만개")])
        self.assertLess(text.index("# 수기 회의록"), text.index("# 회의 자료"))
        self.assertLess(text.index("# 회의 자료"), text.index("# 전사본"))
        self.assertIn("참고용", text)
        self.assertIn("## 자료 1: KT 아젠다.docx", text)

    def test_long_material_is_truncated(self):
        text = self.build(materials=[("긴 보고서.pdf", "가" * (summarize.MATERIAL_MAX_CHARS + 500))])
        self.assertIn("이하 500자 생략", text)

    def test_no_materials_no_section(self):
        self.assertNotIn("회의 자료", self.build(materials=[("빈.txt", "  \n")]))

    def test_skill_has_materials_rules(self):
        prompt = summarize.load_skill_prompt()
        self.assertIn("회의 자료가 함께 오면", prompt)
        self.assertIn("'논의할 거리'이지 '논의된 것'이 아니다", prompt)
        self.assertIn("IP 주소·API 키·토큰·계정 같은 개별 값은 회의록에 옮기지 않는다", prompt)


class TestRenderMaterials(unittest.TestCase):
    def test_footer_lists_materials(self):
        m = summarize.normalize({"title": "KT 아젠다"})
        xml = render.Renderer([]).storage(m, meeting_date=date(2026, 10, 20), duration_sec=None,
                                          transcript_lines=None, materials=["KT 아젠다.docx"])
        self.assertIn("참고 자료: KT 아젠다.docx", xml)
        md = render.markdown(m, meeting_date=date(2026, 10, 20), duration_sec=None,
                             materials=["KT 아젠다.docx"])
        self.assertIn("참고 자료: KT 아젠다.docx", md)


class TestTerminalInput(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_parse_dragged_paths_with_spaces(self):
        a, b = self.d / "KT 아젠다.docx", self.d / "b.pdf"
        line = str(a).replace(" ", "\\ ") + " '" + str(b) + "'\n"
        self.assertEqual(cli._parse_paths(line), [a, b])
        self.assertEqual(cli._parse_paths("it's broken quote"), [])

    def test_notes_input_kinds(self):
        f = self.d / "메모 파일.md"
        f.write_text("x")
        self.assertEqual(cli._classify_notes_input("\n", io.StringIO()), ("editor", ""))
        self.assertEqual(cli._classify_notes_input(str(f).replace(" ", "\\ ") + "\n", io.StringIO()),
                         ("file", str(f)))
        self.assertEqual(cli._classify_notes_input("/없는/경로.md\n", io.StringIO())[0], "missing")

    def test_pasted_notes_consume_all_lines(self):
        """경로 칸에 메모를 붙여넣으면, 나머지 줄까지 읽어야 셸로 새어 나가지 않는다."""
        rest = io.StringIO("KT 전체 자산은 200만개 수준\n도메인 자산 -> kt만이라도 빠르게\n")
        kind, text = cli._classify_notes_input("KT 아젠다 회의\n", rest)
        self.assertEqual(kind, "text")
        self.assertEqual(text.splitlines(), ["KT 아젠다 회의", "KT 전체 자산은 200만개 수준",
                                             "도메인 자산 -> kt만이라도 빠르게"])
        self.assertEqual(rest.read(), "")


if __name__ == "__main__":
    unittest.main()
