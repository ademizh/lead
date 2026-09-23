import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

import av
import easyocr
from dotenv import load_dotenv
from faster_whisper import WhisperModel
from PIL import Image


load_dotenv()

DATABASE_PATH = Path(
    os.getenv("DATABASE_PATH", "data/messages.db")
)

WORKER_INTERVAL = int(
    os.getenv("CONTENT_WORKER_INTERVAL_SECONDS", "5")
)
RETRY_LIMIT = int(
    os.getenv("CONTENT_RETRY_LIMIT", "3")
)
WHISPER_MODEL_NAME = os.getenv(
    "WHISPER_MODEL",
    "small",
)
TRANSCRIPTION_PIPELINE_VERSION = os.getenv(
    "TRANSCRIPTION_PIPELINE_VERSION",
    "2",
).strip()

# ДОБАВЛЕНО: язык голосовых. Пусто — автоопределение (ТЗ: менеджеры переходят
# с русского на английский). Ставьте "ru" или "en", если зал одноязычный:
# на коротких шумных записях автоопределение иногда ошибается целиком.
WHISPER_LANGUAGE = os.getenv("WHISPER_LANGUAGE", "").strip() or None

# ДОБАВЛЕНО: подсказка со словарём выставки. Whisper опирается на неё при
# распознавании имён собственных и терминов — без неё "GITEX", "КП",
# "интегратор" превращаются в созвучный мусор. Дополните названиями своих
# продуктов и типичных компаний-посетителей.
WHISPER_INITIAL_PROMPT = os.getenv(
    "WHISPER_INITIAL_PROMPT",
    "Выставка, стенд, визитка, "
    "контакт, лид, клиент, партнёр, партнёрство, дистрибьютор, интегратор, "
    "реселлер, перепродавать клиентам, внедрять клиентам, КП, "
    "коммерческое предложение, презентация, прайс, "
    "бюджет, внедрение, интеграция, аналитика, платформа, демо, "
    "email, почта, собачка, точка, телефон, WhatsApp, Telegram.",
).strip()

_ocr_reader = None
_whisper_model = None


def connect_database() -> sqlite3.Connection:
    connection = sqlite3.connect(
        DATABASE_PATH,
        timeout=30,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


def add_column_if_missing(
    connection: sqlite3.Connection,
    column_name: str,
    definition: str,
) -> None:
    columns = {
        row["name"]
        for row in connection.execute(
            "PRAGMA table_info(messages)"
        ).fetchall()
    }

    if column_name not in columns:
        connection.execute(
            f"ALTER TABLE messages "
            f"ADD COLUMN {column_name} {definition}"
        )


def initialize_database() -> None:
    connection = connect_database()

    add_column_if_missing(
        connection,
        "content_attempts",
        "INTEGER NOT NULL DEFAULT 0",
    )
    add_column_if_missing(
        connection,
        "content_next_attempt_at",
        "REAL NOT NULL DEFAULT 0",
    )
    add_column_if_missing(
        connection,
        "content_error_code",
        "TEXT",
    )
    add_column_if_missing(
        connection,
        "content_locked_at",
        "REAL",
    )

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS content_artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            message_db_id INTEGER NOT NULL,
            file_id INTEGER,

            source_key TEXT NOT NULL UNIQUE,
            source_sha256 TEXT NOT NULL,

            artifact_type TEXT NOT NULL,
            raw_text TEXT NOT NULL,
            confidence REAL,

            model_name TEXT,
            metadata_json TEXT NOT NULL,
            status TEXT NOT NULL,

            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )

    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_content_artifacts_message
        ON content_artifacts(message_db_id)
        """
    )

    # При локальном запуске предполагается один ML-worker.
    connection.execute(
        """
        UPDATE messages
        SET
            processing_status = 'content_retry',
            content_next_attempt_at = 0,
            content_locked_at = NULL
        WHERE processing_status = 'content_processing'
        """
    )

    connection.commit()
    connection.close()


def claim_message() -> dict | None:
    connection = connect_database()

    try:
        connection.execute("BEGIN IMMEDIATE")

        message = connection.execute(
            """
            SELECT *
            FROM messages
            WHERE processing_status IN (
                'files_ready',
                'content_retry'
            )
              AND content_next_attempt_at <= ?
            ORDER BY id
            LIMIT 1
            """,
            (time.time(),),
        ).fetchone()

        if not message:
            connection.commit()
            return None

        attempts = message["content_attempts"] + 1

        connection.execute(
            """
            UPDATE messages
            SET
                processing_status = 'content_processing',
                content_attempts = ?,
                content_locked_at = ?,
                content_error_code = NULL
            WHERE id = ?
            """,
            (
                attempts,
                time.time(),
                message["id"],
            ),
        )

        connection.commit()

        result = dict(message)
        result["content_attempts"] = attempts
        return result

    finally:
        connection.close()


def source_hash(value: str) -> str:
    return hashlib.sha256(
        value.encode("utf-8")
    ).hexdigest()


def store_artifact(
    message_db_id: int,
    file_id: int | None,
    source_key: str,
    source_sha256: str,
    artifact_type: str,
    raw_text: str,
    confidence: float | None,
    model_name: str,
    metadata: dict,
    status: str = "ready",
) -> None:
    connection = connect_database()
    current_time = time.time()

    # A changed OCR/transcription pipeline produces a new source_key.  Keep
    # the old row for audit, but exclude it from extraction so one audio file
    # never contributes two competing transcripts after reprocessing.
    if file_id is not None and artifact_type in {"ocr", "transcript"}:
        connection.execute(
            """
            UPDATE content_artifacts
            SET status = 'superseded', updated_at = ?
            WHERE file_id = ?
              AND artifact_type = ?
              AND source_key <> ?
              AND status = 'ready'
            """,
            (current_time, file_id, artifact_type, source_key),
        )

    connection.execute(
        """
        INSERT INTO content_artifacts (
            message_db_id,
            file_id,
            source_key,
            source_sha256,
            artifact_type,
            raw_text,
            confidence,
            model_name,
            metadata_json,
            status,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_key)
        DO UPDATE SET
            raw_text = excluded.raw_text,
            confidence = excluded.confidence,
            metadata_json = excluded.metadata_json,
            status = excluded.status,
            updated_at = excluded.updated_at
        """,
        (
            message_db_id,
            file_id,
            source_key,
            source_sha256,
            artifact_type,
            raw_text,
            confidence,
            model_name,
            json.dumps(
                metadata,
                ensure_ascii=False,
            ),
            status,
            current_time,
            current_time,
        ),
    )

    connection.commit()
    connection.close()


def get_ocr_reader():
    global _ocr_reader

    if _ocr_reader is None:
        print("Загрузка EasyOCR...")
        _ocr_reader = easyocr.Reader(
            ["ru", "en"],
            gpu=False,
        )

    return _ocr_reader


def get_whisper_model():
    global _whisper_model

    if _whisper_model is None:
        print(
            f"Загрузка Whisper: "
            f"{WHISPER_MODEL_NAME}..."
        )

        _whisper_model = WhisperModel(
            WHISPER_MODEL_NAME,
            device="cpu",
            compute_type="int8",
        )

    return _whisper_model


def detect_real_file_type(file_path: Path) -> str:
    # Сначала пробуем действительно открыть файл как изображение.
    try:
        with Image.open(file_path) as image:
            image.verify()

        return "image"

    except Exception:
        pass

    # Затем проверяем наличие аудио/видеопотока.
    try:
        with av.open(str(file_path)) as container:
            stream_types = {
                stream.type
                for stream in container.streams
            }

            if "audio" in stream_types:
                return "audio"

    except Exception:
        pass

    return "unsupported"


def run_ocr(
    message_db_id: int,
    file_row: sqlite3.Row,
) -> None:
    file_path = Path(file_row["local_path"])
    reader = get_ocr_reader()

    results = reader.readtext(
        str(file_path),
        detail=1,
        paragraph=False,
    )

    lines = []
    metadata_lines = []
    confidences = []

    for bounding_box, text, confidence in results:
        confidence = float(confidence)
        text = str(text)

        converted_box = [
            [float(point[0]), float(point[1])]
            for point in bounding_box
        ]

        lines.append(text)
        confidences.append(confidence)

        metadata_lines.append(
            {
                "text": text,
                "confidence": confidence,
                "bounding_box": converted_box,
            }
        )

    raw_text = "\n".join(lines)

    average_confidence = (
        sum(confidences) / len(confidences)
        if confidences
        else 0.0
    )

    model_name = (
        f"easyocr-{getattr(easyocr, '__version__', 'unknown')}"
    )

    artifact_key = (
        f"file:{file_row['id']}:"
        f"{file_row['sha256']}:ocr:{model_name}"
    )

    store_artifact(
        message_db_id=message_db_id,
        file_id=file_row["id"],
        source_key=artifact_key,
        source_sha256=file_row["sha256"],
        artifact_type="ocr",
        raw_text=raw_text,
        confidence=average_confidence,
        model_name=model_name,
        metadata={
            "lines": metadata_lines,
            "line_count": len(metadata_lines),
        },
    )


def run_transcription(
    message_db_id: int,
    file_row: sqlite3.Row,
) -> None:
    file_path = Path(file_row["local_path"])
    model = get_whisper_model()

    # ИЗМЕНЕНО: настройки под реальные условия из ТЗ — "голосовые записаны в
    # шумном зале, с оговорками, самоперебиванием и переходом с русского на
    # английский".
    #
    # * temperature — лесенка запасных попыток. При одной температуре (по
    #   умолчанию 0) модель на шумном фрагменте зацикливается и выдаёт
    #   повторяющийся мусор; с лесенкой она переспрашивает себя иначе.
    # * compression_ratio_threshold / log_prob_threshold — отбраковка именно
    #   такого зацикленного вывода, чтобы он не попал в лид как "факт".
    # * no_speech_threshold — шум зала без речи не превращается в текст.
    # * initial_prompt — словарь выставки: без него имена собственные и
    #   продуктовые термины распознаются как созвучный мусор.
    # * language — по умолчанию автоопределение (русский/английский в одном
    #   канале), но фиксируется через WHISPER_LANGUAGE, если зал одноязычный:
    #   на коротких шумных записях автоопределение иногда ошибается.
    segment_iterator, information = model.transcribe(
        str(file_path),
        beam_size=5,
        vad_filter=True,
        word_timestamps=True,
        condition_on_previous_text=False,
        language=WHISPER_LANGUAGE,
        initial_prompt=WHISPER_INITIAL_PROMPT or None,
        temperature=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
        compression_ratio_threshold=2.4,
        log_prob_threshold=-1.0,
        no_speech_threshold=0.6,
    )

    transcript_parts = []
    metadata_segments = []

    for segment in segment_iterator:
        segment_text = segment.text.strip()

        if segment_text:
            transcript_parts.append(segment_text)

        words = []

        for word in segment.words or []:
            words.append(
                {
                    "start": float(word.start),
                    "end": float(word.end),
                    "word": word.word,
                    "probability": float(
                        word.probability
                    ),
                }
            )

        metadata_segments.append(
            {
                "start": float(segment.start),
                "end": float(segment.end),
                "text": segment_text,
                "words": words,
            }
        )

    # Никакого литературного исправления.
    raw_transcript = " ".join(transcript_parts)

    language_probability = float(
        getattr(
            information,
            "language_probability",
            0.0,
        )
    )

    artifact_key = (
        f"file:{file_row['id']}:"
        f"{file_row['sha256']}:"
        f"transcript:{WHISPER_MODEL_NAME}:v{TRANSCRIPTION_PIPELINE_VERSION}"
    )

    store_artifact(
        message_db_id=message_db_id,
        file_id=file_row["id"],
        source_key=artifact_key,
        source_sha256=file_row["sha256"],
        artifact_type="transcript",
        raw_text=raw_transcript,
        confidence=language_probability,
        model_name=(
            f"faster-whisper-{WHISPER_MODEL_NAME}"
        ),
        metadata={
            "language": information.language,
            "language_probability": (
                language_probability
            ),
            "duration": float(
                getattr(information, "duration", 0.0)
            ),
            "segments": metadata_segments,
        },
    )


def store_unsupported_artifact(
    message_db_id: int,
    file_row: sqlite3.Row,
) -> None:
    artifact_key = (
        f"file:{file_row['id']}:"
        f"{file_row['sha256']}:unsupported"
    )

    store_artifact(
        message_db_id=message_db_id,
        file_id=file_row["id"],
        source_key=artifact_key,
        source_sha256=file_row["sha256"],
        artifact_type="unsupported_attachment",
        raw_text="",
        confidence=None,
        model_name="file-type-detector",
        metadata={
            "mime_type": file_row["mime_type"],
            "reason": "unsupported_real_format",
        },
        status="unsupported",
    )


def process_message(message: dict) -> None:
    message_db_id = message["id"]

    # Текст Teams хранится как отдельный артефакт.
    teams_text = message.get("body_text") or ""

    if teams_text:
        text_sha256 = source_hash(teams_text)

        source_key = (
            f"message:{message_db_id}:"
            f"{text_sha256}:teams_text"
        )

        store_artifact(
            message_db_id=message_db_id,
            file_id=None,
            source_key=source_key,
            source_sha256=text_sha256,
            artifact_type="teams_text",
            raw_text=teams_text,
            confidence=1.0,
            model_name="none",
            metadata={
                "source": "teams_message_body",
            },
        )

    connection = connect_database()

    files = connection.execute(
        """
        SELECT *
        FROM message_files
        WHERE message_db_id = ?
        ORDER BY id
        """,
        (message_db_id,),
    ).fetchall()

    connection.close()

    for file_row in files:
        file_path = Path(file_row["local_path"])

        if not file_path.exists():
            raise RuntimeError(
                "downloaded_file_missing"
            )

        real_file_type = detect_real_file_type(
            file_path
        )

        if real_file_type == "image":
            run_ocr(
                message_db_id,
                file_row,
            )

        elif real_file_type == "audio":
            run_transcription(
                message_db_id,
                file_row,
            )

        else:
            store_unsupported_artifact(
                message_db_id,
                file_row,
            )


def mark_completed(message: dict) -> None:
    connection = connect_database()

    connection.execute(
        """
        UPDATE messages
        SET
            processing_status = 'content_ready',
            content_error_code = NULL,
            content_locked_at = NULL
        WHERE id = ?
          AND processing_status = 'content_processing'
          AND COALESCE(etag, '') = ?
          AND COALESCE(last_modified_at, '') = ?
        """,
        (
            message["id"],
            message.get("etag") or "",
            message.get("last_modified_at") or "",
        ),
    )

    connection.commit()
    connection.close()


def mark_failed(
    message: dict,
    error_code: str,
) -> None:
    connection = connect_database()

    attempts = message["content_attempts"]
    current_time = time.time()

    if attempts < RETRY_LIMIT:
        status = "content_retry"
        delay = min(
            10 * (2 ** (attempts - 1)),
            120,
        )
        next_attempt = current_time + delay
    else:
        status = "content_error"
        next_attempt = 0

    connection.execute(
        """
        UPDATE messages
        SET
            processing_status = ?,
            content_next_attempt_at = ?,
            content_error_code = ?,
            content_locked_at = NULL
        WHERE id = ?
          AND processing_status = 'content_processing'
        """,
        (
            status,
            next_attempt,
            error_code,
            message["id"],
        ),
    )

    connection.commit()
    connection.close()

    print(
        f"message_db_id={message['id']} "
        f"status={status} "
        f"error={error_code}"
    )


def run() -> None:
    initialize_database()

    print("Content worker запущен.")
    print("Для остановки нажми Ctrl+C.")

    while True:
        message = claim_message()

        if not message:
            time.sleep(WORKER_INTERVAL)
            continue

        try:
            process_message(message)
            mark_completed(message)

            print(
                f"message_db_id={message['id']} "
                f"status=content_ready"
            )

        except Exception as error:
            # В лог не записываем текст сообщения,
            # имя файла или содержимое OCR.
            mark_failed(
                message,
                type(error).__name__,
            )


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        print("\nContent worker остановлен.")
