"""
ECS Health Check

Container liveness probe (ECS container health check). Hits /live, which only
says the process is up; /ping additionally waits for the S3 Files mount and is
what the ALB target group uses.
"""

import sys
import urllib.request


def main():
    try:
        req = urllib.request.Request("http://localhost:8080/live", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status == 200:
                sys.exit(0)
            else:
                sys.exit(1)
    except Exception:
        sys.exit(1)


if __name__ == "__main__":
    main()
