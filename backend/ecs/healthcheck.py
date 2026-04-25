"""
ECS Health Check

Simple health check script for ALB target group health checks.
Used as HEALTHCHECK in Dockerfile.
"""

import sys
import urllib.request


def main():
    try:
        req = urllib.request.Request("http://localhost:8080/ping", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status == 200:
                sys.exit(0)
            else:
                sys.exit(1)
    except Exception:
        sys.exit(1)


if __name__ == "__main__":
    main()
