"""
run.py — ReflexAgent Production Server Starter
================================================
Starts server.py with gunicorn (Linux/GCP) or uvicorn (Windows).
Mirrors monitoring_agent/run.py exactly.

Usage:
  python run.py                  ← start production server
  PORT=8080 python run.py        ← custom port
  WORKERS=2 python run.py        ← multiple workers

Why uvicorn.workers.UvicornWorker (not sync workers)?
  ReflexAgent tools are async (MCP calls, Spanner SDK, GNN calls).
  Standard gunicorn sync workers block on async code.
  UvicornWorker runs the full asyncio event loop per worker.

Why timeout=0?
  GNN + MCP + Spanner can take 30-60s.
  Default gunicorn timeout (30s) kills in-flight requests.
"""

import os
import sys
import subprocess


def main():
    port    = os.environ.get("PORT", "8080")
    workers = os.environ.get("WORKERS", "1")

    print("=" * 55)
    print("  ReflexAgent — Starting Server")
    print("=" * 55)
    print(f"  Port    : {port}")
    print(f"  Workers : {workers}")
    print(f"  App     : server:app")
    print("=" * 55)

    if os.name == "nt":
        # Windows — gunicorn not available, use uvicorn
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
