# Security

Report problems privately through GitHub's "Report a vulnerability" (Security tab), not in a public issue.

How it is built to limit damage:
- Friends get SSH keys that can only open the Minecraft port (`mctunnel`) or use sftp inside the world folder (`mcsync`). No shell.
- Private keys never travel: each PC makes its own key and sends only the public half with a single-use, 24-hour invite code.
- The manager listens on 127.0.0.1 only, checks the Host header and requires a per-run token.
- The hub (port 8766) limits abuse so it stays cheap for a small VPS:
  - 60 requests per minute per IP; 5 wrong invite codes or owner tokens in 15 minutes block that IP for 15 minutes.
  - 100 wrong invite codes per hour from all IPs together pause joining for an hour (the owner can still manage friends).
  - At most 32 connections at once, 10-second socket timeout, 4 KB request bodies, at most 20 open invites.
  - systemd caps it at 64 MB RAM, 25% CPU and 64 tasks, with a read-only filesystem except its own state and the two key folders.
  - With ufw, the installer uses `ufw limit` on 8766 (new connections rate-limited per IP) if you let it change the firewall.
  - Invite codes have about 60 bits of randomness and the owner token 192 bits, so guessing is not practical anyway.

Known limits:
- The invite request goes to the hub over plain HTTP, so someone watching the network could race a friend for an unused invite.
- The game runs with `online-mode=false` on purpose (cracked launchers). Use the whitelist if strangers could find the address.
