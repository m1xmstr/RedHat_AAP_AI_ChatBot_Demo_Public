#!/usr/bin/env python3
"""Provision and inspect one isolated RHEL inference VM in an operator-managed pool."""
import argparse
from decimal import Decimal
import hashlib
import http.client
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODEL = "Qwen/Qwen2.5-0.5B-Instruct"
IMAGE = "registry.redhat.io/rhaii/vllm-cpu-rhel9@sha256:578e9946e60a14596185937f6a083c1d42f3cff343b18648090b9ac80691d2e0"
STORAGE_CLASS = os.environ.get("EDU_VM_STORAGE_CLASS", "ocs-storagecluster-ceph-rbd-virtualization")
SOURCE = os.environ.get("EDU_VM_SOURCE_NAMESPACE", "openshift-virtualization-os-images")
SOURCE_NAME = os.environ.get("EDU_VM_SOURCE_NAME", "rhel9")
BY = "app.kubernetes.io/managed-by"
CUSTOMER = "edu-ai-demo/customer-name"
REV = "edu-ai-demo/revision"


def oc(*args, obj=None, check=True):
    p = subprocess.run(["oc", "--request-timeout=45s", *args], text=True, capture_output=True,
                       input=json.dumps(obj) if obj is not None else None, timeout=60)
    if check and p.returncode:
        raise RuntimeError(p.stderr.strip() or p.stdout.strip())
    return p


def api_json(method, path, body=None):
    # oc carries the job's existing TLS and Kubernetes credentials. A private
    # local socket allows JSON content type without exposing the API on a TCP port.
    with tempfile.TemporaryDirectory(prefix="edu-vm-") as directory:
        endpoint = str(Path(directory) / "api.sock")
        with subprocess.Popen(["oc", "proxy", "--unix-socket", endpoint],
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True) as proxy:
            conn = http.client.HTTPConnection("localhost", timeout=90)
            try:
                deadline = time.monotonic() + 10
                while not Path(endpoint).exists():
                    if proxy.poll() is not None:
                        raise RuntimeError("Could not open Kubernetes API proxy: " + proxy.stderr.read().strip())
                    if time.monotonic() > deadline:
                        raise RuntimeError("Timed out opening Kubernetes API proxy")
                    time.sleep(0.05)
                conn.sock = socket.socket(socket.AF_UNIX)
                conn.sock.settimeout(90)
                conn.sock.connect(endpoint)
                conn.request(method, path, body=json.dumps(body) if body is not None else None,
                             headers={"Content-Type": "application/json"})
                response = conn.getresponse()
                data = response.read()
                if response.status != 200:
                    raise RuntimeError("Guest inference API returned HTTP %d: %s" % (response.status, data[:350].decode(errors="replace")))
                return json.loads(data)
            finally:
                conn.close()
                proxy.terminate()


class ModelNotReady(RuntimeError):
    pass


def cpu_configuration():
    cores = int(os.environ.get("EDU_VM_CPU_CORES", "4"))
    request = os.environ.get("EDU_VM_CPU_REQUEST", "1500m").strip()
    if not re.fullmatch(r"(?:[0-9]+m|[0-9]+(?:\.[0-9]{1,3})?)", request):
        raise ValueError("Specify the guest CPU request as cores or millicores")
    amount = Decimal(request[:-1]) / 1000 if request.endswith("m") else Decimal(request)
    if cores < 1 or not 0 < amount <= cores:
        raise ValueError("Guest CPU request must be positive and no larger than its vCPU count")
    return cores, request


class Guest:
    def __init__(self, ns, name, customer):
        self.ns, self.name, self.customer = ns, name, customer
        self.labels = {BY: "edu-ai-demo-builder", "edu-ai-demo/vm": name}
        self.annotations = {CUSTOMER: customer}

    def get(self, typ, name=None):
        out = oc("-n", self.ns, "get", typ, name or self.name, "--ignore-not-found", "-o", "json").stdout
        return json.loads(out) if out.strip() else {}

    def assert_owner(self, obj):
        md = obj["metadata"]
        if (any(md.get("labels", {}).get(k) != v for k, v in self.labels.items()) or
                md.get("annotations", {}).get(CUSTOMER) != self.customer):
            raise ValueError("Refusing unowned %s/%s" % (obj.get("kind"), md.get("name")))

    def meta(self, name=None):
        return {"name": name or self.name, "namespace": self.ns, "labels": self.labels, "annotations": self.annotations.copy()}

    def ensure(self, typ, obj):
        md = obj["metadata"]
        revision = hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()
        md["annotations"][REV] = revision
        old = self.get(typ, md["name"])
        if old:
            self.assert_owner(old)
            if old["metadata"]["annotations"].get(REV) == revision:
                return
            if typ == "vm":
                oc("-n", self.ns, "patch", "vm", md["name"], "--type=merge", "-p", '{"spec":{"runStrategy":"Halted"}}')
                self.wait_absent("vmi", md["name"])
            oc("apply", "-f", "-", obj=obj)
        else:
            oc("create", "-f", "-", obj=obj)

    def wait_absent(self, typ, name, uid=None):
        for _ in range(90):
            obj = self.get(typ, name)
            if not obj or (uid and obj["metadata"]["uid"] != uid):
                return
            time.sleep(3)
        raise RuntimeError("Timed out waiting for deletion of %s/%s" % (typ, name))

    def remove(self, typ, api_path, name=None):
        name = name or self.name
        old = self.get(typ, name)
        if not old:
            return
        self.assert_owner(old)
        uid = old["metadata"]["uid"]
        if not old["metadata"].get("deletionTimestamp"):
            path = f"{api_path}/namespaces/{self.ns}/" + {"vm":"virtualmachines","svc":"services","networkpolicy":"networkpolicies","cm":"configmaps"}[typ] + "/" + name
            oc("delete", "--raw", path, "-f", "-", obj={"kind":"DeleteOptions","apiVersion":"v1","preconditions":{"uid":uid},"propagationPolicy":"Foreground"})
        self.wait_absent(typ, name, uid)

    def objects(self):
        script = (HERE / "guest-bootstrap.sh").read_text()
        cores, request = cpu_configuration()
        env = (f"IMAGE_URL=http://{self.name}-image.{self.ns}.svc.cluster.local:8080/image.tar\n"
               f"INFERENCE_THREADS={cores}\n")
        unit = """[Unit]
Description=Customer Red Hat AI Inference
Wants=network-online.target
After=network-online.target
StartLimitIntervalSec=0
[Service]
ExecStart=/usr/local/libexec/edu-inference
Restart=always
RestartSec=30
TimeoutStopSec=90
StandardOutput=journal+console
StandardError=journal+console
[Install]
WantedBy=multi-user.target
"""
        bootstrap = {"apiVersion":"v1","kind":"ConfigMap","metadata":self.meta(self.name + "-bootstrap"),
                     "data":{"guest.sh":script,"inference.env":env,"inference.service":unit}}
        cloud = """#cloud-config
ssh_pwauth: false
disable_root: true
bootcmd:
  - |
    set -ex
    mkdir -p /run/educonfig /usr/local/libexec
    mount -L EDUCFG /run/educonfig
    install -m 755 /run/educonfig/guest.sh /usr/local/libexec/edu-inference
    install -m 644 /run/educonfig/inference.env /etc/edu-inference.env
    install -m 644 /run/educonfig/inference.service /etc/systemd/system/edu-inference.service
    umount /run/educonfig
    systemctl daemon-reload
    systemctl enable edu-inference.service
runcmd:
  - [systemctl, start, --no-block, edu-inference.service]
"""
        rootdisk = self.name + "-root"
        template_md = {"labels":{**self.labels, "kubevirt.io/domain":self.name}, "annotations":self.annotations.copy()}
        vm = {"apiVersion":"kubevirt.io/v1","kind":"VirtualMachine","metadata":self.meta(),
              "spec":{"runStrategy":"Always","dataVolumeTemplates":[{"metadata":self.meta(rootdisk),
                    "spec":{"sourceRef":{"kind":"DataSource","name":SOURCE_NAME,"namespace":SOURCE},
                            "storage":{"storageClassName":STORAGE_CLASS,"volumeMode":"Block","accessModes":["ReadWriteOnce"],
                                       "resources":{"requests":{"storage":"48Gi"}}}}}],
                "template":{"metadata":template_md,"spec":{"evictionStrategy":"LiveMigrateIfPossible",
                    "domain":{"cpu":{"cores":cores,"sockets":1,"threads":1,"model":"host-model","features":[{"name":"avx2","policy":"require"}]},
                              "memory":{"guest":"16Gi"}, "resources":{"requests":{"memory":"16Gi","cpu":request},"limits":{"cpu":str(cores)}},
                              "devices":{"logSerialConsole":True,"disks":[{"name":"rootdisk","disk":{"bus":"virtio"},"bootOrder":1},
                                {"name":"cloudinit","disk":{"bus":"virtio"}},{"name":"bootstrap","disk":{"bus":"virtio"}}],
                                "interfaces":[{"name":"default","masquerade":{},"model":"virtio"}]}},
                    "networks":[{"name":"default","pod":{}}],
                    "volumes":[{"name":"rootdisk","dataVolume":{"name":rootdisk}},
                               {"name":"cloudinit","cloudInitNoCloud":{"userData":cloud}},
                               {"name":"bootstrap","configMap":{"name":self.name+"-bootstrap","volumeLabel":"EDUCFG"}}]
                }}}}
        node = os.environ.get("EDU_VM_NODE", "").strip()
        if node:
            vm["spec"]["template"]["spec"]["nodeSelector"] = {"kubernetes.io/hostname": node}
        # Change the template when the bootstrap contents change, even if disk names stay the same.
        template_md["annotations"]["edu-ai-demo/bootstrap"] = hashlib.sha256(json.dumps(bootstrap["data"],sort_keys=True).encode()).hexdigest()
        service = {"apiVersion":"v1","kind":"Service","metadata":self.meta(),
                   "spec":{"selector":{"kubevirt.io/domain":self.name},"ports":[{"name":"inference","port":8000,"targetPort":8000}]}}
        frontend = os.environ.get("EDU_VM_FRONTEND_NAMESPACE") or ("edu-ai-demo-" + self.name[5:])
        net = {"apiVersion":"networking.k8s.io/v1","kind":"NetworkPolicy","metadata":self.meta(),
               "spec":{"podSelector":{"matchLabels":{"kubevirt.io/domain":self.name}},
                       "policyTypes":["Ingress"],"ingress":[{"ports":[{"protocol":"TCP","port":8000}],
                       "from":[{"namespaceSelector":{"matchLabels":{"kubernetes.io/metadata.name":frontend}},
                                "podSelector":{"matchLabels":{BY:"edu-ai-demo-builder"}}},
                               {"namespaceSelector":{"matchLabels":{"policy-group.network.openshift.io/host-network":""}}}]}]}}
        return bootstrap, net, service, vm

    def base(self):
        pool = json.loads(oc("get", "namespace", self.ns, "-o", "json").stdout)
        if pool["metadata"].get("labels", {}).get("edu-ai-demo/purpose") != "rhel-vms":
            raise ValueError("Choose the operator-managed RHEL VM pool namespace")
        sc = json.loads(oc("get","storageclass",STORAGE_CLASS,"-o","json").stdout)
        if sc.get("reclaimPolicy") != "Delete":
            raise ValueError("The VM storage class must use Delete reclaim policy")
        bootstrap, net, service, vm = self.objects()
        for typ, obj in (("cm",bootstrap),("networkpolicy",net),("svc",service),("vm",vm)):
            self.ensure(typ, obj)

    def check(self, wait=False, timeout=2700):
        deadline=time.monotonic() + (timeout if wait else 0)
        previous = ""
        while True:
            vmi=self.get("vmi")
            status=vmi.get("status", {})
            osinfo=status.get("guestOSInfo", {})
            prefix=f"/api/v1/namespaces/{self.ns}/services/http:{self.name}:inference/proxy/v1"
            if osinfo.get("id") == "rhel" and str(osinfo.get("versionId","")).startswith("9."):
                p=oc("get","--raw", prefix+"/models",check=False)
                if p.returncode and ("Forbidden" in p.stderr or "Unauthorized" in p.stderr):
                    raise ValueError("The VM operator credential requires access to the pool service proxy")
                if not p.returncode and MODEL in [x.get("id") for x in json.loads(p.stdout).get("data",[])]:
                    body = api_json("POST", prefix + "/chat/completions", {
                        "model": MODEL, "messages": [{"role": "user", "content": "What is two plus two? Answer with the number."}],
                        "temperature": 0, "max_tokens": 12,
                    })
                    answer=body["choices"][0]["message"]["content"]
                    if not answer.strip() or not re.search(r"\b(4|four)\b",answer,re.I):
                        raise ValueError("Guest model returned an unexpected verification response")
                    return {"os":osinfo["prettyName"],"model":MODEL,"vm":self.name,"namespace":self.ns,"node":status.get("nodeName"),
                            "endpoint":f"http://{self.name}.{self.ns}.svc.cluster.local:8000/v1/chat/completions","verified_answer":answer.strip()}
            state=status.get("phase","starting") + " " + osinfo.get("prettyName","")
            if previous != state:
                print("Guest status: "+state,file=sys.stderr,flush=True)
                previous=state
            if not wait or time.monotonic()>=deadline:
                raise ModelNotReady("Guest model did not become ready; inspect the VM events and guest-console-log.")
            time.sleep(10)

    def existing_disk_uid(self, original_vm):
        if not original_vm:
            return ""
        self.assert_owner(original_vm)
        disk = self.name + "-root"
        dv, pvc = self.get("dv", disk), self.get("pvc", disk)
        if not dv or not pvc or pvc.get("status", {}).get("phase") != "Bound":
            return ""
        self.assert_owner(dv)
        def owned_by(child, parent):
            return any(ref.get("controller") and ref.get("uid") == parent["metadata"]["uid"]
                       for ref in child["metadata"].get("ownerReferences", []))
        if not owned_by(dv, original_vm) or not owned_by(pvc, dv):
            raise ValueError("The existing boot disk does not belong to this VM")
        return pvc["metadata"]["uid"]

    def deploy(self, original_vm):
        import transport
        original_disk = self.existing_disk_uid(original_vm)
        self.base()
        # Older verified demos have no bootstrap marker. The persisted disk identity
        # and controller ownership chain also cover their first sizing upgrade.
        retained = original_disk and original_disk == self.existing_disk_uid(self.get("vm"))
        try:
            if retained:
                print("Waiting for the existing root disk's inference service before staging an image.", file=sys.stderr, flush=True)
                result = self.check(wait=True, timeout=420)
            else:
                result = self.check()
        except ModelNotReady:
            transport.ensure(self.name, self.ns, self.labels, self.annotations, IMAGE)
            result = self.check(wait=True)
        transport.remove(self.name, self.ns, self.labels, self.annotations)
        return result

    def clean(self):
        # The VM owns its DataVolume and the DataVolume owns its PVC. Never delete a disk by guessed name.
        disk=self.name+"-root"
        vm=self.get("vm")
        dv=self.get("dv",disk)
        pvc=self.get("pvc",disk)
        marker = self.get("cm", self.name + "-bootstrap")
        if marker:
            self.assert_owner(marker)
        if vm:
            self.assert_owner(vm)

        def controller_uid(resource, kind, name):
            owners = [ref["uid"] for ref in resource["metadata"].get("ownerReferences", [])
                      if ref.get("controller") and ref.get("kind") == kind
                      and ref.get("name") == name and ref.get("uid")]
            if len(owners) != 1:
                raise ValueError(f"Cannot verify {kind}/{name} as the disk controller")
            return owners[0]

        if dv:
            self.assert_owner(dv)
            parent_uid = controller_uid(dv, "VirtualMachine", self.name)
            if vm and parent_uid != vm["metadata"]["uid"]:
                raise ValueError("DataVolume has an unexpected VM owner")
            if not vm and not marker:
                raise ValueError("Missing customer marker for the residual DataVolume")
        if pvc:
            parent_uid = controller_uid(pvc, "DataVolume", disk)
            if dv and parent_uid != dv["metadata"]["uid"]:
                raise ValueError("PVC has an unexpected DataVolume owner")
            if not dv:
                # CDI owns the PVC's managed-by label. The customer annotation, VM
                # label and bootstrap marker identify a residual disk after its DV exits.
                md = pvc["metadata"]
                if (not marker or md.get("annotations", {}).get(CUSTOMER) != self.customer
                        or md.get("labels", {}).get("edu-ai-demo/vm") != self.name):
                    raise ValueError("Cannot verify ownership of the residual customer PVC")
        self.remove("vm", "/apis/kubevirt.io/v1")
        # Deletion is exclusively by the owning controllers. Never issue a DELETE
        # for a disk. On retry their original parents may already have disappeared.
        self.wait_absent("dv", disk, dv["metadata"]["uid"] if dv else None)
        self.wait_absent("pvc", disk, pvc["metadata"]["uid"] if pvc else None)
        self.remove("svc","/api/v1")
        self.remove("networkpolicy","/apis/networking.k8s.io/v1")
        import transport
        transport.remove(self.name,self.ns,self.labels,self.annotations)
        self.remove("cm","/api/v1",self.name+"-bootstrap")


def main():
    p=argparse.ArgumentParser()
    p.add_argument("action", choices=["deploy","check","cleanup"])
    for option in ("namespace","name","customer"):
        p.add_argument("--"+option,required=True)
    args=p.parse_args()
    if not re.fullmatch(r"rhel-[a-z0-9-]{1,40}",args.name):
        raise ValueError("Invalid customer VM name")
    guest=Guest(args.namespace,args.name,args.customer)
    if args.action=="cleanup":
        guest.clean()
        print(json.dumps({"removed":args.name}))
    else:
        vm=guest.get("vm")
        if vm:
            guest.assert_owner(vm)
        if args.action=="deploy":
            result = guest.deploy(vm)
        else:
            if not vm:
                raise RuntimeError("Customer VM does not exist")
            result = guest.check()
        print(json.dumps(result))


if __name__=="__main__":
    try:
        main()
    except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as exc:
        sys.exit(str(exc))
