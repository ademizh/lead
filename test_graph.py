import html
import os
import re
import sys
from urllib.parse import quote

import msal
import requests
from dotenv import load_dotenv


load_dotenv()

required_variables = [
    "MICROSOFT_TENANT_ID",
    "MICROSOFT_CLIENT_ID",
    "MICROSOFT_CLIENT_SECRET",
    "TEAMS_TEAM_ID",
    "TEAMS_CHANNEL_ID",
]

missing_variables = [
    name for name in required_variables if not os.getenv(name)
]

if missing_variables:
    print("Не заполнены переменные:")
    for name in missing_variables:
        print(f"- {name}")
    sys.exit(1)


tenant_id = os.environ["MICROSOFT_TENANT_ID"]
client_id = os.environ["MICROSOFT_CLIENT_ID"]
client_secret = os.environ["MICROSOFT_CLIENT_SECRET"]
team_id = os.environ["TEAMS_TEAM_ID"]
channel_id = os.environ["TEAMS_CHANNEL_ID"]


app = msal.ConfidentialClientApplication(
    client_id=client_id,
    authority=f"https://login.microsoftonline.com/{tenant_id}",
    client_credential=client_secret,
)

token_result = app.acquire_token_for_client(
    scopes=["https://graph.microsoft.com/.default"]
)

access_token = token_result.get("access_token")

if not access_token:
    print("TOKEN_FAILED")
    print(token_result.get("error"))
    print(token_result.get("error_description"))
    sys.exit(1)

print("TOKEN_OK")


encoded_channel_id = quote(channel_id, safe="")

url = (
    f"https://graph.microsoft.com/v1.0/"
    f"teams/{team_id}/channels/{encoded_channel_id}/messages"
)

response = requests.get(
    url,
    headers={
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    },
    params={
        "$top": 5,
        "$expand": "replies",
    },
    timeout=30,
)

print(f"HTTP status: {response.status_code}")

if not response.ok:
    print("GRAPH_FAILED")
    print(response.text)
    sys.exit(1)

messages = response.json().get("value", [])

print(f"GRAPH_OK: получено сообщений — {len(messages)}")

for message in messages:
    sender = message.get("from") or {}
    user = sender.get("user") or {}
    author = user.get("displayName", "Неизвестный автор")

    raw_content = (message.get("body") or {}).get("content", "")
    text = re.sub(r"<[^>]+>", " ", raw_content)
    text = html.unescape(text)
    text = " ".join(text.split())

    print("-" * 50)
    print(f"ID: {message.get('id')}")
    print(f"Автор: {author}")
    print(f"Время: {message.get('createdDateTime')}")
    print(f"Текст: {text}")
    print(f"Вложений: {len(message.get('attachments') or [])}")
    print(f"Ответов: {len(message.get('replies') or [])}")