#!/usr/bin/env python3
"""Index a day of synthetic Postfix and Exchange logs and run every ES|QL query in the four dashboards against them.

Meant for a throwaway cluster (see "Checking changes" in README.md). It:

1. creates the index template and data stream logs-postfix-dashboardtest, with the mappings from
   postfix_component_template.json;
2. generates about 9,000 log lines for the last 24 hours (deliveries, deferrals, bounces, expiries, rejects,
   TLS, SASL, OpenDKIM, OpenDMARC and policyd-spf), parses them with postfix_elastic_pipeline.json through
   _simulate, and indexes the result, so the pipeline isn't installed;
3. generates Exchange Online message trace rows and Exchange Server message tracking events in the shape
   Elastic's integrations index them (see exchange_data.py), including Exchange's view of Postfix mail handed on to
   it, and indexes them into logs-microsoft_exchange_online_message_trace.log-dashboardtest and
   logs-microsoft_exchange_server.messagetracking-dashboardtest;
4. runs each panel's and each control's query with several control settings, and fails on a query error, a
   column the panel expects but the query doesn't return, or (with every control at All) a panel with no rows;
   it also compares the headline counts (Postfix tiles; mail flow messages, volume, delivered, not delivered and
   messages seen by more than one system; Exchange failures by reason and status) with counts taken straight from the
   generated data;
5. with --kibana, imports the four dashboards so you can look at them.

It refuses to run when the cluster has other Postfix or Exchange data streams, because the dashboards read
those patterns and the synthetic data would show up next to real mail. --force overrides that.

    python tools/test_dashboards.py --es http://localhost:9200 --kibana http://localhost:5601

Exit status is 0 when every check passes, 1 otherwise.
"""
import datetime as dt
import json
import os
import random
import re
import sys

import exchange_data
from common import (DASHBOARD_FILES, REPO, Client, add_es_args, es_client, load_component_template, load_pipeline,
                    parser, simulate)

STREAM = "logs-postfix-dashboardtest"
TEST_STREAMS = [STREAM, exchange_data.EOL_STREAM, exchange_data.EXS_STREAM]
# data streams whose real data would mix with the synthetic data in the dashboards
WATCHED = ["logs-postfix-*", "logs-microsoft_exchange_online_message_trace.log-*", "logs-microsoft_exchange_server.messagetracking-*"]
# panels that have no rows in the synthetic data, and why
EXPECTED_EMPTY = {}

SENDERS = ["alice@example.org", "bob@example.org", "newsletter@example.org", "billing@example.org", "carol@example.net", "noreply@shop.example"]
DOMAINS = ["gmail.example", "outlook.example", "corp.example", "partner.example", "slowmx.example"]
FAILURES = [
    ("bounced", "5.1.1", True, "host mx.{d}[203.0.113.{n}] said: 550 5.1.1 <{to}>: Recipient address rejected: User unknown in virtual mailbox table (in reply to RCPT TO command)"),
    ("bounced", "5.1.1", True, "host mx.{d}[203.0.113.{n}] said: 550-5.1.1 The email account that you tried to reach does not exist. 550 5.1.1 Please check the address (in reply to RCPT TO command)"),
    ("bounced", "5.2.2", True, "host mx.{d}[203.0.113.{n}] said: 552 5.2.2 <{to}>: Mailbox full, quota exceeded (in reply to RCPT TO command)"),
    ("bounced", "5.7.1", True, "host mx.{d}[203.0.113.{n}] said: 554 5.7.1 Message rejected as spam by content filter (in reply to end of DATA command)"),
    ("bounced", "5.3.4", True, "host mx.{d}[203.0.113.{n}] said: 552 5.3.4 Message size exceeds fixed limit (in reply to MAIL FROM command)"),
    ("bounced", "5.4.4", False, "Host or domain name not found. Name service error for name={d} type=AAAA: Host not found"),
    ("deferred", "4.4.1", False, "connect to mx.{d}[203.0.113.{n}]:25: Connection timed out"),
    ("deferred", "4.4.1", False, "connect to mx.{d}[203.0.113.{n}]:25: Connection refused"),
    ("deferred", "4.7.1", True, "host mx.{d}[203.0.113.{n}] said: 450 4.7.1 <{to}>: Recipient address rejected: Greylisted, try again in 300 seconds (in reply to RCPT TO command)"),
    ("deferred", "4.7.0", True, "host mx.{d}[203.0.113.{n}] said: 421 4.7.0 Too many connections from your host, rate limit exceeded (in reply to MAIL FROM command)"),
    ("deferred", "4.4.2", True, "lost connection with mx.{d}[203.0.113.{n}] while sending message body"),
    ("deferred", "4.2.2", True, "host mx.{d}[203.0.113.{n}] said: 452 4.2.2 <{to}>: user is over quota (in reply to RCPT TO command)"),
]
REJECTS = [
    "NOQUEUE: reject: RCPT from unknown[198.51.100.{n}]: 554 5.7.1 <{to}>: Relay access denied; from=<{frm}> to=<{to}> proto=ESMTP helo=<x{n}.example.net>",
    "NOQUEUE: reject: RCPT from unknown[198.51.100.{n}]: 554 5.7.1 Service unavailable; Client host [198.51.100.{n}] blocked using zen.spamhaus.org; https://www.spamhaus.org/query/ip/198.51.100.{n}; from=<{frm}> to=<{to}> proto=ESMTP helo=<x{n}.example.net>",
    "NOQUEUE: reject: RCPT from mail{n}.example.net[198.51.100.{n}]: 550 5.1.1 <{to}>: Recipient address rejected: User unknown in local recipient table; from=<{frm}> to=<{to}> proto=ESMTP helo=<mail{n}.example.net>",
    "NOQUEUE: reject: RCPT from unknown[198.51.100.{n}]: 450 4.7.25 Client host rejected: cannot find your hostname, [198.51.100.{n}]; from=<{frm}> to=<{to}> proto=ESMTP helo=<x{n}.example.net>",
    "NOQUEUE: reject: RCPT from unknown[198.51.100.{n}]: 450 4.1.8 <{frm}>: Sender address rejected: Domain not found; from=<{frm}> to=<{to}> proto=ESMTP helo=<x{n}.example.net>",
]
# control settings to run each query with; the first is "everything" and is the one the empty-panel check uses
COMBOS = [
    dict(activity="All", min_size_kb=0, max_size_kb=0, sender="All", recipient="All"),
    dict(activity="Not delivered", min_size_kb=0, max_size_kb=0, sender="alice@example.org", recipient="All"),
    dict(activity="bounced", min_size_kb=100, max_size_kb=0, sender="All", recipient="All"),
    dict(activity="All", min_size_kb=0, max_size_kb=0, sender="All", recipient="person1@slowmx.example"),
    dict(activity="sent", min_size_kb=10, max_size_kb=1000, sender="bob@example.org", recipient="person2@gmail.example"),
]


def generate(now, seed=7):
    rnd = random.Random(seed)
    lines = []

    def emit(t, host, prog, msg, pid=None):
        if t < now:
            stamp = t.strftime("%Y-%m-%dT%H:%M:%S.%f") + "+00:00"
            lines.append({"message": "%s %s %s[%d]: %s" % (stamp, host, prog, pid or rnd.randint(1000, 9999), msg), "host": {"name": host}})

    def qid():
        return "".join(rnd.choice("0123456789ABCDEF") for _ in range(10))

    for i in range(1500):
        host = rnd.choice(["mail01", "mail01", "mail02"])
        t = now - dt.timedelta(seconds=rnd.uniform(120, 86000))
        n = rnd.randint(2, 250)
        if rnd.random() < 0.18:
            to = "user%d@example.org" % rnd.randint(1, 30)
            emit(t, host, "postfix/smtpd", rnd.choice(REJECTS).format(n=n, to=to, frm="spam%d@bad%d.example" % (n, n % 7)))
            continue
        if rnd.random() < 0.03:
            emit(t, host, "postfix/smtpd", "warning: unknown[198.51.100.%d]: SASL LOGIN authentication failed: UGFzc3dvcmQ6, sasl_username=user%d@example.org" % (n, n % 9))
        q = qid()
        frm = rnd.choice(SENDERS)
        client = "app%d.example.org[192.0.2.%d]" % (n % 5, n % 5 + 1)
        size = int(rnd.lognormvariate(10.5, 1.8)) + 400
        nrcpt = rnd.choice([1, 1, 1, 2, 3])
        emit(t, host, "postfix/smtpd", "Anonymous TLS connection established from %s: TLSv1.%d with cipher TLS_AES_256_GCM_SHA384 (256/256 bits)" % (client, rnd.choice([2, 3, 3])))
        emit(t, host, "postfix/smtpd", "%s: client=%s" % (q, client))
        emit(t + dt.timedelta(milliseconds=20), host, "opendkim", "%s: DKIM verification %s" % (q, rnd.choice(["successful", "successful", "failed"])))
        emit(t + dt.timedelta(milliseconds=25), host, "opendmarc", "%s: %s %s" % (q, frm.split("@")[1], rnd.choice(["pass", "pass", "fail", "none"])))
        emit(t + dt.timedelta(milliseconds=26), host, "policyd-spf",
             "prepend Received-SPF: %s (mailfrom) identity=mailfrom; client-ip=192.0.2.%d; helo=app.example.org; envelope-from=%s; receiver=<UNKNOWN>"
             % (rnd.choice(["Pass", "Pass", "Softfail", "None"]), n % 5 + 1, frm))
        emit(t + dt.timedelta(milliseconds=30), host, "postfix/cleanup", "%s: message-id=<%s.%d@example.org>" % (q, q.lower(), i))
        emit(t + dt.timedelta(milliseconds=60), host, "postfix/qmgr", "%s: from=<%s>, size=%d, nrcpt=%d (queue active)" % (q, frm, size, nrcpt), 2345)
        for r in range(nrcpt):
            d = rnd.choice(DOMAINS)
            to = "person%d@%s" % (rnd.randint(1, 40), d)
            t2 = t + dt.timedelta(seconds=rnd.uniform(0.3, 5))
            if rnd.random() < (0.45 if d == "slowmx.example" else 0.15):
                act, dsn, has_relay, text = rnd.choice(FAILURES)
                relay = "mx.%s[203.0.113.%d]:25" % (d, n) if has_relay else "none"
                tries = rnd.randint(1, 3) if act == "deferred" else 1
                for k in range(tries):
                    tk = t2 + dt.timedelta(seconds=k * 900)
                    delay = round((tk - t).total_seconds(), 1)
                    if k:
                        # a retried message logs a new queue active line
                        emit(tk - dt.timedelta(milliseconds=50), host, "postfix/qmgr", "%s: from=<%s>, size=%d, nrcpt=%d (queue active)" % (q, frm, size, nrcpt), 2345)
                    emit(tk, host, "postfix/smtp", "%s: to=<%s>, relay=%s, delay=%s, delays=0.1/%s/%s/%s, dsn=%s, status=%s (%s)"
                         % (q, to, relay, delay, round(delay * 0.6, 2), round(delay * 0.3, 2), round(delay * 0.1, 2), dsn, act, text.format(d=d, n=n, to=to)))
                if act == "bounced":
                    emit(t2 + dt.timedelta(seconds=1), host, "postfix/bounce", "%s: sender non-delivery notification: %s" % (q, qid()))
                elif rnd.random() < 0.15:
                    emit(t2 + dt.timedelta(seconds=tries * 900), host, "postfix/qmgr", "%s: from=<%s>, status=expired, returned to sender" % (q, frm), 2345)
            else:
                delay = round(rnd.lognormvariate(0, 1), 2)
                emit(t2, host, "postfix/smtp", "%s: to=<%s>, relay=mx.%s[203.0.113.%d]:25, delay=%s, delays=0.05/0.01/%s/%s, dsn=2.0.0, status=sent (250 2.0.0 Ok: queued as %s)"
                     % (q, to, d, n, delay, round(delay * 0.4, 2), round(delay * 0.5, 2), qid()))
        emit(t + dt.timedelta(seconds=10), host, "postfix/qmgr", "%s: removed" % q, 2345)
    return lines


def index_data(es, now):
    template = load_component_template()["template"]
    template.setdefault("settings", {}).setdefault("index", {})["default_pipeline"] = "_none"
    template["mappings"]["properties"]["@timestamp"] = {"type": "date"}
    es.request("PUT", "/_index_template/" + STREAM, {
        "index_patterns": [STREAM], "data_stream": {}, "priority": 500, "template": template,
        "_meta": {"description": "synthetic data for tools/test_dashboards.py in the elastic-postfix repo"}})
    es.request("DELETE", "/_data_stream/" + STREAM, allow_404=True)

    pipeline = load_pipeline()
    lines = generate(now)
    bad, body = [], []
    for start in range(0, len(lines), 1000):
        batch = lines[start:start + 1000]
        for src, res in zip(batch, simulate(es, pipeline, [{"_source": s} for s in batch])):
            doc = res.get("doc", {}).get("_source")
            if doc is None:
                bad.append((src["message"], json.dumps(res)[:300]))
                continue
            tags = [t for t in doc.get("tags", []) if not t.startswith("_geoip_database_unavailable")]
            if tags or "error" in doc:
                bad.append((src["message"], tags or doc.get("error")))
            body.append('{"create":{}}')
            body.append(json.dumps(doc))
    r = es.request("POST", "/%s/_bulk?refresh=true&pipeline=_none" % STREAM, "\n".join(body) + "\n", content_type="application/x-ndjson")
    errors = [i["create"]["error"] for i in r["items"] if "error" in i["create"]]

    eol, exs = exchange_data.generate(now, lines)
    for stream, template in exchange_data.index_templates().items():
        es.request("PUT", "/_index_template/" + stream, template)
        es.request("DELETE", "/_data_stream/" + stream, allow_404=True)
    for stream, docs in ((exchange_data.EOL_STREAM, eol), (exchange_data.EXS_STREAM, exs)):
        body = "".join('{"create":{}}\n' + json.dumps(d) + "\n" for d in docs)
        r = es.request("POST", "/%s/_bulk?refresh=true&pipeline=_none" % stream, body, content_type="application/x-ndjson")
        errors += [i["create"]["error"] for i in r["items"] if "error" in i["create"]]
    return lines, eol, exs, bad, errors


def expected_counts(lines, eol, exs):
    """Headline numbers per dashboard file, counted from the raw lines rather than through the pipeline."""
    msgs = [(l["host"]["name"], l["message"]) for l in lines]
    # one entry per message, although a retried message logs "queue active" again
    sizes = {}
    for h, m in msgs:
        q = re.search(r": ([0-9A-F]{10}): from=<[^>]*>, size=(\d+)", m)
        if q and "(queue active)" in m:
            sizes[(h, q.group(1))] = int(q.group(2))
    rejects = sum(1 for _, m in msgs if "NOQUEUE: reject:" in m)
    mb = sum(sizes.values()) / 1048576.0
    postfix = {
        "kpi-sent": sum(1 for _, m in msgs if "status=sent" in m),
        "kpi-deferred": sum(1 for _, m in msgs if "status=deferred" in m),
        "kpi-bounced": sum(1 for _, m in msgs if "status=bounced" in m or "status=expired" in m),
        "kpi-rejected": rejects,
        "kpi-messages-queued": len(sizes),
        "kpi-total-volume": round(mb, 1),
        "kpi-matching-messages": len(sizes) + rejects,
    }
    mail_flow = exchange_data.expected_mail_flow(lines, eol, exs)
    return {"postfix_kibana_dashboard.ndjson": postfix,
            "mail_flow_kibana_dashboard.ndjson": mail_flow,
            "mail_flow_all_clusters_kibana_dashboard.ndjson": mail_flow,
            "mail_flow_example_cluster_kibana_dashboard.ndjson": mail_flow}


def same(got, want):
    """Exact for counts; for MB rounded to 0.1, allow for the two sides rounding a .x5 differently."""
    if isinstance(want, float) and isinstance(got, (int, float)):
        return abs(got - want) <= 0.1001
    return got == want


def local_only(query):
    """Drop remote-cluster patterns (*:logs-..., EXAMPLE:logs-...) from FROM, so a single test cluster can run the query."""
    m = re.match(r"(FROM\s+)(.*?)(\s+METADATA\b|\s*\|)", query, re.S)
    if not m:
        return query
    sources = [s.strip() for s in m.group(2).split(",") if ":" not in s]
    return m.group(1) + ", ".join(sources) + query[m.end(2):]


def run_query(es, query, now):
    used = set(re.findall(r"\?(\w+)", query))
    times = {"_tstart": (now - dt.timedelta(hours=24)).isoformat(), "_tend": now.isoformat()}
    controls = [c for c in COMBOS] if used - set(times) else COMBOS[:1]
    results = []
    for combo in controls:
        values = dict(combo, **times)
        params = [{k: values[k]} for k in sorted(used) if k in values]
        unknown = sorted(used - set(values))
        if unknown:
            raise ValueError("query uses parameters the test doesn't set: %s" % unknown)
        r = es.request("POST", "/_query", {"query": local_only(query), "params": params})
        results.append((combo, [c["name"] for c in r["columns"]], r["values"]))
    return results


def check_dashboard(es, path, now, verbose, expected=None):
    failures, count = [], 0
    with open(path, encoding="utf-8") as f:
        objs = [json.loads(l) for l in f if l.strip()]
    for dash in (o for o in objs if o["type"] == "dashboard"):
        a = dash["attributes"]
        queries = []
        for p in json.loads(a["panelsJSON"]):
            st = p.get("embeddableConfig", {}).get("attributes", {}).get("state")
            if not st or "esql" not in st.get("query", {}):
                continue
            layer = st["datasourceStates"]["textBased"]["layers"]["layer1"]
            if layer["query"]["esql"] != st["query"]["esql"]:
                failures.append("%s: the layer query and the panel query differ" % p["panelIndex"])
            queries.append((p["panelIndex"], st["query"]["esql"], [c["fieldName"] for c in layer["columns"]]))
        for cid, c in json.loads(a.get("controlGroupInput", {}).get("panelsJSON", "{}")).items():
            q = c.get("explicitInput", {}).get("esqlQuery")
            if q:
                queries.append(("control " + cid, q, []))
        for name, query, cols in queries:
            count += 1
            try:
                results = run_query(es, query, now)
            except Exception as e:
                failures.append("%s: %s" % (name, e))
                continue
            for combo, names, rows in results:
                missing = [c for c in cols if c not in names]
                if missing:
                    failures.append("%s: columns %s missing from the result %s" % (name, missing, names))
            first_rows = results[0][2]
            if expected and name in expected:
                want = expected[name]
                if callable(want):
                    ok, why = want(results[0][1], first_rows)
                    if not ok:
                        failures.append("%s: %s" % (name, why))
                else:
                    got = first_rows[0][0] if first_rows and first_rows[0] else None
                    if not same(got, want):
                        failures.append("%s: %s with every control at All, expected %s" % (name, got, want))
            empty = not first_rows or all(v is None for row in first_rows for v in row)
            if empty and name not in EXPECTED_EMPTY:
                failures.append("%s: no rows with every control at All" % name)
            if verbose:
                print("  %-40s %4d rows (All), %d control settings" % (name, len(first_rows), len(results)))
    return count, failures


def main():
    p = parser(__doc__)
    add_es_args(p)
    p.add_argument("--kibana", help="Kibana URL; when set, import the dashboards there (same credentials as --es)")
    p.add_argument("--force", action="store_true", help="run even if the cluster has other Postfix or Exchange data streams")
    p.add_argument("--cleanup", action="store_true", help="delete the test data streams and index templates at the end")
    p.add_argument("-v", "--verbose", action="store_true", help="print each query's row count")
    args = p.parse_args()
    es = es_client(args)

    streams = es.request("GET", "/_data_stream/" + ",".join(WATCHED))["data_streams"]
    others = [s["name"] for s in streams if s["name"] not in TEST_STREAMS]
    if others and not args.force:
        print("This cluster has other Postfix or Exchange data streams (%s). The synthetic data would appear in their dashboards.\n"
              "Use a throwaway cluster, or pass --force." % ", ".join(others))
        return 2

    now = dt.datetime.now(dt.timezone.utc)
    lines, eol, exs, bad, errors = index_data(es, now)
    # with other Postfix or Exchange data in the cluster the headline numbers include it, so only compare them without
    expected = None if others else expected_counts(lines, eol, exs)
    failures = ["synthetic line not fully parsed: %s -> %s" % b for b in bad[:20]]
    failures += ["bulk error: %s" % e for e in errors[:5]]
    print("indexed %d Postfix lines, %d Exchange Online rows and %d Exchange Server events" % (len(lines), len(eol), len(exs)))

    checked = 0
    for name in DASHBOARD_FILES:
        print(name)
        count, fails = check_dashboard(es, os.path.join(REPO, name), now, args.verbose,
                                       expected and expected.get(name))
        checked += count
        failures += ["%s: %s" % (name, f) for f in fails]

    if args.kibana:
        kb = Client(args.kibana, args.api_key, args.user, args.password, args.insecure, {"kbn-xsrf": "true"})
        for name in DASHBOARD_FILES:
            with open(os.path.join(REPO, name), "rb") as f:
                r = kb.upload("/api/saved_objects/_import?overwrite=true", name, f.read())
            if not r.get("success"):
                failures.append("Kibana import of %s: %s" % (name, json.dumps(r.get("errors"))[:500]))
        print("imported %d dashboards into %s" % (len(DASHBOARD_FILES), args.kibana))

    if args.cleanup:
        for stream in TEST_STREAMS:
            es.request("DELETE", "/_data_stream/" + stream, allow_404=True)
            es.request("DELETE", "/_index_template/" + stream, allow_404=True)
        print("deleted %s" % ", ".join(TEST_STREAMS))

    for f in failures:
        print("FAIL " + f)
    print("%d queries checked, %d failures" % (checked, len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
