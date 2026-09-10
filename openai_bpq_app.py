import os
import re
import socket
import textwrap
import threading
import time
import html as htmlmod
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import httpx
from openai import OpenAI

HOST = "127.0.0.1"
PORT = 7373

# Increased from 200 to 1000
CHUNK_LEN = 1000

WELCOME = (
    "Welcome to OPENAI.\r\n"
    "Ask anything (radio or non-radio) and press Enter.\r\n"
    "Commands: QRZ <CALL>, QRZ+ <CALL>, BIO <CALL>, MORE, NEW, NODE, HELP.\r\n\r\n"
)

# Grouped one purpose per line, HELP | QUIT last, inside 42 columns - see
# BPQ-Node/NODE-APP-STYLE.md. This app had no menu at all, so a caller who
# did not already know QRZ or BIO existed had no way to find them.
MENU_LINE = ("<question> | QRZ <call> | QRZ+ <call>\r\n"
             "BIO <call> | MORE | NEW\r\n"
             "HELP | QUIT\r\n")

# Two columns inside 42: a 13-wide command field, then the gloss.
HELP = (
    "Commands:\r\n"
    "  <question>   ask the AI, any topic\r\n"
    "  QRZ <call>   short callsign card\r\n"
    "  QRZ+ <call>  extended callsign info\r\n"
    "  BIO <call>   bio text, RF-trimmed\r\n"
    "  MORE         continue a long answer\r\n"
    "  NEW          new question, clear context\r\n"
    "  HELP         show commands\r\n"
    "  Q / BYE / NODE  exit\r\n"
)

# House vocabulary, identical in all four apps. Bare Q was missing here, and
# it is the one everybody types.
EXIT_WORDS = ("Q", "QUIT", "EXIT", "BYE", "NODE")
HELP_WORDS = ("HELP", "?")

PROMPT_FMT = "[{n} left: MORE, NEW, or QUIT]\r\n"

DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

# General-purpose assistant (not ham-only) + ASCII-only to avoid mojibake
SYSTEM_STYLE = (
    "You are a general-purpose assistant running inside a BBS chat app. "
    "Answer any topic the user asks about (not just ham radio). "
    "Do NOT assume the question is ham-related unless the user says so. "
    "Use plain ASCII only (no smart quotes or special symbols). "
    "Be concise and use short paragraphs. "
    "If the answer is long, structure it clearly."
)

# ---------------------------
# Output sanitization (BBS-safe)
# ---------------------------

def sanitize_for_bbs(s: str) -> str:
    """
    Make output safe for mixed BBS/telnet terminals:
      - remove/convert control chars (VT/FF) that show as weird blanks
      - normalize quotes/dashes to ASCII
      - force printable ASCII (prevents mojibake like â€œ â€)
      - output CRLF line endings
    """
    if not s:
        return s

    # Normalize newlines to \n first
    s = s.replace("\r\n", "\n").replace("\r", "\n")

    # Convert common control chars to newlines
    s = s.replace("\x0b", "\n")  # VT
    s = s.replace("\x0c", "\n")  # FF

    # Convert smart punctuation to ASCII
    s = (s.replace("“", '"').replace("”", '"')
           .replace("‘", "'").replace("’", "'")
           .replace("—", "-").replace("–", "-")
           .replace("…", "..."))

    # Remove other control chars except tab/newline
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", s)

    # Force ASCII for maximum compatibility
    s = s.encode("ascii", "ignore").decode("ascii", "ignore")

    # Back to CRLF
    s = s.replace("\n", "\r\n")
    return s


# ---------------------------
# QRZ XML integration
# ---------------------------
QRZ_BASE = "https://xmldata.qrz.com/xml/current/"

# QRZ callsign pattern (no hyphen)
_CALL_RE = re.compile(r"^[A-Z0-9/]{3,12}$", re.IGNORECASE)

# BPQ/user ID pattern (allows -SSID like N0CALL-8)
_BPQ_CALL_RE = re.compile(r"^[A-Z0-9/]{3,12}(-\d{1,2})?$", re.IGNORECASE)

_qrz_lock = threading.Lock()
_qrz_session_key: Optional[str] = None
_qrz_last_login_ts: float = 0.0


def _qrz_get_env() -> Tuple[str, str, str]:
    user = os.getenv("QRZ_USER", "").strip()
    pw = os.getenv("QRZ_PASS", "").strip()
    agent = os.getenv("QRZ_AGENT", "BPQ-OPENAI-1.0").strip()
    return user, pw, agent


def _xml_find_first(root: ET.Element, local_name: str) -> Optional[ET.Element]:
    for el in root.iter():
        if el.tag.split("}")[-1] == local_name:
            return el
    return None


def _qrz_login(client: httpx.Client) -> Tuple[Optional[str], Optional[str]]:
    user, pw, agent = _qrz_get_env()
    if not user or not pw:
        return None, "QRZ not configured (set QRZ_USER and QRZ_PASS)."

    params = {"username": user, "password": pw, "agent": agent}
    r = client.get(QRZ_BASE, params=params, timeout=15.0)
    r.raise_for_status()

    try:
        root = ET.fromstring(r.text)
    except Exception as e:
        return None, f"QRZ login parse error: {e}"

    sess = _xml_find_first(root, "Session")
    key_val = None
    err_val = None

    if sess is not None:
        k = _xml_find_first(sess, "Key")
        if k is not None and (k.text or "").strip():
            key_val = (k.text or "").strip()
        e = _xml_find_first(sess, "Error")
        if e is not None and (e.text or "").strip():
            err_val = (e.text or "").strip()

    if err_val:
        return None, f"QRZ login error: {err_val}"
    if not key_val:
        return None, "QRZ login failed: no session key returned."
    return key_val, None


def _qrz_get_key(client: httpx.Client) -> Tuple[Optional[str], Optional[str]]:
    global _qrz_session_key, _qrz_last_login_ts
    with _qrz_lock:
        key = _qrz_session_key

    if key:
        return key, None

    new_key, err = _qrz_login(client)
    if err:
        return None, err

    with _qrz_lock:
        _qrz_session_key = new_key
        _qrz_last_login_ts = time.time()

    return new_key, None


def _qrz_call_lookup_xml(client: httpx.Client, key: str, callsign: str) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    params = {"s": key, "callsign": callsign.upper().strip()}
    r = client.get(QRZ_BASE, params=params, timeout=15.0)
    r.raise_for_status()

    try:
        root = ET.fromstring(r.text)
    except Exception as e:
        return None, f"QRZ parse error: {e}"

    sess = _xml_find_first(root, "Session")
    if sess is not None:
        err = _xml_find_first(sess, "Error")
        if err is not None and (err.text or "").strip():
            return None, (err.text or "").strip()

    cs = _xml_find_first(root, "Callsign")
    if cs is None:
        return None, "QRZ: No callsign data returned."

    data: Dict[str, str] = {}
    for el in list(cs):
        k = el.tag.split("}")[-1]
        v = (el.text or "").strip()
        if v:
            data[k] = v

    if not data:
        return None, "QRZ: Empty callsign record."
    return data, None


def _qrz_lookup(callsign: str) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    cs = callsign.strip().upper()
    if not _CALL_RE.match(cs):
        return None, "Usage: QRZ <CALL> (example: QRZ W1AW)"

    user, pw, agent = _qrz_get_env()
    if not user or not pw:
        return None, "QRZ not configured. Set QRZ_USER and QRZ_PASS env vars."

    with httpx.Client(headers={"User-Agent": agent}) as client:
        key, err = _qrz_get_key(client)
        if err:
            return None, err
        assert key is not None

        data, err2 = _qrz_call_lookup_xml(client, key, cs)

        if err2 and ("session" in err2.lower() or "invalid" in err2.lower()):
            new_key, err3 = _qrz_login(client)
            if err3:
                return None, err3
            with _qrz_lock:
                global _qrz_session_key, _qrz_last_login_ts
                _qrz_session_key = new_key
                _qrz_last_login_ts = time.time()
            data, err2 = _qrz_call_lookup_xml(client, new_key, cs)

        if err2:
            return None, f"QRZ error: {err2}"
        return data, None


def _format_qrz_short(data: Dict[str, str]) -> str:
    call = data.get("call", "").upper()
    fname = data.get("fname", "")
    name = data.get("name", "")
    fullname = " ".join([x for x in [fname, name] if x]).strip()

    addr2 = data.get("addr2", "")
    state = data.get("state", "")
    country = data.get("country", "")
    grid = data.get("grid", "") or data.get("gridsquare", "")

    line1 = call if call else "CALL"
    if fullname:
        line1 += f"  {fullname}"

    line2_parts = [p for p in [addr2, state, country] if p]
    line2 = "  ".join(line2_parts).strip()

    out = [line1]
    if line2:
        out.append(line2)
    if grid:
        out.append(f"Grid: {grid}")

    return "\r\n".join(out) + "\r\n"


def _format_qrz_plus(data: Dict[str, str]) -> str:
    call = data.get("call", "").upper() or "CALL"
    name_fmt = data.get("name_fmt", "").strip()
    dxcc = data.get("dxcc", "")
    grid = data.get("grid", "") or data.get("gridsquare", "")
    county = data.get("county", "")
    born = data.get("born", "")
    email = data.get("email", "")
    aliases = data.get("aliases", "")
    cls = data.get("class", "")
    efdate = data.get("efdate", "")
    expdate = data.get("expdate", "")
    addr2 = data.get("addr2", "")
    state = data.get("state", "")
    country = data.get("country", "")

    lines = [f"{call}  {name_fmt}".strip()]

    loc = "  ".join([p for p in [addr2, state, country] if p]).strip()
    if loc:
        lines.append(loc)

    if grid:
        lines.append(f"Grid: {grid}")
    if cls or efdate or expdate:
        parts = []
        if cls:
            parts.append(f"Class: {cls}")
        if efdate:
            parts.append(f"Eff: {efdate}")
        if expdate:
            parts.append(f"Exp: {expdate}")
        lines.append("  ".join(parts))

    if county:
        lines.append(f"County: {county}")
    if dxcc:
        lines.append(f"DXCC: {dxcc}")
    if born:
        lines.append(f"Born: {born}")
    if aliases:
        lines.append(f"Aliases: {aliases}")
    if email:
        lines.append(f"Email: {email}")

    if data.get("bio", ""):
        bd = data.get("biodate", "")
        if bd:
            lines.append(f"Bio: yes (updated {bd})  Use: BIO {call}")
        else:
            lines.append(f"Bio: yes  Use: BIO {call}")

    return "\r\n".join([ln for ln in lines if ln.strip()]) + "\r\n"


def _fix_mojibake(text: str) -> str:
    if not text:
        return text

    text = text.replace("\u00A0", " ")

    replacements = {
        "Â ": " ",
        " Â": " ",
        "â€™": "’",
        "â€œ": "“",
        "â€�": "”",
        "â€”": "—",
        "â€“": "–",
        "â€¦": "…",
        "â€˜": "‘",
        "â€¢": "•",
    }
    for bad, good in replacements.items():
        if bad in text:
            text = text.replace(bad, good)

    markers = ("â€", "Ã", "Â", "â€™", "â€œ", "â€�", "â€”", "â€“")
    if any(m in text for m in markers):
        try:
            repaired = text.encode("latin1", errors="ignore").decode("utf-8", errors="ignore")
            repaired = repaired.replace("\u00A0", " ")

            def score(s: str) -> int:
                return sum(s.count(m) for m in markers)

            if score(repaired) < score(text):
                text = repaired
        except Exception:
            pass

    text = text.replace("Â ", " ").replace(" Â", " ")
    return text


def _html_to_text(s: str) -> str:
    s = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", s)
    s = re.sub(r"(?i)<\s*br\s*/?\s*>", "\n", s)
    s = re.sub(r"(?i)</\s*p\s*>", "\n", s)
    s = re.sub(r"(?is)<.*?>", " ", s)
    s = htmlmod.unescape(s)
    s = _fix_mojibake(s)
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"\n\s*\n\s*\n+", "\n\n", s)
    return s.strip()


def qrz_fetch_bio_text(callsign: str, max_chars: int = 1200) -> Tuple[Optional[str], Optional[str]]:
    cs = callsign.strip().upper()
    if not _CALL_RE.match(cs):
        return None, "Usage: BIO <CALL> (example: BIO W1AW)"

    user, pw, agent = _qrz_get_env()
    if not user or not pw:
        return None, "QRZ not configured. Set QRZ_USER and QRZ_PASS env vars."

    with httpx.Client(headers={"User-Agent": agent}) as client:
        key, err = _qrz_get_key(client)
        if err:
            return None, err
        assert key is not None

        r = client.get(QRZ_BASE, params={"s": key, "html": cs}, timeout=20.0)
        r.raise_for_status()
        html = r.text or ""
        if not html.strip():
            return None, "QRZ BIO: empty response."

        text = _html_to_text(html)
        if not text:
            return None, "QRZ BIO: no bio text found."

        if len(text) > max_chars:
            text = text[:max_chars].rstrip() + "...\r\n[Bio trimmed]\r\n"

        text = text.replace("\n", "\r\n")
        if not text.endswith("\r\n"):
            text += "\r\n"

        return sanitize_for_bbs(text), None


# ---------------------------
# OPENAI chat integration
# ---------------------------

@dataclass
class SessionState:
    pending: str = ""
    last_question: str = ""
    history: List[str] = field(default_factory=list)

    # NEW: store initial ID/callsign so we don't treat it like a question
    user_id: str = ""
    saw_first_user_line: bool = False


def safe_recv_line(conn: socket.socket) -> Optional[str]:
    """
    Read until newline. Return line (stripped) or None on disconnect.
    IMPORTANT: swallow Windows reset/abort noise so threads exit quietly.
    """
    data = bytearray()
    try:
        while True:
            b = conn.recv(1)
            if not b:
                return None
            if b == b"\n":
                break
            data += b
            if len(data) > 4096:
                break
    except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
        return None

    line = data.decode("utf-8", errors="ignore").rstrip("\r")
    return line.strip()


OUT_WIDTH = 42


def wrap_out(text: str, width: int) -> str:
    """Fold over-long lines at word boundaries for a narrow terminal.

    A phone client is about 43 columns. This app matters more than most: the
    model returns prose in whatever line lengths it likes, so without this
    every answer folded mid-word. See BPQ-Node/NODE-APP-STYLE.md.
    """
    if width <= 0 or not text:
        return text
    out = []
    for raw in text.replace("\r\n", "\n").splitlines(keepends=True):
        end = "\n" if raw.endswith("\n") else ""
        body = raw[:-1] if end else raw
        if len(body) <= width:
            out.append(body + end)
            continue
        lead = body[:len(body) - len(body.lstrip(" "))]
        # No added hanging indent here, unlike the apps with tables: this one
        # emits paragraphs of model prose, and a two-space hang on every
        # wrapped line makes a paragraph read as a bulleted list.
        folded = textwrap.wrap(body.strip(), width=width,
                               initial_indent=lead,
                               subsequent_indent=lead,
                               break_long_words=True, break_on_hyphens=False)
        out.append("\n".join(folded or [body]) + end)
    return "".join(out)


def send(conn: socket.socket, text: str) -> None:
    # Fold, then sanitize: sanitize_for_bbs restores CRLF and strips anything
    # non-ASCII, and it is idempotent for text already put through it.
    try:
        conn.sendall(sanitize_for_bbs(wrap_out(text, OUT_WIDTH))
                     .encode("utf-8", errors="ignore"))
    except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
        pass


def ask_openai(client: OpenAI, state: SessionState, user_text: str) -> str:
    """
    Use proper message formatting to reduce topic inertia.
    """
    messages = [{"role": "system", "content": SYSTEM_STYLE}]

    # Keep last 4 turns (8 lines) from your "User: / Assistant:" storage
    tail = state.history[-8:] if state.history else []
    for line in tail:
        if line.startswith("User: "):
            messages.append({"role": "user", "content": line[len("User: "):]})
        elif line.startswith("Assistant: "):
            messages.append({"role": "assistant", "content": line[len("Assistant: "):]})

    messages.append({"role": "user", "content": user_text})

    resp = client.responses.create(
        model=DEFAULT_MODEL,
        input=messages,
    )

    text = ""
    if hasattr(resp, "output_text") and resp.output_text:
        text = resp.output_text
    elif hasattr(resp, "output") and resp.output:
        parts = []
        for item in resp.output:
            if getattr(item, "type", None) == "message":
                for c in getattr(item, "content", []) or []:
                    if getattr(c, "type", None) in ("output_text", "text"):
                        parts.append(getattr(c, "text", "") or getattr(c, "value", "") or "")
        text = "".join(parts).strip()

    text = text.strip() or "(No response text returned.)"
    state.history.append(f"User: {user_text}")
    state.history.append(f"Assistant: {text}")
    return text


def _queue_and_send(conn: socket.socket, state: SessionState, text: str) -> None:
    text = sanitize_for_bbs(text)

    if len(text) > CHUNK_LEN:
        first = text[:CHUNK_LEN]
        rest = text[CHUNK_LEN:]
        state.pending = rest
        send(conn, first + ("\r\n" if not first.endswith("\r\n") else ""))
        send(conn, PROMPT_FMT.format(n=len(state.pending)))
    else:
        state.pending = ""
        send(conn, text + ("" if text.endswith("\r\n") else "\r\n"))


def _maybe_capture_initial_id(state: SessionState, cmdline: str) -> bool:
    """
    Some BBS/telnet clients send an ID/callsign immediately on connect (eg "N0CALL-8").
    If the FIRST non-empty line looks like a callsign (optional -SSID), swallow it.
    Returns True if we consumed it.
    """
    if state.saw_first_user_line:
        return False

    # Mark that we have now seen the first non-empty line (even if we don't consume it)
    state.saw_first_user_line = True

    # If it looks like a callsign / ID and has no spaces, treat as ID not a question
    if " " not in cmdline and _BPQ_CALL_RE.match(cmdline.strip()):
        state.user_id = cmdline.strip().upper()
        return True

    return False


def handle_client(conn: socket.socket, addr):
    state = SessionState()
    send(conn, WELCOME)

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    client = OpenAI(api_key=api_key) if api_key else None
    if not client:
        send(conn, "ERROR: OPENAI_API_KEY environment variable is not set.\r\n")
        send(conn, "Set it in Windows, then restart this app.\r\n\r\n")
        send(conn, "Type NODE to exit.\r\n")

    while True:
        line = safe_recv_line(conn)
        if line is None:
            break
        if not line:
            continue

        cmdline = line.strip()
        up = cmdline.upper()
        # A blank line under the command the operator just typed, so the
        # answer reads as a separate block. See NODE-APP-STYLE.md.
        send(conn, "\r\n")

        # NEW: swallow an initial ID/callsign line (like N0CALL-8) so it doesn't get sent to OpenAI
        if _maybe_capture_initial_id(state, cmdline):
            send(conn, f"Hi {state.user_id}. Ask me anything.\r\n\r\n"
                       + MENU_LINE)
            continue

        if up in EXIT_WORDS:
            send(conn, "73!\r\n")
            break

        if up in HELP_WORDS:
            send(conn, HELP + "\r\n" + MENU_LINE)
            continue

        if up == "NEW":
            state.pending = ""
            state.last_question = ""
            state.history.clear()
            send(conn, "OK, cleared. New question:\r\n")
            continue

        if up == "MORE":
            if not state.pending:
                send(conn, "Nothing more to show. Ask a question, "
                           "or HELP.\r\n")
                continue
            next_chunk = state.pending[:CHUNK_LEN]
            state.pending = state.pending[CHUNK_LEN:]
            send(conn, next_chunk + "\r\n")
            if state.pending:
                send(conn, PROMPT_FMT.format(n=len(state.pending)))
            continue

        if up.startswith("QRZ+ "):
            cs = cmdline.split(None, 1)[1].strip()
            data, err = _qrz_lookup(cs)
            if err:
                _queue_and_send(conn, state, err)
            else:
                _queue_and_send(conn, state, _format_qrz_plus(data))
            continue

        if up.startswith("QRZ ") or up.startswith("CALL "):
            parts = cmdline.split(None, 1)
            cs = parts[1].strip() if len(parts) > 1 else ""
            if not cs:
                _queue_and_send(conn, state, "Usage: QRZ <CALL> (example: QRZ W1AW)")
                continue
            data, err = _qrz_lookup(cs)
            if err:
                _queue_and_send(conn, state, err)
            else:
                _queue_and_send(conn, state, _format_qrz_short(data))
            continue

        if up.startswith("BIO "):
            cs = cmdline.split(None, 1)[1].strip()
            text, err = qrz_fetch_bio_text(cs)
            if err:
                _queue_and_send(conn, state, err)
            else:
                _queue_and_send(conn, state, text)
            continue

        if client is None:
            send(conn, "OPENAI not configured. Set OPENAI_API_KEY, restart app.\r\n")
            continue

        try:
            answer = ask_openai(client, state, cmdline)
            _queue_and_send(conn, state, answer)
        except Exception as e:
            _queue_and_send(conn, state, f"ERROR calling OpenAI: {e}")

    try:
        conn.close()
    except Exception:
        pass


def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, PORT))
    srv.listen(20)
    print(f"OPENAI BPQ app listening on {HOST}:{PORT}")

    while True:
        conn, addr = srv.accept()
        t = threading.Thread(target=handle_client, args=(conn, addr), daemon=True)
        t.start()


if __name__ == "__main__":
    main()