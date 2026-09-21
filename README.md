# Teams → Bitrix24: сервис обработки лидов с выставки

Сервис слушает канал Microsoft Teams, распознаёт в потоке смешанных
сообщений (текст, фото визиток, голосовые) отдельные контакты, извлекает и
проверяет структурированные поля лида и заводит/обновляет карточку лида в
Bitrix24 без ручных шагов.

> Этот README и `DECISIONS.md` добавлены при проверке проекта — в присланном
> архиве не было ни README, ни `.env.example`, ни каталогов `templates/` и
> `static/`, необходимых `web_app.py` для запуска. Список конкретных правок
> кода — в конце этого файла и в комментариях `ДОБАВЛЕНО:` / `ИСПРАВЛЕНО:`
> прямо в коде.

## Архитектура

```
poller.py            — опрашивает канал Teams (Graph API), пишет сообщения в SQLite
worker.py            — скачивает вложения (фото визиток, голосовые) из Graph
content_worker.py    — OCR визиток (EasyOCR) + транскрибация голоса (faster-whisper)
grouping_worker.py   — группирует сообщения в лиды: "один контакт = один лид"
extraction_worker.py — извлекает поля лида (OpenAI structured output) с проверкой evidence
bitrix_worker.py     — создаёт/обновляет лид в Bitrix24, ищет дубли, шлёт подтверждение в Teams
web_app.py           — веб-интерфейс (FastAPI): список лидов, карточка, повторная отправка, админка
run_service.py       — supervisor: проверяет .env и поднимает все воркеры одним процессом-родителем
```

Все воркеры общаются только через одну SQLite-базу (`data/messages.db`,
режим WAL) — она же durable-очередь заданий (таблицы `processing_jobs`,
`crm_sync_state` с `attempts`/`next_attempt_at`/backoff). Внешнего брокера
очередей нет: для нагрузки "200–400 лидов за 3 дня" одного файла SQLite с
WAL достаточно, и это резко упрощает эксплуатацию.

Подробные инженерные решения (в т.ч. ответы на раздел 5 ТЗ) — в
[`DECISIONS.md`](./DECISIONS.md).

## Установка с нуля

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -r requirements.txt
pip install -r requirements-ml.txt   # OCR/транскрибация — тяжёлые зависимости

cp .env.example .env
# заполните .env реальными значениями — см. комментарии внутри файла
```

### Переменные окружения

Полный список с описанием каждой переменной — в [`.env.example`](./.env.example).
Кратко, обязательные:

| Переменная | Назначение |
|---|---|
| `MICROSOFT_TENANT_ID`, `MICROSOFT_CLIENT_ID`, `MICROSOFT_CLIENT_SECRET` | app-only доступ к Microsoft Graph |
| `TEAMS_TEAM_ID`, `TEAMS_CHANNEL_ID` | канал-источник лидов |
| `TEAMS_WORKFLOW_WEBHOOK_URL` | куда слать подтверждение менеджеру в канал |
| `BITRIX_WEBHOOK_URL`, `BITRIX_EXHIBITION_ID` | целевой портал Bitrix24 и ID выставки |
| `OPENAI_API_KEY` | группировка/извлечение полей |
| `DASHBOARD_ADMIN_PASSWORD`, `DASHBOARD_USER_PASSWORD` | доступ в веб-интерфейс (без них сервис откажется отдавать страницы — паролей "по умолчанию" нет умышленно) |

`run_service.py --check` проверяет, что все обязательные переменные заданы,
без запуска воркеров:

```bash
python3 run_service.py --check
```

## Сквозной прогон

```bash
python3 run_service.py
```

поднимет все шесть воркеров одним процессом-супервизором (перезапускает
упавший воркер через 3 секунды) и напечатает `SERVICE_CHECK_OK`.

Веб-интерфейс запускается отдельно (у него другой рантайм — ASGI):

```bash
uvicorn web_app:app --host 0.0.0.0 --port 8000
```

Откройте `http://localhost:8000`, войдите под `DASHBOARD_ADMIN_USER` /
`DASHBOARD_ADMIN_PASSWORD` (Basic Auth).

Проверка "вручную": напишите в тестовый канал текст с email/телефоном
("Познакомился с Ивановым, ООО Ромашка, +7 999 000-00-00, ivanov@romashka.ru,
просил перезвонить") — через `POLL_INTERVAL_SECONDS` + `GROUP_DEBOUNCE_SECONDS`
секунд лид должен появиться на `/`, а в канал — прийти подтверждение с ID
карточки в Bitrix24.

Отдельные шаги можно прогнать по одному разу без ожидания опроса:

```bash
python3 grouping_worker.py --once
python3 extraction_worker.py --once
python3 bitrix_worker.py --once          # --dry-run — посчитать, что было бы отправлено, без записи в Bitrix
```


