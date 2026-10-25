# BBC News Most Read RSS

A Python generator for an **unofficial, accumulating RSS 2.0 feed of stories
discovered in [BBC News' Most read section](https://www.bbc.com/news)**. New
stories are added once; previously discovered stories stay in the feed after
leaving the top ten. Rank changes have no effect.

## Run

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```sh
uv run bbc-most-read-rss
```

This fetches BBC once, reads any existing `public/feed.xml`, and adds unseen
stories. The XML file is both the published feed and its persistent history;
keep it across runs and back it up. No separate database is required.
To set the destination and the eventual subscription URL:

```sh
uv run bbc-most-read-rss --output public/feed.xml --feed-url https://example.org/feed.xml
```

Alternatively, install with `python -m pip install .`, then use the same
`bbc-most-read-rss` command. For continuous serving and updating, use `--serve`
as described below.

## Feed behavior

- Reads only the Most read section. Ranks are neither stored nor included in
  entries. New scrape batches appear before earlier batches; ties within a
  batch use URL order so the source's rank never affects feed order.
- Includes BBC headlines, links, and available summaries. Does not fetch full
  articles. A refresh normally makes one request to the news homepage.
- Uses the page's embedded `__NEXT_DATA__` for summaries. The visible section
  determines membership; embedded data is also a fallback if that section
  markup disappears.
- Uses canonical article URLs as stable GUIDs. A rank or headline change does
  not create or update an entry. A story that leaves and returns is not re-added.
- RSS `pubDate` records when a story was first discovered by this app, rather
  than the article's original publication date. This lets an older article that
  newly enters Most read appear as a new feed entry. Existing entries retain
  their original title, summary, date, and GUID.
- Retains all discovered stories indefinitely. The feed grows over time; it
  does not reconstruct history before the first run, and stories that enter and
  leave between refreshes may be missed.
- Matches the BBC edition served to the deployment machine. Location can affect
  results; a server elsewhere may see a different list from your browser.
- Requires exactly ten unique BBC News links. Missing sections, duplicates,
  unexpected links, or partial lists fail the refresh with exit code 1.
- Fetch timeout defaults to 30 seconds per attempt. Transient network errors,
  HTTP 429, and selected HTTP 5xx errors retry up to three times with backoff.
  HTTP 403 is reported without retrying.
- Replaces the output atomically only after successful parsing and rendering.
  Invalid existing history also fails the refresh instead of silently resetting
  it. On failure, an existing feed stays available but stops receiving additions.
- Does not rewrite the file when there are no additions or configuration changes.
  `lastBuildDate` reflects the latest discovery, not the latest scrape. Monitor
  scheduled job success separately to check scraper health.
- `--ttl 15` suggests a 15-minute polling interval to RSS readers. **It does not
  schedule generation**. Server mode schedules refreshes with `--interval`;
  GitHub Actions uses the workflow schedule.

The earlier snapshot version's XML can be imported automatically: old entries
are retained and rank descriptions are removed. Their existing dates remain
unchanged. Run only one writer per feed. The server has one updater thread and the
Actions workflow serializes runs. Do not run a separate one-shot generator
against a feed while the server is using it.

BBC layout and embedded data can change. Both extraction paths were checked
against the live page on 2026-10-25; the tests are offline and use representative
markup so they remain deterministic.

## Self-hosted HTTP server

One process handles both HTTP and periodic updates. No cron or separate refresh
service is needed:

```sh
uv run --frozen bbc-most-read-rss --serve --interval 900
```

Subscribe to `http://127.0.0.1:8000/feed.xml`. The server:

- Refreshes immediately at startup, then waits 900 seconds after each completed
  attempt before refreshing again. `--interval` is in seconds.
- Fetches in a single background worker. HTTP requests serve cached XML without
  contacting BBC; a slow scrape does not block subscribers.
- Loads existing history at startup and serves it while refreshing. Before the
  first successful generation, `/feed.xml` returns 503 with `Retry-After`.
- Keeps the last good feed on update failure, logs the error, and retries on the
  next interval. Invalid persisted XML fails startup instead of resetting history.
- Supports GET, HEAD, ETags, and conditional requests. Only `/feed.xml`, `/healthz`,
  and `/` (redirect to feed) are exposed; it does not serve a filesystem directory.
- Stops on Ctrl-C or SIGTERM. Shutdown waits up to five seconds for the worker;
  an in-flight network request may be abandoned when the process exits. Atomic
  writes preserve the last complete XML file.

`/healthz` returns JSON including the latest attempt, success, error, and whether
an update is in progress. It returns 200 after a successful refresh, or 503 before
the first success, after a failed attempt, or when successful updates are overdue.
A cached feed remains available with HTTP 200 even when health is degraded.
Health state is in memory and resets on restart; story history is on disk.

For a persistent installation:

```sh
uv run --frozen bbc-most-read-rss --serve \
  --host 127.0.0.1 --port 8000 --interval 900 \
  --output /var/lib/bbc-rss/feed.xml \
  --feed-url https://rss.example.org/feed.xml
```

Use an HTTPS reverse proxy such as Caddy or nginx to forward to port 8000. Binding
with `--host 0.0.0.0` permits direct LAN/container access. Keep the output file on
persistent storage and back it up; deleting it resets history. `--feed-url` sets
the advertised public subscription URL, independently of the bind address.

### Linux systemd service

An example is provided in [`deploy/bbc-rss.service`](deploy/bbc-rss.service).
Install the application in `/opt/bbc-rss`, create a `bbc-rss` system user/group,
and run `uv sync --frozen` there to provision `/opt/bbc-rss/.venv`. The service
account needs read/execute access to that directory and its interpreter. If uv's
Python is installed under your home directory, provision a system-accessible
Python instead (for example `uv sync --frozen --python /usr/bin/python3`).

Copy the unit to `/etc/systemd/system/bbc-rss.service`, adjust paths and optionally
add `--feed-url` to `ExecStart`, then run:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now bbc-rss
journalctl -u bbc-rss -f
```

The unit creates `/var/lib/bbc-rss` for persistent state, runs the server as the
service account, and restarts on failure. It requires no systemd timer.

## GitHub Actions + GitHub Pages

[`.github/workflows/feed.yml`](.github/workflows/feed.yml) runs the one-shot
Python generator at `7,22,37,52 * * * *` (approximately every 15 minutes) and
publishes the XML using GitHub Pages. No Python server runs on Pages.

### Setup

1. Push this project, including `uv.lock`, to a GitHub repository. Enable Actions.
2. In **Settings → Pages → Build and deployment**, select **GitHub Actions** as
   the source. The workflow needs permission to write repository contents and
   deploy Pages; organization or branch policies may need to allow that.
3. Optionally set the repository Actions variable `FEED_URL` for a custom public
   feed URL. Otherwise the workflow uses the Pages base URL plus `/feed.xml`.
4. On the default branch, manually run **Update and publish BBC RSS** from the
   Actions tab with **initialize_history** enabled for the first run.
5. Subscribe to the deployed Pages URL followed by `/feed.xml`. Subsequent
   scheduled runs restore and extend the saved history automatically.

### Persistence and failures

The workflow tests the code, restores `feed.xml` from the dedicated `feed-data`
branch, fetches BBC, commits additions to that branch, then uploads and deploys
the XML. The [`scripts/pages-history.sh`](scripts/pages-history.sh) helper handles
restoring and saving history. Only `feed.xml` is committed to the data branch;
application code stays on the default branch.

- The history branch must exist on ordinary scheduled runs. Only the explicit
  first-run option permits creating it. An inaccessible remote, missing XML on
  an existing branch, or invalid XML fails the run instead of resetting history.
- A scrape failure leaves history and the published site unchanged. A deployment
  failure after saving history is recoverable: the next run republishes the saved
  feed even if there are no new additions.
- No-op refreshes do not create commits. The workflow uses one concurrency group
  with `cancel-in-progress: false`, and pushes without force to avoid overwriting
  conflicting history changes.
- History is in Git, rather than an expiring Actions artifact or cache. Back up
  the data branch. Deleting it and reinitializing starts a new archive.
- Pages is deployed explicitly because pushes using `GITHUB_TOKEN` do not trigger
  a branch-based Pages build. No personal access token is required.

Each ordinary scrape is one page request, so Actions can handle the workload.
GitHub scheduling is approximate: runs may be delayed or dropped, and public
repository schedules disable after 60 days without repository activity. Access
to BBC from GitHub's runner IPs still needs checking during the first hosted run.
The workflow and service files are included but have not been deployed.

References: [custom Pages workflows](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages),
[Pages publishing sources](https://docs.github.com/en/pages/getting-started-with-github-pages/configuring-a-publishing-source-for-your-github-pages-site),
and [schedule limitations](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

## Verify

```sh
uv run python -m unittest discover -s tests -v
uv run bbc-most-read-rss  # live smoke check; needs internet access
```

Tests cover extraction boundaries and fallback, exact item counts, duplicate and
URL rejection, metadata, XML escaping, stable identifiers, retries, CLI validation,
preservation of the last successful file, cumulative history across restarts,
stories leaving and returning, position-independent deduplication, no-op
refreshes, and rejection of corrupt history.

Additional integration tests exercise the real HTTP server, background refresh
responsiveness, degraded health and recovery, conditional requests, restart
persistence, and history-branch initialization/restoration using local Git repos.
