# RedHat_AAP_AI_ChatBot_Demo_Public
Use AAP to deploy an AI Chatbot

Enter a customer’s **name** and **public website** in Ansible Automation Platform. The deploy job crawls official sources, builds a downloadable JSONL corpus, deploys a branded chat application, and tests a real cited answer. Use the same corpus to present OpenShift AI and CPU inference on a RHEL virtual machine.

## Run a demo

After the platform administrator completes [AAP setup](aap/README.md), launch **EDU_AI_DEMO**.

1. Enter the customer name and website. To rehearse the included university example, enter **The University of North Carolina** and **unc.edu**.
2. With a configured shared OpenShift AI endpoint, the sample installation defaults to `openshift_ai`. This deploys a frontend and uses the shared model. Select `openshift_ai_and_rhel_vm` when you want to provision a second model server in a dedicated RHEL guest and compare both routes. Optional fields control crawl depth, starting URLs and the logo.
3. Open the HTTPS route printed by the job. Open **Sources**, inspect the passages and download the RAG file. Ask a question, then follow the answer’s citations. For UNC, try “When is enrollment”, “How many bookstores are on campus?” and “Where can I eat on campus?”
4. Launch **EDU_AI_DEMO_VALIDATE** with the same display name before the presentation. When the environment is no longer needed, launch **EDU_AI_DEMO_CLEANUP** and select that customer from its dropdown.

| Runtime choice | What it demonstrates |
| --- | --- |
| `openshift_ai` | A customer-facing app on OpenShift calling the configured OpenShift AI model service. |
| `rhel_vm` | Red Hat AI Inference running inside a dedicated RHEL CPU guest on OpenShift Virtualization. |
| `openshift_ai_and_rhel_vm` | Two routes using the same customer corpus and separate OpenShift AI and RHEL VM model servers. |
| `rhel_ai` | The app calling a separately provisioned RHEL AI appliance endpoint. |
| `all` | OpenShift AI and an existing RHEL AI appliance, each with its own configured endpoint. |
| `openshift` | A Route-backed demo that retrieves excerpts from the corpus. |

## Try a local build

Use Python 3.10+ and Ansible Core 2.16+. From the repository root:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install 'ansible-core>=2.16'
ansible-playbook playbooks/EDU_AI_DEMO.yml \
  -e '{"customer_name":"The University of North Carolina","customer_website":"unc.edu","demo_runtime":"artifact"}'
```

The job prints the customer workspace and checks the generated corpus. To remove those local files, run the cleanup playbook with the same customer name and `demo_runtime: artifact`.

## Platform and source details

- [AAP installation and credentials](aap/README.md)
- [Runtime, storage and ownership](docs/ARCHITECTURE.md)
- [Presentation and cleanup runbook](docs/DEMO_RUNBOOK.md)
- [Daily resource and anonymous chat activity reports](docs/DEMO_INVENTORY.md)
- [Source formats, campus places and Maps links](docs/SOURCES.md)
