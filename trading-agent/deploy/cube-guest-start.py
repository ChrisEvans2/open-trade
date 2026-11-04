"""Minimal Cube template entrypoint: root envd plus an unprivileged health endpoint.

No Jupyter, code interpreter, or guest credential is started. Runtime secures envd
before uploading task input or allowing any model command.
"""

import os
import pwd
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer

user = pwd.getpwnam("user")
os.makedirs("/workspace", exist_ok=True)
fd = os.open("/var/log/envd.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
with os.fdopen(fd, "ab", buffering=0) as log:
    subprocess.Popen(["/usr/bin/envd", "-port", "49983"], stdout=log, stderr=log)
os.setgroups([])
os.setgid(user.pw_gid)
os.setuid(user.pw_uid)


class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == "/health" else 404)
        self.end_headers()

    def log_message(self, *args):
        pass


HTTPServer(("0.0.0.0", 49999), Health).serve_forever()
