from __future__ import annotations

import argparse
import asyncio
import sys

from .config import load_config
from .config import save_config
from .auth.settings import load_or_create_pepper


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="remote-codex-agent")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="initialize local state")
    init.add_argument("--config")
    serve = commands.add_parser("serve", help="start the local WebSocket agent")
    serve.add_argument("--config")
    pair = commands.add_parser("pair", help="issue a pairing code")
    pair.add_argument("--name", default="Mobile device")
    pair.add_argument("--config")
    approve = commands.add_parser("pair-approve", help="approve a pairing request")
    approve.add_argument("pairing_id")
    approve.add_argument("--config")
    devices = commands.add_parser("devices", help="list devices")
    devices.add_argument("--config")
    revoke = commands.add_parser("revoke", help="revoke a device")
    revoke.add_argument("device_id")
    revoke.add_argument("--config")
    doctor = commands.add_parser("doctor", help="inspect local environment")
    doctor.add_argument("--config")
    return parser


async def _open_database(config):
    from .storage.database import Database
    from .config import default_database_path

    database = Database(config.storage.database_path or default_database_path())
    await database.connect()
    return database


async def _run_command(args) -> int:
    config = load_config(args.config)
    config.config_path = args.config
    if args.command == "init":
        database = await _open_database(config)
        await database.close()
        print(f"initialized: {config.storage.database_path or 'default database'}")
        return 0
    if args.command == "serve":
        from .main import Application

        app = Application(config)
        await app.start()
        print(f"agent listening on ws://{config.server.host}:{config.server.port}")
        if config.server.admin_enabled:
            print(f"admin console: {app.admin.url}")
        try:
            await asyncio.Event().wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await app.stop()
        return 0
    if args.command == "pair":
        from .auth.devices import DeviceService
        from .auth.pairing import PairingService

        database = await _open_database(config)
        devices = DeviceService(database, await load_or_create_pepper(database))
        pairing = PairingService(database, devices)
        pairing_id, code = await pairing.issue(args.name)
        print(f"pairing id: {pairing_id}")
        print(f"pairing code: {code}")
        print("approve with: remote-codex-agent pair-approve " + pairing_id)
        await database.close()
        return 0
    if args.command == "pair-approve":
        from .auth.devices import DeviceService
        from .auth.pairing import PairingService

        database = await _open_database(config)
        devices = DeviceService(database, await load_or_create_pepper(database))
        pairing = PairingService(database, devices)
        approved = await pairing.approve(args.pairing_id)
        await database.close()
        print("approved" if approved else "not approved")
        return 0 if approved else 1
    if args.command == "devices":
        database = await _open_database(config)
        rows = await database.fetch_all("SELECT id, name, created_at, last_seen_at, revoked_at FROM devices")
        await database.close()
        for row in rows:
            print(f"{row['id']}\t{row['name']}\tlast_seen={row['last_seen_at']}\trevoked={bool(row['revoked_at'])}")
        return 0
    if args.command == "revoke":
        from .auth.devices import DeviceService

        database = await _open_database(config)
        devices = DeviceService(database, await load_or_create_pepper(database))
        revoked = await devices.revoke(args.device_id)
        await database.close()
        print("revoked" if revoked else "not found")
        return 0 if revoked else 1
    if args.command == "doctor":
        from .codex.cli_gateway import CodexCliGateway

        try:
            diagnostics = await CodexCliGateway().diagnostics()
        except RuntimeError as exc:
            diagnostics = {"available": False, "error": str(exc)}
        print(f"python: {sys.version.split()[0]}")
        print(f"codex cli: {diagnostics}")
        print(f"allowed roots: {config.projects.allowed_roots}")
        return 0
    return 1


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return asyncio.run(_run_command(args))
    except KeyboardInterrupt:
        return 130
