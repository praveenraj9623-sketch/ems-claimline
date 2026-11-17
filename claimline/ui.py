"""iOS "liquid glass" design system for the Streamlit UI.

Everything here is plain Python returning strings, so it can be tested without Streamlit.
All dynamic text goes through `esc`, because transcripts contain whatever people said.
"""

import json
from html import escape

CHECK_LABELS = {
    "introduced_as_billing_office": "Introduced as billing office",
    "claim_number_stated": "Claim number stated",
    "all_four_facts_collected": "All four facts collected",
    "read_back_done": "Read-back done",
    "no_invented_data": "No invented data",
}
FACT_LABELS = {
    "claim_status": "Claim status",
    "paid_amount": "Paid amount",
    "check_or_eft_number": "Check or EFT number",
    "payment_date": "Payment date",
}
STATUS_TEXT = {
    "created": ("Ready", "idle"), "queued": ("Requesting the call…", "busy"), "initiated": ("Dialing…", "busy"),
    "ringing": ("Ringing: pick up your phone", "busy"), "in-progress": ("Connected", "live"),
    "completed": ("Call ended", "idle"), "busy": ("Line busy", "bad"), "failed": ("Call failed", "bad"),
    "no-answer": ("No answer", "bad"), "canceled": ("Canceled", "bad"),
}

CSS = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
:root {
  --blue:#007aff; --green:#34c759; --red:#ff3b30; --orange:#ff9500; --violet:#af52de;
  --label:#1c1c1e; --label2:rgba(60,60,67,.62); --sep:rgba(60,60,67,.14);
  --glass:rgba(255,255,255,.55); --glass-border:rgba(255,255,255,.72);
  --font:-apple-system,BlinkMacSystemFont,"SF Pro Display","SF Pro Text","Inter","Segoe UI",system-ui,sans-serif;
}
html, body, .stApp, button, input, textarea, [data-testid="stMarkdownContainer"] { font-family: var(--font) !important; color: var(--label); }
.stApp {
  background:
    radial-gradient(1200px 700px at 8% -10%, rgba(120,180,255,.55), transparent 60%),
    radial-gradient(900px 700px at 100% 0%, rgba(214,168,255,.50), transparent 60%),
    radial-gradient(1000px 800px at 50% 115%, rgba(255,190,214,.55), transparent 60%),
    linear-gradient(160deg, #eef4ff 0%, #f6efff 55%, #fff1f5 100%);
  background-attachment: fixed;
}
[data-testid="stHeader"] { background: transparent; }
[data-testid="stToolbar"], [data-testid="stDecoration"], #MainMenu, footer { display: none !important; }
[data-testid="stMainBlockContainer"], .block-container { max-width: 760px; padding: 2.6rem 1.1rem 4.5rem; }
[data-testid="stVerticalBlock"] { gap: 1rem; }

/* ---- glass surfaces ---- */
.cl-glass, [class*="st-key-glass"] {
  background: var(--glass); -webkit-backdrop-filter: blur(32px) saturate(185%); backdrop-filter: blur(32px) saturate(185%);
  border: 1px solid var(--glass-border); border-radius: 28px; padding: 22px 24px;
  box-shadow: 0 10px 40px rgba(60,70,140,.14), 0 1px 0 rgba(255,255,255,.9) inset, 0 -1px 0 rgba(255,255,255,.35) inset;
}
.cl-glass.tight { padding: 8px 18px; }

/* ---- hero ---- */
.cl-eyebrow { font-size: 12px; font-weight: 600; letter-spacing: .12em; text-transform: uppercase; color: var(--label2); }
.cl-title { font-size: 46px; line-height: 1.05; font-weight: 700; letter-spacing: -.035em; margin: 6px 0 6px; }
.cl-sub { font-size: 17px; color: var(--label2); margin: 0 0 14px; }
.cl-chips { display: flex; flex-wrap: wrap; gap: 8px; }
.cl-chip { display: inline-flex; align-items: center; gap: 6px; padding: 6px 12px; border-radius: 999px; font-size: 13px; font-weight: 500;
  background: rgba(255,255,255,.6); border: 1px solid var(--glass-border); box-shadow: 0 2px 8px rgba(60,70,140,.08); }
.cl-chip b { font-weight: 600; }

/* ---- status pill ---- */
.cl-pill { display: inline-flex; align-items: center; gap: 8px; padding: 7px 14px; border-radius: 999px; font-size: 14px; font-weight: 600;
  background: rgba(255,255,255,.65); border: 1px solid var(--glass-border); }
.cl-dot { width: 9px; height: 9px; border-radius: 50%; background: #8e8e93; }
.cl-pill.live .cl-dot { background: var(--green); box-shadow: 0 0 0 0 rgba(52,199,89,.6); animation: cl-ping 1.6s infinite; }
.cl-pill.busy .cl-dot { background: var(--orange); animation: cl-ping 1.2s infinite; }
.cl-pill.bad .cl-dot { background: var(--red); }
@keyframes cl-ping { 0% { box-shadow: 0 0 0 0 rgba(52,199,89,.55); } 80%,100% { box-shadow: 0 0 0 10px rgba(52,199,89,0); } }

/* ---- section titles, hints, alerts ---- */
.cl-h { font-size: 20px; font-weight: 700; letter-spacing: -.02em; margin: 0 0 10px; }
.cl-hint { font-size: 14px; color: var(--label2); margin: 8px 0 0; }
.cl-alert { margin-top: 12px; padding: 12px 16px; border-radius: 16px; font-size: 14px; line-height: 1.45;
  background: rgba(255,149,0,.14); border: 1px solid rgba(255,149,0,.35); }
.cl-alert.error { background: rgba(255,59,48,.12); border-color: rgba(255,59,48,.35); }

/* ---- conversation ---- */
.cl-thread { max-height: 420px; overflow-y: auto; display: flex; flex-direction: column; gap: 10px; padding: 4px 2px; }
.cl-empty { color: var(--label2); text-align: center; padding: 36px 0; font-size: 15px; }
.cl-msg { max-width: 82%; padding: 10px 15px; border-radius: 20px; font-size: 16px; line-height: 1.42; white-space: pre-wrap; word-wrap: break-word; }
.cl-msg small { display: block; font-size: 11px; font-weight: 600; letter-spacing: .04em; text-transform: uppercase; opacity: .6; margin-bottom: 2px; }
.cl-msg.agent { align-self: flex-start; background: rgba(255,255,255,.85); border: 1px solid rgba(60,60,67,.08); border-bottom-left-radius: 6px;
  box-shadow: 0 2px 10px rgba(60,70,140,.08); }
.cl-msg.rep { align-self: flex-end; color: #fff; background: linear-gradient(180deg,#4aa3ff,#007aff); border-bottom-right-radius: 6px;
  box-shadow: 0 6px 16px rgba(0,122,255,.28); }
.cl-msg.rep small { opacity: .8; }
.cl-hold { align-self: center; font-size: 13px; color: var(--label2); padding: 4px 12px; border-radius: 999px; background: rgba(120,120,128,.14); }

/* ---- grouped lists (iOS settings style) ---- */
.cl-list { margin: 0; padding: 0; list-style: none; border-radius: 18px; overflow: hidden; background: rgba(255,255,255,.6); border: 1px solid rgba(255,255,255,.7); }
.cl-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 12px 16px; font-size: 16px; }
.cl-row + .cl-row { border-top: 1px solid var(--sep); }
.cl-row .k { color: var(--label); } .cl-row .v { font-weight: 600; text-align: right; word-break: break-all; }
.cl-row .v.null { color: var(--label2); font-weight: 500; }
.cl-badge { width: 26px; height: 26px; border-radius: 50%; display: inline-flex; align-items: center; justify-content: center; color: #fff; font-size: 15px; font-weight: 700; }
.cl-badge.ok { background: var(--green); box-shadow: 0 4px 10px rgba(52,199,89,.4); } .cl-badge.no { background: var(--red); box-shadow: 0 4px 10px rgba(255,59,48,.35); }
.cl-json { margin: 12px 0 0; padding: 14px 16px; border-radius: 16px; font: 13px/1.5 ui-monospace,"SF Mono",Menlo,Consolas,monospace;
  background: rgba(28,28,30,.86); color: #e5e5ea; overflow-x: auto; white-space: pre; }
.cl-json .k { color: #9ec5ff; } .cl-json .s { color: #8fdc9c; } .cl-json .n { color: #ffb86b; } .cl-json .b { color: #6cb6ff; } .cl-json .z { color: #a0a0a8; }
.cl-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
@media (max-width: 700px) { .cl-grid { grid-template-columns: 1fr; } .cl-title { font-size: 38px; } }

/* ---- Streamlit widgets ---- */
[data-testid="stBaseButton-primary"], [data-testid="stBaseButton-primaryFormSubmit"] {
  min-height: 54px; border: 0; border-radius: 999px; font-size: 17px; font-weight: 600; color: #fff;
  background: linear-gradient(180deg,#4aa3ff,#007aff); box-shadow: 0 10px 24px rgba(0,122,255,.38), 0 1px 0 rgba(255,255,255,.45) inset;
  transition: transform .15s ease, filter .15s ease; }
[data-testid="stBaseButton-primary"]:hover, [data-testid="stBaseButton-primaryFormSubmit"]:hover { filter: brightness(1.06); transform: translateY(-1px); color:#fff; border:0; }
[data-testid="stBaseButton-primary"]:active { transform: scale(.98); }
[data-testid="stBaseButton-primary"]:disabled { filter: grayscale(.6) opacity(.55); box-shadow: none; }
[data-testid="stBaseButton-secondary"], [data-testid="stBaseButton-secondaryFormSubmit"] {
  min-height: 54px; border-radius: 999px; font-size: 16px; font-weight: 600; color: var(--label);
  background: rgba(255,255,255,.7); border: 1px solid var(--glass-border); box-shadow: 0 4px 14px rgba(60,70,140,.1); }
[data-testid="stBaseButton-secondary"]:hover, [data-testid="stBaseButton-secondaryFormSubmit"]:hover { border-color: rgba(0,122,255,.4); color: var(--blue); }
[data-testid="stButtonGroup"] { background: rgba(120,120,128,.16); border-radius: 14px; padding: 3px; display: flex; width: 100%; gap: 0; }
[data-testid="stButtonGroup"] button { flex: 1; border: 0; background: transparent; border-radius: 11px; font-weight: 600; font-size: 15px; color: #3c3c43; min-height: 38px; box-shadow: none; }
[data-testid="stButtonGroup"] button[aria-checked="true"], [data-testid="stButtonGroup"] [data-testid="stBaseButton-segmented_controlActive"] {
  background: #fff; color: var(--label); box-shadow: 0 3px 8px rgba(0,0,0,.12), 0 3px 1px rgba(0,0,0,.04); }
[data-baseweb="input"], [data-baseweb="base-input"], .stTextInput input { border-radius: 16px !important; background: rgba(255,255,255,.75) !important; border: 1px solid rgba(60,60,67,.14) !important; font-size: 16px; }
.stTextInput input:focus { box-shadow: 0 0 0 3px rgba(0,122,255,.25) !important; }
[data-testid="stForm"] { border: 0; padding: 0; }
"""


def style_tag() -> str:
    return f"<style>{CSS}</style>"


def esc(value) -> str:
    return escape(str(value), quote=True)


def hero(claim: dict) -> str:
    chips = "".join(f'<span class="cl-chip">{esc(label)} <b>{esc(value)}</b></span>' for label, value in (
        ("Claim", claim["claim_number"]), ("Patient", claim["patient_name"]), ("Service date", claim["date_of_service"])))
    return ('<div class="cl-eyebrow">Cedar Vale EMS · Billing office</div>'
            '<div class="cl-title">ClaimLine</div>'
            '<p class="cl-sub">An AI caller checks a claim by phone. You play the insurance representative.</p>'
            f'<div class="cl-chips">{chips}</div>')


def status_pill(status: str) -> str:
    text, tone = STATUS_TEXT.get(status, (status.replace("-", " ").title() or "Ready", "idle"))
    return f'<span class="cl-pill {esc(tone)}"><span class="cl-dot"></span>{esc(text)}</span>'


def alert(text: str, tone: str = "warn") -> str:
    return f'<div class="cl-alert {"error" if tone == "error" else ""}">{esc(text)}</div>'


def transcript(turns: list, phase: str = "") -> str:
    if not turns:
        return '<div class="cl-thread"><div class="cl-empty">The conversation appears here.</div></div>'
    parts = []
    for turn in turns:
        agent = turn["speaker"] == "agent"
        cls, who = ("agent", "EMS billing agent") if agent else ("rep", "You · representative")
        parts.append(f'<div class="cl-msg {cls}"><small>{who}</small>{esc(turn["text"])}</div>')
    if phase == "hold":
        parts.append('<div class="cl-hold">On hold · the agent is waiting quietly</div>')
    return '<div class="cl-thread">' + "".join(parts) + "</div>"


def _fact_text(key, value) -> str:
    if value is None:
        return ""
    if key == "paid_amount":
        return f"${value:,.2f}"
    return str(value)


def result_rows(result: dict) -> str:
    rows = []
    for key, label in FACT_LABELS.items():
        value = result.get(key)
        cell = f'<span class="v">{esc(_fact_text(key, value))}</span>' if value is not None else '<span class="v null">Not provided</span>'
        rows.append(f'<li class="cl-row"><span class="k">{esc(label)}</span>{cell}</li>')
    return '<ul class="cl-list">' + "".join(rows) + "</ul>"


def score_rows(card: dict) -> str:
    rows = []
    for key, label in CHECK_LABELS.items():
        ok = bool(card.get(key))
        rows.append(f'<li class="cl-row"><span class="k">{esc(label)}</span>'
                    f'<span class="cl-badge {"ok" if ok else "no"}">{"✓" if ok else "✕"}</span></li>')
    return '<ul class="cl-list">' + "".join(rows) + "</ul>"


def json_block(data) -> str:
    """Pretty JSON with light syntax colouring (escaped before any markup is added)."""
    import re
    text = esc(json.dumps(data, indent=2, ensure_ascii=False))
    text = re.sub(r"(&quot;.*?&quot;)(?=:)", r'<span class="k">\1</span>', text)
    text = re.sub(r"(?<=: )(&quot;.*?&quot;)", r'<span class="s">\1</span>', text)
    text = re.sub(r"(?<=: )(-?\d+(?:\.\d+)?)", r'<span class="n">\1</span>', text)
    text = re.sub(r"(?<=: )(true|false)", r'<span class="b">\1</span>', text)
    text = re.sub(r"(?<=: )(null)", r'<span class="z">\1</span>', text)
    return f'<div class="cl-json">{text}</div>'


def results_cards(result: dict, card: dict, method: str | None) -> str:
    foot = f'<p class="cl-hint">Reviewed by {esc(method)}</p>' if method else ""
    return ('<div class="cl-grid">'
            f'<div class="cl-glass"><div class="cl-h">Claim result</div>{result_rows(result)}{json_block(result)}</div>'
            f'<div class="cl-glass"><div class="cl-h">AI reviewer</div>{score_rows(card)}{json_block(card)}{foot}</div>'
            '</div>')
