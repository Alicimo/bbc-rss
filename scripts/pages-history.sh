#!/usr/bin/env bash
# Run from the application repository root. Only feed.xml is committed to feed-data.
set -euo pipefail

branch=feed-data
directory=.feed-history
command=${1:-}

case "$command" in
  restore)
    if [[ -e "$directory" ]]; then
      echo "History directory already exists; refusing to replace it." >&2
      exit 1
    fi
    status=0
    git ls-remote --exit-code --heads origin "refs/heads/$branch" > /dev/null || status=$?
    if [[ "$status" == 0 ]]; then
      git fetch origin "refs/heads/$branch:refs/remotes/origin/$branch"
      git worktree add --detach "$directory" "refs/remotes/origin/$branch"
      if [[ ! -f "$directory/feed.xml" ]]; then
        echo "Existing history branch has no feed.xml; refusing to reset history." >&2
        exit 1
      fi
    elif [[ "$status" == 2 && "${2:-}" == --initialize ]]; then
      git worktree add --detach "$directory" HEAD
      git -C "$directory" switch --orphan "$branch"
    elif [[ "$status" == 2 ]]; then
      echo "No feed-data branch. Run manually with initialize_history enabled once." >&2
      exit 1
    else
      echo "Cannot inspect remote history (git status $status); refusing to reset it." >&2
      exit 1
    fi
    ;;
  save)
    test -f "$directory/feed.xml"
    git -C "$directory" config user.name 'github-actions[bot]'
    git -C "$directory" config user.email '41898282+github-actions[bot]@users.noreply.github.com'
    git -C "$directory" add -- feed.xml
    if ! git -C "$directory" diff --cached --quiet; then
      git -C "$directory" commit -m 'Add newly discovered BBC Most read stories'
      git -C "$directory" push origin "HEAD:refs/heads/$branch"
    fi
    ;;
  *)
    echo "Usage: bash scripts/pages-history.sh restore [--initialize] | save" >&2
    exit 2
    ;;
esac
