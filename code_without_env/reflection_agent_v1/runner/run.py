"""
run.py — ReflectionAgent Production Server Starter
====================================================
Starts server.py with gunicorn (Linux/GCP) or uvicorn (Windows).
Same pattern as ReflexAgent run.py and EngineerAgent run.py.

Port default 8082:
  ReflexAgent    → 8080
  EngineerAgent  → 8081
  ReflectionAgent→ 8082
  (allows all three to run locally simultaneously)

Usage:
  python run.py                  ← production
  PORT=8082 python run.py        ← explicit port
  WORKERS=2 python run.py        ← multiple workers

Why timeout=0?
  evaluate_and_publish calls GNN for post-action validation.
  Default gunicorn timeout (30s) may kill long-running requests.
"""

import os
import sys
import subprocess


def main():
    port    = os.environ.get("PORT", "8082")
    workers = os.environ.get("WORKERS", "1")

    print("=" * 55)
    print("  ReflectionAgent — Starting Server")
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