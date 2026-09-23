"""ДОБАВЛЕНО: доказательство требования ТЗ "Повторы не создают второй лид".

ТЗ называет три способа проверки; дополнительно проверяется полная
пересборка группировки, потому что именно она выявила дубли на скриншотах:

  1. повторный прогон набора целиком;
  2. перезапуск сервиса на середине обработки;
  3. догрузка за уже обработанный период.
  4. ``grouping_worker.py --rebuild`` с сохранением стабильного CRM-ключа;
  5. номер словами, затем тот же номер цифрами — обновление, не второй лид.
  6. ошибка ASR/LLM в раннем извлечении имени ("Тимур Олив") не
     разделяет два сырых источника, где буквально сказано "Тимур Алиев".

Тест поднимает сервис на временной базе и с фальшивым порталом Bitrix24
(обычный счётчик вызовов вместо сети), прогоняет все три сценария и
проверяет, что crm.lead.add вызывается ровно один раз на контакт.

    python test_idempotency.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("BITRIX_WEBHOOK_URL", "https://example.invalid/rest/1/token/")
os.environ.setdefault("TEAMS_WORKFLOW_WEBHOOK_URL", "https://example.invalid/hook")
os.environ.setdefault("BITRIX_EXHIBITION_ID", "1")
os.environ.setdefault("PRODUCT_INTERESTS", "Analytics")

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bitrix_worker as bw  # noqa: E402
import grouping_worker as gw  # noqa: E402


CALLS: list[str] = []
LEADS: dict[str, dict] = {}
_next_lead_id = [100]


def fake_bitrix_call(method: str, payload: dict | None = None):
    """Фальшивый портал: считает вызовы и хранит лиды в памяти."""
    CALLS.append(method)
    payload = payload or {}

    if method == "crm.lead.add":
        _next_lead_id[0] += 1
        lead_id = str(_next_lead_id[0])
        LEADS[lead_id] = dict(payload.get("fields") or {})
        LEADS[lead_id]["STATUS_ID"] = "NEW"
        return int(lead_id)

    if method == "crm.lead.get":
        return LEADS.get(str(payload.get("id")))

    if method == "crm.lead.update":
        lead_id = str(payload.get("id"))
        if lead_id in LEADS:
            LEADS[lead_id].update(payload.get("fields") or {})
        return True

    if method == "crm.lead.list":
        wanted = (payload.get("filter") or {}).get("UF_CRM_TEAMS_GROUP_ID")
        return [
            {"ID": lead_id}
            for lead_id, fields in LEADS.items()
            if wanted and fields.get("UF_CRM_TEAMS_GROUP_ID") == wanted
        ]

    if method == "crm.duplicate.findbycomm":
        return {}

    return True


bw.bitrix_call = fake_bitrix_call
bw.send_teams_confirmation = lambda **kwargs: None
bw.full_input_comment = lambda db, group, extraction: "полный ввод менеджера"


MESSAGES = [
    ("msg-1", "2026-09-21T10:00:00+00:00", "Иван Петров, ООО Ромашка, +7 705 123 45 67"),
    ("msg-2", "2026-09-21T10:00:30+00:00", "Перезвонить в понедельник"),
    ("msg-3", "2026-09-21T11:00:00+00:00", "Новый контакт: Анна Смирнова, +7 701 999 88 77"),
]


def build_database(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    gw.init_db(db)
    bw.initialize_database(db)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT, team_id TEXT,
            channel_id TEXT, message_id TEXT, created_at TEXT, reply_to_id TEXT,
            author_id TEXT, author_name TEXT, body_text TEXT, processing_status TEXT,
            UNIQUE (tenant_id, team_id, channel_id, message_id)
        );
        CREATE TABLE IF NOT EXISTS content_artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, message_db_id INTEGER, file_id INTEGER,
            artifact_type TEXT, raw_text TEXT, confidence REAL, status TEXT
        );
        CREATE TABLE IF NOT EXISTS lead_extractions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, lead_group_id INTEGER,
            group_revision INTEGER, status TEXT, validated_json TEXT,
            raw_transcripts_json TEXT DEFAULT '[]', source_message_ids_json TEXT DEFAULT '[]',
            UNIQUE(lead_group_id, group_revision)
        );
        CREATE TABLE IF NOT EXISTS message_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT, message_db_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS grouping_results_dummy (id INTEGER PRIMARY KEY);
        """
    )
    db.execute(
        "INSERT OR IGNORE INTO employee_mappings(teams_author_id, bitrix_user_id, active)"
        " VALUES ('mgr-1', 1, 1)"
    )
    db.commit()
    return db


def ingest(db: sqlite3.Connection, messages) -> None:
    """Поллер: повторная загрузка тех же сообщений ничего не дублирует
    (UNIQUE по tenant/team/channel/message_id)."""
    for message_id, created_at, body in messages:
        db.execute(
            "INSERT OR IGNORE INTO messages (tenant_id, team_id, channel_id, message_id,"
            " created_at, author_id, author_name, body_text, processing_status)"
            " VALUES ('t','tm','ch',?,?, 'mgr-1','Менеджер',?, 'content_ready')",
            (message_id, created_at, body),
        )
    db.commit()


def run_grouping(db: sqlite3.Connection) -> None:
    gw.process_available(db)


def run_extraction(db: sqlite3.Connection) -> None:
    """Заглушка извлечения: детерминированная, без обращения к модели."""
    groups = db.execute("SELECT * FROM lead_groups WHERE needs_reextract = 1").fetchall()
    for group in groups:
        rows = db.execute(
            "SELECT m.message_id, m.body_text FROM grouping_results r"
            " JOIN messages m ON m.id = r.message_db_id"
            " WHERE r.lead_group_id = ? ORDER BY m.created_at",
            (group["id"],),
        ).fetchall()
        phones = sorted(gw.identities(" ".join(r["body_text"] or "" for r in rows))[1])
        validated = {
            "is_lead": True,
            "full_name": {"value": f"Контакт группы {group['id']}", "evidence": []},
            "company": {"value": None, "evidence": []},
            "position": {"value": None, "evidence": []},
            "country": {"value": None, "evidence": []},
            "emails": [],
            "phones": [{"value": phone} for phone in phones],
            "product_interests": [],
            "summary_ru": "тестовая выжимка",
            "lead_type": "Customer",
        }
        db.execute(
            "INSERT OR REPLACE INTO lead_extractions(lead_group_id, group_revision, status,"
            " validated_json, raw_transcripts_json, source_message_ids_json)"
            " VALUES (?,?, 'ready', ?, '[]', ?)",
            (
                group["id"],
                group["group_revision"],
                json.dumps(validated, ensure_ascii=False),
                json.dumps([r["message_id"] for r in rows]),
            ),
        )
        db.execute(
            "UPDATE lead_groups SET needs_reextract = 0 WHERE id = ?", (group["id"],)
        )
    db.commit()


def run_crm_sync(db: sqlite3.Connection, stop_after: int | None = None) -> int:
    """Синхронизация. stop_after имитирует падение сервиса на середине."""
    rows = db.execute(
        """
        SELECT g.*, e.validated_json, e.raw_transcripts_json, e.source_message_ids_json,
               e.group_revision AS extraction_revision
        FROM lead_groups g
        JOIN lead_extractions e ON e.lead_group_id = g.id
                               AND e.group_revision = g.group_revision
        WHERE e.status = 'ready'
        ORDER BY g.id
        """
    ).fetchall()

    processed = 0
    for row in rows:
        if stop_after is not None and processed >= stop_after:
            print(f"    [сервис остановлен после {processed} лид(ов)]")
            break
        bw.process_candidate(db, row, row, {}, dry_run=False)
        processed += 1
    return processed


def lead_add_count() -> int:
    return CALLS.count("crm.lead.add")


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        db = build_database(db_path)

        print("=" * 70)
        print("СЦЕНАРИЙ 1. Повторный прогон набора целиком")
        print("=" * 70)
        ingest(db, MESSAGES)
        run_grouping(db)
        run_extraction(db)
        run_crm_sync(db)
        after_first = lead_add_count()
        print(f"  создано лидов после первого прогона: {after_first}")
        assert after_first == 2, f"ожидали 2 контакта, получили {after_first}"

        for attempt in range(2):
            ingest(db, MESSAGES)
            run_grouping(db)
            run_extraction(db)
            run_crm_sync(db)
            print(f"  повтор {attempt + 1}: создано лидов всего — {lead_add_count()}")
            assert lead_add_count() == after_first, "повторный прогон создал дубль!"
        print("  РЕЗУЛЬТАТ: дублей нет\n")

        print("=" * 70)
        print("СЦЕНАРИЙ 2. Перезапуск сервиса на середине обработки")
        print("=" * 70)
        db2 = build_database(Path(tmp) / "test2.db")
        CALLS.clear()
        LEADS.clear()
        ingest(db2, MESSAGES)
        run_grouping(db2)
        run_extraction(db2)

        run_crm_sync(db2, stop_after=1)
        mid = lead_add_count()
        print(f"  до падения создано лидов: {mid}")

        db2.close()                      # имитация остановки процесса
        db2 = sqlite3.connect(Path(tmp) / "test2.db")
        db2.row_factory = sqlite3.Row
        print("  [сервис перезапущен, состояние прочитано из базы]")

        run_crm_sync(db2)
        total = lead_add_count()
        print(f"  после перезапуска создано лидов всего: {total}")
        assert total == 2, f"перезапуск создал дубль: {total} вместо 2"
        print("  РЕЗУЛЬТАТ: обработка продолжилась с места остановки, дублей нет\n")

        print("=" * 70)
        print("СЦЕНАРИЙ 3. Догрузка за уже обработанный период")
        print("=" * 70)
        before = lead_add_count()
        # Полный обход канала возвращает и старые сообщения, и одно новое.
        backfill = MESSAGES + [
            ("msg-4", "2026-09-21T10:00:45+00:00", "Ещё по нему: нужен прайс"),
        ]
        ingest(db2, backfill)
        run_grouping(db2)
        run_extraction(db2)
        run_crm_sync(db2)
        after = lead_add_count()
        print(f"  лидов до догрузки: {before}, после: {after}")
        assert after == before, "догрузка старого периода создала дубль!"

        updates = CALLS.count("crm.lead.update")
        print(f"  при этом вызовов обновления лида: {updates}")
        assert updates > 0, "новое сообщение должно было ОБНОВИТЬ существующий лид"
        print("  РЕЗУЛЬТАТ: новое сообщение обновило существующий лид, второй не создан\n")

        print("=" * 70)
        print("СЦЕНАРИЙ 4. Полная пересборка группировки")
        print("=" * 70)
        before_rebuild = lead_add_count()
        gw.rebuild_derived_grouping(db2)
        run_grouping(db2)
        run_extraction(db2)
        run_crm_sync(db2)
        after_rebuild = lead_add_count()
        print(
            f"  лидов до пересборки: {before_rebuild}, "
            f"после: {after_rebuild}"
        )
        assert after_rebuild == before_rebuild, (
            "--rebuild изменил стабильный CRM-ключ и создал дубль!"
        )
        print("  РЕЗУЛЬТАТ: group/CRM key пережил пересборку, дублей нет\n")

        print("=" * 70)
        print("СЦЕНАРИЙ 5. Номер словами, затем уточнение цифрами")
        print("=" * 70)
        db3 = build_database(Path(tmp) / "test3.db")
        CALLS.clear()
        LEADS.clear()
        correction_messages = [
            (
                "corr-1",
                "2026-09-21T12:00:00+00:00",
                "Познакомился с Асель Нурлановной, компания ТОО СтройИнвест, "
                "коммерческий директор. Телефон: восемь семьсот пять сто "
                "двадцать три сорок пять шестьдесят семь",
            ),
            (
                "corr-2",
                "2026-09-21T12:00:30+00:00",
                "Асель Нурлановна, СтройИнвест — записал номер точнее: "
                "+7 705 123 45 67",
            ),
        ]
        ingest(db3, correction_messages)
        run_grouping(db3)
        grouped = db3.execute(
            "SELECT COUNT(DISTINCT lead_group_id) FROM grouping_results "
            "WHERE message_db_id IN "
            "(SELECT id FROM messages WHERE message_id LIKE 'corr-%')"
        ).fetchone()[0]
        assert grouped == 1, f"уточнение телефона разделилось на {grouped} группы"

        spoken_fp = bw.identity_fingerprint(
            {"emails": [], "phones": [{"value": "8 705 123 45 67"}]}
        )
        numeric_fp = bw.identity_fingerprint(
            {"emails": [], "phones": [{"value": "+7 705 123 45 67"}]}
        )
        assert spoken_fp == numeric_fp, "+7 и 8 получили разные ключи телефона"

        run_extraction(db3)
        run_crm_sync(db3)
        assert lead_add_count() == 1, "уточнение телефона создало второй лид"
        print("  РЕЗУЛЬТАТ: обе записи в одной группе, crm.lead.add вызван один раз\n")

        print("=" * 70)
        print("СЦЕНАРИЙ 6. Ошибка в извлечённом имени не создаёт дубль")
        print("=" * 70)
        db4 = build_database(Path(tmp) / "test4.db")
        CALLS.clear()
        LEADS.clear()
        ingest(
            db4,
            [
                (
                    "timur-voice",
                    "2026-09-21T11:27:55+00:00",
                    "Тимур Алиев, номер 8705-332-446, занимается "
                    "строительством и декорацией.",
                )
            ],
        )
        run_grouping(db4)
        timur_group_id = db4.execute(
            "SELECT lead_group_id FROM grouping_results WHERE message_db_id = "
            "(SELECT id FROM messages WHERE message_id = 'timur-voice')"
        ).fetchone()[0]

        # Имитируем ошибку со скриншота: raw transcript
        # говорит "Тимур Алиев", а extracted full_name — "Тимур Олив".
        wrong_extraction = {
            "is_lead": True,
            "full_name": {"value": "Тимур Олив", "evidence": []},
            "company": {"value": None, "evidence": []},
            "position": {"value": None, "evidence": []},
            "country": {"value": None, "evidence": []},
            "emails": [],
            "phones": [{"value": "8705332446"}],
            "product_interests": [],
            "summary_ru": "тест",
            "lead_type": "Customer",
        }
        db4.execute(
            "INSERT INTO lead_extractions(lead_group_id, group_revision, status, "
            "validated_json) VALUES (?, 1, 'ready', ?)",
            (timur_group_id, json.dumps(wrong_extraction, ensure_ascii=False)),
        )
        db4.execute(
            "UPDATE lead_groups SET needs_reextract = 0 WHERE id = ?",
            (timur_group_id,),
        )
        db4.commit()

        ingest(
            db4,
            [
                (
                    "timur-follow-up",
                    "2026-09-21T11:30:32+00:00",
                    "Тимур Алиев сказал, что получил презентацию и хочет "
                    "новости о стенде.",
                )
            ],
        )
        run_grouping(db4)
        result = db4.execute(
            "SELECT lead_group_id, classification, decision_reason FROM grouping_results "
            "WHERE message_db_id = (SELECT id FROM messages "
            "WHERE message_id = 'timur-follow-up')"
        ).fetchone()
        assert result["lead_group_id"] == timur_group_id, "Тимура разделило на две группы"
        assert result["classification"] == "ATTACH_TO_GROUP"
        assert "same_person_name_in_raw_group_source" in result["decision_reason"]
        run_extraction(db4)
        run_crm_sync(db4)
        assert lead_add_count() == 1, "два сообщения о Тимуре создали два CRM-лида"
        print("  РЕЗУЛЬТАТ: одна группа, crm.lead.add вызван один раз\n")

        print("=" * 70)
        print("СЦЕНАРИЙ 7. Другое имя в lower case — это новый лид")
        print("=" * 70)
        db5 = build_database(Path(tmp) / "test5.db")
        CALLS.clear()
        LEADS.clear()
        ingest(
            db5,
            [
                (
                    "anna-voice",
                    "2026-09-21T11:50:51+00:00",
                    "Анна Иванова, CEO, номер 777-874-560, спрашивала "
                    "про презентацию.",
                )
            ],
        )
        run_grouping(db5)
        anna_group_id = db5.execute(
            "SELECT lead_group_id FROM grouping_results WHERE message_db_id = "
            "(SELECT id FROM messages WHERE message_id = 'anna-voice')"
        ).fetchone()[0]
        anna_extraction = {
            "is_lead": True,
            "full_name": {"value": "Анна Иванова", "evidence": []},
            "company": {"value": None, "evidence": []},
            "position": {"value": "CEO", "evidence": []},
            "country": {"value": None, "evidence": []},
            "emails": [],
            "phones": [{"value": "777874560"}],
            "product_interests": [],
            "summary_ru": "тест",
            "lead_type": "Customer",
        }
        db5.execute(
            "INSERT INTO lead_extractions(lead_group_id, group_revision, status, "
            "validated_json) VALUES (?, 1, 'ready', ?)",
            (anna_group_id, json.dumps(anna_extraction, ensure_ascii=False)),
        )
        db5.execute(
            "UPDATE lead_groups SET needs_reextract = 0 WHERE id = ?",
            (anna_group_id,),
        )
        db5.commit()

        ingest(
            db5,
            [
                (
                    "almas-note",
                    "2026-09-21T11:52:54+00:00",
                    "алмас дидар получил презентацию, встреча перенесена на субботу",
                )
            ],
        )
        run_grouping(db5)
        almas_result = db5.execute(
            "SELECT lead_group_id, classification, decision_reason FROM grouping_results "
            "WHERE message_db_id = (SELECT id FROM messages WHERE message_id = 'almas-note')"
        ).fetchone()
        assert almas_result["classification"] == "NEW_GROUP"
        assert almas_result["lead_group_id"] != anna_group_id
        assert "different_leading_named_subject" in almas_result["decision_reason"]
        print("  РЕЗУЛЬТАТ: Алмас не прикреплён к Анне, создана отдельная группа\n")

        # Windows does not allow TemporaryDirectory to delete SQLite files
        # while a connection still has an open file handle. Linux tolerated
        # this, which hid the cleanup defect in earlier test runs.
        for connection in (db, db2, db3, db4, db5):
            connection.close()

        print("=" * 70)
        print("ВСЕ СЕМЬ ПРОВЕРОК ИДЕМПОТЕНТНОСТИ ПРОЙДЕНЫ")
        print("=" * 70)


if __name__ == "__main__":
    main()
