import unittest

from claimline.agent import Agent, HOLD_REPLY, is_hold_request
from claimline.review import CHECKS, review_call

KEYS = ["claim_status", "paid_amount", "check_or_eft_number", "payment_date"]


def run(lines, **kw):
    agent = Agent(**kw)
    replies = [agent.start()]
    for line in lines:
        replies.append(agent.hear(line) if line else agent.silence())
    return agent, replies


def score(agent):
    return review_call(agent.transcript, agent.result())[0]


class Opening(unittest.TestCase):
    def test_introduces_and_states_claim_number(self):
        text = Agent().start().text
        self.assertIn("billing office of Cedar Vale EMS", text)
        self.assertIn("TEST-48213", text)
        self.assertIn("What is the current claim status?", text)


class HappyPath(unittest.TestCase):
    def test_full_call_one_fact_at_a_time(self):
        agent, replies = run(["The status is paid", "$240", "EFT 9134", "March 20, 2026", "Yes, that's correct"])
        self.assertEqual(agent.result(), {"claim_status": "paid", "paid_amount": 240.0,
                                          "check_or_eft_number": "9134", "payment_date": "2026-03-20"})
        self.assertEqual(list(agent.result()), KEYS)
        readback = [t["text"] for t in agent.transcript if "read back" in t["text"]][0]
        for piece in ("paid", "$240.00", "9134", "March 20, 2026"):
            self.assertIn(piece, readback)
        self.assertTrue(replies[-1].end)
        self.assertEqual(score(agent), {k: True for k in CHECKS})

    def test_facts_volunteered_together_are_not_asked_again(self):
        agent, replies = run(["It was paid for 240 dollars on March 20, 2026, EFT number 9134"])
        self.assertIn("read back", replies[1].text)
        agent.hear("yes")
        self.assertEqual(score(agent), {k: True for k in CHECKS})
        asked = " ".join(t["text"] for t in agent.transcript if t["speaker"] == "agent")
        self.assertEqual(asked.count("What amount was paid?"), 0)

    def test_yes_variants_end_the_call(self):
        for yes in ["Yes. Correct.", "yeah that's right", "Correct", "Everything is correct"]:
            agent, replies = run(["paid", "240 dollars", "EFT 9134", "March 20 2026", yes])
            self.assertTrue(replies[-1].end, yes)


class Hold(unittest.TestCase):
    def test_hold_is_polite_silent_and_never_hangs_up(self):
        agent, replies = run(["Please hold", "", "", "", "Hold hold the call.", "", "I'm back"])
        self.assertEqual(replies[1].text, HOLD_REPLY)
        for r in replies[2:6]:
            self.assertEqual(r.says, [])
            self.assertTrue(r.wait)
            self.assertFalse(r.end)
        said = [t["text"] for t in agent.transcript if t["speaker"] == "agent"]
        self.assertEqual(sum(HOLD_REPLY in s for s in said), 1)
        self.assertTrue(replies[-1].text.startswith("Thank you."))
        self.assertIn("claim status", replies[-1].text)
        self.assertNotEqual(agent.phase, "ended")

    def test_details_survive_a_hold(self):
        agent, _ = run(["paid", "one moment please", "", "I'm back, it was 240 dollars"])
        self.assertEqual(agent.result()["claim_status"], "paid")
        self.assertEqual(agent.result()["paid_amount"], 240.0)

    def test_hold_detection(self):
        for yes in ["please hold", "Hold on", "one moment", "give me a second", "let me check that", "Hold hold the call."]:
            self.assertTrue(is_hold_request(yes.lower()), yes)
        self.assertFalse(is_hold_request("the claim is on hold pending review"))
        self.assertFalse(is_hold_request("thanks for holding"))


class SlowClaimNumber(unittest.TestCase):
    def test_spelled_one_character_at_a_time(self):
        agent, replies = run(["Could you say the claim number slowly?"])
        say = replies[1].says[0]
        self.assertEqual(say.spell, ["T", "E", "S", "T", "dash", "four", "eight", "two", "one", "three"])
        self.assertIn("T. E. S. T. Dash. Four. Eight. Two. One. Three.", replies[1].text)
        self.assertIn("claim status", replies[1].text)  # returns to the pending question

    def test_other_phrasings(self):
        for ask in ["spell the claim number", "can you repeat that one character at a time", "What's the claim number?"]:
            _, replies = run([ask])
            self.assertTrue(replies[1].says[0].spell, ask)


class MissingFacts(unittest.TestCase):
    def test_unavailable_date_keeps_the_previously_reported_amount(self):
        agent, _ = run(["paid", "450", "EFT 921", "I don't have the payment date",
                        "The payment date is unavailable", "yes"])
        self.assertEqual(agent.result(), {"claim_status": "paid", "paid_amount": 450.0,
                                          "check_or_eft_number": "921", "payment_date": None})
        self.assertTrue(score(agent)["no_invented_data"])

    def test_unknown_amount_with_extension_is_reasked_and_left_null(self):
        agent, replies = run(["paid", "I don't know the paid amount. My extension is 450."])
        self.assertIsNone(agent.result()["paid_amount"])
        self.assertIn("What amount was paid?", replies[-1].text)
        for message in ("I don't have the amount", "EFT 921", "March 20, 2026", "yes"):
            agent.hear(message)
        self.assertIsNone(agent.result()["paid_amount"])
        self.assertFalse(score(agent)["all_four_facts_collected"])
        self.assertTrue(score(agent)["no_invented_data"])

    def test_ai_quote_cannot_turn_an_extension_into_an_amount(self):
        for text in ("My extension is 450", "I have 450 other records"):
            with self.subTest(text=text):
                agent, replies = run(["paid", text], span_picker=lambda key, text: "450")
                self.assertIsNone(agent.result()["paid_amount"])
                self.assertIn("What amount was paid?", replies[-1].text)

    def test_asks_again_once_then_reports_null(self):
        agent, replies = run(["paid", "240", "EFT 9134", "I don't know", "I don't have that", "yes"])
        texts = [t["text"] for t in agent.transcript if t["speaker"] == "agent"]
        self.assertEqual(sum("On what date was the payment made?" in t for t in texts), 2)
        self.assertEqual(agent.result()["payment_date"], None)
        readback = next(t for t in texts if "read back" in t)
        self.assertIn("Payment date was not provided", readback)
        card = score(agent)
        self.assertFalse(card["all_four_facts_collected"])
        self.assertTrue(card["read_back_done"] and card["no_invented_data"])

    def test_nothing_is_ever_invented(self):
        agent, _ = run(["", "", "", "", "", "", "", ""])
        self.assertEqual(agent.result(), {k: None for k in KEYS})
        self.assertTrue(score(agent)["no_invented_data"])

    def test_silence_escalates_to_read_back_and_closes(self):
        agent, replies = run([""] * 6)
        self.assertTrue(any("read back" in r.text for r in replies))
        self.assertTrue(replies[-1].end or agent.phase == "ended" or replies[-1].says)


class Corrections(unittest.TestCase):
    def test_late_corrections_are_read_back_before_ending(self):
        agent, _ = run(["paid", "240 dollars", "EFT 9134", "March 20 2026"])
        for amount in range(241, 251):
            reply = agent.hear(f"Actually, the paid amount was {amount} dollars")
            self.assertFalse(reply.end)
            self.assertIn(f"${amount}.00", reply.text)
        agent.hear("yes")
        self.assertTrue(all(score(agent).values()))

    def test_retracted_status_is_reported_as_missing(self):
        agent, _ = run(["paid", "240 dollars", "EFT 9134", "March 20 2026",
                        "Actually the claim status is unknown"])
        self.assertIsNone(agent.result()["claim_status"])
        self.assertIn("Claim status was not provided", agent.transcript[-1]["text"])

    def test_correct_a_value_at_read_back(self):
        agent, replies = run(["paid", "240 dollars", "EFT 9134", "March 20 2026",
                              "No, the paid amount was 250 dollars"])
        self.assertIn("$250.00", replies[-1].text)
        self.assertIn("read back", replies[-1].text)
        agent.hear("yes")
        self.assertEqual(agent.result()["paid_amount"], 250.0)
        self.assertEqual(score(agent), {k: True for k in CHECKS})

    def test_bare_no_asks_which_detail(self):
        agent, replies = run(["paid", "240 dollars", "EFT 9134", "March 20 2026", "no"])
        self.assertIn("Which detail", replies[-1].text)
        self.assertFalse(replies[-1].end)


class SplitSpeech(unittest.TestCase):
    def test_quiet_mode_waits_for_rest_of_date(self):
        agent, replies = run(["paid", "240", "EFT 9134", "2026.", "320"], quiet_partials=True)
        self.assertTrue(replies[4].quiet)
        self.assertEqual(replies[4].says, [])
        self.assertEqual(agent.result()["payment_date"], "2026-03-20")
        self.assertIn("read back", replies[5].text)

    def test_your_saved_call_now_works(self):
        agent, replies = run(["The client status is paid.", "Hold hold the call.", "$240.", "EFT", "EFT nine one three four.",
                              "2026.", "Twenty.", "Zero three twenty twenty six.", "Yes. Correct."], quiet_partials=True)
        self.assertEqual(agent.result(), {"claim_status": "paid", "paid_amount": 240.0,
                                          "check_or_eft_number": "9134", "payment_date": "2026-03-20"})
        self.assertTrue(replies[-1].end)
        self.assertEqual(score(agent), {k: True for k in CHECKS})


class EndingEarly(unittest.TestCase):
    def test_hangup_before_read_back_is_scored_false(self):
        agent, _ = run(["paid", "240 dollars"])
        card = score(agent)
        self.assertFalse(card["read_back_done"])
        self.assertFalse(card["all_four_facts_collected"])
        self.assertTrue(card["introduced_as_billing_office"] and card["claim_number_stated"])

    def test_give_up_reads_back_then_ends(self):
        agent, _ = run(["paid", "please hold"])
        reply = agent.give_up()
        self.assertTrue(reply.end)
        self.assertIn("read back", reply.text)
        self.assertTrue(score(agent)["read_back_done"])


class ReviewerIsHonest(unittest.TestCase):
    def test_unrelated_number_in_result_is_flagged(self):
        agent, _ = run(["paid", "I don't know the paid amount. My extension is 450."])
        card, detail = review_call(agent.transcript, {**agent.result(), "paid_amount": 450.0})
        self.assertFalse(card["no_invented_data"])
        self.assertTrue(detail["unsupported_statements"])

    def test_unconfirmed_introduction_and_readback_are_not_credited(self):
        agent, _ = run(["paid", "240 dollars", "EFT 9134", "March 20 2026"])
        for turn in agent.transcript:
            if turn["speaker"] == "agent":
                turn["playback"] = "unconfirmed"
        card = score(agent)
        self.assertFalse(card["introduced_as_billing_office"])
        self.assertFalse(card["claim_number_stated"])
        self.assertFalse(card["read_back_done"])

    def test_readback_with_wrong_value_is_not_complete(self):
        agent, _ = run(["paid", "240 dollars", "EFT 9134", "March 20 2026"])
        agent.transcript[-1]["text"] = agent.transcript[-1]["text"].replace("$240.00", "$999.00")
        self.assertFalse(score(agent)["read_back_done"])
        self.assertFalse(score(agent)["no_invented_data"])

    def test_invented_value_is_flagged(self):
        t = [{"turn": 1, "speaker": "agent", "text": "Hello, this is the billing office of Cedar Vale EMS about claim TEST-48213."},
             {"turn": 2, "speaker": "representative", "text": "It is paid."},
             {"turn": 3, "speaker": "agent", "text": "Let me read back what I captured. Claim status is paid. Paid amount is $500.00. "
                                                      "Check or EFT number was not provided. Payment date was not provided. Is that correct?"}]
        card, detail = review_call(t)
        self.assertFalse(card["no_invented_data"])
        self.assertTrue(detail["unsupported_statements"])

    def test_result_json_with_invented_value_is_flagged(self):
        agent, _ = run(["paid"])
        card = review_call(agent.transcript, {**agent.result(), "paid_amount": 999.0})[0]
        self.assertFalse(card["no_invented_data"])

    def test_partial_playback_is_not_a_read_back(self):
        agent, _ = run(["paid", "240 dollars", "EFT 9134", "March 20 2026"])
        for t in agent.transcript:
            if "read back" in t["text"]:
                t["playback"] = "partial"
        self.assertFalse(score(agent)["read_back_done"])

    def test_scorecard_has_exactly_five_keys(self):
        agent, _ = run(["paid"])
        self.assertEqual(list(score(agent)), list(CHECKS))


class FakeAI:
    enabled = True

    def __init__(self, answer=None, error=None):
        self.answer, self.error = answer, error

    def json_object(self, *a, **k):
        if self.error:
            raise self.error
        return self.answer


class AIReviewer(unittest.TestCase):
    def good(self):
        return run(["paid", "240 dollars", "EFT 9134", "March 20 2026", "yes"])[0]

    def test_ai_can_only_downgrade(self):
        agent = self.good()
        answer = {**{k: True for k in CHECKS}, "read_back_done": False, "notes": "x"}
        card, detail = review_call(agent.transcript, agent.result(), FakeAI(answer))
        self.assertFalse(card["read_back_done"])
        self.assertIn("read_back_done", detail["disagreements"])
        agree = {**{k: True for k in CHECKS}, "notes": ""}
        self.assertEqual(review_call(agent.transcript, agent.result(), FakeAI(agree))[0], {k: True for k in CHECKS})

    def test_ai_failure_falls_back_to_audit(self):
        from claimline.groq import GroqError
        agent = self.good()
        card, detail = review_call(agent.transcript, agent.result(), FakeAI(error=GroqError("down")))
        self.assertEqual(card, {k: True for k in CHECKS})
        self.assertIn("ai_error", detail)


if __name__ == "__main__":
    unittest.main()
