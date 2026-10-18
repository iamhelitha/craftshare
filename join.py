"""Join helper: play on the shared server through a local proxy (e.g. v2rayN). Hosts nothing.

Minecraft ignores Windows proxy settings, so on a throttled connection it cannot use v2rayN by itself.
This listens on localhost:25566 and passes the game connection through the proxy to the VPS.
Run:  python join.py   (or join.bat), then join  localhost:25566  in Minecraft.
"""
import asyncio
import sys

import core

LISTEN_PORT = 25566


async def main() -> None:
    cfg = core.load_config()
    host, port = cfg["ssh_host"], int(cfg["ssh_remote_port"] or 25565)
    if not host:
        print("No server address is set. Open the manager's Setup page and fill in the server address first.")
        return
    proxy = core.parse_proxy(cfg["proxy"]) or core.parse_proxy(core.windows_proxy())
    if not proxy:
        print("No proxy found. Start v2rayN (with 'system proxy' on), then run this again.")
        return
    try:
        core.socks_connect(host, port, proxy, timeout=10).close()
    except OSError as e:
        print(f"Can't reach the server through the proxy {proxy[0]}:{proxy[1]}: {e}")
        print("Check that v2rayN is connected, and that someone is hosting the server.")
        return

    async def client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            r2, w2 = await core.socks_open(host, port, proxy)
        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as e:
            print(f"Connection failed: {e}")
            writer.close()
            return

        async def pump(src, dst):
            try:
                while (data := await src.read(65536)):
                    dst.write(data)
                    await dst.drain()
            except (OSError, ConnectionError):
                pass
            finally:
                dst.close()

        print("Minecraft connected.")
        await asyncio.gather(pump(reader, w2), pump(r2, writer))

    try:
        server = await asyncio.start_server(client, "127.0.0.1", LISTEN_PORT)
    except OSError:
        print(f"Port {LISTEN_PORT} is already in use - is the join helper (or the manager) already open?")
        return
    print("=" * 60)
    print(f"  Ready.  In Minecraft join:   localhost:{LISTEN_PORT}")
    print("=" * 60)
    print(f"Going through your proxy {proxy[0]}:{proxy[1]} to {host}:{port}.")
    print("Keep this window open while you play; close it when you are done.")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
