"""ДОБАВЛЕНО: диагностика состояния лидов в Bitrix24.

Отвечает на вопрос "сервис пишет status=synced, а лида в Bitrix не видно —
где он?". Ничего не меняет в CRM, только читает.

    python check_bitrix.py            # общая картина по порталу
    python check_bitrix.py 35         # подробно по конкретному лиду
"""

from __future__ import annotations

import os
import sys
from typing import Any

import requests

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


WEBHOOK = os.getenv("BITRIX_WEBHOOK_URL", "").strip()


def call(method: str, payload: dict[str, Any] | None = None) -> Any:
    if not WEBHOOK:
        sys.exit("BITRIX_WEBHOOK_URL не задан в .env")
    response = requests.post(
        WEBHOOK.rstrip("/") + "/" + method + ".json", json=payload or {}, timeout=45
    )
    data = response.json()
    if data.get("error"):
        return {"__error__": f"{data.get('error')}: {data.get('error_description', '')}"}
    return data.get("result")


def show_lead(lead_id: str) -> None:
    lead = call("crm.lead.get", {"id": lead_id})

    if isinstance(lead, dict) and lead.get("__error__"):
        print(f"Лид {lead_id}: НЕ НАЙДЕН в Bitrix ({lead['__error__']}).")
        print("  -> Скорее всего его удалили вручную. После этого исправления")
        print("     сервис создаст лид заново при следующем обновлении группы.")
        return

    if not lead:
        print(f"Лид {lead_id}: пустой ответ портала.")
        return

    status = str(lead.get("STATUS_ID") or "")
    print(f"Лид {lead_id}: СУЩЕСТВУЕТ")
    print(f"  Название:     {lead.get('TITLE')}")
    print(f"  Статус:       {status}")
    print(f"  Ответственный ID: {lead.get('ASSIGNED_BY_ID')}")
    print(f"  Изменён:      {lead.get('DATE_MODIFY')}")
    print(f"  Телефон:      {lead.get('PHONE')}")
    print(f"  Email:        {lead.get('EMAIL')}")
    print(f"  Комментарий:  {len(str(lead.get('COMMENTS') or ''))} символов")
    print(f"  UF_CRM_TEAMS_GROUP_ID: {lead.get('UF_CRM_TEAMS_GROUP_ID')}")

    if status.upper() == "CONVERTED":
        deals = call("crm.deal.list", {"filter": {"LEAD_ID": lead_id}, "select": ["ID", "TITLE"]}) or []
        contacts = call("crm.contact.list", {"filter": {"LEAD_ID": lead_id}, "select": ["ID", "NAME"]}) or []
        print()
        print("  !!! Этот лид СКОНВЕРТИРОВАН в сделку/контакт.")
        print("      Поэтому он не виден в рабочем списке лидов, а обновления")
        print("      лида не попадают в созданную из него сделку.")
        print(f"      Сделки из этого лида:  {deals}")
        print(f"      Контакты из этого лида: {contacts}")
        print("      -> Если по ТЗ нужны именно лиды, отключите в портале")
        print("         автоконвертацию: CRM -> Настройки -> Режим CRM")
        print("         (должен быть 'Классический CRM' с лидами), и проверьте")
        print("         роботов на стадии лида, создающих сделку.")


def main() -> None:
    if len(sys.argv) > 1:
        show_lead(sys.argv[1])
        return

    mode = call("crm.settings.mode.get")
    print(f"Режим CRM портала: {mode}  (1 = классический с лидами, 2 = простой, без лидов)")
    print()

    leads = call(
        "crm.lead.list",
        {
            "order": {"ID": "DESC"},
            "select": ["ID", "TITLE", "STATUS_ID", "DATE_CREATE", "UF_CRM_TEAMS_GROUP_ID"],
        },
    ) or []

    if isinstance(leads, dict) and leads.get("__error__"):
        print(f"crm.lead.list вернул ошибку: {leads['__error__']}")
        return

    print(f"Всего лидов видно через API: {len(leads)}")
    ours = [lead for lead in leads if lead.get("UF_CRM_TEAMS_GROUP_ID")]
    converted = [lead for lead in leads if str(lead.get("STATUS_ID") or "").upper() == "CONVERTED"]
    print(f"  из них созданы этим сервисом (есть UF_CRM_TEAMS_GROUP_ID): {len(ours)}")
    print(f"  из них сконвертированы в сделки (STATUS_ID=CONVERTED):     {len(converted)}")
    print()

    for lead in leads[:15]:
        print(
            f"  id={lead['ID']:>4}  status={str(lead.get('STATUS_ID')):<12} "
            f"{str(lead.get('DATE_CREATE'))[:16]}  {lead.get('TITLE')}"
        )

    if converted:
        print()
        print("ВНИМАНИЕ: часть лидов сконвертирована в сделки — в интерфейсе они")
        print("видны в разделе 'Сделки', а НЕ в 'Лиды'. Это настройка портала,")
        print("а не сервиса. Подробности по одному лиду: python check_bitrix.py <ID>")


if __name__ == "__main__":
    main()
