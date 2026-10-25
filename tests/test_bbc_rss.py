from contextlib import redirect_stderr
from datetime import datetime, timezone
from io import BytesIO, StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
import xml.etree.ElementTree as ET

import bbc_rss


def page(*, count=10, visible=True, embedded=True):
    # Mirrors the BBC section and __NEXT_DATA__ shapes inspected on 2026-10-25.
    content = [{
        "title": f"Story {i} & more", "href": f"/news/articles/story{i}",
        "description": 'A <b>literal</b> summary & context',
        "metadata": {"firstUpdated": 1792934551371},
    } for i in range(count)]
    result = '<section><h2>Most watched</h2><a href="/news/videos/other"><h2>Other</h2></a></section>'
    if visible:
        result += '<section data-analytics_group_name="Most read"><h2>Most read</h2><ol>'
        for i in range(count):
            result += (
                f'<li><a href="/news/articles/story{i}?tracking=1#fragment">'
                f'<span>{i + 1}</span><h2 data-testid="card-headline">Story {i} &amp; more</h2>'
                '</a></li>'
            )
        result += '</ol></section>'
    if embedded:
        data = {"props": {"pageProps": {"sections": [
            {"title": "Most watched", "content": []},
            {"title": "Most read", "content": content},
        ]}}}
        result += '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(data) + '</script>'
    return result


class ParsingTests(unittest.TestCase):
    def test_ranking_scope_canonical_links_and_metadata(self):
        stories = bbc_rss.parse_most_read(page())
        self.assertEqual([s.title for s in stories], [f"Story {i} & more" for i in range(10)])
        self.assertEqual(stories[0].url, "https://www.bbc.com/news/articles/story0")
        self.assertIn("summary", stories[0].summary)
        self.assertEqual(stories[0].published, datetime.fromtimestamp(1792934551.371, timezone.utc))

    def test_visible_order_wins_over_embedded_order(self):
        source = page().replace('href="/news/articles/story0?tracking=1#fragment"', 'href="/news/articles/swapped?tracking=1"')
        stories = bbc_rss.parse_most_read(source)
        self.assertTrue(stories[0].url.endswith("/swapped"))
        self.assertIsNone(stories[0].published)

    def test_fallbacks(self):
        for options in ({"embedded": False}, {"visible": False}):
            with self.subTest(options=options):
                self.assertEqual(len(bbc_rss.parse_most_read(page(**options))), 10)
        heading_only = page(embedded=False).replace(' data-analytics_group_name="Most read"', '')
        self.assertEqual(len(bbc_rss.parse_most_read(heading_only)), 10)

    def test_reject_missing_partial_duplicate_and_ambiguous_lists(self):
        duplicate = page(embedded=False).replace('/news/articles/story1?', '/news/articles/story0?')
        for source in ('<h1>Access denied</h1>', page(count=9), page(count=11), duplicate, page() + page()):
            with self.subTest(source=source[:70]):
                with self.assertRaises(bbc_rss.FeedError):
                    bbc_rss.parse_most_read(source)

    def test_reject_unexpected_urls(self):
        for href in ('https://evil.example/news/articles/test', 'javascript:alert(1)', '/sport/article', '//www.bbc.com.evil/news/a'):
            with self.subTest(href=href), self.assertRaises(bbc_rss.FeedError):
                bbc_rss.canonical_url(href)
        self.assertEqual(bbc_rss.canonical_url('https://www.bbc.co.uk/news/live/test'), 'https://www.bbc.com/news/live/test')

    def test_invalid_optional_timestamps_are_omitted(self):
        for value in (None, 'unknown', True, float('inf'), -1e40):
            self.assertIsNone(bbc_rss._published({"firstUpdated": value}))


class FeedTests(unittest.TestCase):
    def test_valid_xml_escaping_dates_order_and_self_link(self):
        stories = bbc_rss.parse_most_read(page())
        xml = bbc_rss.build_rss(stories, feed_url='https://example.org/feed.xml')
        channel = ET.fromstring(xml).find('channel')
        self.assertEqual(channel.find(f'{{{bbc_rss.ATOM}}}link').get('href'), 'https://example.org/feed.xml')
        items = channel.findall('item')
        self.assertEqual(len(items), 10)
        self.assertEqual([i.findtext('link') for i in items], [s.url for s in stories])
        self.assertEqual(items[0].findtext('title'), 'Story 0 & more')
        self.assertIn('&lt;b&gt;literal&lt;/b&gt;', items[0].findtext('description'))
        self.assertNotIn('Most read: #', xml.decode())
        self.assertTrue(items[0].findtext('pubDate').endswith('GMT'))

    def test_guid_stays_stable_when_title_changes(self):
        story = bbc_rss.Story('First title', 'https://www.bbc.com/news/articles/a')
        changed = bbc_rss.Story('New title', story.url)
        first = ET.fromstring(bbc_rss.build_rss([story])).findtext('channel/item/guid')
        second = ET.fromstring(bbc_rss.build_rss([changed])).findtext('channel/item/guid')
        self.assertEqual(first, second)
        self.assertIsNone(ET.fromstring(bbc_rss.build_rss([story])).find('channel/item/pubDate'))

    def test_failed_refresh_preserves_last_good_feed(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'feed.xml'
            previous = bbc_rss.build_rss(bbc_rss.merge_stories([], bbc_rss.parse_most_read(page())))
            bbc_rss.atomic_write(output, previous)
            with patch('bbc_rss.fetch_page', return_value=page(count=9)):
                with self.assertLogs(level='ERROR'):
                    self.assertEqual(bbc_rss.main(['--output', str(output)]), 1)
            self.assertEqual(output.read_bytes(), previous)
            with patch('bbc_rss.fetch_page', return_value=page()):
                self.assertEqual(bbc_rss.main(['--output', str(output)]), 0)
            self.assertEqual(len(ET.parse(output).findall('channel/item')), 10)
            self.assertEqual(list(Path(folder).iterdir()), [output])


class HistoryTests(unittest.TestCase):
    def test_new_story_added_old_story_retained_across_restart(self):
        first_seen = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)
        next_seen = datetime(2026, 10, 25, 12, 15, tzinfo=timezone.utc)
        original = bbc_rss.parse_most_read(page())
        history = bbc_rss.merge_stories([], original, now=first_seen)
        # Simulate a process restart, with only the XML file surviving.
        restored = bbc_rss.load_history(bbc_rss.build_rss(history))
        new = bbc_rss.Story('New story', 'https://www.bbc.com/news/articles/new', 'Summary', published=first_seen)
        second = bbc_rss.merge_stories(restored, original[1:] + [new], now=next_seen)
        self.assertEqual(len(second), 11)
        self.assertEqual(second[0].url, new.url)
        self.assertEqual(second[0].first_seen, next_seen)
        self.assertEqual(second[1:], restored)
        # The removed story reappears and the list is reordered: no extra entry.
        returned = bbc_rss.merge_stories(second, list(reversed(original)), now=next_seen)
        self.assertEqual(returned, second)
        self.assertEqual(len(bbc_rss.load_history(bbc_rss.build_rss(second))), 11)

    def test_rank_and_headline_changes_do_not_rewrite_existing_entries(self):
        current = bbc_rss.parse_most_read(page())
        history = bbc_rss.load_history(bbc_rss.build_rss(bbc_rss.merge_stories([], current)))
        changed = [bbc_rss.Story('Changed title', s.url, 'Changed summary') for s in reversed(current)]
        self.assertEqual(bbc_rss.merge_stories(history, changed), history)
        a = bbc_rss.merge_stories([], current, now=datetime(2026, 10, 25, tzinfo=timezone.utc))
        b = bbc_rss.merge_stories([], list(reversed(current)), now=datetime(2026, 10, 25, tzinfo=timezone.utc))
        self.assertEqual(a, b)

    def test_cli_persists_history_and_skips_unchanged_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'feed.xml'
            args = ['--output', str(path)]
            with patch('bbc_rss.fetch_page', return_value=page()):
                self.assertEqual(bbc_rss.main(args), 0)
                previous = path.read_bytes()
                with patch('bbc_rss.atomic_write') as write:
                    self.assertEqual(bbc_rss.main(args), 0)
                    write.assert_not_called()
                self.assertEqual(path.read_bytes(), previous)
            # One new URL replaces a current member. No historical entry is removed.
            updated = page().replace('story0', 'new-story')
            with patch('bbc_rss.fetch_page', return_value=updated):
                self.assertEqual(bbc_rss.main(args), 0)
            self.assertEqual(len(bbc_rss.load_history(path.read_bytes())), 11)

    def test_corrupt_history_fails_without_overwriting(self):
        for bad in (b'broken', b'<rss/>', b'<rss><channel><item><title>No link</title></item></channel></rss>'):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'feed.xml'
                path.write_bytes(bad)
                with patch('bbc_rss.fetch_page') as fetch, self.assertLogs(level='ERROR'):
                    self.assertEqual(bbc_rss.main(['--output', str(path)]), 1)
                    fetch.assert_not_called()
                self.assertEqual(path.read_bytes(), bad)

    def test_imports_old_snapshot_without_rank_description(self):
        stories = bbc_rss.merge_stories([], bbc_rss.parse_most_read(page()))
        document = ET.fromstring(bbc_rss.build_rss(stories))
        description = document.find('channel/item/description')
        description.text = '<p>BBC News Most read: #1</p>' + description.text
        restored = bbc_rss.load_history(ET.tostring(document))
        self.assertNotIn('Most read: #', restored[0].summary)
        self.assertEqual(restored[0].summary, stories[0].summary)


class FetchTests(unittest.TestCase):
    def test_transient_error_retries_but_forbidden_does_not(self):
        for status, calls in ((503, 3), (403, 1)):
            error = HTTPError(bbc_rss.SOURCE_URL, status, 'error', {}, BytesIO())
            with patch('bbc_rss.urlopen', side_effect=error) as fetch, patch('bbc_rss.time.sleep'):
                with self.assertRaises(bbc_rss.FeedError):
                    bbc_rss.fetch_page()
                self.assertEqual(fetch.call_count, calls)

    def test_cli_rejects_invalid_settings(self):
        for args in (['--ttl', '0'], ['--timeout', 'nan'], ['--timeout', '-1'], ['--feed-url', 'file:///tmp/feed']):
            with self.subTest(args=args), redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                bbc_rss.main(args)


if __name__ == '__main__':
    unittest.main()
