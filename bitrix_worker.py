import argparse
import hashlib
import json
import os
import re
import sqlite3
import time
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

# ДОБАВЛЕНО: текст предупреждений об адресах для комментария лида.
from email_hygiene import email_warnings, warnings_as_text
from lead_rules import position_quote_supports, priority_quote_supports
from dotenv import load_dotenv


load_dotenv(".env")

DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "data/messages.db"))
BITRIX_WEBHOOK_URL = os.environ["BITRIX_WEBHOOK_URL"].rstrip("/") + "/"
TEAMS_WORKFLOW_WEBHOOK_URL = os.environ["TEAMS_WORKFLOW_WEBHOOK_URL"]
POLL_SECONDS = int(os.getenv("BITRIX_POLL_SECONDS", "5"))
MAX_RETRIES = int(os.getenv("BITRIX_RETRY_LIMIT", "8"))

SOURCE_ID = os.getenv("BITRIX_SOURCE_ID", "EXHIBITION")
# ДОБАВЛЕНО: тот же маркер, что читает poller.py — карточка-подтверждение
# помечает себя как служебное сообщение сервиса.
SERVICE_MARKER = os.getenv("SERVICE_MESSAGE_MARKER", "[LEADBOT]")
EXHIBITION_ID = os.environ["BITRIX_EXHIBITION_ID"]

LEAD_TYPE_ENUM = json.loads(
    os.getenv("BITRIX_LEAD_TYPE_ENUM_JSON", '{"Partner":45,"Customer":47}')
)
PRIORITY_ENUM = json.loads(os.getenv("BITRIX_PRIORITY_ENUM_JSON", "{}"))
REGION_ENUM = json.loads(os.getenv("BITRIX_REGION_ENUM_JSON", "{}"))
PRODUCT_ENUM = json.loads(os.getenv("BITRIX_PRODUCT_ENUM_JSON", "{}"))

BITRIX_REQUESTS_PER_SECOND = float(
    os.getenv("BITRIX_REQUESTS_PER_SECOND", "1.5")
)

BITRIX_MIN_REQUEST_INTERVAL = (
    1.0 / BITRIX_REQUESTS_PER_SECOND
)

_last_bitrix_request_at = 0.0

class SyncError(Exception):
    def __init__(self, code: str, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect_database() -> sqlite3.Connection:
    db = sqlite3.connect(DATABASE_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    return db


def initialize_database(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS employee_mappings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            teams_author_id TEXT NOT NULL UNIQUE,
            teams_display_name TEXT,
            bitrix_user_id INTEGER NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS crm_sync_state (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            external_key TEXT NOT NULL UNIQUE,
            lead_group_id INTEGER NOT NULL,
            identity_fingerprint TEXT,
            bitrix_lead_id TEXT,
            synced_revision INTEGER NOT NULL DEFAULT 0,
            timeline_revision INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at REAL NOT NULL DEFAULT 0,
            locked_at REAL,
            error_code TEXT,
            payload_hash TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_crm_sync_status
            ON crm_sync_state(status, next_attempt_at);
        CREATE INDEX IF NOT EXISTS idx_crm_sync_fingerprint
            ON crm_sync_state(identity_fingerprint);

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
    if db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='lead_groups'"
    ).fetchone():
        columns = {
            str(row[1]) for row in db.execute("PRAGMA table_info(lead_groups)")
        }
        if "crm_external_key" not in columns:
            db.execute("ALTER TABLE lead_groups ADD COLUMN crm_external_key TEXT")
        db.execute(
            """
            UPDATE lead_groups
            SET crm_external_key = (
                SELECT state.external_key
                FROM crm_sync_state state
                WHERE state.lead_group_id = lead_groups.id
                ORDER BY state.id DESC
                LIMIT 1
            )
            WHERE crm_external_key IS NULL
            """
        )
    db.commit()

def wait_for_bitrix_rate_limit() -> None:
    global _last_bitrix_request_at

    now = time.monotonic()
    elapsed = now - _last_bitrix_request_at
    remaining = BITRIX_MIN_REQUEST_INTERVAL - elapsed

    if remaining > 0:
        time.sleep(remaining)

    _last_bitrix_request_at = time.monotonic()

def bitrix_call(method: str, payload: dict[str, Any] | None = None) -> Any:
    wait_for_bitrix_rate_limit()
    try:
        response = requests.post(
            BITRIX_WEBHOOK_URL + method + ".json",
            json=payload or {},
            timeout=45,
        )
    except requests.RequestException as exc:
        raise SyncError("bitrix_network_error", retryable=True) from exc

    if response.status_code in {408, 409, 423, 429, 500, 502, 503, 504}:
        raise SyncError(f"bitrix_http_{response.status_code}", retryable=True)
    if response.status_code != 200:
        raise SyncError(f"bitrix_http_{response.status_code}")

    try:
        data = response.json()
    except ValueError as exc:
        raise SyncError("bitrix_invalid_json", retryable=True) from exc

    if data.get("error"):
        code = str(data.get("error"))
        retryable = code in {
            "QUERY_LIMIT_EXCEEDED",
            "INTERNAL_SERVER_ERROR",
            "ERROR_CORE",
        }
        raise SyncError(f"bitrix_{code}", retryable=retryable)

    return data.get("result")


def send_teams_confirmation(
    lead_id: str,
    action: str,
    author_name: str,
) -> None:
    action_text = {
        "created": "создан",
        "updated": "обновлён",
        "merged": "объединён с существующим лидом",
    }.get(action, "обработан")

    payload = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "contentUrl": None,
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": [
                    {
                        "type": "TextBlock",
                        "text": "✅ Лид передан в Bitrix24",
                        "weight": "Bolder",
                        "size": "Medium",
                    },
                    {
                        "type": "FactSet",
                        "facts": [
                            {
                                "title": "CRM ID:",
                                "value": str(lead_id),
                            },
                            {
                                "title": "Результат:",
                                "value": action_text,
                            },
                            {
                                "title": "Ответственный:",
                                "value": author_name or "не определён",
                            },
                        ],
                    },
                    # ДОБАВЛЕНО: служебный маркер, по которому poller.py
                    # узнаёт собственные сообщения сервиса и не пытается
                    # сделать из них ещё один лид (см. SERVICE_MESSAGE_MARKER
                    # и determine_ignore_reason в poller.py).
                    {
                        "type": "TextBlock",
                        "text": SERVICE_MARKER,
                        "size": "Small",
                        "isSubtle": True,
                        "wrap": True,
                    },
                ],
            },
        }],
    }

    try:
        response = requests.post(
            TEAMS_WORKFLOW_WEBHOOK_URL,
            json=payload,
            timeout=30,
        )
    except requests.RequestException as exc:
        raise SyncError(
            "teams_webhook_network_error",
            retryable=True,
        ) from exc

    if response.status_code not in {200, 202}:
        retryable = response.status_code in {
            408, 409, 423, 429, 500, 502, 503, 504
        }
        raise SyncError(
            f"teams_webhook_http_{response.status_code}",
            retryable=retryable,
        )



def json_object(raw: Any, default: Any) -> Any:
    # ИСПРАВЛЕНО: раньше функция предполагала, что `raw` — всегда строка.
    # Bitrix возвращает множественное поле уже как настоящий list/dict (а не
    # JSON-строку), поэтому json.loads(list) падал с TypeError и функция молча
    # возвращала default — то есть уже накопленные UF_CRM_TEAMS_MESSAGE_IDS
    # обнулялись бы при каждом слиянии дублей, если поле в Bitrix множественное.
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw or "")
    except (json.JSONDecodeError, TypeError):
        return default


def normalized_text(value: str | None) -> str:
    return " ".join((value or "").casefold().split())


def source_message_ids(extraction: sqlite3.Row) -> list[str]:
    values = json_object(extraction["source_message_ids_json"], [])
    return [str(value) for value in values if str(value).strip()]


def stable_external_key(group: sqlite3.Row, extraction: sqlite3.Row) -> str:
    if "crm_external_key" in group.keys() and group["crm_external_key"]:
        return str(group["crm_external_key"])
    message_ids = source_message_ids(extraction)
    first_message_id = message_ids[0] if message_ids else f"group-{group['id']}"
    raw = "|".join(
        [
            str(group["tenant_id"] or ""),
            str(group["team_id"] or ""),
            str(group["channel_id"] or ""),
            first_message_id,
        ]
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return f"teams-{digest}"


def field_value(validated: dict[str, Any], name: str) -> str | None:
    field = validated.get(name) or {}
    value = field.get("value") if isinstance(field, dict) else None
    return str(value).strip() if value else None


def list_values(validated: dict[str, Any], name: str) -> list[str]:
    result: list[str] = []
    for item in validated.get(name) or []:
        if isinstance(item, dict) and item.get("value"):
            result.append(str(item["value"]).strip())
    return result


def email_values(validated: dict[str, Any]) -> list[str]:
    """Return only addresses safe enough to write to CRM.

    This is a second safety boundary for extractions saved by an older
    version of the service.  Retrying such a lead clears gmail.con and other
    blocked values instead of sending them to Bitrix again.
    """
    return [
        value
        for value in list_values(validated, "emails")
        if not email_warnings(value)
    ]


def field_evidence_quotes(validated: dict[str, Any], name: str) -> list[str]:
    field = validated.get(name) or {}
    if not isinstance(field, dict):
        return []
    return [
        str(item.get("quote") or "")
        for item in field.get("evidence") or []
        if isinstance(item, dict)
    ]


def identity_fingerprint(validated: dict[str, Any]) -> str | None:
    emails = sorted(
        {
            value.casefold()
            for value in email_values(validated)
            if value.strip()
        }
    )
    if emails:
        return "email:" + emails[0]

    phones: list[str] = []

    for phone in list_values(validated, "phones"):
        digits = re.sub(r"\D", "", phone)

        if digits:
            # Ключ используется только для сравнения. +7 705... и
            # 8 705... — один национальный номер, поэтому сравниваем по
            # последним десяти значащим цифрам. Исходное значение в CRM не
            # изменяем и код страны не придумываем.
            phones.append(digits[-10:] if len(digits) >= 10 else digits)

    if phones:
        return "phone:" + sorted(set(phones))[0]

    # Имя + компания недостаточны для автоматического объединения.
    # Такой случай позднее помечается possible_duplicate.
    return None

def load_candidates(db: sqlite3.Connection) -> list[tuple[sqlite3.Row, sqlite3.Row]]:
    rows = db.execute(
        """
        SELECT
            g.*,
            e.id AS extraction_id,
            e.status AS extraction_status,
            e.validated_json,
            e.raw_transcripts_json,
            e.source_message_ids_json,
            e.group_revision AS extraction_revision
        FROM lead_groups g
        JOIN lead_extractions e
          ON e.lead_group_id = g.id
         AND e.group_revision = g.group_revision
        WHERE e.status = 'ready'
          AND e.is_lead = 1
        ORDER BY g.id
        """
    ).fetchall()
    return [(row, row) for row in rows]


def employee_id(db: sqlite3.Connection, author_id: str | None) -> int:
    row = db.execute(
        """
        SELECT bitrix_user_id
        FROM employee_mappings
        WHERE teams_author_id = ? AND active = 1
        """,
        (author_id or "",),
    ).fetchone()
    if not row:
        raise SyncError("employee_mapping_required")
    return int(row["bitrix_user_id"])


def field_metadata() -> dict[str, bool]:
    result = bitrix_call("crm.lead.userfield.list") or []
    return {
        str(field.get("FIELD_NAME")): str(field.get("MULTIPLE", "N")) == "Y"
        for field in result
    }


def enum_field(
    values: list[str],
    mapping: dict[str, Any],
    multiple: bool,
) -> Any:
    mapped = [mapping[value] for value in values if value in mapping]
    if not mapped:
        return None
    return mapped if multiple else mapped[0]


def full_input_comment(
    db: sqlite3.Connection,
    group: sqlite3.Row,
    extraction: sqlite3.Row,
) -> str:
    """Полный ввод менеджера: набранный текст + дословная расшифровка голоса.

    ИСПРАВЛЕНО: раньше эта функция называлась transcript_comment() и собирала
    COMMENTS ТОЛЬКО из raw_transcripts_json, то есть исключительно из
    голосовых. Набранный текст менеджера в карточку лида не попадал вообще, а
    для контакта без единого голосового (как "Иван иванович 87759196544" +
    приписки в логах) COMMENTS оставался пустым — при том что ТЗ, п. 3.1,
    требует в этом поле именно "Полный ввод менеджера: набранный текст +
    дословная расшифровка голоса", а раздел 2 относит сохранение исходной
    речи к обязательному результату.

    Блоки идут в хронологическом порядке, первая строка каждого блока —
    стабильный маркер с ID сообщения: на него опирается склейка комментариев
    при объединении дублей в merge_existing_fields().
    """
    transcripts_by_message: dict[str, list[str]] = {}

    for item in json_object(extraction["raw_transcripts_json"], []):
        if not isinstance(item, dict):
            continue
        message_id = str(item.get("message_id") or "")
        text = str(item.get("text") or "").strip()
        if text:
            transcripts_by_message.setdefault(message_id, []).append(text)

    rows = db.execute(
        """
        SELECT
            m.id AS message_db_id,
            m.message_id,
            m.author_name,
            m.created_at,
            m.body_text,
            (
                SELECT COUNT(*) FROM message_files f
                WHERE f.message_db_id = m.id
            ) AS file_count
        FROM grouping_results result
        JOIN messages m ON m.id = result.message_db_id
        WHERE result.lead_group_id = ?
        ORDER BY m.created_at, m.id
        """,
        (group["id"],),
    ).fetchall()

    blocks: list[str] = []
    seen_message_ids: set[str] = set()

    for row in rows:
        message_id = str(row["message_id"])
        seen_message_ids.add(message_id)

        typed_text = (row["body_text"] or "").strip()
        spoken = transcripts_by_message.get(message_id, [])
        file_count = int(row["file_count"] or 0)

        # ДОБАВЛЕНО: сообщение, состоящее из одного вложения (фото визитки без
        # подписи — прямо описанный в ТЗ п. 1.1 случай), раньше полностью
        # выпадало из истории ввода. Теперь в комментарии остаётся след, что
        # в этот момент менеджер прислал файл.
        if not typed_text and not spoken:
            if not file_count:
                continue

        header = f"[Teams message {message_id}]"
        author = (row["author_name"] or "").strip()
        created_at = (row["created_at"] or "").strip()

        if author or created_at:
            header = f"{header} {author} {created_at}".rstrip()

        lines = [header]

        if typed_text:
            lines.append(typed_text)

        for text in spoken:
            lines.append(f"Голосовое, дословно: {text}")

        if file_count and not typed_text and not spoken:
            lines.append(
                f"Вложение без подписи: {file_count} файл(ов) — см. карточку лида в сервисе."
            )

        blocks.append("\n".join(lines))

    # Защитно: расшифровка есть, а сообщения в группе уже нет (перегруппировка).
    for message_id, texts in transcripts_by_message.items():
        if message_id in seen_message_ids:
            continue
        for text in texts:
            blocks.append(
                f"[Teams message {message_id}]\nГолосовое, дословно: {text}"
            )

    return "\n\n".join(blocks)


def build_fields(
    db: sqlite3.Connection,  # ИЗМЕНЕНО: нужен для сбора полного ввода менеджера
    group: sqlite3.Row,
    extraction: sqlite3.Row,
    validated: dict[str, Any],
    assigned_by_id: int,
    metadata: dict[str, bool],
    external_key: str,
) -> dict[str, Any]:
    full_name = field_value(validated, "full_name")
    company = field_value(validated, "company")
    position = field_value(validated, "position")
    if position and not any(
        position_quote_supports(quote)
        for quote in field_evidence_quotes(validated, "position")
    ):
        position = None
    country = field_value(validated, "country")

    title_parts = [value for value in [full_name, company] if value]
    title = " — ".join(title_parts) or f"Teams lead {external_key[-8:]}"

    lead_type = str(validated.get("lead_type") or "Customer")
    if lead_type not in LEAD_TYPE_ENUM:
        lead_type = "Customer"

    # УЛУЧШЕНИЕ: раньше ID сообщений Teams всегда отправлялись как JSON-строка
    # (json.dumps(...)), даже если пользовательское поле UF_CRM_TEAMS_MESSAGE_IDS
    # в самом Bitrix24 создано как множественное ("MULTIPLE": "Y") — именно так
    # оно и показано массивом в примере curl из ТЗ. Множественное поле в Bitrix
    # ожидает настоящий список значений, а не строку с текстом JSON внутри неё.
    # Теперь формат определяется тем же способом, что и для
    # UF_CRM_PRODUCT_INTEREST — через crm.lead.userfield.list.
    message_ids = source_message_ids(extraction)
    message_ids_is_multiple = metadata.get("UF_CRM_TEAMS_MESSAGE_IDS", False)
    message_ids_value: Any = (
        message_ids
        if message_ids_is_multiple
        else json.dumps(message_ids, ensure_ascii=False)
    )

    fields: dict[str, Any] = {
        "TITLE": title,
        "ASSIGNED_BY_ID": assigned_by_id,
        "SOURCE_ID": SOURCE_ID,
        "UF_CRM_LEAD_TYPE": LEAD_TYPE_ENUM[lead_type],
        "UF_CRM_EXHIBITION": int(EXHIBITION_ID),
        "UF_CRM_TEAMS_GROUP_ID": external_key,
        "UF_CRM_TEAMS_MESSAGE_IDS": message_ids_value,
        "UF_CRM_TEAMS_AUTHOR": str(group["author_name"] or ""),
    }

    if full_name:
        # Preserve the source spelling without guessing Western name order.
        fields["NAME"] = full_name
    if company:
        fields["COMPANY_TITLE"] = company
    if position:
        fields["POST"] = position
    if country:
        fields["ADDRESS_COUNTRY"] = country

    comments = full_input_comment(db, group, extraction)

    # ДОБАВЛЕНО: замечания по адресам ("домен похож на опечатку", "в адресе
    # русские буквы") выводим прямо в комментарий лида. ТЗ: "Email вбивают с
    # ошибкой — письмо не доходит, лид считается «холодным» и закрывается".
    # Для данных, сохранённых старой версией, показываем подсказку в истории,
    # но email_values() не допускает такой адрес в поле EMAIL.
    warning_blocks: list[str] = []
    for item in validated.get("emails") or []:
        if not isinstance(item, dict) or not item.get("value"):
            continue
        value = str(item["value"])
        text = warnings_as_text(
            value,
            item.get("warnings") or email_warnings(value),
        )
        if text:
            warning_blocks.append(text)
    if warning_blocks:
        comments = "\n\n".join(
            filter(None, [comments, "ВНИМАНИЕ:", *warning_blocks])
        )

    if comments:
        fields["COMMENTS"] = comments

    emails = email_values(validated)
    if emails:
        fields["EMAIL"] = [
            {"VALUE": value, "VALUE_TYPE": "WORK"} for value in emails
        ]

    phones = list_values(validated, "phones")
    if phones:
        fields["PHONE"] = [
            {"VALUE": value, "VALUE_TYPE": "WORK"} for value in phones
        ]

    priority = validated.get("priority")
    priority_evidence = validated.get("priority_evidence") or {}
    priority_quote = (
        str(priority_evidence.get("quote") or "")
        if isinstance(priority_evidence, dict)
        else ""
    )
    if priority in PRIORITY_ENUM and priority_quote_supports(priority, priority_quote):
        fields["UF_CRM_PRIORITY"] = PRIORITY_ENUM[priority]

    region = validated.get("region")
    if region in REGION_ENUM:
        fields["UF_CRM_REGION"] = REGION_ENUM[region]

    product_value = enum_field(
        list_values(validated, "product_interests"),
        PRODUCT_ENUM,
        metadata.get("UF_CRM_PRODUCT_INTEREST", False),
    )
    if product_value is not None:
        fields["UF_CRM_PRODUCT_INTEREST"] = product_value

    return fields


def crm_ids_from_result(value: Any) -> set[str]:
    result: set[str] = set()
    if isinstance(value, dict):
        for child in value.values():
            result.update(crm_ids_from_result(child))
    elif isinstance(value, list):
        for child in value:
            result.update(crm_ids_from_result(child))
    elif isinstance(value, (str, int)) and str(value).isdigit():
        result.add(str(value))
    return result


def find_by_external_key(external_key: str) -> set[str]:
    result = bitrix_call(
        "crm.lead.list",
        {
            "filter": {"UF_CRM_TEAMS_GROUP_ID": external_key},
            "select": ["ID"],
        },
    ) or []
    return {str(item["ID"]) for item in result if item.get("ID")}


def find_by_communications(validated: dict[str, Any]) -> set[str]:
    lead_ids: set[str] = set()
    for kind, values in [
        ("EMAIL", email_values(validated)),
        ("PHONE", list_values(validated, "phones")),
    ]:
        if not values:
            continue
        result = bitrix_call(
            "crm.duplicate.findbycomm",
            {
                "entity_type": "LEAD",
                "type": kind,
                "values": values,
            },
        )
        if isinstance(result, dict):
            lead_ids.update(crm_ids_from_result(result.get("LEAD", [])))
    return lead_ids


def local_duplicate_id(
    db: sqlite3.Connection,
    fingerprint: str | None,
    external_key: str,
) -> str | None:
    if not fingerprint:
        return None
    row = db.execute(
        """
        SELECT bitrix_lead_id
        FROM crm_sync_state
        WHERE identity_fingerprint = ?
          AND external_key <> ?
          AND bitrix_lead_id IS NOT NULL
          AND status IN ('synced', 'partial', 'retry')
        ORDER BY id
        LIMIT 1
        """,
        (fingerprint, external_key),
    ).fetchone()
    return str(row["bitrix_lead_id"]) if row else None


# ДОБАВЛЕНО: состояние лида в Bitrix перед тем, как его обновлять.
#
# Раньше bitrix_worker БЕЗУСЛОВНО доверял сохранённому bitrix_lead_id: если в
# crm_sync_state лежал id=35, воркер всегда звал crm.lead.update(35) и
# рапортовал "status=synced". Это молча ломалось в двух реальных ситуациях:
#
#   1. Лид удалили в Bitrix вручную (при тестировании это происходит постоянно).
#      Тогда crm.lead.get возвращает ошибку NOT_FOUND, воркер падал в error и
#      уходил в retry — и НИКОГДА не создавал лид заново, потому что id из
#      crm_sync_state не очищался. Контакт навсегда пропадал из CRM.
#   2. Лид уже сконвертирован в сделку/контакт (в портале включён простой режим
#      CRM или робот "создать сделку из лида"). Тогда лид получает
#      STATUS_ID="CONVERTED", исчезает из рабочего списка лидов, а сделка,
#      созданная из него, НЕ обновляется при обновлении лида. Формально
#      crm.lead.update отрабатывает успешно, в лог пишется status=synced, в
#      Teams уходит карточка "Лид передан в Bitrix24 — обновлён", а менеджер
#      не видит новых данных нигде. Ровно это и наблюдалось в прогоне 21.09:
#      lead_group_id=14 revision=5 status=synced bitrix_lead_id=35, карточка
#      "обновлён" — и ничего нового в интерфейсе.
CONVERTED_STATUS_ID = "CONVERTED"


def lead_state(lead_id: str) -> dict[str, Any] | None:
    """Текущее состояние лида. None — лида в Bitrix больше нет (удалён)."""
    try:
        existing = bitrix_call("crm.lead.get", {"id": lead_id})
    except SyncError as error:
        # Bitrix отвечает NOT_FOUND на удалённый лид. Это не сбой связи —
        # повторять запрос бессмысленно, лид нужно создавать заново.
        if "NOT_FOUND" in error.code.upper():
            return None
        raise
    return existing or None


def lead_is_converted(existing: dict[str, Any]) -> bool:
    return str(existing.get("STATUS_ID") or "").upper() == CONVERTED_STATUS_ID


def deal_created_from_lead(lead_id: str) -> str | None:
    """ID сделки, в которую портал сконвертировал лид (если она есть)."""
    result = bitrix_call(
        "crm.deal.list",
        {"filter": {"LEAD_ID": lead_id}, "select": ["ID"], "order": {"ID": "DESC"}},
    ) or []
    for item in result:
        if item.get("ID"):
            return str(item["ID"])
    return None


def merge_existing_fields(
    existing: dict[str, Any],  # ИЗМЕНЕНО: принимаем уже загруженный лид
    fields: dict[str, Any],
    cross_group_duplicate: bool,
    metadata: dict[str, bool],
) -> dict[str, Any]:
    # ИСПРАВЛЕНО: раньше здесь делался ещё один crm.lead.get — второй запрос к
    # порталу на каждое обновление (лимит 2 запроса/сек на портал), причём в
    # ветке обычного обновления его результат даже не использовался.
    merged = dict(fields)

    if not cross_group_duplicate:
        # При обновлении той же Teams-группы текущее извлечение является
        # полным состоянием лида. Удаляем значения, которых больше нет.
        empty_values = {
            "NAME": "",
            "COMPANY_TITLE": "",
            "POST": "",
            "ADDRESS_COUNTRY": "",
            "COMMENTS": "",
            "EMAIL": [],
            "PHONE": [],
            "UF_CRM_REGION": "",
            "UF_CRM_PRODUCT_INTEREST": [],
            "UF_CRM_PRIORITY": "",
        }

        for field_name, empty_value in empty_values.items():
            merged.setdefault(field_name, empty_value)

        return merged

    # Ниже — объединение настоящего дубля, принесённого другим менеджером.
    old_comments = str(existing.get("COMMENTS") or "").strip()
    new_comments = str(merged.get("COMMENTS") or "").strip()

    if old_comments and new_comments:
        blocks = [old_comments]

        for block in new_comments.split("\n\n"):
            marker = block.split("\n", 1)[0]

            if marker and marker not in old_comments:
                blocks.append(block)

        merged["COMMENTS"] = "\n\n".join(blocks)

    # Первый менеджер остаётся ответственным.
    merged.pop("ASSIGNED_BY_ID", None)
    merged.pop("UF_CRM_TEAMS_GROUP_ID", None)
    merged.pop("UF_CRM_TEAMS_AUTHOR", None)

    # Позднее упоминание не перезаписывает проверенные реквизиты.
    for field_name in [
        "NAME",
        "SECOND_NAME",
        "LAST_NAME",
        "COMPANY_TITLE",
        "POST",
        "ADDRESS_COUNTRY",
        "EMAIL",
        "PHONE",
    ]:
        merged.pop(field_name, None)

    old_ids = json_object(existing.get("UF_CRM_TEAMS_MESSAGE_IDS"), [])
    new_ids = json_object(merged.get("UF_CRM_TEAMS_MESSAGE_IDS"), [])
    combined = list(dict.fromkeys([str(x) for x in old_ids + new_ids]))

    # Пишем обратно в том же формате, в котором поле реально хранится в
    # Bitrix (см. комментарий в build_fields про UF_CRM_TEAMS_MESSAGE_IDS).
    merged["UF_CRM_TEAMS_MESSAGE_IDS"] = (
        combined
        if metadata.get("UF_CRM_TEAMS_MESSAGE_IDS", False)
        else json.dumps(combined, ensure_ascii=False)
    )

    return merged

def upsert_processing_state(
    db: sqlite3.Connection,
    external_key: str,
    group_id: int,
    fingerprint: str | None,
) -> sqlite3.Row:
    now = utc_now()
    db.execute(
        """
        INSERT INTO crm_sync_state(
            external_key, lead_group_id, identity_fingerprint,
            status, attempts, locked_at, created_at, updated_at
        ) VALUES (?, ?, ?, 'processing', 1, strftime('%s','now'), ?, ?)
        ON CONFLICT(external_key) DO UPDATE SET
            lead_group_id=excluded.lead_group_id,
            identity_fingerprint=excluded.identity_fingerprint,
            status='processing',
            attempts=crm_sync_state.attempts + 1,
            locked_at=strftime('%s','now'),
            error_code=NULL,
            updated_at=excluded.updated_at
        """,
        (external_key, group_id, fingerprint, now, now),
    )
    db.execute(
        "UPDATE lead_groups SET crm_external_key=? WHERE id=?",
        (external_key, group_id),
    )
    db.execute(
        """
        UPDATE group_key_registry
        SET crm_external_key=?, updated_at=?
        WHERE message_db_id IN (
            SELECT message_db_id
            FROM grouping_results
            WHERE lead_group_id=?
        )
        """,
        (external_key, now, group_id),
    )
    db.commit()
    return db.execute(
        "SELECT * FROM crm_sync_state WHERE external_key = ?", (external_key,)
    ).fetchone()


def forget_lead_id(db: sqlite3.Connection, external_key: str) -> None:
    """ДОБАВЛЕНО: забыть удалённый в Bitrix лид, чтобы создать его заново.

    Без этого связка external_key -> bitrix_lead_id оставалась навсегда, и
    контакт, чей лид удалили в CRM, больше никогда туда не возвращался.
    """
    db.execute(
        """
        UPDATE crm_sync_state
        SET bitrix_lead_id=NULL, synced_revision=0, updated_at=?
        WHERE external_key=?
        """,
        (utc_now(), external_key),
    )
    db.commit()


def save_lead_id(db: sqlite3.Connection, external_key: str, lead_id: str) -> None:
    db.execute(
        """
        UPDATE crm_sync_state
        SET bitrix_lead_id=?, status='partial', updated_at=?
        WHERE external_key=?
        """,
        (lead_id, utc_now(), external_key),
    )
    db.commit()


def mark_synced(
    db: sqlite3.Connection,
    external_key: str,
    revision: int,
    payload_hash: str,
) -> None:
    db.execute(
        """
        UPDATE crm_sync_state
        SET synced_revision=?, timeline_revision=?, status='synced',
            next_attempt_at=0, locked_at=NULL, error_code=NULL,
            payload_hash=?, updated_at=?
        WHERE external_key=?
        """,
        (revision, revision, payload_hash, utc_now(), external_key),
    )
    db.commit()


def mark_error(
    db: sqlite3.Connection,
    external_key: str,
    error: SyncError,
) -> None:
    row = db.execute(
        "SELECT attempts FROM crm_sync_state WHERE external_key=?", (external_key,)
    ).fetchone()
    attempts = int(row["attempts"] if row else 1)
    retry = error.retryable and attempts < MAX_RETRIES
    status = "retry" if retry else (
        "mapping_required" if error.code == "employee_mapping_required" else "error"
    )
    if retry:
        base_delay = min(
            5 * (2 ** max(attempts - 1, 0)),
            300,
        )
        jitter = random.uniform(
            0,
            min(base_delay * 0.25, 10),
        )
        delay = base_delay + jitter
    else:
        delay = 0
    db.execute(
        """
        UPDATE crm_sync_state
        SET status=?, next_attempt_at=?, locked_at=NULL,
            error_code=?, updated_at=?
        WHERE external_key=?
        """,
        (status, time.time() + delay, error.code, utc_now(), external_key),
    )
    db.commit()
    print(f"crm_key={external_key[-8:]} status={status} error={error.code}")


def process_candidate(
    db: sqlite3.Connection,
    group: sqlite3.Row,
    extraction: sqlite3.Row,
    metadata: dict[str, bool],
    dry_run: bool,
) -> bool:
    validated = json_object(extraction["validated_json"], {})
    external_key = stable_external_key(group, extraction)
    revision = int(group["group_revision"])
    fingerprint = identity_fingerprint(validated)

    previous = db.execute(
        "SELECT * FROM crm_sync_state WHERE external_key=?", (external_key,)
    ).fetchone()
    if previous:
        if previous["status"] == "synced" and int(previous["synced_revision"]) >= revision:
            return False
        if previous["status"] == "mapping_required" and int(previous["synced_revision"]) == 0:
            return False
        if previous["status"] == "retry" and float(previous["next_attempt_at"]) > time.time():
            return False

    assigned_by_id = employee_id(db, group["author_id"])
    fields = build_fields(
        db,
        group,
        extraction,
        validated,
        assigned_by_id,
        metadata,
        external_key,
    )
    payload_hash = hashlib.sha256(
        json.dumps(fields, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()

    if dry_run:
        print(
            f"lead_group_id={group['id']} revision={revision} "
            f"status=would_sync assigned_by_id={assigned_by_id} "
            f"field_count={len(fields)}"
        )
        return True

    state = upsert_processing_state(
        db, external_key, int(group["id"]), fingerprint
    )

    lead_id = str(state["bitrix_lead_id"] or "")
    cross_group_duplicate = False
    action = "updated" if lead_id else "created"
    existing: dict[str, Any] | None = None

    # ДОБАВЛЕНО: сохранённый bitrix_lead_id может указывать на лид, который
    # удалили в Bitrix вручную (при тестировании — регулярно). Проверяем это ДО
    # поиска дублей, чтобы мёртвая связка не блокировала создание нового лида.
    if lead_id:
        existing = lead_state(lead_id)
        if existing is None:
            forget_lead_id(db, external_key)
            print(
                f"lead_group_id={group['id']} bitrix_lead_id={lead_id} "
                f"status=lead_missing_in_bitrix action=recreate"
            )
            lead_id = ""
            action = "created"

    if not lead_id:
        external_matches = find_by_external_key(external_key)
        if len(external_matches) > 1:
            raise SyncError("multiple_external_key_matches")
        if external_matches:
            lead_id = next(iter(external_matches))

    if not lead_id:
        local_match = local_duplicate_id(db, fingerprint, external_key)
        remote_matches = find_by_communications(validated)
        combined = set(remote_matches)
        if local_match:
            combined.add(local_match)
        if len(combined) > 1:
            raise SyncError("duplicate_ambiguous")
        if combined:
            lead_id = next(iter(combined))
            cross_group_duplicate = True
            action = "merged"

    # Лид, найденный по дублям, тоже нужно загрузить перед слиянием полей.
    if lead_id and existing is None:
        existing = lead_state(lead_id)
        if existing is None:
            raise SyncError("duplicate_lead_vanished", retryable=True)

    # ДОБАВЛЕНО: лид, уже сконвертированный порталом в сделку, не виден
    # менеджеру в списке лидов, и обновление лида НЕ обновляет созданную из
    # него сделку. Новый лид при этом создавать нельзя — по ТЗ это дубль.
    # Поэтому лид обновляем как обычно, а свежие данные дополнительно кладём
    # в таймлайн сделки, где менеджеры реально работают.
    converted_deal_id: str | None = None
    if lead_id and existing is not None and lead_is_converted(existing):
        converted_deal_id = deal_created_from_lead(lead_id)
        print(
            f"lead_group_id={group['id']} bitrix_lead_id={lead_id} "
            f"status=lead_already_converted deal_id={converted_deal_id or 'unknown'}"
        )

    if lead_id:
        update_fields = merge_existing_fields(
            existing or {}, fields, cross_group_duplicate, metadata
        )
        bitrix_call(
            "crm.lead.update",
            {"id": lead_id, "fields": update_fields},
        )
    else:
        lead_id = str(
            bitrix_call(
                "crm.lead.add",
                {
                    "fields": fields,
                    "params": {"REGISTER_SONET_EVENT": "N"},
                },
            )
        )
        if not lead_id or not lead_id.isdigit():
            raise SyncError("lead_add_missing_id", retryable=True)

    # Persist immediately: a later failure must update this lead, never add another.
    save_lead_id(db, external_key, lead_id)

    summary = str(validated.get("summary_ru") or "").strip()
    assessment = field_value(validated, "manager_assessment")
    if summary:
        marker = f"[Teams sync {external_key} revision {revision}]"
        comment_parts = [marker, "AI-выжимка:", summary]
        if assessment:
            comment_parts.extend(["", "Оценка менеджера:", assessment])
        bitrix_call(
            "crm.timeline.comment.add",
            {
                "fields": {
                    "ENTITY_ID": lead_id,
                    "ENTITY_TYPE": "lead",
                    "COMMENT": "\n".join(comment_parts),
                }
            },
        )
        # ДОБАВЛЕНО: если лид уже сконвертирован в сделку, обновление лида
        # менеджер не увидит — он работает в сделке. Дублируем выжимку в
        # таймлайн сделки, чтобы новая информация не терялась.
        if converted_deal_id:
            bitrix_call(
                "crm.timeline.comment.add",
                {
                    "fields": {
                        "ENTITY_ID": converted_deal_id,
                        "ENTITY_TYPE": "deal",
                        "COMMENT": "\n".join(
                            [
                                f"{marker} (лид {lead_id} уже сконвертирован в эту сделку)",
                                *comment_parts[1:],
                            ]
                        ),
                    }
                },
            )
    send_teams_confirmation(
        lead_id=lead_id,
        action=action,
        author_name=str(group["author_name"] or ""),
    )
    mark_synced(db, external_key, revision, payload_hash)
    print(
        f"lead_group_id={group['id']} revision={revision} "
        f"status=synced bitrix_lead_id={lead_id}"
    )
    return True


def process_available(dry_run: bool = False) -> int:
    db = connect_database()
    initialize_database(db)
    metadata = field_metadata()
    processed = 0

    try:
        for group, extraction in load_candidates(db):
            external_key = stable_external_key(group, extraction)
            try:
                if process_candidate(
                    db, group, extraction, metadata, dry_run
                ):
                    processed += 1
            except SyncError as error:
                if dry_run:
                    print(
                        f"lead_group_id={group['id']} status=blocked "
                        f"error={error.code}"
                    )
                    continue
                if not db.execute(
                    "SELECT 1 FROM crm_sync_state WHERE external_key=?",
                    (external_key,),
                ).fetchone():
                    upsert_processing_state(
                        db,
                        external_key,
                        int(group["id"]),
                        identity_fingerprint(
                            json_object(extraction["validated_json"], {})
                        ),
                    )
                mark_error(db, external_key, error)
    finally:
        db.close()

    return processed


def retry_blocked() -> None:
    db = connect_database()
    initialize_database(db)
    db.execute(
        """
        UPDATE crm_sync_state
        SET status='retry', next_attempt_at=0, error_code=NULL, updated_at=?
        WHERE status IN ('mapping_required', 'error')
        """,
        (utc_now(),),
    )
    db.commit()
    db.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--retry-blocked", action="store_true")
    args = parser.parse_args()

    if args.retry_blocked:
        retry_blocked()

    if args.once or args.dry_run:
        processed = process_available(dry_run=args.dry_run)
        print(f"BITRIX_DONE processed={processed} dry_run={str(args.dry_run).lower()}")
        return

    print("Bitrix worker started. Press Ctrl+C to stop.")
    while True:
        process_available()
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nBitrix worker stopped.")
