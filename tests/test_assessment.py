import io
import json
import unittest
from types import SimpleNamespace
from urllib.error import HTTPError

from ems_voice.fields import FIELDS, candidate
from ems_voice.provider import Config, GroqClient, ServiceError
from ems_voice.review import CHECKS, run_review
from ems_voice.workflow import HOLD_REPLY, SLOW_ID, Call, reply

PAID = "The claim was paid for $240 on March 20, 2026. The EFT number is EFT-9134."
EXPECTED = {"claim_status": "paid", "paid_amount": 240.0,
            "check_or_eft_number": "EFT-9134", "payment_date": "2026-03-20"}
KEYS = tuple(EXPECTED)


class Stub:
    config = SimpleNamespace(extract_model="extract", review_model="review")

    def __init__(self, *results):
        self.results = iter(results)
        self.requests = []

    def structured(self, *args):
        self.requests.append(args)
        result = next(self.results)
        if isinstance(result, Exception):
            raise result
        return result


def simulate(*turns):
    call = Call("practice")
    for text in turns:
        reply(call, text, None)
    return call


class WorkflowTests(unittest.TestCase):
    def test_complete_call_and_exact_json_contracts(self):
        call = simulate("Please hold", "I am back", "Repeat the claim number", PAID, "Yes, that is correct.")
        self.assertEqual(call.phase, "ended")
        self.assertEqual(tuple(call.result()), KEYS)
        self.assertEqual(call.result(), EXPECTED)
        self.assertTrue(all(run_review(call, None).scorecard.values()))
        self.assertEqual(tuple(run_review(call, None).scorecard), tuple(CHECKS))

    def test_hold_is_silent_after_first_acknowledgement(self):
        call = simulate("Please hold", "Please hold", "[hold music]")
        self.assertEqual(call.phase, "hold")
        self.assertEqual([t.text for t in call.transcript if t.speaker == "agent"].count(HOLD_REPLY), 1)
        self.assertNotIn("[hold music]", [t.text for t in call.transcript])
        reply(call, "Could you say that a little slower?", None)
        self.assertEqual(call.transcript[-1].text, SLOW_ID)
        self.assertEqual(call.phase, "active")

    def test_missing_reference_is_retried_and_stays_null(self):
        call = simulate("It was paid for $240 on March 20, 2026.", "I cannot share it.", "I cannot share it.", "Yes.")
        self.assertEqual(call.phase, "ended")
        questions = [t.text for t in call.transcript if t.speaker == "agent"]
        self.assertEqual(questions.count("What is the check or EFT number?"), 2)
        self.assertIsNone(call.result()["check_or_eft_number"])
        review = run_review(call, None).scorecard
        self.assertFalse(review["all_four_facts_collected"])
        self.assertTrue(review["read_back_done"])
        self.assertTrue(review["no_invented_data"])

    def test_correction_gets_a_new_readback(self):
        call = simulate(PAID, "No.", "Yes. Correct, but the paid amount was $250.25.")
        self.assertEqual(call.phase, "confirming")
        self.assertEqual(call.result()["paid_amount"], 250.25)
        self.assertIn("$250.25", call.transcript[-1].text)
        reply(call, "Correct.", None)
        self.assertTrue(run_review(call, None).scorecard["no_invented_data"])

    def test_punctuated_and_stuttered_confirmations_end_the_call(self):
        for confirmation in ("Yes. Correct.", "Everything is correct.",
                             "I confirm your re... Read back. Everything is correct."):
            with self.subTest(confirmation=confirmation):
                call = simulate(PAID, confirmation)
                self.assertEqual(call.phase, "ended")
                self.assertEqual(call.result(), EXPECTED)

    def test_explicit_withdrawal_clears_earlier_evidence(self):
        call = simulate(PAID, "Actually the EFT number is unavailable.", "Yes.")
        self.assertIsNone(call.result()["check_or_eft_number"])
        self.assertFalse(run_review(call, None).scorecard["all_four_facts_collected"])

    def test_short_answers_respect_the_current_question(self):
        call = simulate("Paid", "$0.00", "B7 4409 MX", "03/20/2026", "Correct")
        self.assertEqual(call.result(), {**EXPECTED, "paid_amount": 0.0, "check_or_eft_number": "B7 4409 MX"})
        self.assertEqual(call.phase, "ended")

    def test_provider_failure_is_retryable_without_duplicate_turns(self):
        stub = Stub(ServiceError("temporary"), {key: None for key in KEYS})
        call = Call("groq")
        with self.assertRaises(ServiceError):
            reply(call, "It is pending", stub)
        self.assertEqual(len(call.transcript), 1)
        self.assertEqual(call.phase, "active")
        reply(call, "It is pending", stub)
        self.assertEqual(sum(t.speaker == "representative" for t in call.transcript), 1)

    def test_groq_cannot_supply_unspoken_or_partial_values(self):
        stub = Stub({"claim_status": "paid", "paid_amount": "$24",
                     "check_or_eft_number": "EFT-0000", "payment_date": "March 20, 2026"})
        call = Call("groq")
        reply(call, PAID, stub)
        self.assertEqual(call.result(), {**EXPECTED, "paid_amount": None, "check_or_eft_number": None})

    def test_ai_reviewer_receives_transcript_only_and_checks_citations(self):
        call = simulate(PAID, "Yes")
        call.mode = "groq"
        proposed = {key: {"passed": True, "turns": [1]} for key in CHECKS}
        stub = Stub(proposed)
        result = run_review(call, stub)
        self.assertEqual(stub.requests[0][0:2], ("review", "call_review"))
        self.assertEqual(set(stub.requests[0][3]), {"fixed_claim", "rubric", "transcript"})
        self.assertEqual(result.scorecard, {**{key: True for key in CHECKS},
                                           "all_four_facts_collected": False, "read_back_done": False})

    def test_ai_reviewer_rejects_invalid_boolean_and_turn_id(self):
        call = simulate(PAID, "Yes")
        call.mode = "groq"
        good = {key: {"passed": True, "turns": [1]} for key in CHECKS}
        for invalid in [{"passed": "true", "turns": [1]}, {"passed": True, "turns": [999]}]:
            bad = {**good, "claim_number_stated": invalid}
            with self.assertRaises(ServiceError):
                run_review(call, Stub(bad))
        self.assertEqual(call.phase, "ended")


class FieldTests(unittest.TestCase):
    def test_year_first_speech_dates_require_a_single_valid_calendar_date(self):
        for text in ("2026 03 20.", "2026 320.", "2026. 03. 20.", "2026 0320."):
            with self.subTest(text=text):
                found = candidate(FIELDS[3], text, "payment_date")
                self.assertEqual(found, ("2026-03-20", text[:-1]))
        for text in ("2026.", "320.", "2026 112.", "2026 02 30.", "2026 03 20 21."):
            with self.subTest(text=text):
                self.assertIsNone(candidate(FIELDS[3], text, "payment_date"))

    def test_direct_spoken_date_does_not_depend_on_model_guessing_the_format(self):
        call = simulate("The claim was paid for $240. The EFT number is EFT-9134.")
        call.mode = "groq"
        reply(call, "2026 320.", Stub(dict.fromkeys(KEYS)))
        self.assertEqual(call.result(), EXPECTED)

    def test_amount_is_not_service_date_billed_amount_or_wrong_currency(self):
        amount = FIELDS[1]
        for text in ("Date of service 03/14/2026.", "It was not paid. The billed amount was $800.",
                     "The paid amount was EUR 240.", "It was paid for $-25.", "It might be $250."):
            with self.subTest(text=text):
                self.assertIsNone(candidate(amount, text))

    def test_us_date_and_zero_are_accepted(self):
        call = simulate("It was paid for $0.00 on 03/20/2026. Check number is 123456.")
        self.assertEqual(call.result(), {**EXPECTED, "paid_amount": 0.0, "check_or_eft_number": "123456"})

    def test_wrong_or_unrelated_dates_remain_unknown(self):
        for text in ("The payment date was 02/30/2026.", "The date of service was March 14, 2026."):
            self.assertIsNone(candidate(FIELDS[3], text, "payment_date"))

    def test_date_components_and_reference_are_not_paid_amounts(self):
        call = simulate("The claim was paid on March 20, 2026. The EFT number is EFT-9134.")
        self.assertIsNone(call.result()["paid_amount"])
        self.assertEqual(call.result()["payment_date"], "2026-03-20")

    def test_ambiguous_payment_values_are_not_accepted(self):
        for index, text in (
            (1, "The paid amount was $240 or $250."),
            (1, "The paid amount was around $240."),
            (2, "The check number is 9134 or 9135."),
            (3, "The payment date was March 20, 2026 or March 21, 2026."),
        ):
            with self.subTest(text=text):
                self.assertIsNone(candidate(FIELDS[index], text))

    def test_spelled_digits_are_literal_evidence_for_the_reference(self):
        self.assertEqual(candidate(FIELDS[2], "The EFT number is oh nine one three four."),
                         ("09134", "oh nine one three four"))


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.config = Config("test-key", "extract", "review")

    def test_transient_response_retries_once(self):
        requests = []
        def opener(request, timeout):
            requests.append(request)
            if len(requests) == 1:
                raise HTTPError(request.full_url, 503, "server error", {}, None)
            return io.BytesIO(b'{"choices":[{"finish_reason":"stop","message":{"content":"{}"}}]}')
        client = GroqClient(self.config, opener=opener, sleep=lambda _: None)
        self.assertEqual(client.structured("m", "t", "instructions", {}, {}), {})
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0].get_header("User-agent"), "EMS-Voice-Assessment/1.0")
        self.assertTrue(json.loads(requests[0].data)["response_format"]["json_schema"]["strict"])

