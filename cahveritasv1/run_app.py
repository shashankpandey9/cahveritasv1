"""CAH VERITAS — Databricks App entry point.

Databricks Apps assign the listening port in DATABRICKS_APP_PORT; locally it
falls back to PORT, then 8000. The FastAPI app serves the prebuilt UI
(frontend/dist) and the API from the same origin.
"""
import os

import uvicorn

if __name__ == "__main__":
    port = int(os.getenv("DATABRICKS_APP_PORT") or os.getenv("PORT") or 8000)
    uvicorn.run("backend.app.main:app", host="0.0.0.0", port=port,
                proxy_headers=True, forwarded_allow_ips="*")
