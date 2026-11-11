# Phone assessment rehearsal

## Assessor plan

1. Reuse the claim workflow, field validators, and Groq transcript reviewer.
2. Use Streamlit to place one outbound call and FastAPI to answer Twilio’s signed webhooks.
3. Stream caller audio to Deepgram and stream synthesized replies back to the same call.
4. Detect turn endings and handle interruptions with Twilio playback marks and buffer clearing.
5. Export private call records and review the played conversation after the call.
6. Run offline tests, then verify audio in both directions from a mobile.
7. Cut optional voice tuning first if time runs short.

## One-minute mobile test

Click **Call my phone** in Streamlit and answer the incoming call on your mobile. Listen for Cedar Vale EMS, TEST-48213, Alex Morgan, and March 14, 2026.

1. Say “Please hold.” Wait quietly for five seconds after the acknowledgment.
2. Say “Could you say the claim number slowly?” Listen for **T, E, S, T, dash, four, eight, two, one, three**.
3. Say “The claim was paid for 240 dollars on March 20, 2026. The EFT number is 9134.” Answer any follow-up question.
4. Wait for the full read-back, say “Yes,” and listen for the goodbye.
5. Show the claim result and AI reviewer in Streamlit. The claim result should contain paid, 240, 9134, and 2026-03-20.

For an interruption check, make a second call and speak over the read-back with “No, the paid amount was 250 dollars.” Wait for the revised read-back before confirming. To check interrupted playback separately, hang up during the first read-back and check that `read_back_done` is false.

## Add payment_method in five minutes

1. Append this entry to `FIELDS` in `ems_voice/fields.py`. Keep `CORE_KEYS` at four.

   ~~~python
   Field("payment_method", "Payment method", "What was the payment method?",
         re.compile(r"\b(?:paper check|check|electronic funds transfer|EFT|ACH)\b", re.I),
         re.compile(r"\b(?:payment method|paid (?:by|via)|payment was (?:by|via))\b", re.I),
         normalize_text),
   ~~~

2. In `PhoneRecords.finish` in `ems_voice/phone_records.py`, add this after the core claim result write so the extra field has its own export.

   ~~~python
   if "payment_method" in result:
       write_json(directory / "payment_method.json", {"payment_method": result["payment_method"]})
   ~~~

3. Restart the service and begin a new call. Give the core facts, then say “The payment method was ACH.” Confirm the new value is read back and appears in `payment_method.json`. Check that `claim_result.json` still has exactly four keys.
