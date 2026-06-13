"""A DELIBERATELY VULNERABLE demo web app — the scanner's target.

Standard library only: http.server + sqlite3. Do NOT deploy this. Every
endpoint below is a textbook injection sink, planted so the vuln_scan loop has
something real to find and — more importantly — something real to *exploit*.

The loop starts this as a subprocess bound to 127.0.0.1 before scanning and
tears it down after. The verifier never reads this file's verdict; it fires HTTP
requests at the live process and checks whether a per-finding canary echoes back.

Sinks planted here (file:line drift if you edit — the scanner reads the source):
  - GET /ping?host=     -> os.popen("ping -c1 " + host)        # command injection
  - GET /lookup?id=     -> sqlite f-string query               # SQL injection
  - GET /greet?name=    -> reflected, unescaped into HTML       # reflected XSS-ish
  - GET /calc?expr=     -> eval(expr)                           # code/eval injection

Run standalone for manual poking:
    python -m loops.vuln_scan.demo_app --host 127.0.0.1 --port 8077
"""
from __future__ import annotations

import argparse
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


def _init_db() -> sqlite3.Connection:
    """In-memory throwaway DB so the demo needs no files on disk."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, secret TEXT)")
    conn.executemany(
        "INSERT INTO users (id, name, secret) VALUES (?, ?, ?)",
        [(1, "alice", "alice-token"), (2, "bob", "bob-token")],
    )
    conn.commit()
    return conn


class VulnHandler(BaseHTTPRequestHandler):
    db: sqlite3.Connection  # set on the server class below

    # Quiet the default request logging so the loop's stdout stays readable.
    def log_message(self, *args: object) -> None:  # noqa: D401
        return

    def _send(self, body: str, status: int = 200) -> None:
        payload = body.encode("utf-8", errors="replace")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        parsed = urlparse(self.path)
        route = parsed.path
        params = parse_qs(parsed.query)

        def first(key: str, default: str = "") -> str:
            vals = params.get(key)
            return vals[0] if vals else default

        if route == "/":
            self._send(
                "<h1>vuln demo</h1><ul>"
                "<li>/ping?host=127.0.0.1</li>"
                "<li>/lookup?id=1</li>"
                "<li>/greet?name=world</li>"
                "<li>/calc?expr=1+1</li>"
                "</ul>"
            )
            return

        if route == "/ping":
            # COMMAND INJECTION: host is concatenated straight into a shell.
            host = first("host", "127.0.0.1")
            output = os.popen("ping -c1 " + host).read()  # noqa: S605 (intentional)
            self._send(f"<pre>{output}</pre>")
            return

        if route == "/lookup":
            # SQL INJECTION: id is f-stringed into the query, output reflected.
            uid = first("id", "1")
            try:
                cur = self.db.execute(f"SELECT id, name FROM users WHERE id = {uid}")
                rows = cur.fetchall()
            except sqlite3.Error as exc:
                rows = [("error", str(exc))]
            self._send("<pre>" + "\n".join(f"{r[0]}: {r[1]}" for r in rows) + "</pre>")
            return

        if route == "/greet":
            # REFLECTED INJECTION: name echoed into HTML with no escaping.
            name = first("name", "stranger")
            self._send(f"<p>Hello, {name}!</p>")
            return

        if route == "/calc":
            # CODE INJECTION: expr handed to eval().
            expr = first("expr", "0")
            try:
                result = eval(expr)  # noqa: S307 (intentional)
            except Exception as exc:  # noqa: BLE001
                result = f"err: {exc}"
            self._send(f"<pre>{result}</pre>")
            return

        self._send("not found", status=404)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_server(host: str, port: int) -> _Server:
    handler = type("BoundVulnHandler", (VulnHandler,), {"db": _init_db()})
    return _Server((host, port), handler)


def main() -> None:
    ap = argparse.ArgumentParser(description="deliberately vulnerable demo app")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8077)
    args = ap.parse_args()
    server = make_server(args.host, args.port)
    print(f"serving vuln demo on http://{args.host}:{args.port} (ctrl-c to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
