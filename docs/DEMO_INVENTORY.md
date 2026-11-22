# Demo inventory and daily reports

Run the collector from a checkout with Python 3.9+ and an authenticated OpenShift CLI context. Inside a cluster, Python can use the pod's mounted ServiceAccount token and CA directly. Setting `KUBECONFIG` selects the CLI context.

```sh
python3 tools/demo_inventory.py
python3 tools/demo_inventory.py --format json
```

Text output fits a private operator digest. On a successful empty inventory, the text command writes zero bytes. JSON always includes `observed_at`, `activity_window_hours`, `demo_count` and `demos`. A successful empty inventory has a count of zero and an empty array. A failed ownership or workload listing produces a nonzero status and diagnostic on standard error. Missing optional metrics and activity appear as unknown.

### Scheduling delivery

The following wrapper works with a local cron service, an authenticated read-only `oc` context, and a site-supplied sender that reads the report from standard input. Configure the sender to deliver only to your approved authenticated operations audience. Reports contain customer names, route hostnames and workload placement. Keep the checkout, context and sender accessible to the scheduler account.

```sh
#!/bin/sh
set -eu
umask 077
: "${DEMO_CHECKOUT:?Path to the automation checkout}"
: "${DEMO_DIGEST_SENDER:?Absolute path to the approved sender}"
report=$(mktemp)
trap 'rm -f "$report"' EXIT
python3 "$DEMO_CHECKOUT/tools/demo_inventory.py" > "$report"
if [ -s "$report" ]; then
    "$DEMO_DIGEST_SENDER" < "$report"
fi
```

Save the wrapper as `/opt/demo/bin/daily-inventory` and make it executable. For example, this crontab invokes it at 08:00 in the scheduler's timezone. Set `PATH` to include the installed Python and OpenShift CLI paths. Leave scheduler error monitoring enabled so collection and delivery failures reach an operator.

```cron
PATH=/usr/local/bin:/usr/bin:/bin
KUBECONFIG=/etc/demo/report-reader.kubeconfig
DEMO_CHECKOUT=/opt/demo/automation
DEMO_DIGEST_SENDER=/opt/demo/bin/send-to-operations
0 8 * * * /opt/demo/bin/daily-inventory
```

### What the report measures

- **Customer ownership.** Managed namespaces carry the builder's `managed-by` label and exact customer-name annotation. Legacy customer namespaces require the original demo namespace prefix, app label and customer label together. The legacy customer value may be a slug. The collector also checks ConfigMaps, DataVolumes, VMs, VM instances, pods and claims in the pool marked `edu-ai-demo/purpose=rhel-vms`. A surviving owned boot disk or bootstrap marker can therefore appear alone after an interrupted deployment. Controller UIDs connect launcher pods to VM instances and VMs even when generated pods omit the builder label.
- **Dedicated usage.** Pod rows list phase, readiness and node. Per-demo totals use the resource metrics API's CPU and memory samples for scheduled active pods, including VM launchers. A missing sample makes the corresponding total unknown. CPU is in millicores (1,000m = one core). VM rows also show configured vCPUs and guest memory. PVC requests and bound capacity describe storage reserved. The collector does not measure filesystem contents, backend thin-provisioned use, or storage consumed by container images.
- **Inference.** A dedicated guest is accounted for through its launcher pod. OpenShift AI and external model endpoints are reported as shared or external. Their resource consumption cannot be allocated to a customer from frontend metrics. Provider information comes from deployment configuration. Endpoint addresses and credentials are excluded. Standalone Podman hosts and local artifact-only demos fall outside this OpenShift inventory.
- **Activity.** For instrumented chatbot containers, the last 24 hours of retained logs provide counts of browser chat requests, distinct anonymous browser session hashes and unclassified API chat requests. Validation events are excluded. Reloads in a tab can retain a session, and one person can have several sessions. The browser session count measures observed chat use. The report omits questions, answers, hashes, IP addresses and raw logs.
- **Coverage.** `retained` means that current app pods have bounded log coverage back to startup or the start of the window. `partial` means the reported counts are lower bounds. `unknown` means the collector lacks evidence to report a count, as with legacy pods. A confirmed zero requires instrumentation and coverage. Deleted pods, rotated-away logs and older restarts cannot be reconstructed. Each available current or immediately previous container log read is capped at 4 MiB. The inventory is a point-in-time sample collected over several API calls.

### Read permissions

The collector lists namespaces. Within matching customer namespaces it lists pods, deployments, routes and PVCs. PodMetrics list permission enables resource usage. Reading the chatbot's `pods/log` subresource enables activity. Within matching VM pools it also lists ConfigMaps, DataVolumes, VirtualMachines and VirtualMachineInstances. Bind these namespace permissions to an authorized scheduler identity in each applicable namespace, including newly provisioned demos. Use preconfigured delivery credentials in the site's sender.
