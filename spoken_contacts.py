"""ДОБАВЛЕНО: email, продиктованный голосом, а не написанный текстом.

На выставке менеджер часто не печатает адрес, а наговаривает его в голосовое:
"мэйл самал собачка жмэйл ком", "иван точка петров собачка мэйл точка ру".
Транскрипция сохраняет это дословно — без символа "@" и латиницы, поэтому ни
EMAIL_RE, ни модель извлечения такой адрес не видели: в карточке лида поле
Email оставалось пустым, хотя в дословной расшифровке (и в комментарии
Bitrix) адрес был прямо перед глазами.

Модуль переводит продиктованный адрес в настоящий (samal@gmail.com) и ничего
не меняет в самой расшифровке — она по ТЗ обязана остаться дословной.
Нормализованный адрес добавляется рядом, отдельной пометкой.
"""

from __future__ import annotations

import re

# Слова, которыми диктуют "@".
AT_WORDS = {"собачка", "собачкой", "собака", "собаку", "ат", "эт", "at"}

# Слова, которыми диктуют ".".
DOT_WORDS = {"точка", "точкой", "точку", "тчк", "дот", "dot"}

# Служебные слова перед адресом: "мэйл самал собачка..." — "мэйл" здесь не
# часть адреса, а объявление того, что дальше будет почта.
LEAD_IN_WORDS = {
    "мэйл", "мейл", "майл", "имейл", "имэйл", "email", "mail",
    "почта", "почту", "почты", "адрес", "адреса", "и", "а", "на", "его",
    "её", "ее", "это", "вот", "там", "потом", "оставил", "оставила",
}

# Как звучат домены и зоны.
DOMAIN_WORDS = {
    "жмэйл": "gmail", "жмейл": "gmail", "гмэйл": "gmail", "гмейл": "gmail",
    "джимейл": "gmail", "джимэйл": "gmail", "gmail": "gmail",
    "мэйл": "mail", "мейл": "mail", "майл": "mail", "mail": "mail",
    "яндекс": "yandex", "yandex": "yandex",
    "рамблер": "rambler", "аутлук": "outlook", "оутлук": "outlook",
    "хотмэйл": "hotmail", "хотмейл": "hotmail",
    "ком": "com", "com": "com",
    "ру": "ru", "ru": "ru",
    "кз": "kz", "kz": "kz",
    "нет": "net", "net": "net",
    "орг": "org", "org": "org",
    "инфо": "info", "биз": "biz", "эду": "edu", "гов": "gov",
}

TLDS = {"com", "ru", "kz", "net", "org", "info", "biz", "edu", "gov", "io", "de"}

TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}

TOKEN_SPLIT_RE = re.compile(r"[\s.,;:!?()\[\]«»\"'/\\]+")
WORD_RE = re.compile(r"^[a-zA-Zа-яёА-ЯЁ0-9_-]+$")
EMAIL_SHAPE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*@[a-z0-9-]+(?:\.[a-z0-9-]+)+$")


def transliterate(word: str) -> str:
    return "".join(TRANSLIT.get(char, char) for char in word.casefold())


def tokenize(text: str) -> list[str]:
    """Дробим по пробелам И по точкам: Whisper часто ставит точки вместо пауз
    ("мэйл.самал.собачка.жмэйл.ком"), и тогда слово "точка" в расшифровке не
    появляется вовсе."""
    return [token for token in TOKEN_SPLIT_RE.split(text) if token]


def local_part(tokens: list[str], at_index: int) -> str:
    """Имя ящика — слова ПЕРЕД "собачка", в обратном порядке."""
    parts: list[str] = []
    index = at_index - 1
    while index >= 0 and len(parts) < 6:
        token = tokens[index].casefold()
        if token in DOT_WORDS:
            parts.append(".")
            index -= 1
            continue
        if token in LEAD_IN_WORDS or token in AT_WORDS or not WORD_RE.match(token):
            break
        parts.append(transliterate(token))
        index -= 1
    parts.reverse()
    value = "".join(parts).strip(".")
    return value


def domain_part(tokens: list[str], at_index: int) -> str:
    """Домен — слова ПОСЛЕ "собачка" до зоны (ком/ру/кз) включительно."""
    parts: list[str] = []
    for token in tokens[at_index + 1 : at_index + 7]:
        lowered = token.casefold()
        if lowered in DOT_WORDS:
            continue
        if not WORD_RE.match(lowered):
            break
        mapped = DOMAIN_WORDS.get(lowered, transliterate(lowered))
        parts.append(mapped)
        if mapped in TLDS:
            break
    if len(parts) < 2 or parts[-1] not in TLDS:
        return ""
    return ".".join(parts)


def spoken_emails(text: str) -> list[str]:
    """Все адреса, продиктованные словами. Пустой список — ничего не нашли."""
    if not text:
        return []

    tokens = tokenize(text)
    found: list[str] = []

    for index, token in enumerate(tokens):
        if token.casefold() not in AT_WORDS:
            continue
        local = local_part(tokens, index)
        domain = domain_part(tokens, index)
        if not local or not domain:
            continue
        candidate = f"{local}@{domain}"
        if EMAIL_SHAPE_RE.match(candidate) and candidate not in found:
            found.append(candidate)

    return found


def with_spoken_emails(text: str) -> str:
    """Текст плюс пометка с распознанными адресами.

    Саму расшифровку не трогаем: ТЗ требует хранить её дословно. Пометка
    нужна, чтобы адрес увидели и регулярка (проверка "адрес реально есть в
    источнике"), и модель извлечения.
    """
    emails = spoken_emails(text)
    if not emails:
        return text
    return text + "\n[email, продиктованный голосом: " + ", ".join(emails) + "]"
