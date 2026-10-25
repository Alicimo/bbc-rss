# BBC News Most Read RSS

**[Subscribe to the RSS feed](https://tiefenbacher.dev/bbc-rss/feed.xml)**

An unofficial RSS feed of stories appearing in [BBC News’ Most read section](https://www.bbc.com/news).
New stories are added once and retained after leaving the top ten. Rank changes
are ignored. Updates run approximately every 15 minutes.

## Run locally

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```sh
uv run --frozen bbc-most-read-rss
```

Fetches once and updates `public/feed.xml`. This file also stores the complete
story history: keep it across runs and back it up. No database is required.

## Self-hosted server

```sh
uv run --frozen bbc-most-read-rss --serve
```

Subscribe to `http://127.0.0.1:8000/feed.xml`. The same process refreshes at startup
and every 900 seconds after each attempt. Requests serve cached XML while the
updater runs in the background; no cron job is needed.

To customize the server:

```sh
uv run --frozen bbc-most-read-rss --serve \
  --host 127.0.0.1 --port 8000 --interval 900 \
  --output /var/lib/bbc-rss/feed.xml \
  --feed-url https://rss.example.org/feed.xml
```

Use an HTTPS reverse proxy for public access, or `--host 0.0.0.0` for LAN/container
access. Keep the output on persistent storage and run only one writer per feed.

- `/feed.xml`: RSS with HEAD and ETag support; returns 503 until a feed is available.
- `/healthz`: JSON status; returns 503 before a successful refresh or when updates fail or become overdue.
- Failed refreshes preserve the last good feed. Ctrl-C and SIGTERM stop the server.

### Linux service

Use [`deploy/bbc-rss.service`](deploy/bbc-rss.service). It expects the application
and virtual environment in `/opt/bbc-rss`, a `bbc-rss` user/group, and a Python
interpreter accessible to that account. Provision with `uv sync --frozen`, then
copy the unit to `/etc/systemd/system/` and adjust paths as needed.

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now bbc-rss
journalctl -u bbc-rss -f
```

The service creates `/var/lib/bbc-rss` for persistent history and restarts on failure.

### Nix / NixOS deployment

Build the packaged application or run the server directly:

```sh
nix build
nix run . -- --serve --output "$PWD/feed.xml"
```

The flake supports x86_64 and aarch64 Linux, plus Apple Silicon macOS. Python, runtime
dependencies, and HTTPS certificates are included; `uv` is not required.
`flake.lock` pins Nixpkgs and its Python packages independently of `uv.lock`.

For a NixOS host, add this repository to the host's flake inputs and import its
module in the host configuration:

```nix
{
  inputs.bbc-rss.url = "github:Alicimo/bbc-rss";

  # Merge into the host flake's existing outputs:
  outputs = { nixpkgs, bbc-rss, ... }: {
    nixosConfigurations.hostname = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux"; # Or aarch64-linux.
      modules = [ bbc-rss.nixosModules.default ./configuration.nix ];
    };
  };
}
```

Then enable the service in `configuration.nix`:

```nix
{
  services.bbc-rss = {
    enable = true;
    host = "127.0.0.1"; # Default; place an HTTPS reverse proxy in front.
    port = 8000;
    interval = 900;
    feedUrl = "https://rss.example.org/feed.xml";
  };
}
```

Apply with `sudo nixos-rebuild switch --flake /path/to/host-config#hostname`.
The `bbc-rss` systemd service uses a dynamic user, keeps history in
`/var/lib/bbc-rss/feed.xml` across rebuilds and restarts, and restarts on failure.
Back up that file. The module does not open firewall ports; when binding to a
public or LAN address, configure the host firewall as needed. Inspect status with
`systemctl status bbc-rss` and `journalctl -u bbc-rss -f`.

Run `nix flake check` to build the package and run the Python test suite.

## GitHub Actions + Pages

The [workflow](.github/workflows/feed.yml) scrapes approximately every 15 minutes,
saves history on the separate `feed-data` branch, and publishes static XML to Pages.
Only feed changes create commits; application code stays on `main`.

1. Enable Actions and select **GitHub Actions** in **Settings → Pages**.
2. On the default branch, manually run **Update and publish BBC RSS** with
   **initialize_history** enabled for the first run.
3. Subscribe to the Pages URL followed by `/feed.xml`.

Optionally set the Actions variable `FEED_URL` to override the advertised URL.
This repository’s intended feed URL is `https://tiefenbacher.dev/bbc-rss/feed.xml`.

Normal runs require existing history and fail rather than silently resetting it.
Scrape failures leave the published feed unchanged. Back up the `feed-data` branch.
GitHub schedules can be delayed, and public-repository schedules disable after
60 days without activity; see [GitHub’s scheduling documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

## Feed behavior

Entries contain headlines, links, and available summaries. Their RSS dates record
first discovery by this app. Previously saved entries remain unchanged, even if a
story’s title or position changes. Stories appearing only between scrapes may be
missed, and BBC’s list may vary by the deployment machine’s region.

Each normal scrape fetches one page. Invalid or incomplete results preserve the
previous feed. BBC layout changes may require parser updates. `--ttl` only sets a
reader polling hint; server scheduling uses `--interval`.

## Tests

```sh
uv run --frozen python -m unittest discover -s tests -v
```
