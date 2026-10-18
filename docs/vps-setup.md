# Setting up your VPS

You need a Linux VPS where you can log in as root. This does not touch 3x-ui/Xray or other software.

```bash
bash <(curl -Ls https://raw.githubusercontent.com/iamhelitha/craftshare/main/install.sh)
```

It installs `mc-hub` (about 20 MB of RAM), prints an **owner code**, and creates two locked-down accounts:
`mctunnel` (can only open port 25565) and `mcsync` (can only use sftp inside `/srv/mcworld`).

It **asks** whether to open TCP **25565** (the game) and **8766** (invites) in the server's firewall (ufw or
firewalld). Answer `y` to add the rules, or `n` to leave the firewall alone and do it yourself. To skip the
question: `MC_FIREWALL=yes` or `MC_FIREWALL=no` before the command. If your provider also has a firewall in its
web panel, open the same two ports there; a script on the server cannot change that one.

The hub is rate-limited against brute force and floods (see [SECURITY.md](../SECURITY.md)).

Then, in the app: **Setup > Friends & access > I own the VPS**, paste the owner code, and press **Invite a friend**.

Other uses of the same script:
- `... install.sh) v0.2.0` installs a specific release.
- `... install.sh) uninstall` removes the service but keeps your world, accounts and friends.
- Running it again updates the hub.

Manual route: download `mc_hub.py` from the latest release and run `sudo python3 mc_hub.py install`.
