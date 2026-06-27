"""Wait until the orchestrator HTTP server responds on ports 5000-5010."""

import sys
import time
import urllib.error
import urllib.request

PORTS = range(5000, 5011)
MAX_ATTEMPTS = 30
SLEEP_SECONDS = 0.75


def find_server_url() -> str | None:
    for _ in range(MAX_ATTEMPTS):
        for port in PORTS:
            url = f"http://localhost:{port}/api/inputs"
            try:
                with urllib.request.urlopen(url, timeout=2) as response:
                    if response.status == 200:
                        return f"http://localhost:{port}/description_page.html"
            except (urllib.error.URLError, TimeoutError, OSError):
                continue
        time.sleep(SLEEP_SECONDS)
    return None


def main() -> int:
    server_url = find_server_url()
    if server_url:
        print(server_url)
        return 0
    print("Orchestrator did not become ready on ports 5000-5010.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
