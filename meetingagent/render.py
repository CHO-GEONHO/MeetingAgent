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
        """사내 회의록 템플릿(wiki pageId=329155833) 골격으로 만든다.

        번호가 붙은 절 구성이고, 내용이 없는 절은 넣지 않는다.
          핵심 요약 → 1. 회의 개요(배경·진행 순서) → 2..N. 안건 → 결정·합의 → Action Items
          → 미결 → 담당자 검토 의견 → 꼬리말 → 원문 전사본
        """
        out: list[str] = []
        n = 0  # 절 번호

        def head(title: str) -> str:
            nonlocal n
            n += 1
            return f"<h2>{n}. {_e(title)}</h2>"

        # --- 핵심 요약 (템플릿 맨 위 패널) ---
        paras = ([m["summary"]] if m["summary"] else []) + list(m.get("summary_detail") or [])
        if paras:
            body = "".join(f"<p>{_e(p)}</p>" for p in paras)
            out.append('<ac:structured-macro ac:name="info" ac:schema-version="1">'
                       '<ac:parameter ac:name="title">핵심 요약</ac:parameter>'
                       f"<ac:rich-text-body>{body}</ac:rich-text-body></ac:structured-macro>")
        out.append("<hr />")

        # --- 1. 회의 개요 ---
        # Page Properties(details) 매크로를 그대로 쓴다. 상위 '회의록 정리' 페이지의
        # Page Properties Report 가 이 표를 읽어 목록을 만들기 때문에, 행 이름(날짜·유형·
        # 프로젝트·참석자·Action Items)은 index_page_body 의 headings 와 맞춰야 한다.
        out.append(head("회의 개요"))
        when = f'<time datetime="{meeting_date.isoformat()}" />'
        if m.get("started_at"):
            when += f" {_e(m['started_at'])}"
        rows = [("주제", _e(m.get("subject") or m["title"])),
                ("날짜", when),
                ("장소", _e(m.get("location") or "-")),
                ("유형", _e(m["meeting_type"])),
                ("프로젝트", _e(m["project"] or "-"))]
        groups = m.get("attendee_groups") or []
        if groups:
            for g in groups:
                label = f"참석 ({g['org']})" if g.get("org") else "참석"
                rows.append((_e(label), ", ".join(self.person(x) for x in g["members"])))
            rows.append(("참석자", ", ".join(
                self.person(x) for g in groups for x in g["members"]) or "-"))
        else:
            rows.append(("참석자", ", ".join(self.person(a) for a in m["attendees"]) or "-"))
        rows.append(("Action Items", str(len(m["action_items"]))))
        if duration_sec:
            rows.append(("녹음 길이", f"{max(1, round(duration_sec / 60))}분"))
        out.append('<ac:structured-macro ac:name="details" ac:schema-version="1">'
                   '<ac:parameter ac:name="id">meeting</ac:parameter><ac:rich-text-body>'
                   '<table><tbody>')
        out += [f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in rows]
        out.append("</tbody></table></ac:rich-text-body></ac:structured-macro>")

        sub = 0
        if m.get("background"):
            sub += 1
            out.append(f"<h3>{n}.{sub} 배경</h3><p>{_e(m['background'])}</p>")
        if m.get("sequence"):
            sub += 1
            out.append(f"<h3>{n}.{sub} 진행 순서</h3>" + self._ul(m["sequence"]))

        # --- 2..N. 안건 ---
        for i, d in enumerate(m["discussion"], 1):
            topic = d["topic"] or f"안건 {i}"
            out.append(head(f"안건 {i} — {topic}" if d["topic"] else topic))
            out.append(self._ul(d["points"]))

        # --- 결정·합의 사항 ---
        out.append(head("결정·합의 사항"))
        if m["decisions"]:
            out.append("<table><tbody><tr><th> </th><th>합의 내용</th><th>상태</th></tr>")
            for i, d in enumerate(m["decisions"], 1):
                status = _e(d["status"]) if d.get("status") else "합의"
                out.append(f"<tr><td>{i}</td><td>{_e(d['text'])}</td><td>[{status}]</td></tr>")
            out.append("</tbody></table>")
        else:
            out.append("<p><em>없음</em></p>")

        # --- Action Items (Task 매크로 유지: 상위 페이지의 미완료 리포트가 읽는다) ---
        out.append(head("Action Items"))
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

        if m["open_questions"]:
            out.append(head("미결 / 다음에 다룰 것"))
            out.append(self._ul(m["open_questions"]))

        if m.get("review_notes"):
            out.append(head("담당자 검토 의견 및 후속 논의 안건"))
            for i, note in enumerate(m["review_notes"], 1):
                if note.get("topic"):
                    out.append(f"<h3>{n}.{i} {_e(note['topic'])}</h3>")
                out.append(self._ul(note["points"]))

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
    """storage() 와 같은 절 구성의 로컬 사본. 번호도 같게 매긴다."""
    lines = [f"# [{meeting_date.isoformat()}] {m['title']}", ""]
    n = 0

    def head(title: str) -> str:
        nonlocal n
        n += 1
        return f"## {n}. {title}"

    for p in ([m["summary"]] if m["summary"] else []) + list(m.get("summary_detail") or []):
        lines += [f"> {p}", ">"]
    if lines[-1] == ">":
        lines[-1] = ""

    lines += [head("회의 개요"), ""]
    when = meeting_date.isoformat() + (f" {m['started_at']}" if m.get("started_at") else "")
    rows = [("주제", m.get("subject") or m["title"]), ("일시", when),
            ("장소", m.get("location") or "-"), ("유형", m["meeting_type"]),
            ("프로젝트", m["project"] or "-")]
    groups = m.get("attendee_groups") or []
    if groups:
        rows += [(f"참석 ({g['org']})" if g.get("org") else "참석", ", ".join(g["members"]))
                 for g in groups]
    elif m["attendees"]:
        rows.append(("참석", ", ".join(m["attendees"])))
    if duration_sec:
        rows.append(("녹음 길이", f"{max(1, round(duration_sec / 60))}분"))
    lines += ["| 항목 | 내용 |", "|---|---|"] + [f"| {k} | {v} |" for k, v in rows] + [""]

    sub = 0
    if m.get("background"):
        sub += 1
        lines += [f"### {n}.{sub} 배경", "", m["background"], ""]
    if m.get("sequence"):
        sub += 1
        lines += [f"### {n}.{sub} 진행 순서", ""] + [f"- {s}" for s in m["sequence"]] + [""]

    for i, d in enumerate(m["discussion"], 1):
        lines += [head(f"안건 {i} — {d['topic']}" if d["topic"] else f"안건 {i}"), ""]
        lines += [f"- {p}" for p in d["points"]] + [""]

    lines += [head("결정·합의 사항"), ""]
    if m["decisions"]:
        lines += ["|  | 합의 내용 | 상태 |", "|---|---|---|"]
        lines += [f"| {i} | {d['text']} | [{d.get('status') or '합의'}] |"
                  for i, d in enumerate(m["decisions"], 1)]
    else:
        lines.append("- 없음")
    lines.append("")

    lines += [head("Action Items"), ""]
    for a in m["action_items"]:
        ev = f" `[{a['evidence']}]`" if a["evidence"] else ""
        lines.append(f"- [ ] **{a['owner'] or '담당 미정'}** {a['task']} "
                     f"({a['due'] or '기한 미정'}){ev}")
    if not m["action_items"]:
        lines.append("- 없음")
    lines.append("")

    if m["open_questions"]:
        lines += [head("미결 / 다음에 다룰 것"), ""]
        lines += [f"- {q}" for q in m["open_questions"]] + [""]

    if m.get("review_notes"):
        lines += [head("담당자 검토 의견 및 후속 논의 안건"), ""]
        for i, note in enumerate(m["review_notes"], 1):
            if note.get("topic"):
                lines += [f"### {n}.{i} {note['topic']}", ""]
            lines += [f"- {p}" for p in note["points"]] + [""]

    if m["labels"]:
        lines += ["라벨: " + " ".join(f"`{l}`" for l in m["labels"])]
    return "\n".join(lines).rstrip() + "\n"
