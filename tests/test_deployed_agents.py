import os, sys, json, subprocess, time

import requests

SERVICES = {
    "reflex": {
        "url": "https://ran-reflex-test-761300295499.us-central1.run.app",
        "app": "reflex_agent",
    },
    "engineer": {
        "url": "https://ran-engineer-test-761300295499.us-central1.run.app",
        "app": "engineer_agent",
    },
    "reflection": {
        "url": "https://ran-reflection-test-761300295499.us-central1.run.app",
        "app": "reflection_agent",
    },
}

_TOKEN = None


def get_token():
    global _TOKEN
    if not _TOKEN:
        _TOKEN = subprocess.run(
            [
                "gcloud", "auth", "print-identity-token",
                "--account=techm-dev@poc-z-in2300756.iam.gserviceaccount.com",
            ],
            capture_output=True, text=True, timeout=15,
        ).stdout.strip()
    return _TOKEN


def verdict(label, ok):
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}")
    return ok


def main():
    results = []

    print("=" * 65)
    print("  DEPLOYED AGENTS \u2014 LIVE HEALTH CHECK")
    print(f"  {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    print("=" * 65)

    h = {
        "Authorization": f"Bearer {get_token()}",
        "Content-Type": "application/json",
    }

    for name, svc in SERVICES.items():
        agent = svc["app"]
        base = svc["url"]
        print(f"\n  >>> {name.upper()} ({agent})")
        print(f"  URL: {base}")

        r = requests.get(f"{base}/health", headers=h, timeout=15)
        results.append(verdict(f"health \u2192 {r.status_code}", r.status_code == 200))

        r = requests.get(f"{base}/list-apps", headers=h, timeout=15)
        apps_ok = r.status_code == 200 and agent in r.text
        results.append(verdict(f"list-apps \u2192 {r.status_code} (has {agent})", apps_ok))

        r = requests.get(f"{base}/a2a/{agent}/.well-known/agent-card.json", headers=h, timeout=15)
        card_ok = False
        if r.status_code == 200:
            try:
                d = r.json()
                card_ok = bool(d.get("name")) and bool(d.get("skills")) and bool(d.get("capabilities"))
            except Exception:
                pass
        results.append(verdict(f"a2a card \u2192 {r.status_code} (name+skills+capabilities)", card_ok))

        r = requests.get(f"{base}/version", headers=h, timeout=15)
        results.append(verdict(f"version \u2192 {r.status_code}", r.status_code == 200))

    print("\n" + "=" * 65)
    print("  SUMMARY")
    print("=" * 65)
    total = len(results)
    passed = sum(1 for r in results if r)
    failed = total - passed
    print(f"  Total: {total} | Passed: {passed} | Failed: {failed}")

    for name, svc in SERVICES.items():
        print(f"\n  {name.upper()} URL: {svc['url']}")
        print(f"    A2A Card: {svc['url']}/a2a/{svc['app']}/.well-known/agent-card.json")

    print(f"\n  {'ALL CHECKS PASSED' if not failed else 'SOME CHECKS FAILED'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
