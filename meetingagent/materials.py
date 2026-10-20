"""회의 자료(회의 중 화면에 띄운 문서)에서 텍스트를 뽑는다. 맥 기본 도구만 쓴다.

작업 폴더 구조:
  materials/<원본 파일>            그대로 복사
  materials/_text/<원본 파일>.txt  뽑은 텍스트 (회의록 프롬프트에 들어가는 것)

자료는 수기 메모와 성격이 다르다. 메모는 '회의에서 실제로 정한 것'이라 전사본보다 우선하지만,
자료는 '논의할 거리'라서 표기·수치 확인용 참고로만 쓴다 (SKILL.md "회의 자료가 함께 오면").
"""
from __future__ import annotations

import html
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

TEXTUTIL_EXTS = {".docx", ".doc", ".rtf", ".rtfd", ".odt", ".html", ".htm", ".webarchive", ".wordml"}
PLAIN_EXTS = {".txt", ".md", ".csv", ".tsv"}
SUPPORTED = TEXTUTIL_EXTS | PLAIN_EXTS | {".pdf", ".pptx", ".hwpx"}

# PDFKit 으로 PDF 글자를 뽑는다 (osascript JXA). 스캔 이미지 PDF 는 빈 문자열.
_PDF_JXA = """
ObjC.import('PDFKit');
function run(argv) {
  const doc = $.PDFDocument.alloc.initWithURL($.NSURL.fileURLWithPath(argv[0]));
  if (!doc || doc.isNil()) return '';
  const s = doc.string;
  return (s && !s.isNil()) ? s.js : '';
}
"""


class MaterialError(ValueError):
    pass


def extract_text(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in PLAIN_EXTS:
        return path.read_text(encoding="utf-8", errors="replace")
    if ext in TEXTUTIL_EXTS:
        r = subprocess.run(["textutil", "-convert", "txt", "-encoding", "UTF-8", "-stdout", str(path)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise MaterialError(f"{path.name}: 변환 실패 ({r.stderr.strip()[:200]})")
        return r.stdout
    if ext == ".pdf":
        r = subprocess.run(["osascript", "-l", "JavaScript", "-e", _PDF_JXA, str(path)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise MaterialError(f"{path.name}: PDF를 열 수 없습니다 ({r.stderr.strip()[:200]})")
        if not r.stdout.strip():
            raise MaterialError(f"{path.name}: 글자를 뽑을 수 없습니다 (스캔한 이미지 PDF일 수 있음)")
        return r.stdout
    if ext == ".pptx":
        return _zip_xml_text(path, r"ppt/slides/slide(\d+)\.xml", "a:p", "a:t", "슬라이드")
    if ext == ".hwpx":
        return _zip_xml_text(path, r"Contents/section(\d+)\.xml", "hp:p", "hp:t", None)
    if ext == ".hwp":
        raise MaterialError(f"{path.name}: 한글(.hwp)은 읽을 수 없습니다. 한글에서 PDF나 .hwpx로 저장해서 넣어주세요.")
    raise MaterialError(f"{path.name}: 지원하지 않는 형식입니다 (지원: {' '.join(sorted(SUPPORTED))})")


def _zip_xml_text(path: Path, member_re: str, para_tag: str, text_tag: str, unit: str | None) -> str:
    """pptx·hwpx 처럼 zip 안 XML 에 글자가 든 형식. 문단 단위로 한 줄씩."""
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise MaterialError(f"{path.name}: 파일이 손상됐거나 형식이 다릅니다")
    parts = sorted((int(m.group(1)), n) for n in z.namelist() if (m := re.fullmatch(member_re, n)))
    text_re = re.compile(rf"<{text_tag}(?:\s[^>]*)?>([^<]*)</{text_tag}>")
    out: list[str] = []
    for num, name in parts:
        xml = z.read(name).decode("utf-8", errors="replace")
        lines = [html.unescape("".join(text_re.findall(p))) for p in xml.split(f"</{para_tag}>")]
        lines = [l for l in lines if l.strip()]
        if lines and unit:
            out.append(f"[{unit} {num}]")
        out.extend(lines)
    return "\n".join(out)


def clean(text: str) -> str:
    text = re.sub(r"\r\n?| | |\x0c", "\n", text)
    text = re.sub(r"[ \t ]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def attach(job_dir: Path, src: Path) -> tuple[str, int]:
    """원본을 materials/ 에 복사하고 텍스트를 뽑아 둔다. (파일 이름, 글자 수)"""
    if not src.is_file():
        raise MaterialError(f"파일이 없습니다: {src}")
    text = clean(extract_text(src))
    if not text:
        raise MaterialError(f"{src.name}: 내용이 비어 있습니다")
    d = job_dir / "materials"
    (d / "_text").mkdir(parents=True, exist_ok=True)
    if src.resolve() != (d / src.name).resolve():
        shutil.copy2(src, d / src.name)
    (d / "_text" / f"{src.name}.txt").write_text(text, encoding="utf-8")
    return src.name, len(text)


def load(job_dir: Path) -> list[tuple[str, str]]:
    """[(원본 파일 이름, 텍스트)] — 이름 순."""
    d = job_dir / "materials" / "_text"
    if not d.is_dir():
        return []
    return [(f.name[: -len(".txt")], f.read_text(encoding="utf-8"))
            for f in sorted(d.glob("*.txt")) if f.read_text(encoding="utf-8").strip()]
