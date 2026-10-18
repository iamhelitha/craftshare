# Contributing

- Layout: `apps/manager` (the Windows app), `apps/hub` (the VPS service), `install.sh` (VPS installer), `docs/`.
- Use conventional commit messages (`feat: ...`, `fix: ...`, `feat!: ...` for breaking changes). Versions and the changelog are generated from them.
- Test with throwaway data, never your real world or config:
  `MCMGR_DATA=/tmp/x MCMGR_CONFIG=/tmp/x/cfg.json MCMGR_NO_BROWSER=1 MCMGR_NO_LAUNCHER=1 python apps/manager/app.py`
- Hub: `python apps/hub/test_hub.py`.
- Never commit keys, `.env`, server addresses or worlds. `.gitignore` covers them and CI scans for secrets.
- Licensed GPL-3.0. Contributions are accepted under the same license.
