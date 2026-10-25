from contextlib import contextmanager
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import bbc_rss
from bbc_server import FeedService, make_server
from test_bbc_rss import page


@contextmanager
def running_server(service):
    with make_server(service, '127.0.0.1', 0) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01})
        thread.start()
        try:
            yield server.server_port
        finally:
            server.shutdown()
            thread.join(timeout=2)


def request(port, path='/feed.xml', *, method='GET', headers=None):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
    try:
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'feed.xml'
        self.service = FeedService(self.path)

    def update(self, source=None):
        with patch('bbc_rss.fetch_page', return_value=source or page()):
            self.service.update()

    def test_first_run_not_ready_then_valid_feed_and_health(self):
        with running_server(self.service) as port:
            self.assertEqual(request(port)[0], 503)
            self.assertEqual(request(port, '/healthz')[0], 503)
            self.update()
            status, headers, body = request(port)
            self.assertEqual(status, 200)
            self.assertEqual(headers['Content-Type'], 'application/rss+xml; charset=utf-8')
            self.assertEqual(body, self.path.read_bytes())
            status, _, health = request(port, '/healthz')
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(health)['last_success'])
            self.assertEqual(request(port, '/')[1]['Location'], '/feed.xml')

    def test_failed_refresh_keeps_feed_and_reports_degraded_health_then_recovers(self):
        self.update()
        previous = self.path.read_bytes()
        with patch('bbc_rss.fetch_page', side_effect=bbc_rss.FeedError('Unavailable')), self.assertLogs(level='ERROR'):
            self.service.update()
        with running_server(self.service) as port:
            self.assertEqual(request(port)[2], previous)
            self.assertEqual(request(port, '/healthz')[0], 503)
            self.update(page().replace('story0', 'new'))
            self.assertEqual(request(port, '/healthz')[0], 200)
            self.assertEqual(len(bbc_rss.load_history(request(port)[2])), 11)

    def test_http_is_responsive_while_refresh_waits_and_does_not_trigger_scrapes(self):
        self.update()
        entered, release = threading.Event(), threading.Event()

        def blocked_fetch(timeout):
            entered.set()
            release.wait(timeout=3)
            return page()

        with running_server(self.service) as port, patch('bbc_rss.fetch_page', side_effect=blocked_fetch) as fetch:
            worker = threading.Thread(target=self.service.update)
            worker.start()
            try:
                self.assertTrue(entered.wait(timeout=1))
                self.assertEqual(request(port)[0], 200)
                self.assertTrue(json.loads(request(port, '/healthz')[2])['refreshing'])
                self.assertEqual(fetch.call_count, 1)
            finally:
                release.set()
                worker.join(timeout=2)

    def test_etag_head_and_no_filesystem_routes(self):
        self.update()
        with running_server(self.service) as port:
            _, headers, body = request(port)
            status, _, empty = request(port, headers={'If-None-Match': headers['ETag']})
            self.assertEqual((status, empty), (304, b''))
            self.assertEqual(request(port, headers={'If-None-Match': 'W/' + headers['ETag']})[0], 304)
            status, head, empty = request(port, method='HEAD')
            self.assertEqual((status, empty), (200, b''))
            self.assertEqual(int(head['Content-Length']), len(body))
            for path in ('/bbc_rss.py', '/.git/config', '/../feed.xml', '/healthz/other'):
                self.assertEqual(request(port, path)[0], 404)

    def test_scheduler_runs_immediately_repeats_and_stops(self):
        self.service.interval = 0.01
        stop = threading.Event()
        calls = []

        def fetch(timeout):
            calls.append(timeout)
            if len(calls) == 2:
                stop.set()
            return page()

        with patch('bbc_rss.fetch_page', side_effect=fetch):
            worker = threading.Thread(target=self.service.run, args=(stop,))
            worker.start()
            worker.join(timeout=2)
            stop.set()
            self.assertFalse(worker.is_alive())
        self.assertEqual(len(calls), 2)

    def test_restart_serves_cached_history_before_network_succeeds(self):
        self.update()
        restored = FeedService(self.path)
        with running_server(restored) as port:
            self.assertEqual(request(port)[2], self.path.read_bytes())
            self.assertEqual(request(port, '/healthz')[0], 503)
        self.path.write_bytes(b'broken')
        with self.assertRaises(bbc_rss.FeedError):
            FeedService(self.path)
