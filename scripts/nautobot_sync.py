#!/usr/bin/env python3
"""
Run the "Sync Device Types" / "Sync Module Types" jobs on Nautobot through the REST API.

The jobs must already be loaded from this Git repository and enabled in Nautobot.
Dry-run is the default; pass --commit to actually write to Nautobot.

Configuration (environment variables):
    NAUTOBOT_URL     e.g. https://nautobot.example.com
    NAUTOBOT_TOKEN   API token of a user allowed to run the jobs

Examples:
    # Preview Cisco device types whose file name matches 9300
    python3 scripts/nautobot_sync.py device --manufacturer Cisco --filter 9300

    # Import them for real, with elevation images
    python3 scripts/nautobot_sync.py device --manufacturer Cisco --filter 9300 --commit --images

    # Import several manufacturers, one job run each
    python3 scripts/nautobot_sync.py device -m Cisco -m Juniper -m Arista --commit

    # Import every manufacturer folder of module-types, one job run each
    python3 scripts/nautobot_sync.py module --all-manufacturers --commit

Exit code: 0 when every run succeeded without errors in its log, 1 otherwise.
Only the Python standard library is used.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

JOBS = {
    "device": {"name": "Sync Device Types", "folder": "device-types", "images": True},
    "module": {"name": "Sync Module Types", "folder": "module-types", "images": False},
}

FINISHED = {"SUCCESS", "FAILURE", "REVOKED", "REJECTED", "IGNORED"}
PROBLEM_LEVELS = {"warning", "error", "critical", "failure"}


class NautobotAPI:
    def __init__(self, url: str, token: str, verify_tls: bool = True, timeout: int = 60):
        self.base = url.rstrip("/") + "/api"
        self.headers = {
            "Authorization": f"Token {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        self.timeout = timeout
        self.ssl_context = None if verify_tls else ssl._create_unverified_context()

    def request(self, method: str, path: str, payload: dict | None = None, params: dict | None = None) -> dict:
        url = path if path.startswith("http") else f"{self.base}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        body = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=body, headers=self.headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ssl_context) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            raise SystemExit(f"HTTP {e.code} on {method} {url}\n{detail}") from None
        except urllib.error.URLError as e:
            raise SystemExit(f"Could not reach {url}: {e.reason}") from None

    def run_job(self, job_name: str, data: dict) -> dict:
        path = f"/extras/jobs/{urllib.parse.quote(job_name, safe='')}/run/"
        return self.request("POST", path, {"data": data})

    def job_result(self, result_id: str) -> dict:
        return self.request("GET", f"/extras/job-results/{result_id}/")

    def job_logs(self, result_id: str) -> list[dict]:
        logs, page = [], self.request("GET", "/extras/job-logs/", params={"job_result": result_id, "limit": 1000})
        while True:
            logs.extend(page.get("results", []))
            if not page.get("next"):
                break
            page = self.request("GET", page["next"])
        return sorted(logs, key=lambda log: log.get("created", ""))


def status_value(result: dict) -> str:
    status = result.get("status")
    return (status.get("value") if isinstance(status, dict) else status or "").upper()


def run_once(api: NautobotAPI, job: dict, args: argparse.Namespace, manufacturer: str) -> bool:
    data = {
        "manufacturer": manufacturer,
        "text_filter": args.filter or "",
        "dry_run": not args.commit,
        "debug_mode": args.debug,
    }
    if job["images"]:
        data["include_images"] = args.images

    label = f"{job['name']} [{manufacturer or 'all'}{' / ' + args.filter if args.filter else ''}]"
    mode = "COMMIT" if args.commit else "dry-run"
    print(f"\n==> {label} ({mode})")

    response = api.run_job(job["name"], data)
    result = response.get("job_result")
    if not result:
        print("    The job was scheduled instead of run (approval workflow?):", response.get("scheduled_job", {}).get("id"))
        return False

    result_id = result["id"]
    print(f"    Job result: {result_id}")

    deadline = time.monotonic() + args.wait
    while status_value(result) not in FINISHED:
        if time.monotonic() > deadline:
            print(f"    Still {status_value(result)} after {args.wait}s; check the job result in Nautobot.")
            return False
        time.sleep(args.poll)
        result = api.job_result(result_id)

    status = status_value(result)
    logs = api.job_logs(result_id)
    problems = [log for log in logs if str(log.get("log_level", "")).lower() in PROBLEM_LEVELS]

    for log in logs if args.verbose else problems:
        print(f"    [{log.get('log_level', '?'):>8}] {log.get('message', '')}")

    # The jobs log per-file failures and still finish as SUCCESS, so errors in the log also count.
    errors = [p for p in problems if str(p.get("log_level")).lower() != "warning"]
    ok = status == "SUCCESS" and not errors
    print(f"    Status: {status} | log lines: {len(logs)} | warnings/errors: {len(problems)} | {'OK' if ok else 'PROBLEM'}")
    return ok


def local_manufacturers(folder: str) -> list[str]:
    base = REPO_ROOT / folder
    return sorted((p.name for p in base.iterdir() if p.is_dir()), key=str.lower)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the device/module type sync jobs on Nautobot via REST API.",
        epilog="Requires NAUTOBOT_URL and NAUTOBOT_TOKEN in the environment.",
    )
    parser.add_argument("job", choices=JOBS, help="which job to run")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("-m", "--manufacturer", action="append", default=[],
                        help="manufacturer folder name (repeatable; one job run per manufacturer)")
    target.add_argument("--all-manufacturers", action="store_true",
                        help="one job run per manufacturer folder found in this repository")
    parser.add_argument("-f", "--filter", help="regex applied to the file name (device) or model (module)")
    parser.add_argument("--commit", action="store_true", help="write to Nautobot (default is dry-run)")
    parser.add_argument("--images", action="store_true", help="also import elevation images (device job only)")
    parser.add_argument("--debug", action="store_true", help="enable the job's debug logging")
    parser.add_argument("-v", "--verbose", action="store_true", help="print every log line, not only warnings/errors")
    parser.add_argument("--insecure", action="store_true", help="skip TLS certificate verification")
    parser.add_argument("--poll", type=float, default=3, help="seconds between status checks (default 3)")
    parser.add_argument("--wait", type=int, default=1200, help="max seconds to wait per run (default 1200)")
    args = parser.parse_args()

    job = JOBS[args.job]
    if args.images and not job["images"]:
        parser.error("--images is only supported by the device job")

    url, token = os.environ.get("NAUTOBOT_URL"), os.environ.get("NAUTOBOT_TOKEN")
    if not url or not token:
        parser.error("set NAUTOBOT_URL and NAUTOBOT_TOKEN in the environment")

    if args.all_manufacturers:
        manufacturers = local_manufacturers(job["folder"])
    elif args.manufacturer:
        manufacturers = args.manufacturer
    elif args.filter:
        manufacturers = [""]  # filter only, across all manufacturers
    else:
        parser.error("give --manufacturer, --all-manufacturers or --filter (the job refuses to import everything)")

    if args.commit and args.all_manufacturers:
        print(f"About to COMMIT {len(manufacturers)} manufacturers to {url}.")

    api = NautobotAPI(url, token, verify_tls=not args.insecure)
    results = {m or "(filter only)": run_once(api, job, args, m) for m in manufacturers}

    failed = [m for m, ok in results.items() if not ok]
    print(f"\nDone: {len(results) - len(failed)}/{len(results)} runs OK.")
    if failed:
        print("With problems: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
