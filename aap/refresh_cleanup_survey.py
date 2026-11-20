#!/usr/bin/env python3
"""Rebuild the AAP cleanup survey from the same cluster the playbook operated on."""
import argparse
import base64
import hashlib
import json
import os
import re
import ssl
import subprocess
import sys
import urllib.request
from urllib.parse import urlsplit

OWNER = "app.kubernetes.io/managed-by=edu-ai-demo-builder"
EMPTY = "No managed demos available"


def oc_json(*args):
    data = subprocess.check_output(["oc", *args, "-o", "json", "--request-timeout=30s"], timeout=45)
    return json.loads(data) if data.strip() else {}


def customer_name(metadata):
    name = metadata.get("annotations", {}).get("edu-ai-demo/customer-name", "")
    if not name or not name.isprintable() or name != name.strip() or name == EMPTY:
        raise ValueError("Invalid customer-name annotation on " + metadata["name"])
    return name


def cleanup_survey(vm_namespace=None):
    customers = set()
    for item in oc_json("get", "namespaces", "-l", OWNER)["items"]:
        name = customer_name(item["metadata"])
        if name in customers:
            raise ValueError("Multiple managed namespaces claim the same customer")
        customers.add(name)
    if vm_namespace:
        pool = oc_json("get", "namespace", vm_namespace, "--ignore-not-found")
        if pool:
            if pool["metadata"].get("labels", {}).get("edu-ai-demo/purpose") != "rhel-vms":
                raise ValueError("Configured VM pool is missing its infrastructure ownership label")
            # The bootstrap ConfigMap is the first resource created and last removed.
            # This includes interrupted deployments even if their app namespace is gone.
            markers = oc_json("-n", vm_namespace, "get", "configmaps", "-l", OWNER + ",edu-ai-demo/vm")
            for item in markers["items"]:
                meta = item["metadata"]
                vm = meta.get("labels", {}).get("edu-ai-demo/vm", "")
                if meta["name"] != vm + "-bootstrap":
                    continue
                name = customer_name(meta)
                slug = re.sub("[^a-z0-9]+", "-", name.lower()).strip("-")[:28].strip("-")
                expected_vm = "rhel-" + slug + "-" + hashlib.sha1(name.encode()).hexdigest()[:6]
                if vm != expected_vm:
                    raise ValueError("VM bootstrap identity mismatch on " + meta["name"])
                customers.add(name)
    return {
        "name": "EDU_AI_DEMO_CLEANUP",
        "description": "Remove one school's demo, source corpus and dedicated storage.",
        "spec": [{
            "question_name": "School / Organization to remove",
            "question_description": "Select the deployed demo whose resources you want to delete.",
            "required": True, "type": "multiplechoice", "variable": "customer_name",
            "choices": "\n".join(sorted(customers, key=str.casefold) or [EMPTY]), "default": "",
        }],
    }


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def refresh(template_id, vm_namespace=None):
    host = os.environ["CONTROLLER_HOST"].rstrip("/")
    if urlsplit(host).scheme != "https" or not urlsplit(host).netloc:
        raise ValueError("A trusted HTTPS automation gateway URL is required")
    endpoint = f"{host}/api/controller/v2/job_templates/{template_id}/survey_spec/"
    token = os.getenv("CONTROLLER_OAUTH_TOKEN")
    if token:
        authorization = "Bearer " + token
    else:
        credentials = os.environ["CONTROLLER_USERNAME"] + ":" + os.environ["CONTROLLER_PASSWORD"]
        authorization = "Basic " + base64.b64encode(credentials.encode()).decode()
    context = ssl.create_default_context()
    ca_file = os.getenv("EDU_AI_GATEWAY_CA") or os.getenv("K8S_AUTH_SSL_CA_CERT")
    if ca_file:
        context.load_verify_locations(cafile=ca_file)
    client = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context), NoRedirect())

    def request(survey=None):
        body = json.dumps(survey).encode() if survey is not None else None
        req = urllib.request.Request(endpoint, data=body, headers={
            "Authorization": authorization, "Content-Type": "application/json",
        })
        with client.open(req, timeout=30) as response:
            return json.load(response) if survey is None else None

    for attempt in range(5):
        desired = cleanup_survey(vm_namespace)
        if request() != desired:
            request(desired)
        # A deploy and a cleanup can finish concurrently. Confirm both sides again.
        if request() == desired and cleanup_survey(vm_namespace) == desired:
            print("Cleanup survey matches the managed customer namespaces.")
            return
    raise RuntimeError("Managed deployments changed repeatedly during survey refresh. Rerun AAP setup.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--template-id", type=int)
    parser.add_argument("--vm-namespace")
    args = parser.parse_args()
    try:
        if args.template_id is None:
            print(json.dumps(cleanup_survey(args.vm_namespace)))
        else:
            refresh(args.template_id, args.vm_namespace)
    except (KeyError, OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        sys.exit(f"Cleanup survey refresh failed: {exc}")
