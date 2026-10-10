"""Publish lecture summaries to WPS smart docs (Kdocs MCP).

Drop-in replacement for the email step. It consumes the same ``items`` shape that
``Emailer.send`` used::

    {"sub_id", "course_title", "sub_title", "date", "summary"}

``summary`` is already Markdown with ``$LaTeX$`` math (the summarizer emits that
and the emailer converted it to images), so it is sent to Kdocs verbatim -- the
server turns ``$...$`` into native, editable formula nodes.

Documents land at ``<root>/<course>/<sub_title>.otl`` and existing documents are
adopted rather than duplicated, so re-runs are safe.

Auth: ``KDOCS_TOKEN`` env var (GitHub secret). No token is ever written to disk
or printed.
"""
from __future__ import annotations

import json
import os
import re
import ssl
import time
import urllib.error
import urllib.request

MCP_URL = os.environ.get("KDOCS_MCP_URL", "https://mcp-center.wps.cn/skill_hub/mcp")
ROOT_NAME = os.environ.get("ICOURSE_ROOT_NAME", "iCourse 课程摘要")
SKILL_VERSION = os.environ.get("KDOCS_SKILL_VERSION", "1.6.14")


class KdocsClient:
    """Minimal streamable-HTTP MCP client for the Kdocs skill hub."""

    def __init__(self, token: str, timeout: int = 120):
        self.token = token
        self.timeout = timeout
        self.sid: str | None = None
        self.seq = 1

    def _post(self, payload: dict) -> str:
        headers = {
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
            "Authorization": "Bearer " + self.token,
            "X-Request-Source": "workbuddy",
            "X-Skill-Version": SKILL_VERSION,
        }
        if self.sid:
            headers["mcp-session-id"] = self.sid
        req = urllib.request.Request(
            MCP_URL, data=json.dumps(payload).encode("utf-8"),
            headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout,
                                    context=ssl.create_default_context()) as r:
            sid = r.headers.get("mcp-session-id")
            if sid:
                self.sid = sid
            return r.read().decode("utf-8", "replace")

    @staticmethod
    def _parse(text: str):
        """Unwrap a JSON-RPC reply that may be SSE-framed."""
        chunks = []
        for line in text.splitlines():
            m = re.match(r"^data:\s*(.*)$", line)
            if m:
                chunks.append(m.group(1))
        for c in (chunks or [text]):
            if not c or not c.strip():
                continue
            try:
                return json.loads(c)
            except Exception:
                continue
        return None

    def rpc(self, method: str, params=None):
        payload = {"jsonrpc": "2.0", "method": method, "id": self.seq}
        self.seq += 1
        if params is not None:
            payload["params"] = params
        return self._parse(self._post(payload))

    def call(self, name: str, args: dict):
        """Return (parsed_payload_or_None, raw_text)."""
        j = self.rpc("tools/call", {"name": name, "arguments": args})
        try:
            text = j["result"]["content"][0]["text"]
        except Exception:
            return None, json.dumps(j, ensure_ascii=False)
        try:
            return json.loads(text), text
        except Exception:
            return None, text

    def initialize(self):
        self.rpc("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "icourse-subscriber", "version": "1.0.0"},
        })


def _deep(o):
    """Responses nest as data.data for some tools and data for others."""
    if not isinstance(o, dict):
        return {}
    d = o.get("data")
    if isinstance(d, dict) and isinstance(d.get("data"), dict):
        return d["data"]
    return d if isinstance(d, dict) else {}


def _items(o) -> list[dict]:
    """Flatten a listing response; search_files nests hits as {file:{...}}."""
    d = _deep(o)
    arr = d.get("items") if isinstance(d.get("items"), list) else d.get("files")
    if not isinstance(arr, list):
        dd = (o or {}).get("data")
        arr = dd.get("files") if isinstance(dd, dict) and isinstance(dd.get("files"), list) else []
    out = []
    for it in (arr or []):
        if isinstance(it, dict) and isinstance(it.get("file"), dict):
            out.append(dict(it["file"]))
        elif isinstance(it, dict):
            out.append(it)
    return out


def _base_name(name: str) -> str:
    """Strip the office extension: the service stores 'X.otl' but we name 'X'."""
    n = (name or "").strip()
    for ext in (".otl", ".docx", ".md", ".pdf"):
        if n.lower().endswith(ext):
            return n[: -len(ext)]
    return n


def _same_doc(stored: str, wanted: str) -> bool:
    """True when a stored filename denotes the wanted document.

    Tolerates the stored extension and any '(n)' conflict suffix.
    """
    got = _base_name(stored)
    if got == wanted:
        return True
    return re.sub(r"\(\d+\)$", "", got) == wanted


def _safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", (name or "").strip()).rstrip(".")


class WpsPublisher:
    """Publish lecture summaries as WPS smart documents."""

    def __init__(self, token: str | None = None, root_name: str | None = None):
        self.token = (token or os.environ.get("KDOCS_TOKEN", "")).strip()
        self.root_name = root_name or ROOT_NAME
        self.client: KdocsClient | None = None
        self._root_id: str | None = None
        self._course_ids: dict[str, str] = {}
        self._children: dict[str, list[dict]] = {}

    # ------------------------------------------------------------- internals
    def _c(self) -> KdocsClient:
        if self.client is None:
            if not self.token:
                raise RuntimeError("KDOCS_TOKEN is not set")
            c = KdocsClient(self.token)
            c.initialize()
            self.client = c
        return self.client

    def _list_children(self, parent_id: str) -> list[dict]:
        if parent_id in self._children:
            return self._children[parent_id]
        out: list[dict] = []
        page_token = None
        for _ in range(10):
            args: dict = {"parent_id": parent_id, "page_size": 100}
            if page_token:
                args["page_token"] = page_token
            res, _t = self._c().call("list_files", args)
            items = _items(res)
            out.extend(items)
            page_token = _deep(res).get("next_page_token") or None
            if not page_token or not items:
                break
        self._children[parent_id] = out
        return out

    def _find_root(self) -> str:
        if self._root_id:
            return self._root_id
        res, _t = self._c().call("list_my_files", {"page_size": 200})
        hit = next((f for f in _items(res)
                    if f.get("type") == "folder" and (f.get("name") or "") == self.root_name), None)
        if hit and hit.get("id"):
            self._root_id = hit["id"]
            return self._root_id
        res, txt = self._c().call("create_folder",
                                  {"name": self.root_name, "on_name_conflict": "rename"})
        rid = _deep(res).get("id")
        if not rid:
            raise RuntimeError("cannot create WPS root folder: %s" % txt[:180])
        self._root_id = rid
        return rid

    def _course_folder(self, course_title: str) -> str:
        if course_title in self._course_ids:
            return self._course_ids[course_title]
        root = self._find_root()
        hit = next((f for f in self._list_children(root)
                    if f.get("type") == "folder" and (f.get("name") or "") == course_title), None)
        cid = hit.get("id") if hit else None
        if not cid:
            # 'fail' surfaces an existing-but-hidden name instead of silently
            # minting a "(1)" duplicate; fall back to rename only then.
            res, _t = self._c().call("create_folder",
                                     {"name": course_title, "parent_id": root,
                                      "on_name_conflict": "fail"})
            cid = _deep(res).get("id")
            if not cid:
                res, _t = self._c().call("create_folder",
                                         {"name": course_title, "parent_id": root,
                                          "on_name_conflict": "rename"})
                cid = _deep(res).get("id")
        if not cid:
            raise RuntimeError("cannot resolve WPS folder for course %r" % course_title)
        self._course_ids[course_title] = cid
        return cid

    def _existing_doc(self, parent_id: str, title: str) -> str | None:
        for it in self._list_children(parent_id):
            if it.get("type") == "folder":
                continue
            if _same_doc(it.get("name") or "", title):
                return it.get("id")
        return None

    # ---------------------------------------------------------------- public
    def publish(self, items: list[dict]) -> tuple[int, int, list[str]]:
        """Publish every item.

        Returns ``(created, adopted, errors)``. ``adopted`` counts lectures that
        already had a document (never re-created).
        """
        if not items:
            return 0, 0, []

        created = adopted = 0
        errors: list[str] = []

        for it in items:
            course = it["course_title"]
            sub = it["sub_title"]
            title = _safe("%s %s" % (course, sub))
            try:
                parent = self._course_folder(course)

                if self._existing_doc(parent, title):
                    adopted += 1
                    print("[WPS] already exists, skipping: %s" % title, flush=True)
                    continue

                date = (it.get("date") or "").strip()
                heading = "%s (%s)" % (sub, date) if date else sub
                body = "# %s\n\n## %s\n\n%s\n" % (course, heading, it.get("summary") or "")

                res, txt = self._c().call("create_file_with_content", {
                    "name": title, "file_extension": "otl",
                    "content": body, "parent_id": parent,
                })
                data = (res or {}).get("data") or {}
                if (res or {}).get("code") == 0 and data.get("file_id"):
                    created += 1
                    # Refresh the cache so a later item in the same batch sees it.
                    self._children.pop(parent, None)
                    print("[WPS] created: %s -> %s" % (title, data.get("link_url")), flush=True)
                else:
                    errors.append("%s: %s" % (title, str(txt)[:160]))
                    print("[WPS] FAILED: %s -> %s" % (title, str(txt)[:160]), flush=True)
                time.sleep(0.4)
            except urllib.error.HTTPError as e:
                errors.append("%s: HTTP %s" % (title, e.code))
                print("[WPS] HTTP %s for %s" % (e.code, title), flush=True)
            except Exception as e:
                errors.append("%s: %s: %s" % (title, type(e).__name__, e))
                print("[WPS] ERROR %s: %s: %s" % (title, type(e).__name__, e), flush=True)

        return created, adopted, errors
