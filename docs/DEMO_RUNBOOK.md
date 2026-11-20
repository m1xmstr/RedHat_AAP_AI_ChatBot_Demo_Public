# Presenting a customer demo

## Prepare

An administrator runs the [AAP setup](../aap/README.md), connects the project to the Git source, configures the OpenShift credential and model endpoints, and syncs the project. The execution environment needs Ansible, Python 3.10 or newer, `oc`, working DNS and trusted TLS for the API, routes and public sites. In-cluster inference addresses must be reachable from the demo application. Use the model ID returned by the serving endpoint.

## Deploy and rehearse

1. Launch **EDU_AI_DEMO**. For the UNC example use **The University of North Carolina** and **unc.edu**. Choose a configured runtime or accept the default. Reuse the exact display name for subsequent jobs.
2. Open the route in the job output. Check the school name, displayed page and passage counts, and **Download RAG corpus (JSONL)**. Open the file and inspect a passage and its source URL.
3. Ask a detailed question covered by a collected page. Follow the citation to verify the answer. Ask an unrelated question to demonstrate insufficient evidence. Check the provider on the individual response when demonstrating a model.
4. Run **EDU_AI_DEMO_VALIDATE** before the meeting. This uses the live route in a new execution environment. A deploy job or a previous answer alone cannot establish current model health.

Sites can block automation on individual hosts. The UNC homepage and university catalog can respond differently to the same job. Use the downloaded corpus to describe actual coverage. If a topic is missing, supply official seed URLs and rerun the deployment.

## Inspect source coverage

Open **View indexed sources** on the chat page to see the actual URLs, passage counts and named places. The build log records indexed sources, skipped URLs and discovered locations. Use the source links to verify a detail and the Google Maps links to look up a named place or get directions. The named places come from the customer's website. Map links work without an API key and open a search when clicked.

For the UNC rehearsal, ask **When is enrollment**, **How many bookstores are on campus?** and **Where can I eat on campus?**. The deploy and independent validation jobs ask all three on each selected model route. Compare the registration term and cohort with the registrar calendar, the bookstore description with its source, and named dining options with the official listings. A directory or handbook can identify individual bookstores without establishing a campus-wide total.

The builder runs fresh discovery for each customer. When a calendar or PDF is difficult to reach from a homepage, add official seeds in the survey. Administrators can keep reusable hints under `tools/source_profiles/` using the site's domain as the filename. The UNC profile contains URLs and page selections; the job fetches the actual documents at build time. Refresh term-specific hints when the registrar publishes a new calendar.

## Compare the two providers

Use `openshift_ai` for a routine customer presentation. This choice reuses the configured OpenShift AI model and deploys the customer app without creating a VM. Select `openshift_ai_and_rhel_vm` when the presentation includes a RHEL CPU comparison. Each such customer owns one guest until cleanup. The first VM boot clones the disk, obtains the inference image and model, and starts the guest service. Allow time for that work before the presentation. The two HTTPS routes share a corpus and use separate model servers. Ask the same source-backed question in both and inspect the source links. The VM option appears in AAP as `rhel_vm` and the chat labels its answers **Red Hat AI Inference on RHEL**.

## Remove the demo

After the presenter finishes reviewing the environment, launch **EDU_AI_DEMO_CLEANUP** and select the school. Inspect the job outcome, confirm the customer's namespace and route are gone, and reopen the cleanup survey to check that the name disappeared. Other schools should remain selectable. Cleanup ownership checks deliberately exclude legacy deployments and resources belonging to another customer.

See [runtime and storage](ARCHITECTURE.md) for what the deployment creates and where the corpus lives. Archive or delete any presenter-downloaded copies separately.
