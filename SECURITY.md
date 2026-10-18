# Security

Report problems privately through GitHub's "Report a vulnerability" (Security tab), not in a public issue.

## What is exposed to the internet (on the VPS)

| Port | What | Who can use it |
|---|---|---|
| 22 (your existing SSH) | `mctunnel` and `mcsync` accounts | Only keys added through invites. `mctunnel` may only open port 25565, `mcsync` may only use sftp inside `/srv/mcworld`. No shell, no other forwarding. `authorized_keys` are root-owned. |
| 25565 | The Minecraft server of whoever is hosting, through their tunnel | Players. Open only while someone hosts. |
| 8766 | `mc-hub` (HTTPS) | Anyone can connect; everything except `/info` needs an invite code, the owner token or a backup link. |

## How the hub protects itself and you

- **HTTPS with a pinned certificate.** The hub makes a self-signed certificate at install. Every owner code and
  invite carries the start of its SHA-256 fingerprint (80 bits), and the desktop app refuses to talk to a server
  whose certificate does not match, before sending any secret. Someone in between cannot read codes or tokens, or
  pretend to be the hub.
- **Secrets.** Invite codes: about 60 bits, single use, 24 hours, at most 20 open. Owner token: 192 bits. Backup
  links: 256 bits, 1 hour, 3 downloads. Only SHA-256 hashes are stored, compared in constant time.
- **Rate limits.**
  - 60 requests per minute per IP.
  - 5 wrong invite codes, owner tokens or backup links in 15 minutes block that IP for 15 minutes.
  - 100 wrong invite codes per hour from all IPs together pause joining for an hour (the owner can still manage friends).
  - With ufw, the installer uses `ufw limit` on 8766 (new connections rate-limited per IP) if you let it change the firewall.
- **Flood limits.** At most 32 connections at once (extra ones are closed), TLS handshake and requests time out
  after 10 seconds, 4 KB request bodies, bounded memory for tracking IPs, 2 backup downloads at a time.
- **Small surface.** The service uses only the Python standard library. Public `/info` reveals nothing but the
  protocol number; the version is only shown to the owner. Names and keys are validated with strict patterns
  before anything is written.
- **Sandbox.** systemd runs it with no Linux capabilities, a read-only system (writable: its own state and the two
  key folders), no new privileges, private /tmp and devices, a system-call filter, and caps of 64 MB RAM, 25% CPU
  and 64 tasks.
- **Updates.** `mc-hub update` downloads over HTTPS from GitHub releases, checks the release's SHA-256, the version
  inside the file and that it compiles, asks first, and rolls back if the new version does not start.
  Releases are built by GitHub Actions with build provenance attestations.
- **Uninstall.** Removes exactly what the hub created (it records the firewall rules and sshd line it added).

## Desktop app

- Listens on 127.0.0.1 only, checks the Host header and requires a per-run token.
- Private keys never travel: each PC makes its own key and sends only the public half with an invite code.

## Known limits

- The game runs with `online-mode=false` on purpose (cracked launchers). Use the whitelist if strangers could find the address.
- Anyone with your owner code can invite people. Keep it private; `mc-hub owner-code` replaces it.
- The checksum proves the file is what the release published; it cannot protect against a compromised GitHub account.
  Keep 2FA on the GitHub account that owns the repo.
