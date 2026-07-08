# Codebase Rules

These rules are binding for all code in this repository.

## Runtime

- **Python 3.11+** is required.
- **ffmpeg is a required system-level install** — it is installed on the host OS, **not** via pip. Do not add it to `requirements.txt`.

## Architecture

- **Single orchestrator:** `pipeline.py` at the repo root is the only entrypoint. It routes to modular code in `src/`.
- **Modular `src/`:** each concern lives in its own module (`audio.py`, `assets.py`, `dashboard.py`, `timeline.py`, ...). **No monolith files** — keep modules focused and small.
- `pipeline.py` orchestrates; it does not implement domain logic itself.

## Secrets & configuration

- **All secrets are read via `os.environ.get`.** Never hardcode API keys, tokens, or absolute paths anywhere in the codebase.
- Required keys are documented in `.env.example`. A real `.env` is never committed (it is gitignored).

## Human-approval gate (hard requirement)

`pipeline.py` MUST enforce a hard human-approval gate:

1. The pipeline runs up to and including **draft-image generation**.
2. It then **pauses** and **launches the Flask dashboard** (`src/dashboard.py`) for human review.
3. It **refuses to call any paid video API** until an **approved `storyboard_manifest.json`** has been written.

The paid-video stage is unreachable until an approved `storyboard_manifest.json` exists. This gate must never be bypassed or made optional.
