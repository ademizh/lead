from __future__ import annotations

import unittest

from email_hygiene import email_warnings
from lead_rules import (
    PARTNER_RE,
    explicit_high_priority_match,
    position_quote_supports,
    priority_quote_supports,
)
from spoken_contacts import spoken_phones, with_spoken_contacts


class LeadRulesTest(unittest.TestCase):
    def test_typo_email_is_blocking(self) -> None:
        codes = {item["code"] for item in email_warnings("qa.test@gmail.con")}
        self.assertIn("domain_looks_like_typo", codes)

    def test_normal_corporate_email_is_not_guessed_invalid(self) -> None:
        self.assertEqual(email_warnings("irina@demorobotics.kz"), [])

    def test_noisy_reseller_phrase_is_partner(self) -> None:
        text = "Он хочет наценить наш продукт своим клиентам."
        self.assertIsNotNone(PARTNER_RE.search(text))

    def test_vague_cooperation_is_customer(self) -> None:
        self.assertIsNone(PARTNER_RE.search("Компания хочет сотрудничать."))

    def test_company_label_is_not_position(self) -> None:
        self.assertFalse(position_quote_supports("QA Cooperation Test"))
        self.assertFalse(position_quote_supports("QA Partner"))
        self.assertTrue(position_quote_supports("директор по закупкам"))

    def test_deadline_alone_is_not_high_priority(self) -> None:
        quote = "Нужно отправить КП до пятницы"
        self.assertFalse(priority_quote_supports("High", quote))
        self.assertIsNone(explicit_high_priority_match(quote))

    def test_explicit_urgency_is_high_priority(self) -> None:
        self.assertTrue(priority_quote_supports("High", "Срочно перезвонить"))
        self.assertIsNotNone(explicit_high_priority_match("Срочно перезвонить"))
        self.assertIsNone(explicit_high_priority_match("Не срочно"))

    def test_spoken_phone_is_reconstructed(self) -> None:
        text = (
            "Телефон: восемь семьсот пять сто двадцать три "
            "сорок пять шестьдесят семь"
        )
        self.assertEqual(spoken_phones(text), ["87051234567"])
        self.assertIn("87051234567", with_spoken_contacts(text))


if __name__ == "__main__":
    unittest.main()
