#!/usr/bin/env python3
"""
Dropzone - share files between devices on the same Wi-Fi, through a browser.

Run:   python server.py
Open the printed address on any device on the same network.

Options:
  --port 8000      port to listen on
  --dir shared     folder where files are stored
  --pin 1234       require a PIN (the PIN is included in the printed link)

No dependencies. Python 3.8+.
"""
import argparse
import json
import os
import re
import secrets
import socket
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CHUNK = 4 * 1024 * 1024  # 4 MB: large chunks keep Python out of the way on fast links

args = None
STORE = None


# ---------------------------------------------------------------- helpers
def lan_ip():
    """Best guess at this machine's LAN address."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packet is actually sent
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def clean_name(raw):
    name = os.path.basename(urllib.parse.unquote(raw).replace("\\", "/"))
    name = re.sub(r"[\x00-\x1f<>:\"|?*]", "_", name).strip(" .")
    return name or "file"


def unique_path(name):
    p = STORE / name
    if not p.exists():
        return p
    stem, suffix = p.stem, p.suffix
    i = 1
    while True:
        p = STORE / f"{stem} ({i}){suffix}"
        if not p.exists():
            return p
        i += 1


def list_files():
    out = []
    for p in STORE.iterdir():
        if p.is_file() and not p.name.endswith(".part"):
            st = p.stat()
            out.append({"name": p.name, "size": st.st_size, "time": st.st_mtime})
    out.sort(key=lambda f: f["time"], reverse=True)
    return out


# ---------------------------------------------------------------- handler
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive
    server_version = "Dropzone"

    def log_message(self, *a):
        pass

    # --- plumbing
    def send_bytes(self, code, body, ctype="text/plain; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, code, obj):
        self.send_bytes(code, json.dumps(obj).encode(), "application/json")

    def authorized(self):
        if not args.pin:
            return True
        cookie = self.headers.get("Cookie", "")
        return f"dz_pin={args.pin}" in cookie

    def deny(self):
        page = (b"<meta name=viewport content='width=device-width'>"
                b"<body style='font:18px system-ui;padding:2rem'>"
                b"This Dropzone needs a PIN. Open the full link shown in the terminal.")
        self.send_bytes(401, page, "text/html; charset=utf-8")

    # --- routes
    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query)

        if url.path == "/" and args.pin and query.get("pin", [""])[0] == args.pin:
            self.send_response(303)
            self.send_header("Set-Cookie", f"dz_pin={args.pin}; Path=/; SameSite=Lax")
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if not self.authorized():
            return self.deny()

        if url.path == "/":
            return self.send_bytes(200, PAGE.encode(), "text/html; charset=utf-8")
        if url.path == "/api/files":
            return self.send_json(200, list_files())
        if url.path.startswith("/d/"):
            return self.download(clean_name(url.path[3:]))
        self.send_bytes(404, b"Not found")

    def download(self, name):
        path = STORE / name
        if not path.is_file():
            return self.send_bytes(404, b"File not found")
        size = path.stat().st_size
        start, end, code = 0, size - 1, 200

        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip())
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else size - 1
                else:  # suffix range: last N bytes
                    start = max(0, size - int(m.group(2)))
                if start >= size or start > end:
                    return self.send_bytes(416, b"", extra={"Content-Range": f"bytes */{size}"})
                end = min(end, size - 1)
                code = 206

        self.send_response(code)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Disposition",
                         "attachment; filename*=UTF-8''" + urllib.parse.quote(name))
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()

        remaining = end - start + 1
        try:
            with open(path, "rb", buffering=0) as f:
                f.seek(start)
                while remaining > 0:
                    buf = f.read(min(CHUNK, remaining))
                    if not buf:
                        break
                    self.wfile.write(buf)
                    remaining -= len(buf)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True

    def do_PUT(self):
        if not self.authorized():
            return self.deny()
        url = urllib.parse.urlsplit(self.path)
        if url.path != "/api/upload":
            return self.send_bytes(404, b"Not found")

        name = clean_name(urllib.parse.parse_qs(url.query).get("name", ["file"])[0])
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self.send_bytes(411, b"Content-Length required")

        dest = unique_path(name)
        part = dest.with_name(dest.name + ".part")
        remaining = length
        try:
            # Stream straight to disk: memory use stays flat even for 50 GB files.
            with open(part, "wb", buffering=0) as f:
                while remaining > 0:
                    buf = self.rfile.read(min(CHUNK, remaining))
                    if not buf:
                        raise ConnectionError("client disconnected")
                    f.write(buf)
                    remaining -= len(buf)
            os.replace(part, dest)
        except (ConnectionError, OSError):
            try:
                part.unlink()
            except OSError:
                pass
            self.close_connection = True
            return
        self.send_json(200, {"name": dest.name, "size": length})

    def do_DELETE(self):
        if not self.authorized():
            return self.deny()
        path = urllib.parse.urlsplit(self.path).path
        if not path.startswith("/api/files/"):
            return self.send_bytes(404, b"Not found")
        target = STORE / clean_name(path[len("/api/files/"):])
        try:
            target.unlink()
        except OSError:
            return self.send_bytes(404, b"File not found")
        self.send_json(200, {"ok": True})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64
    allow_reuse_address = True


# ---------------------------------------------------------------- web UI
PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dropzone</title>
<style>
  :root {
    --bg: #e8edf1; --surface: #ffffff; --ink: #13202b; --muted: #5d6b78;
    --line: #d3dbe2; --accent: #0b63c9; --accent-ink: #ffffff; --ok: #1b8556; --bad: #c2342b;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #10171d; --surface: #18222b; --ink: #e8eef3; --muted: #8b9aa7;
      --line: #2a3742; --accent: #4c9bf5; --accent-ink: #07121d; --ok: #4cc38a; --bad: #ff7a70;
    }
  }
  * { box-sizing: border-box; }
  html { color-scheme: light dark; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font: 16px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    padding: max(16px, env(safe-area-inset-top)) 16px max(24px, env(safe-area-inset-bottom));
  }
  main { max-width: 720px; margin: 0 auto; display: grid; gap: 16px; }
  h1 { font-size: 1.25rem; margin: 8px 0 0; letter-spacing: -.01em; }
  .sub { color: var(--muted); margin: 0; font-size: .9rem; }

  .drop {
    display: grid; place-items: center; text-align: center; gap: 6px;
    min-height: 200px; padding: 24px; cursor: pointer;
    background: var(--surface); border: 2px dashed var(--line); border-radius: 18px;
    transition: border-color .15s, background .15s;
  }
  .drop:hover, .drop:focus-visible, .drop.over { border-color: var(--accent); outline: none; }
  .drop.over { background: color-mix(in srgb, var(--accent) 8%, var(--surface)); }
  .drop strong { font-size: 1.35rem; letter-spacing: -.01em; }
  .drop span { color: var(--muted); }
  .speed {
    font-variant-numeric: tabular-nums; font-size: 2.6rem; font-weight: 650;
    letter-spacing: -.03em; color: var(--accent); line-height: 1;
  }
  .speed small { font-size: 1rem; font-weight: 500; color: var(--muted); letter-spacing: 0; }
  .idle .speed { display: none; }
  .busy strong, .busy > span { display: none; }

  section { background: var(--surface); border-radius: 18px; padding: 6px 0; overflow: hidden; }
  h2 { font-size: .95rem; margin: 0; padding: 12px 18px 6px; color: var(--muted); font-weight: 600; }
  ul { list-style: none; margin: 0; padding: 0; }
  li { display: grid; grid-template-columns: 1fr auto; gap: 2px 12px; align-items: center;
       padding: 12px 18px; border-top: 1px solid var(--line); }
  li:first-child { border-top: 0; }
  .name { overflow-wrap: anywhere; font-weight: 550; }
  .meta { color: var(--muted); font-size: .85rem; font-variant-numeric: tabular-nums; grid-column: 1; }
  .actions { grid-column: 2; grid-row: 1 / span 2; display: flex; gap: 8px; }
  a.btn, button.btn {
    font: inherit; font-size: .9rem; font-weight: 600; text-decoration: none; cursor: pointer;
    padding: 8px 14px; border-radius: 10px; border: 1px solid var(--line);
    background: transparent; color: var(--ink);
  }
  a.btn.primary { background: var(--accent); border-color: var(--accent); color: var(--accent-ink); }
  button.btn.del { color: var(--bad); }
  a.btn:focus-visible, button.btn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .bar { grid-column: 1 / -1; height: 6px; border-radius: 3px; background: var(--line); overflow: hidden; margin-top: 6px; }
  .bar i { display: block; height: 100%; width: 0; background: var(--accent); }
  .done .bar i { background: var(--ok); }
  .failed .meta { color: var(--bad); }
  .empty { padding: 18px; color: var(--muted); }
</style>
</head>
<body>
<main>
  <div>
    <h1>Dropzone</h1>
    <p class="sub">Anything you add here shows up on every device on this Wi-Fi.</p>
  </div>

  <div class="drop idle" id="drop" tabindex="0" role="button" aria-label="Choose files to send">
    <strong>Drop files here, or tap to choose</strong>
    <span>Any size. Files go straight to the host computer.</span>
    <div class="speed" id="speed">0 <small>MB/s</small></div>
    <span id="status" style="display:block"></span>
  </div>
  <input type="file" id="picker" multiple hidden>

  <section id="sending" hidden>
    <h2>Sending</h2>
    <ul id="uploads"></ul>
  </section>

  <section>
    <h2>Shared files</h2>
    <ul id="files"></ul>
  </section>
</main>

<script>
const $ = s => document.querySelector(s);
const drop = $('#drop'), picker = $('#picker');
const MAX_PARALLEL = 3;
let queue = [], active = 0, totalSpeed = new Map();

function fmtSize(n) {
  const u = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (n >= 100 || i === 0 ? n.toFixed(0) : n.toFixed(1)) + ' ' + u[i];
}
function fmtTime(s) {
  if (!isFinite(s)) return '';
  if (s < 60) return Math.ceil(s) + 's left';
  if (s < 3600) return Math.floor(s / 60) + 'm ' + Math.ceil(s % 60) + 's left';
  return Math.floor(s / 3600) + 'h ' + Math.floor(s % 3600 / 60) + 'm left';
}
function ago(t) {
  const d = Date.now() / 1000 - t;
  if (d < 60) return 'just now';
  if (d < 3600) return Math.floor(d / 60) + ' min ago';
  if (d < 86400) return Math.floor(d / 3600) + ' h ago';
  return new Date(t * 1000).toLocaleDateString();
}
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

// ---------- picking / dropping
drop.addEventListener('click', () => picker.click());
drop.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); picker.click(); } });
picker.addEventListener('change', () => { add(picker.files); picker.value = ''; });
['dragenter', 'dragover'].forEach(ev => window.addEventListener(ev, e => { e.preventDefault(); drop.classList.add('over'); }));
['dragleave', 'drop'].forEach(ev => window.addEventListener(ev, e => { e.preventDefault(); if (ev === 'drop' || e.target === document.documentElement) drop.classList.remove('over'); }));
window.addEventListener('drop', e => { if (e.dataTransfer.files.length) add(e.dataTransfer.files); });

// ---------- uploading
function add(files) {
  for (const file of files) {
    const li = el('li');
    const name = el('div', 'name', file.name);
    const meta = el('div', 'meta', 'Waiting · ' + fmtSize(file.size));
    const bar = el('div', 'bar'); const fill = el('i'); bar.append(fill);
    li.append(name, meta, bar);
    $('#uploads').prepend(li);
    queue.push({ file, li, meta, fill });
  }
  $('#sending').hidden = false;
  pump();
}
function pump() {
  while (active < MAX_PARALLEL && queue.length) {
    const job = queue.shift();
    active++;
    send(job).finally(() => { active--; totalSpeed.delete(job); updateSpeed(); pump(); loadFiles(); });
  }
  drop.classList.toggle('busy', active > 0);
  drop.classList.toggle('idle', active === 0);
  $('#status').textContent = active ? (active + queue.length) + ' file' + (active + queue.length > 1 ? 's' : '') + ' sending' : '';
  $('#status').style.display = active ? 'block' : 'none';
}
function updateSpeed() {
  let sum = 0; for (const v of totalSpeed.values()) sum += v;
  const mb = sum / 1048576;
  $('#speed').innerHTML = (mb >= 10 ? mb.toFixed(0) : mb.toFixed(1)) + ' <small>MB/s</small>';
}
function send(job) {
  const { file, li, meta, fill } = job;
  return new Promise(resolve => {
    const x = new XMLHttpRequest();
    x.open('PUT', '/api/upload?name=' + encodeURIComponent(file.name));
    let t0 = performance.now(), lastT = t0, lastB = 0, rate = 0;
    x.upload.onprogress = e => {
      const now = performance.now();
      if (now - lastT >= 400) {
        const inst = (e.loaded - lastB) / ((now - lastT) / 1000);
        rate = rate ? rate * 0.6 + inst * 0.4 : inst;
        lastT = now; lastB = e.loaded;
        totalSpeed.set(job, rate); updateSpeed();
      }
      const pct = e.total ? e.loaded / e.total * 100 : 0;
      fill.style.width = pct + '%';
      meta.textContent = fmtSize(e.loaded) + ' of ' + fmtSize(e.total) +
        (rate ? ' · ' + fmtTime((e.total - e.loaded) / rate) : '');
    };
    x.onload = () => {
      if (x.status === 200) {
        const secs = (performance.now() - t0) / 1000;
        fill.style.width = '100%'; li.classList.add('done');
        meta.textContent = 'Sent · ' + fmtSize(file.size) + ' in ' + secs.toFixed(1) + 's (' + fmtSize(file.size / secs) + '/s avg)';
      } else fail('Failed (' + x.status + ')');
      resolve();
    };
    x.onerror = () => { fail('Connection lost. Check the Wi-Fi and try again.'); resolve(); };
    function fail(msg) { li.classList.add('failed'); meta.textContent = msg; }
    meta.textContent = 'Starting…';
    x.send(file);  // browser streams the file from disk; nothing is loaded into memory
  });
}

// ---------- file list
let lastJson = '';
async function loadFiles() {
  try {
    const r = await fetch('/api/files', { cache: 'no-store' });
    const list = await r.json();
    const j = JSON.stringify(list);
    if (j === lastJson) return;
    lastJson = j;
    const ul = $('#files'); ul.textContent = '';
    if (!list.length) { ul.append(el('li', 'empty', 'Nothing here yet. Send a file from any device.')); return; }
    for (const f of list) {
      const li = el('li');
      li.append(el('div', 'name', f.name), el('div', 'meta', fmtSize(f.size) + ' · ' + ago(f.time)));
      const act = el('div', 'actions');
      const a = el('a', 'btn primary', 'Download');
      a.href = '/d/' + encodeURIComponent(f.name); a.download = f.name;
      const d = el('button', 'btn del', 'Delete');
      d.onclick = async () => {
        if (!confirm('Delete ' + f.name + '?')) return;
        await fetch('/api/files/' + encodeURIComponent(f.name), { method: 'DELETE' });
        lastJson = ''; loadFiles();
      };
      act.append(a, d); li.append(act); ul.append(li);
    }
  } catch (e) { /* host unreachable; try again on next tick */ }
}
loadFiles(); setInterval(loadFiles, 2000);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------- main
def main():
    global args, STORE
    ap = argparse.ArgumentParser(description="Share files over local Wi-Fi via the browser.")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--dir", default="shared")
    ap.add_argument("--pin", nargs="?", const="auto", default=None,
                    help="require a PIN; give a value or leave empty for a random one")
    args = ap.parse_args()
    if args.pin == "auto":
        args.pin = "".join(secrets.choice("0123456789") for _ in range(4))

    STORE = Path(args.dir).resolve()
    STORE.mkdir(parents=True, exist_ok=True)
    for stale in STORE.glob("*.part"):  # leftovers from an interrupted transfer
        stale.unlink()

    try:
        server = Server(("0.0.0.0", args.port), Handler)
    except OSError as e:
        sys.exit(f"Cannot use port {args.port}: {e}. Try --port 8080")

    url = f"http://{lan_ip()}:{args.port}" + (f"/?pin={args.pin}" if args.pin else "")
    print("\n  Dropzone is running\n")
    print(f"  Open on any device on this Wi-Fi:  {url}")
    print(f"  Files are saved in:                {STORE}")
    if args.pin:
        print(f"  PIN:                               {args.pin}")
    try:  # optional: scan-to-open QR code if the 'qrcode' package is installed
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        print()
        qr.print_ascii(invert=True)
    except ImportError:
        print("\n  Tip: pip install qrcode  -> shows a QR code to scan from your phone")
    print("\n  Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")


if __name__ == "__main__":
    main()
