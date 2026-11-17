import unittest

from claimline.extract import ingest
from claimline.numbers import spoken_to_digits


def values(text, focus=None, fragment=""):
    return {k: f.value for k, f in ingest(text, focus, fragment).values.items()}


class NumberWords(unittest.TestCase):
    def test_conversions(self):
        cases = {
            "nine one three four": "9 1 3 4",
            "two hundred forty dollars": "240 dollars",
            "twenty twenty six zero three twenty": "20 26 0 3 20",
            "two thousand twenty six": "2026",
            "twenty-first of March": "21st of March",
            "Oh, the status is paid": "Oh, the status is paid",
        }
        for spoken, digits in cases.items():
            self.assertEqual(spoken_to_digits(spoken), digits, spoken)


class Dates(unittest.TestCase):
    """Every phrasing below failed or was fragile in the original project."""

    def test_many_spoken_formats(self):
        for text in ["2026 0320", "2026 hyphen 03 hyphen 20", "twenty twenty six zero three twenty",
                     "zero three twenty twenty six", "Twenty, hyphen, zero three, hyphen, twenty twenty six",
                     "March 20, 2026", "20th of March 2026", "march twentieth twenty twenty six", "2026-03-20",
                     "03/20/2026", "20/03/2026", "2026 03 20", "2026 320"]:
            self.assertEqual(values(text, "payment_date").get("payment_date"), "2026-03-20", text)

    def test_split_fragments_merge(self):
        first = ingest("2026", "payment_date")
        self.assertEqual(first.values, {})
        self.assertTrue(first.fragment)
        self.assertEqual(values("320", "payment_date", first.fragment).get("payment_date"), "2026-03-20")
        again = ingest("twenty", "payment_date", ingest("2026", "payment_date").fragment)
        self.assertTrue(again.fragment or again.values)

    def test_ambiguous_date_is_not_guessed(self):
        res = ingest("03/04/2026", "payment_date")
        self.assertEqual(res.values, {})
        self.assertIn("payment_date", res.ambiguous)

    def test_month_without_year_is_partial_not_invented(self):
        res = ingest("March 20", "payment_date")
        self.assertEqual(res.values, {})
        self.assertTrue(res.fragment)

    def test_date_of_service_is_not_a_payment_date(self):
        self.assertNotIn("payment_date", values("The date of service was March 14, 2026 and it was paid", "claim_status"))
        self.assertNotIn("payment_date", values("For service date 03/14/2026 it is paid", "payment_date"))
        self.assertNotIn("payment_date", values("claim TEST-48213", None))


class Fields(unittest.TestCase):
    def test_amount_statement_does_not_imply_claim_status(self):
        for focus in ("paid_amount", "claim_status", None):
            self.assertEqual(values("I paid $450", focus), {"paid_amount": 450.0})
        self.assertEqual(values("The claim status is unpaid but I paid $450", "claim_status"),
                         {"claim_status": "not paid", "paid_amount": 450.0})
        self.assertEqual(values("The claim was paid for $450", "paid_amount"),
                         {"claim_status": "paid", "paid_amount": 450.0})

    def test_unrelated_numbers_are_not_payment_facts(self):
        cases = [
            ("I don't know the paid amount. My extension is 450.", "paid_amount"),
            ("My phone number is 5551234567.", "paid_amount"),
            ("The deductible is $450.", "paid_amount"),
            ("The billed amount is 450 dollars.", "paid_amount"),
            ("450 dollars is the deductible.", "paid_amount"),
            ("My extension is 921.", "check_or_eft_number"),
            ("The member number is 921.", "check_or_eft_number"),
            ("The appointment date is March 20, 2026.", "payment_date"),
        ]
        for text, focus in cases:
            with self.subTest(text=text):
                self.assertNotIn(focus, values(text, focus))

    def test_direct_answers_and_mixed_payment_context_are_preserved(self):
        for text in ("450", "450.", "It is 450", "Yes, the amount is 450"):
            self.assertEqual(values(text, "paid_amount")["paid_amount"], 450.0, text)
        for text in ("921", "It is 921", "Aft. 921", "EFT 9 2 1"):
            self.assertEqual(values(text, "check_or_eft_number")["check_or_eft_number"], "921", text)
        self.assertEqual(values("The deductible is $50, the paid amount is $450", "paid_amount"),
                         {"paid_amount": 450.0})

    def test_real_transcript_lines(self):
        self.assertEqual(values("The client status is paid."), {"claim_status": "paid"})
        self.assertEqual(values("$240.", "paid_amount"), {"paid_amount": 240.0})
        self.assertEqual(values("EFT 9134.", "check_or_eft_number"), {"check_or_eft_number": "9134"})
        self.assertEqual(values("EFT nine one three four.", "check_or_eft_number"), {"check_or_eft_number": "9134"})
        self.assertEqual(values("EFT9134.", "check_or_eft_number"), {"check_or_eft_number": "9134"})

    def test_everything_in_one_sentence(self):
        got = values("The claim was paid for 240 dollars on March 20, 2026. The EFT number is 9134.")
        self.assertEqual(got, {"claim_status": "paid", "paid_amount": 240.0, "payment_date": "2026-03-20",
                               "check_or_eft_number": "9134"})

    def test_paid_amount_phrase_is_not_a_status(self):
        self.assertEqual(values("The paid amount is $240", "claim_status"), {"paid_amount": 240.0})

    def test_status_variants(self):
        self.assertEqual(values("it hasn't been paid yet", "claim_status"), {"claim_status": "not paid"})
        self.assertEqual(values("It's pending review", "claim_status")["claim_status"], "pending")
        self.assertEqual(values("The claim was denied", None), {"claim_status": "denied"})

    def test_correction_and_negation(self):
        self.assertEqual(values("No, the paid amount was 250 dollars"), {"paid_amount": 250.0})
        self.assertEqual(values("not 240, it's 250 dollars"), {"paid_amount": 250.0})
        self.assertEqual(values("it was 240, actually 250 dollars"), {"paid_amount": 250.0})

    def test_two_different_amounts_are_ambiguous(self):
        res = ingest("it was 240 dollars or 250 dollars", "paid_amount")
        self.assertEqual(res.values, {})
        self.assertIn("paid_amount", res.ambiguous)

    def test_reference_formats(self):
        self.assertEqual(values("check number 5512 for 240 dollars"), {"check_or_eft_number": "5512", "paid_amount": 240.0})
        self.assertEqual(values("E F T 7 X 9 Q 2", "check_or_eft_number"), {"check_or_eft_number": "7X9Q2"})
        self.assertEqual(values("EFT", "check_or_eft_number"), {})

    def test_claim_identifiers_are_not_payment_facts(self):
        self.assertEqual(values("yes claim TEST-48213 for Alex Morgan, service date 03/14/2026", "claim_status"), {})
        self.assertEqual(values("test dash 48213", "check_or_eft_number"), {})

    def test_unavailable(self):
        res = ingest("I don't have a check number for that", "check_or_eft_number")
        self.assertIn("check_or_eft_number", res.unavailable)

    def test_foreign_currency_rejected(self):
        self.assertEqual(values("240 rupees", "paid_amount"), {})


if __name__ == "__main__":
    unittest.main()
