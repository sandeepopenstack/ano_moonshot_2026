"""
engineer_run.py — EngineerAgent Production Server Starter
===========================================================
Starts server.py with gunicorn (Linux/GCP)
or uvicorn (Windows).

Same pattern as run.py for ReflexAgent.

Usage:
  python engineer_run.py                 ← production
  PORT=8081 python engineer_run.py       ← custom port
  WORKERS=2 python engineer_run.py       ← multiple workers

Why UvicornWorker?
  engineeragent_service.py uses async def endpoints.
  Standard gunicorn sync workers block on async code.

Why timeout=0?
  generate_healing_plan + BigQuery metadata fetch can take 30-60s.
  Default gunicorn timeout (30s) kills in-flight requests.

Port default 8081 (ReflexAgent uses 8080):
  Allows both services to run locally at the same time for testing.
"""

import os
import sys
import subprocess


def main():
    port    = os.environ.get("PORT", "8081")
    workers = os.environ.get("WORKERS", "1")

    print("=" * 55)
    print("  EngineerAgent — Starting Server")
    print("=" * 55)
    print(f"  Port    : {port}")
    print(f"  Workers : {workers}")
    print(f"  App     : server:app")
    print("=" * 55)

    if os.name == "nt":
        subprocess.run([
            sys.executable, "-m", "uvicorn",
            "server:app",
            "--host", "0.0.0.0",
            "--port", port,
            "--reload",
        ])
    else:
        # Linux / GCP Cloud Run
        subprocess.run([
            "gunicorn",
            f"--bind=0.0.0.0:{port}",
            f"--workers={workers}",
            "--worker-class=uvicorn.workers.UvicornWorker",
            "--timeout=0",
            "--keep-alive=65",
            "--log-level=info",
            "--access-logfile=-",
            "--error-logfile=-",
            "server:app",
        ])


if __name__ == "__main__":
    main()
