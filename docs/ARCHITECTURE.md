# Runtime and storage

AAP collects public HTML, PDF and iCalendar sources in its execution environment using Python 3.10 or newer. The default crawl samples up to 64 pages. It follows same-site department links and sitemaps, respects robots rules and logs each indexed or skipped URL. Site-specific source profiles can add official calendars and selected handbook pages. The builder extracts page text, splits it into passages, records each passage's source, and writes a JSON Lines file. The chat application retrieves passages locally. An AI runtime sends the retrieved text and question to the configured model. For calendar and venue questions with structured records, the model selects record IDs. The application displays the selected source fields and links so a model cannot rewrite a registration date or invent a venue. The chat labels those responses “selected sources.” The provider returned by the chat API identifies which path served that answer. Verify a model-backed answer before presenting either AI platform.

| Runtime | Application | Inference |
| --- | --- | --- |
| `openshift_ai` | OpenShift Deployment, Service and HTTPS Route | Administrator-configured OpenShift AI model |
| `rhel_vm` | OpenShift frontend and an OpenShift Virtualization RHEL guest | Red Hat AI Inference CPU container running in the guest |
| `openshift_ai_and_rhel_vm` | Two HTTPS frontends with the same corpus | OpenShift AI and Red Hat AI Inference on RHEL |
| `rhel_ai` | Podman on the specified RHEL target, or a Route-backed app when configured | Administrator-configured RHEL AI model |
| `all` | Both configured AI providers | Separate model endpoints |
| `openshift` | OpenShift Deployment, Service and HTTPS Route | Local evidence retrieval |
| `podman` | Container on the RHEL target | Local evidence retrieval |
| `artifact` | Files in the builder workspace | Local evidence retrieval |

The application host and inference host have separate roles. Configure the full chat-completions URL and the exact served model ID on the administrator side. An external inference service has its own capacity, credentials and lifecycle. Customer jobs create the chat application and its corpus; a shared endpoint can serve multiple demos.

## OpenShift storage and lifecycle

One dedicated namespace holds the customer's application, Service, Routes and immutable ConfigMaps. The builder compresses the application and corpus, splits the bundle into 512 KiB parts, and mounts the parts in an init container. The init container verifies the digest and extracts the bundle to a pod-local `emptyDir`. ConfigMaps carry the bundle across pod replacement. Working storage limits and container resources are declared in `roles/edu_ai_demo_builder/templates/openshift/demo-app.yml.j2`. The application has 48 MiB of pod-local working storage. Its persisted bundle uses ConfigMaps and requires no PVC or external vector database.

The full customer name is recorded in a namespace annotation. A name hash distinguishes schools whose names normalize to the same slug. Job-owned environments carry a managed-by label. Cleanup checks the ownership and customer before deleting the dedicated namespace. Keep unrelated workloads and shared credentials outside customer namespaces. Shared models, the AAP installation, execution history, and copies a presenter downloads have separate lifecycles.

## Virtual machine storage and compute

The optional CPU path clones a RHEL 9 boot disk in OpenShift Virtualization. The default VM has four virtual CPUs, requests 1.5 CPUs from the scheduler, uses 16 GiB guest RAM and has a 48 GiB persistent root disk. Administrators can set `demo_rhel_vm_cpu_cores` to cap both the VM and its inference threads and `demo_rhel_vm_cpu_request` to reserve scheduling capacity. Budget each comparison guest against cluster failover capacity. The root disk holds the RHEL system, CPU inference image, model weights, and guest logs. The frontend corpus stays in the customer application namespace. The guest runs the digest-pinned Red Hat AI Inference CPU image and serves a pinned Qwen 2.5 0.5B Instruct model. The smaller model and CPU performance are part of this demo configuration.

The administrator provisions one VM pool. Each school owns individually named VM, boot disk, bootstrap, private image-transfer, network and Service resources in that pool. The transfer workload stages the vendor image on ephemeral storage. If a guest node is configured, the transfer pod runs there too. A sizing change restarts the guest on its existing disk and gives the cached inference service time to resume before staging an archive. Its service account provides access to the internal registry. The guest receives the image through the customer-specific service.

The installer needs an entitled image source and supported x86 CPU instructions. The pool manager needs permission to clone the configured RHEL boot source. Cleanup checks customer identity and the disk ownership chain, removes the VM and image-transfer resources, and waits for its DataVolume and PVC to disappear. The storage class must have Delete reclaim policy. The VM pool, storage class, original RHEL boot source and other schools have separate ownership. The current RWO boot disk requires a guest restart to move between nodes.

## Podman and local storage

The local artifact build prints the workspace containing its report, source passages, and runnable app. Set `EDU_AI_DEMO_OUTPUT` to use another workspace parent. Podman deployments additionally create a customer-specific directory, image and container on the specified target. Use the same target and customer identity for cleanup. AAP execution environments have job-local filesystems; use a deployed route to retrieve the corpus across jobs.
