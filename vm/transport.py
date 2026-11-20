"""Stage a digest-pinned vendor OCI archive for one customer VM inside its namespace."""

import json
import os
import re
import subprocess
import time

SKOPEO_IMAGE = "registry.access.redhat.com/ubi9/skopeo@sha256:f47495a8b30700edc9070a83c84203a153c9595fc4e903d796b6f31b2db74c70"
HTTP_IMAGE = "registry.access.redhat.com/ubi9/python-311@sha256:7b6cb58d3ff034df7b300800bd89a469d9bd2f739d43250d76b9c9e805307ab5"
REGISTRY = "image-registry.openshift-image-registry.svc:5000"
GUEST_TAG = "localhost/edu-rhel-inference:3.5.0"
VM_LABEL = "edu-ai-demo/vm"
COMPONENT = "edu-ai-demo/component"
VENDOR = "edu-ai-demo/vendor-image"

RESOURCES = {
    "ImageStream": ("image.openshift.io/v1", "imagestreams"),
    "ServiceAccount": ("v1", "serviceaccounts"),
    "NetworkPolicy": ("networking.k8s.io/v1", "networkpolicies"),
    "Service": ("v1", "services"),
    "Pod": ("v1", "pods"),
}

COPY = """set -eu
umask 077
export HOME=/tmp TMPDIR=/bundle/tmp
mkdir -p "$TMPDIR"
trap 'rm -f /tmp/vendor-auth' EXIT
skopeo login --cert-dir /trust --authfile /tmp/vendor-auth --username guest-image-puller \
  --password-stdin "$REGISTRY" < /identity/token
skopeo copy --retry-times 3 --src-cert-dir /trust --authfile /tmp/vendor-auth \
  "docker://$SOURCE" "oci-archive:/bundle/image.tar:$GUEST_TAG"
cd /bundle
sha256sum image.tar > image.tar.sha256
chmod 0444 image.tar image.tar.sha256
"""
HTTP = """from http.server import SimpleHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
class Handler(SimpleHTTPRequestHandler):
    def send_head(self):
        if self.path not in ('/image.tar','/image.tar.sha256'):
            self.send_error(404)
            return None
        return super().send_head()
ThreadingHTTPServer(('0.0.0.0',8080),Handler).serve_forever()
"""


def oc(args, body=None, *, check=True):
    r = subprocess.run(["oc", *args], input=json.dumps(body) if body is not None else None,
                       capture_output=True, text=True, timeout=100)
    if check and r.returncode:
        raise RuntimeError(r.stderr.strip()[:600] or "OpenShift operation failed")
    return r


def get(ns, kind, name):
    r = oc(["-n", ns, "get", RESOURCES[kind][1], name, "--ignore-not-found", "-o", "json"])
    return json.loads(r.stdout) if r.stdout.strip() else {}


def owned(resource, labels, annotations):
    m = resource["metadata"]
    if not all(m.get("labels", {}).get(k) == v for k, v in labels.items()) or not all(
            m.get("annotations", {}).get(k) == v for k, v in annotations.items()):
        raise ValueError("Unowned image staging resource: " + m["name"])


def delete_owned(namespace, kind, obj, labels, annotations):
    owned(obj, labels, annotations)
    name = obj["metadata"]["name"]
    uid = obj["metadata"]["uid"]
    version, plural = RESOURCES[kind]
    prefix = "/api/v1" if version == "v1" else "/apis/" + version
    options = {"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {"uid": uid}}
    if not obj["metadata"].get("deletionTimestamp"):
        oc(["delete", "--raw", f"{prefix}/namespaces/{namespace}/{plural}/{name}", "-f", "-"], options)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        remaining = get(namespace, kind, name)
        if not remaining or remaining["metadata"]["uid"] != uid:
            return
        time.sleep(1)
    raise RuntimeError(f"Timed out removing the staging {kind} {name}")


def ensure(name, namespace, owner_labels, owner_annotations, image):
    if not re.fullmatch(r"[a-z0-9./:_-]+@sha256:[0-9a-f]{64}", image):
        raise ValueError("VM inference image must be pinned by digest")
    name_image = name + "-image"
    labels = {**owner_labels, VM_LABEL: name, COMPONENT: "vm-image"}
    annotations = {**owner_annotations, VENDOR: image}
    selector = {"matchLabels": {VM_LABEL: name, COMPONENT: "vm-image"}}

    node = os.environ.get("EDU_VM_NODE", "").strip()

    def create(kind, **fields):
        obj = {"apiVersion": RESOURCES[kind][0], "kind": kind, "metadata": {
            "name": name_image, "namespace": namespace, "labels": labels, "annotations": annotations}, **fields}
        existing = get(namespace, kind, name_image)
        if existing and kind == "Pod" and (existing.get("status", {}).get("phase") == "Failed" or
                node and existing.get("spec", {}).get("nodeSelector", {}).get("kubernetes.io/hostname") != node):
            delete_owned(namespace, kind, existing, labels, annotations)
            existing = {}
        if not existing:
            result = oc(["-n", namespace, "create", "-f", "-"], obj, check=False)
            if result.returncode and "AlreadyExists" not in result.stderr:
                raise RuntimeError(result.stderr.strip()[:600])
            existing = get(namespace, kind, name_image)
        owned(existing, labels, annotations)
        return existing

    create("NetworkPolicy", spec={
        "podSelector": selector, "policyTypes": ["Ingress", "Egress"],
        "ingress": [{"from": [{"podSelector": {"matchLabels": {"kubevirt.io/domain": name}}}],
                     "ports": [{"port": 8080, "protocol": "TCP"}]}],
        "egress": [
            {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "openshift-dns"}}}],
             "ports": [{"protocol": proto, "port": port} for proto in ("UDP", "TCP") for port in (53, 5353)]},
            {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "openshift-image-registry"}},
                     "podSelector": {"matchLabels": {"docker-registry": "default"}}}],
             "ports": [{"port": 5000, "protocol": "TCP"}]},
        ],
    })
    create("ServiceAccount", automountServiceAccountToken=False)
    create("ImageStream", spec={"tags": [{"name": "runtime", "from": {"kind": "DockerImage", "name": image},
                                         "importPolicy": {"importMode": "Legacy"}, "referencePolicy": {"type": "Local"}}]})
    # Import resolves the vendor's pinned manifest list to the cluster's amd64 image.
    for _ in range(60):
        stream = get(namespace, "ImageStream", name_image)
        tags = [tag for tag in stream.get("status", {}).get("tags", []) if tag["tag"] == "runtime"]
        if tags and tags[0].get("items"):
            digest = tags[0]["items"][0]["image"]
            break
        if tags and any(c.get("status") == "False" for c in tags[0].get("conditions", [])):
            raise RuntimeError("Vendor image import failed: " + str(tags[0].get("conditions"))[:400])
        time.sleep(2)
    else:
        raise RuntimeError("Timed out importing the vendor image")
    create("Service", spec={"selector": selector["matchLabels"], "ports": [{"name": "archive", "port": 8080}]})
    safe = {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}, "runAsNonRoot": True}
    create("Pod", spec={
        **({"nodeSelector": {"kubernetes.io/hostname": node}} if node else {}),
        "serviceAccountName": name_image, "automountServiceAccountToken": False, "restartPolicy": "Never",
        "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
        "volumes": [
            {"name": "bundle", "emptyDir": {"sizeLimit": "7Gi"}},
            {"name": "identity", "projected": {"sources": [{"serviceAccountToken": {"path": "token", "expirationSeconds": 900}}]}},
            {"name": "trust", "configMap": {"name": "openshift-service-ca.crt", "items": [{"key": "service-ca.crt", "path": "ca.crt"}]}},
        ],
        "initContainers": [{
            "name": "copy", "image": SKOPEO_IMAGE, "command": ["/bin/bash", "-c", COPY], "securityContext": safe,
            "env": [{"name": "REGISTRY", "value": REGISTRY},
                    {"name": "SOURCE", "value": f"{REGISTRY}/{namespace}/{name_image}@{digest}"},
                    {"name": "GUEST_TAG", "value": GUEST_TAG}],
            "resources": {"requests": {"cpu": "100m", "memory": "128Mi", "ephemeral-storage": "4Gi"},
                          "limits": {"cpu": "2", "memory": "768Mi", "ephemeral-storage": "7Gi"}},
            "volumeMounts": [{"name": v, "mountPath": "/" + v, "readOnly": v != "bundle"} for v in ("bundle", "trust", "identity")],
        }],
        "containers": [{
            "name": "archive", "image": HTTP_IMAGE, "workingDir": "/bundle", "command": ["python3", "-c", HTTP],
            "securityContext": safe, "ports": [{"name": "archive", "containerPort": 8080}],
            "volumeMounts": [{"name": "bundle", "mountPath": "/bundle", "readOnly": True}],
            "resources": {"requests": {"cpu": "20m", "memory": "32Mi", "ephemeral-storage": "4Gi"},
                          "limits": {"cpu": "1", "memory": "256Mi", "ephemeral-storage": "7Gi"}},
            "readinessProbe": {"httpGet": {"path": "/image.tar.sha256", "port": 8080}},
        }],
    })
    # Surface copy/image failures in the AAP job instead of waiting for a guest HTTP timeout.
    deadline = time.monotonic() + 900
    last_state = "pending"
    while time.monotonic() < deadline:
        pod = get(namespace, "Pod", name_image)
        if not pod:
            raise RuntimeError("The customer image staging pod disappeared")
        owned(pod, labels, annotations)
        status = pod.get("status", {})
        if any(item.get("type") == "Ready" and item.get("status") == "True" for item in status.get("conditions", [])):
            return f"http://{name_image}.{namespace}.svc.cluster.local:8080/image.tar"
        for container in status.get("initContainerStatuses", []) + status.get("containerStatuses", []):
            state = container.get("state", {})
            last_state = state.get("waiting", {}).get("reason") or last_state
            stopped = state.get("terminated", {})
            if stopped.get("exitCode", 0) != 0:
                raise RuntimeError(f"Image staging {container['name']} failed ({stopped.get('reason', 'exit')} {stopped['exitCode']})")
        if status.get("phase") == "Failed":
            raise RuntimeError("Image staging pod failed: " + status.get("reason", "inspect its events"))
        time.sleep(4)
    raise RuntimeError("Image staging pod did not become ready: " + last_state)


def remove(name, namespace, owner_labels, owner_annotations):
    labels = {**owner_labels, VM_LABEL: name, COMPONENT: "vm-image"}
    for kind in ("Pod", "Service", "ImageStream", "ServiceAccount", "NetworkPolicy"):
        objname = name + "-image"
        old = get(namespace, kind, objname)
        if not old:
            continue
        delete_owned(namespace, kind, old, labels, owner_annotations)
