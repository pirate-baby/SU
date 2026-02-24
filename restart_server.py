#!/usr/bin/env python3
"""
Host-side restart server for SU self-iteration.

Listens on port 8932 and accepts POST /restart to rebuild and restart
the claude-executor container via docker compose.

This script runs on the HOST (not inside Docker) and is launched by startup.sh.
"""
import os
import subprocess
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler

PORT = 8932
# The SU repo directory — set by startup.sh or default to script's directory
REPO_DIR = os.environ.get("SU_REPO_DIR", os.path.dirname(os.path.abspath(__file__)))


class RestartHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == "/restart":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Restart initiated\n")
            self.wfile.flush()

            print(f"[restart_server] Rebuilding and restarting claude-executor in {REPO_DIR}...")
            # Run in a subprocess so the HTTP response is sent first.
            # Only restart the claude-executor service, not nginx.
            subprocess.Popen(
                [
                    "docker", "compose",
                    "-f", "docker-compose.yml",
                    "-f", "docker-compose.local.yml",
                    "up", "--build", "-d", "claude-executor",
                ],
                cwd=REPO_DIR,
                stdout=sys.stdout,
                stderr=sys.stderr,
            )
        elif self.path == "/update":
            # Pull latest main, push to main, then rebuild the container.
            # Runs on the host so git credentials and ssh keys are available.
            print(f"[restart_server] Running update in {REPO_DIR}...")
            try:
                # git pull --rebase origin main
                pull = subprocess.run(
                    ["git", "pull", "--rebase", "origin", "main"],
                    cwd=REPO_DIR,
                    capture_output=True,
                    text=True,
                )
                print(f"[restart_server] git pull: {pull.stdout.strip()} {pull.stderr.strip()}")
                if pull.returncode != 0:
                    raise RuntimeError(f"git pull failed: {pull.stderr.strip()}")

                # git push origin main
                push = subprocess.run(
                    ["git", "push", "origin", "main"],
                    cwd=REPO_DIR,
                    capture_output=True,
                    text=True,
                )
                print(f"[restart_server] git push: {push.stdout.strip()} {push.stderr.strip()}")
                if push.returncode != 0:
                    raise RuntimeError(f"git push failed: {push.stderr.strip()}")

                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"Update initiated — rebuilding container\n")
                self.wfile.flush()
            except Exception as exc:
                self.send_response(500)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(f"Update failed: {exc}\n".encode())
                self.wfile.flush()
                return

            # Rebuild and restart the container after a successful git update.
            subprocess.Popen(
                [
                    "docker", "compose",
                    "-f", "docker-compose.yml",
                    "-f", "docker-compose.local.yml",
                    "up", "--build", "-d", "claude-executor",
                ],
                cwd=REPO_DIR,
                stdout=sys.stdout,
                stderr=sys.stderr,
            )
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok\n")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        print(f"[restart_server] {format % args}")


def main():
    server = HTTPServer(("0.0.0.0", PORT), RestartHandler)
    print(f"[restart_server] Listening on port {PORT} (repo dir: {REPO_DIR})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[restart_server] Shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
