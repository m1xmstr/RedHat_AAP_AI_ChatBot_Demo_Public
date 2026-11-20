# Configure AAP for customer demos

Use an administrator workstation with Python 3.10+, Ansible Core 2.16+, `oc`, an authenticated cluster context, and HTTPS trust for the automation gateway. The job execution environment needs Python 3.10+, `oc`, Ansible and network access to the customer’s public website, cluster API and demo routes.

## 1. Prepare a local operator inventory

```bash
mkdir -p operator-inventory/group_vars
cp inventories/demo/hosts.yml operator-inventory/hosts.yml
cp inventories/demo/group_vars/all.yml.example operator-inventory/group_vars/all.yml
```

Edit `operator-inventory/group_vars/all.yml`. Set your automation gateway URL, OpenShift API URL, organization, credential names and desired default runtime. The included SCM URL points to this public repository. Keep site-specific configuration in the ignored operator inventory.

The sample requests `demo_runtime: "openshift_ai"`. Set this explicitly to make the shared model the default when its endpoint is configured. Ordinary launches create an OpenShift frontend without a VM. When no model or VM is configured, the survey offers `openshift` for on-cluster retrieval. To enable the shared model, set `demo_openshift_ai_endpoint` to the full OpenAI-compatible **chat completions** endpoint reachable from the app, and set `demo_openshift_ai_model` to its served model identifier. Set the analogous `demo_rhel_ai_endpoint` and `demo_rhel_ai_model` for an existing RHEL AI appliance. Optional `demo_openshift_ai_secret` and `demo_rhel_ai_secret` reference a Kubernetes Secret with key `api-key` in the customer app namespace. Provision it before model authentication is needed and let the customer namespace lifecycle govern that copy. Keep model credentials out of surveys.

## 2. Grant cluster access

Inspect [the runner RBAC](../openshift/edu-ai-demo-runner-rbac.yml). The demo runner manages customer namespaces, Deployments, ConfigMaps, Services and Routes. Its cluster role includes namespace deletion and reads used to guard cleanup. Use a cluster approved for customer demos. The supplied manifests assume a ServiceAccount in a namespace named `aap`. If your AAP runs elsewhere, change the ServiceAccount namespace and the subjects in both RBAC manifests before applying them.

```bash
oc apply -f openshift/edu-ai-demo-runner-rbac.yml
```

In AAP, create a custom credential type and credential matching `aap_openshift_credential_name`. Use the following input fields:

```yaml
fields:
  - {id: api_url, type: string, label: OpenShift API URL}
  - {id: token, type: string, label: Service account token, secret: true}
  - {id: ca_bundle, type: string, label: PEM trust bundle, multiline: true}
required: [api_url, token, ca_bundle]
```

Use these injectors:

```yaml
env:
  OCP_API_URL: "{{ api_url }}"
  OCP_API_TOKEN: "{{ token }}"
  K8S_AUTH_SSL_CA_CERT: "{{ tower.filename }}"
file:
  template: "{{ ca_bundle }}"
```

Supply the API server URL, a token for the demo runner, and the CA chain for your cluster API, demo routes and automation gateway. Manage the token’s expiration in AAP. The setup playbook verifies the gateway’s TLS certificate and uses `aap_ca_path` when supplied.

## 3. Enable the optional RHEL CPU VM

With OpenShift Virtualization and CDI available, apply the [VM pool manifest](../openshift/edu-ai-demo-rhel-vm-rbac.yml) once. Confirm the runner can clone your RHEL 9 boot source in its source namespace, and that the cluster’s integrated registry can import the Red Hat inference image with its configured entitlement. The guest requires outbound HTTPS access to the public UBI package repository and Hugging Face for the pinned model files.

```bash
oc apply -f openshift/edu-ai-demo-rhel-vm-rbac.yml
```

Set `demo_rhel_vm_enabled: true`, the VM pool namespace, `demo_rhel_vm_source_namespace`, `demo_rhel_vm_source_name`, and `demo_rhel_vm_storage_class`. Use a virtualization-compatible block storage class with **Delete** reclaim policy and adequate capacity for the 48 GiB disk. Use an x86-64, AVX2-capable worker with room for the configured CPU allocation, 16-GiB guest, and concurrent image staging. The portable example sets `demo_rhel_vm_cpu_cores: 4` and `demo_rhel_vm_cpu_request: "1500m"`. For a smaller comparison guest, set 2 cores and a `500m` request, then account for staging and cluster failover capacity before enabling it. Leave `demo_rhel_vm_node` empty for ordinary scheduling or specify a worker’s `kubernetes.io/hostname` label. Review [runtime and storage](../docs/ARCHITECTURE.md) before enabling the paired mode.

## 4. Synchronize the project and install the templates

Log in to the target cluster with `oc`. The setup compares `oc whoami --show-server` with your configured `openshift_api_url`. Supply the AAP administrator password through the process environment. For example, in Bash:

```bash
read -rsp 'AAP administrator password: ' AAP_PASSWORD
echo
export AAP_PASSWORD
ansible-playbook -i operator-inventory/hosts.yml aap/setup_EDU_AI_DEMO_AAP.yml
unset AAP_PASSWORD
```

The installer synchronizes the Git project, installs the inventory and three templates, and stores the dedicated survey updater’s generated password in an encrypted AAP credential. The updater has JobTemplate Admin on the cleanup template. Deploy and cleanup jobs use it to refresh the school dropdown from the cluster. Run setup again after changing endpoint settings, VM options, project assets or the SCM branch. See the [presentation runbook](../docs/DEMO_RUNBOOK.md) for launch and validation steps.
