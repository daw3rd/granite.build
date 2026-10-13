# Copyright LLM.build Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The build-runner pod gets the configured grace period between SIGTERM and SIGKILL.

`gb build cancel` deletes the build-runner pod. The runner then needs time to tear
down its workload and finalize the build as CANCELLED (issue #426). Too short a grace
period leaves the build CANCEL_REQUESTED. Both places that set it must use
BUILDRUNNERJOB_TERMINATION_GRACE_SECONDS: the pod spec in the Job template, and the
explicit pod delete in BuildRunnerJob. A shorter grace on that delete would override
the pod spec.

Asserted against the template and source, not a live ``BuildRunnerJob``, whose
constructor reads config off disk and resolves an image tag.
"""

import ast
from pathlib import Path

import yaml

from gbserver.types.constants import BUILDRUNNERJOB_TERMINATION_GRACE_SECONDS
from gbserver.utils.template import fill_template

_REPO_ROOT = Path(__file__).resolve().parents[3]
_BUILDRUNNERJOB = (
    _REPO_ROOT / "src" / "gbserver" / "buildrunnerjob" / "buildrunnerjob.py"
)
_TEMPLATE = _REPO_ROOT / "k8s" / "dep-build-runner.yaml"

# Every value the template requires (see its "Required:" header), so strict
# rendering fails if the template grows a variable this test doesn't supply.
_TEMPLATE_DATA = {
    "build_runner_name": "gb-build-runner-test",
    "build_runner_namespace": "test-ns",
    "build_runner_image": "test-image",
    "build_runner_command": ["gbserver"],
    "main_secret_name": "test-secret",
    "build_workspace_pvc_name": "test-pvc",
    "configmap_name": "test-configmap",
    "dev_staging_prod_env": "DEV",
    "build_runner_labels": {},
    "build_runner_annotations": {},
    "build_runner_extra_env_vars": {},
    "is_metrics_enabled": False,
    "is_metrics_auth_enabled": False,
    "termination_grace_period_seconds": BUILDRUNNERJOB_TERMINATION_GRACE_SECONDS,
}


def test_template_sets_termination_grace_period():
    """The rendered Job's pod spec carries the configured grace period as an int."""
    rendered = fill_template(
        templ=_TEMPLATE.read_text(encoding="utf-8"), data=_TEMPLATE_DATA, strict=True
    )
    pod_spec = yaml.safe_load(rendered)["spec"]["template"]["spec"]
    assert (
        pod_spec["terminationGracePeriodSeconds"]
        == BUILDRUNNERJOB_TERMINATION_GRACE_SECONDS
    )


def test_pod_delete_uses_configured_grace_period():
    """Every grace_period_seconds passed in BuildRunnerJob is the shared constant.

    A literal (like the former 5s) on the explicit pod delete would cut the pod
    spec's grace short and SIGKILL the runner mid-cancel.
    """
    tree = ast.parse(_BUILDRUNNERJOB.read_text(encoding="utf-8"))
    grace_values = [
        kw.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for kw in node.keywords
        if kw.arg == "grace_period_seconds"
    ]
    assert grace_values, "no grace_period_seconds found in buildrunnerjob.py"
    for value in grace_values:
        assert (
            isinstance(value, ast.Name)
            and value.id == "BUILDRUNNERJOB_TERMINATION_GRACE_SECONDS"
        ), f"grace_period_seconds is not the shared constant: {ast.dump(value)}"
