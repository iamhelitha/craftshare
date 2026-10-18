"""Hub self-test: python apps/hub/test_hub.py  (no VPS needed; uses a temp folder and a local port)."""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PORT = 18766
KEY = "ssh-ed25519 " + "A" * 68


def call(method, path, body=None, token=""):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def main() -> None:
    root = tempfile.mkdtemp()
    os.environ.update(MC_HUB_ROOT=root, MC_HUB_PORT=str(PORT))
    sys.path.insert(0, str(HERE))
    import mc_hub
    st = mc_hub.load()
    st["admin"] = mc_hub.sha("secret")
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
        assert call("GET", "/info")[1]["mc_hub"] == mc_hub.HUB_VERSION
        assert call("GET", "/admin/friends")[0] == 403, "admin needs a token"
        code = call("POST", "/admin/invite", {}, "secret")[1]["code"]
        assert call("POST", "/enroll", {"code": "AAAA-AAAA-AAAA", "name": "bob", "pubkey": KEY})[0] == 403
        assert call("POST", "/enroll", {"code": code, "name": "bob", "pubkey": 'ssh-ed25519 AAAA" x'})[0] == 400
        assert call("POST", "/enroll", {"code": code, "name": "bob", "pubkey": KEY})[0] == 200
        assert call("POST", "/enroll", {"code": code, "name": "bob2", "pubkey": KEY})[0] == 403, "invite is single use"
        friends = call("GET", "/admin/friends", token="secret")[1]
        assert [f["name"] for f in friends["friends"]] == ["bob"] and friends["legacy"] == 2
        assert call("POST", "/admin/remove", {"name": "bob"}, "secret")[0] == 200
        assert "AAAAOLD" in mc_hub.AUTH["mcsync"].read_text(), "old keys must survive"
        assert "mc-hub:bob" not in mc_hub.AUTH["mctunnel"].read_text()

        # oversized / broken bodies are refused without being read
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}/enroll", method="POST", data=b"x" * 10,
                                     headers={"Content-Length": "999999"})
        try:
            urllib.request.urlopen(req)
            raise AssertionError("huge body accepted")
        except urllib.error.HTTPError as e:
            assert e.code == 400
        except OSError:
            pass                                     # server closed the connection: also fine

        # brute force: wrong owner tokens get this IP blocked, then everything answers 429
        for i in range(mc_hub.Limiter.FAIL_LIMIT):
            if call("GET", "/admin/friends", token=f"guess{i}")[0] == 429:
                break
        status, body = call("GET", "/info")
        assert status == 429 and "Try again" in body["error"], (status, body)
        assert call("GET", "/admin/friends", token="secret")[0] == 429, "even the right token waits while blocked"
        print("hub tests passed")
    finally:
        proc.terminate()


if __name__ == "__main__":
    main()
