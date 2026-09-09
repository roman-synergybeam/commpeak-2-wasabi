"""Entry point for running the API without uvicorn's CLI.

The systemd unit invokes uvicorn directly; this exists so ``c2w-api`` works
as a console script too.
"""

from __future__ import annotations


def main() -> int:
    import uvicorn

    uvicorn.run("c2w.api.app:app", host="127.0.0.1", port=8000, proxy_headers=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
