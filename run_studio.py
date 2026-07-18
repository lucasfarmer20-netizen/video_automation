"""Launch the studio bound to 0.0.0.0 so remote devices (via Tailscale) can reach it.

Run it in your OWN terminal so it keeps running after any tool session ends:
    .venv/Scripts/python.exe run_studio.py

Then, from any device signed into your tailnet, open:
    http://<this-machine's-tailscale-ip>:5000     (find it with: tailscale ip -4)
"""

import os

from src.dashboard import run

if __name__ == "__main__":
    run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
