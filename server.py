#!/usr/bin/env python3
"""
Stockfish bridge for the Best Move Analyzer.

Run it next to analyzer.html and the extracted `stockfish` folder:
    python server.py
It starts your native Stockfish, serves the analyzer at http://localhost:8765
and pipes UCI commands between the page and the engine. Standard library only.
"""
import argparse, atexit, glob, json, os, queue, re, subprocess, sys, threading, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "analyzer.html")

# Only these UCI commands are forwarded, so a stray web page can't make the engine write files etc.
ALLOWED = re.compile(
    r"^(uci|isready|ucinewgame|stop"
    r"|position (startpos|fen [0-9a-zA-Z/ \-]+)( moves( [a-h][1-8][a-h][1-8][qrbn]?)*)?"
    r"|go (depth \d{1,2}|movetime \d{1,6}|nodes \d{1,12}|infinite)"
    r"|setoption name (MultiPV|Threads|Hash|Skill Level|UCI_ShowWDL) value \d+)$"
)


def find_engine():
    cands = []
    for pat in ("stockfish*.exe", "stockfish/**/stockfish*.exe", "**/stockfish*.exe",
                "stockfish/stockfish*", "stockfish"):
        cands += glob.glob(os.path.join(HERE, pat), recursive=True)
    for c in cands:
        if os.path.isfile(c) and not c.lower().endswith((".txt", ".md", ".zip", ".py", ".html")):
            return c
    return None


class Engine:
    def __init__(self, path, threads, hash_mb):
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen([path], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, bufsize=1,
                                     cwd=os.path.dirname(path), creationflags=flags)
        self.lock = threading.Lock()
        self.subs = set()
        self.name = "Stockfish"
        self.threads, self.hash = threads, hash_mb
        self.write("uci")
        for line in self.proc.stdout:                      # handshake before the page connects
            line = line.strip()
            if line.startswith("id name "):
                self.name = line[8:]
            if line == "uciok":
                break
        self.write(f"setoption name Threads value {threads}")
        self.write(f"setoption name Hash value {hash_mb}")
        self.write("isready")
        for line in self.proc.stdout:
            if line.strip() == "readyok":
                break
        threading.Thread(target=self._pump, daemon=True).start()
        atexit.register(self.close)

    def write(self, cmd):
        with self.lock:
            self.proc.stdin.write(cmd + "\n")
            self.proc.stdin.flush()

    def _pump(self):
        for line in self.proc.stdout:
            line = line.rstrip()
            for q in list(self.subs):
                q.put(line)
        print("Engine process exited.")
        os._exit(1)

    def subscribe(self):
        q = queue.Queue()
        self.subs.add(q)
        return q

    def unsubscribe(self, q):
        self.subs.discard(q)

    def close(self):
        try:
            self.proc.stdin.write("quit\n"); self.proc.stdin.flush()
            self.proc.wait(timeout=2)
        except Exception:
            self.proc.kill()


engine = None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _origin_ok(self):
        o = self.headers.get("Origin")
        return o is None or o == "null" or re.match(r"^http://(localhost|127\.0\.0\.1)(:\d+)?$", o)

    def _cors(self):
        o = self.headers.get("Origin")
        if o:
            self.send_header("Access-Control-Allow-Origin", o)
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        if not self._origin_ok():
            return self.send_error(403)
        self.send_response(204); self._cors(); self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html", "/analyzer.html"):
            try:
                data = open(PAGE, "rb").read()
            except OSError:
                return self.send_error(404, "analyzer.html not found next to server.py")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers(); self.wfile.write(data); return
        if not self._origin_ok():
            return self.send_error(403)
        if path == "/info":
            data = json.dumps({"name": engine.name, "threads": engine.threads, "hash": engine.hash}).encode()
            self.send_response(200); self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers(); self.wfile.write(data); return
        if path == "/events":
            self.send_response(200); self._cors()
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            q = engine.subscribe()
            try:
                self.wfile.write(b": connected\n\n"); self.wfile.flush()
                while True:
                    try:
                        line = q.get(timeout=15)
                        self.wfile.write(f"data: {line}\n\n".encode())
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
            except OSError:
                pass
            finally:
                engine.unsubscribe(q)
            return
        self.send_error(404)

    def do_POST(self):
        if urlparse(self.path).path != "/cmd" or not self._origin_ok():
            return self.send_error(403)
        n = min(int(self.headers.get("Content-Length") or 0), 4096)
        cmd = self.rfile.read(n).decode("utf-8", "ignore").strip()
        if not ALLOWED.match(cmd):
            return self.send_error(400, "command not allowed")
        engine.write(cmd)
        self.send_response(204); self._cors(); self.end_headers()


def main():
    global engine
    cpu = os.cpu_count() or 2
    ap = argparse.ArgumentParser(description="Stockfish bridge for the Best Move Analyzer")
    ap.add_argument("--engine", help="path to the Stockfish executable (auto-detected by default)")
    ap.add_argument("--threads", type=int, default=max(1, cpu - 1), help="search threads (default: cores - 1)")
    ap.add_argument("--hash", type=int, default=1024, help="hash table size in MB (default: 1024)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()

    path = a.engine or find_engine()
    if not path or not os.path.isfile(path):
        sys.exit("Could not find Stockfish. Extract the zip next to server.py, or pass --engine PATH.")
    print(f"Starting {os.path.basename(path)} ...")
    engine = Engine(path, a.threads, a.hash)
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    srv.daemon_threads = True
    url = f"http://localhost:{a.port}/"
    print(f"{engine.name} | {a.threads} threads | {a.hash} MB hash\nAnalyzer: {url}\nCtrl+C to stop.")
    if not a.no_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
