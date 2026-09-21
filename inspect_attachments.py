import json
import sqlite3
from urllib.parse import urlparse


connection = sqlite3.connect("data/messages.db")

rows = connection.execute(
    """
    SELECT message_id, attachments_json, raw_json
    FROM messages
    ORDER BY id DESC
    LIMIT 10
    """
).fetchall()

found = False

for message_id, attachments_json, raw_json in rows:
    attachments = json.loads(attachments_json or "[]")
    raw_message = json.loads(raw_json)

    if not attachments:
        continue

    found = True

    print("-" * 50)
    print("Message ID ending:", message_id[-8:])
    print("Attachments:", len(attachments))
    print(
        "Hosted contents:",
        len(raw_message.get("hostedContents") or []),
    )

    for number, attachment in enumerate(
        attachments,
        start=1,
    ):
        content_url = attachment.get("contentUrl") or ""

        print(f"Attachment #{number}")
        print("Keys:", sorted(attachment.keys()))
        print(
            "Content type:",
            attachment.get("contentType"),
        )
        print(
            "Has content URL:",
            bool(content_url),
        )
        print(
            "URL host:",
            urlparse(content_url).netloc
            if content_url
            else None,
        )

connection.close()

if not found:
    print("Вложения в последних сообщениях не найдены.")
    