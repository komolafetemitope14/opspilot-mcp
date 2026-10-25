#!/bin/sh
# Requires the project's Python dependencies and kubectl. Run again to renew.
# Administrator credentials are used only by this provisioning process.
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 - "$script_dir/.." "$@" <<'PY'
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description="Provision the restricted OpsPilot identity.")
    parser.add_argument("--admin-context", required=True)
    args = parser.parse_args(sys.argv[2:])
    if not args.admin_context.strip():
        parser.error("--admin-context must be nonempty")
    root = Path(sys.argv[1]).resolve()
    # Fixed, ignored destination prevents accidentally overwriting an admin kubeconfig.
    directory = root / ".local/kubernetes"
    destination = directory / "opspilot-reader.kubeconfig"
    base = ["kubectl", "--context", args.admin_context]
    stage = "reading administrator cluster configuration"
    temporary = None
    try:
        def kubectl(*arguments):
            return subprocess.run(base + list(arguments), check=True, capture_output=True,
                                  text=True, timeout=60).stdout

        source = json.loads(kubectl("config", "view", "--minify", "--flatten", "--raw", "-o", "json"))
        cluster = source["clusters"][0]["cluster"]
        server = cluster["server"]
        ca = cluster["certificate-authority-data"]
        if not server.startswith("https://") or not ca or cluster.get("insecure-skip-tls-verify"):
            raise ValueError("TLS configuration required")
        stage = "preparing private credential directory"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if directory.is_symlink():
            raise ValueError("credential directory cannot be a symlink")
        os.chmod(directory, 0o700)
        stage = "applying OpsPilot RBAC manifest"
        kubectl("apply", "-f", str(root / "kubernetes/opspilot-reader-rbac.yaml"))
        stage = "requesting short-lived reader token"
        token = kubectl("-n", "opspilot-system", "create", "token", "opspilot-reader",
                        "--duration=1h").strip()
        if not token or any(c.isspace() for c in token):
            raise ValueError("invalid token response")
        restricted = {
            "apiVersion": "v1", "kind": "Config",
            "clusters": [{"name": "opspilot", "cluster": {
                "server": server, "certificate-authority-data": ca}}],
            "users": [{"name": "opspilot-reader", "user": {"token": token}}],
            "contexts": [{"name": "opspilot-reader", "context": {
                "cluster": "opspilot", "user": "opspilot-reader", "namespace": "default"}}],
            "current-context": "opspilot-reader",
        }
        stage = "writing restricted kubeconfig"
        with tempfile.NamedTemporaryFile(mode="w", dir=directory, prefix=".reader-",
                                         delete=False) as output:
            temporary = output.name
            os.fchmod(output.fileno(), 0o600)
            json.dump(restricted, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
        temporary = None
    except Exception:
        # kubectl errors and kubeconfig content can contain credentials.
        print(f"Kubernetes reader provisioning failed while {stage}.", file=sys.stderr)
        return 1
    finally:
        if temporary is not None:
            os.unlink(temporary)
    print("Restricted kubeconfig written to .local/kubernetes/opspilot-reader.kubeconfig")
    print("Export KUBECONFIG to that file's absolute path before launching OpsPilot.")
    print("Token lifetime requested: 1 hour; actual lifetime is controlled by the API server.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
PY
