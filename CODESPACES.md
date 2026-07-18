# Running the Studio in GitHub Codespaces (live)

A private, Linux, browser-based way to run the dashboard remotely. The Codespace is
visible only to your GitHub login — there is no public URL.

## 1. Push these files
The `.devcontainer/` and `requirements-deploy.txt` must be on the branch you open the
Codespace from. Commit and push them to `main` (or your branch) first.

## 2. Add your API keys as Codespaces secrets
GitHub → **Settings → Codespaces → Secrets** (or the repo's Settings → Secrets and
variables → Codespaces). Add, scoped to this repo:

- `ANTHROPIC_API_KEY` — Vesper chat + storyboard drafting
- `FAL_KEY` — image generation / regeneration
- `ELEVENLABS_API_KEY` — narration/SFX (only if you exercise the audio stage)

They are injected as environment variables; `config.py` reads them directly. **Do not
commit a `.env`.**

## 3. Create the Codespace
On the repo page: **Code ▸ Codespaces ▸ Create codespace**. It builds the Python 3.11
container and runs `pip install -r requirements-deploy.txt` automatically.

## 4. Launch the studio
In the Codespace terminal:

```bash
flask --app wsgi run --host 0.0.0.0 --port 5000
```

A "Studio" port (5000) appears in the **Ports** tab — click it to open the UI in your
browser. Binding `0.0.0.0` lets Codespaces forward it; the forward stays private to you.

## 5. Keeping your work
- The Codespace filesystem **persists across stop/resume** (it's only lost if you delete
  the Codespace; GitHub auto-deletes idle ones after a retention window).
- `storyboard_manifest.json` is **git-tracked** — `git commit` + `git push` to save your
  storyboards durably to the repo.
- `references/` and `assets/` (uploads + generated images) are **gitignored**, so they
  live only in the Codespace volume. To keep those too, either commit them on a branch or
  copy them out.

## Notes
- This runs Flask's dev server — fine for a single user. It uses one process, which also
  keeps the dashboard's active-project state consistent.
- `requirements-deploy.txt` is the lean dashboard set. The full local pipeline (audio,
  depth, motion, timeline) needs the heavier stack and, on Linux, plain `onnxruntime`
  instead of the Windows-only `onnxruntime-directml` pinned in `requirements.txt`.
