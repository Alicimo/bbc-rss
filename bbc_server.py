"""Small HTTP feed service with a single background refresh worker."""

from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import signal
import threading
from urllib.parse import urlsplit

from bbc_rss import load_history, refresh

LOG = logging.getLogger(__name__)


class FeedService:
    def __init__(self, output: Path, *, interval=900, timeout=30, feed_url=None, ttl=15):
        self.output = output
        self.interval = interval
        self.options = dict(timeout=timeout, feed_url=feed_url, ttl=ttl)
        self.lock = threading.Lock()
        try:
            self.content = output.read_bytes()
        except FileNotFoundError:
            self.content = None
        if self.content is not None:
            load_history(self.content)  # Fail startup rather than serving corrupt history.
        self.last_attempt = None
        self.last_success = None
        self.last_error = None
        self.refreshing = False

    def update(self):
        with self.lock:
            self.last_attempt = datetime.now(timezone.utc)
            self.refreshing = True
        try:
            content = refresh(self.output, **self.options)
        except Exception as error:
            # A bad response or unexpected parser failure must not kill the scheduler.
            LOG.exception("Refresh failed; keeping the last successful feed")
            with self.lock:
                self.last_error = str(error)
        else:
            with self.lock:
                self.content = content
                self.last_success = datetime.now(timezone.utc)
                self.last_error = None
        finally:
            with self.lock:
                self.refreshing = False

    def run(self, stop: threading.Event):
        while not stop.is_set():
            self.update()
            if stop.wait(self.interval):
                break

    def snapshot(self):
        with self.lock:
            now = datetime.now(timezone.utc)
            healthy = (
                self.last_success is not None and self.last_error is None
                and (now - self.last_success).total_seconds() <= self.interval + 3 * self.options['timeout'] + 60
            )
            return self.content, {
                "healthy": healthy,
                "has_feed": self.content is not None,
                "refreshing": self.refreshing,
                "last_attempt": self.last_attempt.isoformat() if self.last_attempt else None,
                "last_success": self.last_success.isoformat() if self.last_success else None,
                "last_error": self.last_error,
                "interval_seconds": self.interval,
            }


def make_server(service: FeedService, host: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "BBCMostReadRSS/0.1"

        def do_GET(self):
            self.respond()

        def do_HEAD(self):
            self.respond(head=True)

        def respond(self, head=False):
            path = urlsplit(self.path).path
            if path == "/":
                self.send_response(302)
                self.send_header("Location", "/feed.xml")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            content, status = service.snapshot()
            headers = {"Cache-Control": "no-store"}
            if path == "/healthz":
                body = json.dumps(status).encode()
                code, mime = (200 if status['healthy'] else 503), "application/json; charset=utf-8"
            elif path == "/feed.xml" and content is not None:
                etag = '"' + hashlib.sha256(content).hexdigest() + '"'
                headers = {"ETag": etag, "Cache-Control": "no-cache"}
                candidates = [value.strip().removeprefix('W/') for value in self.headers.get('If-None-Match', '').split(',')]
                if etag in candidates or '*' in candidates:
                    self.send_response(304)
                    for key, value in headers.items():
                        self.send_header(key, value)
                    self.end_headers()
                    return
                body, code, mime = content, 200, "application/rss+xml; charset=utf-8"
            elif path == "/feed.xml":
                body, code, mime = b"Feed not ready; retry shortly.\n", 503, "text/plain; charset=utf-8"
                headers["Retry-After"] = "60"
            else:
                body, code, mime = b"Not found\n", 404, "text/plain; charset=utf-8"
            self.send_response(code)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            if not head:
                self.wfile.write(body)

        def log_message(self, fmt, *args):
            LOG.info("%s %s", self.client_address[0], fmt % args)

    return ThreadingHTTPServer((host, port), Handler)


def serve(args):
    service = FeedService(args.output, interval=args.interval, timeout=args.timeout, feed_url=args.feed_url, ttl=args.ttl)
    stop = threading.Event()

    def terminate(signum, frame):
        raise KeyboardInterrupt

    with make_server(service, args.host, args.port) as server:
        previous_handler = signal.signal(signal.SIGTERM, terminate)
        worker = threading.Thread(target=service.run, args=(stop,), name="feed-updater", daemon=True)
        try:
            worker.start()
            LOG.info("Serving http://%s:%s/feed.xml; refreshing every %s seconds", args.host, args.port, args.interval)
            server.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            LOG.info("Stopping feed server")
        finally:
            stop.set()
            worker.join(timeout=5)
            signal.signal(signal.SIGTERM, previous_handler)
