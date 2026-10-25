#!/usr/bin/env python3
"""Non-destructive live authorization checks; no workloads or secrets are read.

Usage: python scripts/verify-kubernetes-reader.py --kubeconfig PATH
SelfSubjectAccessReview creates authorization reviews, not workload resources.
The default system:basic-user binding supplies this review permission.
"""

import argparse
import json
from pathlib import Path
import sys

from kubernetes import client, config


IDENTITY = "system:serviceaccount:opspilot-system:opspilot-reader"


def load_restricted(path):
    # Accept only our standalone format; reject executable credential plugins and
    # external credential files before the SDK can process any configuration.
    data = json.loads(Path(path).read_text())
    if set(data) != {"apiVersion", "kind", "clusters", "users", "contexts", "current-context"}:
        raise ValueError("unexpected kubeconfig fields")
    if data["apiVersion"] != "v1" or data["kind"] != "Config":
        raise ValueError("invalid kubeconfig type")
    if any(len(data[key]) != 1 for key in ("clusters", "users", "contexts")):
        raise ValueError("expected a single identity and cluster")
    cluster = data["clusters"][0]
    user = data["users"][0]
    context = data["contexts"][0]
    if set(cluster["cluster"]) != {"server", "certificate-authority-data"}:
        raise ValueError("unexpected cluster credentials")
    if not cluster["cluster"]["server"].startswith("https://") or not cluster["cluster"]["certificate-authority-data"]:
        raise ValueError("TLS configuration required")
    if set(user["user"]) != {"token"} or not user["user"]["token"]:
        raise ValueError("expected only a bearer token")
    if (data["current-context"] != context["name"]
            or context["context"] != {"cluster": cluster["name"], "user": user["name"], "namespace": "default"}):
        raise ValueError("invalid context references")
    configuration = client.Configuration()
    config.load_kube_config_from_dict(data, context=context["name"],
                                     client_configuration=configuration, persist_config=False)
    return client.ApiClient(configuration=configuration)


def authorization_cases():
    # None means a cluster-wide request; named cases also cover arbitrary namespaces.
    for namespace in (None, "default", "opspilot-rbac-verification"):
        for resource, verbs in (("pods", ("get", "list")), ("pods/log", ("get",)),
                                ("events", ("list",))):
            # Pod get/log requests always specify a namespace in actual OpsPilot calls.
            if namespace is None and resource == "pods/log":
                continue
            for verb in verbs:
                if namespace is None and verb == "get":
                    continue
                yield True, resource, verb, namespace
        for verb in ("get", "list", "watch"):
            yield False, "secrets", verb, namespace
        for verb in ("create", "update", "patch", "delete", "deletecollection", "watch"):
            yield False, "pods", verb, namespace
        for resource in ("pods/exec", "pods/attach", "pods/portforward", "pods/proxy"):
            for verb in ("get", "create"):
                yield False, resource, verb, namespace
    yield True, "namespaces", "list", None
    for resource in ("nodes", "deployments", "configmaps", "serviceaccounts"):
        for verb in ("get", "list"):
            yield False, resource, verb, None
    yield False, "serviceaccounts/token", "create", "opspilot-system"
    for resource in ("roles", "rolebindings", "clusterroles", "clusterrolebindings"):
        for verb in ("create", "update", "patch", "delete", "bind", "escalate"):
            yield False, resource, verb, None
    yield False, "users", "impersonate", None


def verify(api_client):
    authentication = client.AuthenticationV1Api(api_client)
    identity = authentication.create_self_subject_review(
        client.V1SelfSubjectReview(), _request_timeout=15).status.user_info.username
    if identity != IDENTITY:
        raise ValueError("unexpected authenticated identity")
    authorization = client.AuthorizationV1Api(api_client)
    count = 0
    for expected, resource, verb, namespace in authorization_cases():
        group = ("apps" if resource == "deployments" else
                 "rbac.authorization.k8s.io" if resource in {
                     "roles", "rolebindings", "clusterroles", "clusterrolebindings"} else "")
        name, _, subresource = resource.partition("/")
        attributes = client.V1ResourceAttributes(
            group=group, resource=name, subresource=subresource or None,
            verb=verb, namespace=namespace)
        review = client.V1SelfSubjectAccessReview(spec=client.V1SelfSubjectAccessReviewSpec(
            resource_attributes=attributes))
        status = authorization.create_self_subject_access_review(review, _request_timeout=15).status
        if status.evaluation_error or (status.allowed is True) != expected:
            raise ValueError("unexpected authorization result")
        count += 1
    review = client.V1SelfSubjectAccessReview(spec=client.V1SelfSubjectAccessReviewSpec(
        non_resource_attributes=client.V1NonResourceAttributes(path="/api/v1", verb="get")))
    status = authorization.create_self_subject_access_review(review, _request_timeout=15).status
    if status.evaluation_error or status.allowed is not True:
        raise ValueError("discovery authorization denied")
    # Exercise real read paths that require no fixtures or sensitive object access.
    core = client.CoreV1Api(api_client)
    core.get_api_resources(_request_timeout=15)
    core.list_namespace(_request_timeout=15)
    core.list_pod_for_all_namespaces(_request_timeout=15)
    core.list_event_for_all_namespaces(_request_timeout=15)
    core.list_namespaced_pod("default", _request_timeout=15)
    core.list_namespaced_event("default", _request_timeout=15)
    return count + 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    args = parser.parse_args(argv)
    try:
        with load_restricted(args.kubeconfig) as api_client:
            count = verify(api_client)
    except Exception:
        print("Kubernetes reader verification failed; check credentials, identity, and RBAC.",
              file=sys.stderr)
        return 1
    print(f"Passed {count} authorization checks and six live read checks as {IDENTITY}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
