"""Claim a new private customer directory or remove an existing owned one."""
import argparse
import json
import os
import shutil
from pathlib import Path


def owned_directory(path, root, customer, remove=False):
    path = Path(path)
    if path.parent.resolve() != Path(root).resolve() or path.is_symlink() or path.name in ("", ".", ".."):
        raise ValueError("Invalid demo directory location")
    if not path.exists() and remove:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    marker = path / ".edu-ai-demo-owner"
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        if not marker.is_file() or marker.is_symlink() or json.loads(marker.read_text()) != customer:
            raise ValueError("Directory is unowned or belongs to another customer: " + str(path))
    else:
        with marker.open("x") as fp:
            json.dump(customer, fp)
        return True
    if remove:
        shutil.rmtree(path)
        return True
    return False


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["claim", "remove"])
    p.add_argument("path")
    p.add_argument("root")
    p.add_argument("customer")
    args = p.parse_args()
    print("changed" if owned_directory(args.path, args.root, args.customer, args.action == "remove") else "unchanged")
