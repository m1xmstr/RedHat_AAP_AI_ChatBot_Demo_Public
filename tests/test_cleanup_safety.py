"""Run the real cleanup entry point against an in-memory cluster; never invoke oc."""
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cluster = load_module("cluster_demo", "roles/edu_ai_demo_builder/files/cluster_demo.py")
workspaces = load_module("workspace", "roles/edu_ai_demo_builder/files/workspace.py")
survey = load_module("refresh_cleanup_survey", "aap/refresh_cleanup_survey.py")


class FakeCluster:
    """Models the Kubernetes objects read and mutated by the cleanup CLI."""
    def __init__(self):
        self.namespaces = {}
        self.resources = {}
        self.delete_requests = []

    def add_customer(self, namespace, customer, owner=True):
        self.namespaces[namespace] = {
            "metadata": {
                "name": namespace, "uid": "uid-" + namespace,
                "labels": dict(cluster.OWNER) if owner else {},
                "annotations": {cluster.CUSTOMER: customer, cluster.RUNTIME: "openshift_ai"},
            },
        }
        self.resources[namespace] = {}

    def oc(self, *args, obj=None, check=True):
        args = list(args)
        namespace = args[1] if args[0] == "-n" else None
        if namespace:
            args = args[2:]
        contents = ""
        error = ""
        objects = self.resources.get(namespace, {})
        if args[0] == "api-resources":
            contents = "persistentvolumeclaims\nstatefulsets.apps\n"
        elif args[:2] == ["get", "namespace"]:
            item = self.namespaces.get(args[2])
            contents = json.dumps(item) if item else ""
        elif args[:2] == ["get", "configmap"]:
            item = objects.get(("ConfigMap", args[2]))
            contents = json.dumps(item) if item else ""
        elif args[:2] == ["get", "persistentvolumeclaims"]:
            contents = "\n".join(name for kind, name in objects if kind == "PersistentVolumeClaim")
        elif args[:2] == ["get", "statefulsets.apps"]:
            contents = ""
        elif args[0] == "get" and "," in args[1]:
            contents = json.dumps({"items": [obj for (kind, _), obj in objects.items()
                                              if kind in ("ConfigMap","Deployment","ReplicaSet","Pod","Service","Route")]})
        elif args[0] == "create":
            ns, name = obj["metadata"]["namespace"], obj["metadata"]["name"]
            key = (obj["kind"], name)
            if key in self.resources[ns]:
                error = "AlreadyExists"
            else:
                self.resources[ns][key] = obj
        elif args[:2] == ["delete", "configmap"]:
            objects.pop(("ConfigMap", args[2]), None)
        elif args[0] == "delete" and (args[1] == "namespace" or "--raw" in args or args[1].startswith("--raw=")):
            if args[1] == "namespace":
                ns = args[2]
            else:
                raw_path = args[args.index("--raw")+1] if "--raw" in args else args[1].split("=", 1)[1]
                ns = raw_path.rsplit("/", 1)[1]
                if obj["preconditions"]["uid"] != self.namespaces[ns]["metadata"]["uid"]:
                    raise RuntimeError("UID conflict")
            self.delete_requests.append(ns)
            self.resources.pop(ns)
            self.namespaces.pop(ns)
        else:
            raise AssertionError(("Unexpected command", args))
        if error and check:
            raise RuntimeError(error)
        return subprocess.CompletedProcess(args, int(bool(error)), stdout=contents, stderr=error)


class CleanupSafetyTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeCluster()
        self.api.add_customer("alpha", "Alpha University")
        self.api.add_customer("bravo", "Bravo University")

    def cleanup(self, customer="Alpha University"):
        args = ["cluster_demo.py","cleanup","--namespace","alpha","--customer",customer,
                "--workspace","/unused","--runtime","openshift_ai"]
        with patch.object(sys, "argv", args), patch.object(cluster, "oc", self.api.oc), contextlib.redirect_stdout(io.StringIO()):
            cluster.main()

    def test_cleanup_after_partial_deployment_and_safe_repeat(self):
        self.api.resources["alpha"][("ConfigMap", "data")] = {
            "kind":"ConfigMap", "metadata":{"name":"data","labels":cluster.OWNER},
        }
        self.cleanup()
        self.cleanup()
        self.assertListEqual(self.api.delete_requests, ["alpha"])
        self.assertEqual(set(self.api.namespaces), {"bravo"})

    def test_foreign_namespace_and_wrong_customer_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "owner or customer"):
            self.cleanup("Bravo University")
        self.api.namespaces["alpha"]["metadata"]["labels"] = {}
        with self.assertRaisesRegex(ValueError, "owner or customer"):
            self.cleanup()
        self.assertEqual(self.api.delete_requests, [])

    def test_unexpected_storage_or_foreign_workload_blocks_deletion(self):
        for kind, name in (("PersistentVolumeClaim","foreign-data"),("Deployment","foreign-app")):
            with self.subTest(kind=kind):
                self.api.resources["alpha"][(kind, name)] = {
                    "kind":kind, "metadata":{"name":name},
                }
                with self.assertRaises(ValueError):
                    self.cleanup()
                self.assertNotIn(("ConfigMap", "edu-ai-operation"), self.api.resources["alpha"])
                self.assertIn((kind, name), self.api.resources["alpha"])
                del self.api.resources["alpha"][(kind, name)]
        self.assertEqual(self.api.delete_requests, [])

    def test_stale_or_active_lock_is_preserved(self):
        lock = {"kind":"ConfigMap", "metadata":{"name":"edu-ai-operation","labels":cluster.OWNER},
                "data":{"token":"previous-job","action":"deploy","job":"999"}}
        self.api.resources["alpha"][("ConfigMap", "edu-ai-operation")] = lock
        with self.assertRaisesRegex(ValueError, "999"):
            self.cleanup()
        self.assertEqual(self.api.resources["alpha"][("ConfigMap","edu-ai-operation")],lock)
        self.assertEqual(self.api.delete_requests, [])

    def test_resume_waits_for_namespace_already_being_deleted(self):
        self.api.namespaces["alpha"]["metadata"]["deletionTimestamp"] = "2026-03-12T00:00:00Z"
        def finish(_seconds):
            self.api.namespaces.pop("alpha")
        with patch.object(cluster.time, "sleep", side_effect=finish):
            self.cleanup()
        self.assertEqual(self.api.delete_requests, [])
        self.assertIn("bravo", self.api.namespaces)

    def test_replacement_namespace_is_protected_at_delete_time(self):
        issue = self.api.oc
        def replace_before_delete(*args, **kwargs):
            if args[:2] == ("delete", "--raw"):
                self.api.add_customer("alpha", "Replacement University")
                self.api.namespaces["alpha"]["metadata"]["uid"] = "replacement-uid"
            return issue(*args, **kwargs)

        with patch.object(self.api, "oc", side_effect=replace_before_delete):
            with self.assertRaisesRegex(RuntimeError, "UID conflict"):
                self.cleanup()
        self.assertEqual(self.api.delete_requests, [])
        self.assertEqual(self.api.namespaces["alpha"]["metadata"]["uid"], "replacement-uid")

    def test_survey_lists_only_managed_customers(self):
        self.api.add_customer("legacy", "Legacy University", owner=False)

        def list_namespaces(argv, **kwargs):
            self.assertIn("app.kubernetes.io/managed-by=edu-ai-demo-builder", argv)
            return json.dumps({"items":[i for i in self.api.namespaces.values()
                                        if i["metadata"]["labels"] == cluster.OWNER]}).encode()

        with patch.object(survey.subprocess, "check_output", side_effect=list_namespaces):
            self.assertEqual(survey.cleanup_survey()["spec"][0]["choices"],"Alpha University\nBravo University")
            self.cleanup()
            self.assertEqual(survey.cleanup_survey()["spec"][0]["choices"],"Bravo University")

    def test_vm_orphan_is_listed_only_for_a_matching_owner_marker(self):
        company = "Gamma University"
        vm = "rhel-gamma-university-" + hashlib.sha1(company.encode()).hexdigest()[:6]
        marker = {"metadata":{"name":vm+"-bootstrap", "labels":{"edu-ai-demo/vm":vm},
                              "annotations":{"edu-ai-demo/customer-name":company}}}
        pool = {"metadata":{"labels":{"edu-ai-demo/purpose":"rhel-vms"}}}
        responses = [{"items":list(self.api.namespaces.values())}, pool, {"items":[marker]}]
        with patch.object(survey, "oc_json", side_effect=responses) as get:
            choices = survey.cleanup_survey("test-vm-pool")["spec"][0]["choices"]
            self.assertEqual(choices, "Alpha University\nBravo University\nGamma University")
            self.assertIn(survey.OWNER+",edu-ai-demo/vm", get.call_args_list[-1].args)
        marker["metadata"]["annotations"]["edu-ai-demo/customer-name"] = "Different University"
        with patch.object(survey, "oc_json", side_effect=responses):
            with self.assertRaisesRegex(ValueError, "identity mismatch"):
                survey.cleanup_survey("test-vm-pool")

    def test_workspace_deletes_only_the_selected_customer_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for slug, name in (("alpha","Alpha University"),("bravo","Bravo University")):
                workspaces.owned_directory(root / slug, root, name)
                (root / slug / "corpus.jsonl").write_text("example\n")
            with self.assertRaises(ValueError):
                workspaces.owned_directory(root / "alpha",root,"Bravo University",remove=True)
            self.assertTrue(workspaces.owned_directory(root / "alpha", root, "Alpha University", remove=True))
            self.assertFalse(workspaces.owned_directory(root / "alpha", root, "Alpha University", remove=True))
            self.assertTrue((root / "bravo" / "corpus.jsonl").is_file())


if __name__ == "__main__":
    unittest.main()
