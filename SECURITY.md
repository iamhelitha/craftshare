# Security

Report problems privately through GitHub's "Report a vulnerability" (Security tab), not in a public issue.

How it is built to limit damage:
- Friends get SSH keys that can only open the Minecraft port (`mctunnel`) or use sftp inside the world folder (`mcsync`). No shell.
- Private keys never travel: each PC makes its own key and sends only the public half with a single-use, 24-hour invite code.
- The manager listens on 127.0.0.1 only, checks the Host header and requires a per-run token.

Known limits:
- The invite request goes to the hub over plain HTTP, so someone watching the network could race a friend for an unused invite.
- The game runs with `online-mode=false` on purpose (cracked launchers). Use the whitelist if strangers could find the address.
