"""Shared helpers for the scripts in tools/: Elasticsearch and Kibana requests, and reading the repo's files.

Standard library only, so the scripts run with any Python 3.8 or later.
"""
import argparse
import base64
import json
import os
import re
import ssl
import urllib.error
import urllib.request
import uuid

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PIPELINE_FILE = os.path.join(REPO, "postfix_elastic_pipeline.json")
TEMPLATE_FILE = os.path.join(REPO, "postfix_component_template.json")
README_FILE = os.path.join(REPO, "README.md")
DASHBOARD_FILES = [
    "postfix_kibana_dashboard.ndjson",
    "mail_flow_kibana_dashboard.ndjson",
    "mail_flow_all_clusters_kibana_dashboard.ndjson",
    "mail_flow_example_cluster_kibana_dashboard.ndjson",
]


class RequestError(Exception):
    def __init__(self, status, method, url, body):
        super().__init__("%s %s -> HTTP %s: %s" % (method, url, status, body[:2000]))
        self.status = status


class Client:
    """Minimal JSON client for Elasticsearch or Kibana."""

    def __init__(self, url, api_key=None, user=None, password=None, insecure=False, extra_headers=None):
        self.url = url.rstrip("/")
        self.headers = dict(extra_headers or {})
        if api_key:
            self.headers["Authorization"] = "ApiKey " + api_key
        elif user:
            token = base64.b64encode(("%s:%s" % (user, password or "")).encode()).decode()
            self.headers["Authorization"] = "Basic " + token
        self.context = None
        if insecure:
            self.context = ssl.create_default_context()
            self.context.check_hostname = False
            self.context.verify_mode = ssl.CERT_NONE

    def request(self, method, path, body=None, content_type="application/json", allow_404=False):
        headers = dict(self.headers)
        data = None
        if body is not None:
            if isinstance(body, bytes):
                data = body
            elif isinstance(body, str):
                data = body.encode("utf-8")
            else:
                data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = content_type
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=300, context=self.context) as resp:
                text = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            if allow_404 and e.code == 404:
                return None
            raise RequestError(e.code, method, self.url + path, text) from None
        except urllib.error.URLError as e:
            raise SystemExit("can't reach %s: %s" % (self.url, e.reason))
        return json.loads(text) if text.strip() else None

    def upload(self, path, filename, data):
        """multipart/form-data POST with one file field, as Kibana's saved objects import expects."""
        boundary = uuid.uuid4().hex
        body = (("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"
                 "Content-Type: application/ndjson\r\n\r\n") % (boundary, filename)).encode() + data + ("\r\n--%s--\r\n" % boundary).encode()
        return self.request("POST", path, body, content_type="multipart/form-data; boundary=" + boundary)


def add_es_args(parser):
    parser.add_argument("--es", default=os.environ.get("ES_URL", "http://localhost:9200"),
                        help="Elasticsearch URL (default: $ES_URL or http://localhost:9200)")
    parser.add_argument("--api-key", default=os.environ.get("ES_API_KEY"), help="API key, base64 encoded (default: $ES_API_KEY)")
    parser.add_argument("--user", default=os.environ.get("ES_USER"), help="user for basic auth (default: $ES_USER)")
    parser.add_argument("--password", default=os.environ.get("ES_PASSWORD"), help="password for basic auth (default: $ES_PASSWORD)")
    parser.add_argument("--insecure", action="store_true", help="don't verify the TLS certificate")


def es_client(args):
    return Client(args.es, args.api_key, args.user, args.password, args.insecure)


def load_pipeline():
    """The pipeline file uses Kibana Console triple-quoted strings; turn them into JSON strings."""
    with open(PIPELINE_FILE, encoding="utf-8") as f:
        src = f.read()
    src = re.sub(r'"""(.*?)"""', lambda m: json.dumps(m.group(1)), src, flags=re.S)
    return json.loads(src)


def load_component_template():
    with open(TEMPLATE_FILE, encoding="utf-8") as f:
        return json.load(f)


def readme_samples():
    """The documents in the README's _simulate example."""
    with open(README_FILE, encoding="utf-8") as f:
        md = f.read()
    m = re.search(r"POST _ingest/pipeline/logs-postfix@custom/_simulate\r?\n(\{.*?\r?\n\})\r?\n```", md, re.S)
    if not m:
        raise SystemExit("couldn't find the _simulate example in README.md")
    return json.loads(m.group(1))["docs"]


def simulate(es, pipeline, docs):
    """Run docs through the pipeline without installing it."""
    return es.request("POST", "/_ingest/pipeline/_simulate", {"pipeline": pipeline, "docs": docs})["docs"]


def get(doc, path):
    """Value at a dotted path, or the MISSING sentinel."""
    cur = doc
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, dict) and path in cur:
            return cur[path]
        else:
            return MISSING
    return cur


class _Missing:
    def __repr__(self):
        return "<missing>"


MISSING = _Missing()


def parser(description):
    return argparse.ArgumentParser(description=description, formatter_class=argparse.RawDescriptionHelpFormatter)
