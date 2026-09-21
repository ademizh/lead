import base64
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

import msal
import requests
from dotenv import load_dotenv


load_dotenv()

TENANT_ID = os.getenv("MICROSOFT_TENANT_ID")
CLIENT_ID = os.getenv("MICROSOFT_CLIENT_ID")
CLIENT_SECRET = os.getenv("MICROSOFT_CLIENT_SECRET")

DATABASE_PATH = Path("data/messages.db")
DOWNLOAD_DIRECTORY = Path("data/attachments")
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB


def encode_sharing_url(url: str) -> str:
    encoded = base64.urlsafe_b64encode(
        url.encode("utf-8")
    ).decode("ascii")

    return "u!" + encoded.rstrip("=")


def get_access_token() -> str:
    app = msal.ConfidentialClientApplication(
        client_id=CLIENT_ID,
        authority=(
            f"https://login.microsoftonline.com/"
            f"{TENANT_ID}"
        ),
        client_credential=CLIENT_SECRET,
    )

    result = app.acquire_token_for_client(
        scopes=["https://graph.microsoft.com/.default"]
    )

    token = result.get("access_token")

    if not token:
        print("TOKEN_FAILED")
        print(result.get("error"))
        sys.exit(1)

    return token


def find_latest_attachment() -> tuple[str, dict]:
    connection = sqlite3.connect(DATABASE_PATH)

    rows = connection.execute(
        """
        SELECT message_id, attachments_json
        FROM messages
        ORDER BY id DESC
        """
    ).fetchall()

    connection.close()

    for message_id, attachments_json in rows:
        attachments = json.loads(
            attachments_json or "[]"
        )

        for attachment in attachments:
            if attachment.get("contentUrl"):
                return message_id, attachment

    raise RuntimeError("Вложение с contentUrl не найдено")


def safe_suffix(filename: str) -> str:
    suffix = Path(filename or "").suffix.lower()

    if not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
        return ".bin"

    return suffix


def download_attachment() -> None:
    message_id, attachment = find_latest_attachment()

    content_url = attachment["contentUrl"]
    attachment_id = str(attachment.get("id") or "")

    sharing_token = encode_sharing_url(content_url)

    graph_url = (
        "https://graph.microsoft.com/v1.0/"
        f"shares/{sharing_token}/driveItem/content"
    )

    access_token = get_access_token()

    response = None

    for attempt in range(1, 7):
        response = requests.get(
            graph_url,
            headers={
                "Authorization": f"Bearer {access_token}"
            },
            stream=True,
            allow_redirects=True,
            timeout=60,
        )

        if response.status_code == 200:
            break

        if response.status_code in {
            404,
            409,
            423,
            429,
            500,
            502,
            503,
            504,
        }:
            retry_after = int(
                response.headers.get(
                    "Retry-After",
                    str(min(2**attempt, 30)),
                )
            )

            print(
                f"Файл пока недоступен. "
                f"Повтор через {retry_after} секунд."
            )
            time.sleep(retry_after)
            continue

        print(f"DOWNLOAD_FAILED: HTTP {response.status_code}")

        if response.status_code == 403:
            print(
                "У приложения, вероятно, нет "
                "Application permission Files.Read.All "
                "с admin consent."
            )

        sys.exit(1)

    if response is None or response.status_code != 200:
        print("DOWNLOAD_FAILED: retry limit exceeded")
        sys.exit(1)

    content_length = response.headers.get("Content-Length")

    if (
        content_length
        and int(content_length) > MAX_FILE_SIZE
    ):
        print("DOWNLOAD_FAILED: файл больше 20 MB")
        sys.exit(1)

    file_hash = hashlib.sha256(
        f"{message_id}:{attachment_id}".encode("utf-8")
    ).hexdigest()[:20]

    suffix = safe_suffix(attachment.get("name") or "")
    output_path = DOWNLOAD_DIRECTORY / f"{file_hash}{suffix}"

    DOWNLOAD_DIRECTORY.mkdir(
        parents=True,
        exist_ok=True,
    )

    downloaded_size = 0

    with output_path.open("wb") as output_file:
        for chunk in response.iter_content(
            chunk_size=64 * 1024
        ):
            if not chunk:
                continue

            downloaded_size += len(chunk)

            if downloaded_size > MAX_FILE_SIZE:
                output_file.close()
                output_path.unlink(missing_ok=True)
                print("DOWNLOAD_FAILED: файл больше 20 MB")
                sys.exit(1)

            output_file.write(chunk)

    print("DOWNLOAD_OK")
    print(f"Размер: {downloaded_size} байт")
    print(f"Сохранено: {output_path}")


if __name__ == "__main__":
    download_attachment()