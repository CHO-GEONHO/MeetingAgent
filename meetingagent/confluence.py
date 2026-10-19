"""Confluence Data Center REST 클라이언트 (PAT Bearer 인증)."""
from __future__ import annotations

import json
from pathlib import Path

import requests


class ConfluenceError(RuntimeError):
    pass


class ConfluenceConflict(ConfluenceError):
    """업로드 이후 누가 페이지를 고쳤다. 덮어쓰지 않고 멈춘다."""


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
        if not r.content:
            return {}
        try:
            return r.json()
        except ValueError:
            # 사내망 밖에서 캡티브 포털/프록시가 200 에 HTML 을 끼워 넣는 경우가 있다.
            raise ConfluenceError(f"{method} {path} → JSON 이 아닌 응답: {r.text[:200]}")

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

    def update_page(self, page_id: str, title: str, body: str, *,
                    known_version: int | None = None, message: str = "",
                    minor: bool = True, force: bool = False) -> dict:
        """페이지 본문을 교체한다.

        `known_version` 을 주면 **우리가 마지막으로 올린 버전과 같을 때만** 덮어쓴다.
        회의록은 사람이 직접 고치는 것을 전제로 하므로(README "오류는 직접 수정해 주세요"),
        그 사이 수정이 있었으면 ConfluenceConflict 로 멈춘다. 말없이 덮으면 수정이 사라진다.

        `minor=True` 가 기본이다 — 자동 재업로드가 지켜보는 사람에게 알림을 보내지 않게 한다.
        """
        current = self._req("GET", f"/content/{page_id}", params={"expand": "version"})
        cur_ver = current["version"]["number"]
        if known_version is not None and not force and cur_ver != known_version:
            who = (current["version"].get("by") or {}).get("displayName") or "누군가"
            raise ConfluenceConflict(
                f"업로드 이후 페이지가 수정되었습니다 (v{known_version} → v{cur_ver}, 수정: {who}).\n"
                f"  덮어쓰지 않았습니다. 먼저 내용을 확인하세요:\n"
                f"    {self.url(current)}\n"
                f"  확인 후 그래도 이쪽으로 덮어쓰려면: meet redo <작업> --from upload --force"
            )
        version: dict = {"number": cur_ver + 1, "minorEdit": minor}
        if message:
            version["message"] = message
        payload = {"id": page_id, "type": "page", "title": title, "version": version,
                   "body": {"storage": {"value": body, "representation": "storage"}}}
        return self._req("PUT", f"/content/{page_id}", data=json.dumps(payload))

    def page_version(self, page_id: str) -> int:
        return self._req("GET", f"/content/{page_id}", params={"expand": "version"})["version"]["number"]

    def labels(self, page_id: str) -> list[str]:
        res = self._req("GET", f"/content/{page_id}/label")
        return [l["name"] for l in res.get("results") or []]

    def sync_labels(self, page_id: str, want: list[str], previously_set: list[str] | None = None) -> list[str]:
        """라벨을 맞춘다. 돌려주는 값은 '이번에 우리가 설정한 라벨' 목록이다.

        `previously_set` 은 지난 업로드에서 우리가 붙인 라벨이다. 거기서 빠진 것만 지운다 —
        사람이 손으로 붙인 라벨은 건드리지 않는다. (프로젝트가 바뀌면 옛 proj-* 가 남아
        상위 페이지의 목록 표에서 엉뚱하게 묶이는 것을 막는다.)
        """
        want = list(dict.fromkeys(l for l in want if l))
        have = self.labels(page_id)
        for name in [l for l in (previously_set or []) if l not in want and l in have]:
            self._req("DELETE", f"/content/{page_id}/label", params={"name": name})
        add = [l for l in want if l not in have]
        if add:
            self._req("POST", f"/content/{page_id}/label",
                      data=json.dumps([{"prefix": "global", "name": l} for l in add]))
        return want

    def get_or_create_page(self, title: str, body: str, parent_id: str | None) -> dict:
        """제목이 같은 페이지가 있으면 **본문은 그대로 두고** 그것을 쓴다.

        연/월 묶음 페이지처럼 한 번 만들고 마는 용도다. 본문을 갱신하지 않는다는 뜻으로
        이름을 ensure_page 에서 바꿨다 — 예전 이름은 템플릿이 반영되는 것처럼 읽혔다.
        """
        return self.find_page(title) or self.create_page(title, body, parent_id)


# --- 상위 페이지 템플릿 ---

def index_page_body(space_key: str, label: str = "meeting") -> str:
    """회의록 '홈': 모든 회의록을 표로(Page Properties Report) + 미완료 Action Item 모아보기."""
    cql = f'label = "{label}" and space = "{space_key}"'
    return (
        "<h2>회의록 목록</h2>"
        '<ac:structured-macro ac:name="detailssummary" ac:schema-version="2">'
        '<ac:parameter ac:name="id">meeting</ac:parameter>'
        '<ac:parameter ac:name="headings">주제,날짜,장소,유형,프로젝트,참석자,Action Items</ac:parameter>'
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
