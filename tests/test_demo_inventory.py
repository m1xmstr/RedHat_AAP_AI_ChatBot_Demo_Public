import contextlib
import io
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import demo_inventory as inv

SINCE = datetime(2026, 11, 21, 10, tzinfo=timezone.utc)
NOW = "2026-11-22T08:23:17.721111234Z"
OLD = "2026-11-20T23:15:00Z"


def obj(n, *, labels=None, notes=None, uid=None, controller=None, **rest):
    m = {"name": n, "labels": labels or {}, "annotations": notes or {}}
    if uid:
        m["uid"] = uid
    if controller:
        m["ownerReferences"] = [
            {"controller": True, "kind": controller[0], "name": controller[1], "uid": controller[2]}
        ]
    return {"metadata": m, **rest}


def chat(stamp, source, session=None):
    event = {"v": 1, "event": "edu_demo_chat", "source": source}
    if session:
        event["session"] = session
    return stamp.encode() + b" " + json.dumps(event).encode() + b"\n"


class FakeCluster:
    def __init__(self):
        self.rows = {}
        self.logs = {}

    def items(self, path, selector=""):
        value = self.rows.get(path, [])
        if isinstance(value, Exception):
            raise value
        if selector and "=" in selector:
            k, v = selector.split("=", 1)
            return [x for x in value if inv.labels(x).get(k) == v]
        return value

    def read(self, path, params):
        value = self.logs[(path, params.get("previous", "false"))]
        if isinstance(value, Exception):
            raise value
        return value


class InventoryTest(unittest.TestCase):
    def test_no_demos_and_failed_discovery_are_distinct(self):
        fake = FakeCluster()
        for fmt in ("text", "json"):
            out = io.StringIO()
            with patch.object(inv, "Cluster", return_value=fake), contextlib.redirect_stdout(out):
                self.assertEqual(0, inv.main(["--format", fmt]))
            if fmt == "text":
                self.assertEqual("", out.getvalue())
            else:
                self.assertEqual([], json.loads(out.getvalue())["demos"])
        fake.rows["/api/v1/namespaces"] = inv.Unavailable("cannot list namespaces")
        out, err = io.StringIO(), io.StringIO()
        with (
            patch.object(inv, "Cluster", return_value=fake),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            self.assertNotEqual(0, inv.main(["--format", "json"]))
        self.assertEqual("", out.getvalue())
        self.assertIn("cannot list namespaces", err.getvalue())

    def test_legacy_observation_and_separately_accounted_inference(self):
        c = FakeCluster()
        ns = "edu-ai-demo-previous-school"
        c.rows["/api/v1/namespaces"] = [
            obj(ns, labels={inv.APP: "edu-ai-demo", inv.LEGACY: "old-school"}),
            obj("edu-ai-demo-lookalike"),
        ]
        workload = {inv.APP: "edu-ai-demo"}
        spec = {
            "containers": [
                {
                    "name": "chatbot",
                    "env": [
                        {"name": "DEMO_AI_PROVIDER", "value": "openshift_ai"},
                        {"name": "DEMO_LLM_ENDPOINT", "value": "http://private.example/?key=hidden"},
                    ],
                }
            ],
            "nodeName": "worker",
        }
        status = {
            "phase": "Running",
            "conditions": [{"type": "Ready", "status": "True"}],
            "containerStatuses": [{"name": "chatbot", "ready": True, "state": {"running": {}}}],
        }
        c.rows[inv.ns_path(ns, "pods")] = [obj("app", labels=workload, spec=spec, status=status)]
        c.rows[inv.ns_path(ns, "deployments", "/apis/apps/v1")] = [
            obj(
                "app",
                labels=workload,
                spec={"template": {"spec": spec}, "replicas": 1},
                status={"readyReplicas": 1},
            )
        ]
        c.rows[inv.ns_path(ns, "pods", "/apis/metrics.k8s.io/v1beta1")] = [
            obj("app", containers=[{"name": "chatbot", "usage": {"cpu": "1250000n", "memory": "64Mi"}}])
        ]
        c.rows[inv.ns_path(ns, "persistentvolumeclaims")] = [
            obj("foreign", spec={"resources": {"requests": {"storage": "1Ti"}}})
        ]
        rows = inv.collect(c, SINCE)
        self.assertEqual(1, len(rows))
        u = rows[0]["dedicated_usage"]
        self.assertEqual(
            (1, 1, 1.25, 64 * 1024**2, 0),
            (u["ready_pods"], u["running_pods"], u["cpu_m"], u["memory_bytes"], u["requested_pvc_bytes"]),
        )
        self.assertEqual("unknown", rows[0]["activity"]["coverage"])
        self.assertIn("usage unallocated", rows[0]["backends"][0]["backend"])
        self.assertNotIn("hidden", json.dumps(rows))

    def test_vm_pool_partial_deployment_and_unlabeled_launcher(self):
        c = FakeCluster()
        ns = "vm-pool"
        guest = "rhel-school"
        tag = {inv.VM: guest, inv.MANAGER: inv.OWNER}
        who = {inv.CUSTOMER: "School"}
        c.rows["/api/v1/namespaces"] = [obj(ns, labels={"edu-ai-demo/purpose": "rhel-vms"})]
        c.rows[inv.ns_path(ns, "virtualmachines", "/apis/kubevirt.io/v1")] = [
            obj(
                guest,
                labels=tag,
                notes=who,
                uid="vm",
                spec={"template": {"spec": {"domain": {"cpu": {"cores": 2}}}}},
            )
        ]
        c.rows[inv.ns_path(ns, "virtualmachineinstances", "/apis/kubevirt.io/v1")] = [
            obj(
                guest,
                controller=("VirtualMachine", guest, "vm"),
                uid="vmi",
                spec={"domain": {"cpu": {"cores": 2}}},
                status={"phase": "Running", "nodeName": "worker-four"},
            )
        ]
        c.rows[inv.ns_path(ns, "pods")] = [
            obj(
                "launcher",
                controller=("VirtualMachineInstance", guest, "vmi"),
                spec={"nodeName": "worker-four", "containers": [{"name": "compute"}]},
                status={"phase": "Running"},
            ),
            obj("unrelated", spec={}, status={}),
        ]
        c.rows[inv.ns_path(ns, "pods", "/apis/metrics.k8s.io/v1beta1")] = inv.Unavailable("no metrics")
        c.rows[inv.ns_path(ns, "persistentvolumeclaims")] = [
            obj(
                guest + "-root",
                labels={inv.VM: guest, inv.MANAGER: "cdi-controller"},
                notes=who,
                controller=("DataVolume", guest + "-root", "dv"),
                spec={"resources": {"requests": {"storage": "48Gi"}}},
                status={"phase": "Bound", "capacity": {"storage": "48Gi"}},
            )
        ]
        rows = inv.collect(c, SINCE)
        self.assertEqual(1, len(rows))
        self.assertEqual(
            ("vm_pool_only", 1, 1, "VM launcher"),
            (rows[0]["ownership"], len(rows[0]["vms"]), len(rows[0]["pods"]), rows[0]["pods"][0]["role"]),
        )
        self.assertIsNone(rows[0]["dedicated_usage"]["cpu_m"])
        self.assertEqual(48 * 1024**3, rows[0]["dedicated_usage"]["bound_pvc_capacity_bytes"])

    def test_bounded_session_dedup_across_current_and_previous(self):
        activity = inv.Activity(SINCE)
        p = obj(
            "app",
            notes={inv.ACTIVITY: "1"},
            status={"containerStatuses": [{"name": "chatbot", "restartCount": 1}]},
        )
        c = FakeCluster()
        path = inv.ns_path("demo", "pods/app/log")
        token = "a" * 32
        c.logs[(path, "false")] = (
            (NOW + ' {"v":1,"event":"edu_demo_started"}\n').encode()
            + chat(NOW, "browser", token)
            + chat(NOW, "validation")
            + chat(NOW, "api")
        )
        c.logs[(path, "true")] = chat(OLD, "browser", "b" * 32) + chat(NOW, "browser", token)
        activity.pod(c, "demo", p)
        r = activity.result()
        self.assertEqual(
            ("retained", 1, 2, 1),
            (
                r["coverage"],
                r["browser_sessions_observed"],
                r["browser_requests_observed"],
                r["api_requests_observed"],
            ),
        )
        self.assertNotIn(token, json.dumps(r))

    def test_zero_unknown_and_partial_are_different(self):
        a = inv.Activity(SINCE)
        a.segment((NOW + ' {"event":"edu_demo_started","v":1}\n').encode())
        a.segment(chat(NOW, "validation"))
        self.assertEqual("partial", a.result()["coverage"])
        self.assertEqual(0, a.result()["api_requests_observed"])
        b = inv.Activity(SINCE)
        b.segment(b"")
        self.assertIsNone(b.result()["browser_sessions_observed"])
        b.segment(chat(NOW, "browser", "not-a-token"))
        self.assertEqual(
            ("partial", 0, 1),
            (
                b.result()["coverage"],
                b.result()["browser_sessions_observed"],
                b.result()["browser_requests_observed"],
            ),
        )

    def test_pool_bootstrap_without_workload_counts(self):
        ns = "pool"
        c = FakeCluster()
        c.rows["/api/v1/namespaces"] = [obj(ns, labels={"edu-ai-demo/purpose": "rhel-vms"})]
        c.rows[inv.ns_path(ns, "configmaps")] = [
            obj(
                "rhel-a-bootstrap",
                labels={inv.VM: "rhel-a", inv.MANAGER: inv.OWNER},
                notes={inv.CUSTOMER: "A"},
            )
        ]
        d = inv.collect(c, SINCE)[0]
        self.assertEqual(0, d["dedicated_usage"]["running_pods"])
        self.assertIsNone(d["activity"]["api_requests_observed"])
        self.assertEqual("configmaps", d["pool_objects"][0]["kind"])

    def test_pool_datavolume_without_vm_marker_or_claim_is_reported(self):
        c = FakeCluster()
        c.rows["/api/v1/namespaces"] = [obj("pool", labels={"edu-ai-demo/purpose": "rhel-vms"})]
        c.rows[inv.ns_path("pool", "datavolumes", "/apis/cdi.kubevirt.io/v1beta1")] = [
            obj(
                "rhel-school-root",
                labels={inv.VM: "rhel-school", inv.MANAGER: inv.OWNER},
                notes={inv.CUSTOMER: "School"},
            )
        ]
        d = inv.collect(c, SINCE)[0]
        self.assertEqual("vm_pool_only", d["ownership"])
        self.assertEqual(["datavolumes"], [item["kind"] for item in d["pool_objects"]])
        self.assertEqual(0, d["dedicated_usage"]["bound_pvc_capacity_bytes"])

    def test_bytes_cap_makes_an_observed_zero_a_lower_bound(self):
        raw = (NOW + ' {"event":"edu_demo_started","v":1}\n').encode()
        a = inv.Activity(SINCE)
        with patch.object(inv, "LOG_LIMIT", len(raw)):
            a.segment(raw)
        self.assertEqual("partial", a.result()["coverage"])
        self.assertEqual(0, a.result()["api_requests_observed"])


if __name__ == "__main__":
    unittest.main()
