#!/usr/bin/env python3
"""WorkBuddy relay — turn the WorkBuddy/CodeBuddy chat gateway into an
OpenAI-compatible endpoint.

The upstream gateway (``POST https://copilot.tencent.com/v2/chat/completions``)
is *almost* OpenAI-compatible but has four hard requirements that the OpenAI
Python SDK (``src/ai/summarizer.py``) cannot satisfy on its own:

  1. ``stream:true`` is mandatory.
     ``stream:false`` / omitted → HTTP 400 ``code:11101``
     "Non-stream chat request is currently not supported".
  2. ``role:"developer"`` is rejected.
     → HTTP 400 ``code:11128``
     "Illegal API invocation from an unapproved channel".
     Must be rewritten to ``role:"system"``.
  3. ``tool_choice`` must be a *string*.
     An object → HTTP 400 ``code:11101``
     "cannot unmarshal object into Go struct field Request.tool_choice
      of type string".
  4. Responses are always SSE frames, never a JSON ``ChatCompletion`` body.

So this script is a thin shim: accept an ordinary OpenAI request, apply the
four rewrites, stream upstream, aggregate the SSE frames, and hand back a
standard non-streaming ``ChatCompletion`` JSON object.

Design goals
------------
* **Zero third-party dependencies** — stdlib ``http.server`` + ``urllib`` only.
  GitHub Actions runners and this machine both already have Python 3.12.
* **No credential on disk.** The access token is read from the
  ``WORKBUDDY_ACCESS_TOKEN`` env var (a GitHub secret in CI) or, when absent,
  decrypted on the fly from the local WorkBuddy login file. It is never
  printed, logged, or written anywhere.
* **Fail loudly, not silently.** Every rewrite that changes the request is
  logged to stderr so a debugging session can see exactly what was mutated.

Usage
-----
Local (decrypts the login file itself, needs ``WorkBuddy.exe``)::

    python workbuddy_relay.py --port 8787

GitHub Actions (token comes from a secret, no exe needed)::

    WORKBUDDY_ACCESS_TOKEN=... python workbuddy_relay.py --port 8787

Then point a provider at ``http://127.0.0.1:8787/v1`` with any non-empty
``api_key`` (the value is ignored — the real credential is the access token).

Env vars
--------
``WORKBUDDY_ACCESS_TOKEN``   upstream access token (CI path). If unset, the
                             local decryption path is used.
``WORKBUDDY_BASE_URL``       override the upstream base (default
                             ``https://copilot.tencent.com``).
``WORKBUDDY_AUTH_FILE``      override the login-file path (local path).
``WORKBUDDY_EXE``            override the WorkBuddy.exe path (local path).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ── upstream constants ────────────────────────────────────────────────────
DEFAULT_BASE = "https://copilot.tencent.com"
CHAT_PATH = "/v2/chat/completions"
CONFIG_PATH = "/v3/config"

# Local login file + exe, in the same order the dsh-workbuddy-connect plugin
# probes them.  The D:\... entry covers the "小番茄C盘清理软件搬家" relocation.
AUTH_CANDIDATES = (
    os.path.join(os.environ.get("LOCALAPPDATA", ""),
                 "CodeBuddyExtension", "Data", "Public", "auth",
                 "workbuddy-desktop.info"),
    os.path.join(os.environ.get("APPDATA", ""),
                 "CodeBuddyExtension", "Data", "Public", "auth",
                 "workbuddy-desktop.info"),
)
EXE_CANDIDATES = (
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "WorkBuddy",
                 "WorkBuddy.exe"),
    r"D:\小番茄C盘清理软件搬家目录\WorkBuddy\WorkBuddy.exe",
)

# The exact AAD the desktop app builds (transcribed from
# dsh-workbuddy-connect/lib/host-heartbeat-L93zSVB5.js:1777-1798).
#   b"WB-AAD\0" + [1] + lenPrefixed("WBEV1") + lenPrefixed("sym-v1")
#   + u32be(suite) + lenPrefixed(keyId) + [2,0,0]
def _lp(s: str) -> bytes:
    b = s.encode("utf-8")
    return len(b).to_bytes(4, "big") + b


# CREATE_NO_WINDOW (0x08000000) keeps the helper exe from flashing a console.
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def build_aad(key_id: str, suite: int) -> bytes:
    return (b"WB-AAD\0" + bytes([1]) + _lp("WBEV1") + _lp("sym-v1")
            + suite.to_bytes(4, "big") + _lp(key_id) + bytes([2, 0, 0]))


# ── credential resolution ─────────────────────────────────────────────────
class Credential:
    """Access-token provider with the env-var path preferred."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._token: str | None = None
        self._obtained_at = 0.0
        self._ttl = float(os.environ.get("WORKBUDDY_TOKEN_TTL", "1800"))

    # -- public ----------------------------------------------------------
    def get(self) -> str:
        with self._lock:
            if self._token and (time.time() - self._obtained_at) < self._ttl:
                return self._token
            self._token = self._resolve()
            self._obtained_at = time.time()
            return self._token

    # -- internals -------------------------------------------------------
    def _resolve(self) -> str:
        env = os.environ.get("WORKBUDDY_ACCESS_TOKEN", "").strip()
        if env:
            _log("credential: using WORKBUDDY_ACCESS_TOKEN from environment")
            return env
        return _decrypt_local_login()

    def invalidate(self) -> None:
        with self._lock:
            self._token = None


_CRED = Credential()


def _log(msg: str) -> None:
    print(f"[relay] {msg}", file=sys.stderr, flush=True)


def _first_existing(paths) -> str | None:
    for p in paths:
        if p and os.path.isfile(p):
            return p
    return None


def _find_exe() -> str:
    p = os.environ.get("WORKBUDDY_EXE") or _first_existing(EXE_CANDIDATES)
    if not p:
        raise RuntimeError(
            "WorkBuddy.exe not found; set WORKBUDDY_EXE or provide "
            "WORKBUDDY_ACCESS_TOKEN")
    return p


def _find_auth_file() -> str:
    p = os.environ.get("WORKBUDDY_AUTH_FILE") or _first_existing(AUTH_CANDIDATES)
    if not p:
        raise RuntimeError(
            "workbuddy-desktop.info not found; set WORKBUDDY_AUTH_FILE or "
            "provide WORKBUDDY_ACCESS_TOKEN")
    return p


def _at_rest_secret() -> str:
    """Run WorkBuddy.exe as a plain Node process to extract the key.

    Uses a temp file rather than a pipe — several sandboxes refuse to let a
    child inherit stdio handles.
    """
    exe = _find_exe()
    fd, out = tempfile.mkstemp(suffix=".json", prefix="wbkey-")
    os.close(fd)
    try:
        script = (
            'require("fs").writeFileSync(process.env.WB_KEY_OUT,'
            'String(process._linkedBinding('
            '"electron_browser_workbuddy_storage").loggerGet()))'
        )
        env = dict(os.environ, ELECTRON_RUN_AS_NODE="1", WB_KEY_OUT=out)
        # NOTE: Popen has no `windowsHide` kwarg — suppressing the console
        # window on Windows is done through `creationflags`.
        subprocess.run([exe, "-e", script], env=env, timeout=30000,
                       check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL,
                       creationflags=_NO_WINDOW)
        with open(out, "r", encoding="utf-8") as fh:
            return json.load(fh)["atRestSecretKey"]
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass


def _open_field(field: dict, key: bytes) -> str:
    """AES-256-GCM-decrypt one ``{$wbEncrypted:1, envelope:...}`` field."""
    env = json.loads(base64.b64decode(field["envelope"]))
    nonce = base64.b64decode(env["nonce"])
    tag = base64.b64decode(env["authTag"])
    ct = base64.b64decode(env["ciphertext"])
    aad = build_aad(env["keyId"], env.get("suite", 1))
    return _aes_gcm_decrypt(key, nonce, ct, tag, aad).decode("utf-8")


def _aes_gcm_decrypt(key: bytes, nonce: bytes, ct: bytes, tag: bytes,
                     aad: bytes) -> bytes:
    """Decrypt with whichever AES-GCM the runtime offers.

    Prefers ``cryptography`` (fast C path).  Falls back to a pure-Python
    implementation so the relay still works on a bare runner — the bundled
    DSH Python has no ``cryptography`` module.
    """
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        return AESGCM(key).decrypt(nonce, ct + tag, aad)
    except ImportError:
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (here, os.path.dirname(here)):
        if os.path.isfile(os.path.join(cand, "_aesgcm.py")):
            if cand not in sys.path:
                sys.path.insert(0, cand)
            break
    try:
        from _aesgcm import gcm_decrypt           # noqa: E402
    except ImportError as e:
        raise RuntimeError(
            "no AES-GCM available: install `cryptography`, or place "
            "_aesgcm.py next to this script") from e
    return gcm_decrypt(key, nonce, ct, tag, aad)


def _decrypt_local_login() -> str:
    auth_file = _find_auth_file()
    with open(auth_file, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    secret = _at_rest_secret()
    key = hashlib.sha256(secret.encode("utf-8")).digest()
    field = doc["auth"]["accessToken"]
    token = _open_field(field, key)
    exp = doc["auth"].get("expiresAt")
    if exp:
        left_days = (exp / 1000 - time.time()) / 86400
        _log(f"credential: decrypted local login, token valid "
             f"{left_days:.1f} more days")
        if left_days < 3:
            _log("credential: WARNING — token expires soon, refresh in the "
                 "WorkBuddy app")
    else:
        _log("credential: decrypted local login (no expiry recorded)")
    return token


# ── request rewriting ─────────────────────────────────────────────────────
def rewrite_request(body: dict) -> tuple[dict, list[str]]:
    """Apply the four upstream requirements.  Returns (body, notes)."""
    notes: list[str] = []

    # 1. stream must be true.
    if body.get("stream") is not True:
        body["stream"] = True
        notes.append("stream→true")

    # 2. role:"developer" → "system".
    msgs = body.get("messages")
    if isinstance(msgs, list):
        for m in msgs:
            if isinstance(m, dict) and m.get("role") == "developer":
                m["role"] = "system"
                notes.append("developer→system")

    # 3. tool_choice must be a string.
    tc = body.get("tool_choice")
    if isinstance(tc, dict):
        fn = tc.get("function")
        name = fn.get("name") if isinstance(fn, dict) else None
        if name:
            body["tool_choice"] = json.dumps(
                {"type": "function", "function": {"name": name}},
                ensure_ascii=False)
        else:
            body["tool_choice"] = "auto"
        notes.append("tool_choice→string")

    return body, notes


def _usage_from_frame(frame: dict) -> dict | None:
    u = frame.get("usage")
    return u if isinstance(u, dict) else None


# ── SSE aggregation ───────────────────────────────────────────────────────
def aggregate_sse(frames: list[dict]) -> dict:
    """Fold streamed deltas into a non-streaming ChatCompletion payload."""
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_parts: list[dict] = []
    role = "assistant"
    finish_reason = None
    usage = None
    obj_id = None
    model = None
    created = None

    for f in frames:
        obj_id = obj_id or f.get("id")
        model = model or f.get("model")
        created = created if created is not None else f.get("created")
        if f.get("error"):
            raise RuntimeError(f"upstream error: {json.dumps(f['error'])[:400]}")
        u = _usage_from_frame(f)
        if u:
            usage = u
        choices = f.get("choices") or []
        if not choices:
            continue
        ch = choices[0] or {}
        d = ch.get("delta") or {}
        if d.get("role"):
            role = d["role"]
        if d.get("content"):
            content_parts.append(d["content"])
        for key in ("reasoning_content", "reasoning"):
            v = d.get(key)
            if isinstance(v, str) and v:
                reasoning_parts.append(v)
        for tc in d.get("tool_calls") or []:
            tool_parts.append(tc)
        if ch.get("finish_reason"):
            finish_reason = ch["finish_reason"]

    message: dict = {"role": role, "content": "".join(content_parts)}
    if reasoning_parts:
        # Keep the reasoning trace out of ``content`` (the pipeline writes
        # ``content`` straight into the doc) but don't discard it either.
        message["reasoning_content"] = "".join(reasoning_parts)
    if tool_parts:
        message["tool_calls"] = tool_parts

    payload = {
        "id": obj_id or "chatcmpl-relay",
        "object": "chat.completion",
        "created": created if created is not None else int(time.time()),
        "model": model or "unknown",
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish_reason or "stop",
            "logprobs": None,
        }],
    }
    if usage:
        payload["usage"] = usage
    return payload


# ── upstream call ─────────────────────────────────────────────────────────
def call_upstream(body: dict, timeout: float) -> dict:
    """POST to the gateway, read the SSE stream, return aggregated JSON."""
    base = os.environ.get("WORKBUDDY_BASE_URL", DEFAULT_BASE).rstrip("/")
    token = _CRED.get()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://www.codebuddy.cn",
        "Referer": "https://www.codebuddy.cn/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) WorkBuddy/5.7.6",
        "X-IDE-Type": "WorkBuddy",
        "X-IDE-Name": "WorkBuddy",
        "X-IDE-Version": "5.7.6",
        "X-Product": "SaaS",
    }
    req = urllib.request.Request(
        base + CHAT_PATH, data=json.dumps(body).encode("utf-8"),
        headers=headers, method="POST")
    ctx = ssl.create_default_context()
    frames: list[dict] = []
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            buf = b""
            while True:
                chunk = r.read(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line.startswith(b"data:"):
                        continue
                    data = line[5:].strip()
                    if data == b"[DONE]":
                        continue
                    try:
                        frames.append(json.loads(data))
                    except ValueError:
                        pass
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:600]
        if e.code in (401, 403):
            _log("upstream 401/403 — dropping cached token and retrying once")
            _CRED.invalidate()
            raise _UpstreamAuthError(detail) from e
        raise RuntimeError(f"upstream HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"upstream connection failed: {e}") from e

    return aggregate_sse(frames)


class _UpstreamAuthError(RuntimeError):
    pass


# ── HTTP surface ──────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    server_version = "WorkBuddyRelay/1.0"
    timeout = 600

    # -- helpers ---------------------------------------------------------
    def _send_json(self, code: int, obj) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_body(self) -> dict | None:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return None
        try:
            return json.loads(self.rfile.read(n))
        except ValueError:
            return None

    def log_message(self, fmt, *args):   # silence the default access log
        pass

    # -- routes ----------------------------------------------------------
    def do_GET(self):
        if self.path in ("/healthz", "/v1/healthz", "/health"):
            self._send_json(200, {"status": "ok",
                                  "credential": "env" if os.environ.get(
                                      "WORKBUDDY_ACCESS_TOKEN") else "local"})
            return
        if self.path in ("/v1/models", "/models"):
            try:
                models = self._upstream_models()
            except Exception as e:               # noqa: BLE001
                self._send_json(502, {"error": str(e)[:400]})
                return
            self._send_json(200, {"object": "list", "data": models})
            return
        self._send_json(404, {"error": f"unknown path {self.path}"})

    def _upstream_models(self) -> list[dict]:
        base = os.environ.get("WORKBUDDY_BASE_URL", DEFAULT_BASE).rstrip("/")
        req = urllib.request.Request(base + CONFIG_PATH, headers={
            "Authorization": f"Bearer {_CRED.get()}",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "CLI/2.63.2 CodeBuddy/2.63.2",
        })
        with urllib.request.urlopen(req, timeout=60,
                                    context=ssl.create_default_context()) as r:
            j = json.loads(r.read().decode("utf-8"))
        raw = j.get("data", {}).get("models") or j.get("models") or []
        return [{"id": m.get("id"), "object": "model",
                 "created": 0, "owned_by": "workbuddy",
                 "context_window": m.get("maxInputTokens"),
                 "max_output_tokens": m.get("maxOutputTokens")}
                for m in raw if m.get("id")]

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if path not in ("/v1/chat/completions", "/chat/completions"):
            self._send_json(404, {"error": f"unknown path {self.path}"})
            return

        body = self._read_body()
        if body is None:
            self._send_json(400, {"error": "invalid JSON body"})
            return

        model = body.get("model", "")
        body, notes = rewrite_request(body)
        _log(f"POST {model} rewrites=[{','.join(notes) or 'none'}] "
             f"messages={len(body.get('messages') or [])}")

        # The SDK's own timeout (Summarizer uses 180s) bounds the whole
        # request, so a longer upstream budget is safe: if the SDK gives up
        # first the socket just closes on our side.
        timeout = float(os.environ.get("WORKBUDDY_TIMEOUT", "600"))
        try:
            result = call_upstream(body, timeout)
        except _UpstreamAuthError as e:
            # One retry with a freshly resolved credential.
            try:
                result = call_upstream(body, timeout)
            except Exception as e2:                # noqa: BLE001
                self._send_json(502, {"error": {
                    "message": str(e2)[:400], "type": "relay_upstream_error",
                    "first_error": str(e)[:200]}})
                return
        except Exception as e:                     # noqa: BLE001
            self._send_json(502, {"error": {
                "message": str(e)[:400], "type": "relay_upstream_error"}})
            return

        u = result.get("usage") or {}
        _log(f"OK {model}: {len(result['choices'][0]['message'].get('content') or '')} chars, "
             f"prompt={u.get('prompt_tokens', '?')} "
             f"completion={u.get('completion_tokens', '?')} "
             f"credit={u.get('credit', '?')}")
        self._send_json(200, result)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("WORKBUDDY_PORT", "8787")))
    args = ap.parse_args()

    # Resolve the credential once at startup so a broken setup fails fast
    # instead of on the first request.
    try:
        _CRED.get()
    except Exception as e:                         # noqa: BLE001
        _log(f"FATAL: cannot obtain a credential — {e}")
        return 2

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    _log(f"listening on http://{args.host}:{args.port}/v1  "
         f"(upstream {os.environ.get('WORKBUDDY_BASE_URL', DEFAULT_BASE)})")
    _log("routes: GET /healthz, GET /v1/models, POST /v1/chat/completions")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        _log("shutting down")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
