#!/usr/bin/env python3
"""Read-only inventory of EDU demo workloads and anonymous observed chat activity."""

import argparse
import json
import os
import re
import ssl
import subprocess
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote, urlencode

MANAGER = "app.kubernetes.io/managed-by"
OWNER = "edu-ai-demo-builder"
CUSTOMER = "edu-ai-demo/customer-name"
VM = "edu-ai-demo/vm"
APP = "app.kubernetes.io/name"
LEGACY = "edu-ai-demo/customer"
RUNTIME = "edu-ai-demo/runtime"
ACTIVITY = "edu-ai-demo/activity-version"
SA = Path("/var/run/secrets/kubernetes.io/serviceaccount")
LOG_LIMIT = 4 * 1024 * 1024


class Unavailable(RuntimeError):
    pass


class Cluster:
    def __init__(self):
        self.base = None
        if (
            os.environ.get("KUBERNETES_SERVICE_HOST")
            and (SA / "token").is_file()
            and not os.environ.get("KUBECONFIG")
        ):
            self.base = "https://kubernetes.default.svc"
            self.trust = ssl.create_default_context(cafile=str(SA / "ca.crt"))

    def read(self, path, params=None):
        if params:
            path += "?" + urlencode(params)
        try:
            if self.base:
                req = urllib.request.Request(
                    self.base + path,
                    headers={
                        "Authorization": "Bearer " + (SA / "token").read_text().strip(),
                    },
                )
                with urllib.request.urlopen(req, context=self.trust, timeout=25) as response:
                    return response.read()
            proc = subprocess.run(
                ["oc", "--request-timeout=25s", "get", "--raw", path],
                check=False,
                capture_output=True,
                timeout=35,
            )
            if proc.returncode:
                raise Unavailable("Unable to read " + path.partition("?")[0])
            return proc.stdout
        except (OSError, subprocess.SubprocessError) as exc:
            raise Unavailable("Unable to read " + path.partition("?")[0]) from exc

    def items(self, path, selector=""):
        params = {"limit": "200"}
        if selector:
            params["labelSelector"] = selector
        result = []
        while True:
            try:
                page = json.loads(self.read(path, params))
                items = page["items"]
                if not isinstance(items, list):
                    raise TypeError("Expected a resource list")
                result.extend(items)
                token = page.get("metadata", {}).get("continue")
            except (KeyError, TypeError, ValueError) as exc:
                raise Unavailable("Invalid inventory listing for " + path) from exc
            if not token:
                return result
            params["continue"] = token


def ns_path(ns, kind, api="/api/v1"):
    return f"{api}/namespaces/{quote(ns, safe='')}/{kind}"


def meta(obj):
    return obj.get("metadata", {})


def labels(obj):
    return meta(obj).get("labels", {})


def annotations(obj):
    return meta(obj).get("annotations", {})


def name(obj):
    return meta(obj)["name"]


def quantity(raw):
    if raw is None:
        return None
    units = {"": 1, "n": Decimal("1e-9"), "u": Decimal("1e-6"), "m": Decimal("1e-3")}
    units.update({u: 1000**i for i, u in enumerate(("k", "M", "G", "T", "P", "E"), 1)})
    units.update({u: 1024**i for i, u in enumerate(("Ki", "Mi", "Gi", "Ti", "Pi", "Ei"), 1)})
    m = re.fullmatch(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)([A-Za-z]*)", str(raw))
    if not m or m[2] not in units:
        return None
    try:
        return Decimal(m[1]) * units[m[2]]
    except InvalidOperation:
        return None


def whole_or_none(x):
    return int(x) if x is not None else None


def sum_known(values):
    values = list(values)
    return sum(values) if all(v is not None for v in values) else None


def pod_row(p, role, ns, metric):
    spec, status = p.get("spec", {}), p.get("status", {})
    containers = status.get("containerStatuses", [])
    expected = {c["name"] for c in spec.get("containers", [])}
    running = {c["name"] for c in containers if "running" in c.get("state", {})}
    measurements = (metric or {}).get("containers", [])
    sampled = {c["name"] for c in measurements}
    covered = bool(measurements) and (not running or running <= sampled)
    cpu = sum_known(quantity(c.get("usage", {}).get("cpu")) for c in measurements) if covered else None
    memory = sum_known(quantity(c.get("usage", {}).get("memory")) for c in measurements) if covered else None
    return {
        "name": name(p),
        "namespace": ns,
        "role": role,
        "phase": status.get("phase", "Unknown"),
        "ready": any(
            c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions", [])
        ),
        "ready_containers": sum(1 for c in containers if c["name"] in expected and c.get("ready")),
        "containers": len(expected),
        "node": spec.get("nodeName"),
        "cpu_m": round(float(cpu * 1000), 4) if cpu is not None else None,
        "memory_bytes": whole_or_none(memory),
        "sample_time": (metric or {}).get("timestamp"),
    }


def instant(stamp):
    if not isinstance(stamp, str):
        return None
    # Kubernetes CRI timestamps may carry nanoseconds. Older Python versions'
    # fromisoformat accepts fractional seconds only with three or six digits.
    stamp = re.sub(r"\.(\d+)", lambda m: "." + m[1][:6].ljust(6, "0"), stamp)
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None


class Activity:
    def __init__(self, since):
        self.since = since
        self.sessions = set()
        self.browser = self.api = self.covered_segments = 0
        self.incomplete = False

    def segment(self, raw):
        covers_window = False
        if len(raw) >= LOG_LIMIT:
            self.incomplete = True
        for line in raw.splitlines():
            stamp, _, message = line.partition(b" ")
            at = instant(stamp.decode("ascii", errors="replace"))
            if at is None or at.tzinfo is None:
                self.incomplete = True
                continue
            if at <= self.since:
                covers_window = True
            if not message.startswith(b"{"):
                continue
            try:
                event = json.loads(message)
            except (ValueError, UnicodeError, RecursionError):
                continue
            if not isinstance(event, dict) or type(event.get("v")) is not int or event["v"] != 1:
                continue
            if event.get("event") == "edu_demo_started":
                covers_window = True
            if at < self.since or event.get("event") != "edu_demo_chat":
                continue
            if event.get("source") == "api":
                self.api += 1
            elif event.get("source") == "browser":
                self.browser += 1
                session = event.get("session")
                if isinstance(session, str) and re.fullmatch("[a-f0-9]{32}", session):
                    self.sessions.add(session)
                else:
                    self.incomplete = True
        if covers_window:
            self.covered_segments += 1
        else:
            self.incomplete = True

    def pod(self, cluster, ns, pod):
        status = next(
            (c for c in pod.get("status", {}).get("containerStatuses", []) if c["name"] == "chatbot"), {}
        )
        if annotations(pod).get(ACTIVITY) != "1" or not status:
            self.incomplete = True
            return
        restarts = status.get("restartCount", 0)
        terminated = status.get("lastState", {}).get("terminated", {})
        prior_end = instant(terminated.get("finishedAt"))
        recent_previous = restarts and (not prior_end or prior_end >= self.since)
        prior_start = instant(terminated.get("startedAt"))
        if restarts > 1 and recent_previous and (not prior_start or prior_start > self.since):
            self.incomplete = True
        for previous in [False, True] if recent_previous else [False]:
            # Read the latest bounded log with timestamps: a startup record *or*
            # a retained line preceding the 24h window establishes coverage.
            query = {"container": "chatbot", "timestamps": "true", "limitBytes": str(LOG_LIMIT)}
            if previous:
                query["previous"] = "true"
            try:
                raw = cluster.read(ns_path(ns, "pods/" + quote(name(pod), safe="") + "/log"), query)
            except Unavailable:
                self.incomplete = True
                continue
            self.segment(raw)

    def result(self):
        observed = bool(self.covered_segments or self.browser or self.api)
        scope = ("partial" if self.incomplete else "retained") if observed else "unknown"
        return {
            "coverage": scope,
            "browser_sessions_observed": len(self.sessions) if observed else None,
            "browser_requests_observed": self.browser if observed else None,
            "api_requests_observed": self.api if observed else None,
        }


def customer(obj):
    value = annotations(obj).get(CUSTOMER)
    if not isinstance(value, str) or not value.isprintable() or not value or value != value.strip():
        raise Unavailable("Invalid customer identity on " + name(obj))
    return value


def app_inventory(cluster, ns, legacy=False):
    selector = APP + "=edu-ai-demo" if legacy else MANAGER + "=" + OWNER
    pods = cluster.items(ns_path(ns, "pods"), selector)
    deployments = cluster.items(ns_path(ns, "deployments", "/apis/apps/v1"), selector)
    pod_specs = [p["spec"] for p in pods] + [d["spec"]["template"]["spec"] for d in deployments]
    mounts = {
        v["persistentVolumeClaim"]["claimName"]
        for spec in pod_specs
        for v in spec.get("volumes", [])
        if v.get("persistentVolumeClaim", {}).get("claimName")
    }
    claims = [
        c
        for c in cluster.items(ns_path(ns, "persistentvolumeclaims"))
        if name(c) in mounts
        or (labels(c).get(APP) == "edu-ai-demo" if legacy else labels(c).get(MANAGER) == OWNER)
    ]
    return {
        "pods": pods,
        "deployments": deployments,
        "claims": claims,
        "routes": cluster.items(ns_path(ns, "routes", "/apis/route.openshift.io/v1"), selector),
    }


def project(ns, owner, legacy=False):
    return {
        "customer": owner,
        "namespace": ns,
        "ownership": "legacy" if legacy else "managed" if ns else "vm_pool_only",
        "pods": [],
        "deployments": [],
        "routes": [],
        "vms": [],
        "pvcs": [],
        "backends": [],
        "pool_objects": [],
    }


def pvc_row(claim, ns):
    spec, status = claim.get("spec", {}), claim.get("status", {})
    return {
        "name": name(claim),
        "namespace": ns,
        "phase": status.get("phase", "Unknown"),
        "requested_bytes": whole_or_none(
            quantity(spec.get("resources", {}).get("requests", {}).get("storage"))
        ),
        "bound_capacity_bytes": whole_or_none(quantity(status.get("capacity", {}).get("storage"))),
    }


def model_config(pod_spec):
    container = next((c for c in pod_spec.get("containers", []) if c.get("name") == "chatbot"), {})
    env = {v["name"]: v for v in container.get("env", [])}
    provider = env.get("DEMO_AI_PROVIDER", {}).get("value", "unknown")
    endpoint = env.get("DEMO_LLM_ENDPOINT", {})
    # Secrets, model URLs, auth parameters and raw environment never leave the collector.
    configured = bool(endpoint.get("value") or endpoint.get("valueFrom"))
    backend = "retrieval only"
    if configured:
        backend = "dedicated VM" if provider == "rhel_vm" else "external/shared, usage unallocated"
    return {"provider": provider, "backend": backend}


def vm_row(vm_obj, instance, ns, vm_name):
    spec = (vm_obj or {}).get("spec", {}).get("template", {}).get("spec", {})
    if instance:
        spec = instance.get("spec", {}) or spec
    domain = spec.get("domain", {})
    cpu = domain.get("cpu", {})
    status = (instance or {}).get("status", {})
    return {
        "name": vm_name,
        "namespace": ns,
        "phase": status.get("phase") or (vm_obj or {}).get("status", {}).get("printableStatus", "Unknown"),
        "ready": any(
            c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions", [])
        ),
        "node": status.get("nodeName"),
        "vcpus": cpu.get("cores", 1) * cpu.get("sockets", 1) * cpu.get("threads", 1) if cpu else None,
        "cpu_request": domain.get("resources", {}).get("requests", {}).get("cpu"),
        "guest_memory_bytes": whole_or_none(quantity(domain.get("memory", {}).get("guest"))),
    }


def collect(cluster, since):
    projects, meters, groups = {}, {}, defaultdict(dict)
    namespaces = cluster.items("/api/v1/namespaces")
    pools = []
    for obj in namespaces:
        ns, lab = name(obj), labels(obj)
        if lab.get("edu-ai-demo/purpose") == "rhel-vms":
            pools.append(ns)
            continue
        managed = lab.get(MANAGER) == OWNER
        legacy = (
            not managed
            and ns.startswith("edu-ai-demo-")
            and lab.get(APP) == "edu-ai-demo"
            and bool(lab.get(LEGACY))
        )
        if not managed and not legacy:
            continue
        owner = customer(obj) if managed else lab[LEGACY]
        identity = (owner, "legacy" if legacy else "managed")
        if identity in projects:
            raise Unavailable("Two demo namespaces claim the same customer")
        projects[identity] = project(ns, owner, legacy)
        projects[identity]["runtime"] = annotations(obj).get(RUNTIME)

    def metrics(ns):
        if ns not in meters:
            try:
                items = cluster.items(ns_path(ns, "pods", "/apis/metrics.k8s.io/v1beta1"))
                meters[ns] = {name(obj): obj for obj in items}
            except Unavailable:
                meters[ns] = {}
        return meters[ns]

    for demo in list(projects.values()):
        ns = demo["namespace"]
        objects = app_inventory(cluster, ns, demo["ownership"] == "legacy")
        activity = Activity(since)
        for pod in objects["pods"]:
            demo["pods"].append(pod_row(pod, "app", ns, metrics(ns).get(name(pod))))
            if any(c["name"] == "chatbot" for c in pod["spec"].get("containers", [])):
                activity.pod(cluster, ns, pod)
        demo["activity"] = activity.result()
        for deploy in objects["deployments"]:
            demo["deployments"].append(
                {
                    "name": name(deploy),
                    "desired": deploy["spec"].get("replicas", 1),
                    "ready": deploy.get("status", {}).get("readyReplicas", 0),
                }
            )
            backend = model_config(deploy["spec"]["template"]["spec"])
            if backend not in demo["backends"]:
                demo["backends"].append(backend)
        for route in objects["routes"]:
            host = route.get("spec", {}).get("host")
            if host:
                demo["routes"].append(("https://" if route["spec"].get("tls") else "http://") + host)
        demo["pvcs"] = [pvc_row(c, ns) for c in objects["claims"]]
    # The bootstrap marker is the first and last resource; also discover workloads
    # and stale disks directly, including a CDI PVC whose controller replaces managed-by.
    for ns in pools:
        controllers = {}
        for kind, api in (
            ("configmaps", "/api/v1"),
            ("datavolumes", "/apis/cdi.kubevirt.io/v1beta1"),
            ("virtualmachines", "/apis/kubevirt.io/v1"),
            ("virtualmachineinstances", "/apis/kubevirt.io/v1"),
            ("pods", "/api/v1"),
            ("persistentvolumeclaims", "/api/v1"),
        ):
            selector = "" if kind in ("pods", "virtualmachineinstances") else VM
            for obj in cluster.items(ns_path(ns, kind, api), selector):
                lab = labels(obj)
                vm_name = lab.get(VM)
                refs = [r for r in meta(obj).get("ownerReferences", []) if r.get("controller")]
                parent = next((controllers[r["uid"]] for r in refs if r.get("uid") in controllers), None)
                if parent and kind in ("pods", "virtualmachineinstances"):
                    who, vm_name = parent
                elif vm_name and (
                    lab.get(MANAGER) == OWNER
                    or (
                        kind == "persistentvolumeclaims"
                        and lab.get(MANAGER) == "cdi-controller"
                        and any(
                            r.get("kind") == "DataVolume" and r.get("name") == vm_name + "-root" for r in refs
                        )
                    )
                ):
                    who = customer(obj)
                else:
                    continue
                if kind in ("virtualmachines", "virtualmachineinstances") and meta(obj).get("uid"):
                    controllers[meta(obj)["uid"]] = (who, vm_name)
                identity = (who, "managed")
                if identity not in projects:
                    projects[identity] = project(None, who)
                demo = projects[identity]
                demo["pool_objects"].append({"namespace": ns, "kind": kind, "name": name(obj)})
                if kind in ("virtualmachines", "virtualmachineinstances"):
                    groups[(who, ns, vm_name)][kind] = obj
                elif kind == "persistentvolumeclaims":
                    demo["pvcs"].append(pvc_row(obj, ns))
                elif kind == "pods":
                    role = (
                        "VM launcher"
                        if lab.get("kubevirt.io/domain") == vm_name
                        or any(r.get("kind") == "VirtualMachineInstance" for r in refs)
                        else "image staging"
                    )
                    demo["pods"].append(pod_row(obj, role, ns, metrics(ns).get(name(obj))))
    for (who, ns, vm_name), v in groups.items():
        projects[(who, "managed")]["vms"].append(
            vm_row(v.get("virtualmachines"), v.get("virtualmachineinstances"), ns, vm_name)
        )
    for demo in projects.values():
        demo.setdefault("runtime", "rhel_vm")
        demo.setdefault("activity", Activity(since).result())
        running = [p for p in demo["pods"] if p["phase"] == "Running"]
        active = [p for p in demo["pods"] if p["phase"] not in ("Succeeded", "Failed") and p["node"]]
        demo["dedicated_usage"] = {
            "running_pods": len(running),
            "ready_pods": sum(p["ready"] for p in running),
            "cpu_m": sum_known(p["cpu_m"] for p in active),
            "memory_bytes": sum_known(p["memory_bytes"] for p in active),
            "requested_pvc_bytes": sum_known(c["requested_bytes"] for c in demo["pvcs"]),
            "bound_pvc_capacity_bytes": sum_known(
                c["bound_capacity_bytes"] for c in demo["pvcs"] if c["phase"] == "Bound"
            ),
        }
    return sorted(projects.values(), key=lambda x: (x["customer"].casefold(), x["namespace"] or ""))


def pretty_bytes(value):
    if value is None:
        return "unknown"
    for divisor, unit in ((1024**3, "GiB"), (1024**2, "MiB"), (1024, "KiB")):
        if value >= divisor:
            return f"{value / divisor:.1f} {unit}"
    return f"{value} B"


def text_report(demos, at):
    lines = [f"EDU demo inventory · {at}"]
    for d in demos:
        u, a = d["dedicated_usage"], d["activity"]
        mem = pretty_bytes(u["memory_bytes"])
        cpu = "unknown" if u["cpu_m"] is None else f"{u['cpu_m']:.2f}m"
        lines += [
            "",
            f"{d['customer']} · {d['ownership']} · {d['namespace'] or 'VM pool only'}",
            f"  {u['running_pods']} running pods ({u['ready_pods']} ready) · CPU {cpu} · memory {mem}",
            f"  {len(d['vms'])} VMs · {pretty_bytes(u['requested_pvc_bytes'])} PVC requested · {pretty_bytes(u['bound_pvc_capacity_bytes'])} bound capacity",
        ]
        for deploy in d["deployments"]:
            lines.append(
                f"  Deployment {deploy['name']} · {deploy['ready']}/{deploy['desired']} replicas ready"
            )
        for p in d["pods"]:
            lines.append(
                f"  {p['role']} {p['name']} · {p['phase']} · {'ready' if p['ready'] else 'not ready'} · {p['node'] or 'unscheduled'}"
            )
        for vm in d["vms"]:
            lines.append(
                f"  VM {vm['name']} · {vm['phase']} · {vm['node'] or 'unscheduled'} · {vm['vcpus']} vCPU · {pretty_bytes(vm['guest_memory_bytes'])} guest RAM"
            )
        for url in d["routes"]:
            lines.append("  " + url)
        if d["ownership"] == "vm_pool_only":
            lines.append(
                "  Pool objects · " + ", ".join(f"{o['kind']}/{o['name']}" for o in d["pool_objects"])
            )
        for b in d["backends"]:
            lines.append(f"  {b['provider']} · {b['backend']}")
        if a["coverage"] == "unknown":
            lines.append("  Chat in the last 24h · unknown (legacy, unavailable or incomplete app logs)")
        else:
            coverage = "lower bounds, logs incomplete" if a["coverage"] == "partial" else "retained logs"
            lines.append(
                f"  Observed chat in the last 24h · {a['browser_sessions_observed']} browser sessions, {a['browser_requests_observed']} browser requests, "
                f"{a['api_requests_observed']} API requests ({coverage})."
            )
    lines.extend(
        ["", "Chat activity covers currently present app pods and at most one previous container per pod."]
    )
    return "\n".join(lines)


def main(argv=None):
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument("--format", choices=("text", "json"), default="text")
    parsed = args.parse_args(argv)
    try:
        now = datetime.now(timezone.utc)
        demos = collect(Cluster(), now - timedelta(hours=24))
    except (Unavailable, OSError, ValueError, KeyError) as e:
        print("EDU inventory failed: " + str(e), file=sys.stderr)
        return 1
    at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    if parsed.format == "json":
        print(
            json.dumps(
                {"observed_at": at, "activity_window_hours": 24, "demo_count": len(demos), "demos": demos},
                ensure_ascii=False,
            )
        )
    elif demos:
        print(text_report(demos, at))
    return 0


if __name__ == "__main__":
    sys.exit(main())
