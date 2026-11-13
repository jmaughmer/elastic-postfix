"""Synthetic Exchange Online message trace and Exchange Server message tracking documents for test_dashboards.py.

The documents have the shape Elastic's integrations produce after their ingest pipelines
(microsoft_exchange_online_message_trace 2.4 "log" and microsoft_exchange_server 1.6 "messagetracking"), so they
can be indexed directly; the integrations' pipelines aren't run.

Two kinds of mail are generated:

- each system's own traffic: Exchange Online rows (one per recipient) with every Graph message trace status, and
  Exchange Server RECEIVE events followed by DELIVER, SEND, DEFER, FAIL and DSN events;
- Postfix mail handed on to Exchange: every Postfix delivery to corp.example (on-premises Exchange) gets a
  RECEIVE and DELIVER on the Exchange server, and every delivery to outlook.example (the Exchange Online tenant)
  gets a delivered message trace row, with the same Message-ID, so the mail flow dashboards' cross-system
  panel has matches.
"""
import datetime as dt
import random
import re
import uuid

EOL_STREAM = "logs-microsoft_exchange_online_message_trace.log-dashboardtest"
EXS_STREAM = "logs-microsoft_exchange_server.messagetracking-dashboardtest"
ONPREM_DOMAIN = "corp.example"
TENANT_DOMAIN = "outlook.example"
EXCHANGE_SERVERS = [("exch01", "exch01.corp.example", "10.10.0.11"), ("exch02", "exch02.corp.example", "10.10.0.12")]
POSTFIX_IPS = {"mail01": "192.0.2.25", "mail02": "192.0.2.26"}
EXTERNAL = ["partner.example", "vendor.example", "gmail.example", "news.example"]
SUBJECTS = ["Quarterly report", "Re: Meeting tomorrow", "Invoice 2026-114", "Your order has shipped", "Password expiry notice",
            "FW: Project plan", "Weekly digest", "Lunch?", "Contract draft v3", "Security alert"]
# Graph message trace status values and how often they occur
EOL_STATUSES = [("delivered", 80), ("failed", 5), ("filteredAsSpam", 5), ("quarantined", 3), ("pending", 3), ("expanded", 2), ("gettingStatus", 2)]
EOL_FAILED = {"failed", "quarantined", "filteredasspam"}
SCHEMA = "15.02.1544.004"


def _iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (t.microsecond // 1000)


def _common(stream, dataset):
    ns = stream.rsplit("-", 1)[1]
    return {"data_stream": {"type": "logs", "dataset": dataset, "namespace": ns},
            "event": {"dataset": dataset}, "ecs": {"version": "8.17.0"}}


def eol_row(t, sender, recipient, subject, size, message_id, trace_id, status, from_ip, to_ip=""):
    """One Exchange Online message trace row, as the integration's pipeline leaves it."""
    doc = _common(EOL_STREAM, "microsoft_exchange_online_message_trace.log")
    s = status.lower()
    doc["@timestamp"] = _iso(t)
    doc["event"].update({"category": ["email"], "type": ["info"],
                         "outcome": "success" if s == "delivered" else "failure" if s in EOL_FAILED else "unknown"})
    doc["microsoft"] = {"online_message_trace": {
        "Received": _iso(t), "SenderAddress": sender, "RecipientAddress": recipient, "Subject": subject, "Status": status,
        "Size": size, "MessageId": message_id, "MessageTraceId": trace_id, "FromIP": from_ip, "ToIP": to_ip}}
    doc["email"] = {"from": {"address": [sender]}, "to": {"address": [recipient]}, "subject": subject, "local_id": trace_id,
                    "message_id": message_id, "delivery_timestamp": _iso(t), "attachments": {"file": {"size": size}},
                    "direction": "inbound" if recipient.endswith("@" + TENANT_DOMAIN) and not sender.endswith("@" + TENANT_DOMAIN)
                    else "outbound" if not recipient.endswith("@" + TENANT_DOMAIN) else "intra-org"}
    doc["source"] = {"ip": from_ip, "user": {"email": sender, "id": sender, "name": sender.split("@")[0], "domain": sender.split("@")[1]},
                     "domain": sender.split("@")[1]}
    doc["destination"] = {"user": {"email": recipient, "id": recipient, "name": recipient.split("@")[0], "domain": recipient.split("@")[1]},
                          "domain": recipient.split("@")[1]}
    if to_ip:
        doc["destination"]["ip"] = to_ip
    doc["related"] = {"user": [recipient, sender, recipient.split("@")[0], sender.split("@")[0]]}
    return doc


def exs_event(t, server, eventid, source, sender, recipients, subject, size, message_id, network_id, internal_id,
              client=None, connector="", status="", context="", direction="Originating"):
    """One Exchange Server message tracking event, as the integration's pipeline leaves it."""
    host, fqdn, ip = server
    doc = _common(EXS_STREAM, "microsoft_exchange_server.messagetracking")
    client_ip, client_name = client or (ip, fqdn)
    fields = [_iso(t), client_ip, client_name, ip, fqdn, context, connector, source, eventid, internal_id, message_id, network_id,
              ";".join(recipients), status, str(size), str(len(recipients)), "", "", subject, sender, sender, "", direction, "", "", "",
              "S:DeliveryPriority=Normal", "Email", str(uuid.UUID(int=random.getrandbits(128))), SCHEMA]
    doc["@timestamp"] = _iso(t)
    doc["message"] = ",".join('"%s"' % f if "," in f else f for f in fields)
    doc["event"]["original"] = doc["message"]
    doc["client"] = {"ip": client_ip, "domain": client_name}
    doc["server"] = {"ip": ip, "domain": fqdn}
    doc["host"] = {"name": host, "hostname": host, "os": {"family": "windows", "platform": "windows", "type": "windows"}}
    doc["network"] = {"bytes": size}
    doc["email"] = {"message_id": message_id, "local_id": network_id, "to": {"address": recipients}, "subject": subject,
                    "from": {"address": [sender]}, "sender": {"address": [sender]}, "direction": direction}
    doc["microsoft"] = {"exchange": {
        "sourcecontext": context, "connectorid": connector, "source": source, "eventid": eventid, "internalmessageid": internal_id,
        "networkmessageid": network_id, "recipientstatus": status, "recipientcount": len(recipients), "returnpath": sender,
        "customdata": "S:DeliveryPriority=Normal", "transporttraffictype": "Email", "schemaversion": SCHEMA}}
    return doc


def from_postfix(lines, rnd):
    """Exchange's view of Postfix mail delivered to the on-premises and Exchange Online domains."""
    msgid, sender, size = {}, {}, {}
    for l in lines:
        host, m = l["host"]["name"], l["message"]
        q = re.search(r": ([0-9A-F]{10}): message-id=<([^>]+)>", m)
        if q:
            msgid[(host, q.group(1))] = q.group(2)
        q = re.search(r": ([0-9A-F]{10}): from=<([^>]*)>, size=(\d+)", m)
        if q:
            sender[(host, q.group(1))], size[(host, q.group(1))] = q.group(2), int(q.group(3))
    # recipients Postfix delivered to each Exchange domain, per message
    handed = {}
    for l in lines:
        host, m = l["host"]["name"], l["message"]
        q = re.search(r"^(\S+) .*?: ([0-9A-F]{10}): to=<([^>]+)>.*status=sent", m)
        if not q:
            continue
        key, rcpt = (host, q.group(2)), q.group(3)
        domain = rcpt.split("@")[1]
        if key in msgid and key in sender and domain in (ONPREM_DOMAIN, TENANT_DOMAIN):
            handed.setdefault((key, domain), []).append((dt.datetime.fromisoformat(q.group(1)), rcpt))
    eol, exs = [], []
    for ((host, qid), domain), deliveries in sorted(handed.items()):
        key = (host, qid)
        t = min(d[0] for d in deliveries) + dt.timedelta(seconds=rnd.uniform(0.5, 3))
        rcpts = sorted({d[1] for d in deliveries})
        # the message as Exchange sees it: Postfix's Message-ID and sender, a subject, and a size with Exchange's added headers
        mid, frm, sz, subject = "<%s>" % msgid[key], sender[key], size[key] + rnd.randint(800, 3000), rnd.choice(SUBJECTS)
        if domain == ONPREM_DOMAIN:
            server, nid, iid = rnd.choice(EXCHANGE_SERVERS), str(uuid.UUID(int=rnd.getrandbits(128))), str(rnd.randint(10**13, 10**14))
            client = (POSTFIX_IPS.get(host, "192.0.2.25"), host + ".example.org")
            exs.append(exs_event(t, server, "RECEIVE", "SMTP", frm, rcpts, subject, sz, mid, nid, iid, client,
                                 "%s\\Default Frontend %s" % (server[0].upper(), server[0].upper()), "", "08DCB1F6A3C2D4E5;250 2.6.0 Queued mail for delivery", "Incoming"))
            for k, rcpt in enumerate(rcpts):
                exs.append(exs_event(t + dt.timedelta(seconds=rnd.uniform(0.2, 2) + k * 0.1), server, "DELIVER", "STOREDRIVER", frm, [rcpt], subject, sz,
                                     mid, nid, iid, None, "", "250 2.1.5 Recipient OK", "MDB:Mailbox Database 01", "Incoming"))
        else:
            trace = str(uuid.UUID(int=rnd.getrandbits(128)))
            for rcpt in rcpts:
                eol.append(eol_row(t, frm, rcpt, subject, sz, mid, trace, "delivered", "203.0.113.%d" % (25 if host == "mail01" else 26)))
    return eol, exs


def native(now, rnd, messages=600):
    """Each system's own traffic over the last 24 hours."""
    eol, exs = [], []
    statuses = [s for s, w in EOL_STATUSES for _ in range(w)]
    for i in range(messages):
        t = now - dt.timedelta(seconds=rnd.uniform(300, 86000))
        subject = rnd.choice(SUBJECTS)
        size = int(rnd.lognormvariate(10.8, 1.5)) + 900
        if i % 2 == 0:
            # Exchange Online: tenant users sending out, and outside mail coming in
            if rnd.random() < 0.5:
                frm = "user%d@%s" % (rnd.randint(1, 40), TENANT_DOMAIN)
                rcpts = ["%s%d@%s" % (rnd.choice(["sales", "info", "pat", "lee"]), rnd.randint(1, 9), rnd.choice(EXTERNAL)) for _ in range(rnd.choice([1, 1, 2, 3]))]
                from_ip = "40.107.%d.%d" % (rnd.randint(1, 250), rnd.randint(1, 250))
            else:
                frm = "%s@%s" % (rnd.choice(["news", "billing", "noreply", "support", "promo"]), rnd.choice(EXTERNAL))
                rcpts = ["user%d@%s" % (rnd.randint(1, 40), TENANT_DOMAIN) for _ in range(rnd.choice([1, 1, 2]))]
                from_ip = "198.51.100.%d" % rnd.randint(1, 250)
            mid = "<%s@%s.prod.outlook.example>" % (uuid.UUID(int=rnd.getrandbits(128)), rnd.choice(["am6pr01mb", "db9pr02mb", "vi1pr04mb"]))
            trace = str(uuid.UUID(int=rnd.getrandbits(128)))
            for r in rcpts:
                status = rnd.choice(statuses)
                to_ip = "" if r.endswith("@" + TENANT_DOMAIN) else "203.0.113.%d" % rnd.randint(1, 250)
                eol.append(eol_row(t, frm, r, subject, size, mid, trace, status, from_ip, to_ip))
        else:
            # Exchange Server: a user's message submitted from a mailbox, then delivered, sent out, deferred or failed per recipient
            server = rnd.choice(EXCHANGE_SERVERS)
            frm = "staff%d@%s" % (rnd.randint(1, 60), ONPREM_DOMAIN)
            rcpts = []
            for _ in range(rnd.choice([1, 1, 1, 2, 3])):
                if rnd.random() < 0.5:
                    rcpts.append("staff%d@%s" % (rnd.randint(1, 60), ONPREM_DOMAIN))
                else:
                    rcpts.append("%s%d@%s" % (rnd.choice(["sales", "info", "pat"]), rnd.randint(1, 9), rnd.choice(EXTERNAL)))
            mid = "<%s@%s>" % (uuid.UUID(int=rnd.getrandbits(128)), server[1])
            nid, iid = str(uuid.UUID(int=rnd.getrandbits(128))), str(rnd.randint(10**13, 10**14))
            def ev(tt, eventid, source, rcpt_list, connector="", status="", context="", direction="Originating"):
                exs.append(exs_event(tt, server, eventid, source, frm, rcpt_list, subject, size, mid, nid, iid, None, connector, status, context, direction))

            ev(t, "RECEIVE", "STOREDRIVER", rcpts, "", "", "MDB:Mailbox Database 01")
            ev(t + dt.timedelta(milliseconds=200), "SUBMIT", "STOREDRIVER", rcpts)
            for k, r in enumerate(rcpts):
                tk = t + dt.timedelta(seconds=1 + k * 0.3)
                if r.endswith("@" + ONPREM_DOMAIN):
                    if rnd.random() < 0.06:
                        ev(tk, "FAIL", "ROUTING", [r], "", "550 5.1.1 RESOLVER.ADR.RecipNotFound; not found")
                        ev(tk + dt.timedelta(milliseconds=500), "DSN", "DSN", [frm], "", "", "", "Originating")
                    else:
                        ev(tk, "DELIVER", "STOREDRIVER", [r], "", "250 2.1.5 Recipient OK", "MDB:Mailbox Database 01")
                else:
                    x = rnd.random()
                    if x < 0.08:
                        ev(tk, "DEFER", "SMTP", [r], "Outbound to Internet", "451 4.4.0 Primary target IP address responded with: \"421 4.4.2 Connection dropped.\"")
                        if rnd.random() < 0.5:
                            ev(tk + dt.timedelta(minutes=15), "SEND", "SMTP", [r], "Outbound to Internet", "250 2.0.0 OK")
                    elif x < 0.13:
                        ev(tk, "FAIL", "SMTP", [r], "Outbound to Internet", "550 5.4.316 Message expired, connection timeout(SocketError)")
                        ev(tk + dt.timedelta(milliseconds=500), "DSN", "DSN", [frm])
                    else:
                        ev(tk, "SEND", "SMTP", [r], "Outbound to Internet", "250 2.0.0 OK")
    return [d for d in eol if d["@timestamp"] < _iso(now)], [d for d in exs if d["@timestamp"] < _iso(now)]


def generate(now, postfix_lines, seed=11):
    rnd = random.Random(seed)
    random.seed(seed)
    eol, exs = native(now, rnd)
    e2, x2 = from_postfix(postfix_lines, rnd)
    return eol + e2, exs + x2


def index_templates():
    """Index templates for the two test data streams: the integrations' own field types for their non-ECS fields, and the
    stack's ecs@mappings for ECS fields, as a package installed by Fleet gets."""
    eol = {"properties": {"microsoft": {"properties": {"online_message_trace": {"properties": {
        "EndDate": {"type": "date_nanos"}, "StartDate": {"type": "date_nanos"}, "Received": {"type": "date_nanos"},
        "FromIP": {"type": "keyword"}, "ToIP": {"type": "keyword"}, "Index": {"type": "long"}, "MessageId": {"type": "keyword"},
        "MessageTraceId": {"type": "keyword"}, "Organization": {"type": "keyword"}, "RecipientAddress": {"type": "keyword"},
        "SenderAddress": {"type": "keyword"}, "Size": {"type": "long"}, "Status": {"type": "keyword"}, "Subject": {"type": "keyword"}}}}}}}
    keyword = {"type": "keyword"}
    exs = {"properties": {"microsoft": {"properties": {"exchange": {"properties": dict(
        {k: keyword for k in ["sourcecontext", "connectorid", "source", "eventid", "networkmessageid", "internalmessageid",
                              "recipientstatus", "relatedrecipientaddress", "reference", "returnpath", "messageinfo", "tenantid",
                              "customdata", "transporttraffictype", "logid", "schemaversion", "logtype"]},
        recipientcount={"type": "long"}, originalclientip={"type": "ip"}, originalserverip={"type": "ip"})}}}}}
    out = {}
    for stream, mappings in ((EOL_STREAM, eol), (EXS_STREAM, exs)):
        out[stream] = {"index_patterns": [stream], "data_stream": {}, "priority": 500,
                       "composed_of": ["logs@mappings", "logs@settings", "ecs@mappings"],
                       "ignore_missing_component_templates": ["ecs@mappings"],
                       "template": {"settings": {"index": {"default_pipeline": "_none"}}, "mappings": mappings},
                       "_meta": {"description": "synthetic data for tools/test_dashboards.py in the elastic-postfix repo"}}
    return out


def expected_mail_flow(postfix_lines, eol, exs):
    """Headline numbers of the mail flow dashboards, counted from the generated data."""
    msgs = [(l["host"]["name"], l["message"]) for l in postfix_lines]
    sizes = {}
    for h, m in msgs:
        q = re.search(r": ([0-9A-F]{10}): from=<[^>]*>, size=(\d+)", m)
        if q and "(queue active)" in m:
            sizes[(h, q.group(1))] = int(q.group(2))
    receive = [d for d in exs if d["microsoft"]["exchange"]["eventid"] == "RECEIVE"]
    status = [d["microsoft"]["online_message_trace"]["Status"].lower() for d in eol]
    events = [d["microsoft"]["exchange"]["eventid"] for d in exs]
    pf_delivered = sum(1 for _, m in msgs if "status=sent" in m)
    pf_failed = sum(1 for _, m in msgs if "status=bounced" in m or "status=expired" in m or "NOQUEUE: reject:" in m)
    total_bytes = sum(sizes.values()) + sum(d["network"]["bytes"] for d in receive) + sum(d["microsoft"]["online_message_trace"]["Size"] for d in eol)
    ids = {}
    for _, m in msgs:
        q = re.search(r"message-id=<([^>]+)>", m)
        if q:
            ids.setdefault(q.group(1), set()).add("Postfix")
    for d in eol:
        ids.setdefault(d["email"]["message_id"].strip("<>"), set()).add("Exchange Online")
    for d in exs:
        ids.setdefault(d["email"]["message_id"].strip("<>"), set()).add("Exchange Server")
    return {
        "kpi-messages": len(sizes) + len(receive) + len(eol),
        "kpi-volume": round(total_bytes / 1048576.0, 1),
        "kpi-delivered": pf_delivered + sum(1 for s in status if s == "delivered") + sum(1 for e in events if e in ("DELIVER", "SEND")),
        "kpi-not-delivered": pf_failed + sum(1 for s in status if s in EOL_FAILED) + sum(1 for e in events if e == "FAIL"),
        "cross-system-messages": cross_system_check(sum(1 for v in ids.values() if len(v) > 1)),
    }


def cross_system_check(matches):
    """Every message seen by two systems in the generated data came through Postfix, so each row of the panel
    (it shows up to 50) has to list Postfix among its systems."""
    def check(names, rows):
        want = min(50, matches)
        systems = names.index("systems") if "systems" in names else None
        without = [r for r in rows if systems is None or "Postfix" not in (r[systems] if isinstance(r[systems], list) else [r[systems]])]
        if len(rows) != want or without:
            return False, "%d rows, %d without Postfix; expected %d rows, all with Postfix" % (len(rows), len(without), want)
        return True, ""
    return check
