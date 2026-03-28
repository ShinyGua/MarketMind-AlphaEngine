---
name: mm:dashboard
description: Launch the MarketMind web report browser
user-invocable: true
---

Launch the MarketMind web dashboard — a single-process server that serves the full UI and API.

## Usage

```text
/mm:dashboard [--port PORT]
```

Default port: 7860. Browser opens automatically.

## Steps

1. Ensure `flask` is installed in `.venv`:

   ```bash
   .venv/bin/pip install -q flask
   ```

2. Start the dashboard server (ONE command, no separate processes):

   ```bash
   .venv/bin/python3 web/server.py --port 7860
   ```

3. Browser opens automatically at `http://localhost:7860`

   - If it doesn't open, navigate there manually.
   - Press `Ctrl+C` to stop.

## What You'll See

- **Home**: Grid of all analyzed tickers with BUY/HOLD/SELL badges
- **Ticker page**: Table of all report dates for that ticker
- **Report page**: Three view modes:
  - **Document** — full scrollable report with styled markdown
  - **Slides** — section-by-section presentation (keyboard ← → supported)
  - **PDF** — embedded PDF viewer (if PDF was exported)

## Notes

- Reports are read directly from `workspaces/` — no pre-generation needed
- Adding `--no-browser` skips the auto-open
- Use `--port PORT` to change the port (e.g. `--port 8080`)
