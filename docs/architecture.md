# Architecture

- **Manager (`apps/manager`):** local aiohttp backend plus one static page. Runs a vanilla server on your PC, opens an SSH reverse tunnel to the VPS so friends join `VPS:25565`, and keeps a shared world on the VPS (`world-N.zip`; one host at a time through a `lock/` directory plus heartbeat).
- **Hub (`apps/hub`):** tiny VPS service. A friend sends an invite code and a public key; the hub adds the key to the two restricted accounts.
- **Releases:** conventional commits -> release-please PR -> merge -> tag, then `MinecraftManager.exe`, the friend zip, `mc_hub.py` and `SHA256SUMS.txt` are attached.

Developer notes and gotchas are in `CLAUDE.md`.
