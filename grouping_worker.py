"""

Input: messages with processing_status='content_ready'.
Output:
  * lead_groups: one preliminary lead assembled from one or more messages;
  * grouping_results: auditable ATTACH/NEW/NON_LEAD/AMBIGUOUS decision.

The worker uses deterministic rules first. Only uncertain cases are sent to an
optional structured-output LLM. If no API key is configured, uncertain cases
remain AMBIGUOUS instead of being guessed.

Examples:
    python grouping_worker.py --once
    python grouping_worker.py --rebuild --once
    python grouping_worker.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ДОБАВЛЕНО: email, продиктованный голосом, должен участвовать в
# сопоставлении контактов наравне с написанным текстом.
from spoken_contacts import spoken_emails

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


DB_PATH = Path(os.getenv("DATABASE_PATH", "data/messages.db"))
POLL_SECONDS = int(os.getenv("GROUPING_POLL_SECONDS", "5"))
DEBOUNCE_SECONDS = int(os.getenv("GROUP_DEBOUNCE_SECONDS", "20"))
ACTIVE_GROUP_HOURS = int(os.getenv("ACTIVE_GROUP_HOURS", "12"))
PARTS_WINDOW_SECONDS = int(os.getenv("PARTS_WINDOW_SECONDS", "180"))
# ДОБАВЛЕНО: окно, в котором короткая приписка без собственных признаков лида
# ("Проект игры", "И пн встреча", "срочно, перезвонить в понедельник")
# считается продолжением уже открытой группы того же автора, а не отдельным
# сообщением-не-лидом. ТЗ (п. 1.1) прямо описывает такие приписки как
# нормальную часть потока.
FOLLOW_UP_WINDOW_SECONDS = int(
    os.getenv("FOLLOW_UP_WINDOW_SECONDS", str(PARTS_WINDOW_SECONDS))
)
# ДОБАВЛЕНО: насколько последний контакт должен быть свежее предыдущего,
# чтобы приписка без собственных признаков считалась относящейся именно к
# нему. Если два контакта закрыты почти одновременно (разница меньше этого
# значения), выбирать не из чего — сообщение уходит на страницу "Без лида",
# а не приклеивается наугад.
FOLLOW_UP_TIEBREAK_SECONDS = int(os.getenv("FOLLOW_UP_TIEBREAK_SECONDS", "45"))
MAX_CANDIDATES = int(os.getenv("GROUPING_MAX_CANDIDATES", "5"))
LLM_MODEL = os.getenv("OPENAI_GROUPING_MODEL", "gpt-4o-mini")
ALGORITHM_VERSION = "hybrid-v3"

EMAIL_RE = re.compile(
    r"(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![\w.-])", re.I
)
PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\d\s().-]{6,}\d)(?!\d)")
EMOJI_OR_PUNCT_RE = re.compile(r"^[\W_]+$", re.UNICODE)

# ИСПРАВЛЕНО: как и в PARTNER_RE (extraction_worker.py), корни здесь были без
# \w*, а внешний \b(...)\b требует границу слова сразу после корня. Для
# русского языка это означало, что почти ни одна реальная словоформа не
# матчилась: "Познакомился с Петровым" (нужен корень "познаком") не находило
# совпадение, "заинтересован" (корень "заинтерес") — тоже, "интересуется" —
# тоже (там даже корня "интерес" не было в списке). На практике сообщения без
# явного телефона/email почти никогда не получали ни одной лид-подсказки и
# либо неверно уходили в NON_LEAD, либо ошибочно не находили языковых
# сигналов для правил группировки ниже (CONTINUATION_RE/NEW_LEAD_RE делят ту
# же проблему). Добавлен также корень "интерес" — раньше был только
# "заинтерес", то есть "интересуется"/"интересует" не ловились вовсе; именно
# это привело к тому, что сообщение-уточнение "Интересуется проект игры" в
# логе получило decision=NON_LEAD reason=no_contact_or_lead_evidence.
LEAD_HINT_RE = re.compile(
    r"\b(контакт\w*|визитк\w*|клиент\w*|заказчик\w*|партн[её]р\w*|директор\w*|"
    r"руководител\w*|компани\w*|телефон\w*|почт\w*|e-?mail\w*|заинтерес\w*|"
    r"интерес\w*|перезвон\w*|встрети\w*|встреч\w*|созвон\w*|познаком\w*|"
    r"подош[её]л\w*|проси\w*|предложени\w*|презентаци\w*|прайс\w*|бюджет\w*|"
    r"кп\b|демо\b|commercial proposal|customer|partner\w*)\b",
    re.I,
)
NEW_LEAD_RE = re.compile(
    r"\b(нов\w*\s+контакт\w*|нов\w*\s+клиент\w*|нов\w*\s+партн[её]р\w*|"
    r"нов\w*\s+посетител\w*|нов\w*\s+визитк\w*|следующ\w*\s+контакт\w*|"
    r"следующ\w*\s+клиент\w*|следующ\w*\s+посетител\w*|ещ[её]\s+одн\w*\s+контакт\w*|"
    r"друг\w*\s+контакт\w*|теперь\s+следующ\w*)\b",
    re.I,
)

CONTINUATION_RE = re.compile(
    r"\b(забыл(?:а)? сказать|добавлю|дополню|"
    r"уточн(?:ю|ение)|ещ[её] по нему|ещ[её] по ней|"
    r"по этому контакту|также просил|ещ[её] просил)\b",
    re.I,
)
# ДОБАВЛЕНО: слова, которыми менеджер ВВОДИТ нового человека. Если они есть,
# короткая реплика без реквизитов может оказаться и новым контактом — такие
# случаи по-прежнему отдаём LLM, а не решаем правилом.
NEW_PERSON_HINT_RE = re.compile(
    r"\b(подош[её]л\w*|подошл\w*|встрети\w*|познаком\w*|подвёл\w*|подвел\w*|"
    r"представил\w*|привёл\w*|привел\w*|зашл\w*|заш[её]л\w*)\b",
    re.I,
)
NON_LEAD_RE = re.compile(
    r"\b(стенд закрываем|стенд открываем|ид[её]м на обед|пойд[её]м на обед|"
    r"где обед|кофе|бейдж|смена стенда|расписание стенда|тестовое сообщение|"
    r"проверка связи|доброе утро|всем привет)\b",
    re.I,
)

LLM_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {
            "type": "string",
            "enum": ["ATTACH_TO_GROUP", "NEW_GROUP", "NON_LEAD", "AMBIGUOUS"],
        },
        "target_group_id": {"type": "integer"},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
        "contact_name": {"type": "string"},
        "company": {"type": "string"},
    },
    "required": [
        "decision",
        "target_group_id",
        "confidence",
        "reason",
        "contact_name",
        "company",
    ],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You group noisy Microsoft Teams exhibition messages into contacts.
Return only the requested structured object.

Rules:
1. A thread reply belongs to the root contact unless its content is clearly non-lead.
2. The same normalized email or phone is strong evidence of the same contact.
3. Different emails or phones are strong evidence of different contacts.
4. The same person name plus company is medium evidence of the same contact.
5. A different manager/author alone does not mean a different contact.
6. Time proximity alone is never sufficient: three contacts may arrive in 40 seconds.
7. A voice note, business-card OCR and a short follow-up may be parts of one contact.
8. If a late message such as 'forgot to say, he needs a proposal' can refer to more
   than one group, return AMBIGUOUS. Never guess.
9. target_group_id must be a supplied candidate ID for ATTACH_TO_GROUP and 0 otherwise.
10. contact_name/company must be empty strings when they are not clearly present.
11. already_extracted_contact is the person a candidate group is already about.
    If the new message names a different person, it is a DIFFERENT contact:
    return NEW_GROUP, never ATTACH_TO_GROUP. Two business cards photographed
    seconds apart are two visitors, not one.
12. Do not justify ATTACH_TO_GROUP with a phone or email that is absent from the
    new message. Cite only identifiers that literally appear in it.
"""


def utc_now_dt() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def utc_now() -> str:
    return iso(utc_now_dt())


def parse_dt(value: str | None) -> datetime:
    if not value:
        return utc_now_dt()
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def column_names(db: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}


def ensure_column(db: sqlite3.Connection, table: str, name: str, definition: str) -> None:
    if name not in column_names(db, table):
        db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def init_db(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS lead_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_key TEXT NOT NULL UNIQUE,
            crm_external_key TEXT,
            tenant_id TEXT,
            team_id TEXT,
            channel_id TEXT,
            author_id TEXT,
            author_name TEXT,
            contributors_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'open',
            identity_emails_json TEXT NOT NULL DEFAULT '[]',
            identity_phones_json TEXT NOT NULL DEFAULT '[]',
            identity_names_json TEXT NOT NULL DEFAULT '[]',
            identity_companies_json TEXT NOT NULL DEFAULT '[]',
            group_revision INTEGER NOT NULL DEFAULT 1,
            ready_at TEXT,
            needs_reextract INTEGER NOT NULL DEFAULT 1,
            crm_lead_id TEXT,
            first_message_at TEXT NOT NULL,
            last_message_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS grouping_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_db_id INTEGER NOT NULL UNIQUE,
            lead_group_id INTEGER,
            classification TEXT NOT NULL,
            confidence REAL,
            decision_reason TEXT NOT NULL,
            source_hash TEXT NOT NULL,
            algorithm_version TEXT NOT NULL DEFAULT 'hybrid-v2',
            model_used TEXT,
            notification_status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            FOREIGN KEY(message_db_id) REFERENCES messages(id),
            FOREIGN KEY(lead_group_id) REFERENCES lead_groups(id)
        );

        CREATE TABLE IF NOT EXISTS group_key_registry (
            message_db_id INTEGER PRIMARY KEY,
            group_key TEXT NOT NULL,
            crm_external_key TEXT,
            last_group_revision INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )

    # Safe migrations from the earlier prototype schema.
    group_columns = {
        "crm_external_key": "TEXT",
        "tenant_id": "TEXT",
        "team_id": "TEXT",
        "channel_id": "TEXT",
        "contributors_json": "TEXT NOT NULL DEFAULT '[]'",
        "identity_names_json": "TEXT NOT NULL DEFAULT '[]'",
        "identity_companies_json": "TEXT NOT NULL DEFAULT '[]'",
        "group_revision": "INTEGER NOT NULL DEFAULT 1",
        "ready_at": "TEXT",
        "needs_reextract": "INTEGER NOT NULL DEFAULT 1",
        "crm_lead_id": "TEXT",
    }
    for name, definition in group_columns.items():
        ensure_column(db, "lead_groups", name, definition)

    result_columns = {
        "confidence": "REAL",
        "algorithm_version": "TEXT NOT NULL DEFAULT 'hybrid-v2'",
        "model_used": "TEXT",
        "notification_status": "TEXT NOT NULL DEFAULT 'pending'",
    }
    for name, definition in result_columns.items():
        ensure_column(db, "grouping_results", name, definition)

    db.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_lead_groups_channel_time
            ON lead_groups(tenant_id, team_id, channel_id, last_message_at);
        CREATE INDEX IF NOT EXISTS idx_lead_groups_owner_time
            ON lead_groups(author_id, last_message_at);
        CREATE INDEX IF NOT EXISTS idx_grouping_results_group
            ON grouping_results(lead_group_id);
        CREATE INDEX IF NOT EXISTS idx_group_key_registry_group
            ON group_key_registry(group_key);
        """
    )
    db.commit()


def table_exists(db: sqlite3.Connection, table: str) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def stable_keys_for_message(message: sqlite3.Row) -> tuple[str, str]:
    raw = "|".join(
        [
            str(message["tenant_id"] or ""),
            str(message["team_id"] or ""),
            str(message["channel_id"] or ""),
            str(message["message_id"] or f"db-{message['id']}"),
        ]
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return f"group-{digest}", f"teams-{digest}"


def remember_message_group(
    db: sqlite3.Connection,
    message_db_id: int,
    group_key: str,
    crm_external_key: str | None,
    revision: int,
) -> None:
    now = utc_now()
    db.execute(
        """
        INSERT INTO group_key_registry(
            message_db_id, group_key, crm_external_key,
            last_group_revision, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(message_db_id) DO UPDATE SET
            group_key=excluded.group_key,
            crm_external_key=COALESCE(
                group_key_registry.crm_external_key,
                excluded.crm_external_key
            ),
            last_group_revision=MAX(
                group_key_registry.last_group_revision,
                excluded.last_group_revision
            ),
            updated_at=excluded.updated_at
        """,
        (
            message_db_id,
            group_key,
            crm_external_key,
            revision,
            now,
            now,
        ),
    )


def rebuild_derived_grouping(db: sqlite3.Connection) -> None:
    # Preserve the stable group/CRM identity for every source message before
    # deleting derived rows.  Without this registry a rebuild could choose a
    # different first message, generate another external key and create a
    # duplicate lead in Bitrix.
    has_crm_state = table_exists(db, "crm_sync_state")
    if has_crm_state:
        rows = db.execute(
            """
            SELECT result.message_db_id, g.group_key,
                   COALESCE(g.crm_external_key, s.external_key) AS crm_external_key,
                   g.group_revision
            FROM grouping_results result
            JOIN lead_groups g ON g.id = result.lead_group_id
            LEFT JOIN crm_sync_state s ON s.lead_group_id = g.id
            WHERE result.lead_group_id IS NOT NULL
            """
        ).fetchall()
    else:
        rows = db.execute(
            """
            SELECT result.message_db_id, g.group_key,
                   g.crm_external_key, g.group_revision
            FROM grouping_results result
            JOIN lead_groups g ON g.id = result.lead_group_id
            WHERE result.lead_group_id IS NOT NULL
            """
        ).fetchall()

    for row in rows:
        remember_message_group(
            db,
            int(row["message_db_id"]),
            str(row["group_key"]),
            str(row["crm_external_key"]) if row["crm_external_key"] else None,
            int(row["group_revision"]),
        )

    # Source messages/artifacts and the stable identity registry remain intact.
    db.execute("DELETE FROM grouping_results")
    db.execute("DELETE FROM lead_groups")
    db.commit()
    print(
        "GROUPING_REBUILT source_messages_preserved=true stable_keys_preserved=true",
        flush=True,
    )


def source_for_message(
    db: sqlite3.Connection, message_id: int
) -> tuple[str, list[dict[str, Any]]]:
    row = db.execute(
        "SELECT body_text FROM messages WHERE id = ?", (message_id,)
    ).fetchone()
    artifacts = db.execute(
        """
        SELECT artifact_type, raw_text, confidence, file_id
        FROM content_artifacts
        WHERE message_db_id = ? AND status = 'ready'
        ORDER BY id
        """,
        (message_id,),
    ).fetchall()

    parts: list[str] = []
    body = (row[0] if row else "") or ""
    if body.strip():
        parts.append(body.strip())

    artifact_rows: list[dict[str, Any]] = []
    for artifact in artifacts:
        item = {
            "artifact_type": artifact[0],
            "raw_text": artifact[1] or "",
            "confidence": artifact[2],
            "file_id": artifact[3],
        }
        artifact_rows.append(item)
        if item["raw_text"].strip():
            parts.append(item["raw_text"].strip())

    return "\n".join(parts).strip(), artifact_rows


def identities(text: str) -> tuple[set[str], set[str]]:
    emails = {match.group(0).lower() for match in EMAIL_RE.finditer(text)}
    # ДОБАВЛЕНО: адрес, продиктованный голосом ("самал собачка жмэйл ком"),
    # — такой же идентификатор контакта, как и написанный текстом. Без этого
    # голосовое с почтой не совпадало по email ни с визиткой, ни с другим
    # сообщением того же человека, и группировка теряла главный признак.
    emails.update(spoken_emails(text))
    phones: set[str] = set()
    for match in PHONE_RE.finditer(text):
        digits = re.sub(r"\D", "", match.group(0))
        if 7 <= len(digits) <= 15:
            phones.add(digits)
    return emails, phones


# ДОБАВЛЕНО: один и тот же номер менеджеры на выставке вводят по-разному —
# "+7 705 123-45-67" в одном сообщении и "8 705 1234567" (голосом, через
# транскрибацию) в другом. identities() хранит "сырые" цифры без нормализации
# кода страны/выхода на межгород, поэтому "77051234567" и "87051234567" —
# один и тот же номер — раньше считались РАЗНЫМИ идентификаторами: точное
# совпадение в deterministic_decision() не срабатывало, и решение уходило в
# LLM. Именно это, судя по логам пользователя ("similarity in phone numbers"
# в объяснении модели), и стало причиной AMBIGUOUS на голосовом сообщении —
# LLM увидела два "почти одинаковых, но не идентичных" номера у разных
# кандидатов и не рискнула угадать, хотя по сути это один и тот же контакт.
# Для сравнения (не для хранения/отображения) берём последние 10 значащих
# цифр — это национальный номер без кода страны/трансграничного префикса,
# устойчивый и к "+7"/"8", и к необязательному коду страны при местном звонке.
PHONE_MATCH_SIGNIFICANT_DIGITS = 10


def phone_match_keys(phones: set[str]) -> set[str]:
    """Ключи для СРАВНЕНИЯ номеров (не для хранения) — см. комментарий выше."""
    return {
        phone[-PHONE_MATCH_SIGNIFICANT_DIGITS:]
        if len(phone) >= PHONE_MATCH_SIGNIFICANT_DIGITS
        else phone
        for phone in phones
    }


def json_set(value: str | None) -> set[str]:
    try:
        return set(json.loads(value or "[]"))
    except (json.JSONDecodeError, TypeError):
        return set()


def is_attachment_only(body_text: str | None, artifacts: list[dict[str, Any]]) -> bool:
    return not (body_text or "").strip() and any(
        artifact["file_id"] is not None for artifact in artifacts
    )


def classify_content(text: str, artifacts: list[dict[str, Any]]) -> tuple[str, str]:
    clean = text.strip()
    emails, phones = identities(clean)
    if not clean:
        # ИСПРАВЛЕНО: раньше сообщение без единого символа текста ВСЕГДА
        # считалось NON_LEAD "empty_content" — не различая "сообщение и
        # правда пустое" и "к сообщению приложен файл (визитка/голосовое), но
        # OCR/транскрибация не смогли вытащить из него текст". ТЗ прямо
        # описывает первый случай как нормальную часть потока ("Бывает
        # визитка без единого слова комментария") — такое сообщение не
        # должно тихо выбрасываться и никогда не показываться модели
        # извлечения, у которой на этапе extraction_worker есть доступ к
        # самому изображению (через vision-модель), а не только к тексту
        # OCR. Реальный кейс из теста: фото визитки без подписи, EasyOCR не
        # распознал ни одной строки -> раньше NON_LEAD/empty_content, лид
        # терялся до того, как модель вообще увидела фотографию.
        has_real_attachment = any(
            artifact["file_id"] is not None for artifact in artifacts
        )
        if has_real_attachment:
            return "LEAD_PART", "attachment_present_no_readable_text"
        return "NON_LEAD", "empty_content"
    if len(clean) <= 4 and EMOJI_OR_PUNCT_RE.fullmatch(clean):
        return "NON_LEAD", "emoji_or_punctuation_only"
    if NON_LEAD_RE.search(clean) and not (emails or phones or LEAD_HINT_RE.search(clean)):
        return "NON_LEAD", "explicit_organizational_message"
    if emails or phones:
        return "LEAD_PART", "contact_identifier_present"
    has_ml_text = any(
        artifact["artifact_type"] in {"ocr", "transcript"}
        and artifact["raw_text"].strip()
        for artifact in artifacts
    )
    if has_ml_text:
        if NON_LEAD_RE.search(clean) and not LEAD_HINT_RE.search(clean):
            return "NON_LEAD", "artifact_contains_explicit_non_lead_content"
        return "LEAD_PART", "ocr_or_transcript_content_present"
    if LEAD_HINT_RE.search(clean):
        return "LEAD_PART", "lead_language_present"
    return "NON_LEAD", "no_contact_or_lead_evidence"


def parent_group(db: sqlite3.Connection, message: sqlite3.Row) -> sqlite3.Row | None:
    if not message["reply_to_id"]:
        return None
    return db.execute(
        """
        SELECT g.*
        FROM messages parent
        JOIN grouping_results result ON result.message_db_id = parent.id
        JOIN lead_groups g ON g.id = result.lead_group_id
        WHERE parent.tenant_id = ? AND parent.team_id = ? AND parent.channel_id = ?
          AND parent.message_id = ?
        LIMIT 1
        """,
        (
            message["tenant_id"],
            message["team_id"],
            message["channel_id"],
            message["reply_to_id"],
        ),
    ).fetchone()


def follow_up_target(
    db: sqlite3.Connection, message: sqlite3.Row, content_reason: str
) -> tuple[int, str] | None:
    """Куда присоединить приписку, у которой нет собственных признаков лида.

    ДОБАВЛЕНО. Раньше process_one() на классификации NON_LEAD сразу выходил,
    и логика продолжения (тред/тот же автор/короткая пауза) до таких
    сообщений просто не доходила. Из-за этого терялись именно те приписки,
    которые ТЗ (п. 1.1) описывает как нормальную часть контакта: "потом
    приписка вроде «срочно, перезвонить в понедельник»". В логах пользователя
    так потерялись "Проект игры" и "И пн встреча" — обе получили
    NON_LEAD/no_contact_or_lead_evidence и не попали ни в лид, ни в его
    комментарий.

    Сознательно НЕ спасаем сообщения, которые распознаны как явно
    организационные ("стенд закрываем"), как пустые без вложения или как
    один эмодзи: для них отсутствие признаков лида — это и есть ответ.
    """
    if content_reason != "no_contact_or_lead_evidence":
        return None

    # Ответ в треде принадлежит контакту корневого сообщения.
    parent = parent_group(db, message)
    if parent is not None and parent["status"] == "open":
        return int(parent["id"]), "thread_follow_up_without_own_evidence"

    if message["reply_to_id"]:
        return None

    created = parse_dt(message["created_at"])
    threshold = created - timedelta(seconds=FOLLOW_UP_WINDOW_SECONDS)

    rows = db.execute(
        """
        SELECT id, last_message_at FROM lead_groups
        WHERE status = 'open'
          AND tenant_id = ? AND team_id = ? AND channel_id = ?
          AND COALESCE(author_id, '') = COALESCE(?, '')
          AND last_message_at >= ?
          AND last_message_at <= ?
        ORDER BY last_message_at DESC
        LIMIT 2
        """,
        (
            message["tenant_id"],
            message["team_id"],
            message["channel_id"],
            message["author_id"],
            iso(threshold),
            message["created_at"],
        ),
    ).fetchall()

    if not rows:
        return None

    if len(rows) == 1:
        return int(rows[0]["id"]), "recent_same_author_follow_up_without_own_evidence"

    # ИСПРАВЛЕНО: раньше на этом месте было жёсткое `if len(rows) != 1: return
    # None` — то есть любая приписка терялась, стоило менеджеру вести двух
    # посетителей подряд. На выставке это норма, а не исключение: в боевом
    # прогоне так пропали "Проект по экологии" и "Встреча в вт" — за три
    # минуты до них менеджер завёл ещё один контакт, и обе приписки не попали
    # ни в один лид.
    #
    # Приписка почти всегда относится к тому, о ком менеджер только что писал.
    # Поэтому берём самую свежую группу, но только если она ЗАМЕТНО свежее
    # следующей: если два контакта закрыты почти одновременно, выбрать
    # действительно не из чего, и тогда по ТЗ гадать нельзя.
    newest = parse_dt(rows[0]["last_message_at"])
    runner_up = parse_dt(rows[1]["last_message_at"])
    if (newest - runner_up).total_seconds() < FOLLOW_UP_TIEBREAK_SECONDS:
        return None

    return int(rows[0]["id"]), "closest_same_author_group_follow_up_without_own_evidence"


def candidate_groups(db: sqlite3.Connection, message: sqlite3.Row) -> list[sqlite3.Row]:
    threshold = parse_dt(message["created_at"]) - timedelta(hours=ACTIVE_GROUP_HOURS)
    return db.execute(
        """
        SELECT * FROM lead_groups
        WHERE status = 'open'
          AND tenant_id = ? AND team_id = ? AND channel_id = ?
          AND last_message_at >= ?
        ORDER BY
          CASE WHEN COALESCE(author_id, '') = COALESCE(?, '') THEN 0 ELSE 1 END,
          last_message_at DESC
        LIMIT ?
        """,
        (
            message["tenant_id"],
            message["team_id"],
            message["channel_id"],
            iso(threshold),
            message["author_id"],
            MAX_CANDIDATES,
        ),
    ).fetchall()


def group_source(db: sqlite3.Connection, group_id: int) -> list[dict[str, Any]]:
    rows = db.execute(
        """
        SELECT m.id, m.message_id, m.author_id, m.created_at
        FROM grouping_results result
        JOIN messages m ON m.id = result.message_db_id
        WHERE result.lead_group_id = ?
        ORDER BY m.created_at, m.id
        """,
        (group_id,),
    ).fetchall()
    output: list[dict[str, Any]] = []
    for row in rows[-6:]:
        text, _ = source_for_message(db, row["id"])
        output.append(
            {
                "message_db_id": row["id"],
                "teams_message_id": row["message_id"],
                "author_id": row["author_id"],
                "created_at": row["created_at"],
                "content": text[:1500],
            }
        )
    return output


def group_is_attachment_only(db: sqlite3.Connection, group_id: int) -> bool:
    row = db.execute(
        """
        SELECT m.id, m.body_text
        FROM grouping_results result
        JOIN messages m ON m.id = result.message_db_id
        WHERE result.lead_group_id = ?
        ORDER BY m.created_at DESC, m.id DESC LIMIT 1
        """,
        (group_id,),
    ).fetchone()
    if not row:
        return False
    artifacts = db.execute(
        "SELECT 1 FROM content_artifacts WHERE message_db_id = ? AND file_id IS NOT NULL LIMIT 1",
        (row["id"],),
    ).fetchone()
    return not (row["body_text"] or "").strip() and artifacts is not None


def group_extracted_identity(db: sqlite3.Connection, group_id: int) -> dict[str, Any]:
    """ДОБАВЛЕНО: кого этот лид УЖЕ описывает, по данным извлечения.

    Раньше группировка знала о кандидате только то, что успела вытащить
    регулярками из сырого текста (identity_*_json). Имя и компания туда
    попадали лишь в редком случае, когда их вернула LLM. При этом
    extraction_worker давно сохранил для группы проверенные ФИО/компанию/
    телефоны — но grouping_worker в эту таблицу никогда не заглядывал.

    Из-за этого при разборе следующего сообщения ни правила, ни LLM не знали,
    что группа 14 — это уже "Роман Гуцаев, Альфа Групп", и голосовое про
    совершенно другого человека ("Марат Маратович... почта марат@gmail.com")
    спокойно приклеивалось к ней. В карточке лида оказывались два разных
    контакта. Теперь личность группы передаётся и в правила, и в модель.
    """
    row = db.execute(
        """
        SELECT validated_json FROM lead_extractions
        WHERE lead_group_id = ? AND validated_json IS NOT NULL
        ORDER BY group_revision DESC, id DESC
        LIMIT 1
        """,
        (group_id,),
    ).fetchone()
    if not row:
        return {}

    try:
        validated = json.loads(row["validated_json"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}

    def single(name: str) -> str:
        field = validated.get(name) or {}
        value = field.get("value") if isinstance(field, dict) else None
        return str(value).strip() if value else ""

    def multiple(name: str) -> list[str]:
        values: list[str] = []
        for item in validated.get(name) or []:
            if isinstance(item, dict) and item.get("value"):
                values.append(str(item["value"]).strip())
        return values

    identity = {
        "full_name": single("full_name"),
        "company": single("company"),
        "emails": multiple("emails"),
        "phones": multiple("phones"),
    }
    return {key: value for key, value in identity.items() if value}


def candidate_payload(db: sqlite3.Connection, candidates: list[sqlite3.Row]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for group in candidates:
        payload.append(
            {
                "group_id": group["id"],
                "owner_author_id": group["author_id"],
                "contributors": sorted(json_set(group["contributors_json"])),
                "emails": sorted(json_set(group["identity_emails_json"])),
                "phones": sorted(json_set(group["identity_phones_json"])),
                "names": sorted(json_set(group["identity_names_json"])),
                "companies": sorted(json_set(group["identity_companies_json"])),
                # ДОБАВЛЕНО: проверенная личность лида — главный сигнал "это
                # тот же человек или уже другой".
                "already_extracted_contact": group_extracted_identity(db, int(group["id"])),
                "messages": group_source(db, group["id"]),
            }
        )
    return payload


def llm_decide(
    db: sqlite3.Connection,
    message: sqlite3.Row,
    text: str,
    candidates: list[sqlite3.Row],
) -> dict[str, Any]:
    if not os.getenv("OPENAI_API_KEY"):
        return {
            "decision": "AMBIGUOUS",
            "target_group_id": 0,
            "confidence": 0.0,
            "reason": "llm_not_configured",
            "contact_name": "",
            "company": "",
            "model_used": None,
        }

    try:
        from openai import OpenAI

        client = OpenAI(timeout=25.0)
        payload = {
            "new_message": {
                "message_db_id": message["id"],
                "teams_message_id": message["message_id"],
                "author_id": message["author_id"],
                "reply_to_id": message["reply_to_id"],
                "created_at": message["created_at"],
                "content": text[:2500],
            },
            "candidate_groups": candidate_payload(db, candidates),
        }
        response = client.responses.create(
            model=LLM_MODEL,
            store=False,
            input=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            text={
                "format": {
                    "type": "json_schema",
                    "name": "lead_grouping_decision",
                    "strict": True,
                    "schema": LLM_SCHEMA,
                }
            },
        )
        result = json.loads(response.output_text)
        result["model_used"] = LLM_MODEL
        return validate_llm_decision(result, candidates, text)
    except Exception as exc:
        # Do not log prompts, personal data, credentials or provider response bodies.
        return {
            "decision": "AMBIGUOUS",
            "target_group_id": 0,
            "confidence": 0.0,
            "reason": f"llm_failure:{type(exc).__name__}",
            "contact_name": "",
            "company": "",
            "model_used": LLM_MODEL,
        }


# ДОБАВЛЕНО: проверка обоснования модели по тому же принципу, что и в
# extraction_worker (каждое значение должно подтверждаться цитатой из
# источника). В боевом логе LLM присоединила голосовое к чужой группе с
# объяснением "The same normalized phone number (87759196544)" — притом что
# в самом сообщении никакого номера не было вовсе. Модель придумала
# обоснование, а сервис принял его на веру, потому что уверенность была выше
# порога. Теперь, если модель ссылается на конкретный телефон или email,
# он обязан реально присутствовать в новом сообщении — иначе решение не
# засчитывается.
CLAIMED_PHONE_RE = re.compile(r"(?<!\d)\d[\d\s().-]{5,}\d(?!\d)")


def attach_evidence_is_supported(reason: str, text: str) -> bool:
    claimed_emails = {match.group(0).lower() for match in EMAIL_RE.finditer(reason)}
    claimed_phones: set[str] = set()
    for match in CLAIMED_PHONE_RE.finditer(reason):
        digits = re.sub(r"\D", "", match.group(0))
        if 7 <= len(digits) <= 15:
            claimed_phones.add(digits)

    # Модель не ссылалась на конкретный идентификатор — проверять нечего.
    if not claimed_emails and not claimed_phones:
        return True

    text_emails, text_phones = identities(text)
    if claimed_emails & text_emails:
        return True
    if phone_match_keys(claimed_phones) & phone_match_keys(text_phones):
        return True
    return False


def validate_llm_decision(
    result: dict[str, Any], candidates: list[sqlite3.Row], text: str = ""
) -> dict[str, Any]:
    allowed = {"ATTACH_TO_GROUP", "NEW_GROUP", "NON_LEAD", "AMBIGUOUS"}
    candidate_ids = {int(group["id"]) for group in candidates}
    decision = result.get("decision")
    target = int(result.get("target_group_id") or 0)
    confidence = float(result.get("confidence") or 0.0)

    if decision not in allowed:
        decision, target, confidence = "AMBIGUOUS", 0, 0.0
        result["reason"] = "invalid_llm_decision"
    if decision == "ATTACH_TO_GROUP" and target not in candidate_ids:
        decision, target, confidence = "AMBIGUOUS", 0, 0.0
        result["reason"] = "llm_target_not_in_candidates"
    if decision == "ATTACH_TO_GROUP" and confidence < 0.70:
        decision, target = "AMBIGUOUS", 0
        result["reason"] = "llm_attach_confidence_below_0.70"
    # ДОБАВЛЕНО: см. attach_evidence_is_supported — обоснование "тот же
    # телефон/email" должно подтверждаться самим сообщением.
    if decision == "ATTACH_TO_GROUP" and not attach_evidence_is_supported(
        str(result.get("reason") or ""), text
    ):
        decision, target, confidence = "AMBIGUOUS", 0, 0.0
        result["reason"] = "llm_claimed_identifier_absent_in_message"
    if decision != "ATTACH_TO_GROUP":
        target = 0

    result["decision"] = decision
    result["target_group_id"] = target
    result["confidence"] = max(0.0, min(1.0, confidence))
    result["contact_name"] = str(result.get("contact_name") or "").strip()
    result["company"] = str(result.get("company") or "").strip()
    result["reason"] = str(result.get("reason") or "unspecified")[:250]
    return result


def group_activity_before(
    db: sqlite3.Connection, group_id: int, moment: str
) -> datetime | None:
    """ДОБАВЛЕНО: когда в этой группе в последний раз писали ДО указанного
    момента. None — до этого момента группы ещё не существовало.

    Нужно для догрузки за уже обработанный период (сценарий прямо назван в
    ТЗ). Поле lead_groups.last_message_at хранит САМОЕ ПОЗДНЕЕ сообщение
    группы, поэтому при разборе старого догруженного сообщения "ближайшей"
    ошибочно оказывалась группа, созданная ЧАСОМ ПОЗЖЕ него. Приписка
    "ещё по нему: нужен прайс", отправленная в 10:00, уезжала к контакту,
    с которым менеджер познакомился в 11:00. В живом потоке этого не видно —
    там сообщения всегда приходят по возрастанию времени, — а на догрузке
    видно сразу.
    """
    row = db.execute(
        """
        SELECT MAX(m.created_at) AS last_at
        FROM grouping_results result
        JOIN messages m ON m.id = result.message_db_id
        WHERE result.lead_group_id = ? AND m.created_at <= ?
        """,
        (group_id, moment),
    ).fetchone()
    if not row or not row["last_at"]:
        return None
    return parse_dt(row["last_at"])


def closest_same_author_group(
    db: sqlite3.Connection,
    message: sqlite3.Row,
    candidates: list[sqlite3.Row],
) -> sqlite3.Row | None:
    """ИСПРАВЛЕНО: раньше правила для сообщений того же менеджера работали
    только когда у него открыт РОВНО ОДИН контакт (`len(same_author) == 1`).
    На выставке менеджер ведёт нескольких посетителей подряд, поэтому почти
    все дописки и части контакта проваливались мимо правил — в LLM, а оттуда
    нередко в AMBIGUOUS.

    Теперь берём самый свежий контакт этого менеджера, но только если он
    заметно свежее следующего: когда два контакта закрыты почти одновременно,
    выбирать действительно не из чего, и по ТЗ гадать нельзя — решение уходит
    в LLM, а затем при необходимости на страницу "Без лида".
    """
    moment = message["created_at"]
    # Учитываем только активность ДО самого сообщения (см. group_activity_before).
    scored: list[tuple[datetime, sqlite3.Row]] = []
    for group in candidates:
        if (group["author_id"] or "") != (message["author_id"] or ""):
            continue
        activity = group_activity_before(db, int(group["id"]), moment)
        if activity is not None:
            scored.append((activity, group))

    if not scored:
        return None
    if len(scored) == 1:
        return scored[0][1]

    scored.sort(key=lambda item: item[0], reverse=True)
    separation = (scored[0][0] - scored[1][0]).total_seconds()
    if separation < FOLLOW_UP_TIEBREAK_SECONDS:
        return None
    return scored[0][1]


def deterministic_decision(
    db: sqlite3.Connection,
    message: sqlite3.Row,
    text: str,
    artifacts: list[dict[str, Any]],
    candidates: list[sqlite3.Row],
    content_reason: str,  # ДОБАВЛЕНО: нужен, чтобы отличить "вложение без
    # читаемого текста" от вложения, из которого OCR всё-таки достал визитку.
) -> dict[str, Any] | None:
    parent = parent_group(db, message)
    if parent is not None:
        return {
            "decision": "ATTACH_TO_GROUP",
            "target_group_id": parent["id"],
            "confidence": 1.0,
            "reason": "thread_root_group",
            "contact_name": "",
            "company": "",
            "model_used": None,
        }

    incoming_emails, incoming_phones = identities(text)
    # ИСПРАВЛЕНО: сравниваем телефоны по нормализованным ключам
    # (phone_match_keys), а не по сырым строкам цифр — иначе один и тот же
    # номер в разных форматах ("+7..." / "8...") считался разными людьми.
    incoming_phone_keys = phone_match_keys(incoming_phones)
    matches: list[sqlite3.Row] = []
    for group in candidates:
        if incoming_emails & json_set(group["identity_emails_json"]):
            matches.append(group)
            continue
        if incoming_phone_keys & phone_match_keys(json_set(group["identity_phones_json"])):
            matches.append(group)

    unique_matches = {int(group["id"]): group for group in matches}
    if len(unique_matches) == 1:
        target = next(iter(unique_matches.values()))
        return {
            "decision": "ATTACH_TO_GROUP",
            "target_group_id": target["id"],
            "confidence": 1.0,
            "reason": "exact_email_or_phone_match",
            "contact_name": "",
            "company": "",
            "model_used": None,
        }
    if len(unique_matches) > 1:
        return {
            "decision": "AMBIGUOUS",
            "target_group_id": 0,
            "confidence": 0.0,
            "reason": "identifier_matches_multiple_groups",
            "contact_name": "",
            "company": "",
            "model_used": None,
        }

    if not candidates:
        return {
            "decision": "NEW_GROUP",
            "target_group_id": 0,
            "confidence": 0.95,
            "reason": "no_open_candidate_groups",
            "contact_name": "",
            "company": "",
            "model_used": None,
        }

    if NEW_LEAD_RE.search(text):
        return {
            "decision": "NEW_GROUP",
            "target_group_id": 0,
            "confidence": 0.95,
            "reason": "explicit_new_contact_language",
            "contact_name": "",
            "company": "",
            "model_used": None,
        }

    candidate = closest_same_author_group(db, message, candidates)
    if candidate is not None:
        # ИСПРАВЛЕНО: разрыв считаем до последнего сообщения группы,
        # случившегося ДО текущего, а не до самого позднего вообще —
        # иначе при догрузке старого периода разрыв получался
        # отрицательным и правила срабатывали не на той группе.
        candidate_activity = group_activity_before(
            db, int(candidate["id"]), message["created_at"]
        ) or parse_dt(candidate["last_message_at"])
        gap = max(
            0.0,
            (parse_dt(message["created_at"]) - candidate_activity).total_seconds(),
        )
        if CONTINUATION_RE.search(text):
            return {
                "decision": "ATTACH_TO_GROUP",
                "target_group_id": candidate["id"],
                "confidence": 0.90,
                "reason": "single_same_author_group_and_continuation_language",
                "contact_name": "",
                "company": "",
                "model_used": None,
            }
        # ИСПРАВЛЕНО: это правило склеивало по времени РАЗНЫХ людей.
        #
        # Оно задумано под случай из ТЗ: голосовое и фото визитки ОДНОГО
        # человека, отправленные подряд без общего текста — у каждой части
        # самой по себе нет личности, поэтому их объединяет близость по
        # времени. Но ровно так же на выставке выглядит и другой сценарий:
        # менеджер сфотографировал визитку посетителя A, а через минуту —
        # визитку посетителя B. Обе части "только вложение", тот же автор,
        # разрыв меньше окна — и два разных контакта попадали в один лид.
        # Это прямо противоречит принципу ТЗ "близость по времени сама по
        # себе никогда не достаточна: три контакта могут прийти за 40 секунд".
        #
        # Теперь правило работает только там, где оно и задумано — когда
        # склеивать нечего с чем спутать:
        #   * у нового сообщения нет своих реквизитов, противоречащих группе
        #     (точное совпадение реквизитов обработано выше и уже вернуло бы
        #     ATTACH, значит любые свои реквизиты здесь — это НЕ совпадение);
        #   * у группы ещё нет установленной личности, либо у нового
        #     сообщения нет собственного читаемого содержимого.
        # Всё остальное уходит в LLM, которая теперь видит личность группы
        # (см. group_extracted_identity) и может сказать "это другой человек".
        candidate_identity = group_extracted_identity(db, int(candidate["id"]))
        incoming_has_own_identifiers = bool(incoming_emails or incoming_phones)
        incoming_is_contentless = content_reason == "attachment_present_no_readable_text"
        candidate_is_identified = bool(
            candidate_identity.get("full_name")
            or candidate_identity.get("emails")
            or candidate_identity.get("phones")
            or json_set(candidate["identity_emails_json"])
            or json_set(candidate["identity_phones_json"])
        )

        if (
            gap <= PARTS_WINDOW_SECONDS
            and (
                is_attachment_only(message["body_text"], artifacts)
                or group_is_attachment_only(db, candidate["id"])
            )
            and not incoming_has_own_identifiers
            and (incoming_is_contentless or not candidate_is_identified)
        ):
            return {
                "decision": "ATTACH_TO_GROUP",
                "target_group_id": candidate["id"],
                "confidence": 0.85,
                "reason": "single_same_author_group_and_nearby_attachment_part",
                "contact_name": "",
                "company": "",
                "model_used": None,
            }
        # ДОБАВЛЕНО: короткая приписка к своему же контакту ("И пн встреча",
        # "срочно, перезвонить в понедельник", "Проект игры"). У неё нет
        # собственных реквизитов, значит она физически не может описывать
        # НОВОГО человека — опознать его было бы нечем; и в ней нет слов,
        # которыми менеджер вводит нового посетителя. Раньше такой случай
        # проваливался в LLM: лишний вызов модели и риск AMBIGUOUS (в логах
        # пользователя именно так и терялись приписки).
        # ИСПРАВЛЕНО: правило срабатывало не только на приписках, но и на
        # визитках. Фото визитки другого человека ("Марат Маратович, Green
        # Project, директор") не содержит ни телефона, ни email, ни слов
        # "подошёл/познакомился" — и спокойно приклеивалось к чужому контакту.
        # Приписка — это то, что менеджер НАБРАЛ руками вдогонку; сообщение с
        # вложением (визитка, голосовое) несёт собственную личность и должно
        # разбираться по существу, а не приклеиваться по близости во времени.
        is_typed_note = bool((message["body_text"] or "").strip()) and not any(
            artifact["file_id"] is not None for artifact in artifacts
        )
        if (
            gap <= PARTS_WINDOW_SECONDS
            and is_typed_note
            and not (incoming_emails or incoming_phones)
            and not NEW_PERSON_HINT_RE.search(text)
        ):
            return {
                "decision": "ATTACH_TO_GROUP",
                "target_group_id": candidate["id"],
                "confidence": 0.80,
                "reason": "single_same_author_group_and_nearby_note_without_identifiers",
                "contact_name": "",
                "company": "",
                "model_used": None,
            }

    incoming_ids = incoming_emails | incoming_phones
    identified_candidates = [
        group
        for group in candidates
        if json_set(group["identity_emails_json"]) or json_set(group["identity_phones_json"])
    ]
    if incoming_ids and identified_candidates and len(identified_candidates) == len(candidates):
        return {
            "decision": "NEW_GROUP",
            "target_group_id": 0,
            "confidence": 0.95,
            "reason": "different_identifier_from_all_candidates",
            "contact_name": "",
            "company": "",
            "model_used": None,
        }

    return None


def create_group(
    db: sqlite3.Connection,
    message: sqlite3.Row,
    emails: set[str],
    phones: set[str],
    contact_name: str,
    company: str,
) -> int:
    now = utc_now_dt()
    default_group_key, default_crm_key = stable_keys_for_message(message)
    registry = db.execute(
        """
        SELECT group_key, crm_external_key, last_group_revision
        FROM group_key_registry
        WHERE message_db_id = ?
        """,
        (message["id"],),
    ).fetchone()

    group_key = str(registry["group_key"]) if registry else default_group_key
    crm_external_key = (
        str(registry["crm_external_key"])
        if registry and registry["crm_external_key"]
        else default_crm_key
    )
    group_revision = (
        max(1, int(registry["last_group_revision"]) + 1)
        if registry
        else 1
    )

    # If a corrected grouping splits one historical group into two, only the
    # first new group may reuse the old identity.  The second receives a key
    # based on its own root message and will still be deduplicated by email or
    # phone in Bitrix.
    if db.execute(
        "SELECT 1 FROM lead_groups WHERE group_key = ?",
        (group_key,),
    ).fetchone():
        group_key = default_group_key
        crm_external_key = default_crm_key
        if db.execute(
            "SELECT 1 FROM lead_groups WHERE group_key = ?",
            (group_key,),
        ).fetchone():
            group_key = f"{default_group_key}-{uuid.uuid4().hex[:8]}"

    cursor = db.execute(
        """
        INSERT INTO lead_groups(
            group_key, crm_external_key, tenant_id, team_id, channel_id,
            author_id, author_name, contributors_json, status,
            identity_emails_json, identity_phones_json,
            identity_names_json, identity_companies_json,
            group_revision, ready_at, needs_reextract,
            first_message_at, last_message_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
        """,
        (
            group_key,
            crm_external_key,
            message["tenant_id"],
            message["team_id"],
            message["channel_id"],
            message["author_id"],
            message["author_name"],
            json.dumps([message["author_id"]] if message["author_id"] else []),
            json.dumps(sorted(emails), ensure_ascii=False),
            json.dumps(sorted(phones), ensure_ascii=False),
            json.dumps([contact_name] if contact_name else [], ensure_ascii=False),
            json.dumps([company] if company else [], ensure_ascii=False),
            group_revision,
            iso(now + timedelta(seconds=DEBOUNCE_SECONDS)),
            message["created_at"],
            message["created_at"],
            iso(now),
            iso(now),
        ),
    )
    group_id = int(cursor.lastrowid)
    remember_message_group(
        db,
        int(message["id"]),
        group_key,
        crm_external_key,
        group_revision,
    )
    return group_id


def update_group(
    db: sqlite3.Connection,
    group_id: int,
    message: sqlite3.Row,
    emails: set[str],
    phones: set[str],
    contact_name: str,
    company: str,
) -> None:
    group = db.execute("SELECT * FROM lead_groups WHERE id = ?", (group_id,)).fetchone()
    now = utc_now_dt()
    contributors = json_set(group["contributors_json"])
    if message["author_id"]:
        contributors.add(message["author_id"])
    names = json_set(group["identity_names_json"])
    companies = json_set(group["identity_companies_json"])
    if contact_name:
        names.add(contact_name)
    if company:
        companies.add(company)

    db.execute(
        """
        UPDATE lead_groups
        SET contributors_json = ?, identity_emails_json = ?, identity_phones_json = ?,
            identity_names_json = ?, identity_companies_json = ?,
            group_revision = group_revision + 1,
            ready_at = ?, needs_reextract = 1,
            last_message_at = CASE WHEN last_message_at < ? THEN ? ELSE last_message_at END,
            updated_at = ?
        WHERE id = ?
        """,
        (
            json.dumps(sorted(contributors), ensure_ascii=False),
            json.dumps(sorted(json_set(group["identity_emails_json"]) | emails), ensure_ascii=False),
            json.dumps(sorted(json_set(group["identity_phones_json"]) | phones), ensure_ascii=False),
            json.dumps(sorted(names), ensure_ascii=False),
            json.dumps(sorted(companies), ensure_ascii=False),
            iso(now + timedelta(seconds=DEBOUNCE_SECONDS)),
            message["created_at"],
            message["created_at"],
            iso(now),
            group_id,
        ),
    )
    remember_message_group(
        db,
        int(message["id"]),
        str(group["group_key"]),
        str(group["crm_external_key"]) if group["crm_external_key"] else None,
        int(group["group_revision"]) + 1,
    )


def save_result(
    db: sqlite3.Connection,
    message_id: int,
    group_id: int | None,
    decision: str,
    confidence: float,
    reason: str,
    source_hash: str,
    model_used: str | None,
) -> None:
    db.execute(
        """
        INSERT OR IGNORE INTO grouping_results(
            message_db_id, lead_group_id, classification, confidence,
            decision_reason, source_hash, algorithm_version,
            model_used, notification_status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
        """,
        (
            message_id,
            group_id,
            decision,
            confidence,
            reason,
            source_hash,
            ALGORITHM_VERSION,
            model_used,
            utc_now(),
        ),
    )


def process_one(db: sqlite3.Connection, message: sqlite3.Row) -> tuple[str, int | None, str]:
    text, artifacts = source_for_message(db, message["id"])
    source_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    content_class, content_reason = classify_content(text, artifacts)

    if content_class == "NON_LEAD":
        # ДОБАВЛЕНО: прежде чем окончательно списать сообщение в не-лиды,
        # проверяем, не является ли оно припиской к уже открытому контакту
        # (см. follow_up_target).
        follow_up = follow_up_target(db, message, content_reason)

        if follow_up is None:
            save_result(
                db, message["id"], None, "NON_LEAD", 1.0, content_reason, source_hash, None
            )
            db.commit()
            return "NON_LEAD", None, content_reason

        group_id, follow_up_reason = follow_up
        emails, phones = identities(text)
        update_group(db, group_id, message, emails, phones, "", "")
        save_result(
            db,
            message["id"],
            group_id,
            "ATTACH_TO_GROUP",
            0.80,
            f"{content_reason}; {follow_up_reason}",
            source_hash,
            None,
        )
        db.commit()
        return "ATTACH_TO_GROUP", group_id, follow_up_reason

    candidates = candidate_groups(db, message)
    result = deterministic_decision(
        db, message, text, artifacts, candidates, content_reason
    )
    if result is None:
        result = llm_decide(db, message, text, candidates)

    decision = result["decision"]
    group_id: int | None = None
    emails, phones = identities(text)

    if decision == "NEW_GROUP":
        group_id = create_group(
            db, message, emails, phones, result["contact_name"], result["company"]
        )
    elif decision == "ATTACH_TO_GROUP":
        group_id = int(result["target_group_id"])
        update_group(
            db,
            group_id,
            message,
            emails,
            phones,
            result["contact_name"],
            result["company"],
        )

    reason = f"{content_reason}; {result['reason']}"
    save_result(
        db,
        message["id"],
        group_id,
        decision,
        float(result["confidence"]),
        reason,
        source_hash,
        result.get("model_used"),
    )
    db.commit()
    return decision, group_id, result["reason"]


def process_available(db: sqlite3.Connection) -> int:
    messages = db.execute(
        """
        SELECT m.*
        FROM messages m
        LEFT JOIN grouping_results result ON result.message_db_id = m.id
        WHERE result.id IS NULL
          AND m.processing_status = 'content_ready'
        ORDER BY m.created_at, m.id
        """
    ).fetchall()

    for message in messages:
        decision, group_id, reason = process_one(db, message)
        # Intentionally no message text, contact data, author name or credentials in logs.
        print(
            f"message_db_id={message['id']} decision={decision} "
            f"lead_group_id={group_id} reason={reason}",
            flush=True,
        )
    return len(messages)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="process available messages and exit")
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="delete only derived grouping rows and rebuild them from source messages",
    )
    args = parser.parse_args()

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    init_db(db)
    if args.rebuild:
        rebuild_derived_grouping(db)

    if args.once:
        count = process_available(db)
        print(f"GROUPING_DONE processed={count}")
        return

    print("GROUPING_WORKER_STARTED algorithm=hybrid-v2", flush=True)
    while True:
        try:
            process_available(db)
        except Exception as exc:
            print(f"GROUPING_ERROR type={type(exc).__name__}", flush=True)
            db.rollback()
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
