"""Hub self-test: python apps/hub/test_hub.py  (no VPS needed; temp folder, local port, needs openssl)."""
import hashlib
import http.client
import json
import os
import ssl
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PORT = 18766
KEY = "ssh-ed25519 " + "A" * 68
ADMIN = "test-secret-d0-not-use-in-production"      # throwaway owner token for the local test hub


def main() -> None:
    root = tempfile.mkdtemp()
    os.environ.update(MC_HUB_ROOT=root, MC_HUB_PORT=str(PORT))
    sys.path.insert(0, str(HERE))
    sys.path.insert(0, str(HERE.parent / "manager"))
    import mc_hub
    mc_hub.make_cert()
    fp = mc_hub.fingerprint()
    ctx = ssl.create_default_context(cafile=str(mc_hub.CERT))       # trust exactly the hub's certificate
    ctx.check_hostname = False

    def call(method, path, body=None, token=""):
        conn = http.client.HTTPSConnection("127.0.0.1", PORT, context=ctx, timeout=10)
        headers = {"Authorization": "Bearer " + token} if token else {}
        conn.request(method, path, json.dumps(body).encode() if body is not None else None, headers)
        r = conn.getresponse()
        data = r.read()
        conn.close()
        try:
            return r.status, json.loads(data)
        except ValueError:
            return r.status, data

    st = mc_hub.load()
    st["admin"] = mc_hub.sha(ADMIN)
    st["public_ip"] = "203.0.113.5"
    mc_hub.save(st)
    for user, path in mc_hub.AUTH.items():          # pretend an old shared key already exists
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(mc_hub.OPTIONS[user] + " ssh-ed25519 AAAAOLD legacy\n")
    proc = subprocess.Popen([sys.executable, str(HERE / "mc_hub.py"), "serve"])
    try:
        for _ in range(50):
            try:
                call("GET", "/info")
                break
            except OSError:
                time.sleep(0.1)
        assert call("GET", "/info")[1] == {"mc_hub": mc_hub.HUB_VERSION}, "public /info reveals nothing else"
        assert call("GET", "/admin/friends")[0] == 403, "admin needs a token"
        code = call("POST", "/admin/invite", {}, ADMIN)[1]["code"]
        assert call("POST", "/enroll", {"code": code, "name": "bob", "pubkey": 'ssh-ed25519 AAAA" x'})[0] == 400

        # the desktop app's own client: pinned certificate
        import core
        good = core.split_hub(f"127.0.0.1:{PORT}/{code}/{fp[:20]}")
        assert good == ("127.0.0.1", PORT, code, fp[:20])
        r = core.hub_call("127.0.0.1", PORT, "POST", "/enroll", {"code": code, "name": "bob", "pubkey": KEY}, fp=fp[:20])
        assert r["ok"] and r["ssh_user"] == "mctunnel"
        try:
            core.hub_call("127.0.0.1", PORT, "GET", "/admin/friends", token=ADMIN, fp="0" * 20)
            raise AssertionError("wrong fingerprint accepted")
        except RuntimeError as e:
            assert "identity" in str(e)

        assert call("POST", "/enroll", {"code": code, "name": "bob2", "pubkey": KEY})[0] == 403, "invite is single use"
        friends = call("GET", "/admin/friends", token=ADMIN)[1]
        assert [f["name"] for f in friends["friends"]] == ["bob"] and friends["legacy"] == 2
        # the owner's own PC: add-key needs the owner token, is idempotent, and refuses a name clash
        assert call("POST", "/admin/add-key", {"name": "me", "pubkey": KEY})[0] == 403
        assert call("POST", "/admin/add-key", {"name": "me", "pubkey": KEY}, ADMIN)[0] == 200
        assert call("POST", "/admin/add-key", {"name": "me", "pubkey": KEY}, ADMIN)[0] == 200
        assert call("POST", "/admin/add-key", {"name": "me", "pubkey": "ssh-ed25519 " + "B" * 68}, ADMIN)[0] == 409
        assert call("POST", "/admin/add-key", {"name": "x y", "pubkey": KEY}, ADMIN)[0] == 400
        assert call("POST", "/admin/remove", {"name": "me"}, ADMIN)[0] == 200
        assert call("POST", "/admin/remove", {"name": "bob"}, ADMIN)[0] == 200
        assert "AAAAOLD" in mc_hub.AUTH["mcsync"].read_text(), "old keys must survive"
        assert "mc-hub:bob" not in mc_hub.AUTH["mctunnel"].read_text()

        # world backup download link (uninstall): secret token, limited downloads
        mc_hub.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        (mc_hub.EXPORT_DIR / "backup.zip").write_bytes(b"PK-test-data")
        token = "t" * 43
        with mc_hub.locked():
            st = mc_hub.load()
            st["export"] = {"hash": mc_hub.sha(token), "file": "backup.zip", "exp": time.time() + 60, "left": 1}
            mc_hub.save(st)
        assert call("GET", f"/export/{token}") == (200, b"PK-test-data")
        assert call("GET", f"/export/{token}")[0] == 404, "download limit"
        assert call("GET", "/export/" + "x" * 43)[0] == 404, "wrong link"

        # the mc-hub command (plain or rich output) runs against the same state
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        out = subprocess.run([sys.executable, str(HERE / "mc_hub.py"), "invite"], capture_output=True, text=True,
                             env=env, encoding="utf-8")
        assert out.returncode == 0 and f"/{fp[:20]}" in out.stdout, out.stdout + out.stderr
        out = subprocess.run([sys.executable, str(HERE / "mc_hub.py"), "status"], capture_output=True, text=True,
                             env=env, encoding="utf-8")
        assert out.returncode == 0 and "CraftShare hub" in out.stdout, out.stdout + out.stderr

        # brute force: wrong owner tokens get this IP blocked, then everything answers 429
        for i in range(mc_hub.Limiter.FAIL_LIMIT):
            if call("GET", "/admin/friends", token=f"guess{i}")[0] == 429:
                break
        status, body = call("GET", "/info")
        assert status == 429 and "Try again" in body["error"], (status, body)
        assert call("GET", "/admin/friends", token=ADMIN)[0] == 429, "even the right token waits while blocked"

        # plain HTTP is not served
        conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
        try:
            conn.request("GET", "/info")
            conn.getresponse()
            raise AssertionError("plain HTTP answered")
        except (OSError, http.client.HTTPException):
            pass
        print("hub tests passed")
    finally:
        proc.terminate()


if __name__ == "__main__":
    main()
