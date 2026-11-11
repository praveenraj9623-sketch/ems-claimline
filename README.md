# EMS Claim Desk

Open Streamlit on your PC and click **Call my phone**. Twilio calls your configured mobile. Answer in your phone’s dialer and play the insurer for fictional claim **TEST-48213**, Alex Morgan, with a service date of **03/14/2026**. The dashboard shows Twilio’s call status, then the claim result and the AI reviewer scorecard.

## Windows setup

Run these commands from the project folder. If Python 3.11 or ngrok is missing, install it and reopen PowerShell. [ngrok for Windows](https://ngrok.com/download/windows)

~~~powershell
# Run these only if the corresponding tools are missing.
winget install -e --id Python.Python.3.11
winget install ngrok -s msstore
~~~

~~~powershell
if (-not (Test-Path -LiteralPath .\.venv\Scripts\python.exe)) { py -3.11 -m venv .venv }
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if (-not (Test-Path -LiteralPath .\.env)) { Copy-Item -LiteralPath .\.env.example -Destination .\.env }
notepad .\.env
~~~

Set these values in your local `.env`. Environment variables with the same names take precedence.

| Setting | Where to get it |
|---|---|
| `TWILIO_ACCOUNT_SID` | Account SID beginning with AC for the account that owns the voice number. [Twilio Console](https://www.twilio.com/console) |
| `TWILIO_AUTH_TOKEN` | Primary Auth Token for that account. [Twilio Auth Token guide](https://help.twilio.com/articles/223136027-Auth-Tokens-and-How-to-Change-Them) |
| `TWILIO_FROM_NUMBER` | Your voice-capable Twilio number in international format, including + and country code |
| `CALL_TO_NUMBER` | Your mobile number in the same format |
| `DEEPGRAM_API_KEY` | Live speech recognition and synthesis key. [Deepgram Console](https://console.deepgram.com/) |
| `GROQ_API_KEY` | Structured extraction and reviewer key. [Groq API Keys](https://console.groq.com/keys) |
| `VOICE_PUBLIC_URL` | The HTTPS **Forwarding** origin printed by ngrok, with no path |

Optional model overrides are `DEEPGRAM_STT_MODEL` (default `nova-3`), `DEEPGRAM_TTS_MODEL` (default `aura-2-thalia-en`), `GROQ_MODEL` (default `openai/gpt-oss-20b`), and `GROQ_REVIEW_MODEL` (defaults to `GROQ_MODEL`).

If you use a Twilio trial, verify your mobile in Twilio. If Twilio reports that the destination country is blocked, check your account’s Voice Geographic Permissions. [Trial destination verification](https://www.twilio.com/docs/api/errors/21219) · [Voice Geographic Permissions](https://help.twilio.com/articles/41016476253467)

### Terminal 1 · ngrok

Run the first three lines only to configure a new ngrok installation. If a tunnel already forwards to port 5000, reuse it. Copy the HTTPS origin into `VOICE_PUBLIC_URL` and keep the tunnel running. [ngrok authtoken](https://dashboard.ngrok.com/get-started/your-authtoken) · [ngrok HTTP agent endpoints](https://ngrok.com/docs/gateway/endpoints/http)

~~~powershell
$ngrokToken = Read-Host 'ngrok authtoken' -AsSecureString
ngrok config add-authtoken ([System.Net.NetworkCredential]::new('', $ngrokToken).Password)
Remove-Variable ngrokToken
ngrok http 127.0.0.1:5000 --inspect=false
~~~

### Terminal 2 · voice backend

Start FastAPI from the project folder. If a previous version is running on port 5000, stop it with **Ctrl+C** in its terminal first.

~~~powershell
.\.venv\Scripts\python.exe -m ems_voice.phone serve
~~~

### Terminal 3 · Streamlit

Start the dashboard from the project folder. Open [http://127.0.0.1:8501](http://127.0.0.1:8501) and click **Call my phone**.

~~~powershell
.\.venv\Scripts\python.exe -m streamlit run app.py --server.address=127.0.0.1 --server.port=8501
~~~

Keep the tunnel pointed at FastAPI on port 5000. Streamlit reads credentials and the destination from server settings and displays only the destination’s last four digits.

### Outbound webhook URLs

The dashboard supplies both URLs automatically in each Twilio Calls API request. The answer webhook opens the signed WebSocket at `wss://YOUR-NGROK-HOST/voice/media/<CallSid>`. If the ngrok origin changes, update `VOICE_PUBLIC_URL` and restart the voice backend before placing another call. [Twilio outbound calls](https://www.twilio.com/docs/voice/tutorials/how-to-make-outbound-phone-calls) · [Twilio bidirectional Media Streams](https://www.twilio.com/docs/voice/media-streams)

| Calls API field | URL | Method |
|---|---|---|
| Answer URL | `https://YOUR-NGROK-HOST/voice/outbound/<attempt-id>/answer` | POST |
| Status callback | `https://YOUR-NGROK-HOST/voice/outbound/<attempt-id>/status` | POST |

## Verify the call

Follow the [one-minute mobile script](INTERVIEW.md#one-minute-mobile-test). A live pass requires the button to ring your mobile, audible agent speech, a spoken reply, completed read-back, and both JSON outputs in Streamlit. The dashboard refreshes as Twilio statuses and results arrive. The files also appear in the project’s `phone_calls/<CallSid>/` folder.

| File | Contents |
|---|---|
| `claim_result.json` | Exactly `claim_status`, `paid_amount`, `check_or_eft_number`, and `payment_date`. Unavailable values are JSON `null`. Amounts are USD numbers and dates use YYYY-MM-DD. |
| `transcript.json` | Finalized representative speech and agent phrases with confirmed playback. A partially played agent turn has `"playback": "partial"`. |
| `review.json` | Exactly five boolean checks for the billing introduction, claim number, collection of all four facts, completed read-back, and absence of invented data. |
| `review_error.json` | Retryable reviewer failure. The transcript and claim result remain available. |
| `call_error.json` | Local diagnostics if streaming or synthesis fails. |

To retry a failed review, keep the voice backend running and use another project terminal. Restart the backend first if you changed its credentials.

~~~powershell
$callSid = Read-Host 'CallSid of the failed review'
.\.venv\Scripts\python.exe -m ems_voice.phone retry-review $callSid
~~~

If the dial request times out without confirmation, check the **Twilio Calls Log**. Once it confirms there is no active call, use the `release-call` command shown in the dashboard to unlock the next attempt.

### Run the offline tests

~~~powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
~~~

The tests click the Streamlit button, exercise the Twilio SDK request and signed answer and status webhooks, then run the audio session with simulated provider responses. They also cover duplicate clicks, hold, spell-back, interruption, missing facts, early hang-up, and reviewer failure. The [five-minute field change](INTERVIEW.md#add-payment_method-in-five-minutes) adds `payment_method` while preserving the four-key claim export.

## How the phone service works

The dashboard and voice backend share a private SQLite file for outbound attempts and status. A signed readiness check verifies that the public backend recognizes an attempt before the dashboard requests a call. A transaction reserves one active call, and earlier Twilio status updates cannot regress a newer status. [Twilio call progress events](https://www.twilio.com/docs/voice/api/call-resource#statuscallbackevent)

FastAPI validates Twilio HTTP and WebSocket signatures against the public origin. Deepgram Nova-3 receives Twilio’s inbound 8 kHz mu-law audio. Deepgram Aura streams speech audio back through the same call. Recognized speech interrupts queued agent audio. The service retires outstanding playback marks before sending `clear` so discarded audio cannot credit a read-back. [Deepgram live audio](https://developers.deepgram.com/reference/speech-to-text/listen-streaming) · [Twilio media, marks, and clear](https://www.twilio.com/docs/voice/media-streams/websocket-messages)

The workflow requires literal representative evidence and field validation for payment values. The separate Groq reviewer receives the finalized transcript. WebSocket closure and the terminal callback share the finalization path. Call files stay under the ignored `phone_calls` directory.
