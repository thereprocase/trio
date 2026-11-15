"""`setup.sh hub-service` stages, validates, then swaps the release in.

Runs the real script against a throwaway TRIO_HUB_ROOT with stub `id`,
`systemctl`, `sleep` and `curl`, a venv that holds synthetic mcp/uvicorn/PIL
packages, and a loopback HTTP server for the health probe. Nothing outside the
temp directory is touched and nothing is installed (the stub pip does nothing).

Covers: a failed import or compile check leaves the live tree and web/ intact;
two deploys in one day keep both previous releases; a wrong pinned version
fails with the package named; the health check runs with no working curl and a
failed check makes the deploy exit non-zero; backup retention is bounded.

Usage: python tests/test-hub-service-deploy.py
"""
import http.server
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETUP = ROOT / "setup.sh"
failures = []


def check(name, cond):
    print(("PASS" if cond else "FAIL") + f": {name}")
    if not cond:
        failures.append(name)


text = SETUP.read_text(encoding="utf-8")
loop = re.search(r"for f in ((?:[\w./\\$]+\.(?:py|js)\s*\\?\s*)+);\s*do", text)
MODULES = re.findall(r"(\w+\.(?:py|js))", loop.group(1))
check("hub-service module list found", len(MODULES) > 3)

tmp = Path(tempfile.mkdtemp(prefix="trio-hub-deploy-"))
repo = tmp / "repo"
root = tmp / "root"
bin_dir = tmp / "bin"
scratch_tmp = tmp / "tmp"
for d in (repo / "server" / "web" / "css", root, bin_dir, scratch_tmp):
    d.mkdir(parents=True)
shutil.copy(SETUP, repo / "setup.sh")

for name, body in {
    "id": 'if [ "$1" = "-u" ]; then echo 0; else echo uid=0; fi\n',
    "systemctl": 'echo "$@" >> "$TRIO_TEST_LOG/systemctl.log"\n',
    "sleep": "exit 0\n",
    # A curl that cannot work: the deploy must not depend on it.
    "curl": 'echo called >> "$TRIO_TEST_LOG/curl.log"\nexit 127\n',
}.items():
    (bin_dir / name).write_text("#!/bin/sh\n" + body)
    (bin_dir / name).chmod(0o755)
log_dir = tmp / "log"
log_dir.mkdir()

hub = root / "opt" / "quartet-hub"
venv = hub / "venv"
subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
site = next((venv / "lib").glob("python*/site-packages"))


def fake_dist(name, version, files):
    for rel, body in files.items():
        p = site / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    di = site / f"{name}-{version}.dist-info"
    di.mkdir(exist_ok=True)
    (di / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")


def set_dist(name, old, new):
    (site / f"{name}-{old}.dist-info").rename(site / f"{name}-{new}.dist-info")
    (site / f"{name}-{new}.dist-info" / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {new}\n")


fake_dist("mcp", "1.9.0", {
    "mcp/__init__.py": "", "mcp/server/__init__.py": "",
    "mcp/server/fastmcp.py": "class FastMCP: pass\n"})
fake_dist("uvicorn", "0.52.1", {"uvicorn/__init__.py": ""})
fake_dist("Pillow", "12.3.0", {"PIL/__init__.py": ""})
# Stub pip: records the call, installs nothing, so a wrong pin stays wrong.
(site / "pip").mkdir()
(site / "pip" / "__init__.py").write_text("")
(site / "pip" / "__main__.py").write_text(
    "import os\nopen(os.path.join(os.environ['TRIO_TEST_LOG'], 'pip.log'), 'a')"
    ".write('pip called\\n')\n")


def write_release(version, broken=None):
    srv = repo / "server"
    for m in MODULES:
        body = f"# {version}\n"
        if m.endswith(".py"):
            body += "VERSION = %r\n" % version
        (srv / m).write_text(body)
    (srv / "quartet_server.py").write_text(f"import nth_web\nVERSION = {version!r}\n")
    (srv / "web" / "index.html").write_text(f"<p>{version}</p>\n")
    (srv / "web" / "css" / "a.css").write_text(f"/* {version} */\n")
    if broken == "import":
        (srv / "nth_web.py").write_text("import module_that_is_absent\n")
    elif broken == "syntax":
        (srv / "nth_web.py").write_text("def broken(:\n")


def live(name):
    return (hub / name).read_text() if (hub / name).exists() else None


def web_marker():
    p = hub / "web" / "index.html"
    return p.read_text().strip() if p.exists() else None


def backups():
    d = hub / "backups"
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


def restarts():
    p = log_dir / "systemctl.log"
    return p.read_text().count("restart") if p.exists() else 0


class Health(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


def serve(tls=None):
    srv = http.server.HTTPServer(("127.0.0.1", 0), Health)
    if tls:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(*tls)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


plain = serve()
base = f"http://127.0.0.1:{plain.server_port}/"


def deploy(**extra):
    env = dict(os.environ)
    env.update({
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "TRIO_HUB_ROOT": str(root), "TRIO_TEST_LOG": str(log_dir),
        "TMPDIR": str(scratch_tmp), "HUB_HEALTH_URL": base + "healthz",
        "HUB_WEB_URL": base, "HUB_HEALTH_TRIES": "1",
    })
    env.update(extra)
    r = subprocess.run(["bash", str(repo / "setup.sh"), "hub-service"],
                       env=env, capture_output=True, text=True, timeout=120)
    return r.returncode, r.stdout + r.stderr


try:
    # 1. First deploy.
    write_release("v1")
    rc, out = deploy()
    check("first deploy succeeds", rc == 0)
    check("first deploy installs files and web/", "v1" in (live("nth_web.py") or "")
          and web_marker() == "<p>v1</p>")
    check("unit files land under the fake root",
          (root / "etc/systemd/system/quartet-hub.service").exists())
    check("health check ran without curl",
          "/healthz OK" in out and not (log_dir / "curl.log").exists())

    # 2. Two further deploys on the same day keep both earlier releases.
    write_release("v2")
    check("second deploy succeeds", deploy()[0] == 0)
    write_release("v3")
    check("third deploy succeeds", deploy()[0] == 0)
    names = backups()
    check("two same-day deploys keep two distinct backups", len(names) == 2)
    seen = {}
    for n in names:
        b = hub / "backups" / n
        seen[(b / "nth_web.py").read_text().split("VERSION = ")[1].strip()] = (
            (b / "web" / "index.html").read_text().strip())
    check("the first release survives with its web/ tree",
          seen == {"'v1'": "<p>v1</p>", "'v2'": "<p>v2</p>"})
    check("live tree is the newest release",
          "v3" in live("nth_web.py") and web_marker() == "<p>v3</p>")

    # 3. A failed import or compile check changes nothing.
    for kind in ("import", "syntax"):
        write_release("v4", broken=kind)
        before = (live("nth_web.py"), web_marker(), backups())
        r0 = restarts()
        rc, out = deploy()
        check(f"{kind} failure exits non-zero", rc != 0)
        check(f"{kind} failure leaves live files, web/ and backups alone",
              before == (live("nth_web.py"), web_marker(), backups()))
        check(f"{kind} failure leaves no staging directory",
              not [p for p in hub.iterdir() if p.name.startswith(".stage")])
        check(f"{kind} failure does not restart services", restarts() == r0)
    write_release("v4")

    # 4. A wrong pinned version fails and names the package.
    set_dist("uvicorn", "0.52.1", "0.51.0")
    before = (live("nth_web.py"), web_marker())
    rc, out = deploy()
    check("wrong uvicorn version exits non-zero", rc != 0)
    check("the message names the package and the pin",
          "uvicorn 0.51.0" in out and "0.52.1" in out)
    check("wrong pin leaves the live tree alone",
          before == (live("nth_web.py"), web_marker()))
    set_dist("uvicorn", "0.51.0", "0.52.1")
    set_dist("mcp", "1.9.0", "2.0.0")
    rc, out = deploy()
    check("mcp 2.x fails", rc != 0 and "mcp 2.0.0" in out)
    set_dist("mcp", "2.0.0", "1.9.0")

    # 5. Unhealthy service: deploy is reported and exits non-zero.
    rc, out = deploy(HUB_HEALTH_URL="http://127.0.0.1:9/healthz")
    check("unreachable health check exits 1 and says UNVERIFIED",
          rc == 1 and "UNVERIFIED" in out)

    # 6. Loopback https with a certificate that names another host.
    if shutil.which("openssl"):
        crt, key = tmp / "c.pem", tmp / "k.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", str(key), "-out", str(crt), "-days", "1",
                        "-subj", "/CN=host.example.invalid"],
                       check=True, capture_output=True)
        tls = serve((str(crt), str(key)))
        rc, out = deploy(HUB_WEB_URL=f"https://127.0.0.1:{tls.server_port}/")
        check("https health check accepts a certificate for another name",
              rc == 0 and "landing page OK" in out)
    else:
        print("SKIP: openssl not available for the https probe")

    # 7. Retention is bounded.
    for v in ("v5", "v6", "v7", "v8"):
        write_release(v)
        deploy(HUB_KEEP_BACKUPS="2")
    check("HUB_KEEP_BACKUPS bounds the retained backups", len(backups()) == 2)
finally:
    plain.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)

print()
if failures:
    print(f"FAILED ({len(failures)}): " + "; ".join(failures))
    sys.exit(1)
print("OK — hub-service validates before replacing, keeps backups, and verifies health")
