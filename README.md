# EMS ClaimLine

An AI caller from the **billing office of Cedar Vale EMS** phones an insurance company about claim **TEST-48213**
(patient Alex Morgan, date of service 03/14/2026). **You play the insurance representative.**
The interface is a Streamlit app in an iOS "glass" design.

The agent introduces itself, states the claim number, collects **claim status, paid amount, check/EFT number and payment
date**, waits politely on hold, spells the claim number one character at a time on request, re-asks for anything missing,
reads everything back, and prints the result as JSON (`null` for anything it could not get; it never invents a value):

```json
{ "claim_status": "paid", "paid_amount": 240.0, "check_or_eft_number": "9134", "payment_date": "2026-03-20" }
```

A separate AI reviewer then reads the transcript and prints a scorecard with exactly five true/false keys:
`introduced_as_billing_office`, `claim_number_stated`, `all_four_facts_collected`, `read_back_done`, `no_invented_data`.

## Start (Windows)

1. Install Python 3.10 or newer.
2. Copy `.env.example` to `.env` and fill it in (your old `.env` works as is, including `VOICE_PUBLIC_URL`).
3. Double-click **Start-Windows.cmd** (Mac/Linux: `./start.sh`). The first run installs Streamlit; the page opens at
   <http://127.0.0.1:8501>.

Manual start: `pip install -r requirements.txt` then `streamlit run app.py`.

## The three modes

| Mode | What it does | Needs |
|---|---|---|
| **Phone call** | The agent rings your mobile; you pick up and talk | Twilio + ngrok |
| **Voice** | Live call in the browser; the orb pulses with your voice (blue listening, green hearing you, amber thinking, purple speaking, grey on hold) | Chrome or Edge |
| **Type** | Reply by typing | nothing |

Without any keys, Voice and Type already work and a transcript audit scores the call. Add `GROQ_API_KEY` to switch on the
AI reviewer; a check passes only when the audit and the AI both agree.

### Phone calls (Twilio)

1. In `.env` set `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`, `CALL_TO_NUMBER` (international format,
   e.g. `+919876543210`).
2. In a second window run `ngrok http 8000` (detected automatically, or set `PUBLIC_BASE_URL`).
3. Press **Call my phone** and pick up.

Trial accounts only call verified numbers and play a short trial message first (press a key when asked); enable your country
under Twilio's Voice → Geographic Permissions. The app turns Twilio's error codes into these hints. Calls use Twilio's own
speech recognition and voices, so trial accounts work and no Deepgram or media stream is needed.

For the 75-minute Voice trial, set `TWILIO_TRIAL_MODE=true` and set `TWILIO_TRIAL_WEBHOOK_TOKEN` to a private random value.
Generate a value with `python -c "import secrets; print(secrets.token_urlsafe(32))"` and copy it into your local `.env`.
The app includes it in the answer, speech, and status callback URLs to authenticate the trial Voice proxy.
Use the app's **Call my phone** button to include the token automatically. Restart Streamlit after changing settings.

### Hosting

Streamlit Community Cloud deploys this repository using `app.py` as its entrypoint. Configure credentials through app
Secrets and keep `.env`, `.streamlit/secrets.toml`, virtual environments, and call records private.
The phone workflow also needs a publicly reachable HTTPS webhook server for `/twilio/*`. Browser Voice needs a hosted
API address and an allowed HTTPS origin. The current local setup serves these on port 8000 and uses ngrok for Twilio.
Public phone and browser Voice deployment requires adapting that routing and configuring `PUBLIC_BASE_URL`.

## How it fits together

`streamlit run app.py` also starts the ClaimLine server on port 8000 inside the same process. That server answers Twilio's
webhooks (the tunnel exposes only `/twilio/*`, signed by Twilio) and the microphone API used by the Voice orb (allowed only
from the Streamlit page's origin). The Streamlit page reads call state directly, so the transcript and results update live.

- `app.py` is the Streamlit UI. `claimline/ui.py` holds the whole glass design system (one stylesheet, pure HTML helpers).
  `claimline/voice/` is the microphone component.
- `claimline/agent.py` is the caller. `numbers.py` and `extract.py` understand speech ("twenty twenty six zero three
  twenty", dates split across pauses, ambiguous dates, corrections, "I don't have that").
- `claimline/review.py` is the reviewer. `server.py` is the webhook/API server.

Every call is saved under `calls/<id>/` (`transcript.json`, `claim_result.json`, `review.json`, `review_detail.json`,
`meta.json`) and both JSON blocks print in the terminal.

## Command line (no UI)

```
python -m claimline simulate                  # rehearse in the terminal
python -m claimline review calls/<id>/transcript.json --result calls/<id>/claim_result.json
python -m claimline serve                     # headless server with a plain page at http://127.0.0.1:8000
```

## Try this

"Please hold" (stay quiet a few seconds) → "I'm back" → "Could you say the claim number slowly?" → "The claim status is
paid" → "Two hundred forty dollars" → "EFT nine one three four" → "2026 … 03 … 20" → "Yes, that's correct".
Then call again and leave the date out, correct a value during the read-back, or hang up early: the scorecard follows what
really happened.

## Tests

`python -m unittest discover -s tests` (105 tests: parsing, agent, reviewer, Twilio signing, simulated phone and browser calls,
playback acknowledgements, and the Streamlit app run against a stand-in for `st`).

## Notes

- Restart the app after editing `.env`.
- Use headphones for the Voice mode so the agent does not hear itself.
- Rotate any API key that was ever shared in a zip file or chat.
