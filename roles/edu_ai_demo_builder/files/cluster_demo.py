"""Manage one demo namespace and its content-addressed application bundles."""
import argparse
import base64
import hashlib
import io
import json
import os
import subprocess
import sys
import time
import uuid
import zipfile
from pathlib import Path


OWNER = {"app.kubernetes.io/managed-by": "edu-ai-demo-builder"}
CUSTOMER = "edu-ai-demo/customer-name"
RUNTIME = "edu-ai-demo/runtime"
VM_NAMESPACE = "edu-ai-demo/vm-namespace"
VM_NAME = "edu-ai-demo/vm-name"
VM_MODES = {"rhel_vm", "openshift_ai_and_rhel_vm"}


def oc(*args, obj=None, check=True):
    timeout = "--request-timeout=250s" if "rollout" in args else "--request-timeout=30s"
    p = subprocess.run(["oc", timeout, *args], input=json.dumps(obj) if obj is not None else None,
                       text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and p.returncode:
        raise RuntimeError(p.stderr.strip() or p.stdout.strip())
    return p


def get(kind, name, ns=None):
    args = ["-n", ns] if ns else []
    out = oc(*args, "get", kind, name, "--ignore-not-found", "-o", "json").stdout
    return json.loads(out) if out.strip() else {}


def owned_namespace(args, create=False):
    namespace = get("namespace", args.namespace)
    if not namespace and create:
        meta = {"name": args.namespace, "labels": OWNER, "annotations": {CUSTOMER: args.customer, RUNTIME: args.runtime}}
        oc("create", "-f", "-", obj={"apiVersion": "v1", "kind": "Namespace", "metadata": meta})
        namespace = get("namespace", args.namespace)
    if not namespace:
        return {}
    m = namespace["metadata"]
    if m.get("labels", {}).get("app.kubernetes.io/managed-by") != OWNER["app.kubernetes.io/managed-by"] or m.get("annotations", {}).get(CUSTOMER) != args.customer:
        raise ValueError("Namespace exists with a different owner or customer. Refusing to change it.")
    if m.get("deletionTimestamp") and args.action != "cleanup":
        raise ValueError("Namespace is terminating. Wait for deletion before retrying.")
    current = m["annotations"].get(RUNTIME)
    additive = current in {"openshift_ai", "rhel_vm"} and args.runtime == "openshift_ai_and_rhel_vm"
    if create and current != args.runtime and not additive:
        raise ValueError("An environment for this customer already uses another runtime. Clean it up before switching runtimes.")
    return namespace


def vm_action(args, action, annotations):
    pool = annotations.get(VM_NAMESPACE, args.vm_namespace)
    name = annotations.get(VM_NAME, args.vm_name)
    if not pool or not name:
        raise ValueError("Missing VM pool or customer VM name")
    helper = Path(__file__).resolve().parents[3] / "vm" / "manage_vm.py"
    subprocess.run([sys.executable, str(helper), action, "--namespace", pool, "--name", name,
                    "--customer", args.customer], check=True, stdout=sys.stderr)


def deploy_vm(args, namespace):
    metadata = namespace["metadata"]
    annotations = metadata["annotations"]
    if (annotations.get(VM_NAMESPACE, args.vm_namespace), annotations.get(VM_NAME, args.vm_name)) != (args.vm_namespace, args.vm_name):
        raise ValueError("The registered VM location changed. Review the existing demo before redeploying.")
    desired = {RUNTIME: args.runtime, VM_NAMESPACE: args.vm_namespace, VM_NAME: args.vm_name}
    patch = {"metadata": {"resourceVersion": metadata["resourceVersion"], "annotations": desired}}
    oc("patch", "namespace", args.namespace, "--type=merge", "-p", json.dumps(patch))
    vm_action(args, "deploy", desired)


def bundle(app):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(Path(app).rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                if path.is_symlink():
                    raise ValueError("Unexpected symlink in app bundle")
                info = zipfile.ZipInfo(path.relative_to(app).as_posix())
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, path.read_bytes())
    return buf.getvalue()


def publish(args):
    data = bundle(Path(args.workspace) / "app")
    digest = hashlib.sha256(data).hexdigest()
    parts = []
    for offset in range(0, len(data), 512 * 1024):
        name = "edu-ai-bundle-%s-%04d" % (digest[:20], len(parts))
        encoded = base64.b64encode(data[offset:offset + 512 * 1024]).decode("ascii")
        existing = get("configmap", name, args.namespace)
        if existing:
            if existing.get("binaryData", {}).get("part") != encoded:
                raise ValueError("Existing bundle failed its content identity check")
        else:
            oc("create", "-f", "-", obj={"apiVersion": "v1", "kind": "ConfigMap", "immutable": True,
               "metadata": {"name": name, "namespace": args.namespace, "labels": {**OWNER, "edu-ai-demo/bundle": "true"}},
               "binaryData": {"part": encoded}})
        parts.append(name)
    template_path = Path(args.workspace) / "openshift-demo.json"
    manifest = json.loads(template_path.read_text())
    for resource in manifest["items"]:
        if resource["kind"] == "Deployment":
            pod = resource["spec"]["template"]["spec"]
            pod["volumes"].append({"name": "bundle", "projected": {"sources": [
                {"configMap": {"name": n, "items": [{"key": "part", "path": "%04d" % i}]}} for i, n in enumerate(parts)]}})
            pod["initContainers"][0]["env"] = [{"name": "BUNDLE_SHA256", "value": digest}]
    oc("apply", "-f", "-", obj=manifest)
    urls = {}
    for resource in manifest["items"]:
        if resource["kind"] == "Route":
            name = resource["metadata"]["name"]
            oc("-n", args.namespace, "rollout", "status", "deployment/" + name, "--timeout=240s")
            route = get("route", name, args.namespace)
            urls[resource["metadata"]["annotations"][RUNTIME]] = "https://" + route["spec"]["host"]
    # Keep the bundles of retained ReplicaSets so a rollback can still start pods.
    # Discard failed/stale revisions once none of those ReplicaSets reference them.
    replicas = json.loads(oc("-n", args.namespace, "get", "replicasets", "-l", "app.kubernetes.io/managed-by=edu-ai-demo-builder", "-o", "json").stdout)["items"]
    retained = set(parts)
    for rs in replicas:
        for volume in rs["spec"]["template"]["spec"].get("volumes", []):
            for source in volume.get("projected", {}).get("sources", []):
                retained.add(source.get("configMap", {}).get("name"))
    bundles = oc("-n", args.namespace, "get", "cm", "-l", "edu-ai-demo/bundle=true,app.kubernetes.io/managed-by=edu-ai-demo-builder", "-o", "name")
    for item in bundles.stdout.splitlines():
        if item.partition("/")[2] not in retained:
            oc("-n", args.namespace, "delete", item)
    return urls


def wait_for_deletion(args, uid):
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        if get("namespace", args.namespace).get("metadata", {}).get("uid") != uid:
            return
        time.sleep(2)
    raise RuntimeError("Namespace deletion is still in progress; inspect its finalizers")


def cleanup(args, uid, annotations):
    # A namespace is the lifecycle unit for generated configmaps, routes, pods, and services.
    # Refuse a namespace that acquired persistent or foreign workloads after deployment.
    resources = ["persistentvolumeclaims", "statefulsets.apps", "daemonsets.apps", "jobs.batch", "cronjobs.batch",
                 "virtualmachines.kubevirt.io", "datavolumes.cdi.kubevirt.io"]
    installed = set(oc("api-resources", "--namespaced", "-o", "name").stdout.splitlines())
    for resource in resources:
        if resource in installed and oc("-n", args.namespace, "get", resource, "-o", "name").stdout.strip():
            raise ValueError("Found " + resource + " in a demo that provisions none; inspect before cleanup")
    claims = oc("-n", args.namespace, "get", "configmaps,deployments,replicasets,pods,services,routes", "-o", "json")
    for item in json.loads(claims.stdout)["items"]:
        m = item["metadata"]
        if m["name"] in ("kube-root-ca.crt", "openshift-service-ca.crt", "odh-kserve-custom-ca-bundle", "odh-trusted-ca-bundle") and item["kind"] == "ConfigMap":
            continue
        if m.get("labels", {}).get("app.kubernetes.io/managed-by") != OWNER["app.kubernetes.io/managed-by"]:
            raise ValueError("Found an unmanaged resource in the demo namespace: " + item["kind"] + "/" + m["name"])
    vm_recorded = annotations.get(RUNTIME) in VM_MODES or annotations.get(VM_NAME)
    # A pool bootstrap can survive an interrupted upgrade from the OpenShift-only demo.
    pool_marker = args.vm_enabled and get("configmap", args.vm_name + "-bootstrap", args.vm_namespace)
    if vm_recorded or pool_marker:
        vm_action(args, "cleanup", annotations)
    oc("delete", "--raw", "/api/v1/namespaces/" + args.namespace, "-f", "-",
       obj={"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {"uid": uid}})
    wait_for_deletion(args, uid)


def discover(args):
    ns = owned_namespace(args)
    if not ns:
        raise ValueError("No managed namespace was found for this customer")
    items = json.loads(oc("-n", args.namespace, "get", "route", "-l", "app.kubernetes.io/managed-by=edu-ai-demo-builder", "-o", "json").stdout)
    urls = {i["metadata"]["annotations"][RUNTIME]: "https://" + i["spec"]["host"] for i in items["items"]}
    runtime = ns["metadata"]["annotations"][RUNTIME]
    expected = {"openshift_ai", "rhel_ai"} if runtime == "all" else {runtime}
    if runtime in VM_MODES:
        vm_action(args, "check", ns["metadata"]["annotations"])
        if runtime == "openshift_ai_and_rhel_vm":
            expected = {"openshift_ai", "rhel_vm"}
    # 'all' with a separate Podman target validates its RHEL app on that host.
    if args.podman_rhel and runtime == "all":
        expected = {"openshift_ai"}
    if set(urls) != expected:
        raise ValueError("Deployed routes do not match the registered runtime")
    return urls


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["discover", "deploy", "cleanup"])
    for name in ("namespace", "customer", "runtime", "workspace"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--podman-rhel", action="store_true")
    parser.add_argument("--vm-enabled", action="store_true")
    parser.add_argument("--vm-namespace")
    parser.add_argument("--vm-name")
    args = parser.parse_args()
    if args.action == "discover":
        print(json.dumps(discover(args)))
        return
    ns = owned_namespace(args, create=args.action == "deploy")
    if not ns:
        if args.action == "cleanup" and args.vm_enabled:
            vm_action(args, "cleanup", {})
        print("{}")
        return
    if ns["metadata"].get("deletionTimestamp"):
        wait_for_deletion(args, ns["metadata"]["uid"])
        print("{}")
        return
    token = uuid.uuid4().hex
    lock = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "edu-ai-operation", "namespace": args.namespace, "labels": OWNER}, "data": {"token": token, "action": args.action, "job": os.environ.get("AWX_JOB_ID", "")}}
    operation = oc("create", "-f", "-", obj=lock, check=False)
    if operation.returncode:
        previous = get("configmap", "edu-ai-operation", args.namespace)
        if previous:
            info = previous.get("data", {})
            raise ValueError("Another operation holds the customer lock (action %s, job %s). Verify that the job has ended before manually clearing the edu-ai-operation ConfigMap." % (info.get("action", "?"), info.get("job") or "CLI/unknown"))
        raise RuntimeError(operation.stderr.strip())
    try:
        current_namespace = owned_namespace(args)
        if current_namespace.get("metadata", {}).get("uid") != ns["metadata"]["uid"]:
            raise ValueError("Namespace identity changed while acquiring the lock")
        if args.action == "deploy":
            if args.runtime in VM_MODES:
                deploy_vm(args, current_namespace)
            print(json.dumps(publish(args)))
        else:
            cleanup(args, ns["metadata"]["uid"], current_namespace["metadata"]["annotations"])
            print("{}")
    finally:
        current = get("configmap", "edu-ai-operation", args.namespace)
        if current.get("data", {}).get("token") == token:
            oc("-n", args.namespace, "delete", "configmap", "edu-ai-operation", "--ignore-not-found")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError) as exc:
        sys.exit(str(exc))
