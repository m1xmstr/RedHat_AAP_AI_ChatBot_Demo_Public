"""Guest disk garbage collection, namespace ownership and supported upgrades."""
import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


def load(module_name, path):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


vm = load("vm_lifecycle", "vm/manage_vm.py")
cluster = load("cluster_lifecycle", "roles/edu_ai_demo_builder/files/cluster_demo.py")


class GuestCleanupTest(unittest.TestCase):
    def setUp(self):
        self.guest = vm.Guest("pool", "rhel-test-123456", "Example School")
        self.disk = self.guest.name + "-root"
        self.marker = self.guest.name + "-bootstrap"
        self.objects = {}
        self.make("cm", self.marker, "ConfigMap")
        self.make("vm", self.guest.name, "VirtualMachine")
        self.make("dv", self.disk, "DataVolume", owner=("VirtualMachine",self.guest.name,"vm"))
        self.make("pvc", self.disk, "PersistentVolumeClaim", owner=("DataVolume",self.disk,"dv"))
        self.objects[("pvc",self.disk)]["metadata"]["labels"][vm.BY] = "cdi-controller"
        self.guest.get = lambda kind, name=None: self.objects.get((kind, name or self.guest.name), {})
        self.guest.remove = Mock(side_effect=lambda kind, api, name=None: self.objects.pop((kind, name or self.guest.name), None))
        self.guest.wait_absent = Mock()
        mock_transport = patch.dict(sys.modules, transport=Mock())
        mock_transport.start()
        self.addCleanup(mock_transport.stop)

    def make(self, typ, name, kind, owner=None):
        meta = self.guest.meta(name)
        meta["labels"] = dict(meta["labels"])
        meta["uid"] = typ
        if owner:
            meta["ownerReferences"] = [{"controller":True,"kind":owner[0],"name":owner[1],"uid":owner[2]}]
        self.objects[(typ,name)] = {"metadata":meta, "kind":kind}

    def test_gc_retry_after_vm_disappears(self):
        del self.objects[("vm",self.guest.name)]
        self.guest.clean()
        self.guest.wait_absent.assert_any_call("dv", self.disk, "dv")
        self.guest.wait_absent.assert_any_call("pvc", self.disk, "pvc")
        self.assertNotIn(("cm", self.marker), self.objects)
        self.assertFalse(any(call.args[0] in ("dv","pvc") for call in self.guest.remove.call_args_list))

    def test_pvc_only_with_matching_marker_waits_for_cdi(self):
        del self.objects[("vm",self.guest.name)]
        del self.objects[("dv",self.disk)]
        self.guest.clean()
        self.guest.wait_absent.assert_any_call("pvc", self.disk, "pvc")
        self.assertNotIn(("cm",self.marker), self.objects)

    def test_foreign_residual_and_inconsistent_parent_are_protected(self):
        self.objects[("dv",self.disk)]["metadata"]["ownerReferences"][0]["uid"] = "another-vm"
        with self.assertRaisesRegex(ValueError, "unexpected VM"):
            self.guest.clean()
        del self.objects[("vm",self.guest.name)]
        del self.objects[("dv",self.disk)]
        self.objects[("pvc",self.disk)]["metadata"]["annotations"][vm.CUSTOMER] = "Other School"
        with self.assertRaisesRegex(ValueError, "residual"):
            self.guest.clean()
        self.guest.remove.assert_not_called()

    def test_timed_out_disk_preserves_marker_for_retry(self):
        self.guest.wait_absent.side_effect = RuntimeError("GC still running")
        with self.assertRaises(RuntimeError):
            self.guest.clean()
        self.assertIn(("cm", self.marker), self.objects)

    def test_switch_to_dual_mode_preserves_existing_platform(self):
        args = SimpleNamespace(namespace="app", customer="Example School", action="deploy", runtime="openshift_ai_and_rhel_vm")
        for source in ("openshift_ai","rhel_vm"):
            namespace = {"metadata":{"name":"app","uid":"same","labels":cluster.OWNER,"annotations":{
                cluster.CUSTOMER:args.customer,cluster.RUNTIME:source}}}
            with patch.object(cluster,"get",return_value=namespace), patch.object(cluster,"oc") as write:
                self.assertEqual(cluster.owned_namespace(args, create=True),namespace)
                write.assert_not_called()
                args.runtime = "rhel_ai"
                with self.assertRaisesRegex(ValueError,"another runtime"):
                    cluster.owned_namespace(args, create=True)
                args.runtime = "openshift_ai_and_rhel_vm"


if __name__ == "__main__":
    unittest.main()
