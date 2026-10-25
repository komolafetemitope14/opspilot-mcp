"""Offline RBAC and credential isolation checks; never invoke a live kubectl."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/provision-kubernetes-reader.sh"


def test_exact_rbac_matrix_and_subject():
    namespace, account, role, binding = list(yaml.safe_load_all(
        (ROOT / "kubernetes/opspilot-reader-rbac.yaml").read_text()))
    assert [x["kind"] for x in (namespace, account, role, binding)] == [
        "Namespace", "ServiceAccount", "ClusterRole", "ClusterRoleBinding"]
    assert namespace["metadata"] == {"name": "opspilot-system"}
    assert account["metadata"] == {"name": "opspilot-reader", "namespace": "opspilot-system"}
    assert account["automountServiceAccountToken"] is False
    assert role["metadata"] == binding["metadata"] == {"name": "opspilot-reader"}
    assert role["rules"] == [
        {"apiGroups": [""], "resources": ["namespaces"], "verbs": ["list"]},
        {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list"]},
        {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]},
        {"apiGroups": [""], "resources": ["events"], "verbs": ["list"]},
        {"nonResourceURLs": ["/api/v1"], "verbs": ["get"]},
    ]
    assert binding["roleRef"] == {"apiGroup": "rbac.authorization.k8s.io",
                                   "kind": "ClusterRole", "name": "opspilot-reader"}
    assert binding["subjects"] == [{"kind": "ServiceAccount", "name": "opspilot-reader",
                                    "namespace": "opspilot-system"}]
    assert all(set(r["verbs"]) <= {"get", "list"} for r in role["rules"])
    assert not any("secrets" in r.get("resources", []) or "*" in str(r) for r in role["rules"])


@pytest.fixture
def provision(monkeypatch, tmp_path):
    source = SCRIPT.read_text().split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    module = {"__name__": "test_provision"}
    exec(compile(source, str(SCRIPT), "exec"), module)
    monkeypatch.setattr(sys, "argv", ["-", str(tmp_path), "--admin-context", "kind-explicit"])
    source_config = {"clusters": [{"cluster": {
        "server": "https://127.0.0.1:6443", "certificate-authority-data": "Y2E=",
        "proxy-url": "https://admin-secret"}}], "users": [{"user": {
            "client-key-data": "admin-secret", "client-certificate-data": "admin-secret",
            "token": "admin-secret", "exec": {"command": "evil"},
            "auth-provider": {"name": "admin-secret"}}}]}
    run = MagicMock(side_effect=[SimpleNamespace(stdout=json.dumps(source_config)),
                                SimpleNamespace(stdout="applied"),
                                SimpleNamespace(stdout="reader-secret\n")])
    monkeypatch.setattr(subprocess, "run", run)
    return module, run, tmp_path, source_config


def test_standalone_generation_and_atomic_private_output(provision, capsys):
    module, run, root, _ = provision
    destination = root / ".local/kubernetes/opspilot-reader.kubeconfig"
    destination.parent.mkdir(parents=True)
    destination.write_text("old credentials")
    assert module["main"]() == 0
    generated = json.loads(destination.read_text())
    assert generated["clusters"] == [{"name": "opspilot", "cluster": {
        "server": "https://127.0.0.1:6443", "certificate-authority-data": "Y2E="}}]
    assert generated["users"] == [{"name": "opspilot-reader", "user": {"token": "reader-secret"}}]
    assert generated["contexts"] == [{"name": "opspilot-reader", "context": {
        "cluster": "opspilot", "user": "opspilot-reader", "namespace": "default"}}]
    assert generated["current-context"] == "opspilot-reader"
    assert destination.stat().st_mode & 0o777 == 0o600
    assert destination.parent.stat().st_mode & 0o777 == 0o700
    assert list(destination.parent.iterdir()) == [destination]
    commands = [call.args[0] for call in run.call_args_list]
    assert all(c[:3] == ["kubectl", "--context", "kind-explicit"] for c in commands)
    assert commands[1][3:] == ["apply", "-f", str(root / "kubernetes/opspilot-reader-rbac.yaml")]
    assert commands[2][3:] == ["-n", "opspilot-system", "create", "token", "opspilot-reader", "--duration=1h"]
    assert "secret" not in str(generated["clusters"])
    output = capsys.readouterr()
    assert "secret" not in output.out + output.err


@pytest.mark.parametrize("arguments", [[], ["--admin-context", ""], ["--admin-context", " "]])
def test_explicit_context_required(provision, monkeypatch, arguments):
    module, run, root, _ = provision
    monkeypatch.setattr(sys, "argv", ["-", str(root)] + arguments)
    with pytest.raises(SystemExit):
        module["main"]()
    run.assert_not_called()


@pytest.mark.parametrize("stage", [0, 1, 2])
def test_failure_is_secret_safe_and_preserves_old_credentials(provision, stage, capsys):
    module, run, root, source = provision
    destination = root / ".local/kubernetes/opspilot-reader.kubeconfig"
    destination.parent.mkdir(parents=True)
    destination.write_text("old")
    responses = [SimpleNamespace(stdout=json.dumps(source)), SimpleNamespace(stdout="ok")]
    run.side_effect = responses[:stage] + [RuntimeError("admin-secret reader-secret")]
    assert module["main"]() == 1
    assert destination.read_text() == "old"
    assert list(destination.parent.iterdir()) == [destination]
    output = capsys.readouterr()
    assert "failed" in output.err
    assert "secret" not in output.out + output.err


def test_insecure_cluster_rejected_before_apply(provision):
    module, run, _, source = provision
    source["clusters"][0]["cluster"]["insecure-skip-tls-verify"] = True
    run.side_effect = [SimpleNamespace(stdout=json.dumps(source))]
    assert module["main"]() == 1
    assert run.call_count == 1


@pytest.fixture
def verifier():
    spec = importlib.util.spec_from_file_location("verify_reader", ROOT / "scripts/verify-kubernetes-reader.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_verifier_loads_only_explicit_configuration(provision, verifier, monkeypatch):
    module, _, root, _ = provision
    assert module["main"]() == 0
    load = MagicMock()
    api = MagicMock()
    monkeypatch.setattr(verifier.config, "load_kube_config_from_dict", load)
    monkeypatch.setattr(verifier.client, "ApiClient", api)
    monkeypatch.setenv("KUBECONFIG", "/admin/config")
    verifier.load_restricted(root / ".local/kubernetes/opspilot-reader.kubeconfig")
    assert load.call_args.kwargs["persist_config"] is False
    configuration = load.call_args.kwargs["client_configuration"]
    assert api.call_args.kwargs == {"configuration": configuration}
    assert load.call_args.args[0]["users"][0]["user"] == {"token": "reader-secret"}


@pytest.mark.parametrize("credential", ["exec", "auth-provider", "client-key-data", "tokenFile"])
def test_verifier_rejects_alternate_credentials(provision, verifier, credential, monkeypatch):
    module, _, root, _ = provision
    assert module["main"]() == 0
    path = root / ".local/kubernetes/opspilot-reader.kubeconfig"
    data = json.loads(path.read_text())
    data["users"][0]["user"][credential] = "admin-secret"
    path.write_text(json.dumps(data))
    load = MagicMock()
    monkeypatch.setattr(verifier.config, "load_kube_config_from_dict", load)
    with pytest.raises(ValueError):
        verifier.load_restricted(path)
    load.assert_not_called()


def test_verification_uses_reviews_never_workload_mutations(verifier, monkeypatch):
    authentication, authorization, core = MagicMock(), MagicMock(), MagicMock()
    authentication.create_self_subject_review.return_value.status.user_info.username = verifier.IDENTITY
    expected = [case[0] for case in verifier.authorization_cases()] + [True]
    authorization.create_self_subject_access_review.side_effect = [
        SimpleNamespace(status=SimpleNamespace(allowed=value, evaluation_error=None)) for value in expected]
    for name, mock in (("AuthenticationV1Api", authentication), ("AuthorizationV1Api", authorization),
                       ("CoreV1Api", core)):
        monkeypatch.setattr(verifier.client, name, lambda api, result=mock: result)
    assert verifier.verify(MagicMock()) == len(expected)
    assert {call[0] for call in core.mock_calls} == {
        "get_api_resources", "list_namespace", "list_pod_for_all_namespaces",
        "list_event_for_all_namespaces", "list_namespaced_pod", "list_namespaced_event"}
    cases = list(verifier.authorization_cases())
    assert (False, "pods", "delete", "default") in cases
    assert (False, "secrets", "get", "default") in cases
    authentication.create_self_subject_review.return_value.status.user_info.username = "admin"
    with pytest.raises(ValueError):
        verifier.verify(MagicMock())


def test_verifier_failure_has_no_fallback_or_secret_output(verifier, monkeypatch, capsys):
    load = MagicMock(side_effect=RuntimeError("admin-secret reader-secret"))
    monkeypatch.setattr(verifier, "load_restricted", load)
    assert verifier.main(["--kubeconfig", "/missing/restricted"]) == 1
    load.assert_called_once_with("/missing/restricted")
    output = capsys.readouterr()
    assert "secret" not in output.out + output.err


def test_generated_path_ignored_and_shell_syntax():
    result = subprocess.run(["git", "check-ignore", ".local/kubernetes/opspilot-reader.kubeconfig"],
                            cwd=ROOT, check=True, capture_output=True, text=True)
    assert result.stdout.strip() == ".local/kubernetes/opspilot-reader.kubeconfig"
    subprocess.run(["sh", "-n", str(SCRIPT)], check=True, capture_output=True)


def test_atomic_write_failure_preserves_credentials(provision, monkeypatch, capsys):
    module, _, root, _ = provision
    destination = root / ".local/kubernetes/opspilot-reader.kubeconfig"
    destination.parent.mkdir(parents=True)
    destination.write_text("old")
    monkeypatch.setattr(module["os"], "replace", MagicMock(side_effect=OSError("reader-secret")))
    assert module["main"]() == 1
    assert destination.read_text() == "old"
    assert list(destination.parent.iterdir()) == [destination]
    output = capsys.readouterr()
    assert "secret" not in output.out + output.err


@pytest.mark.parametrize("allowed,error", [(True, None), (False, "evaluation failed")])
def test_verifier_rejects_excess_permission_or_evaluation_error(verifier, monkeypatch, allowed, error):
    authentication, authorization = MagicMock(), MagicMock()
    authentication.create_self_subject_review.return_value.status.user_info.username = verifier.IDENTITY
    monkeypatch.setattr(verifier.client, "AuthenticationV1Api", lambda api: authentication)
    monkeypatch.setattr(verifier.client, "AuthorizationV1Api", lambda api: authorization)
    monkeypatch.setattr(verifier, "authorization_cases", lambda: iter([(False, "secrets", "get", "default")]))
    authorization.create_self_subject_access_review.return_value.status = SimpleNamespace(
        allowed=allowed, evaluation_error=error)
    with pytest.raises(ValueError):
        verifier.verify(MagicMock())
