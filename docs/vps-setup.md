# Setting up your VPS

You need a Linux VPS where you can log in as root. This does not touch 3x-ui/Xray or other software.

```bash
bash <(curl -Ls https://raw.githubusercontent.com/OWNER/REPO/main/install.sh)
```

It installs `mc-hub` (about 20 MB of RAM), prints an **owner code**, and creates two locked-down accounts:
`mctunnel` (can only open port 25565) and `mcsync` (can only use sftp inside `/srv/mcworld`).
Open TCP **8766** (invites) and **25565** (the game) in your provider's firewall panel if it has one.

Then, in the app: **Setup > Friends & access > I own the VPS**, paste the owner code, and press **Invite a friend**.

Other uses of the same script:
- `... install.sh) v0.2.0` installs a specific release.
- `... install.sh) uninstall` removes the service but keeps your world, accounts and friends.
- Running it again updates the hub.

Manual route: download `mc_hub.py` from the latest release and run `sudo python3 mc_hub.py install`.
