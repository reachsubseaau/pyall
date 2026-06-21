# name:         monitor
# created:      June 2026
# by:           paul.kennedy@guardiangeomatics.com
# description:  tiny web server that shows the live status and log of pyall while it processes
#               .all files.  The page auto-refreshes every few seconds so you can watch progress.
#
# Run it in a second terminal while pyall.py / the MCP server is processing:
#
#       .venv\Scripts\python.exe monitor.py
#       .venv\Scripts\python.exe monitor.py --dir <folder> --port 8770
#
# Then open http://127.0.0.1:8770/ in a browser.
#
# It reads two things written by pyall:
#   * pyall.log          - the shared rotating run log (one file for the whole server)
#   * pyall_status.json  - the current job/file/progress (written by pyall.writestatus)
# By default it watches the shared log folder (the PYALL_LOG_DIR environment variable, or a
# "logs" folder next to this script) so it shows everything the MCP server processes.
# Use --dir to watch a specific job's output folder instead.

import os
import glob
import json
import html
import time
import re
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOGO_NAME = "reachlogo.png"
LOGO_PATH = os.path.join(SCRIPT_DIR, LOGO_NAME)
LOG_NAME = "pyall.log"
STATUS_NAME = "pyall_status.json"
README_NAME = "README.MD"
LOG_TAIL_LINES = 500


###############################################################################
def pyall_version():
    '''return the pyall MCP server version by reading __version__ from pyall_mcp.py.
    Reads the source file directly (no import) so the monitor has no heavy dependencies.'''
    try:
        path = os.path.join(SCRIPT_DIR, "pyall_mcp.py")
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip().startswith("__version__"):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return "unknown"



###############################################################################
def central_log_dir():
    '''return the shared log folder used by pyall (must match pyall.logdirectory()).'''
    d = os.environ.get("PYALL_LOG_DIR", "")
    if not d:
        d = os.path.join(SCRIPT_DIR, "logs")
    return d


###############################################################################
def find_watch_dir(start):
    '''return the most sensible folder to watch.

    Prefer the shared central log folder, then the newest folder that already contains a
    pyall log/status file, then the start folder itself.
    '''
    central = central_log_dir()
    if os.path.isfile(os.path.join(central, LOG_NAME)) or os.path.isfile(os.path.join(central, STATUS_NAME)):
        return central

    start = os.path.abspath(start)
    candidates = []
    for name in (LOG_NAME, STATUS_NAME):
        candidates += glob.glob(os.path.join(start, "**", name), recursive=True)
    if candidates:
        newest = max(candidates, key=lambda p: os.path.getmtime(p))
        return os.path.dirname(newest)

    # nothing yet - default to the central log folder so it appears once logging starts
    return central


###############################################################################
def tail(path, maxlines):
    '''return the last *maxlines* lines of a text file (empty string if missing).'''
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return "".join(lines[-maxlines:])
    except FileNotFoundError:
        return ""
    except Exception as ex:  # pragma: no cover - defensive
        return "monitor: could not read log: %s" % ex


###############################################################################
def read_status(path):
    '''return the parsed status dict, or an empty dict if missing/unreadable.'''
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


###############################################################################
def humansize(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024.0:
            return "%.0f %s" % (n, unit) if unit == "B" else "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%.1f PB" % n


###############################################################################
def render_page(watchdir, interval, base=""):
    '''build the full HTML page for the current state of *watchdir*.

    *base* is an optional URL prefix (e.g. "/monitor") so the page can be mounted
    under a sub-path of another server.  Empty serves it at the site root.
    '''
    base = base.rstrip("/")
    logourl = (base + "/" + LOGO_NAME) if base else LOGO_NAME
    abouturl = (base + "/about") if base else "/about"
    logpath = os.path.join(watchdir, LOG_NAME)
    statuspath = os.path.join(watchdir, STATUS_NAME)

    status = read_status(statuspath)
    logtext = tail(logpath, LOG_TAIL_LINES)

    now = time.time()
    updated = status.get("updated")
    age = (now - float(updated)) if updated else None

    # decide whether processing looks live (status or log changed recently)
    last_activity = None
    for p in (statuspath, logpath):
        try:
            last_activity = max(last_activity or 0, os.path.getmtime(p))
        except OSError:
            pass
    live = last_activity is not None and (now - last_activity) < max(10, interval * 3)

    state = str(status.get("state", "idle"))
    if not live and state in ("loading", "processing"):
        state = "stalled?"

    badge_colour = {
        "loading": "#0a84ff", "processing": "#0a84ff",
        "loaded": "#30d158", "done": "#30d158",
        "error": "#ff453a", "stalled?": "#ff9f0a",
    }.get(state, "#8e8e93")

    progress = status.get("progress")
    try:
        pct = max(0.0, min(1.0, float(progress))) * 100.0
    except (TypeError, ValueError):
        pct = None

    # build the status detail rows
    rows = []

    def row(label, value):
        if value in (None, ""):
            return
        rows.append("<tr><th>%s</th><td>%s</td></tr>" % (html.escape(str(label)), html.escape(str(value))))

    row("File", status.get("file"))
    row("Job", status.get("job"))
    if status.get("pings") is not None and status.get("recordcount"):
        row("Pings", "%s / %s" % (status.get("pings"), status.get("recordcount")))
    elif status.get("pings") is not None:
        row("Pings", status.get("pings"))
    row("EPSG", status.get("epsg"))
    if status.get("elapsed") is not None:
        try:
            row("Elapsed", "%.1f s" % float(status.get("elapsed")))
        except (TypeError, ValueError):
            pass
    row("GeoTIFF", status.get("geotiff"))
    row("Point cloud CSV", status.get("pointcloud_csv"))
    if status.get("message"):
        row("Message", status.get("message"))
    if age is not None:
        row("Status updated", "%.0f s ago" % age)

    try:
        logsize = humansize(os.path.getsize(logpath))
    except OSError:
        logsize = "no log yet"

    progressbar = ""
    if pct is not None:
        progressbar = (
            '<div class="bar"><div class="fill" style="width:%.1f%%"></div>'
            '<span class="pct">%.1f%%</span></div>' % (pct, pct)
        )

    statustable = "<table class='status'>%s</table>" % "".join(rows) if rows else \
        "<p class='dim'>No status reported yet. pyall writes <code>pyall_status.json</code> " \
        "into this folder once it starts processing a file &mdash; it will appear here automatically.</p>"

    logblock = html.escape(logtext) if logtext else "(no log output yet)"

    return """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{interval}">
<title>pyall monitor - {state}</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; font-family: -apple-system, Segoe UI, Roboto, Arial, sans-serif;
         background:#1c1c1e; color:#e5e5ea; }}
  header {{ padding:14px 20px; background:#2c2c2e; border-bottom:1px solid #3a3a3c;
            display:flex; align-items:center; gap:14px; flex-wrap:wrap; }}
  .logo {{ height:30px; width:auto; display:block; }}
  h1 {{ font-size:16px; margin:0; font-weight:600; }}
  .badge {{ padding:3px 12px; border-radius:999px; font-size:12px; font-weight:700;
            text-transform:uppercase; letter-spacing:.04em; color:#000; background:{badge}; }}
  .dim {{ color:#8e8e93; }}
  .path {{ font-family: ui-monospace, Consolas, monospace; font-size:12px; color:#aeaeb2; }}
  main {{ padding:20px; max-width:1100px; margin:0 auto; }}
  .card {{ background:#2c2c2e; border:1px solid #3a3a3c; border-radius:10px;
           padding:16px 18px; margin-bottom:18px; }}
  table.status {{ width:100%; border-collapse:collapse; }}
  table.status th {{ text-align:left; color:#8e8e93; font-weight:500; padding:4px 12px 4px 0;
                     white-space:nowrap; vertical-align:top; width:160px; }}
  table.status td {{ padding:4px 0; font-family: ui-monospace, Consolas, monospace;
                     font-size:13px; word-break:break-all; }}
  .bar {{ position:relative; height:22px; background:#3a3a3c; border-radius:6px;
          overflow:hidden; margin-top:12px; }}
  .fill {{ height:100%; background:linear-gradient(90deg,#0a84ff,#30d158); transition:width .3s; }}
  .pct {{ position:absolute; inset:0; display:flex; align-items:center; justify-content:center;
          font-size:12px; font-weight:700; }}
  h2 {{ font-size:13px; text-transform:uppercase; letter-spacing:.05em; color:#8e8e93; margin:0 0 10px; }}
  pre {{ margin:0; max-height:60vh; overflow:auto; background:#0c0c0d; border-radius:8px;
         padding:14px; font-family: ui-monospace, Consolas, monospace; font-size:12.5px;
         line-height:1.45; white-space:pre-wrap; word-break:break-word; }}
  footer {{ text-align:center; color:#636366; font-size:12px; padding:0 0 24px; }}
  a.about {{ margin-left:auto; color:#0a84ff; text-decoration:none; font-size:13px;
            border:1px solid #0a84ff; padding:5px 12px; border-radius:8px; }}
  a.about:hover {{ background:#0a84ff; color:#fff; }}
</style>
</head>
<body>
<header>
  <img class="logo" src="{logoname}" alt="REACH" onerror="this.style.display='none'">
  <h1>pyall monitor</h1>
  <span class="badge">{state}</span>
  <span class="path">v{version}</span>
  <span class="path">{watchdir}</span>
  <a class="about" href="{abouturl}">About / Help</a>
</header>
<main>
  <div class="card">
    <h2>Status</h2>
    {statustable}
    {progressbar}
  </div>
  <div class="card">
    <h2>Log &mdash; {logname} ({logsize})</h2>
    <pre id="log">{logblock}</pre>
  </div>
</main>
<footer>auto-refreshing every {interval}s &middot; {clock}</footer>
<script>
  // keep the log scrolled to the newest line after each refresh
  var el = document.getElementById('log');
  if (el) {{ el.scrollTop = el.scrollHeight; }}
</script>
</body>
</html>""".format(
        interval=interval,
        state=html.escape(state),
        badge=badge_colour,
        version=html.escape(pyall_version()),
        watchdir=html.escape(watchdir),
        statustable=statustable,
        progressbar=progressbar,
        logname=html.escape(LOG_NAME),
        logsize=html.escape(str(logsize)),
        logblock=logblock,
        logoname=html.escape(logourl),
        abouturl=html.escape(abouturl),
        clock=time.strftime("%Y-%m-%d %H:%M:%S"),
    )


###############################################################################
def _render_inline(text):
    '''render inline markdown (already HTML-escaped) - code, bold, italic and links.'''
    text = re.sub(r'`([^`]+)`', lambda m: "<code>%s</code>" % m.group(1), text)
    text = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'\[([^\]]+)\]\(([^)\s]+)\)',
                  r'<a href="\2" target="_blank" rel="noopener">\1</a>', text)
    text = re.sub(r'(?<!\*)\*([^*]+)\*(?!\*)', r'<em>\1</em>', text)
    return text


###############################################################################
def _is_table_separator(line):
    '''True for a GitHub table separator row such as "| --- | :--: |".'''
    s = line.strip()
    return "-" in s and bool(re.match(r'^\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?$', s))


###############################################################################
def markdown_to_html(md):
    '''convert a useful subset of Markdown (headings, code fences, lists, tables, rules,
    inline code/bold/italic/links) to HTML.  Deliberately dependency free.'''
    lines = md.replace("\r\n", "\n").split("\n")
    out = []
    para = []
    liststack = []
    i, n = 0, len(lines)

    def flush_para():
        if para:
            out.append("<p>%s</p>" % _render_inline(html.escape(" ".join(para))))
            para.clear()

    def close_lists():
        while liststack:
            out.append("</%s>" % liststack.pop())

    while i < n:
        line = lines[i]
        stripped = line.strip()

        # fenced code block
        if stripped.startswith("```"):
            flush_para(); close_lists()
            code = []
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                code.append(lines[i]); i += 1
            i += 1  # consume closing fence
            out.append("<pre><code>%s</code></pre>" % html.escape("\n".join(code)))
            continue

        # blank line ends paragraphs / lists
        if stripped == "":
            flush_para(); close_lists()
            i += 1
            continue

        # horizontal rule
        if re.match(r'^(-{3,}|\*{3,}|_{3,})$', stripped):
            flush_para(); close_lists()
            out.append("<hr>")
            i += 1
            continue

        # heading
        m = re.match(r'^(#{1,6})\s+(.*)$', stripped)
        if m:
            flush_para(); close_lists()
            level = len(m.group(1))
            out.append("<h%d>%s</h%d>" % (level, _render_inline(html.escape(m.group(2).strip())), level))
            i += 1
            continue

        # table (header row followed by a separator row)
        if "|" in stripped and i + 1 < n and _is_table_separator(lines[i + 1]):
            flush_para(); close_lists()
            headers = [c.strip() for c in stripped.strip().strip("|").split("|")]
            i += 2
            body = []
            while i < n and "|" in lines[i] and lines[i].strip():
                body.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            thead = "".join("<th>%s</th>" % _render_inline(html.escape(h)) for h in headers)
            rows = "".join(
                "<tr>%s</tr>" % "".join("<td>%s</td>" % _render_inline(html.escape(c)) for c in r)
                for r in body)
            out.append("<table class='md'><thead><tr>%s</tr></thead><tbody>%s</tbody></table>" % (thead, rows))
            continue

        # list item (unordered or ordered) - single level
        m = re.match(r'^(\s*)([-*+]|\d+\.)\s+(.*)$', line)
        if m:
            flush_para()
            listtype = "ol" if m.group(2)[0].isdigit() else "ul"
            if not liststack or liststack[-1] != listtype:
                close_lists()
                out.append("<%s>" % listtype)
                liststack.append(listtype)
            out.append("<li>%s</li>" % _render_inline(html.escape(m.group(3).strip())))
            i += 1
            continue

        # ordinary paragraph text
        para.append(stripped)
        i += 1

    flush_para(); close_lists()
    return "\n".join(out)


###############################################################################
def render_about_page(base=""):
    '''render README.MD into a styled HTML help/about page for the monitor.

    *base* is an optional URL prefix (e.g. "/monitor") so links resolve correctly
    when the page is mounted under a sub-path of another server.
    '''
    base = base.rstrip("/")
    logourl = (base + "/" + LOGO_NAME) if base else LOGO_NAME
    homeurl = base if base else "/"
    readmepath = os.path.join(SCRIPT_DIR, README_NAME)
    try:
        with open(readmepath, "r", encoding="utf-8") as f:
            body = markdown_to_html(f.read())
    except OSError:
        body = "<p class='dim'>README.MD was not found next to the monitor.</p>"

    return """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>pyall monitor - about &amp; help</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; font-family: -apple-system, Segoe UI, Roboto, Arial, sans-serif;
         background:#1c1c1e; color:#e5e5ea; }}
  header {{ padding:14px 20px; background:#2c2c2e; border-bottom:1px solid #3a3a3c;
            display:flex; align-items:center; gap:14px; flex-wrap:wrap; }}
  .logo {{ height:30px; width:auto; display:block; }}
  h1 {{ font-size:16px; margin:0; font-weight:600; }}
  a.back {{ margin-left:auto; color:#0a84ff; text-decoration:none; font-size:13px;
            border:1px solid #0a84ff; padding:5px 12px; border-radius:8px; }}
  a.back:hover {{ background:#0a84ff; color:#fff; }}
  main {{ padding:20px; max-width:900px; margin:0 auto; line-height:1.55; }}
  .card {{ background:#2c2c2e; border:1px solid #3a3a3c; border-radius:10px; padding:8px 22px 22px; }}
  h2 {{ font-size:20px; margin:26px 0 10px; border-bottom:1px solid #3a3a3c; padding-bottom:6px; }}
  h3 {{ font-size:16px; margin:20px 0 8px; }}
  a {{ color:#0a84ff; }}
  code {{ background:#0c0c0d; padding:2px 6px; border-radius:5px;
          font-family: ui-monospace, Consolas, monospace; font-size:90%; }}
  pre {{ background:#0c0c0d; border-radius:8px; padding:14px; overflow:auto;
         font-family: ui-monospace, Consolas, monospace; font-size:12.5px; line-height:1.45; }}
  pre code {{ background:none; padding:0; }}
  table.md {{ border-collapse:collapse; width:100%; margin:12px 0; font-size:13px; }}
  table.md th, table.md td {{ border:1px solid #3a3a3c; padding:6px 10px; text-align:left; }}
  table.md th {{ background:#3a3a3c; }}
  hr {{ border:none; border-top:1px solid #3a3a3c; margin:24px 0; }}
  footer {{ text-align:center; color:#636366; font-size:12px; padding:24px 0; }}
</style>
</head>
<body>
<header>
  <img class="logo" src="{logoname}" alt="REACH" onerror="this.style.display='none'">
  <h1>pyall monitor &mdash; about &amp; help</h1>
  <a class="back" href="{homeurl}">&larr; back to monitor</a>
</header>
<main>
  <div class="card">
    {body}
  </div>
</main>
<footer>pyall v{version}</footer>
</body>
</html>""".format(
        logoname=html.escape(logourl),
        homeurl=html.escape(homeurl),
        body=body,
        version=html.escape(pyall_version()),
    )


###############################################################################
class MonitorHandler(BaseHTTPRequestHandler):
    # set by the server factory below
    watchdir = "."
    interval = 3

    def log_message(self, *args):
        # keep the console quiet; the page itself is the output
        pass

    def _send(self, body, content_type="text/html; charset=utf-8", status=200):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(render_page(self.watchdir, self.interval))
        elif path in ("/about", "/about.html", "/help"):
            self._send(render_about_page())
        elif path == "/log":
            self._send(tail(os.path.join(self.watchdir, LOG_NAME), LOG_TAIL_LINES),
                       content_type="text/plain; charset=utf-8")
        elif path == "/status.json":
            self._send(json.dumps(read_status(os.path.join(self.watchdir, STATUS_NAME))),
                       content_type="application/json")
        elif path == "/" + LOGO_NAME:
            try:
                with open(LOGO_PATH, "rb") as f:
                    self._send(f.read(), content_type="image/png")
            except OSError:
                self._send("not found", status=404, content_type="text/plain; charset=utf-8")
        else:
            self._send("not found", status=404, content_type="text/plain; charset=utf-8")


###############################################################################
def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Serve a web page showing the live status and log of pyall processing.")
    parser.add_argument("--dir", default="",
                        help="Folder to watch (the one containing pyall.log / pyall_status.json). "
                             "Default: the shared log folder (PYALL_LOG_DIR or a 'logs' folder next to this script).")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Interface to bind (default 127.0.0.1; use 0.0.0.0 for other machines).")
    parser.add_argument("--port", type=int, default=8770, help="TCP port (default 8770).")
    parser.add_argument("--interval", type=int, default=3,
                        help="Page auto-refresh interval in seconds (default 3).")
    args = parser.parse_args(argv)

    watchdir = os.path.abspath(args.dir) if args.dir else find_watch_dir(os.getcwd())

    MonitorHandler.watchdir = watchdir
    MonitorHandler.interval = max(1, int(args.interval))

    server = ThreadingHTTPServer((args.host, args.port), MonitorHandler)
    url = "http://%s:%d/" % ("127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host, args.port)
    print("pyall monitor serving %s" % url)
    print("Watching folder: %s" % watchdir)
    print("Auto-refresh every %ds.  Press Ctrl+C to stop." % MonitorHandler.interval)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping monitor.")
    finally:
        server.server_close()


###############################################################################
if __name__ == "__main__":
    main()
