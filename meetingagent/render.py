"""회의록 JSON → Confluence storage format(XHTML) / Markdown."""
from __future__ import annotations

import re
from datetime import date
from typing import Callable
from xml.sax.saxutils import escape

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

UserKeyResolver = Callable[[str], "str | None"]  # 정식 이름 → Confluence userkey


def _e(text: str) -> str:
    return escape(str(text))


def normalize_name(name: str) -> str:
    return re.sub(r"\s*(\(추정\)|님)$", "", name.strip()).strip()


def find_person(name: str, people: list[dict]) -> dict | None:
    key = normalize_name(name)
    if not key:
        return None
    for p in people:
        candidates = [p.get("name", "")] + list(p.get("aliases") or [])
        if any(normalize_name(c) == key for c in candidates if c):
            return p
    return None


class Renderer:
    def __init__(self, people: list[dict], resolve_userkey: UserKeyResolver | None = None):
        self.people = people
        self.resolve_userkey = resolve_userkey or (lambda _u: None)

    def person(self, name: str) -> str:
        """가능하면 @멘션, 아니면 이름 텍스트."""
        p = find_person(name, self.people)
        if p and p.get("username"):
            key = self.resolve_userkey(p["username"])
            if key:
                return f'<ac:link><ri:user ri:userkey="{_e(key)}" /></ac:link>'
        return _e(p["name"] if p else name)

    def storage(self, m: dict, *, meeting_date: date, duration_sec: float | None,
                transcript_lines: list[str] | None, source_name: str = "") -> str:
        out: list[str] = []

        # Page Properties: 상위 페이지의 Page Properties Report가 이 표를 모아 DB처럼 보여준다
        attendees = ", ".join(self.person(a) for a in m["attendees"]) or "-"
        rows = [
            ("날짜", f'<time datetime="{meeting_date.isoformat()}" />'),
            ("유형", _e(m["meeting_type"])),
            ("프로젝트", _e(m["project"] or "-")),
            ("참석자", attendees),
            ("Action Items", str(len(m["action_items"]))),
        ]
        if duration_sec:
            rows.append(("녹음 길이", f"{max(1, round(duration_sec / 60))}분"))
        out.append('<ac:structured-macro ac:name="details" ac:schema-version="1">'
                   '<ac:parameter ac:name="id">meeting</ac:parameter><ac:rich-text-body>'
                   '<table><tbody>')
        out += [f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in rows]
        out.append("</tbody></table></ac:rich-text-body></ac:structured-macro>")

        if m["summary"]:
            out.append('<ac:structured-macro ac:name="info" ac:schema-version="1">'
                       f'<ac:rich-text-body><p>{_e(m["summary"])}</p></ac:rich-text-body>'
                       '</ac:structured-macro>')

        out.append("<h2>결정사항</h2>")
        out.append(self._ul(m["decisions"]) if m["decisions"] else "<p><em>없음</em></p>")

        out.append("<h2>Action Items</h2>")
        if m["action_items"]:
            out.append("<ac:task-list>")
            for i, a in enumerate(m["action_items"], 1):
                owner = self.person(a["owner"]) if a["owner"] else "<em>담당 미정</em>"
                due = (f'<time datetime="{a["due"]}" />' if DATE_RE.match(a["due"])
                       else "<em>기한 미정</em>")
                evidence = f' <span style="color: #6b778c;">[{_e(a["evidence"])}]</span>' \
                    if a["evidence"] else ""
                out.append(
                    f"<ac:task><ac:task-id>{i}</ac:task-id>"
                    "<ac:task-status>incomplete</ac:task-status>"
                    f"<ac:task-body>{owner} {_e(a['task'])} {due}{evidence}</ac:task-body>"
                    "</ac:task>"
                )
            out.append("</ac:task-list>")
        else:
            out.append("<p><em>없음</em></p>")

        out.append("<h2>논의 내용</h2>")
        for d in m["discussion"]:
            if d["topic"]:
                out.append(f"<h3>{_e(d['topic'])}</h3>")
            out.append(self._ul(d["points"]))

        out.append("<h2>미결 / 다음에 다룰 것</h2>")
        out.append(self._ul(m["open_questions"]) if m["open_questions"] else "<p><em>없음</em></p>")

        footer = []
        if m["speaker_map"]:
            footer.append("화자 추정: " + ", ".join(f"{k}={v}" for k, v in m["speaker_map"].items()))
        if m["corrections"]:
            footer.append("전사 보정: " + ", ".join(f"{c['from']}→{c['to']}" for c in m["corrections"]))
        footer.append("MeetingAgent 자동 생성 (로컬 Whisper 전사 + Claude 요약). 오류는 직접 수정해 주세요.")
        out.append("<hr />" + "".join(
            f'<p><span style="color: #6b778c;"><small>{_e(f)}</small></span></p>' for f in footer))

        if transcript_lines:
            title = "원문 전사본" + (f" ({_e(source_name)})" if source_name else "")
            body = "".join(f"<p>{_e(l)}</p>" for l in transcript_lines if l.strip())
            out.append('<ac:structured-macro ac:name="expand" ac:schema-version="1">'
                       f'<ac:parameter ac:name="title">{title}</ac:parameter>'
                       f"<ac:rich-text-body>{body}</ac:rich-text-body></ac:structured-macro>")
        return "\n".join(out)

    @staticmethod
    def _ul(items: list[str]) -> str:
        return "<ul>" + "".join(f"<li>{_e(i)}</li>" for i in items) + "</ul>"


def markdown(m: dict, *, meeting_date: date, duration_sec: float | None) -> str:
    lines = [f"# [{meeting_date.isoformat()}] {m['title']}", ""]
    meta = [f"유형: {m['meeting_type']}"]
    if m["project"]:
        meta.append(f"프로젝트: {m['project']}")
    if m["attendees"]:
        meta.append(f"참석: {', '.join(m['attendees'])}")
    if duration_sec:
        meta.append(f"{max(1, round(duration_sec / 60))}분")
    lines += [" · ".join(meta), ""]
    if m["summary"]:
        lines += [f"> {m['summary']}", ""]
    lines += ["## 결정사항"] + ([f"- {d}" for d in m["decisions"]] or ["- 없음"]) + [""]
    lines.append("## Action Items")
    for a in m["action_items"]:
        due = a["due"] or "기한 미정"
        owner = a["owner"] or "담당 미정"
        ev = f" `[{a['evidence']}]`" if a["evidence"] else ""
        lines.append(f"- [ ] **{owner}** {a['task']} ({due}){ev}")
    if not m["action_items"]:
        lines.append("- 없음")
    lines += ["", "## 논의 내용"]
    for d in m["discussion"]:
        if d["topic"]:
            lines.append(f"### {d['topic']}")
        lines += [f"- {p}" for p in d["points"]] + [""]
    lines.append("## 미결 / 다음에 다룰 것")
    lines += [f"- {q}" for q in m["open_questions"]] or ["- 없음"]
    if m["labels"]:
        lines += ["", "라벨: " + " ".join(f"`{l}`" for l in m["labels"])]
    return "\n".join(lines) + "\n"
