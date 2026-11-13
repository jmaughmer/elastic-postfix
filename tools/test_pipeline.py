#!/usr/bin/env python3
"""Run the README's sample log lines through postfix_elastic_pipeline.json and check the parsed fields.

The pipeline is sent inline to POST _ingest/pipeline/_simulate, so nothing is installed or indexed and the
script is safe to point at any cluster. It needs Elasticsearch 8.19 or later.

    python tools/test_pipeline.py --es http://localhost:9200

Exit status is 0 when every check passes, 1 otherwise.
"""
import datetime as dt
import json
import sys

from common import MISSING, add_es_args, es_client, get, load_pipeline, parser, readme_samples, simulate


def startswith(prefix):
    f = lambda v: isinstance(v, str) and v.startswith(prefix)
    f.desc = "starts with %r" % prefix
    return f


def endswith(suffix):
    f = lambda v: isinstance(v, str) and v.endswith(suffix)
    f.desc = "ends with %r" % suffix
    return f


def dec31_year():
    """BSD timestamps have no year: the pipeline moves a date more than two days in the future back a year."""
    now = dt.datetime.now(dt.timezone.utc)
    this_year = dt.datetime(now.year, 12, 31, 23, 59, 58, tzinfo=dt.timezone.utc)
    year = now.year - 1 if this_year > now + dt.timedelta(days=2) else now.year
    f = lambda v: isinstance(v, str) and v.startswith("%d-12-31T23:59:58" % year)
    f.desc = "is Dec 31 %d" % year
    return f


# (text that appears in exactly one sample's message, {field: expected value, MISSING, or a predicate})
CHECKS = [
    # null sender
    ("ABCD1234567890: from=<>, size=2345", {"postfix.null_sender": True, "email.from.address": MISSING, "postfix.activity": "queued"}),
    ("1234ABCD: from=<sender@example.net>, size=1234", {"postfix.size": 1234, "postfix.activity": "queued"}),
    # NOQUEUE is not a queue ID; unknown is not a hostname
    ("RCPT from client.example.com[192.0.2.10]", {"email.local_id": MISSING, "source.domain": "client.example.com", "postfix.activity": "rejected",
                                                 "postfix.reason": "Relay access denied", "postfix.reason_category": "Relay denied",
                                                 "postfix.status_code": 550, "postfix.status_code_enhanced": "5.7.1"}),
    ("blocked using zen.spamhaus.org", {"email.local_id": MISSING, "source.domain": MISSING, "postfix.dnsbl_domain": "zen.spamhaus.org",
                                        "postfix.reason_category": "Blocklisted or poor reputation"}),
    # each delivery agent fills its own response field
    ("postfix/local[4001]", {"postfix.local_response": "delivered to mailbox", "postfix.smtp_response": MISSING, "postfix.activity": "sent",
                             "postfix.reason": MISSING}),
    ("postfix/pipe[3001]", {"postfix.pipe_response": "delivered via dovecot-lda service", "postfix.smtp_response": MISSING}),
    ("postfix/error[4002]", {"postfix.error_response": startswith("delivery temporarily suspended: connect to mx.example.org"),
                             "postfix.smtp_response": MISSING, "postfix.activity": "deferred", "destination.domain": "mx.example.org",
                             "destination.ip": "203.0.113.5", "postfix.reason_category": "Connection timed out"}),
    ("postfix/smtp[45678]", {"postfix.smtp_response": "250 OK", "postfix.activity": "sent", "destination.domain": "mx.example.org",
                             "postfix.delay": 0.8, "postfix.delay_transmission": 0.4}),
    # smtpd session counters
    ("xforward=1 mail=1", {"postfix.cmd_xforward": 1, "postfix.cmd_count": 6, "event.type": ["connection", "end"], "event.action": "disconnect"}),
    # header_checks and milter actions
    ("postfix/cleanup[6001]", {"postfix.cleanup_action": "reject", "event.type": ["denied"], "event.outcome": "failure",
                               "postfix.activity": "rejected", "postfix.status_code_enhanced": "5.7.1",
                               "postfix.reason_category": "Spam or content policy"}),
    ("postfix/cleanup[6002]", {"postfix.cleanup_action": "hold", "postfix.activity": "held"}),
    ("milter-reject: END-OF-MESSAGE", {"postfix.milter_result": "reject", "postfix.activity": "rejected", "postfix.smtp_stage": "END-OF-MESSAGE"}),
    # next-hop queue IDs
    ("postfix/smtp[7005]", {"postfix.queued_as": "5B6C7D8E9F", "destination.ip": "127.0.0.1", "destination.domain": MISSING}),
    ("postfix/local[4003]", {"postfix.queued_as": "5B6C7D8E9F"}),
    # timestamps
    ("connect from unknown[203.0.113.76]", {"@timestamp": endswith("T10:00:00.000-04:00")}),
    ("connect from unknown[203.0.113.77]", {"@timestamp": dec31_year()}),
    ("connect from unknown[203.0.113.65]", {"@timestamp": startswith("2026-10-14T10:01:14.123"), "postfix.activity": MISSING,
                                           "event.type": ["connection", "start"]}),
    ("connect from unknown[203.0.113.75]", {"process.name": "postfix/smtpd", "event.action": "connect"}),
    # failure reasons
    ("postfix/smtp[7007]", {"postfix.activity": "bounced", "postfix.reason": startswith("host mx.example.org[203.0.113.5] said: 550 5.1.1"),
                            "postfix.reason_category": "Unknown or disabled recipient", "postfix.status_code": 550,
                            "postfix.status_code_enhanced": "5.1.1", "postfix.smtp_stage": "RCPT TO", "postfix.dsn": "5.1.1",
                            "event.outcome": "failure"}),
    ("postfix/smtp[7008]", {"postfix.activity": "deferred", "destination.domain": "mx.example.net", "destination.ip": "198.51.100.25",
                            "destination.port": 25, "postfix.reason_category": "Connection timed out"}),
    ("postfix/smtp[7009]", {"postfix.status_code": 452, "postfix.status_code_enhanced": "4.2.2", "postfix.reason_category": "Mailbox full"}),
    ("status=expired, returned to sender", {"postfix.activity": "expired", "postfix.reason": "returned to sender",
                                            "postfix.reason_category": "Expired in queue"}),
    ("postfix/qmgr[23456]: 4F2A1B3C4D: to=<user@example.org>", {"postfix.activity": "deferred", "postfix.reason": "mail transport unavailable",
                                                               "postfix.reason_category": "Delivery suspended or transport unavailable"}),
    # other daemons and milters
    ("Trusted TLS connection established", {"tls.version": "1.3", "tls.version_protocol": "tls", "tls.cipher": "TLS_AES_256_GCM_SHA384"}),
    ("SASL LOGIN authentication failed", {"postfix.sasl_method": "LOGIN", "postfix.sasl_username": "info@example.org", "log.level": "warning"}),
    ("DKIM verification successful", {"opendkim.verification": "successful", "email.local_id": "1234ABCD"}),
    ("opendmarc[5601]: 4F2A1B3C4D: example.net pass", {"opendmarc.result": "pass", "opendmarc.domain": "example.net"}),
    ("prepend Received-SPF: Pass", {"policyd_spf.result": "Pass", "policyd_spf.identity": "mailfrom", "source.ip": "198.51.100.30"}),
]
NOMATCH_SAMPLE = "something postfix has never logged"
IGNORED_TAG_PREFIXES = ("_geoip_database_unavailable",)


def main():
    p = parser(__doc__)
    add_es_args(p)
    p.add_argument("-v", "--verbose", action="store_true", help="print every parsed document")
    args = p.parse_args()

    docs = readme_samples()
    results = simulate(es_client(args), load_pipeline(), docs)
    parsed = []
    for d, r in zip(docs, results):
        if "doc" not in r:
            print("FAIL simulate returned an error for %r: %s" % (d["_source"].get("message"), json.dumps(r)[:500]))
            return 1
        parsed.append((d["_source"].get("message", ""), r["doc"]["_source"]))

    failures, count = [], 0
    for message, src in parsed:
        if args.verbose:
            print(json.dumps(src, indent=1, sort_keys=True))
        tags = [t for t in src.get("tags", []) if not t.startswith(IGNORED_TAG_PREFIXES)]
        expected = ["_grok_postfix_nomatch"] if NOMATCH_SAMPLE in message else []
        count += 1
        if tags != expected:
            failures.append("%s\n    tags %s, expected %s" % (message, tags, expected))
        if "error" in src:
            failures.append("%s\n    error: %s" % (message, src["error"]))

    for needle, fields in CHECKS:
        hits = [(m, s) for m, s in parsed if needle in m]
        if len(hits) != 1:
            failures.append("%r matches %d samples, expected 1" % (needle, len(hits)))
            continue
        message, src = hits[0]
        for field, want in fields.items():
            count += 1
            got = get(src, field)
            if callable(want):
                ok, desc = want(got), want.desc
            else:
                ok, desc = got == want, repr(want)
            if not ok:
                failures.append("%s\n    %s = %r, expected %s" % (message, field, got, desc))

    for f in failures:
        print("FAIL " + f)
    print("%d of %d checks passed on %d samples" % (count - len(failures), count, len(parsed)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
