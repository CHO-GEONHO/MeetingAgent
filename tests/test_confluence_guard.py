"""재업로드 덮어쓰기 가드. 회의록은 사람이 고치는 문서라 이 테스트가 깨지면 안 된다."""
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from meetingagent import config, pipeline, summarize
from meetingagent.confluence import Confluence, ConfluenceConflict, ConfluenceError


class FakeServer:
    def __init__(self):
        self.pages: dict[str, dict] = {}
        self.next_id = 100
        self.down = False

    def human_edit(self, pid: str) -> None:
        p = self.pages[pid]
        p["version"] += 1
        p["by"] = "홍길동"


class FakeConfluence(Confluence):
    """HTTP 대신 FakeServer를 쓰는 클라이언트. 버전 규칙은 실제 Confluence처럼 +1만 허용."""

    def __init__(self, server: FakeServer):
        self.base, self.space, self.cache_path, self._userkeys = "http://fake", "SRE", None, {}
        self.srv = server

    def _view(self, p):
        return {"id": p["id"], "title": p["title"],
                "version": {"number": p["version"], "by": {"displayName": p["by"]}},
                "_links": {"webui": f"/pages/viewpage.action?pageId={p['id']}"}}

    def _req(self, method, path, **kw):
        if self.srv.down:
            raise ConfluenceError("Confluence 접속 실패. VPN 연결을 확인하세요.")
        params = kw.get("params") or {}
        data = json.loads(kw["data"]) if kw.get("data") else None
        if method == "GET" and path == "/content":
            return {"results": [self._view(p) for p in self.srv.pages.values()
                                if p["title"] == params.get("title")]}
        if method == "POST" and path == "/content":
            self.srv.next_id += 1
            pid = str(self.srv.next_id)
            self.srv.pages[pid] = {"id": pid, "title": data["title"], "version": 1, "by": "bot",
                                   "labels": [], "minor": None}
            return self._view(self.srv.pages[pid])
        m = re.fullmatch(r"/content/(\d+)", path)
        if m:
            p = self.srv.pages[m.group(1)]
            if method == "GET":
                return self._view(p)
            if method == "PUT":
                if data["version"]["number"] != p["version"] + 1:
                    raise ConfluenceError("409 version conflict")
                p.update(version=data["version"]["number"], by="bot",
                         minor=data["version"].get("minorEdit"))
                return self._view(p)
        m = re.fullmatch(r"/content/(\d+)/label", path)
        if m:
            p = self.srv.pages[m.group(1)]
            if method == "GET":
                return {"results": [{"name": l} for l in p["labels"]]}
            if method == "POST":
                p["labels"] += [l["name"] for l in data if l["name"] not in p["labels"]]
                return {}
            if method == "DELETE":
                p["labels"].remove(params["name"])
                return {}
        if path == "/user":
            return {}
        raise AssertionError(f"예상하지 못한 요청: {method} {path}")


class TestUpdateGuard(unittest.TestCase):
    def setUp(self):
        self.srv = FakeServer()
        self.cf = FakeConfluence(self.srv)
        self.pid = self.cf.create_page("[2026-10-19] 회의", "<p>v1</p>", None)["id"]

    def test_update_when_unchanged(self):
        page = self.cf.update_page(self.pid, "[2026-10-19] 회의", "<p>v2</p>", known_version=1)
        self.assertEqual(page["version"]["number"], 2)
        self.assertTrue(self.srv.pages[self.pid]["minor"])  # 지켜보는 사람에게 알림 안 가게

    def test_conflict_when_human_edited(self):
        self.srv.human_edit(self.pid)
        with self.assertRaises(ConfluenceConflict) as ctx:
            self.cf.update_page(self.pid, "t", "<p>x</p>", known_version=1)
        self.assertIn("홍길동", str(ctx.exception))
        self.assertEqual(self.srv.pages[self.pid]["by"], "홍길동")  # 덮어쓰지 않음

    def test_force_overwrites(self):
        self.srv.human_edit(self.pid)
        page = self.cf.update_page(self.pid, "t", "<p>x</p>", known_version=1, force=True)
        self.assertEqual(page["version"]["number"], 3)

    def test_sync_labels_keeps_human_labels(self):
        self.srv.pages[self.pid]["labels"] = ["meeting", "proj-old", "human-tag"]
        out = self.cf.sync_labels(self.pid, ["meeting", "proj-new"], ["meeting", "proj-old"])
        self.assertEqual(out, ["meeting", "proj-new"])
        self.assertEqual(sorted(self.srv.pages[self.pid]["labels"]),
                         ["human-tag", "meeting", "proj-new"])


class TestPipelineForce(unittest.TestCase):
    """`meet redo --from upload --force`가 실패해도 나중의 자동 재시도가 확인 없이 덮으면 안 된다."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        raw = config._deep_merge(config.DEFAULTS, {
            "paths": {"workdir": str(root), "inbox": str(root / "inbox")},
            "confluence": {"enabled": True, "space_key": "SRE", "parent_id": "500"},
            "notify": False,
        })
        self.cfg = config.Settings(raw, [], [])
        jdir = root / "meetings" / "2026-10-19_1000_test"
        jdir.mkdir(parents=True)
        (jdir / "transcript.json").write_text(json.dumps([{"start": 0, "end": 1, "text": "안녕"}]))
        (jdir / "minutes.json").write_text(json.dumps(summarize.normalize(
            {"title": "테스트 회의", "labels": ["proj-test"]}), ensure_ascii=False))
        stamp = "2026-10-19T10:00:00"
        (jdir / "state.json").write_text(json.dumps({
            "meeting_date": "2026-10-19", "meeting_time": "10:00", "hints": {}, "status": "summarized",
            "stages": {"convert": stamp, "transcribe": stamp, "summarize": stamp},
        }))
        self.job = pipeline.Job(jdir)
        self.srv = FakeServer()
        patcher = mock.patch.object(pipeline, "confluence_client", lambda cfg: FakeConfluence(self.srv))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def redo(self, **kw) -> bool:
        self.job.reset_from("upload")
        return pipeline.run(self.job, self.cfg, **kw)

    def test_failed_force_does_not_leak_into_retry(self):
        self.assertTrue(pipeline.run(self.job, self.cfg))
        pid = self.job.state["page"]["id"]
        self.assertEqual(self.job.state["page"]["version"], 1)

        self.srv.human_edit(pid)                 # v2: 사람이 고침
        self.srv.down = True                     # --force 하는 순간 VPN 끊김
        self.assertFalse(self.redo(force_upload=True))
        self.assertEqual(self.job.status, "failed")
        self.assertNotIn("force_upload", json.loads(self.job.state_path.read_text()))

        self.srv.down = False
        self.srv.human_edit(pid)                 # v3: 그 뒤 다른 사람이 또 고침
        for j in pipeline.pending(self.cfg):     # = meet retry / 감시의 자동 재시도
            pipeline.run(j, self.cfg)
        self.assertEqual(pipeline.Job(self.job.dir).status, "conflict")
        self.assertEqual(self.srv.pages[pid]["version"], 3)
        self.assertEqual(self.srv.pages[pid]["by"], "홍길동")

    def test_legacy_force_flag_in_state_is_ignored(self):
        self.assertTrue(pipeline.run(self.job, self.cfg))
        pid = self.job.state["page"]["id"]
        self.srv.human_edit(pid)
        self.job.state["force_upload"] = True    # 예전 버전이 남긴 플래그
        self.assertFalse(self.redo())
        self.assertEqual(self.job.status, "conflict")
        self.assertEqual(self.srv.pages[pid]["by"], "홍길동")

    def test_force_overwrites_when_reachable(self):
        self.assertTrue(pipeline.run(self.job, self.cfg))
        pid = self.job.state["page"]["id"]
        self.srv.human_edit(pid)
        self.assertTrue(self.redo(force_upload=True))
        self.assertEqual(self.job.status, "done")
        self.assertEqual(self.job.state["page"]["version"], 3)
        self.assertEqual(self.srv.pages[pid]["by"], "bot")


if __name__ == "__main__":
    unittest.main()
