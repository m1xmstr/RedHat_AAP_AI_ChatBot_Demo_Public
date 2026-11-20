"""Exercise the real role finalizer in a local job with no external services."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("ansible-playbook"), "Ansible is required")
class JobFinalizationTests(unittest.TestCase):
    def test_failed_deploy_reconciles_survey_and_releases_credential(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            role = project / "roles" / "edu_ai_demo_builder"
            shutil.copytree(ROOT / "roles" / role.name, role)
            for dirname in ("playbooks", "aap"):
                (project / dirname).mkdir()
            shutil.copy(ROOT / "playbooks/EDU_AI_DEMO.yml", project / "playbooks")
            shutil.copy(ROOT / "aap/refresh_cleanup_survey.yml", project / "aap")
            tasks = role / "tasks"
            # Mimic a partially published customer followed by an error in a nested check.
            (tasks / "build.yml").write_text(
                "- ansible.builtin.file: {path: \"{{ playbook_dir }}/published\", state: touch}\n"
                "- ansible.builtin.include_tasks: failed_live_check.yml\n"
            )
            (tasks / "failed_live_check.yml").write_text(
                "- name: Simulated failed live check\n"
                "  ansible.builtin.command: /usr/bin/false\n"
                "  run_once: true\n"
            )
            # Verify the actual finalizer can still use the per-job credential and fails
            # after reconciling the partial deployment. This exercises its own rescue.
            (project / "aap/refresh_cleanup_survey.py").write_text(
                "import os,sys\nfrom pathlib import Path\n"
                "p=Path(__file__).resolve().parents[1]\n"
                "kube=Path(os.environ['KUBECONFIG'])\n"
                "assert kube.is_file() and (p/'playbooks/published').is_file()\n"
                "(p/'reconciled').write_text(str(kube))\nsys.exit(1)\n"
            )
            env = dict(os.environ, ANSIBLE_ROLES_PATH=str(project / "roles"),
                       OCP_API_URL="https://fixture.invalid", OCP_API_TOKEN="fixture-only")
            env.pop("ANSIBLE_CONFIG", None)
            test_vars = {"customer_name": "Fixture University", "demo_runtime": "openshift",
                         "edu_ai_refresh_cleanup_survey": True, "edu_ai_cleanup_template_id": 1}
            result = subprocess.run(
                ["ansible-playbook", "-i", "localhost,", "-c", "local", "playbooks/EDU_AI_DEMO.yml",
                 "-e", json.dumps(test_vars)], cwd=project, env=env, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue((project / "reconciled").exists(), result.stdout)
            self.assertFalse(Path((project / "reconciled").read_text()).exists())
            self.assertIn("Report any customer operation or survey failure after finalization", result.stdout)


if __name__ == "__main__":
    unittest.main()
