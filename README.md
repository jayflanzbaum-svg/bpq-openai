# bpq-openai

An AI assistant app for BPQ32 packet radio nodes, with built-in QRZ callsign
lookups.

Users connect to your node, type `OPENAI`, and can ask anything — answers are
chunked and paged for RF with a `MORE` command. QRZ XML subscribers also get
`QRZ <CALL>` callsign cards and `BIO <CALL>` bio lookups over the air.

```
Welcome to OPENAI.
Ask anything (radio or non-radio) and press Enter.
Commands: QRZ <CALL>, QRZ+ <CALL>, BIO <CALL>, MORE, NEW, NODE, HELP.
```

## Features

- **Ask anything** — general-purpose AI chat with short-paragraph,
  plain-ASCII answers tuned for BBS/RF terminals
- **Conversation memory** — keeps the last few turns for follow-up questions;
  `NEW` clears context
- **Paged output** — long answers are sent in chunks; `MORE` continues
- **QRZ integration** (optional) — `QRZ <CALL>` short card, `QRZ+ <CALL>`
  extended record, `BIO <CALL>` bio text, all RF-trimmed
- Robust output sanitization: smart quotes, mojibake, and control characters
  are normalized to clean ASCII with CRLF line endings

## Requirements

- Python 3.10+
- `pip install openai httpx`
- An OpenAI API key
- Optionally a QRZ.com XML subscription for the QRZ/BIO commands
- A BPQ32 node with a Telnet port

## Install

1. Clone this repo somewhere on the node PC.
2. Set environment variables (System Properties > Environment Variables on
   Windows, or your shell profile):
   - `OPENAI_API_KEY` — required
   - `OPENAI_MODEL` — optional, defaults to `gpt-4.1-mini`
   - `QRZ_USER` / `QRZ_PASS` — optional, enables QRZ/BIO commands
   - `QRZ_AGENT` — optional agent string for QRZ requests
3. The app listens on `127.0.0.1:7373` (edit `HOST`/`PORT` at the top of
   `openai_bpq_app.py` to change).
4. Add the app to `BPQ32.cfg`:

   In your **Telnet port** `CONFIG` block, add the listen port to `CMDPORT`
   (space-separated list; note its zero-based position — that's the HOST
   number):
   ```
   CMDPORT=7373
   ```
   In the **APPLICATIONS** section (adjust the application number, HOST
   index, and your callsign/alias):
   ```
   APPLICATION 3,OPENAI,C 7 HOST 0 S,MYCALL-16,NODEAI,255
   ```
   `C 7` is your Telnet port number; `HOST 0` is the CMDPORT position; the
   `S` flag makes BPQ send the connecting user's callsign to the app.
5. Restart BPQ32, start the app (a startup batch file or a Windows Terminal
   tab works well), and type `OPENAI` at your node prompt.

## Notes

- Never hardcode your API key — the app reads it from the environment only.
- Answers are limited to your OpenAI account's rate limits and costs; the
  default model is inexpensive, but you may want to watch usage on a busy
  node.

## License

MIT — see [LICENSE](LICENSE).
