import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

WORKERS = [
    "poller.py",
    "worker.py",
    "content_worker.py",
    "grouping_worker.py",
    "extraction_worker.py",
    "bitrix_worker.py",
]

REQUIRED_FILES = [
    *WORKERS,
    "templates/base.html",
    "templates/leads.html",
    "templates/lead_detail.html",
    "templates/unassigned.html",
    "templates/admin.html",
    "static/styles.css",
]

REQUIRED_ENV = [
    "MICROSOFT_TENANT_ID",
    "MICROSOFT_CLIENT_ID",
    "MICROSOFT_CLIENT_SECRET",
    "TEAMS_TEAM_ID",
    "TEAMS_CHANNEL_ID",
    "BITRIX_WEBHOOK_URL",
    "BITRIX_EXHIBITION_ID",
    "TEAMS_WORKFLOW_WEBHOOK_URL",
    "OPENAI_API_KEY",
]


def check_configuration() -> bool:
    missing_files = [
        filename
        for filename in REQUIRED_FILES
        if not (BASE_DIR / filename).exists()
    ]

    missing_env = [
        name
        for name in REQUIRED_ENV
        if not os.getenv(name)
    ]

    if missing_files:
        print("MISSING_FILES:", ", ".join(missing_files))

    if missing_env:
        print("MISSING_ENV:", ", ".join(missing_env))

    if missing_files or missing_env:
        return False

    print("SERVICE_CHECK_OK")
    return True


def start_worker(filename: str) -> subprocess.Popen:
    environment = os.environ.copy()
    environment["PYTHONUNBUFFERED"] = "1"

    print(f"[supervisor] starting {filename}")

    return subprocess.Popen(
        [
            sys.executable,
            "-u",
            str(BASE_DIR / filename),
        ],
        cwd=BASE_DIR,
        env=environment,
    )


def stop_workers(processes: dict[str, subprocess.Popen]) -> None:
    print("\n[supervisor] stopping workers")

    for process in processes.values():
        if process.poll() is None:
            process.terminate()

    deadline = time.time() + 10

    while time.time() < deadline:
        if all(process.poll() is not None for process in processes.values()):
            return
        time.sleep(0.2)

    for process in processes.values():
        if process.poll() is None:
            process.kill()


def run_service() -> None:
    if not check_configuration():
        raise SystemExit(1)

    processes = {
        filename: start_worker(filename)
        for filename in WORKERS
    }

    print("[supervisor] all workers started")
    print("[supervisor] press Ctrl+C to stop")

    try:
        while True:
            for filename, process in list(processes.items()):
                exit_code = process.poll()

                if exit_code is not None:
                    print(
                        f"[supervisor] {filename} stopped "
                        f"with code {exit_code}; restarting"
                    )
                    time.sleep(3)
                    processes[filename] = start_worker(filename)

            time.sleep(1)

    except KeyboardInterrupt:
        stop_workers(processes)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    if args.check:
        raise SystemExit(0 if check_configuration() else 1)

    run_service()


if __name__ == "__main__":
    main()
