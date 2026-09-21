"""
A small stand-in for the kind of application this system is really aimed at:
a back-office screen with no test ids, table markup, and the two runtime
conditions SauceDemo never produces.

Why it exists: the error taxonomy claims three classes, and a public demo
site only ever gave us two. This app produces the third on demand —

  * a notice that appears on *some* loads and covers the control you need,
    which is recoverable: dismiss it and carry on;
  * a `--break` mode where the control is renamed, so every locator in the
    cascade misses, which is a hard failure and should look like one.

Run it, record against it, then replay it both ways:

    python scripts/mock_legacy_app.py --port 8099
    python scripts/mock_legacy_app.py --port 8099 --break
    python scripts/mock_legacy_app.py --port 8099 --block-every 1
"""

from __future__ import annotations

import argparse
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# Deliberately unhelpful markup: tables for layout, no data-test attributes,
# inputs identified by name only. This is what "no clean DOM" looks like.
SEARCH_PAGE = """<!doctype html>
<html><head><title>Member Services</title></head>
<body>
<h1>Member Services</h1>
{notice}
<form method="get" action="/record">
  <table>
    <tr><td><b>Member number</b></td><td><input type="text" name="member"></td></tr>
    <tr><td colspan="2">{lookup}</td></tr>
  </table>
</form>
</body></html>
"""

# A modal, not a banner: it covers the form and swallows clicks, so the page
# underneath is genuinely unusable until it's gone. A decorative notice would
# prove nothing -- automation would sail straight past it.
NOTICE = """
<div role="alert" id="notice" style="position:fixed;inset:0;background:rgba(0,0,0,.45);
     display:flex;align-items:center;justify-content:center;z-index:99">
  <div style="background:#fff;padding:24px;border:1px solid #888;max-width:24em">
    <p>Scheduled maintenance this weekend.</p>
    <button type="button" onclick="document.getElementById('notice').remove()">Dismiss</button>
  </div>
</div>
"""

RECORD_PAGE = """<!doctype html>
<html><head><title>Member Services</title></head>
<body>
<h1>Member {member}</h1>
<table>
  <tr><td>Status</td><td>Active</td></tr>
  <tr><td>Balance</td><td>$1,204.55</td></tr>
</table>
<form method="post" action="/confirm">
  <input type="hidden" name="member" value="{member}">
  <input type="submit" value="{confirm_label}">
</form>
</body></html>
"""

DONE_PAGE = """<!doctype html>
<html><head><title>Member Services</title></head>
<body><h1>Statement requested</h1>
<p>A statement for member {member} is on its way.</p></body></html>
"""


def build_server(port: int, broken: bool, block_every: int) -> ThreadingHTTPServer:
    loads = {"count": 0}

    class Handler(BaseHTTPRequestHandler):
        def _send(self, body: str) -> None:
            encoded = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self):
            url = urlparse(self.path)
            query = parse_qs(url.query)
            if url.path == "/record":
                member = (query.get("member") or ["unknown"])[0]
                self._send(RECORD_PAGE.format(member=member, confirm_label="Request statement"))
                return
            loads["count"] += 1
            showing = block_every > 0 and loads["count"] % block_every == 0
            # In broken mode the control the recording depends on is simply
            # gone -- the way a release removes a button. Every locator in
            # the cascade misses and nothing on the page explains why, which
            # is what a hard failure is: not a refusal, not something to
            # retry, just a flow that no longer matches the application.
            lookup = "" if broken else '<input type="submit" value="Look up">'
            self._send(SEARCH_PAGE.format(notice=NOTICE if showing else "", lookup=lookup))

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            fields = parse_qs(self.rfile.read(length).decode())
            self._send(DONE_PAGE.format(member=(fields.get("member") or ["unknown"])[0]))

        def log_message(self, *args):
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--break", dest="broken", action="store_true",
                        help="Remove the control the recording depends on, to force a hard failure")
    parser.add_argument("--block-every", type=int, default=2, metavar="N",
                        help="Show the blocking notice on every Nth load of the search page (0 = never)")
    args = parser.parse_args()

    server = build_server(args.port, args.broken, args.block_every)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Mock legacy app on http://127.0.0.1:{args.port}")
    print(f"  blocking notice: {'every ' + str(args.block_every) + ' loads' if args.block_every else 'never'}")
    print(f"  control renamed: {args.broken}")
    print("Ctrl-C to stop.")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
