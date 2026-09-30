"""Serve the console on plain HTTP and HTTPS at the same time, from ONE process.

Two uvicorn processes would be two separate apps: each with its own in-memory run
registry, so a run started over HTTP would be invisible over HTTPS and two runs could
execute at once. Here both listeners share the same `app` object.

    python -m jarvis.console.serve --http-port 8090 --https-port 9443 \
        --ssl-certfile certs/console.crt --ssl-keyfile certs/console.key

Browsers only allow the microphone on HTTPS (or localhost), hence the second port.
"""
from __future__ import annotations

import argparse
import asyncio
import logging

import uvicorn

from jarvis.console.server import app


async def _serve(args: argparse.Namespace) -> None:
    servers = [uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.http_port, log_level="info"))]
    if args.https_port:
        servers.append(
            uvicorn.Server(
                uvicorn.Config(
                    app,
                    host=args.host,
                    port=args.https_port,
                    ssl_certfile=args.ssl_certfile,
                    ssl_keyfile=args.ssl_keyfile,
                    log_level="info",
                )
            )
        )
    await asyncio.gather(*(server.serve() for server in servers))


def main() -> None:
    parser = argparse.ArgumentParser(description="JARVIS console on HTTP and HTTPS")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--http-port", type=int, default=8090)
    parser.add_argument("--https-port", type=int, default=0, help="0 = HTTP only")
    parser.add_argument("--ssl-certfile")
    parser.add_argument("--ssl-keyfile")
    args = parser.parse_args()
    if args.https_port and not (args.ssl_certfile and args.ssl_keyfile):
        parser.error("--https-port needs --ssl-certfile and --ssl-keyfile")
    # JARVIS' own INFO lines (repo choice, voice attempts, Jira status) into the journal; uvicorn keeps its own format.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(_serve(args))


if __name__ == "__main__":
    main()
