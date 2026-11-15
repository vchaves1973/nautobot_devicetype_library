#!/usr/bin/env python3
"""
Sync device types, module types, rack types and their images from the NetBox
devicetype-library (https://github.com/netbox-community/devicetype-library) into this repo.

How it works
  - One GitHub API call per folder lists every upstream file with its git blob SHA
    (git/trees/<sha>?recursive=1). Local files are hashed the same way, so unchanged
    files are detected without downloading anything.
  - Files are downloaded from raw.githubusercontent.com (paths URL-encoded).
  - Upstream .yml files are saved as .yaml, the extension the import jobs read.
  - YAML files are kept byte-for-byte as upstream. The import jobs understand both the
    old front-port format (rear_port on each front port) and the newer port-mappings one.

What it changes
  - New upstream files are always added (unless --dry-run).
  - Files that exist locally with different content are only overwritten with --update,
    because they may carry local edits. They are listed so you can review them.
  - Local files that do not exist upstream are never touched.

Examples
  python3 scripts/sync_from_netbox_repo.py --dry-run           # report only
  python3 scripts/sync_from_netbox_repo.py                     # add new files
  python3 scripts/sync_from_netbox_repo.py --update            # add new + overwrite changed
  python3 scripts/sync_from_netbox_repo.py --only device-types elevation-images --vendor Cisco

Set GITHUB_TOKEN to raise the GitHub API rate limit (60 requests/hour without it;
a full run uses 7 API calls). Requires PyYAML (see requirements.txt).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

UPSTREAM_REPO = "netbox-community/devicetype-library"
API = f"https://api.github.com/repos/{UPSTREAM_REPO}"
RAW = f"https://raw.githubusercontent.com/{UPSTREAM_REPO}"
REPO_ROOT = Path(__file__).resolve().parent.parent

YAML_FOLDERS = ("device-types", "module-types", "rack-types")
IMAGE_FOLDERS = ("elevation-images", "module-images")
FOLDERS = YAML_FOLDERS + IMAGE_FOLDERS
YAML_EXT = (".yaml", ".yml")
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif")


def http_get(url: str, accept: str | None = None, retries: int = 3) -> bytes:
    headers = {"User-Agent": "devicetype-library-sync"}
    if accept:
        headers["Accept"] = accept
    token = os.environ.get("GITHUB_TOKEN")
    if token and url.startswith(API):
        headers["Authorization"] = f"Bearer {token}"
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 403 and url.startswith(API):
                raise SystemExit("GitHub API rate limit reached; set GITHUB_TOKEN and retry.") from None
            if e.code == 404 or attempt == retries:
                raise
        except urllib.error.URLError:
            if attempt == retries:
                raise
        time.sleep(2 * attempt)
    raise RuntimeError("unreachable")


def git_blob_sha(data: bytes) -> str:
    """Same hash GitHub reports for a file in a git tree."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def upstream_folders(ref: str) -> tuple[str, dict[str, str]]:
    """Return the resolved commit SHA and {top-level folder: tree SHA}."""
    commit = json.loads(http_get(f"{API}/commits/{urllib.parse.quote(ref)}"))
    root = json.loads(http_get(f"{API}/git/trees/{commit['commit']['tree']['sha']}"))
    return commit["sha"], {e["path"]: e["sha"] for e in root["tree"] if e["type"] == "tree"}


def upstream_files(folder: str, tree_sha: str) -> dict[str, str]:
    """Return {path relative to repo root: blob SHA} for every file under folder."""
    tree = json.loads(http_get(f"{API}/git/trees/{tree_sha}?recursive=1"))
    if tree.get("truncated"):
        raise SystemExit(f"GitHub truncated the file list of {folder}; sync it with --vendor in smaller parts.")
    return {f"{folder}/{e['path']}": e["sha"] for e in tree["tree"] if e["type"] == "blob"}


def local_path_for(upstream_path: str) -> str:
    return upstream_path[:-4] + ".yaml" if upstream_path.endswith(".yml") else upstream_path


def validate_yaml(data: bytes) -> str | None:
    """Return an error message, or None when the file is usable by the import jobs."""
    try:
        doc = yaml.safe_load(data)
    except yaml.YAMLError as e:
        return f"invalid YAML: {str(e).splitlines()[0]}"
    if not isinstance(doc, dict) or not doc.get("manufacturer") or not doc.get("model"):
        return "missing manufacturer/model"
    return None


def plan(folder: str, files: dict[str, str], vendors: set[str]) -> dict[str, list[str]]:
    """Classify upstream files of one folder as new, changed or unchanged."""
    exts = YAML_EXT if folder in YAML_FOLDERS else IMAGE_EXT
    result = {"new": [], "changed": [], "unchanged": []}
    for path, sha in sorted(files.items()):
        parts = path.split("/")
        if len(parts) != 3 or not path.lower().endswith(exts):
            continue  # only <folder>/<Vendor>/<file>
        if vendors and parts[1].lower() not in vendors:
            continue
        local = REPO_ROOT / local_path_for(path)
        if not local.exists():
            result["new"].append(path)
        elif git_blob_sha(local.read_bytes()) != sha:
            result["changed"].append(path)
        else:
            result["unchanged"].append(path)
    return result


def download(commit: str, path: str) -> tuple[str, bytes | None, str | None]:
    url = f"{RAW}/{commit}/{urllib.parse.quote(path)}"
    try:
        data = http_get(url)
    except Exception as e:  # noqa: BLE001 - report and continue with the other files
        return path, None, f"download failed: {e}"
    if path.endswith(YAML_EXT):
        error = validate_yaml(data)
        if error:
            return path, None, error
    return path, data, None


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync this repo with the NetBox devicetype-library.")
    parser.add_argument("--only", nargs="+", choices=FOLDERS, default=list(FOLDERS), help="folders to sync (default: all)")
    parser.add_argument("--vendor", action="append", default=[], help="only this manufacturer folder (repeatable)")
    parser.add_argument("--update", action="store_true", help="also overwrite local files whose content differs")
    parser.add_argument("--dry-run", action="store_true", help="report what would change, write nothing")
    parser.add_argument("--ref", default="master", help="upstream branch, tag or commit (default: master)")
    parser.add_argument("--workers", type=int, default=16, help="parallel downloads (default: 16)")
    parser.add_argument("-v", "--verbose", action="store_true", help="list every new/changed file")
    args = parser.parse_args()

    vendors = {v.lower() for v in args.vendor}
    commit, folders = upstream_folders(args.ref)
    print(f"Upstream {UPSTREAM_REPO}@{args.ref} ({commit[:10]})")

    to_download: list[str] = []
    changed_kept: list[str] = []
    for folder in args.only:
        if folder not in folders:
            print(f"{folder}: not found upstream, skipped")
            continue
        p = plan(folder, upstream_files(folder, folders[folder]), vendors)
        print(f"{folder}: {len(p['new'])} new, {len(p['changed'])} changed, {len(p['unchanged'])} unchanged")
        to_download += p["new"]
        if args.update:
            to_download += p["changed"]
        else:
            changed_kept += p["changed"]
        if args.verbose:
            for path in p["new"]:
                print(f"  + {local_path_for(path)}")
            for path in p["changed"]:
                print(f"  ~ {local_path_for(path)}")

    if changed_kept:
        print(f"\n{len(changed_kept)} local files differ from upstream and were kept (use --update to overwrite, -v to list).")

    if args.dry_run or not to_download:
        print("\nDry-run: nothing written." if args.dry_run else "\nNothing to download.")
        return 0

    print(f"\nDownloading {len(to_download)} files...")
    written, skipped = 0, []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for path, data, error in pool.map(lambda p: download(commit, p), to_download):
            if error:
                skipped.append((path, error))
                continue
            dst = REPO_ROOT / local_path_for(path)
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(data)
            written += 1

    for path, error in skipped:
        print(f"  SKIP {path}: {error}", file=sys.stderr)
    print(f"Done: {written} written, {len(skipped)} skipped.")
    return 1 if skipped else 0


if __name__ == "__main__":
    sys.exit(main())
