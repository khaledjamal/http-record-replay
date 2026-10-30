"""Show a GitHub repository and its latest release, from GitHub's public REST API (no
key or login needed).

Its recording, examples/recordings/github_repo/, is committed, so anyone can replay
this run without the network (CI does it on every push):

    python -m http_record_replay compare examples/recordings/github_repo --strict

- Run it for real: `python examples/github_repo.py` (default psf/requests), or pass
  another repository: `python examples/github_repo.py owner/name`.
- Record it again, from the repo root (two of the 60 calls an hour GitHub allows
  without a key):

      HTTP_REPLAY=record HTTP_REPLAY_DIR=examples/recordings/github_repo \\
          python examples/github_repo.py
"""

import sys

import requests

import http_record_replay

GITHUB_API = "https://api.github.com"
DEFAULT_REPOSITORY = "psf/requests"


def get_github_json(path):
    response = requests.get(f"{GITHUB_API}{path}", headers={"Accept": "application/vnd.github+json"}, timeout=30)
    response.raise_for_status()
    return response.json()


def main():
    repository_name = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_REPOSITORY
    repository = get_github_json(f"/repos/{repository_name}")
    latest_release = get_github_json(f"/repos/{repository_name}/releases/latest")
    print(f"{repository['full_name']}: {repository['description']}")
    print(f"stars: {repository['stargazers_count']}, license: {repository['license']['spdx_id']}")
    print(f"latest release: {latest_release['tag_name']}, published {latest_release['published_at'][:10]}")


if __name__ == "__main__":
    http_record_replay.enable()
    main()
