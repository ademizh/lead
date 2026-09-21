"""ДОБАВЛЕНО: проверка email на опечатки (пункт ТЗ "письмо не доходит").

ТЗ прямо называет эту боль: "Email вбивают с ошибкой — письмо не доходит, лид
считается «холодным» и закрывается". Раньше сервис проверял только то, что
адрес синтаксически валиден и дословно присутствует в источнике. Но
"ivan@gmail.con" синтаксически безупречен и в источнике присутствует —
и при этом мёртв.

Модуль НЕ исправляет адрес молча. ТЗ требует обратного: "Ненадёжно
прочитанное значение остаётся пустым... Пустое поле менеджер дозаполнит за
10 секунд; неверное он не заметит". Поэтому предупреждение является
блокирующим: ошибочный адрес не попадает в validated_json/Bitrix, а причина
остаётся в validation_issues и показывается в карточке лида.

Проверяются три разные причины недоставки:
  1. Кириллические буквы-двойники внутри латинского адреса (gmаil.com с
     русской "а" выглядит идентично и никогда не доставится).
  2. Опечатка в домене (gmail.con, yadnex.ru) — расстояние редактирования
     до известного домена.
  3. Домен физически не принимает почту — нет MX-записи (опционально,
     требует сети, включается переменной окружения).
"""

from __future__ import annotations

import os
import re

# Домены, которые встречаются на выставочных визитках чаще всего. Список
# нужен только как эталон для поиска опечаток — адрес на незнакомом
# корпоративном домене НЕ считается ошибкой.
KNOWN_DOMAINS = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com",
    "yahoo.com", "icloud.com", "me.com", "proton.me", "protonmail.com",
    "gmx.com", "gmx.de", "web.de", "aol.com", "zoho.com",
    "mail.ru", "bk.ru", "list.ru", "inbox.ru", "internet.ru",
    "yandex.ru", "yandex.kz", "ya.ru", "rambler.ru",
    "mail.kz", "bostonlink.kz",
}

# Кириллические буквы, неотличимые от латинских на глаз.
CYRILLIC_LOOKALIKES = {
    "а": "a", "в": "b", "е": "e", "к": "k", "м": "m", "н": "h", "о": "o",
    "р": "p", "с": "c", "т": "t", "у": "y", "х": "x", "ѕ": "s", "і": "i",
    "ј": "j", "һ": "h", "ԁ": "d", "ɡ": "g",
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O",
    "Р": "P", "С": "C", "Т": "T", "У": "Y", "Х": "X",
}

CYRILLIC_RE = re.compile(r"[Ѐ-ӿѐ-џ]")

MX_CHECK_ENABLED = os.getenv("EMAIL_MX_CHECK", "false").strip().lower() in {
    "1", "true", "yes"
}
MX_TIMEOUT_SECONDS = float(os.getenv("EMAIL_MX_TIMEOUT_SECONDS", "3"))

_mx_cache: dict[str, bool | None] = {}


def edit_distance(left: str, right: str) -> int:
    """Расстояние Левенштейна. Свой код, чтобы не тянуть зависимость."""
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)

    previous = list(range(len(right) + 1))
    for i, left_char in enumerate(left, start=1):
        current = [i]
        for j, right_char in enumerate(right, start=1):
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + (left_char != right_char),
                )
            )
        previous = current
    return previous[-1]


def latinized(value: str) -> str:
    return "".join(CYRILLIC_LOOKALIKES.get(char, char) for char in value)


def homoglyph_warning(email: str) -> dict[str, str] | None:
    """Кириллица внутри адреса — самая незаметная и самая фатальная ошибка."""
    if not CYRILLIC_RE.search(email):
        return None
    suggestion = latinized(email)
    return {
        "code": "cyrillic_lookalike",
        "message_ru": (
            "В адресе есть русские буквы, неотличимые от латинских — письмо "
            "гарантированно не дойдёт."
        ),
        "suggestion": suggestion if suggestion != email else "",
    }


def domain_typo_warning(email: str) -> dict[str, str] | None:
    """Домен похож на известный, но не совпадает с ним: gmail.con, yadnex.ru."""
    if "@" not in email:
        return None
    domain = email.rsplit("@", 1)[1].casefold()
    if domain in KNOWN_DOMAINS:
        return None

    best: tuple[int, str] | None = None
    for known in KNOWN_DOMAINS:
        # Сравниваем только с доменами сопоставимой длины: иначе короткий
        # корпоративный домен "похож" на что угодно.
        if abs(len(known) - len(domain)) > 2:
            continue
        distance = edit_distance(domain, known)
        if best is None or distance < best[0]:
            best = (distance, known)

    if best is None:
        return None

    distance, known = best
    # Расстояние 1 — почти наверняка опечатка (gmail.con -> gmail.com).
    # Расстояние 2 допускаем только для доменов подлиннее, где случайное
    # совпадение маловероятно.
    if distance == 1 or (distance == 2 and len(domain) >= 8):
        return {
            "code": "domain_looks_like_typo",
            "message_ru": (
                f"Домен «{domain}» отличается от «{known}» "
                f"на {distance} символ(а) — похоже на опечатку."
            ),
            "suggestion": email.rsplit("@", 1)[0] + "@" + known,
        }
    return None


def domain_accepts_mail(domain: str) -> bool | None:
    """Есть ли у домена MX-запись. None — проверить не удалось."""
    if domain in _mx_cache:
        return _mx_cache[domain]

    result: bool | None = None
    try:
        import dns.resolver  # type: ignore

        resolver = dns.resolver.Resolver()
        resolver.lifetime = MX_TIMEOUT_SECONDS
        resolver.timeout = MX_TIMEOUT_SECONDS
        answers = resolver.resolve(domain, "MX")
        result = len(answers) > 0
    except ImportError:
        # dnspython не установлен — молча пропускаем проверку, а не падаем.
        result = None
    except Exception:
        # NXDOMAIN, отсутствие MX, таймаут. Различать их здесь не нужно:
        # интересует только "точно принимает" против "не подтвердилось".
        result = False

    _mx_cache[domain] = result
    return result


def mx_warning(email: str) -> dict[str, str] | None:
    if not MX_CHECK_ENABLED or "@" not in email:
        return None
    domain = email.rsplit("@", 1)[1].casefold()
    accepts = domain_accepts_mail(domain)
    if accepts is False:
        return {
            "code": "domain_has_no_mx",
            "message_ru": (
                f"У домена «{domain}» нет почтовых серверов (MX) — "
                f"письмо на этот адрес не доставится."
            ),
            "suggestion": "",
        }
    return None


def email_warnings(email: str) -> list[dict[str, str]]:
    """Все замечания к адресу. Пустой список — адрес выглядит рабочим."""
    warnings: list[dict[str, str]] = []
    for check in (homoglyph_warning, domain_typo_warning, mx_warning):
        warning = check(email)
        if warning:
            warnings.append(warning)
    return warnings


def warnings_as_text(email: str, warnings: list[dict[str, str]]) -> str:
    """Готовый текст для комментария в Bitrix24 — чтобы менеджер увидел."""
    if not warnings:
        return ""
    lines = [f"Проверьте адрес {email}:"]
    for warning in warnings:
        line = "  - " + warning["message_ru"]
        if warning.get("suggestion"):
            line += f" Возможно, имелось в виду: {warning['suggestion']}"
        lines.append(line)
    return "\n".join(lines)
