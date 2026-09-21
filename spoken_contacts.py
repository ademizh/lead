"""Контакты, продиктованные голосом или написанные словами.

На выставке менеджер часто не печатает адрес, а наговаривает его в голосовое:
"мэйл самал собачка жмэйл ком", "иван точка петров собачка мэйл точка ру".
Транскрипция сохраняет это дословно — без символа "@" и латиницы, поэтому ни
EMAIL_RE, ни модель извлечения такой адрес не видели: в карточке лида поле
Email оставалось пустым, хотя в дословной расшифровке (и в комментарии
Bitrix) адрес был прямо перед глазами.

Модуль переводит продиктованный адрес в настоящий (samal@gmail.com) и ничего
не меняет в самой расшифровке — она по ТЗ обязана остаться дословной.
Нормализованный адрес добавляется рядом, отдельной пометкой.

Телефон менеджер тоже может записать словами: ``восемь семьсот пять сто
двадцать три сорок пять шестьдесят семь``. Для группировки это тот же номер,
что ``+7 705 123 45 67``. Поэтому модуль также восстанавливает телефонные
цифры, не изменяя исходный текст.
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

RU_UNITS = {
    0: "ноль", 1: "один", 2: "два", 3: "три", 4: "четыре",
    5: "пять", 6: "шесть", 7: "семь", 8: "восемь", 9: "девять",
}
RU_TEENS = {
    10: "десять", 11: "одиннадцать", 12: "двенадцать",
    13: "тринадцать", 14: "четырнадцать", 15: "пятнадцать",
    16: "шестнадцать", 17: "семнадцать", 18: "восемнадцать",
    19: "девятнадцать",
}
RU_TENS = {
    20: "двадцать", 30: "тридцать", 40: "сорок", 50: "пятьдесят",
    60: "шестьдесят", 70: "семьдесят", 80: "восемьдесят",
    90: "девяносто",
}
RU_HUNDREDS = {
    100: "сто", 200: "двести", 300: "триста", 400: "четыреста",
    500: "пятьсот", 600: "шестьсот", 700: "семьсот",
    800: "восемьсот", 900: "девятьсот",
}
NUMBER_TOKEN_ALIASES = {
    "нуль": "ноль", "одна": "один", "одно": "один", "единица": "один",
    "две": "два",
}


def _number_words(value: int) -> tuple[str, ...]:
    """Каноническое русское написание числа 0..999."""
    if value < 10:
        return (RU_UNITS[value],)

    parts: list[str] = []
    hundreds = value // 100 * 100
    remainder = value % 100
    if hundreds:
        parts.append(RU_HUNDREDS[hundreds])
    if remainder in RU_TEENS:
        parts.append(RU_TEENS[remainder])
        return tuple(parts)
    tens = remainder // 10 * 10
    units = remainder % 10
    if tens:
        parts.append(RU_TENS[tens])
    if units:
        parts.append(RU_UNITS[units])
    return tuple(parts)


RU_NUMBER_PHRASES = {_number_words(value): value for value in range(1000)}
RU_NUMBER_TOKENS = {word for phrase in RU_NUMBER_PHRASES for word in phrase}


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


def spoken_phones(text: str) -> list[str]:
    """Восстанавливает номера из последовательностей русских числительных.

    Части телефона могут произноситься группами: ``восемь`` + ``семьсот
    пять`` + ``сто двадцать три`` + ``сорок пять`` + ``шестьдесят семь``.
    Самое длинное допустимое число берётся жадно, после чего части
    соединяются. Последовательности короче семи и длиннее пятнадцати цифр
    телефонами не считаются.
    """
    if not text:
        return []

    normalized = [
        NUMBER_TOKEN_ALIASES.get(token.casefold(), token.casefold())
        for token in tokenize(text)
    ]
    found: list[str] = []
    index = 0

    while index < len(normalized):
        if normalized[index] not in RU_NUMBER_TOKENS:
            index += 1
            continue

        end = index
        while end < len(normalized) and normalized[end] in RU_NUMBER_TOKENS:
            end += 1

        pieces: list[str] = []
        cursor = index
        while cursor < end:
            matched = False
            for size in range(min(3, end - cursor), 0, -1):
                phrase = tuple(normalized[cursor : cursor + size])
                if phrase not in RU_NUMBER_PHRASES:
                    continue
                pieces.append(str(RU_NUMBER_PHRASES[phrase]))
                cursor += size
                matched = True
                break
            if not matched:
                cursor += 1

        candidate = "".join(pieces)
        if 7 <= len(candidate) <= 15 and candidate not in found:
            found.append(candidate)
        index = end

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


def with_spoken_contacts(text: str) -> str:
    """Добавляет машинно-читаемые email/телефоны, сохраняя исходник."""
    annotations: list[str] = []
    emails = spoken_emails(text)
    phones = spoken_phones(text)
    if emails:
        annotations.append("email, продиктованный голосом: " + ", ".join(emails))
    if phones:
        annotations.append("телефон, продиктованный словами: " + ", ".join(phones))
    if not annotations:
        return text
    return text + "\n[" + "; ".join(annotations) + "]"
