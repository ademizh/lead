import html
import json
import os
import re
import sqlite3
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import msal
import requests
from dotenv import load_dotenv


load_dotenv()


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Не заполнена переменная {name}")
    return value


TENANT_ID = required_env("MICROSOFT_TENANT_ID")
CLIENT_ID = required_env("MICROSOFT_CLIENT_ID")
CLIENT_SECRET = required_env("MICROSOFT_CLIENT_SECRET")
TEAM_ID = required_env("TEAMS_TEAM_ID")
CHANNEL_ID = required_env("TEAMS_CHANNEL_ID")

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "30"))
FULL_SYNC_INTERVAL = int(
    os.getenv("FULL_SYNC_INTERVAL_SECONDS", "3600")
)
DATABASE_PATH = Path(
    os.getenv("DATABASE_PATH", "data/messages.db")
)

SERVICE_MARKER = os.getenv(
    "SERVICE_MESSAGE_MARKER",
    "[LEADBOT]",
)

DENYLIST_USER_IDS = {
    value.strip()
    for value in os.getenv(
        "TEAMS_DENYLIST_USER_IDS",
        "",
    ).split(",")
    if value.strip()
}

# ДОБАВЛЕНО: по умолчанию игнорируем сообщения, опубликованные приложениями
# (боты, Workflows, административные интеграции) — см. determine_ignore_reason.
IGNORE_APPLICATION_MESSAGES = os.getenv(
    "IGNORE_APPLICATION_MESSAGES",
    "true",
).strip().lower() not in {"0", "false", "no"}


msal_app = msal.ConfidentialClientApplication(
    client_id=CLIENT_ID,
    authority=(
        f"https://login.microsoftonline.com/{TENANT_ID}"
    ),
    client_credential=CLIENT_SECRET,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def html_to_text(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    text = html.unescape(text)
    return " ".join(text.split())


def get_access_token() -> str:
    result = msal_app.acquire_token_for_client(
        scopes=["https://graph.microsoft.com/.default"]
    )

    token = result.get("access_token")

    if not token:
        error = result.get("error", "unknown_error")
        description = result.get(
            "error_description",
            "No description",
        )
        raise RuntimeError(f"{error}: {description}")

    return token


def create_database() -> sqlite3.Connection:
    DATABASE_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row

    connection.execute("PRAGMA journal_mode=WAL")

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL,
            team_id TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            message_id TEXT NOT NULL,

            etag TEXT,
            created_at TEXT,
            last_modified_at TEXT,
            reply_to_id TEXT,

            author_id TEXT,
            author_name TEXT,
            message_type TEXT,

            body_html TEXT,
            body_text TEXT,
            attachments_json TEXT,
            web_url TEXT,
            raw_json TEXT NOT NULL,

            processing_status TEXT NOT NULL,
            ignore_reason TEXT,

            stored_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,

            UNIQUE (
                tenant_id,
                team_id,
                channel_id,
                message_id
            )
        )
        """
    )

    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_messages_processing_status
        ON messages(processing_status)
        """
    )

    connection.commit()
    return connection


def determine_ignore_reason(
    message_type: str,
    author_id: str,
    body_html: str,
    raw_message: dict | None = None,
    is_application: bool = False,
) -> str | None:
    if message_type != "message":
        return f"message_type:{message_type}"

    if author_id and author_id in DENYLIST_USER_IDS:
        return "denylisted_author"

    # ДОБАВЛЕНО: сообщения, опубликованные приложением/ботом (в т.ч. наша
    # собственная карточка-подтверждение "Лид передан в Bitrix24", которую
    # шлёт bitrix_worker через Workflows). ТЗ, п. 1.3, прямо называет ботов и
    # административные аккаунты частью шума в канале. Раньше такие сообщения
    # проходили как обычные: в логах пользователя карточка бота стала
    # message_db_id=23 и пошла в обработку как потенциальный лид.
    if is_application and IGNORE_APPLICATION_MESSAGES:
        return "application_message"

    if SERVICE_MARKER:
        # ИСПРАВЛЕНО: маркер искали только в body_html, но у адаптивной
        # карточки тело пустое, а весь текст лежит в attachments. Теперь
        # проверяем всё сообщение целиком.
        haystack = body_html or ""

        if raw_message is not None:
            haystack = json.dumps(raw_message, ensure_ascii=False)

        if SERVICE_MARKER in haystack:
            return "service_message"

    return None


def upsert_message(
    connection: sqlite3.Connection,
    message: dict,
) -> tuple[str, str]:
    message_id = str(message["id"])

    etag = (
        message.get("etag")
        or message.get("@odata.etag")
        or ""
    )

    created_at = message.get("createdDateTime") or ""
    last_modified_at = (
        message.get("lastModifiedDateTime")
        or created_at
    )
    reply_to_id = message.get("replyToId")

    sender = message.get("from") or {}
    user = sender.get("user") or {}
    application = sender.get("application") or {}

    author_id = (
        user.get("id")
        or application.get("id")
        or ""
    )
    author_name = (
        user.get("displayName")
        or application.get("displayName")
        or ""
    )

    message_type = message.get(
        "messageType",
        "unknown",
    )

    body_html = (
        (message.get("body") or {}).get("content")
        or ""
    )
    body_text = html_to_text(body_html)

    ignore_reason = determine_ignore_reason(
        message_type=message_type,
        author_id=author_id,
        body_html=body_html,
        raw_message=message,
        is_application=bool(application.get("id")),
    )

    processing_status = (
        "ignored" if ignore_reason else "pending"
    )

    existing = connection.execute(
        """
        SELECT etag, last_modified_at
        FROM messages
        WHERE tenant_id = ?
          AND team_id = ?
          AND channel_id = ?
          AND message_id = ?
        """,
        (
            TENANT_ID,
            TEAM_ID,
            CHANNEL_ID,
            message_id,
        ),
    ).fetchone()

    if existing:
        changed = (
            existing["etag"] != etag
            or existing["last_modified_at"]
            != last_modified_at
        )

        if not changed:
            return "unchanged", processing_status

        connection.execute(
            """
            UPDATE messages
            SET
                etag = ?,
                created_at = ?,
                last_modified_at = ?,
                reply_to_id = ?,
                author_id = ?,
                author_name = ?,
                message_type = ?,
                body_html = ?,
                body_text = ?,
                attachments_json = ?,
                web_url = ?,
                raw_json = ?,
                processing_status = ?,
                ignore_reason = ?,
                updated_at = ?
            WHERE tenant_id = ?
              AND team_id = ?
              AND channel_id = ?
              AND message_id = ?
            """,
            (
                etag,
                created_at,
                last_modified_at,
                reply_to_id,
                author_id,
                author_name,
                message_type,
                body_html,
                body_text,
                json.dumps(
                    message.get("attachments") or [],
                    ensure_ascii=False,
                ),
                message.get("webUrl") or "",
                json.dumps(
                    message,
                    ensure_ascii=False,
                ),
                processing_status,
                ignore_reason,
                utc_now(),
                TENANT_ID,
                TEAM_ID,
                CHANNEL_ID,
                message_id,
            ),
        )

        return "changed", processing_status

    timestamp = utc_now()

    connection.execute(
        """
        INSERT INTO messages (
            tenant_id,
            team_id,
            channel_id,
            message_id,
            etag,
            created_at,
            last_modified_at,
            reply_to_id,
            author_id,
            author_name,
            message_type,
            body_html,
            body_text,
            attachments_json,
            web_url,
            raw_json,
            processing_status,
            ignore_reason,
            stored_at,
            updated_at
        )
        VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        """,
        (
            TENANT_ID,
            TEAM_ID,
            CHANNEL_ID,
            message_id,
            etag,
            created_at,
            last_modified_at,
            reply_to_id,
            author_id,
            author_name,
            message_type,
            body_html,
            body_text,
            json.dumps(
                message.get("attachments") or [],
                ensure_ascii=False,
            ),
            message.get("webUrl") or "",
            json.dumps(
                message,
                ensure_ascii=False,
            ),
            processing_status,
            ignore_reason,
            timestamp,
            timestamp,
        ),
    )

    return "new", processing_status


def graph_get(url: str, params=None) -> dict:
    token = get_access_token()

    for attempt in range(1, 6):
        response = requests.get(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            params=params,
            timeout=30,
        )

        if response.status_code == 401 and attempt == 1:
            token = get_access_token()
            continue

        if (
            response.status_code == 429
            or response.status_code >= 500
        ):
            retry_after = int(
                response.headers.get("Retry-After", "5")
            )
            time.sleep(min(retry_after, 30))
            continue

        response.raise_for_status()
        return response.json()

    raise RuntimeError("Microsoft Graph retry limit exceeded")


def synchronize(full_sync: bool) -> Counter:
    encoded_channel_id = quote(
        CHANNEL_ID,
        safe="",
    )

    url = (
        "https://graph.microsoft.com/v1.0/"
        f"teams/{TEAM_ID}/channels/"
        f"{encoded_channel_id}/messages"
    )

    params = {
        "$top": 50,
        "$expand": "replies",
    }

    connection = create_database()
    statistics = Counter()

    try:
        while url:
            payload = graph_get(url, params=params)
            params = None

            for parent in payload.get("value", []):
                messages = [parent]

                for reply in parent.get("replies") or []:
                    reply_copy = dict(reply)

                    if not reply_copy.get("replyToId"):
                        reply_copy["replyToId"] = parent["id"]

                    messages.append(reply_copy)

                for message in messages:
                    statistics["received"] += 1

                    action, status = upsert_message(
                        connection,
                        message,
                    )

                    statistics[action] += 1

                    if status == "pending" and action in {
                        "new",
                        "changed",
                    }:
                        statistics["queued"] += 1

                    if status == "ignored":
                        statistics["ignored"] += 1

            connection.commit()

            if not full_sync:
                break

            url = payload.get("@odata.nextLink")

    finally:
        connection.close()

    return statistics


def run() -> None:
    print(
        f"Poller запущен. Интервал: "
        f"{POLL_INTERVAL} секунд."
    )
    print("Для остановки нажми Ctrl+C.")

    first_run = True
    last_full_sync = 0.0

    while True:
        should_full_sync = (
            first_run
            or time.monotonic() - last_full_sync
            >= FULL_SYNC_INTERVAL
        )

        try:
            statistics = synchronize(
                full_sync=should_full_sync
            )

            sync_type = (
                "full" if should_full_sync else "recent"
            )

            print(
                f"[{utc_now()}] "
                f"sync={sync_type} "
                f"received={statistics['received']} "
                f"new={statistics['new']} "
                f"changed={statistics['changed']} "
                f"unchanged={statistics['unchanged']} "
                f"queued={statistics['queued']} "
                f"ignored={statistics['ignored']}"
            )

            if should_full_sync:
                last_full_sync = time.monotonic()

            first_run = False

        except Exception as error:
            # Не выводим тело сообщений и токены.
            print(
                f"[{utc_now()}] "
                f"poll_error={type(error).__name__}: "
                f"{error}"
            )

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        print("\nPoller остановлен.")