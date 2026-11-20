import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "vm"))
import manage_vm as vm
import transport


class SizingTest(unittest.TestCase):
    def setUp(self):
        self.guest = vm.Guest("pool", "rhel-example-aabbcc", "Example")

    def test_configured_limits_match_guest_and_portable_defaults(self):
        for environment, cores, request in (({},4,"1500m"),
            ({"EDU_VM_CPU_CORES":"2", "EDU_VM_CPU_REQUEST":"500m"},2,"500m")):
            with self.subTest(cores=cores), patch.dict(os.environ,environment,clear=True):
                config, _, _, machine = self.guest.objects()
                domain = machine["spec"]["template"]["spec"]["domain"]
                self.assertEqual(cores,domain["cpu"]["cores"])
                self.assertEqual(str(cores),domain["resources"]["limits"]["cpu"])
                self.assertEqual(request,domain["resources"]["requests"]["cpu"])
                self.assertIn(f"INFERENCE_THREADS={cores}\n",config["data"]["inference.env"])
                self.assertIn('OMP_NUM_THREADS="$INFERENCE_THREADS"',config["data"]["guest.sh"])

    def test_invalid_request_is_rejected_before_vm_mutation(self):
        for request in ("0m","3000m","bad"):
            with self.subTest(request=request), patch.dict(os.environ,{"EDU_VM_CPU_CORES":"2","EDU_VM_CPU_REQUEST":request}):
                with self.assertRaises(ValueError):
                    self.guest.objects()

    def test_first_cpu_upgrade_reuses_verified_disk_without_archive(self):
        guest = self.guest
        owner = {"controller":True,"uid":"vm-id"}
        original = {"metadata":{**guest.meta(),"uid":"vm-id"}}
        volume = {"metadata":{**guest.meta(),"uid":"dv-id","ownerReferences":[owner]}}
        claim = {"metadata":{"uid":"persistent-disk","ownerReferences":[{"controller":True,"uid":"dv-id"}]},"status":{"phase":"Bound"}}
        guest.get = lambda kind,*a: {"vm":original,"dv":volume,"pvc":claim}[kind]
        guest.base,guest.check = Mock(),Mock(return_value={"ready":True})
        with patch.object(transport,"ensure") as stage,patch.object(transport,"remove"):
            guest.deploy(original)
            guest.check.assert_called_once_with(wait=True,timeout=420)
            stage.assert_not_called()

    def test_cache_miss_can_stage_once_after_grace(self):
        g = self.guest
        g.existing_disk_uid = Mock(return_value="disk-id")
        g.get,g.base = Mock(),Mock()
        g.check = Mock(side_effect=[vm.ModelNotReady("not ready"),{"ready":True}])
        with patch.object(transport,"ensure") as stage,patch.object(transport,"remove"):
            g.deploy({"metadata":{}})
            self.assertEqual([call(wait=True,timeout=420), call(wait=True)],g.check.call_args_list)
            stage.assert_called_once()

    def test_cold_archive_uses_selected_worker(self):
        captured = {}
        def existing(ns, kind, name):
            if kind not in captured:
                return {}
            obj = captured[kind]
            if kind=="ImageStream":
                obj["status"]={"tags":[{"tag":"runtime","items":[{"image":"sha256:"+"a"*64}]}]}
            if kind=="Pod":
                obj["status"]={"phase":"Running","conditions":[{"type":"Ready","status":"True"}]}
            return obj
        def fake_oc(args, body=None, **kwargs):
            if body and body.get("kind"):
                captured[body["kind"]] = body
            return Mock(returncode=0)
        with patch.dict(os.environ,{"EDU_VM_NODE":"worker-four"}),patch.object(transport,"get",side_effect=existing),patch.object(transport,"oc",side_effect=fake_oc):
            transport.ensure(self.guest.name,self.guest.ns,self.guest.labels,self.guest.annotations,vm.IMAGE)
        self.assertEqual({"kubernetes.io/hostname":"worker-four"},captured["Pod"]["spec"]["nodeSelector"])


if __name__=="__main__":
    unittest.main()
