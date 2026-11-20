"""Inspect and manage only Podman resources marked for the selected customer."""
import argparse
import hashlib
import json
import subprocess
import sys


def podman(*args, check=True):
    p = subprocess.run(["podman", *args], capture_output=True, text=True)
    if check and p.returncode:
        raise RuntimeError(p.stderr.strip() or p.stdout.strip())
    return p


def owned(kind, name, owner):
    exists = podman(kind, "exists", name, check=False)
    if exists.returncode == 1:
        return False
    if exists.returncode != 0:
        raise RuntimeError(exists.stderr.strip() or "Cannot inspect Podman " + kind)
    resource = json.loads(podman(kind, "inspect", name).stdout)[0]
    labels = ((resource.get("Config") or {}).get("Labels") or resource.get("Labels") or {})
    if labels.get("edu-ai-demo.owner") != owner:
        raise ValueError("Refusing to modify an unowned Podman " + kind)
    return True


def main():
    p = argparse.ArgumentParser()
    for item in ("action", "customer", "container", "image"):
        p.add_argument(item)
    p.add_argument("--dir")
    p.add_argument("--port")
    p.add_argument("--provider")
    p.add_argument("--key-secret")
    a = p.parse_args()
    owner = hashlib.sha256(a.customer.encode()).hexdigest()
    has_container = owned("container", a.container, owner)
    has_image = owned("image", a.image, owner)
    if a.action == "check":
        return
    if a.action == "remove":
        if has_container:
            podman("rm", "-f", a.container)
        if has_image:
            podman("rmi", a.image)
        if owned("container", a.container, owner) or owned("image", a.image, owner):
            raise RuntimeError("Podman cleanup did not finish")
    elif a.action == "deploy":
        secret_options = []
        if a.key_secret:
            podman("secret", "inspect", a.key_secret)
            secret_options = ["--secret", a.key_secret + ",type=env,target=DEMO_LLM_API_KEY"]
        old_id = json.loads(podman("image", "inspect", a.image).stdout)[0]["Id"] if has_image else None
        podman("build", "--label", "edu-ai-demo.owner=" + owner, "-t", a.image, a.dir)
        if has_container:
            podman("rm", "-f", a.container)
        podman("run", "-d", "--name", a.container, "--label", "edu-ai-demo.owner=" + owner,
               "-p", str(int(a.port)) + ":8080", "-e", "DEMO_AI_PROVIDER=" + a.provider,
               "-e", "DEMO_LLM_ENDPOINT", "-e", "DEMO_LLM_MODEL", *secret_options, a.image)
        if old_id and old_id != json.loads(podman("image", "inspect", a.image).stdout)[0]["Id"] and owned("image", old_id, owner):
            podman("rmi", old_id)
    else:
        raise ValueError("Unsupported Podman action")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, RuntimeError) as exc:
        sys.exit(str(exc))
