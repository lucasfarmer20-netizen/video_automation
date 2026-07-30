"""Launch the studio bound to 0.0.0.0 using uvicorn programmatically.

Uvicorn runs our Next.js static asset server and FastAPI backend router.
"""

import os
import uvicorn

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    uvicorn.run("backend.main:app", host="0.0.0.0", port=port, reload=False)
