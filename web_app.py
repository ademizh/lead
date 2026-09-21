from __future__ import annotations

import json
import os
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from email_hygiene import email_warnings


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def resolve_local_path(raw_value: str) -> Path:
    path = Path(raw_value)
    return path if path.is_absolute() else BASE_DIR / path


DATABASE_PATH = resolve_local_path(os.getenv("DATABASE_PATH", "data/messages.db"))
ATTACHMENTS_ROOT = resolve_local_path(
    os.getenv("ATTACHMENTS_DIRECTORY", "data/attachments")
).resolve()

app = FastAPI(title="Teams → Bitrix24 Lead Flow")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")
security = HTTPBasic()


@dataclass(frozen=True)
class DashboardUser:
    username: str
    role: str


def secure_equal(left: str, right: str) -> bool:
    return secrets.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def current_user(
    credentials: Annotated[HTTPBasicCredentials, Depends(security)],
) -> DashboardUser:
    admin_user = os.getenv("DASHBOARD_ADMIN_USER", "admin")
    admin_password = os.getenv("DASHBOARD_ADMIN_PASSWORD", "")
    regular_user = os.getenv("DASHBOARD_USER", "user")
    regular_password = os.getenv("DASHBOARD_USER_PASSWORD", "")

    if not admin_password or not regular_password:
        raise HTTPException(
            status_code=500,
            detail="Dashboard passwords are not configured",
        )

    accounts = [
        (admin_user, admin_password, "admin"),
        (regular_user, regular_password, "user"),
    ]

    matched_role: str | None = None
    for expected_user, expected_password, role in accounts:
        username_ok = secure_equal(credentials.username, expected_user)
        password_ok = secure_equal(credentials.password, expected_password)
        if username_ok and password_ok:
            matched_role = role

    if not matched_role:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )

    return DashboardUser(credentials.username, matched_role)


def admin_user(user: Annotated[DashboardUser, Depends(current_user)]) -> DashboardUser:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Administrator role required")
    return user


def connect_database() -> sqlite3.Connection:
    db = sqlite3.connect(DATABASE_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    return db


# ДОБАВЛЕНО: web_app.py раньше не создавал вообще никаких таблиц и полагался
# на то, что poller/worker/content_worker/grouping_worker/extraction_worker/
# bitrix_worker уже когда-то запускались и создали свои схемы. Если открыть
# веб-интерфейс раньше или в отдельном процессе от воркеров (например, при
# первом деплое, когда сначала поднимают только UI), любой запрос падал с
# "no such table".
#
# ВАЖНО: определения ниже дословно скопированы из CREATE TABLE соответствующих
# воркеров (poller.py/worker.py/content_worker.py/grouping_worker.py/
# extraction_worker.py/bitrix_worker.py), а не сокращены. Если бы здесь были
# только "нужные для чтения" колонки, а воркер запустился бы позже web_app —
# его "CREATE TABLE IF NOT EXISTS" ничего не сделал бы (таблица уже есть) и
# схема осталась бы неполной навсегда (например, retry_lead() уже падал на
# отсутствующей next_attempt_at в таком сценарии при проверке). Поэтому здесь
# полные схемы, а довески в виде ALTER TABLE ... ADD COLUMN (которые воркеры
# делают при своём старте) остаются идемпотентными миграциями поверх них.
def ensure_schema() -> None:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = connect_database()
    try:
        db.executescript(
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
                UNIQUE (tenant_id, team_id, channel_id, message_id)
            );
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
                UNIQUE (message_db_id, attachment_id)
            );
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
            );
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
    finally:
        db.close()


@app.on_event("startup")
def on_startup() -> None:
    ensure_schema()


def json_value(raw: str | None, default: Any) -> Any:
    try:
        return json.loads(raw or "")
    except (json.JSONDecodeError, TypeError):
        return default


# ДОБАВЛЕНО: человеческие объяснения к кодам валидации. Без них в карточке
# лида было видно только "emails[0]:invalid_or_absent_in_source", и на вопрос
# "почему поле Email пустое" ответить было нечем — приходилось лезть в код.
ISSUE_EXPLANATIONS: dict[str, str] = {
    "missing_evidence": (
        "модель не указала, откуда взято значение, поэтому оно не принято "
        "(по ТЗ пустое поле лучше неверного)"
    ),
    "low_confidence": (
        "значение прочитано неуверенно — оставлено пустым, чтобы менеджер "
        "дозаполнил вручную"
    ),
    "quote_not_verified": (
        "цитату-основание не удалось найти в исходном тексте — значение "
        "не принято"
    ),
    "invalid_or_absent_in_source": (
        "адрес не найден дословно в тексте сообщения, OCR или расшифровке. "
        "Если он есть только на фотографии визитки, нужна более высокая "
        "уверенность распознавания (IMAGE_EVIDENCE_MIN_CONFIDENCE)"
    ),
    "conflicting_readings": (
        "один и тот же адрес прочитан по-разному в разных источниках — "
        "принять какой-то один было бы гаданием"
    ),
    "absent_in_source": (
        "номер не найден дословно в источнике — значение не принято"
    ),
    "cyrillic_lookalike": (
        "в адресе есть русские буквы, неотличимые от латинских — письмо "
        "не дойдёт, адрес нужно исправить вручную"
    ),
    "domain_looks_like_typo": (
        "домен похож на опечатку (подсказка с вариантом — в комментарии лида)"
    ),
    "domain_has_no_mx": (
        "у домена нет почтовых серверов — письмо на этот адрес не доставится"
    ),
    "not_in_crm_enum": (
        "значение не входит в список допустимых значений справочника Bitrix24"
    ),
    "partner_not_explicitly_supported": (
        "тип Partner не подтверждён явными словами партнёр/дистрибьютор/"
        "реселлер/интегратор — оставлен Customer"
    ),
    "not_explicitly_supported": (
        "значение не подтверждено явной формулировкой в источнике и поэтому "
        "не отправлено в CRM"
    ),
    "source_conflict": (
        "источники дают разные значения; сервис не выбирает один вариант "
        "наугад"
    ),
}

FIELD_LABELS: dict[str, str] = {
    "full_name": "Имя",
    "company": "Компания",
    "position": "Должность",
    "country": "Страна",
    "emails": "Email",
    "phones": "Телефон",
    "product_interests": "Интерес к продукту",
    "priority": "Приоритет",
    "lead_type": "Тип лида",
    "manager_assessment": "Оценка менеджера",
    "conflict": "Конфликт источников",
}


def explained_issues(issues: list[Any]) -> list[dict[str, str]]:
    """Код валидации -> понятное объяснение для карточки лида."""
    explained: list[dict[str, str]] = []
    for raw in issues:
        code = str(raw)
        field, _, reason = code.partition(":")
        if field == "conflict":
            field_name = reason.split("[")[0]
            reason = "source_conflict"
        else:
            field_name = field.split("[")[0]
        explained.append(
            {
                "code": code,
                "field": FIELD_LABELS.get(field_name, field_name),
                "explanation": ISSUE_EXPLANATIONS.get(
                    reason, reason or "причина не распознана"
                ),
            }
        )
    return explained


def extracted_field(validated: dict[str, Any], name: str) -> str | None:
    item = validated.get(name)
    if isinstance(item, dict):
        value = item.get("value")
        return str(value) if value not in (None, "") else None
    if item not in (None, ""):
        return str(item)
    return None


def extracted_list(validated: dict[str, Any], name: str) -> list[str]:
    result: list[str] = []
    for item in validated.get(name) or []:
        value = item.get("value") if isinstance(item, dict) else item
        if value not in (None, ""):
            result.append(str(value))
    return result


def extracted_emails(validated: dict[str, Any]) -> list[str]:
    # Hide values produced by an older extraction version as well.  A retry
    # will clear the same blocked address in Bitrix via bitrix_worker.py.
    return [
        value
        for value in extracted_list(validated, "emails")
        if not email_warnings(value)
    ]


def view_model(row: sqlite3.Row) -> dict[str, Any]:
    validated = json_value(row["validated_json"], {})
    full_name = extracted_field(validated, "full_name")
    company = extracted_field(validated, "company")
    title = " — ".join(value for value in [full_name, company] if value)

    crm_status = row["crm_status"] or "pending"
    if (
        crm_status == "synced"
        and int(row["synced_revision"] or 0) < int(row["group_revision"] or 0)
    ):
        crm_status = "pending"

    return {
        "id": row["id"],
        "title": title or f"Группа {row['id']}",
        "author_name": row["author_name"] or "—",
        "last_message_at": row["last_message_at"],
        "group_revision": row["group_revision"],
        "extraction_status": row["extraction_status"] or "pending",
        "crm_status": crm_status,
        "bitrix_lead_id": row["bitrix_lead_id"],
        "error_code": row["crm_error_code"] or row["extraction_error_code"],
        "emails": extracted_emails(validated),
        "phones": extracted_list(validated, "phones"),
        "lead_type": validated.get("lead_type") or "Customer",
        "priority": validated.get("priority") or "—",
    }


LEAD_QUERY = """
    SELECT
        g.*,
        e.id AS extraction_id,
        e.status AS extraction_status,
        e.is_lead,
        e.validated_json,
        e.raw_transcripts_json,
        e.validation_issues_json,
        e.error_code AS extraction_error_code,
        s.bitrix_lead_id,
        s.status AS crm_status,
        s.error_code AS crm_error_code,
        s.synced_revision
    FROM lead_groups g
    LEFT JOIN lead_extractions e
      ON e.lead_group_id = g.id
     AND e.group_revision = g.group_revision
    LEFT JOIN crm_sync_state s
      ON s.external_key = g.crm_external_key
"""


@app.get("/health")
def health() -> dict[str, str]:
    if not DATABASE_PATH.exists():
        raise HTTPException(status_code=503, detail="Database not found")
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def lead_list(
    request: Request,
    user: Annotated[DashboardUser, Depends(current_user)],
) -> HTMLResponse:
    with connect_database() as db:
        rows = db.execute(
            LEAD_QUERY
            + """
                WHERE e.is_lead = 1
                ORDER BY g.last_message_at DESC, g.id DESC
            """
        ).fetchall()

    leads = [view_model(row) for row in rows]
    statistics = {
        "total": len(leads),
        "synced": sum(lead["crm_status"] == "synced" for lead in leads),
        "pending": sum(
            lead["crm_status"] in {"pending", "processing", "partial", "retry"}
            for lead in leads
        ),
        "errors": sum(
            lead["crm_status"] in {"error", "mapping_required"} for lead in leads
        ),
    }

    return templates.TemplateResponse(
        request=request,
        name="leads.html",
        context={
            "user": user,
            "leads": leads,
            "statistics": statistics,
            "queued": request.query_params.get("queued") == "1",
        },
    )


@app.get("/leads/{group_id}", response_class=HTMLResponse)
def lead_detail(
    group_id: int,
    request: Request,
    user: Annotated[DashboardUser, Depends(current_user)],
) -> HTMLResponse:
    with connect_database() as db:
        row = db.execute(
            LEAD_QUERY + " WHERE g.id = ?",
            (group_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Lead group not found")

        sources = db.execute(
            """
            SELECT
                m.id AS message_db_id,
                m.message_id,
                m.author_name,
                m.created_at,
                m.body_text,
                m.web_url,
                result.classification,
                result.decision_reason
            FROM grouping_results result
            JOIN messages m ON m.id = result.message_db_id
            WHERE result.lead_group_id = ?
            ORDER BY m.created_at, m.id
            """,
            (group_id,),
        ).fetchall()

        artifacts = db.execute(
            """
            SELECT
                a.artifact_type,
                a.raw_text,
                a.confidence,
                a.model_name,
                m.message_id
            FROM content_artifacts a
            JOIN messages m ON m.id = a.message_db_id
            JOIN grouping_results result ON result.message_db_id = m.id
            WHERE result.lead_group_id = ?
            ORDER BY a.id
            """,
            (group_id,),
        ).fetchall()

        files = db.execute(
            """
            SELECT DISTINCT
                f.id,
                f.original_name,
                f.mime_type,
                f.size_bytes,
                m.message_id
            FROM message_files f
            JOIN messages m ON m.id = f.message_db_id
            JOIN grouping_results result ON result.message_db_id = m.id
            WHERE result.lead_group_id = ?
            ORDER BY f.id
            """,
            (group_id,),
        ).fetchall()

        evidence = []
        if row["extraction_id"]:
            evidence = db.execute(
                """
                SELECT field_name, item_index, message_id,
                       source_type, quote, confidence
                FROM lead_field_evidence
                WHERE extraction_id = ?
                ORDER BY field_name, item_index, id
                """,
                (row["extraction_id"],),
            ).fetchall()

    lead = view_model(row)
    validated = json_value(row["validated_json"], {})
    details = {
        "full_name": extracted_field(validated, "full_name"),
        "company": extracted_field(validated, "company"),
        "position": extracted_field(validated, "position"),
        "country": extracted_field(validated, "country"),
        "emails": extracted_emails(validated),
        "phones": extracted_list(validated, "phones"),
        "product_interests": extracted_list(validated, "product_interests"),
        "region": validated.get("region"),
        "priority": validated.get("priority"),
        "lead_type": validated.get("lead_type") or "Customer",
        "summary_ru": validated.get("summary_ru") or "",
        "manager_assessment": extracted_field(validated, "manager_assessment"),
        "transcripts": json_value(row["raw_transcripts_json"], []),
        # ИЗМЕНЕНО: замечания валидации показывались техническими строками
        # вида "emails[0]:invalid_or_absent_in_source" — по ним нельзя было
        # понять, почему поле в карточке пустое. Теперь рядом человеческое
        # объяснение: это и есть ответ на вопрос "почему не прочитал email".
        "validation_issues": explained_issues(
            json_value(row["validation_issues_json"], [])
        ),
    }

    return templates.TemplateResponse(
        request=request,
        name="lead_detail.html",
        context={
            "user": user,
            "lead": lead,
            "details": details,
            "sources": sources,
            "artifacts": artifacts,
            "files": files,
            "evidence": evidence,
            "queued": request.query_params.get("queued") == "1",
        },
    )


# ДОБАВЛЕНО: страница сообщений, которые не попали ни в один лид.
# Сервис намеренно не гадает: если приписка может относиться к двум открытым
# контактам сразу, группировка оставляет её AMBIGUOUS (требование ТЗ —
# "Никогда не гадайте"). Но раньше такое сообщение исчезало навсегда: оно не
# показывалось нигде в интерфейсе, и вернуть его в лид было нечем. Теперь
# менеджер видит их списком и может присоединить к нужному лиду вручную —
# это и есть "доступ к исходным сообщениям" из п. 4.1 ТЗ, доведённый до
# действия.
@app.get("/unassigned", response_class=HTMLResponse)
def unassigned_messages(
    request: Request,
    user: Annotated[DashboardUser, Depends(current_user)],
) -> HTMLResponse:
    with connect_database() as db:
        rows = db.execute(
            """
            SELECT
                m.id AS message_db_id,
                m.message_id,
                m.author_name,
                m.created_at,
                m.body_text,
                m.web_url,
                result.classification,
                result.decision_reason
            FROM grouping_results result
            JOIN messages m ON m.id = result.message_db_id
            WHERE result.lead_group_id IS NULL
            ORDER BY m.created_at DESC, m.id DESC
            LIMIT 200
            """
        ).fetchall()

        groups = db.execute(
            """
            SELECT
                g.id,
                g.author_name,
                g.last_message_at,
                e.validated_json
            FROM lead_groups g
            LEFT JOIN lead_extractions e
              ON e.lead_group_id = g.id
             AND e.group_revision = g.group_revision
            WHERE g.status = 'open'
            ORDER BY g.last_message_at DESC
            LIMIT 100
            """
        ).fetchall()

    lead_options = []
    for group in groups:
        validated = json_value(group["validated_json"], {})
        name = extracted_field(validated, "full_name")
        company = extracted_field(validated, "company")
        label = " — ".join(value for value in [name, company] if value)
        lead_options.append(
            {
                "id": group["id"],
                "label": label or f"Группа {group['id']}",
                "author_name": group["author_name"] or "—",
                "last_message_at": group["last_message_at"],
            }
        )

    return templates.TemplateResponse(
        request=request,
        name="unassigned.html",
        context={
            "user": user,
            "messages": rows,
            "lead_options": lead_options,
            "attached": request.query_params.get("attached") == "1",
        },
    )


@app.post("/unassigned/{message_db_id}/attach")
def attach_message_to_lead(
    message_db_id: int,
    user: Annotated[DashboardUser, Depends(current_user)],
    lead_group_id: Annotated[int, Form()],
) -> RedirectResponse:
    del user
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with connect_database() as db:
        result = db.execute(
            """
            SELECT id, lead_group_id, decision_reason
            FROM grouping_results
            WHERE message_db_id = ?
            """,
            (message_db_id,),
        ).fetchone()
        if not result:
            raise HTTPException(status_code=404, detail="Message not found")
        if result["lead_group_id"] is not None:
            raise HTTPException(
                status_code=409,
                detail="Message is already attached to a lead",
            )

        group = db.execute(
            """
            SELECT id, group_key, crm_external_key, group_revision
            FROM lead_groups
            WHERE id = ?
            """,
            (lead_group_id,),
        ).fetchone()
        if not group:
            raise HTTPException(status_code=404, detail="Lead group not found")

        db.execute(
            """
            UPDATE grouping_results
            SET lead_group_id = ?,
                classification = 'ATTACH_TO_GROUP',
                decision_reason = ?
            WHERE message_db_id = ?
            """,
            (
                lead_group_id,
                f"{result['decision_reason']}; manual_attach_from_dashboard",
                message_db_id,
            ),
        )

        # Поднимаем ревизию группы: extraction_worker переизвлечёт лид с
        # учётом нового сообщения, а bitrix_worker обновит уже созданную
        # карточку (второй лид не создаётся — сверка идёт по synced_revision).
        db.execute(
            """
            UPDATE lead_groups
            SET group_revision = group_revision + 1,
                needs_reextract = 1,
                ready_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (now, now, lead_group_id),
        )
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
                group["group_key"],
                group["crm_external_key"],
                int(group["group_revision"]) + 1,
                now,
                now,
            ),
        )
        db.commit()

    return RedirectResponse(url="/unassigned?attached=1", status_code=303)


@app.post("/leads/{group_id}/retry")
def retry_lead(
    group_id: int,
    user: Annotated[DashboardUser, Depends(current_user)],
) -> RedirectResponse:
    del user
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with connect_database() as db:
        lead = db.execute(
            """
            SELECT g.id, g.crm_external_key
            FROM lead_groups g
            JOIN lead_extractions e
              ON e.lead_group_id = g.id
             AND e.group_revision = g.group_revision
            WHERE g.id = ? AND e.is_lead = 1
            """,
            (group_id,),
        ).fetchone()
        if not lead:
            raise HTTPException(status_code=404, detail="Ready lead not found")

        state = db.execute(
            "SELECT id FROM crm_sync_state WHERE external_key = ?",
            (lead["crm_external_key"],),
        ).fetchone()

        if state:
            db.execute(
                """
                UPDATE crm_sync_state
                SET status = 'retry', next_attempt_at = 0,
                    locked_at = NULL, error_code = NULL, updated_at = ?
                WHERE external_key = ?
                """,
                (now, lead["crm_external_key"]),
            )
            db.commit()

    return RedirectResponse(url=f"/leads/{group_id}?queued=1", status_code=303)


@app.get("/files/{file_id}")
def protected_file(
    file_id: int,
    user: Annotated[DashboardUser, Depends(current_user)],
) -> FileResponse:
    del user
    with connect_database() as db:
        row = db.execute(
            "SELECT local_path, original_name, mime_type FROM message_files WHERE id = ?",
            (file_id,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="File not found")

    path = resolve_local_path(row["local_path"]).resolve()
    if path != ATTACHMENTS_ROOT and ATTACHMENTS_ROOT not in path.parents:
        raise HTTPException(status_code=404, detail="Invalid file path")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Stored file is unavailable")

    return FileResponse(
        path,
        media_type=row["mime_type"] or "application/octet-stream",
        filename=row["original_name"] or path.name,
    )


def unmapped_authors(db: sqlite3.Connection) -> list[sqlite3.Row]:
    # ДОБАВЛЕНО: показываем администратору авторов Teams, которые уже писали
    # в канал, но ещё не сопоставлены ни одному сотруднику Bitrix — иначе
    # узнать author_id можно было бы только прямым запросом к базе.
    return db.execute(
        """
        SELECT DISTINCT m.author_id, m.author_name
        FROM messages m
        LEFT JOIN employee_mappings em
          ON em.teams_author_id = m.author_id AND em.active = 1
        WHERE m.author_id IS NOT NULL
          AND m.author_id <> ''
          AND em.id IS NULL
        ORDER BY m.author_name
        """
    ).fetchall()


@app.get("/admin", response_class=HTMLResponse)
def admin_page(
    request: Request,
    user: Annotated[DashboardUser, Depends(admin_user)],
) -> HTMLResponse:
    with connect_database() as db:
        mappings = db.execute(
            """
            SELECT id, teams_display_name, teams_author_id, bitrix_user_id, active
            FROM employee_mappings
            ORDER BY teams_display_name, teams_author_id
            """
        ).fetchall()
        unmapped = unmapped_authors(db)

    settings = {
        "database": str(DATABASE_PATH),
        "team_id": os.getenv("TEAMS_TEAM_ID", "не задан"),
        "channel_id": os.getenv("TEAMS_CHANNEL_ID", "не задан"),
        "source_id": os.getenv("BITRIX_SOURCE_ID", "EXHIBITION"),
        "exhibition_id": os.getenv("BITRIX_EXHIBITION_ID", "не задан"),
        "poll_seconds": os.getenv("POLL_INTERVAL_SECONDS", "30"),
        "microsoft_secret_configured": bool(os.getenv("MICROSOFT_CLIENT_SECRET")),
        "bitrix_webhook_configured": bool(os.getenv("BITRIX_WEBHOOK_URL")),
        "openai_key_configured": bool(os.getenv("OPENAI_API_KEY")),
    }

    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={
            "user": user,
            "settings": settings,
            "mappings": mappings,
            "unmapped": unmapped,
            "saved": request.query_params.get("saved") == "1",
        },
    )


# ДОБАВЛЕНО: раньше сопоставление "автор Teams -> сотрудник Bitrix" можно было
# создать только вручную через SQL, хотя ТЗ прямо требует, чтобы у
# администратора был полный доступ к этой настройке через интерфейс (раздел
# 4.2), а корректный "Ответственный" в лиде — это обязательный результат
# (раздел 2). Теперь администратор может добавить/изменить сопоставление
# прямо со страницы /admin.
@app.post("/admin/mappings")
def upsert_mapping(
    user: Annotated[DashboardUser, Depends(admin_user)],
    teams_author_id: Annotated[str, Form()],
    teams_display_name: Annotated[str, Form()],
    bitrix_user_id: Annotated[int, Form()],
) -> RedirectResponse:
    del user
    teams_author_id = teams_author_id.strip()
    teams_display_name = teams_display_name.strip()

    if not teams_author_id or bitrix_user_id <= 0:
        raise HTTPException(
            status_code=400,
            detail="teams_author_id и bitrix_user_id обязательны",
        )

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connect_database() as db:
        db.execute(
            """
            INSERT INTO employee_mappings(
                teams_author_id, teams_display_name, bitrix_user_id,
                active, created_at, updated_at
            ) VALUES (?, ?, ?, 1, ?, ?)
            ON CONFLICT(teams_author_id) DO UPDATE SET
                teams_display_name = excluded.teams_display_name,
                bitrix_user_id = excluded.bitrix_user_id,
                active = 1,
                updated_at = excluded.updated_at
            """,
            (teams_author_id, teams_display_name, bitrix_user_id, now, now),
        )
        db.commit()

    return RedirectResponse(url="/admin?saved=1", status_code=303)


@app.post("/admin/mappings/{mapping_id}/deactivate")
def deactivate_mapping(
    mapping_id: int,
    user: Annotated[DashboardUser, Depends(admin_user)],
) -> RedirectResponse:
    del user
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connect_database() as db:
        db.execute(
            """
            UPDATE employee_mappings
            SET active = 0, updated_at = ?
            WHERE id = ?
            """,
            (now, mapping_id),
        )
        db.commit()

    return RedirectResponse(url="/admin?saved=1", status_code=303)
