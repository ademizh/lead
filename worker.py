import base64
import hashlib
import html  # ДОБАВЛЕНО: нужен для раскодирования &amp; в src встроенных картинок
import json
import os
import re
import sqlite3
import time
from pathlib import Path

import msal
import requests
from dotenv import load_dotenv


load_dotenv()

TENANT_ID = os.environ["MICROSOFT_TENANT_ID"]
CLIENT_ID = os.environ["MICROSOFT_CLIENT_ID"]
CLIENT_SECRET = os.environ["MICROSOFT_CLIENT_SECRET"]

DATABASE_PATH = Path(
    os.getenv("DATABASE_PATH", "data/messages.db")
)
# ИСПРАВЛЕНО: раньше путь был захардкожен и не читал ATTACHMENTS_DIRECTORY из
# .env, хотя web_app.py читает именно эту переменную для отдачи файлов.
# Если бы её переопределили в .env, worker.py продолжал бы писать файлы в
# "data/attachments", а web_app.py искал бы их в другом месте — вложения
# считались бы отсутствующими ("Stored file is unavailable").
ATTACHMENTS_DIRECTORY = Path(
    os.getenv("ATTACHMENTS_DIRECTORY", "data/attachments")
)

WORKER_INTERVAL = int(
    os.getenv("WORKER_INTERVAL_SECONDS", "5")
)
MAX_RETRIES = int(
    os.getenv("ATTACHMENT_RETRY_LIMIT", "6")
)
MAX_FILE_SIZE = (
    int(os.getenv("ATTACHMENT_MAX_MB", "25"))
    * 1024
    * 1024
)


class DownloadError(Exception):
    def __init__(
        self,
        code: str,
        retryable: bool,
    ):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


msal_app = msal.ConfidentialClientApplication(
    client_id=CLIENT_ID,
    authority=(
        f"https://login.microsoftonline.com/{TENANT_ID}"
    ),
    client_credential=CLIENT_SECRET,
)


def connect_database() -> sqlite3.Connection:
    connection = sqlite3.connect(
        DATABASE_PATH,
        timeout=30,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


def initialize_database() -> None:
    connection = connect_database()

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS processing_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_db_id INTEGER NOT NULL,
            job_type TEXT NOT NULL,
            version_key TEXT NOT NULL,

            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at REAL NOT NULL DEFAULT 0,
            locked_at REAL,

            error_code TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,

            UNIQUE (
                message_db_id,
                job_type,
                version_key
            )
        )
        """
    )

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS message_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_db_id INTEGER NOT NULL,
            attachment_id TEXT NOT NULL,

            original_name TEXT,
            local_path TEXT NOT NULL,
            mime_type TEXT,
            size_bytes INTEGER NOT NULL,
            sha256 TEXT NOT NULL,

            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,

            UNIQUE (
                message_db_id,
                attachment_id
            )
        )
        """
    )

    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_jobs_status
        ON processing_jobs(
            status,
            next_attempt_at
        )
        """
    )

    connection.commit()
    connection.close()


def recover_interrupted_jobs() -> None:
    connection = connect_database()

    stale_before = time.time() - 600

    connection.execute(
        """
        UPDATE processing_jobs
        SET
            status = 'retry',
            next_attempt_at = ?,
            locked_at = NULL,
            error_code = 'worker_interrupted',
            updated_at = ?
        WHERE status = 'processing'
          AND locked_at < ?
        """,
        (
            time.time(),
            time.time(),
            stale_before,
        ),
    )

    connection.commit()
    connection.close()


def enqueue_pending_messages() -> int:
    connection = connect_database()
    current_time = time.time()

    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO processing_jobs (
            message_db_id,
            job_type,
            version_key,
            status,
            attempts,
            next_attempt_at,
            created_at,
            updated_at
        )
        SELECT
            id,
            'prepare_attachments',
            COALESCE(etag, '') || '|' ||
                COALESCE(last_modified_at, ''),
            'pending',
            0,
            0,
            ?,
            ?
        FROM messages
        WHERE processing_status = 'pending'
        """,
        (
            current_time,
            current_time,
        ),
    )

    connection.commit()
    inserted = max(cursor.rowcount, 0)
    connection.close()

    return inserted


def claim_job() -> dict | None:
    connection = connect_database()

    try:
        connection.execute("BEGIN IMMEDIATE")

        job = connection.execute(
            """
            SELECT *
            FROM processing_jobs
            WHERE status IN ('pending', 'retry')
              AND next_attempt_at <= ?
            ORDER BY id
            LIMIT 1
            """,
            (time.time(),),
        ).fetchone()

        if not job:
            connection.commit()
            return None

        attempts = job["attempts"] + 1
        current_time = time.time()

        connection.execute(
            """
            UPDATE processing_jobs
            SET
                status = 'processing',
                attempts = ?,
                locked_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                attempts,
                current_time,
                current_time,
                job["id"],
            ),
        )

        connection.execute(
            """
            UPDATE messages
            SET processing_status = 'downloading_files'
            WHERE id = ?
            """,
            (job["message_db_id"],),
        )

        connection.commit()

        result = dict(job)
        result["attempts"] = attempts
        return result

    finally:
        connection.close()


def get_access_token() -> str:
    result = msal_app.acquire_token_for_client(
        scopes=["https://graph.microsoft.com/.default"]
    )

    token = result.get("access_token")

    if not token:
        raise DownloadError(
            code="token_error",
            retryable=True,
        )

    return token


def encode_sharing_url(url: str) -> str:
    encoded = base64.urlsafe_b64encode(
        url.encode("utf-8")
    ).decode("ascii")

    return "u!" + encoded.rstrip("=")


def safe_suffix(filename: str) -> str:
    suffix = Path(filename or "").suffix.lower()

    if not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
        return ".bin"

    return suffix


def suffix_from_mime_type(mime_type: str) -> str:
    normalized = (mime_type or "").split(";", 1)[0].strip().lower()

    return {
        "audio/mp4": ".m4a",
        "video/mp4": ".mp4",
        "audio/mpeg": ".mp3",
        "audio/ogg": ".ogg",
        "audio/webm": ".webm",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "application/pdf": ".pdf",
    }.get(normalized, ".bin")


def parse_audio_card_url(attachment: dict) -> str | None:
    if attachment.get("contentType") != (
        "application/vnd.microsoft.card.audio"
    ):
        return None

    content = attachment.get("content")

    if isinstance(content, str):
        try:
            content = json.loads(content)
        except json.JSONDecodeError:
            return None

    if not isinstance(content, dict):
        return None

    media = content.get("media")

    if not isinstance(media, list):
        return None

    for item in media:
        if not isinstance(item, dict):
            continue

        media_url = item.get("url")

        if (
            isinstance(media_url, str)
            and media_url.startswith(
                "https://graph.microsoft.com/"
            )
        ):
            return media_url

    return None


# ДОБАВЛЕНО: картинка, вставленная в сообщение Teams прямо в тело (а не
# приложенная файлом), не попадает в attachments вообще. Она лежит в
# hostedContents, а в body.content на неё стоит <img src="https://
# graph.microsoft.com/v1.0/teams/.../messages/.../hostedContents/.../$value">.
# Раньше worker.py смотрел только в attachments_json, поэтому такие фото
# НИКОГДА не скачивались: не было файла -> не было OCR -> content_worker
# помечал сообщение content_ready без единого артефакта -> grouping_worker
# видел пустой текст и выдавал NON_LEAD "empty_content". Именно это в логах
# происходило с фото визитки (message_db_id=16, потом 21) в обоих прогонах.
HOSTED_CONTENT_SRC_RE = re.compile(
    r"src\s*=\s*[\"'](https://graph\.microsoft\.com/[^\"']+)[\"']",
    re.IGNORECASE,
)
HOSTED_CONTENT_ID_RE = re.compile(
    r"/hostedContents/([^/]+)/",
    re.IGNORECASE,
)


def hosted_content_attachments(
    body_html: str,
    raw_json: str,
) -> list[dict]:
    """Синтетические "вложения" для картинок, встроенных в тело сообщения."""
    urls: list[str] = []

    for match in HOSTED_CONTENT_SRC_RE.finditer(body_html or ""):
        url = html.unescape(match.group(1))

        if "/hostedContents/" in url and url not in urls:
            urls.append(url)

    # Некоторые сообщения отдают hostedContents отдельным списком.
    try:
        raw_message = json.loads(raw_json or "{}")
    except json.JSONDecodeError:
        raw_message = {}

    for item in raw_message.get("hostedContents") or []:
        if not isinstance(item, dict):
            continue

        hosted_id = str(item.get("id") or "").strip()
        odata_id = str(item.get("@odata.id") or "").strip()

        if not hosted_id and not odata_id:
            continue

        url = (
            f"https://graph.microsoft.com/v1.0/{odata_id.lstrip('/')}/$value"
            if odata_id
            else ""
        )

        if url and "/hostedContents/" in url and url not in urls:
            urls.append(url)

    attachments: list[dict] = []

    for url in urls:
        id_match = HOSTED_CONTENT_ID_RE.search(url)
        hosted_id = (
            id_match.group(1)
            if id_match
            else hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
        )

        attachments.append(
            {
                "id": f"hosted-{hosted_id[:48]}",
                "contentType": "hosted_content",
                "contentUrl": url,
                "name": "",
            }
        )

    return attachments


def is_graph_url(value) -> bool:
    return isinstance(value, str) and value.startswith(
        "https://graph.microsoft.com/"
    )


def is_downloadable_attachment(attachment: dict) -> bool:
    if (
        attachment.get("contentType") == "reference"
        and attachment.get("contentUrl")
    ):
        return True

    # ДОБАВЛЕНО: прямые ссылки на Graph (встроенные картинки/hostedContents)
    # и вложения-картинки, у которых contentType — это mime-тип, а не
    # "reference". Раньше они молча пропускались как "не скачиваемые".
    if is_graph_url(attachment.get("contentUrl")):
        return True

    content_type = str(attachment.get("contentType") or "").lower()

    if content_type.startswith(("image/", "audio/", "video/")) and attachment.get(
        "contentUrl"
    ):
        return True

    return parse_audio_card_url(attachment) is not None


def attachment_key(attachment: dict) -> str:
    attachment_id = str(attachment.get("id") or "")

    if attachment_id:
        return attachment_id

    content_url = (
        attachment.get("contentUrl")
        or parse_audio_card_url(attachment)
        or ""
    )

    return hashlib.sha256(
        content_url.encode("utf-8")
    ).hexdigest()[:32]


def file_already_downloaded(
    message_db_id: int,
    attachment_id: str,
) -> bool:
    connection = connect_database()

    row = connection.execute(
        """
        SELECT local_path
        FROM message_files
        WHERE message_db_id = ?
          AND attachment_id = ?
        """,
        (
            message_db_id,
            attachment_id,
        ),
    ).fetchone()

    connection.close()

    if not row:
        return False

    return Path(row["local_path"]).exists()


def download_attachment(
    message_db_id: int,
    message_id: str,
    attachment: dict,
    access_token: str,
) -> None:
    content_url = attachment.get("contentUrl")
    audio_card_url = parse_audio_card_url(attachment)

    # ИСПРАВЛЕНО: раньше ЛЮБОЙ contentUrl превращался в sharing-токен
    # (/shares/u!.../driveItem/content). Для ссылок на SharePoint это верно, а
    # для прямых ссылок Graph (встроенные картинки в hostedContents) — нет:
    # такая ссылка уже является конечной точкой скачивания, и оборачивание её
    # в /shares/ давало бы 400/404.
    if is_graph_url(content_url):
        graph_url = content_url
    elif content_url:
        sharing_token = encode_sharing_url(content_url)
        graph_url = (
            "https://graph.microsoft.com/v1.0/"
            f"shares/{sharing_token}/driveItem/content"
        )
    elif audio_card_url:
        graph_url = audio_card_url
    else:
        raise DownloadError(
            code="unsupported_attachment",
            retryable=False,
        )

    attachment_id = attachment_key(attachment)

    if file_already_downloaded(
        message_db_id,
        attachment_id,
    ):
        return

    try:
        response = requests.get(
            graph_url,
            headers={
                "Authorization": f"Bearer {access_token}"
            },
            stream=True,
            allow_redirects=True,
            timeout=60,
        )
    except requests.RequestException as error:
        raise DownloadError(
            code="network_error",
            retryable=True,
        ) from error

    if response.status_code != 200:
        if response.status_code == 403:
            raise DownloadError(
                code="files_permission_denied",
                retryable=False,
            )

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
            raise DownloadError(
                code=f"graph_{response.status_code}",
                retryable=True,
            )

        raise DownloadError(
            code=f"graph_{response.status_code}",
            retryable=False,
        )

    content_length = response.headers.get(
        "Content-Length"
    )

    if (
        content_length
        and int(content_length) > MAX_FILE_SIZE
    ):
        raise DownloadError(
            code="file_too_large",
            retryable=False,
        )

    response_mime_type = response.headers.get(
        "Content-Type",
        "application/octet-stream",
    )
    filename = attachment.get("name") or ""
    suffix = safe_suffix(filename)

    if suffix == ".bin":
        suffix = suffix_from_mime_type(response_mime_type)

    if audio_card_url and suffix == ".bin":
        suffix = ".m4a"

    if not filename and audio_card_url:
        filename = f"teams_voice_{message_id}{suffix}"

    # ДОБАВЛЕНО: у встроенной картинки нет имени файла — даём осмысленное,
    # чтобы она была узнаваема в карточке лида и в веб-интерфейсе.
    if not filename and attachment.get("contentType") == "hosted_content":
        filename = f"teams_image_{message_id}{suffix}"

    file_key = hashlib.sha256(
        f"{message_id}:{attachment_id}".encode("utf-8")
    ).hexdigest()

    ATTACHMENTS_DIRECTORY.mkdir(
        parents=True,
        exist_ok=True,
    )

    output_path = (
        ATTACHMENTS_DIRECTORY
        / f"{file_key}{suffix}"
    )
    temporary_path = output_path.with_suffix(
        output_path.suffix + ".part"
    )

    content_hash = hashlib.sha256()
    downloaded_size = 0

    try:
        with temporary_path.open("wb") as output_file:
            for chunk in response.iter_content(
                chunk_size=64 * 1024
            ):
                if not chunk:
                    continue

                downloaded_size += len(chunk)

                if downloaded_size > MAX_FILE_SIZE:
                    raise DownloadError(
                        code="file_too_large",
                        retryable=False,
                    )

                content_hash.update(chunk)
                output_file.write(chunk)

        temporary_path.replace(output_path)

    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise

    connection = connect_database()
    current_time = time.time()

    connection.execute(
        """
        INSERT INTO message_files (
            message_db_id,
            attachment_id,
            original_name,
            local_path,
            mime_type,
            size_bytes,
            sha256,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(
            message_db_id,
            attachment_id
        )
        DO UPDATE SET
            original_name = excluded.original_name,
            local_path = excluded.local_path,
            mime_type = excluded.mime_type,
            size_bytes = excluded.size_bytes,
            sha256 = excluded.sha256,
            updated_at = excluded.updated_at
        """,
        (
            message_db_id,
            attachment_id,
            filename,
            str(output_path),
            response_mime_type,
            downloaded_size,
            content_hash.hexdigest(),
            current_time,
            current_time,
        ),
    )

    connection.commit()
    connection.close()


def process_job(job: dict) -> None:
    connection = connect_database()

    message = connection.execute(
        """
        SELECT
            id,
            message_id,
            etag,
            last_modified_at,
            attachments_json,
            body_html,
            raw_json
        FROM messages
        WHERE id = ?
        """,
        (job["message_db_id"],),
    ).fetchone()

    connection.close()

    if not message:
        raise DownloadError(
            code="message_not_found",
            retryable=False,
        )

    current_version = (
        f"{message['etag'] or ''}|"
        f"{message['last_modified_at'] or ''}"
    )

    if current_version != job["version_key"]:
        mark_superseded(job)
        return

    attachments = json.loads(
        message["attachments_json"] or "[]"
    )

    # ДОБАВЛЕНО: к обычным вложениям добавляем встроенные в тело картинки
    # (hostedContents). Без этого фото визитки, вставленное в сообщение, а не
    # приложенное файлом, не скачивалось вообще — см. комментарий у
    # hosted_content_attachments().
    attachments = attachments + hosted_content_attachments(
        message["body_html"] or "",
        message["raw_json"] or "{}",
    )

    for attachment in attachments:
        content_type = attachment.get("contentType")

        if (
            content_type == "reference"
            and not attachment.get("contentUrl")
        ):
            raise DownloadError(
                code="attachment_not_ready",
                retryable=True,
            )

        if (
            content_type
            == "application/vnd.microsoft.card.audio"
            and not parse_audio_card_url(attachment)
        ):
            raise DownloadError(
                code="audio_not_ready",
                retryable=True,
            )

    file_attachments = [
        attachment
        for attachment in attachments
        if is_downloadable_attachment(attachment)
    ]

    if file_attachments:
        access_token = get_access_token()

        for attachment in file_attachments:
            download_attachment(
                message_db_id=message["id"],
                message_id=message["message_id"],
                attachment=attachment,
                access_token=access_token,
            )

        for attachment in file_attachments:
            if not file_already_downloaded(
                message["id"],
                attachment_key(attachment),
            ):
                raise DownloadError(
                    code="attachment_not_ready",
                    retryable=True,
                )

    mark_completed(job)


def mark_completed(job: dict) -> None:
    connection = connect_database()
    current_time = time.time()

    current_message = connection.execute(
        """
        SELECT
            COALESCE(etag, '') || '|' ||
            COALESCE(last_modified_at, '')
                AS version_key
        FROM messages
        WHERE id = ?
        """,
        (job["message_db_id"],),
    ).fetchone()

    if (
        current_message
        and current_message["version_key"]
        == job["version_key"]
    ):
        connection.execute(
            """
            UPDATE messages
            SET processing_status = 'files_ready'
            WHERE id = ?
            """,
            (job["message_db_id"],),
        )

    connection.execute(
        """
        UPDATE processing_jobs
        SET
            status = 'done',
            error_code = NULL,
            locked_at = NULL,
            updated_at = ?
        WHERE id = ?
        """,
        (
            current_time,
            job["id"],
        ),
    )

    connection.commit()
    connection.close()


def mark_superseded(job: dict) -> None:
    connection = connect_database()

    connection.execute(
        """
        UPDATE processing_jobs
        SET
            status = 'superseded',
            locked_at = NULL,
            updated_at = ?
        WHERE id = ?
        """,
        (
            time.time(),
            job["id"],
        ),
    )

    connection.execute(
        """
        UPDATE messages
        SET processing_status = 'pending'
        WHERE id = ?
        """,
        (job["message_db_id"],),
    )

    connection.commit()
    connection.close()


def mark_failed(
    job: dict,
    error: DownloadError,
) -> None:
    connection = connect_database()
    current_time = time.time()

    should_retry = (
        error.retryable
        and job["attempts"] < MAX_RETRIES
    )

    if should_retry:
        delay = min(
            5 * (2 ** (job["attempts"] - 1)),
            300,
        )

        job_status = "retry"
        message_status = "waiting_files"
        next_attempt_at = current_time + delay
    else:
        job_status = "failed"
        message_status = "attachment_error"
        next_attempt_at = 0

    connection.execute(
        """
        UPDATE processing_jobs
        SET
            status = ?,
            next_attempt_at = ?,
            locked_at = NULL,
            error_code = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            job_status,
            next_attempt_at,
            error.code,
            current_time,
            job["id"],
        ),
    )

    connection.execute(
        """
        UPDATE messages
        SET processing_status = ?
        WHERE id = ?
        """,
        (
            message_status,
            job["message_db_id"],
        ),
    )

    connection.commit()
    connection.close()

    print(
        f"job={job['id']} "
        f"status={job_status} "
        f"error={error.code}"
    )


def run() -> None:
    initialize_database()
    recover_interrupted_jobs()

    print("Attachment worker запущен.")
    print("Для остановки нажми Ctrl+C.")

    while True:
        enqueued = enqueue_pending_messages()

        if enqueued:
            print(f"Создано задач: {enqueued}")

        job = claim_job()

        if not job:
            time.sleep(WORKER_INTERVAL)
            continue

        try:
            process_job(job)
            print(
                f"job={job['id']} status=done"
            )

        except DownloadError as error:
            mark_failed(job, error)

        except Exception:
            mark_failed(
                job,
                DownloadError(
                    code="unexpected_error",
                    retryable=True,
                ),
            )


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        print("\nWorker остановлен.")
