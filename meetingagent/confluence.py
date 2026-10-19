"""Confluence Data Center REST 클라이언트 (PAT Bearer 인증)."""
from __future__ import annotations

import json
from pathlib import Path

import requests


class ConfluenceError(RuntimeError):
    pass


class Confluence:
    def __init__(self, base_url: str, token: str, space_key: str, verify_ssl: bool | str = True,
                 userkey_cache: Path | None = None):
        self.base = base_url.rstrip("/")
        self.space = space_key
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token}",
                               "Accept": "application/json",
                               "Content-Type": "application/json"})
        self.s.verify = verify_ssl
        self.cache_path = userkey_cache
        self._userkeys: dict[str, str | None] = {}
        if userkey_cache and userkey_cache.exists():
            self._userkeys = json.loads(userkey_cache.read_text())

    def _req(self, method: str, path: str, **kw) -> dict:
        try:
            r = self.s.request(method, f"{self.base}/rest/api{path}", timeout=30, **kw)
        except requests.exceptions.ConnectionError as e:
            raise ConfluenceError(f"Confluence 접속 실패. VPN 연결을 확인하세요. ({e.__class__.__name__})")
        except requests.exceptions.Timeout:
            raise ConfluenceError("Confluence 응답 시간 초과. VPN 연결을 확인하세요.")
        if r.status_code == 401:
            raise ConfluenceError("인증 실패(401). PAT를 다시 등록하세요: meet setup --token")
        if r.status_code >= 400:
            raise ConfluenceError(f"{method} {path} → {r.status_code}: {r.text[:400]}")
        return r.json() if r.content else {}

    # --- 조회 ---
    def ping(self) -> dict:
        return self._req("GET", f"/space/{self.space}")

    def find_page(self, title: str) -> dict | None:
        res = self._req("GET", "/content", params={
            "spaceKey": self.space, "title": title, "type": "page", "expand": "version"})
        results = res.get("results") or []
        return results[0] if results else None

    def userkey(self, username: str) -> str | None:
        if username in self._userkeys:
            return self._userkeys[username]
        try:
            key = self._req("GET", "/user", params={"username": username}).get("userKey")
        except ConfluenceError:
            key = None
        self._userkeys[username] = key
        if self.cache_path:
            self.cache_path.write_text(json.dumps(self._userkeys, indent=2))
        return key

    def url(self, page: dict) -> str:
        links = page.get("_links") or {}
        webui = links.get("webui") or f"/pages/viewpage.action?pageId={page['id']}"
        return f"{links.get('base') or self.base}{webui}"

    # --- 쓰기 ---
    def create_page(self, title: str, body: str, parent_id: str | None) -> dict:
        payload = {"type": "page", "title": title, "space": {"key": self.space},
                   "body": {"storage": {"value": body, "representation": "storage"}}}
        if parent_id:
            payload["ancestors"] = [{"id": parent_id}]
        return self._req("POST", "/content", data=json.dumps(payload))

    def update_page(self, page_id: str, title: str, body: str) -> dict:
        current = self._req("GET", f"/content/{page_id}", params={"expand": "version"})
        payload = {"id": page_id, "type": "page", "title": title,
                   "version": {"number": current["version"]["number"] + 1},
                   "body": {"storage": {"value": body, "representation": "storage"}}}
        return self._req("PUT", f"/content/{page_id}", data=json.dumps(payload))

    def add_labels(self, page_id: str, labels: list[str]) -> None:
        if labels:
            self._req("POST", f"/content/{page_id}/label",
                      data=json.dumps([{"prefix": "global", "name": l} for l in labels]))

    def ensure_page(self, title: str, body: str, parent_id: str | None) -> dict:
        return self.find_page(title) or self.create_page(title, body, parent_id)


# --- 상위 페이지 템플릿 ---

def index_page_body(space_key: str, label: str = "meeting") -> str:
    """회의록 '홈': 모든 회의록을 표로(Page Properties Report) + 미완료 Action Item 모아보기."""
    cql = f'label = "{label}" and space = "{space_key}"'
    return (
        "<h2>회의록 목록</h2>"
        '<ac:structured-macro ac:name="detailssummary" ac:schema-version="2">'
        '<ac:parameter ac:name="id">meeting</ac:parameter>'
        '<ac:parameter ac:name="headings">날짜,유형,프로젝트,참석자,Action Items</ac:parameter>'
        '<ac:parameter ac:name="sortBy">날짜</ac:parameter>'
        '<ac:parameter ac:name="reverseSort">true</ac:parameter>'
        '<ac:parameter ac:name="pageSize">50</ac:parameter>'
        f'<ac:parameter ac:name="cql">{cql.replace(chr(34), "&quot;")}</ac:parameter>'
        "</ac:structured-macro>"
        "<h2>미완료 Action Items</h2>"
        '<ac:structured-macro ac:name="tasks-report-macro" ac:schema-version="1">'
        f'<ac:parameter ac:name="spaces">{space_key}</ac:parameter>'
        f'<ac:parameter ac:name="labels">{label}</ac:parameter>'
        '<ac:parameter ac:name="status">incomplete</ac:parameter>'
        '<ac:parameter ac:name="columns">description,duedate,assignee,location</ac:parameter>'
        '<ac:parameter ac:name="pageSize">30</ac:parameter>'
        "</ac:structured-macro>"
        '<p><small>MeetingAgent가 생성한 페이지입니다. 프로젝트별로 보려면 라벨(proj-*)로 필터하세요.</small></p>'
    )


CHILDREN_BODY = '<ac:structured-macro ac:name="children" ac:schema-version="2">' \
                '<ac:parameter ac:name="sort">title</ac:parameter>' \
                '<ac:parameter ac:name="reverse">true</ac:parameter></ac:structured-macro>'
