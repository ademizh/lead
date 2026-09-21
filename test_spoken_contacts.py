"""ДОБАВЛЕНО: регрессионный тест на дубль лида из-за телефона,
продиктованного числительными вместо цифр.

Боевой прогон 21.09 (вторая проверка): голосовое "Телефон: восемь семьсот
пять сто двадцать три сорок пять шестьдесят семь" не давало ни одной цифры
для PHONE_RE в grouping_worker.py, поэтому группа оставалась без телефона в
identity_phones_json. Когда тот же номер позже написали цифрами
("+7 705 123 45 67"), сравнивать было не с чем — grouping_worker заводил
ВТОРОЙ лид на того же человека вместо того, чтобы присоединить сообщение к
первому. В интерфейсе это выглядело как два разных лида "Асель Нурлановна"
с разными Bitrix ID.

    python3 test_spoken_contacts.py
"""

from __future__ import annotations

import unittest

from spoken_contacts import spoken_phones, with_spoken_phones


class SpokenPhonesTests(unittest.TestCase):
    def test_recognizes_dictated_phone_with_country_code_digit(self) -> None:
        text = (
            "Познакомился с Асель Нурлановной, компания ТОО СтройИнвест, "
            "коммерческий директор. Телефон: восемь семьсот пять сто "
            "двадцать три сорок пять шестьдесят семь"
        )
        self.assertEqual(spoken_phones(text), ["87051234567"])

    def test_matches_last_ten_digits_of_the_same_number_written_with_plus(
        self,
    ) -> None:
        # То же требование, что проверяет grouping_worker.phone_match_keys():
        # "+7..." и "8..." для одного и того же номера должны совпадать по
        # последним 10 значащим цифрам.
        spoken = spoken_phones(
            "Телефон: восемь семьсот пять сто двадцать три сорок пять "
            "шестьдесят семь"
        )[0]
        written_digits = "77051234567"  # "+7 705 123 45 67"
        self.assertEqual(spoken[-10:], written_digits[-10:])

    def test_second_real_world_number_from_acceptance_dataset(self) -> None:
        text = (
            "Ержан Тлеуов, KazMunay Trans, зам.директора, оставил номер "
            "семь семьсот один девятьсот девяносто девять восемьдесят "
            "восемь семьдесят семь"
        )
        self.assertEqual(spoken_phones(text), ["77019998877"])

    def test_does_not_misfire_on_ordinary_phrases_with_a_couple_of_numbers(
        self,
    ) -> None:
        self.assertEqual(
            spoken_phones("Встреча в два часа сорок минут"),
            [],
        )
        self.assertEqual(
            spoken_phones("Купили три стенда и два баннера"),
            [],
        )

    def test_empty_and_none_safe(self) -> None:
        self.assertEqual(spoken_phones(""), [])
        self.assertEqual(spoken_phones(None), [])  # type: ignore[arg-type]

    def test_with_spoken_phones_appends_marker_without_changing_transcript(
        self,
    ) -> None:
        text = "Телефон: восемь семьсот пять сто двадцать три сорок пять шестьдесят семь"
        result = with_spoken_phones(text)
        self.assertTrue(result.startswith(text))
        self.assertIn("87051234567", result)

    def test_no_marker_added_when_nothing_found(self) -> None:
        text = "Просто текст без номера"
        self.assertEqual(with_spoken_phones(text), text)


class GroupingIdentitiesTests(unittest.TestCase):
    """Проверяет, что identities() в grouping_worker.py реально использует
    spoken_phones() — а не только сам модуль spoken_contacts.py."""

    def test_voice_and_text_variants_of_same_number_produce_matching_keys(
        self,
    ) -> None:
        import grouping_worker as gw

        voice_text = (
            "Телефон: восемь семьсот пять сто двадцать три сорок пять "
            "шестьдесят семь"
        )
        text_text = "Записал номер точнее: +7 705 123 45 67"

        _, voice_phones = gw.identities(voice_text)
        _, text_phones = gw.identities(text_text)

        self.assertTrue(
            gw.phone_match_keys(voice_phones) & gw.phone_match_keys(text_phones),
            "голосовой и текстовый варианты одного номера должны совпасть "
            "по phone_match_keys() — иначе grouping_worker создаст второй "
            "лид на того же человека",
        )


if __name__ == "__main__":
    unittest.main()
