"""CDP (Chrome DevTools Protocol) driver implemented with the Python standard library only.

Used to drive a local Edge/Chrome instance that is already logged into QQ, so that
authenticated requests can be issued *same-origin* from inside the page
(no signature/reverse-engineering needed, cookies + g_tk handled by the browser).
"""
from __future__ import annotations

import base64
import json
import os
import socket
import struct
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

def _default_root() -> str:
    env = os.environ.get("QQALBUM_ROOT")
    if env:
        return os.path.abspath(env)
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(here, "backup")


DEFAULT_PROFILE_DIR = os.path.join(_default_root(), "edge-profile")

_BROWSER_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]


def find_browser() -> Optional[str]:
    for p in _BROWSER_CANDIDATES:
        if p and os.path.isfile(p):
            return p
    return None


def is_timeout_error(exc: BaseException) -> bool:
    """True for the CDP/socket timeout shapes that a browser restart can cure."""
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return True
    if isinstance(exc, CDPError):
        text = str(exc)
        return "timeout" in text or "timed out" in text
    return False


class CDPError(RuntimeError):
    pass


class _WS:
    """Minimal WebSocket client (RFC 6455) - text frames only."""

    def __init__(self, url: str, timeout: float = 30.0):
        rest = url.split("://", 1)[1]
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        # No fixed socket timeout: each CDP call sets its own deadline (long
        # Runtime.evaluate calls would otherwise die at the default timeout).
        self.sock = socket.create_connection((host, int(port or 80)), timeout=None)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET /{path} HTTP/1.1\r\n"
            f"Host: {hostport}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise CDPError("websocket handshake failed")
            buf += chunk
        head, _, self._buf = buf.partition(b"\r\n\r\n")
        if b" 101" not in head.split(b"\r\n")[0]:
            raise CDPError(f"websocket handshake rejected: {head[:200]!r}")

    # -- framing ---------------------------------------------------------
    def _send_frame(self, data: bytes, opcode: int = 0x1) -> None:
        header = bytearray([0x80 | opcode])
        mask = os.urandom(4)
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(bytes(header) + masked)

    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            try:
                chunk = self.sock.recv(65536)
            except OSError as exc:
                raise CDPError(f"websocket read failed: {exc}") from None
            if not chunk:
                raise CDPError("websocket closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def recv(self) -> Dict[str, Any]:
        while True:
            b1, b2 = self._read_exact(2)
            opcode = b1 & 0x0F
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read_exact(8))[0]
            payload = self._read_exact(length)
            if opcode == 0x1:
                return json.loads(payload.decode("utf-8"))
            if opcode == 0x8:
                raise CDPError("websocket closed by peer")
            if opcode == 0x9:  # ping -> pong
                self._send_frame(payload, opcode=0xA)

    def send(self, obj: Any) -> None:
        self._send_frame(json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def close(self) -> None:
        try:
            self._send_frame(b"", opcode=0x8)
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


class Browser:
    """Attach to (or launch) a CDP-enabled browser."""

    def __init__(self, port: int = 9222, profile_dir: str = DEFAULT_PROFILE_DIR,
                 browser_path: Optional[str] = None, headless: bool = False):
        self.port = port
        self.profile_dir = profile_dir
        self.browser_path = browser_path or find_browser()
        self.headless = headless
        self._browser_ws: Optional[_WS] = None
        self._events: List[Dict[str, Any]] = []
        self._handlers: Dict[str, Any] = {}

    # -- HTTP endpoints --------------------------------------------------
    def _http(self, path: str, timeout: float = 5.0) -> Any:
        url = f"http://127.0.0.1:{self.port}{path}"
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def is_up(self) -> bool:
        try:
            self._http("/json/version")
            return True
        except Exception:
            return False

    def version(self) -> Dict[str, Any]:
        return self._http("/json/version")

    def targets(self) -> List[Dict[str, Any]]:
        return self._http("/json/list")

    def pages(self) -> List[Dict[str, Any]]:
        return [t for t in self.targets() if t.get("type") == "page"]

    # -- process ---------------------------------------------------------
    def launch(self, url: str = "about:blank", wait: float = 30.0) -> None:
        if self.is_up():
            return
        if not self.browser_path:
            raise CDPError("No Edge/Chrome executable found")
        os.makedirs(self.profile_dir, exist_ok=True)
        args = [
            self.browser_path,
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=Translate,msEdgeSplitScreen",
            "--remote-allow-origins=*",
        ]
        if self.headless:
            args.append("--headless=new")
        args.append(url)
        creationflags = 0
        if os.name == "nt":
            creationflags = 0x00000008 | 0x08000000  # DETACHED_PROCESS | CREATE_NO_WINDOW
        subprocess.Popen(args, creationflags=creationflags, close_fds=True)
        deadline = time.time() + wait
        while time.time() < deadline:
            if self.is_up():
                return
            time.sleep(0.4)
        raise CDPError("browser did not expose the CDP port in time")

    def restart(self, url: str = "about:blank", wait: float = 40.0) -> None:
        """Kill the debug browser and start it again (recovers a wedged network stack).

        QZone pages occasionally stop resolving any fetch after a very long crawl;
        restarting the browser process clears it. The login state lives in the
        profile directory, so it survives the restart.
        """
        self.close()
        if os.name == "nt" and self.profile_dir:
            marker = self.profile_dir.replace('"', "")
            try:
                ps = ("Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
                      f"Where-Object {{ $_.CommandLine -like '*{marker}*' }} | "
                      "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
                subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                               capture_output=True, timeout=60)
            except Exception:
                pass
        deadline = time.time() + 25
        while self.is_up() and time.time() < deadline:
            time.sleep(0.5)
        self.launch(url, wait=wait)

    def ensure_page(self, url: str) -> Dict[str, Any]:
        """Return a page target, navigating the first page to `url` when needed."""
        self.launch(url)
        pages = self.pages()
        if not pages:
            # fresh profile sometimes only has the "new tab" internal target
            try:
                self._http(f"/json/new?{urllib.parse.quote(url, safe='')}", timeout=10)
            except Exception:
                pass
            time.sleep(1.0)
            pages = self.pages()
        if not pages:
            raise CDPError("no page target available")
        page = pages[0]
        if url not in (page.get("url") or ""):
            self.call("Page.navigate", {"url": url}, target_id=page["id"])
        return page

    # -- browser-level connection (sessions) -----------------------------
    def _connect_browser(self) -> _WS:
        if self._browser_ws is None:
            ws_url = self.version()["webSocketDebuggerUrl"]
            self._browser_ws = _WS(ws_url, timeout=60)
        return self._browser_ws

    def call(self, method: str, params: Optional[Dict[str, Any]] = None,
             target_id: Optional[str] = None, timeout: float = 30.0,
             session_id: Optional[str] = None) -> Dict[str, Any]:
        """Issue a CDP command, rebuilding the browser connection once if it dropped."""
        payload: Dict[str, Any] = {"method": method, "params": params or {}}
        try:
            return self._call_once(payload, session_id, timeout)
        except OSError as exc:  # connection reset / broken pipe: reconnect and retry once
            self._browser_ws = None
            time.sleep(0.6)
            try:
                return self._call_once(payload, session_id, timeout)
            except OSError as retry_exc:
                raise CDPError(f"{method}: browser connection lost ({retry_exc})") from None
        except CDPError:
            if session_id:
                # the flattened session may have died with a browser restart
                self._browser_ws = None
                time.sleep(0.3)
                try:
                    return self._call_once(payload, session_id, timeout)
                except CDPError:
                    pass
            raise

    def _call_once(self, payload: Dict[str, Any], session_id: Optional[str],
                   timeout: float) -> Dict[str, Any]:
        ws = self._connect_browser()
        msg_id = getattr(self, "_mid", 0) + 1
        self._mid = msg_id
        payload = dict(payload)
        payload["id"] = msg_id
        if session_id:
            payload["sessionId"] = session_id
        try:
            ws.send(payload)
        except OSError as exc:
            raise CDPError(f"send failed: {exc}") from None
        method = payload.get("method", "?")
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise CDPError(f"timeout waiting for {method}")
            ws.sock.settimeout(remaining)
            try:
                msg = ws.recv()
            except socket.timeout:
                raise CDPError(f"timeout waiting for {method}") from None
            if msg.get("id") == msg_id:
                if "error" in msg:
                    raise CDPError(f"{method}: {msg['error']}")
                return msg.get("result", {})
            self._dispatch(msg)

    def _dispatch(self, msg: Dict[str, Any]) -> None:
        method = msg.get("method")
        if not method:
            return
        self._events.append(msg)
        handler = self._handlers.get(method)
        if handler:
            try:
                handler(msg.get("params", {}), msg.get("sessionId"))
            except Exception:
                pass

    def pump(self, seconds: float) -> None:
        """Read and dispatch events for `seconds` (keeps the ws buffer drained)."""
        ws = self._connect_browser()
        ws.sock.settimeout(0.5)
        deadline = time.time() + seconds
        try:
            while time.time() < deadline:
                try:
                    self._dispatch(ws.recv())
                except socket.timeout:
                    continue
                except CDPError:
                    break
        finally:
            ws.sock.settimeout(60)

    def on(self, event: str, handler) -> None:
        self._handlers[event] = handler

    def drain_events(self) -> List[Dict[str, Any]]:
        out, self._events = self._events, []
        return out

    # -- target sessions -------------------------------------------------
    def attach(self, target_id: str) -> str:
        """Attach to a target and return its flattened session id."""
        res = self.call("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        return res["sessionId"]

    def page(self, url: str = "about:blank", reuse: bool = True) -> "PageSession":
        """Get a PageSession for `url`, reusing an existing tab of that site."""
        self.launch(url)
        target = None
        if reuse:
            for t in self.pages():
                if _same_site(t.get("url", ""), url):
                    target = t
                    break
        if target is None:
            for t in self.pages():
                if t.get("url", "").startswith("about:") or "newtab" in t.get("url", ""):
                    target = t
                    break
        if target is None:
            pages = self.pages()
            target = pages[0] if pages else None
        if target is None:
            raise CDPError("no page target available; launch the browser first")
        session = self.attach(target["id"])
        ps = PageSession(self, session, target["id"])
        if url not in (target.get("url") or "") and not _same_site(target.get("url", ""), url):
            ps.navigate(url)
        return ps

    # -- convenience -----------------------------------------------------
    def eval_js(self, expression: str, target_id: Optional[str] = None,
                await_promise: bool = True, timeout: float = 60.0) -> Any:
        """Evaluate JS in a page (attaching a session on demand)."""
        session = self.attach(target_id) if target_id else None
        try:
            return PageSession(self, session, target_id).eval(
                expression, await_promise=await_promise, timeout=timeout)
        finally:
            if session:
                try:
                    self.call("Target.detachFromTarget", {"sessionId": session})
                except Exception:
                    pass

    def close(self) -> None:
        if self._browser_ws:
            self._browser_ws.close()
            self._browser_ws = None


def _same_site(a: str, b: str) -> bool:
    def host(u: str) -> str:
        u = u.split("://", 1)[-1]
        return u.split("/", 1)[0].split("?", 1)[0]
    return bool(a) and bool(b) and host(a) == host(b)


class PageSession:
    """A flattened CDP session bound to one page target."""

    def __init__(self, browser: "Browser", session_id: str, target_id: Optional[str] = None):
        self.browser = browser
        self.session_id = session_id
        self.target_id = target_id

    def call(self, method: str, params: Optional[Dict[str, Any]] = None,
             timeout: float = 30.0) -> Dict[str, Any]:
        return self.browser.call(method, params, session_id=self.session_id, timeout=timeout)

    def enable_domains(self) -> None:
        for domain in ("Page", "Runtime", "Network"):
            try:
                self.call(f"{domain}.enable", timeout=20)
            except CDPError:
                pass

    def eval(self, expression: str, await_promise: bool = True, timeout: float = 60.0) -> Any:
        res = self.call("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": await_promise,
            "userGesture": True,
        }, timeout=timeout)
        if res.get("exceptionDetails"):
            detail = res["exceptionDetails"]
            text = (detail.get("exception") or {}).get("description") or detail.get("text")
            raise CDPError(f"JavaScript error: {text}")
        return res.get("result", {}).get("value")

    def url(self) -> str:
        try:
            return self.eval("location.href", timeout=15) or ""
        except CDPError:
            return ""

    def navigate(self, url: str, wait: float = 3.0) -> None:
        self.call("Page.navigate", {"url": url}, timeout=45)
        time.sleep(wait)

    def wait_ready(self, timeout: float = 45.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.eval("document.readyState", timeout=10) == "complete":
                    return True
            except CDPError:
                pass
            time.sleep(0.5)
        return False

    def wait_until(self, expression: str, timeout: float = 60.0, interval: float = 0.5) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.eval(f"!!({expression})", timeout=15):
                    return True
            except CDPError:
                pass
            time.sleep(interval)
        return False

    def detach(self) -> None:
        try:
            self.browser.call("Target.detachFromTarget", {"sessionId": self.session_id})
        except Exception:
            pass
