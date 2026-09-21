"""Extract and validate a structured CRM lead from each ready lead group.

Pipeline per group revision:
1. One multimodal structured-output extraction call.
2. Deterministic validation of evidence, email, phone, enums and Partner rule.
3. One verification call only when a conflict or low-confidence value is found.

The exact transcript is never replaced by the summary. It remains in
content_artifacts and is copied separately into lead_extractions.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from email_validator import EmailNotValidError, validate_email
import phonenumbers
from phonenumbers import PhoneNumberFormat
from PIL import Image
from pydantic import BaseModel, Field
from email_hygiene import email_warnings

# ДОБАВЛЕНО: распознавание email, продиктованного голосом.
from spoken_contacts import with_spoken_emails

# ДОБАВЛЕНО: проверка email на опечатки/недоставляемость (пункт ТЗ
# "Email вбивают с ошибкой — письмо не доходит").
from email_hygiene import email_warnings
from lead_rules import (
    PARTNER_RE,
    explicit_high_priority_match,
    position_quote_supports,
    priority_quote_supports,
)


load_dotenv()

DB_PATH = Path(os.getenv("DATABASE_PATH", "data/messages.db"))
POLL_SECONDS = int(os.getenv("EXTRACTION_POLL_SECONDS", "5"))
MODEL_NAME = os.getenv("OPENAI_EXTRACTION_MODEL", "gpt-4o-mini")
MAX_IMAGES = int(os.getenv("EXTRACTION_MAX_IMAGES", "6"))
MIN_EVIDENCE_CONFIDENCE = float(os.getenv("MIN_EVIDENCE_CONFIDENCE", "0.75"))
# ДОБАВЛЕНО: порог для значений, прочитанных прямо с фотографии визитки
# vision-моделью (когда OCR не справился и сверить цитату не с чем).
IMAGE_EVIDENCE_MIN_CONFIDENCE = float(
    os.getenv("IMAGE_EVIDENCE_MIN_CONFIDENCE", "0.90")
)

EMAIL_RE = re.compile(
    r"(?<![\w.+-])[A-Z0-9._%+-]+@(?:[A-Z0-9-]+\.)+[A-Z]{2,63}(?![\w-])",
    re.I,
)
PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\d\s().-]{6,}\d)(?!\d)")

DEFAULT_COUNTRY_ISO = {
    "kazakhstan": "KZ",
    "казахстан": "KZ",
    "russia": "RU",
    "россия": "RU",
    "germany": "DE",
    "германия": "DE",
    "united states": "US",
    "usa": "US",
    "сша": "US",
    "united kingdom": "GB",
    "uk": "GB",
    "великобритания": "GB",
    "china": "CN",
    "китай": "CN",
    "india": "IN",
    "индия": "IN",
    "turkey": "TR",
    "турция": "TR",
    "uzbekistan": "UZ",
    "узбекистан": "UZ",
    "kyrgyzstan": "KG",
    "кыргызстан": "KG",
    "uae": "AE",
    "оаэ": "AE",
}


class Evidence(BaseModel):
    message_id: str
    source_type: Literal["text", "transcript", "image"]
    quote: str
    confidence: float = Field(ge=0.0, le=1.0)


class FieldValue(BaseModel):
    value: str | None
    evidence: list[Evidence]


class Conflict(BaseModel):
    field: Literal[
        "full_name",
        "company",
        "position",
        "country",
        "phones",
        "emails",
        "product_interests",
        "priority",
        "lead_type",
    ]
    description: str


class LeadExtraction(BaseModel):
    is_lead: bool
    non_lead_reason: str | None

    full_name: FieldValue
    company: FieldValue
    position: FieldValue
    country: FieldValue

    phones: list[FieldValue]
    emails: list[FieldValue]
    product_interests: list[FieldValue]

    priority: Literal["High", "Medium", "Low"] | None
    priority_evidence: Evidence | None
    lead_type: Literal["Partner", "Customer"]
    partner_explicit_evidence: Evidence | None

    manager_assessment: FieldValue
    summary_ru: str
    conflicts: list[Conflict]


SYSTEM_PROMPT = """You extract a CRM lead from an exhibition conversation.
Return the Pydantic structure exactly.

Non-negotiable rules:
- Do not infer a field. If a reliable source is absent, return null and [].
- Every non-null factual field must cite a concrete source with message_id,
  source_type, a short exact quote, and confidence.
- Preserve the spelling/script found in the source. Do not transliterate names.
- A business card normally wins for the full spelling of name/company.
- An explicit spoken correction such as 'now works at another company' wins over
  an older business card.
- If sources truly conflict, return null for that field and add a conflict.
- Keep the manager's subjective assessment separate in manager_assessment.
- summary_ru is a short Russian analytical summary; it never replaces transcripts.
- Partner is allowed only with explicit partner/distributor/reseller/integrator
  language or an explicit intention to resell/implement the product for clients.
  'Wants to cooperate', 'interesting company' and 'could be useful' are Customer.
- product_interests must use only the supplied allowed values.
- Do not repair an uncertain email. Keep the exact supported spelling or null.
- If an email contains mixed-script lookalikes, a likely domain typo, or a
  domain proven unable to receive mail, leave the CRM email field empty. Never
  silently replace it with a guessed correction.
- position is a job title only when the source explicitly contains a role cue
  (for example director, procurement manager, CEO, engineer). A company name,
  project label, or phrase ending in "Test" is not a position.
- priority is null unless the source explicitly states urgency/priority. A
  deadline such as "by Friday" alone does not mean High.
- A contact is still a lead without phone/email when a reliably evidenced person
  or company is accompanied by concrete business context or a next action such as
  call back, send a proposal, arrange a meeting, or follow up. A bare name alone
  is not sufficient.

LEAD DECISION:
- Return is_lead=true for an identifiable person/company/business card only
  when there is concrete business context, a next action, or an explicit label
  such as prospect/customer/partner/distributor/reseller/integrator.
- A bare name or company with no business context is not sufficient.
"""


ACTIONABLE_LEAD_RE = re.compile(
    r"\b(?:перезвон\w*|связат\w*|позвон\w*|отправ\w*|выслат\w*|"
    r"кп|предложени\w*|встреч\w*|заинтерес\w*|интересует\w*|"
    r"клиент\w*|партн[её]р\w*|дистрибьютор\w*|реселлер\w*|интегратор\w*|"
    r"call\s*back|follow[ -]?up|proposal|quotation|meeting|interested)\b",
    re.IGNORECASE,
)

FULL_NAME_RE = re.compile(
    r"(?<![\w-])(?:"
    r"[А-ЯЁ][а-яё-]{1,}(?:\s+[А-ЯЁ][а-яё-]{1,}){1,2}"
    r"|"
    r"[A-Z][A-Za-z'-]{1,}(?:\s+[A-Z][A-Za-z'-]{1,}){1,2}"
    r")(?![\w-])"
)

NAME_STOPWORDS = {
    "клиент",
    "контакт",
    "новый",
    "новая",
    "компания",
    "срочно",
    "customer",
    "contact",
    "company",
    "urgent",
}

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def norm_text(value: str) -> str:
    return " ".join(value.casefold().split())


def json_env(name: str, default: Any) -> Any:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def allowed_products() -> list[str]:
    return [item.strip() for item in os.getenv("PRODUCT_INTERESTS", "").split(",") if item.strip()]


def product_aliases() -> dict[str, str]:
    raw = json_env("PRODUCT_ALIASES_JSON", {})
    return {str(key).casefold(): str(value) for key, value in raw.items()}


def country_iso_map() -> dict[str, str]:
    result = dict(DEFAULT_COUNTRY_ISO)
    raw = json_env("COUNTRY_ISO_MAP_JSON", {})
    result.update({str(key).casefold(): str(value).upper() for key, value in raw.items()})
    return result


def country_region_map() -> dict[str, str]:
    raw = json_env("COUNTRY_REGION_MAP_JSON", {})
    return {str(key).casefold(): str(value) for key, value in raw.items()}


def column_names(db: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}


def ensure_column(db: sqlite3.Connection, table: str, name: str, definition: str) -> None:
    if name not in column_names(db, table):
        db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def init_db(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS lead_extractions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_group_id INTEGER NOT NULL,
            group_revision INTEGER NOT NULL,
            status TEXT NOT NULL,
            is_lead INTEGER,
            model_name TEXT,
            verification_used INTEGER NOT NULL DEFAULT 0,
            raw_model_json TEXT,
            validated_json TEXT,
            raw_transcripts_json TEXT NOT NULL DEFAULT '[]',
            source_message_ids_json TEXT NOT NULL DEFAULT '[]',
            validation_issues_json TEXT NOT NULL DEFAULT '[]',
            error_code TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(lead_group_id, group_revision),
            FOREIGN KEY(lead_group_id) REFERENCES lead_groups(id)
        );

        CREATE TABLE IF NOT EXISTS lead_field_evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            extraction_id INTEGER NOT NULL,
            field_name TEXT NOT NULL,
            item_index INTEGER NOT NULL DEFAULT 0,
            message_id TEXT NOT NULL,
            source_type TEXT NOT NULL,
            quote TEXT NOT NULL,
            confidence REAL NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(extraction_id) REFERENCES lead_extractions(id)
        );

        CREATE INDEX IF NOT EXISTS idx_extractions_group_revision
            ON lead_extractions(lead_group_id, group_revision);
        CREATE INDEX IF NOT EXISTS idx_field_evidence_extraction
            ON lead_field_evidence(extraction_id);
        """
    )
    ensure_column(db, "lead_groups", "last_extracted_revision", "INTEGER")
    db.commit()


def group_sources(db: sqlite3.Connection, group_id: int) -> dict[str, Any]:
    messages = db.execute(
        """
        SELECT m.id, m.message_id, m.body_text, m.created_at
        FROM grouping_results result
        JOIN messages m ON m.id = result.message_db_id
        WHERE result.lead_group_id = ?
        ORDER BY m.created_at, m.id
        """,
        (group_id,),
    ).fetchall()

    sources: list[dict[str, str]] = []
    transcripts: list[dict[str, str]] = []
    message_ids: list[str] = []
    db_ids: list[int] = []

    for message in messages:
        message_id = str(message["message_id"])
        message_ids.append(message_id)
        db_ids.append(int(message["id"]))
        body = (message["body_text"] or "").strip()
        if body:
            sources.append(
                {"message_id": message_id, "source_type": "text", "content": body[:8000]}
            )

        artifacts = db.execute(
            """
            SELECT artifact_type, raw_text
            FROM content_artifacts
            WHERE message_db_id = ? AND status = 'ready'
            ORDER BY id
            """,
            (message["id"],),
        ).fetchall()
        for artifact in artifacts:
            text = (artifact["raw_text"] or "").strip()
            if not text:
                continue
            if artifact["artifact_type"] == "transcript":
                source_type = "transcript"
                # Дословная расшифровка для комментария в Bitrix — без изменений.
                transcripts.append({"message_id": message_id, "text": text})
                # ДОБАВЛЕНО: а в источник для модели добавляем расшифрованный
                # из речи email ("самал собачка жмэйл ком" -> samal@gmail.com).
                # Без этого продиктованный адрес не видели ни регулярка, ни
                # модель, и поле Email в лиде оставалось пустым, хотя в
                # расшифровке адрес был.
                text = with_spoken_emails(text)
            elif artifact["artifact_type"] == "ocr":
                source_type = "image"
            else:
                continue
            sources.append(
                {"message_id": message_id, "source_type": source_type, "content": text[:8000]}
            )

    images: list[dict[str, str]] = []
    if db_ids:
        placeholders = ",".join("?" for _ in db_ids)
        files = db.execute(
            f"""
            SELECT f.id, f.message_db_id, f.local_path, f.mime_type, m.message_id
            FROM message_files f
            JOIN messages m ON m.id = f.message_db_id
            WHERE f.message_db_id IN ({placeholders})
            ORDER BY f.id
            """,
            db_ids,
        ).fetchall()
        for file in files:
            path = Path(file["local_path"] or "")
            if path.is_file() and is_image_file(path):
                images.append(
                    {
                        "message_id": str(file["message_id"]),
                        "path": str(path),
                    }
                )
                if len(images) >= MAX_IMAGES:
                    break

    return {
        "sources": sources,
        "images": images,
        "transcripts": transcripts,
        "message_ids": message_ids,
    }


def is_image_file(path: Path) -> bool:
    try:
        with Image.open(path) as image:
            image.verify()
        return True
    except Exception:
        return False


def image_data_url(path: Path) -> str:
    with Image.open(path) as image:
        image = image.convert("RGB")
        image.thumbnail((1800, 1800))
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=88, optimize=True)
    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def model_content(bundle: dict[str, Any], extra_text: str) -> list[dict[str, Any]]:
    prompt_data = {
        "allowed_product_interests": allowed_products(),
        "sources": bundle["sources"],
    }
    content: list[dict[str, Any]] = [
        {
            "type": "input_text",
            "text": extra_text + "\nINPUT:\n" + json.dumps(prompt_data, ensure_ascii=False),
        }
    ]
    for image in bundle["images"]:
        content.append(
            {
                "type": "input_text",
                "text": f"Business-card image belonging to message_id={image['message_id']}",
            }
        )
        content.append(
            {
                "type": "input_image",
                "image_url": image_data_url(Path(image["path"])),
                "detail": "high",
            }
        )
    return content


def call_extraction(bundle: dict[str, Any]) -> LeadExtraction:
    from openai import OpenAI

    client = OpenAI(timeout=60.0)
    response = client.responses.parse(
        model=MODEL_NAME,
        store=False,
        input=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": model_content(bundle, "Extract one lead from this complete message group."),
            },
        ],
        text_format=LeadExtraction,
    )
    if response.output_parsed is None:
        raise RuntimeError("model_returned_no_parsed_output")
    return response.output_parsed


def call_verification(
    bundle: dict[str, Any], primary: LeadExtraction, issues: list[str]
) -> LeadExtraction:
    from openai import OpenAI

    client = OpenAI(timeout=60.0)
    verification_text = (
        "Verify and correct the extraction. Return the complete LeadExtraction again. "
        "Resolve only from sources; set uncertain fields to null.\n"
        f"VALIDATION ISSUES: {json.dumps(issues, ensure_ascii=False)}\n"
        f"PRIMARY EXTRACTION: {primary.model_dump_json()}"
    )
    response = client.responses.parse(
        model=MODEL_NAME,
        store=False,
        input=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": model_content(bundle, verification_text)},
        ],
        text_format=LeadExtraction,
    )
    if response.output_parsed is None:
        raise RuntimeError("verification_returned_no_parsed_output")
    return response.output_parsed


def source_index(bundle: dict[str, Any]) -> dict[tuple[str, str], list[str]]:
    result: dict[tuple[str, str], list[str]] = {}
    for source in bundle["sources"]:
        key = (source["message_id"], source["source_type"])
        result.setdefault(key, []).append(source["content"])
    return result


def image_message_ids(bundle: dict[str, Any]) -> set[str]:
    return {image["message_id"] for image in bundle["images"]}


def supported_by_image(evidence_list: list[Evidence], bundle: dict[str, Any]) -> bool:
    """ДОБАВЛЕНО: значение подтверждено самой фотографией визитки.

    Сверить цитату с картинкой построчно невозможно, поэтому для таких
    значений действует другая проверка: у сообщения действительно есть
    приложенное изображение, модель сослалась именно на него, и
    доказательство уже прошло порог уверенности в valid_evidence().
    """
    image_ids = image_message_ids(bundle)
    return any(
        item.source_type == "image" and item.message_id in image_ids
        for item in evidence_list
    )


def evidence_is_supported(
    evidence: Evidence,
    bundle: dict[str, Any],
    allow_second_pass_image: bool,
) -> bool:
    quote = norm_text(evidence.quote)
    if not quote:
        return False
    candidates = source_index(bundle).get((evidence.message_id, evidence.source_type), [])
    if any(quote in norm_text(candidate) for candidate in candidates):
        return True
    if (
        evidence.source_type == "image"
        and evidence.message_id in image_message_ids(bundle)
        and allow_second_pass_image
        # ИЗМЕНЕНО: порог вынесен в переменную окружения. Цитату с фотографии
        # нечем сверить построчно, поэтому для неё нужна более высокая
        # уверенность, чем для текста; но если OCR на вашем оборудовании
        # стабильно не дочитывает визитки, порог можно понизить осознанно,
        # не правя код.
        and evidence.confidence >= IMAGE_EVIDENCE_MIN_CONFIDENCE
    ):
        return True
    return False


def explicit_partner_evidence(bundle: dict[str, Any]) -> Evidence | None:
    """ДОБАВЛЕНО: явные партнёрские слова, дословно найденные в источнике.

    Возвращает готовое доказательство (цитату с точной позицией в тексте),
    если менеджер или визитка прямо говорят про партнёрство/дистрибуцию.
    Это не догадка и не пересказ модели: цитата берётся из самого источника,
    поэтому evidence_is_supported для неё выполняется по построению.

    Намеренно не трогаем источники типа "image": цитату с фотографии сверить
    построчно нельзя, а тип лида — поле, где ошибка дорогая.
    """
    for source in bundle.get("sources", []):
        if source.get("source_type") not in {"text", "transcript"}:
            continue
        content = source.get("content") or ""
        match = PARTNER_RE.search(content)
        if not match:
            continue
        start = max(0, match.start() - 40)
        end = min(len(content), match.end() + 40)
        return Evidence(
            message_id=str(source["message_id"]),
            source_type=str(source["source_type"]),
            quote=content[start:end].strip(),
            confidence=1.0,
        )
    return None


def all_field_values(extraction: LeadExtraction) -> list[tuple[str, int, FieldValue]]:
    fields: list[tuple[str, int, FieldValue]] = [
        ("full_name", 0, extraction.full_name),
        ("company", 0, extraction.company),
        ("position", 0, extraction.position),
        ("country", 0, extraction.country),
        ("manager_assessment", 0, extraction.manager_assessment),
    ]
    fields.extend(("phones", index, item) for index, item in enumerate(extraction.phones))
    fields.extend(("emails", index, item) for index, item in enumerate(extraction.emails))
    fields.extend(
        ("product_interests", index, item)
        for index, item in enumerate(extraction.product_interests)
    )
    return fields


def canonical_email(value: str) -> str | None:
    value = value.strip()
    if "@" not in value:
        return None
    local, domain = value.rsplit("@", 1)
    candidate = f"{local}@{domain.lower()}"
    try:
        validate_email(candidate, check_deliverability=False)
    except EmailNotValidError:
        return None
    return candidate


def source_emails(bundle: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    for source in bundle["sources"]:
        for match in EMAIL_RE.finditer(source["content"]):
            canonical = canonical_email(match.group(0))
            if canonical:
                result.add(canonical)
    return result


def ambiguous_email_candidates(bundle: dict[str, Any]) -> set[str]:
    groups: dict[tuple[str, str], set[str]] = {}
    for email in source_emails(bundle):
        local, domain = email.rsplit("@", 1)
        skeleton = re.sub(r"[._-]", "", local.casefold())
        groups.setdefault((skeleton, domain.casefold()), set()).add(email)
    ambiguous: set[str] = set()
    for values in groups.values():
        if len(values) > 1:
            ambiguous.update(values)
    return ambiguous


def source_phone_digits(bundle: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    for source in bundle["sources"]:
        for match in PHONE_RE.finditer(source["content"]):
            digits = re.sub(r"\D", "", match.group(0))
            if 7 <= len(digits) <= 15:
                result.add(digits)
    return result


def detect_issues(extraction: LeadExtraction, bundle: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    for field_name, index, field in all_field_values(extraction):
        if field.value is None:
            continue
        if not field.evidence:
            issues.append(f"{field_name}[{index}]:missing_evidence")
            continue
        for evidence in field.evidence:
            if evidence.confidence < MIN_EVIDENCE_CONFIDENCE:
                issues.append(f"{field_name}[{index}]:low_confidence")
            if not evidence_is_supported(evidence, bundle, False):
                issues.append(f"{field_name}[{index}]:quote_not_verified")

    observed_emails = source_emails(bundle)
    ambiguous_emails = ambiguous_email_candidates(bundle)
    for index, item in enumerate(extraction.emails):
        if item.value is None:
            continue
        email = canonical_email(item.value)
        if not email or email not in observed_emails:
            issues.append(f"emails[{index}]:invalid_or_absent_in_source")
        elif email in ambiguous_emails:
            issues.append(f"emails[{index}]:conflicting_readings")
        else:
            # ДОБАВЛЕНО: адрес формально валиден, но, скорее всего, не
            # доставится (опечатка в домене, кириллические двойники, нет MX).
            for warning in email_warnings(email):
                issues.append(f"emails[{index}]:{warning['code']}")

    observed_phones = source_phone_digits(bundle)
    for index, item in enumerate(extraction.phones):
        if item.value is None:
            continue
        digits = re.sub(r"\D", "", item.value)
        if digits not in observed_phones:
            issues.append(f"phones[{index}]:absent_in_source")

    if extraction.position.value is not None:
        if not any(
            position_quote_supports(evidence.quote)
            for evidence in extraction.position.evidence
        ):
            issues.append("position:not_explicitly_supported")

    allowed = set(allowed_products())
    aliases = product_aliases()
    for index, item in enumerate(extraction.product_interests):
        if item.value is None:
            continue
        mapped = aliases.get(item.value.casefold(), item.value)
        if mapped not in allowed:
            issues.append(f"product_interests[{index}]:not_in_crm_enum")

    if extraction.priority and extraction.priority_evidence is None:
        issues.append("priority:missing_evidence")
    if extraction.priority_evidence and not evidence_is_supported(
        extraction.priority_evidence, bundle, False
    ):
        issues.append("priority:quote_not_verified")
    if (
        extraction.priority
        and extraction.priority_evidence
        and not priority_quote_supports(
            extraction.priority, extraction.priority_evidence.quote
        )
    ):
        issues.append("priority:not_explicitly_supported")

    if extraction.lead_type == "Partner":
        evidence = extraction.partner_explicit_evidence
        if (
            evidence is None
            or not PARTNER_RE.search(evidence.quote)
            or not evidence_is_supported(evidence, bundle, False)
        ):
            issues.append("lead_type:partner_not_explicitly_supported")

    issues.extend(f"conflict:{conflict.field}" for conflict in extraction.conflicts)
    return sorted(set(issues))


def valid_evidence(
    field: FieldValue, bundle: dict[str, Any], allow_second_pass_image: bool
) -> list[Evidence]:
    return [
        evidence
        for evidence in field.evidence
        if evidence.confidence >= MIN_EVIDENCE_CONFIDENCE
        and evidence_is_supported(evidence, bundle, allow_second_pass_image)
    ]


def sanitize_field(
    field: FieldValue,
    bundle: dict[str, Any],
    allow_second_pass_image: bool,
    force_null: bool = False,
) -> dict[str, Any]:
    if field.value is None or force_null:
        return {"value": None, "evidence": []}
    evidence = valid_evidence(field, bundle, allow_second_pass_image)
    if not evidence:
        return {"value": None, "evidence": []}
    return {
        "value": field.value.strip(),
        "evidence": [item.model_dump() for item in evidence],
    }


def normalized_phone(raw: str, country: str | None) -> str | None:
    try:
        if raw.strip().startswith("+"):
            parsed = phonenumbers.parse(raw, None)
        else:
            region = country_iso_map().get((country or "").casefold())
            if not region:
                return None
            parsed = phonenumbers.parse(raw, region)
        if not phonenumbers.is_possible_number(parsed):
            return None
        return phonenumbers.format_number(parsed, PhoneNumberFormat.E164)
    except phonenumbers.NumberParseException:
        return None


def sanitize_extraction(
    extraction: LeadExtraction,
    bundle: dict[str, Any],
    verification_used: bool,
) -> dict[str, Any]:
    conflict_fields = {conflict.field for conflict in extraction.conflicts}
    result: dict[str, Any] = {
        "is_lead": extraction.is_lead,
        "non_lead_reason": extraction.non_lead_reason,
        "full_name": sanitize_field(
            extraction.full_name, bundle, verification_used, "full_name" in conflict_fields
        ),
        "company": sanitize_field(
            extraction.company, bundle, verification_used, "company" in conflict_fields
        ),
        "position": sanitize_field(
            extraction.position,
            bundle,
            verification_used,
            "position" in conflict_fields
            or not any(
                position_quote_supports(evidence.quote)
                for evidence in extraction.position.evidence
            ),
        ),
        "country": sanitize_field(
            extraction.country, bundle, verification_used, "country" in conflict_fields
        ),
        "manager_assessment": sanitize_field(
            extraction.manager_assessment, bundle, verification_used
        ),
        "summary_ru": extraction.summary_ru.strip(),
        "conflicts": [item.model_dump() for item in extraction.conflicts],
    }

    observed_emails = source_emails(bundle)
    ambiguous_emails = ambiguous_email_candidates(bundle)
    emails = []

    # Значения, предложенные моделью (например, прочитанные с визитки через OCR
    # или продиктованные голосом и распознанные транскрипцией).
    # При заявленном конфликте модели им не доверяем.
    #
    # ИСПРАВЛЕНО: раньше здесь было `result.get("emails", [])` — ключа "emails"
    # в `result` на этот момент ещё не существует (он появляется ниже, в строке
    # `result["emails"] = emails`), поэтому цикл всегда получал пустой список и
    # ни один email, извлечённый моделью из фото визитки или расшифровки голоса,
    # никогда не попадал в лид — оставались только адреса, найденные регуляркой
    # в сыром тексте сообщения Teams. Также вызывалась не существующая нигде в
    # проекте функция `evidence_supports_value` и использовалась переменная
    # `issues`, не определённая в этой функции (в `sanitize_extraction` нет
    # параметра/переменной `issues` — это осталось от `detect_issues`). Из-за
    # того, что цикл не выполнялся ни разу, эти ошибки были "тихими" и не
    # проявлялись как исключение, а функционально ломали извлечение email.
    # Исправление: используем реальный список `extraction.emails` от модели и
    # переиспользуем ту же проверку доказательств (`valid_evidence`), что и для
    # телефонов/интересов ниже.
    if "emails" not in conflict_fields:
        for item in extraction.emails:
            if item.value is None:
                continue

            value = canonical_email(item.value)
            evidence = valid_evidence(item, bundle, verification_used)

            # ИСПРАВЛЕНО: условие `value in observed_emails` требовало, чтобы
            # адрес был найден регуляркой в ТЕКСТОВЫХ источниках (тело
            # сообщения, результат OCR, расшифровка). Но главный источник на
            # выставке — само фото визитки, которое читает vision-модель, а не
            # EasyOCR: на CPU он регулярно не распознаёт мелкий шрифт с
            # адресом. В таком случае адрес есть на картинке, модель его
            # видит и цитирует, а сервис его молча выбрасывал — в карточке
            # лида Email оставался пустым, хотя в аналитической выжимке того
            # же лида адрес был прямо написан ("Указан адрес электронной
            # почты ghijkk@ghijk.ru"). Теперь адрес, подтверждённый ссылкой на
            # изображение, засчитывается: проверять его текстом попросту не с
            # чем, а доказательство (evidence) у него есть, и оно уже прошло
            # порог уверенности в valid_evidence().
            warnings = email_warnings(value) if value else []
            if (
                value
                and value not in ambiguous_emails
                and evidence
                and (value in observed_emails or supported_by_image(evidence, bundle))
                and not warnings
            ):
                emails.append(
                    {
                        "value": value,
                        "evidence": [entry.model_dump() for entry in evidence],
                    }
                )
                emails.append(
                    {
                        "value": value,
                # Замечания по доставляемости (опечатка в домене,
                # кириллические буквы-двойники, отсутствие MX). Адрес НЕ
                # правится молча — менеджер должен увидеть и исправить сам.
                        "warnings": email_warnings(value),
                        "evidence": [entry.model_dump() for entry in evidence],
                    }
                )
    # Точный адрес, написанный менеджером в текстовом сообщении.
    # Он может исправить ложный конфликт модели, но не реальный
    # конфликт двух похожих адресов.
    added_emails = {
        item["value"].casefold()
        for item in emails
        if item.get("value")
    }

    for source in bundle.get("sources", []):
        if source.get("source_type") != "text":
            continue

        content = source.get("content") or ""

        for match in EMAIL_RE.finditer(content):
            value = canonical_email(match.group(0))

            if not value:
                continue

            if value in ambiguous_emails:
                continue

            # A syntactically valid typo such as gmail.con is more dangerous
            # than an empty field: the manager will not notice that mail is
            # undeliverable.  Keep it only in validation issues, never in CRM.
            if email_warnings(value):
                continue

            if value.casefold() in added_emails:
                continue

            emails.append(
                {
                    "value": value,
                    "evidence": [
                        {
                            "message_id": str(source["message_id"]),
                            "source_type": "text",
                            "quote": match.group(0),
                            "confidence": 1.0,
                        }
                    ],
                }
            )
            added_emails.add(value.casefold())

    result["emails"] = emails

    country = result["country"]["value"]
    observed_phones = source_phone_digits(bundle)
    phones: list[dict[str, Any]] = []
    if "phones" not in conflict_fields:
        for item in extraction.phones:
            if item.value is None:
                continue
            raw = item.value.strip()
            digits = re.sub(r"\D", "", raw)
            evidence = valid_evidence(item, bundle, verification_used)
            # ИСПРАВЛЕНО: та же причина, что и для email выше — телефон с
            # визитки, который прочитала vision-модель, а не OCR, отбрасывался.
            if evidence and (
                digits in observed_phones or supported_by_image(evidence, bundle)
            ):
                e164 = normalized_phone(raw, country)
                phones.append(
                    {
                        "value": e164 or raw,
                        "raw_value": raw,
                        "normalized_e164": e164,
                        "evidence": [entry.model_dump() for entry in evidence],
                    }
                )
    result["phones"] = phones

    allowed = set(allowed_products())
    aliases = product_aliases()
    interests: list[dict[str, Any]] = []
    if "product_interests" not in conflict_fields:
        for item in extraction.product_interests:
            if item.value is None:
                continue
            mapped = aliases.get(item.value.casefold(), item.value)
            evidence = valid_evidence(item, bundle, verification_used)
            if mapped in allowed and evidence:
                interests.append(
                    {
                        "value": mapped,
                        "evidence": [entry.model_dump() for entry in evidence],
                    }
                )
    result["product_interests"] = interests

    priority_evidence = extraction.priority_evidence
    priority_valid = (
        extraction.priority is not None
        and "priority" not in conflict_fields
        and priority_evidence is not None
        and priority_evidence.confidence >= MIN_EVIDENCE_CONFIDENCE
        and evidence_is_supported(priority_evidence, bundle, verification_used)
        and priority_quote_supports(extraction.priority, priority_evidence.quote)
    )
    result["priority"] = extraction.priority if priority_valid else None
    result["priority_evidence"] = (
        priority_evidence.model_dump() if priority_valid and priority_evidence else None
    )

    partner_evidence = extraction.partner_explicit_evidence
    partner_valid = (
        extraction.lead_type == "Partner"
        and "lead_type" not in conflict_fields
        and partner_evidence is not None
        and partner_evidence.confidence >= MIN_EVIDENCE_CONFIDENCE
        and PARTNER_RE.search(partner_evidence.quote) is not None
        and evidence_is_supported(partner_evidence, bundle, verification_used)
    )

    # ИСПРАВЛЕНО: тип лида определялся ИСКЛЮЧИТЕЛЬНО решением модели — если она
    # вернула "Customer", проверка выше даже не запускалась, и лид навсегда
    # оставался Customer. На боевом прогоне так и вышло: в расшифровке прямо
    # сказано "Роман Михайлович предложил предложение о партнерстве", в
    # аналитической выжимке лида написано "предложил партнерство", а в поле
    # Тип стоял Customer.
    #
    # ТЗ формулирует правило не как "как решит модель", а как проверяемое
    # условие: Partner допустим при ЯВНЫХ словах партнёр/дистрибьютор/
    # реселлер/интегратор. Если такие слова дословно есть в источнике —
    # условие выполнено, и это можно установить детерминированно, не полагаясь
    # на настроение модели. Осторожность ТЗ ("цена двух видов ошибки разная")
    # сохраняется: поднимаем тип только по дословному совпадению в реальном
    # тексте источника, а не по пересказу модели и не по догадке.
    if not partner_valid and "lead_type" not in conflict_fields:
        explicit = explicit_partner_evidence(bundle)
        if explicit is not None:
            partner_valid = True
            partner_evidence = explicit

    result["lead_type"] = "Partner" if partner_valid else "Customer"
    result["partner_explicit_evidence"] = (
        partner_evidence.model_dump() if partner_valid and partner_evidence else None
    )

    region = country_region_map().get((country or "").casefold())
    result["region"] = region

    # Business rule: контакт без телефона/email тоже может быть лидом,
    # если есть подтверждённое имя и конкретное следующее действие.
    sources = bundle.get("sources", [])

    source_text = "\n".join(
        str(source.get("content") or "")
        for source in sources
    )

    action_match = ACTIONABLE_LEAD_RE.search(source_text)

    # Если модель не извлекла имя, берём только точное имя из источника.
    if result["full_name"]["value"] is None and action_match:
        for source in sources:
            if source.get("source_type") not in {
                "text",
                "transcript",
            }:
                continue

            match = FULL_NAME_RE.search(
                str(source.get("content") or "")
            )

            if not match:
                continue

            candidate = match.group(0).strip()
            words = {
                word.casefold()
                for word in candidate.split()
            }

            if words & NAME_STOPWORDS:
                continue

            result["full_name"] = {
                "value": candidate,
                "evidence": [
                    {
                        "message_id": str(
                            source.get("message_id") or ""
                        ),
                        "source_type": str(
                            source.get("source_type")
                            or "transcript"
                        ),
                        "quote": candidate,
                        "confidence": 0.90,
                    }
                ],
            }
            break

    has_reliable_identity = bool(
        result["full_name"]["value"]
        or result["company"]["value"]
        or result["emails"]
        or result["phones"]
    )

    if (
        not result["is_lead"]
        and has_reliable_identity
        and action_match
    ):
        result["is_lead"] = True
        result["non_lead_reason"] = None

        if (
            not result["summary_ru"]
            or "недостаточно"
            in result["summary_ru"].casefold()
        ):
            result["summary_ru"] = (
                "Зафиксирован контакт и конкретное "
                "следующее действие; неподтверждённые "
                "реквизиты оставлены пустыми."
            )

    # ДОБАВЛЕНО: подтверждённый (прошедший проверку evidence, то есть реально
    # встречающийся в источнике) телефон или email сам по себе — уже
    # достаточное основание считать контакт лидом, даже без слов вроде
    # "перезвонить"/"интересует". Это то же самое правило, которым уже
    # руководствуется группировка (grouping_worker.classify_content:
    # "if emails or phones: return LEAD_PART" без каких-либо дополнительных
    # условий) — здесь мы просто не даём этапу извлечения быть строже, чем
    # этап группировки уже был. Реальный кейс из теста: сообщение "Иван
    # иванович 87759196544" — телефон подтверждён (цифры действительно есть в
    # источнике), но явного "перезвонить"/"интересует" в тексте нет, и модель
    # извлечения сама по себе решила is_lead=false. Стоимость ошибок здесь
    # асимметрична в другую сторону, чем Partner/Customer: лишний лид с
    # именем и телефоном менеджер закроет за секунды, а потерянный телефон
    # реального посетителя выставки не восстановить.
    if not result["is_lead"] and (result["phones"] or result["emails"]):
        result["is_lead"] = True
        result["non_lead_reason"] = None

        if (
            not result["summary_ru"]
            or "недостаточно"
            in result["summary_ru"].casefold()
        ):
            result["summary_ru"] = (
                "Зафиксирован контакт с подтверждённым телефоном/email; "
                "остальные реквизиты не подтверждены и оставлены пустыми."
            )

    if (
        result["is_lead"]
        and result["priority"] is None
    ):
        for source in sources:
            if source.get("source_type") not in {"text", "transcript"}:
                continue

            urgent_match = explicit_high_priority_match(
                str(source.get("content") or "")
            )

            if not urgent_match:
                continue

            result["priority"] = "High"
            result["priority_evidence"] = {
                "message_id": str(
                    source.get("message_id") or ""
                ),
                "source_type": str(
                    source.get("source_type")
                    or "transcript"
                ),
                "quote": urgent_match.group(0),
                "confidence": 0.95,
            }
            break

    return result


def iter_validated_evidence(validated: dict[str, Any]):
    singular = ["full_name", "company", "position", "country", "manager_assessment"]
    for field_name in singular:
        field = validated.get(field_name) or {}
        for evidence in field.get("evidence", []):
            yield field_name, 0, evidence
    for field_name in ["phones", "emails", "product_interests"]:
        for index, item in enumerate(validated.get(field_name, [])):
            for evidence in item.get("evidence", []):
                yield field_name, index, evidence
    for field_name in ["priority_evidence", "partner_explicit_evidence"]:
        evidence = validated.get(field_name)
        if evidence:
            yield field_name, 0, evidence


def save_success(
    db: sqlite3.Connection,
    group: sqlite3.Row,
    raw: LeadExtraction,
    validated: dict[str, Any],
    bundle: dict[str, Any],
    issues: list[str],
    verification_used: bool,
) -> int:
    now = utc_now()
    cursor = db.execute(
        """
        INSERT INTO lead_extractions(
            lead_group_id, group_revision, status, is_lead, model_name,
            verification_used, raw_model_json, validated_json,
            raw_transcripts_json, source_message_ids_json,
            validation_issues_json, error_code, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
        ON CONFLICT(lead_group_id, group_revision) DO UPDATE SET
            status=excluded.status, is_lead=excluded.is_lead,
            model_name=excluded.model_name,
            verification_used=excluded.verification_used,
            raw_model_json=excluded.raw_model_json,
            validated_json=excluded.validated_json,
            raw_transcripts_json=excluded.raw_transcripts_json,
            source_message_ids_json=excluded.source_message_ids_json,
            validation_issues_json=excluded.validation_issues_json,
            error_code=NULL, updated_at=excluded.updated_at
        RETURNING id
        """,
        (
            group["id"],
            group["group_revision"],
            "ready" if validated["is_lead"] else "not_lead",
            int(validated["is_lead"]),
            MODEL_NAME,
            int(verification_used),
            raw.model_dump_json(),
            json.dumps(validated, ensure_ascii=False),
            json.dumps(bundle["transcripts"], ensure_ascii=False),
            json.dumps(bundle["message_ids"], ensure_ascii=False),
            json.dumps(issues, ensure_ascii=False),
            now,
            now,
        ),
    )
    extraction_id = int(cursor.fetchone()[0])
    db.execute("DELETE FROM lead_field_evidence WHERE extraction_id = ?", (extraction_id,))
    for field_name, item_index, evidence in iter_validated_evidence(validated):
        db.execute(
            """
            INSERT INTO lead_field_evidence(
                extraction_id, field_name, item_index, message_id,
                source_type, quote, confidence, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                extraction_id,
                field_name,
                item_index,
                evidence["message_id"],
                evidence["source_type"],
                evidence["quote"],
                evidence["confidence"],
                now,
            ),
        )

    db.execute(
        """
        UPDATE lead_groups
        SET needs_reextract = CASE WHEN group_revision = ? THEN 0 ELSE 1 END,
            last_extracted_revision = CASE
                WHEN group_revision = ? THEN ? ELSE last_extracted_revision END,
            updated_at = ?
        WHERE id = ?
        """,
        (
            group["group_revision"],
            group["group_revision"],
            group["group_revision"],
            now,
            group["id"],
        ),
    )
    db.commit()
    return extraction_id


def save_error(db: sqlite3.Connection, group: sqlite3.Row, error_code: str) -> None:
    now = utc_now()
    db.execute(
        """
        INSERT INTO lead_extractions(
            lead_group_id, group_revision, status, model_name,
            error_code, created_at, updated_at
        ) VALUES (?, ?, 'error', ?, ?, ?, ?)
        ON CONFLICT(lead_group_id, group_revision) DO UPDATE SET
            status='error', model_name=excluded.model_name,
            error_code=excluded.error_code, updated_at=excluded.updated_at
        """,
        (group["id"], group["group_revision"], MODEL_NAME, error_code, now, now),
    )
    db.commit()


def process_group(db: sqlite3.Connection, group: sqlite3.Row) -> tuple[str, int | None, bool]:
    bundle = group_sources(db, group["id"])
    primary = call_extraction(bundle)
    issues = detect_issues(primary, bundle)
    final_extraction = primary
    verification_used = False

    if issues:
        try:
            final_extraction = call_verification(bundle, primary, issues)
            verification_used = True
            issues = detect_issues(final_extraction, bundle)
        except Exception as exc:
            issues.append(f"verification_failure:{type(exc).__name__}")

    validated = sanitize_extraction(final_extraction, bundle, verification_used)
    extraction_id = save_success(
        db,
        group,
        final_extraction,
        validated,
        bundle,
        sorted(set(issues)),
        verification_used,
    )
    status = "ready" if validated["is_lead"] else "not_lead"
    return status, extraction_id, verification_used


def ready_groups(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return db.execute(
        """
        SELECT * FROM lead_groups
        WHERE status = 'open'
          AND needs_reextract = 1
          AND ready_at IS NOT NULL
          AND ready_at <= ?
        ORDER BY ready_at, id
        """,
        (utc_now(),),
    ).fetchall()


def process_available(db: sqlite3.Connection) -> int:
    groups = ready_groups(db)
    for group in groups:
        try:
            status, extraction_id, verification_used = process_group(db, group)
            # Logs contain only internal IDs/status, never extracted personal data.
            print(
                f"lead_group_id={group['id']} revision={group['group_revision']} "
                f"status={status} extraction_id={extraction_id} "
                f"verification_used={str(verification_used).lower()}",
                flush=True,
            )
        except Exception as exc:
            error_code = type(exc).__name__
            save_error(db, group, error_code)
            print(
                f"lead_group_id={group['id']} revision={group['group_revision']} "
                f"status=error error_code={error_code}",
                flush=True,
            )
    return len(groups)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is missing in .env")

    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    init_db(db)

    if args.once:
        count = process_available(db)
        print(f"EXTRACTION_DONE processed={count}")
        return

    print("EXTRACTION_WORKER_STARTED", flush=True)
    while True:
        process_available(db)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
