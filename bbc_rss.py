"""Turn the BBC News Most read section into a static RSS 2.0 document."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
import html
import json
import logging
import math
import os
from pathlib import Path
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup

SOURCE_URL = "https://www.bbc.com/news"
ATOM = "http://www.w3.org/2005/Atom"
ET.register_namespace("atom", ATOM)
LOG = logging.getLogger(__name__)


class FeedError(Exception):
    """A failed refresh; the previous feed must be kept."""


@dataclass(frozen=True)
class Story:
    title: str
    url: str
    summary: str = ""
    published: datetime | None = None
    first_seen: datetime | None = None


def canonical_url(href: str) -> str:
    """Keep BBC News links, without tracking queries or fragments."""
    parts = urlsplit(urljoin(SOURCE_URL, href))
    if (
        parts.scheme not in {"http", "https"}
        or parts.netloc.lower() not in {"www.bbc.com", "bbc.com", "www.bbc.co.uk", "bbc.co.uk"}
        or not parts.path.startswith("/news/")
    ):
        raise FeedError(f"Unexpected story URL in Most read: {href!r}")
    return urlunsplit(("https", "www.bbc.com", parts.path.rstrip("/"), "", ""))


def _collections(value):
    """Locate the named collection without depending on Next.js nesting keys."""
    if isinstance(value, dict):
        title = value.get("title")
        if isinstance(title, str) and title.strip().casefold() == "most read":
            if isinstance(value.get("content"), list):
                yield value["content"]
        for child in value.values():
            yield from _collections(child)
    elif isinstance(value, list):
        for child in value:
            yield from _collections(child)


def _published(metadata) -> datetime | None:
    # BBC supplies firstUpdated as Unix milliseconds. Do not invent dates if absent.
    value = metadata.get("firstUpdated") if isinstance(metadata, dict) else None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value / 1000, timezone.utc)
        except (OverflowError, OSError, ValueError):
            pass
    return None


def _json_stories(soup: BeautifulSoup) -> list[Story]:
    script = soup.find("script", id="__NEXT_DATA__")
    if script is None:
        return []
    try:
        data = json.loads(script.get_text())
    except (ValueError, RecursionError):
        LOG.warning("BBC page data is unreadable; trying the visible section")
        return []
    collections = list(_collections(data))
    if not collections:
        return []
    if len(collections) != 1:
        raise FeedError("Ambiguous page: multiple Most read data collections")
    stories = []
    for entry in collections[0]:
        if not isinstance(entry, dict):
            raise FeedError("Malformed Most read data entry")
        title, href = entry.get("title"), entry.get("href")
        if not isinstance(title, str) or not title.strip() or not isinstance(href, str):
            raise FeedError("Most read data entry is missing a title or link")
        summary = entry.get("description")
        stories.append(Story(
            " ".join(title.split()), canonical_url(href),
            summary.strip() if isinstance(summary, str) else "",
            _published(entry.get("metadata")),
        ))
    return stories


def parse_most_read(page: str) -> list[Story]:
    """Read exactly ten unique stories from the visible Most read section."""
    soup = BeautifulSoup(page, "html.parser")
    sections = [
        section for section in soup.find_all("section")
        if str(section.get("data-analytics_group_name", "")).strip().casefold() == "most read"
    ]
    if not sections:
        for heading in soup.find_all(["h2", "h3"]):
            if heading.get_text(" ", strip=True).casefold() == "most read":
                section = heading.find_parent("section")
                if section is not None and all(section is not s for s in sections):
                    sections.append(section)
    if len(sections) > 1:
        raise FeedError("Ambiguous page: multiple visible Most read sections")

    metadata = _json_stories(soup)
    if sections:
        by_url = {story.url: story for story in metadata}
        stories = []
        for link in sections[0].select("a[href]"):
            headline = link.select_one('[data-testid="card-headline"]')
            if headline is None:
                headline = link.find(["h2", "h3"])
            if headline is None:
                continue
            title = headline.get_text(" ", strip=True)
            if not title:
                raise FeedError("Most read headline is empty")
            url = canonical_url(link["href"])
            extra = by_url.get(url)
            stories.append(Story(title, url, extra.summary if extra else "", extra.published if extra else None))
    else:
        stories = metadata

    if len(stories) != 10 or len({story.url for story in stories}) != 10:
        raise FeedError(f"Expected ten unique Most read stories; found {len(stories)} entries")
    return stories


def fetch_page(timeout: float = 30, attempts: int = 3) -> str:
    """Retry transient failures only; bound both request time and response size."""
    request = Request(SOURCE_URL, headers={
        "User-Agent": "bbc-most-read-rss/0.1 (+personal RSS feed generator)",
        "Accept": "text/html", "Accept-Language": "en-GB,en;q=0.9",
    })
    for attempt in range(attempts):
        try:
            with urlopen(request, timeout=timeout) as response:
                if response.headers.get_content_type() != "text/html":
                    raise FeedError("BBC returned a non-HTML response")
                body = response.read(5_000_001)
                if len(body) > 5_000_000:
                    raise FeedError("BBC page exceeds the 5 MB response limit")
                return body.decode(response.headers.get_content_charset() or "utf-8")
        except HTTPError as error:
            if error.code not in {429, 500, 502, 503, 504} or attempt == attempts - 1:
                raise FeedError(f"BBC returned HTTP {error.code}") from error
        except (URLError, TimeoutError, OSError) as error:
            if attempt == attempts - 1:
                raise FeedError(f"Could not fetch BBC News: {error}") from error
        time.sleep(2 ** attempt)
    raise FeedError("Could not fetch BBC News")


def load_history(content: bytes) -> list[Story]:
    """The published RSS file is also the durable, append-only story archive."""
    try:
        root = ET.fromstring(content)
        channel = root.find("channel")
        if root.tag != "rss" or channel is None:
            raise ValueError("expected an RSS channel")
        stories = []
        seen = set()
        for item in channel.findall("item"):
            url = canonical_url(item.findtext("link") or "")
            title = item.findtext("title")
            if not title or item.findtext("guid") != url or url in seen:
                raise ValueError("missing title, invalid GUID, or duplicate story")
            seen.add(url)
            date = item.findtext("pubDate")
            first_seen = parsedate_to_datetime(date) if date else None
            if first_seen is not None:
                if first_seen.tzinfo is None:
                    raise ValueError("entry date has no timezone")
                first_seen = first_seen.astimezone(timezone.utc)
            description = BeautifulSoup(item.findtext("description") or "", "html.parser")
            # Import feeds created by the earlier snapshot implementation, removing ranks.
            for paragraph in description.find_all("p"):
                if paragraph.get_text().startswith("BBC News Most read: #"):
                    paragraph.decompose()
            stories.append(Story(title, url, description.get_text().strip(), first_seen=first_seen))
        return stories
    except (ET.ParseError, ValueError, TypeError, OverflowError) as error:
        raise FeedError(f"Existing feed history is invalid; refusing to discard it: {error}") from error


def merge_stories(history: list[Story], current: list[Story], *, now: datetime | None = None) -> list[Story]:
    """Add unseen URLs once, keeping prior entries unchanged and ignoring ranks."""
    known = {story.url for story in history}
    discovered = now or datetime.now(timezone.utc)
    additions = []
    # Stable tie order for a scrape batch; source ranking never affects the archive.
    for story in sorted(current, key=lambda story: story.url):
        if story.url not in known:
            additions.append(replace(story, first_seen=discovered))
            known.add(story.url)
    return additions + history


def build_rss(stories: list[Story], *, feed_url: str | None = None, ttl: int = 15) -> bytes:
    root = ET.Element("rss", version="2.0")
    channel = ET.SubElement(root, "channel")
    for name, value in (
        ("title", "BBC News — Most read"),
        ("link", SOURCE_URL),
        ("description", "Stories discovered in BBC News' Most read list, retained after they leave. Unofficial feed."),
        ("language", "en"),
        ("lastBuildDate", format_datetime(max(
            (story.first_seen or story.published for story in stories if story.first_seen or story.published),
            default=datetime.now(timezone.utc),
        ), usegmt=True)),
        ("ttl", str(ttl)),
    ):
        ET.SubElement(channel, name).text = value
    if feed_url:
        ET.SubElement(channel, f"{{{ATOM}}}link", href=feed_url, rel="self", type="application/rss+xml")
    for story in stories:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = story.title
        ET.SubElement(item, "link").text = story.url
        ET.SubElement(item, "guid", isPermaLink="true").text = story.url
        ET.SubElement(item, "description").text = f"<p>{html.escape(story.summary)}</p>"
        date = story.first_seen or story.published
        if date:
            ET.SubElement(item, "pubDate").text = format_datetime(date, usegmt=True)
    ET.indent(root)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def atomic_write(path: Path, content: bytes) -> None:
    """Publish only a complete file, readable by a separate static web server."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o644)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def positive_timeout(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return number


def refresh(output: Path, *, timeout: float = 30, feed_url: str | None = None, ttl: int = 15) -> bytes:
    """Fetch, merge and publish one complete update; return the resulting XML."""
    try:
        previous = output.read_bytes()
    except FileNotFoundError:
        previous = None
    history = load_history(previous) if previous is not None else []
    current = parse_most_read(fetch_page(timeout))
    stories = merge_stories(history, current)
    content = build_rss(stories, feed_url=feed_url, ttl=ttl)
    if content != previous:
        atomic_write(output, content)
    LOG.info("Refreshed %s: %d new stories, %d retained in total", output, len(stories) - len(history), len(stories))
    return content


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("public/feed.xml"))
    parser.add_argument("--feed-url", help="Public feed URL, used for the Atom self link")
    parser.add_argument("--timeout", type=positive_timeout, default=30, help="Timeout per request in seconds")
    parser.add_argument("--ttl", type=int, default=15, help="Suggested reader refresh interval in minutes (not a scheduler)")
    parser.add_argument("--serve", action="store_true", help="Serve HTTP and refresh automatically in the background")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="HTTP port (default: 8000)")
    parser.add_argument("--interval", type=positive_timeout, default=900, help="Server refresh interval in seconds (default: 900)")
    args = parser.parse_args(argv)
    if args.ttl < 1:
        parser.error("--ttl must be at least 1")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if args.feed_url:
        url = urlsplit(args.feed_url)
        if url.scheme not in {"http", "https"} or not url.netloc:
            parser.error("--feed-url must be an absolute HTTP(S) URL")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        if args.serve:
            from bbc_server import serve
            serve(args)
        else:
            refresh(args.output, timeout=args.timeout, feed_url=args.feed_url, ttl=args.ttl)
    except (FeedError, OSError, UnicodeError, ValueError) as error:
        LOG.error("Refresh failed; previous feed preserved: %s", error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
