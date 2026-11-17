"""Twilio pieces implemented with the standard library: REST call, signature check, TwiML."""

import base64
import hashlib
import hmac
import json
import urllib.error
import urllib.parse
import urllib.request
from xml.sax.saxutils import escape, quoteattr

HINTS = ("hold, please hold, claim number, claim status, paid, denied, pending, processing, EFT, check number, "
         "zero, one, two, three, four, five, six, seven, eight, nine, ten, twenty, thirty, hundred, thousand, "
         "dollars, cents, January, February, March, April, May, June, July, August, September, October, November, "
         "December, yes, no, correct, incorrect")
TWILIO_HELP = {
    21219: "This Twilio trial account can only call verified numbers. Verify CALL_TO_NUMBER in the Twilio console "
           "(Phone Numbers → Verified Caller IDs).",
    21215: "Twilio blocks calls to that country. Enable it under Voice → Geographic Permissions.",
    21211: "CALL_TO_NUMBER is not a valid phone number. Use international format, e.g. +919876543210.",
    21212: "TWILIO_FROM_NUMBER is not a valid Twilio number.",
    21606: "TWILIO_FROM_NUMBER is not a voice-capable number on this Twilio account.",
    20003: "Twilio rejected the credentials. Check TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN.",
}


class TwilioError(Exception):
    pass


def compute_signature(auth_token: str, url: str, params: dict) -> str:
    """Twilio's X-Twilio-Signature: base64(HMAC-SHA1(token, url + sorted key/value pairs))."""
    data = url + "".join(key + value for key in sorted(params) for value in sorted(params[key]))
    digest = hmac.new(auth_token.encode(), data.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def valid_signature(auth_token: str, url: str, params: dict, signature: str) -> bool:
    return bool(signature) and hmac.compare_digest(compute_signature(auth_token, url, params), signature)


class TwilioClient:
    def __init__(self, account_sid: str, auth_token: str, *, base: str = "https://api.twilio.com", timeout: float = 20,
                 trial_mode: bool = False):
        self.sid, self.token, self.base, self.timeout = account_sid, auth_token, base.rstrip("/"), timeout
        self.trial_mode = trial_mode

    def place_call(self, *, to: str, from_: str, answer_url: str, status_url: str) -> str:
        fields = [("To", to), ("From", from_), ("Url", answer_url), ("StatusCallback", status_url)]
        if not self.trial_mode:
            fields += [("Method", "POST"), ("StatusCallbackMethod", "POST"), ("Timeout", "40")]
            fields += [("StatusCallbackEvent", e) for e in ("initiated", "ringing", "answered", "completed")]
        request = urllib.request.Request(
            f"{self.base}/2010-04-01/Accounts/{self.sid}/Calls.json", data=urllib.parse.urlencode(fields).encode(),
            method="POST", headers={
                "Authorization": "Basic " + base64.b64encode(f"{self.sid}:{self.token}".encode()).decode(),
                "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read())["sid"]
        except urllib.error.HTTPError as error:
            try:
                body = json.loads(error.read(4000))
            except ValueError:
                body = {}
            code = body.get("code")
            message = TWILIO_HELP.get(code) or body.get("message") or f"Twilio returned HTTP {error.code}."
            raise TwilioError(f"{message} (Twilio error {code})" if code else message) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise TwilioError("Twilio could not be reached. Check the internet connection.") from None
        except (KeyError, ValueError):
            raise TwilioError("Twilio returned an unexpected response.") from None


def say_element(say, voice: str) -> str:
    inner = say.ssml if say.ssml else escape(say.text)
    return f"<Say voice={quoteattr(voice)} language=\"en-US\">{inner}</Say>"


def gather_attrs(action: str, expect: str, quiet: bool, wait: bool) -> str:
    speech_timeout = {"date": "4", "number": "3"}.get(expect, "2")
    start_timeout = "30" if wait else "7" if quiet else "8"
    return (f'input="speech" action={quoteattr(action)} method="POST" language="en-US" '
            f'speechTimeout="{speech_timeout}" timeout="{start_timeout}" speechModel="phone_call" enhanced="true" '
            f'actionOnEmptyResult="true" profanityFilter="false" hints={quoteattr(HINTS)}')


def build_twiml(reply, *, action: str, voice: str) -> str:
    """TwiML for one agent reply. Non-interruptible speech plays before the Gather;
    questions are inside it so the representative can talk over them."""
    parts = ['<?xml version="1.0" encoding="UTF-8"?><Response>']
    says = list(reply.says)
    if reply.end:
        parts += [say_element(s, voice) for s in says]
        parts.append("<Pause length=\"1\"/><Hangup/></Response>")
        return "".join(parts)
    lead = []
    while says and not says[0].interruptible:
        lead.append(says.pop(0))
    parts += [say_element(s, voice) for s in lead]
    parts.append(f"<Gather {gather_attrs(action, reply.expect, reply.quiet, reply.wait)}>")
    parts += [say_element(s, voice) for s in says]
    parts.append("</Gather>")
    parts.append(f"<Redirect method=\"POST\">{escape(action)}</Redirect></Response>")
    return "".join(parts)
