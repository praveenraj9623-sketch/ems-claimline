# EMS ClaimLine

EMS ClaimLine follows up on ambulance insurance claims. The agent plays the Cedar Vale EMS billing caller, and you play the insurance representative for fictional claim **TEST-48213**, Alex Morgan, with a service date of **03/14/2026**.

- **Chat** accepts typed or recorded replies. A recorded reply produces both text and audio.
- **Book a call → Talk here** starts a live browser call. Click **Start voice call** and allow microphone access. The opening plays automatically. Speak naturally and pause for the response. Say “Please hold,” stay silent, and speak again when you are ready. Use **End call** to stop the microphone and see the saved results.
- **Book a call → Call my phone** places a HuskyVoice call to the mobile number configured in `CALL_TO_NUMBER`. Answer it in your phone's dialer.

## HuskyVoice phone calls

Set `HUSKYVOICE_API_KEY` in the ignored local `.env`. Use a key with `calls:create` and `calls:read`. Set `HUSKYVOICE_AGENT_ID` to the agent ID from your HuskyVoice dashboard. The app uses your existing `CALL_TO_NUMBER` and `GROQ_API_KEY`. [HuskyVoice key permissions](https://docs.huskyvoice.ai/api/api-keys/)

Apply the [Cedar Vale greeting and prompt](HUSKYVOICE_SARAH.md) in HuskyVoice and confirm Sarah has an outbound number assigned. Start Streamlit and click **Book a call → Call my phone**, then click the blue **Call my phone** button once. The HuskyVoice route uses Streamlit alone.

If the API doesn't return the transcript, open the matching call in **HuskyVoice → All Calls → Insights**, paste the transcript into the app, and click **Import and review**. The app accepts transcript JSON or text with speaker labels such as `Sarah:` and `Representative:`.

On a private Streamlit Community Cloud deployment, configure `HUSKYVOICE_API_KEY`, `HUSKYVOICE_AGENT_ID`, `CALL_TO_NUMBER`, and `GROQ_API_KEY` as top-level app secrets.

## Windows setup

Run these commands from the project folder. Install Python 3.11 if needed. The ngrok install is for the optional legacy Twilio setup. [ngrok for Windows](https://ngrok.com/download/windows)

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

Set the values you need in your local `.env`. Text chat and microphone transcription use `GROQ_API_KEY`. Browser audio replies also use `DEEPGRAM_API_KEY`. For the legacy Twilio dialer, explicitly set `VOICE_TRANSPORT=twilio` and use the Twilio settings below. Environment variables with the same names take precedence.

| Setting | Where to get it |
|---|---|
| `TWILIO_ACCOUNT_SID` | Account SID beginning with AC for the account that owns the voice number. [Twilio Console](https://www.twilio.com/console) |
| `TWILIO_AUTH_TOKEN` | Primary Auth Token for that account. [Twilio Auth Token guide](https://help.twilio.com/articles/223136027-Auth-Tokens-and-How-to-Change-Them) |
| `TWILIO_FROM_NUMBER` | Your voice-capable Twilio number in international format, including + and country code |
| `CALL_TO_NUMBER` | Your mobile number in the same format |
| `DEEPGRAM_API_KEY` | Telephone speech recognition and audio synthesis for phone and browser. [Deepgram Console](https://console.deepgram.com/) |
| `GROQ_API_KEY` | Browser microphone transcription, structured extraction, and review. [Groq API Keys](https://console.groq.com/keys) |
| `VOICE_PUBLIC_URL` | The HTTPS **Forwarding** origin printed by ngrok, with no path |

Optional model overrides are `GROQ_STT_MODEL` for browser recordings (default `whisper-large-v3-turbo`), `DEEPGRAM_STT_MODEL` for telephone audio (default `nova-3`), `DEEPGRAM_TTS_MODEL` (default `aura-2-thalia-en`), `GROQ_MODEL` (default `openai/gpt-oss-20b`), and `GROQ_REVIEW_MODEL` (defaults to `GROQ_MODEL`).

The optional legacy Twilio route requires Media Streams. Twilio's current Voice trial limits outbound API calls to its templates and blocks Media Streams, which the EMS voice agent uses. If Twilio reports a blocked destination country after the upgrade, check Voice Geographic Permissions. [Twilio Voice trial limits](https://www.twilio.com/docs/usage/trials/try-out-voice) · [Voice Geographic Permissions](https://help.twilio.com/articles/41016476253467)

For **Chat**, run the Streamlit command under Terminal 3. For live browser voice, also run the local voice service below. For the optional Twilio route, run Terminals 1–3. HuskyVoice calls use only the Streamlit command in Terminal 3.

### Live browser voice

From the same project folder, open a separate PowerShell terminal and run:

~~~powershell
.\.venv\Scripts\python.exe -m ems_voice.browser_voice
~~~

Leave it running on port 5001. Open [Streamlit on this PC](http://127.0.0.1:8501) in Chrome or Edge. Use the existing `GROQ_API_KEY` and `DEEPGRAM_API_KEY` in `.env`. The browser voice service runs on localhost and uses a direct audio connection to the Streamlit page. For recorded turns, expand **Voice options** and enable **Use recorded replies**.

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

Start the dashboard from the project folder. Open [http://127.0.0.1:8501](http://127.0.0.1:8501), then choose **Chat** or **Book a call**.

~~~powershell
.\.venv\Scripts\python.exe -m streamlit run app.py --server.address=127.0.0.1 --server.port=8501
~~~

If using the legacy Twilio route, keep its tunnel pointed at FastAPI on port 5000. Streamlit reads credentials and the destination from server settings and displays only the destination’s last four digits.

### Outbound webhook URLs

The dashboard supplies both URLs automatically in each Twilio Calls API request. The answer webhook opens the signed WebSocket at `wss://YOUR-NGROK-HOST/voice/media/<CallSid>`. If the ngrok origin changes, update `VOICE_PUBLIC_URL` and restart the voice backend before placing another call. [Twilio outbound calls](https://www.twilio.com/docs/voice/tutorials/how-to-make-outbound-phone-calls) · [Twilio bidirectional Media Streams](https://www.twilio.com/docs/voice/media-streams)

| Calls API field | URL | Method |
|---|---|---|
| Answer URL | `https://YOUR-NGROK-HOST/voice/outbound/<attempt-id>/answer` | POST |
| Status callback | `https://YOUR-NGROK-HOST/voice/outbound/<attempt-id>/status` | POST |

## Verify chat and voice

In **Talk here**, start the live call and say “Please hold.” Wait a few seconds, ask for the claim number slowly, then provide the claim status, paid amount, check or EFT reference, and payment date. Try saying the date as “2026 … 03 … 20” with brief pauses. The bars beside the call controls respond to the microphone while you speak. Say “Yes” after the read-back. The claim result and review appear below the transcript. You can run the same script by typing in **Chat**.

In **Book a call → Call my phone**, click the call button and answer on your mobile. A live pass requires the button to ring your mobile, audible agent speech, a spoken reply, completed read-back, and both JSON outputs in Streamlit. The dashboard refreshes as HuskyVoice status and transcript data arrive. The phone transcript appears with the two results after the call.

The app saves chat and recorded turns under `chat_calls/<conversation-id>/`, live browser calls under `browser_calls/<conversation-id>/`, HuskyVoice records under `huskyvoice_calls/<attempt-id>/`, and legacy Twilio records under `phone_calls/<CallSid>/`. Git ignores these folders. The live browser reviewer uses the transcript and playback confirmations.

| File | Contents |
|---|---|
| `claim_result.json` | Exactly `claim_status`, `paid_amount`, `check_or_eft_number`, and `payment_date`. Unavailable values are JSON `null`. Amounts are USD numbers and dates use YYYY-MM-DD. |
| `transcript.json` | Finalized representative speech and agent phrases with confirmed playback. A partially played agent turn has `"playback": "partial"`. |
| `review.json` | Exactly five boolean keys: `introduced_as_billing_office`, `claim_number_stated`, `all_four_facts_collected`, `read_back_done`, and `no_invented_data`. |
| `review_error.json` | Retryable reviewer failure. The transcript and claim result remain available. |
| `call_error.json` | Local diagnostics if streaming or synthesis fails. |

Click **Retry review** under the failed scorecard. The saved result and transcript stay available during the retry. For a phone review, keep the voice backend running. You can also queue a phone review retry from PowerShell:

~~~powershell
$callSid = Read-Host 'CallSid of the failed review'
.\.venv\Scripts\python.exe -m ems_voice.phone retry-review $callSid
~~~

If the dial request times out without confirmation, check the **Twilio Calls Log**. Once it confirms there is no active call, use the `release-call` command shown in the dashboard to unlock the next attempt.

### Run the offline tests

~~~powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
~~~

The tests exercise Streamlit chat and browser voice replies, the outbound call button, signed webhooks, and the phone audio session with simulated providers. They cover the exact JSON keys, duplicate clicks and callbacks, hold, spell-back, corrections, missing facts, early hang-up, and review retry.

## How the phone service works

The dashboard and voice backend share a private SQLite file for outbound attempts and status. A signed readiness check verifies that the public backend recognizes an attempt before the dashboard requests a call. A transaction reserves one active call, and earlier Twilio status updates cannot regress a newer status. [Twilio call progress events](https://www.twilio.com/docs/voice/api/call-resource#statuscallbackevent)

FastAPI validates Twilio HTTP and WebSocket signatures against the public origin. Deepgram Nova-3 receives Twilio’s inbound 8 kHz mu-law audio. Deepgram Aura streams speech audio back through the same call. Recognized speech interrupts queued agent audio. The service retires outstanding playback marks before sending `clear` so discarded audio cannot credit a read-back. [Deepgram live audio](https://developers.deepgram.com/reference/speech-to-text/listen-streaming) · [Twilio media, marks, and clear](https://www.twilio.com/docs/voice/media-streams/websocket-messages)

Both tabs use the same workflow, validators, and Groq reviewer. Payment values require literal evidence from the representative. Chat and Talk here review the displayed thread. The Twilio mode reviews the recognized transcript and confirmed playback, including on early hang-up. The final phone read-back must finish playing to earn credit. WebSocket closure and the terminal callback share the phone finalization path.

The callback setup in this README is for local use. Keep the Streamlit dashboard on the local machine. A public callback deployment needs authenticated call controls and per-user call records.
