# elastic-postfix

Elasticsearch ingest pipeline for parsing Postfix, OpenDKIM, OpenDMARC and policyd-spf syslog messages.

## Install Pipeline

Apply the pipeline in Kibana Dev Tools:

```http
PUT _ingest/pipeline/logs-postfix@custom
<paste contents of postfix_elastic_pipeline.json>
```

## Install Mapping Template

Apply the mapping template in Kibana Dev Tools:

```http
PUT _component_template/logs-postfix@custom
<paste contents of postfix_component_template.json>
```

## Route Documents Through the Pipeline

Neither object takes effect until something points at it. One way is an index template for `logs-postfix-*` data streams that uses the component template and makes the pipeline the default:

```http
PUT _index_template/logs-postfix
{
	"index_patterns": ["logs-postfix-*"],
	"data_stream": {},
	"priority": 200,
	"composed_of": ["logs@mappings", "logs@settings", "logs-postfix@custom", "ecs@mappings"],
	"ignore_missing_component_templates": ["ecs@mappings"],
	"template": {
		"settings": {
			"index.default_pipeline": "logs-postfix@custom"
		}
	}
}
```

Then ship to a data stream such as `logs-postfix-default`. This replaces the built-in `logs@default-pipeline` for those data streams. Alternatively, set `pipeline: logs-postfix@custom` on the Filebeat or Elastic Agent input.

## Install Dashboard

`postfix_kibana_dashboard.ndjson` is a dashboard ("Postfix Mail Overview") for Kibana 8.19 and later, built from ES|QL panels over `logs-postfix-*`: delivery and rejection KPIs and trends over time, a breakdown per activity (sent, deferred, bounced, expired, rejected), why mail was deferred, bounced or rejected, messages by size and outcome, top rejection reasons and rejected clients, DNSBL hits, SASL failures, TLS versions, top domains, message sizes (total volume, average and largest message, volume over time, size ranges, and sender domains by volume), top sender and recipient addresses by message count and data volume, relay delays, warnings by daemon, DKIM/DMARC/SPF results, and messages the pipeline couldn't fully parse.

Import it in Kibana under **Stack Management → Saved Objects → Import**, choosing the NDJSON file. It also creates a `logs-postfix-*` data view (id `logs-postfix`) that the host control uses. Re-importing with "Automatically overwrite conflicts" replaces both.

The new panels and the Activity control read `postfix.activity`, `postfix.reason` and `postfix.reason_category`, so install the current pipeline and component template first. A component template only applies to backing indices created after it, so roll the data stream over (`POST logs-postfix-default/_rollover`) or wait for the first new line to arrive; until one of those happens the panels fail with "Unknown column [postfix.activity]". Lines indexed before the new pipeline have no activity and don't appear in the activity sections.

### Filters

Six controls sit at the top:

- **Sender** and **Recipient**: one address, or `All`. Each list holds the 1,000 most frequent addresses in the dashboard's time range (`email.from.address`, `email.to.address`).
- **Host**: `host.name`, set by the shipper. Every panel follows it.
- **Activity**: `All`, `Not delivered` (everything except sent), or one of `sent`, `deferred`, `bounced`, `expired`, `rejected`, `discarded`, `held`. It reads `postfix.activity`.
- **Min size (KB)** and **Max size (KB, 0 = any)**: a size range in KB from `postfix.size`. `0` means no limit on that side.

All but Host are ES|QL variable controls (`?sender`, `?recipient`, `?activity`, `?min_size_kb`, `?max_size_kb`), not data-view controls. They pick one value at a time and only change the panels whose query uses them:

| Panels | Sender | Recipient | Activity | Size | Counts |
| --- | --- | --- | --- | --- | --- |
| Four tiles at the top (delivered, deferred, bounced, rejected) | yes | yes | no | yes | log lines, joined to their message |
| Mail activity | no | yes | yes | no | log lines: one per delivery attempt or reject |
| Why mail was not delivered | no | yes | yes | no | log lines |
| Messages by size and activity | yes | yes | yes | yes | messages: lines joined on host and queue ID |
| Reject panels (rejections over time, top rejection reasons, rejected senders and clients, DNSBL) | yes | yes | no | no | log lines |
| Message size panels (messages queued, data queued, size ranges, sender domains, top senders) | yes | no | no | yes | messages |
| Top recipients by messages and by volume | yes | yes | no | yes | messages |
| Top recipient domains, delay by relay | no | yes | no | no | log lines |

The panels not listed (SASL, TLS, warnings, DKIM, DMARC, SPF, parse health) follow only Host and the time range.

Why Sender doesn't reach every panel: Postfix logs the sender and size on the queue line (`from=`, `size=`) and each outcome on a delivery line (`to=`, `status=`), tied together only by the queue ID. A filter on the sender therefore has to be applied after joining a message's lines. The tiles and the *Messages by size and activity* section do that join; the line-level sections don't, because joining every line for every panel would make the whole dashboard slow. To see what happened to one sender's mail and why, select the sender and read *Messages by size and activity*: it has the outcome counts, the reasons, and the list of messages with recipients, DSN codes and responses. Reject lines carry both addresses themselves, so reject panels follow both controls directly.

In *Messages by size and activity*, a message matches an activity when any of its recipients had it (or the selected recipient, when one is selected), and its *outcome* is the worst result seen: bounced, expired, rejected, discarded or held, then sent, then deferred. A message queued before the dashboard's time range has no size or sender in range, so it drops out when a size limit or a sender is set. Rejects that never got a queue ID count as one message each with no size. These panels and the tiles group every line in the time range by queue ID, so they are the slowest on the dashboard; shorten the time range if they lag.

Things to know about the controls:

- To add a size step, edit the control and add a number to its list; the queries take any number.
- An address outside the 1,000 most frequent isn't in the Sender or Recipient list. Raise the `LIMIT` in the control's query, or narrow the time range.
- The message size panels count each message once. A deferred message logs a new `queue active` line on every retry, and those repeats are folded together on host and queue ID.
- Postfix reuses short queue IDs, so over a long time range two messages on one host can share an ID and be joined as one. Setting `enable_long_queue_ids = yes` in Postfix avoids that.
- Charts over time size their buckets to the time range (about 40 buckets), so a one-hour view shows minutes and a 30-day view shows most of a day per bar.

### Activity panels

*Mail activity* breaks the flow down per activity: counts over time, a summary table (lines, messages, recipients, average, median, 95th percentile and maximum delay, and where the delay was spent: before the queue manager, in the queue, connection setup, transmission), 95th percentile delay over time, attempts by delay range, and the top recipient domains, relays, recipients and rejected senders with a count per activity.

*Why mail was not delivered* covers deferred, bounced, expired, rejected, discarded and held mail:

- the most common reasons by category, overall and over time
- DSN status codes (`postfix.dsn`, or the enhanced code of a reject) and SMTP reply codes (`postfix.status_code`)
- failed deliveries by relay or remote server
- the most common response texts, with addresses in angle brackets masked so the same response groups together
- the 100 newest lines with host, queue ID, sender (rejects only), recipient, relay, DSN, SMTP code, the SMTP command being answered, delay, category and the full response

*Messages by size and activity* shows how many messages match and their volume, message counts and size statistics by outcome, size ranges by outcome, the reasons matching messages were not delivered, the top senders with a count per outcome, and the 100 newest matching messages with size, recipients, activities, DSN codes and responses.

## Install Combined Mail Flow Dashboard

`mail_flow_kibana_dashboard.ndjson` is a second dashboard ("Mail Flow Overview (Postfix + Exchange)", Kibana 8.19+) that combines this data stream with Elastic's [Microsoft Exchange Online Message Trace](https://www.elastic.co/docs/reference/integrations/microsoft_exchange_online_message_trace) and [Microsoft Exchange Server](https://www.elastic.co/docs/reference/integrations/microsoft_exchange_server) (message tracking) integrations. It shows totals and trends over time by system, delivery outcomes by system, why Postfix mail was not delivered (reason categories and the most common responses), top senders and recipients across systems, messages seen by more than one system (matched on Message-ID), a searchable list of the latest message events with the Postfix DSN code and response, events by host, and data volume by system.

Controls at the top filter every panel by sender (`email.from.address`), recipient (`email.to.address`), host (`host.name`), queue or local ID (`email.local_id`), Message-ID (`email.message_id`) or subject (`email.subject`). The ID and subject controls search anywhere in the value, so part of an ID or subject is enough. Import it the same way, under **Stack Management → Saved Objects → Import**. It also creates a `mail-flow-logs` data view over all three sources for the controls.

Install the current component template first. It maps the few Exchange fields the dashboard queries (`microsoft.exchange.eventid`, `microsoft.online_message_trace.Size`/`Status`, `network.bytes`, `server.domain`, `email.subject`) on the Postfix indices too, so the ES|QL panels don't fail with "Unknown column" when one of the Exchange integrations isn't installed. The dashboard also reads `postfix.activity`, `postfix.reason` and `postfix.reason_category`; as with the Postfix dashboard, roll the data stream over after installing the template if those columns are reported unknown.

How the sources are lined up:

- **Messages and data volume** count one event per message: the Postfix `queue active` line (`postfix.size`), the Exchange Server `RECEIVE` event (`network.bytes`), and each Exchange Online trace row (`Size`). Exchange Online returns one row per recipient, and a message received by several Exchange servers has a `RECEIVE` on each, so those systems can count a message more than once.
- **Outcomes** map Postfix `postfix.activity` (sent, deferred, bounced/expired, and rejected/discarded/held), Exchange Online `Delivered/Pending/Failed/FilteredAsSpam/Quarantined`, and Exchange Server `DELIVER`/`SEND`/`DEFER`/`FAIL` onto delivered, deferred, failed and rejected/filtered. Postfix lines indexed before `postfix.activity` existed fall back to `postfix.status` and the reject actions, so older data still counts.
- **Failure reasons** come from Postfix only (`postfix.reason_category`, `postfix.reason`, `postfix.dsn`), so the two "Postfix:" panels need lines parsed by the current pipeline. The Exchange integrations' reason fields aren't mapped on the Postfix indices, so they aren't shown.
- **Hosts** use `host.name`, falling back to the Exchange Server `server.domain`. Exchange Online events have no host, so the host control filters them out.
- **IDs and subjects** aren't the same across systems. `email.local_id` is the Postfix queue ID, the Exchange Online `MessageTraceId`, or the Exchange Server local ID, so it finds one system's view of a message. Postfix stores the Message-ID without angle brackets and Exchange Online keeps them, so the Message-ID control lists both forms; search for part of the ID to find both. Postfix logs no subject, so the subject control filters out Postfix events.

### All-clusters version

`mail_flow_all_clusters_kibana_dashboard.ndjson` is the same dashboard ("Mail Flow Overview (Postfix + Exchange) - All Clusters (may be slow)") with every source also searched on all connected remote clusters through ES|QL cross-cluster search: each `FROM` lists the local pattern and the same pattern with `*:` in front (for example `logs-postfix-*, *:logs-postfix-*`), because `*:` matches remote clusters only. Its controls use their own `mail-flow-logs-all-clusters` data view with the same patterns, so their lists include remote values. It has its own ids, so it imports alongside the local dashboard.

- Remote clusters must already be connected to this cluster, run 8.16 or later, and have a license that covers cross-cluster search.
- Every panel queries every remote, so it can respond slowly. A remote that is down or doesn't have the indices is skipped, and its panels show a partial-results warning instead of failing.
- Each remote should have the current component template on its Postfix indices if it doesn't run both Exchange integrations.

### Local + EXAMPLE cluster version

`mail_flow_example_cluster_kibana_dashboard.ndjson` ("Mail Flow Overview (Postfix + Exchange) - Local + EXAMPLE Cluster (may be slow)") searches the local cluster and only the remote cluster whose alias is `EXAMPLE`: each `FROM` lists the local pattern and the same pattern with `EXAMPLE:` in front (for example `logs-postfix-*, EXAMPLE:logs-postfix-*`). Its controls use their own `mail-flow-logs-example` data view, and it has its own ids, so it imports alongside the other two. The same requirements apply as for the all-clusters version, for `EXAMPLE` only. The alias must match the remote cluster's name in this cluster's remote-cluster settings exactly, including case.

## Validate with _simulate

Run the sample messages through the pipeline before rollout:

```http
POST _ingest/pipeline/logs-postfix@custom/_simulate
{
	"docs": [
		{
			"_source": {
				"message": "Jun 25 12:34:56 mail01 postfix/smtpd[12345]: NOQUEUE: reject: RCPT from client.example.com[192.0.2.10]: 550 5.7.1 <user@example.org>: Relay access denied; from=<sender@example.net> to=<user@example.org> proto=ESMTP helo=<client.example.com>"
			}
		},
		{
			"_source": {
				"message": "Jun 25 12:35:02 mail01 postfix/qmgr[23456]: 1234ABCD: from=<sender@example.net>, size=1234, nrcpt=1 (queue active)"
			}
		},
		{
			"_source": {
				"message": "Jun 25 12:35:15 mail01 opendkim[34567]: 1234ABCD: DKIM verification successful"
			}
		},
		{
			"_source": {
				"message": "Jun 25 12:35:30 mail01 postfix/smtp[45678]: 1234ABCD: to=<user@example.org>, relay=mx.example.org[203.0.113.5]:25, delay=0.8, delays=0.1/0/0.3/0.4, dsn=2.0.0, status=sent (250 OK)"
			}
		},
		{
			"_source": {
				"message": "Jun 25 12:36:00 mail01 postfix/qmgr[23456]: ABCD1234567890: from=<>, size=2345, nrcpt=1 (queue active)"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:00:01 mail01 postfix/smtpd[2001]: disconnect from localhost[127.0.0.1] ehlo=1 xforward=1 mail=1 rcpt=1 data=1 quit=1 commands=6"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:00:02 mail01 postfix/smtpd[2002]: disconnect from unknown[203.0.113.51] helo=1 vrfy=0/2 quit=1 commands=2/4"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:00:03 mail01 postfix/smtpd[2003]: disconnect from mail.example.net[198.51.100.11] ehlo=2 starttls=1 mail=2 rcpt=2 data=1 bdat=1 quit=1 commands=10"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:00:04 mail01 postfix/pipe[3001]: 4F2A1B3C4D: to=<user@example.org>, relay=dovecot-lda, delay=0.2, delays=0.1/0/0/0.1, dsn=2.0.0, status=sent (delivered via dovecot-lda service)"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:00:05 mail01 postfix/smtpd[2005]: 4F2A1B3C4D: client=mail_gw.example.net[198.51.100.12]"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:00:06 mail01 postfix/local[4001]: 4F2A1B3C4D: to=<alice@example.org>, orig_to=<postmaster@example.org>, relay=local, delay=0.05, delays=0.01/0/0/0.04, dsn=2.0.0, status=sent (delivered to mailbox)"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:00:07 mail01 postfix/pipe[3002]: 4F2A1B3C4D: to=<user@example.org>, relay=dovecot, delay=0.1, delays=0/0/0/0.1, dsn=2.0.0, status=sent (delivered via dovecot service)"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:00:08 mail01 postfix/error[4002]: 4F2A1B3C4D: to=<user@example.org>, relay=none, delay=3600, delays=3599/1/0/0, dsn=4.4.1, status=deferred (delivery temporarily suspended: connect to mx.example.org[203.0.113.5]:25: Connection timed out)"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:00:09 mail01 postfix/postsuper[5001]: 4F2A1B3C4D: removed"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:01:01 mail01 postfix/cleanup[6001]: 4F2A1B3C4D: reject: header Subject: You won a prize from unknown[203.0.113.60]; from=<spam@example.net> to=<user@example.org> proto=ESMTP helo=<spammer.example.net>: 5.7.1 Message content rejected"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:01:02 mail01 postfix/cleanup[6002]: 4F2A1B3C4D: hold: header Subject: invoice from mail.example.net[198.51.100.13]; from=<a@example.net> to=<user@example.org> proto=ESMTP helo=<mail.example.net>: quarantine"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:01:03 mail01 postfix/cleanup[6003]: 4F2A1B3C4D: prepend: header Subject: cron output from local; from=<root@mail01.example.org> to=<admin@example.org>: X-Local: yes"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:01:04 mail01 postfix/smtpd[2010]: NOQUEUE: warn: RCPT from unknown[203.0.113.61]: Suspicious sender; from=<x@example.net> to=<user@example.org> proto=ESMTP helo=<x.example.net>"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:01:05 mail01 postfix/smtpd[2011]: NOQUEUE: redirect: RCPT from unknown[203.0.113.62]: <old@example.org>: Recipient address triggers REDIRECT new@example.org; from=<x@example.net> to=<old@example.org> proto=ESMTP helo=<x.example.net>"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:01:06 mail01 postfix/smtp[7001]: 4F2A1B3C4D: lost connection with mx.example.org[203.0.113.5] while performing the EHLO handshake"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:01:07 mail01 postfix/smtp[7002]: 4F2A1B3C4D: conversation with mx.example.org[203.0.113.5] timed out while sending DATA command"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:01:08 mail01 postfix/smtp[7003]: connect to mx.example.org[203.0.113.5]:25: server dropped connection without sending the initial SMTP greeting"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:01:09 mail01 postfix/smtp[7004]: server certificate verification failed for mx.example.org[203.0.113.5]:25: num=10:certificate has expired"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:01:10 mail01 postfix/anvil[8001]: statistics: max message rate 3/60s for (smtp:203.0.113.63) at Oct 14 10:00:00"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:01:11 mail01 postfix/anvil[8001]: statistics: max auth rate 12/60s for (submission:203.0.113.64) at Oct 14 10:00:00"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:01:12 mail01 opendkim[3456]: OpenDKIM Filter v2.11.0 terminating with status 0, errno = 0"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:01:13 mail01 opendkim[3456]: 4F2A1B3C4D: external host mail.example.net attempted to send as example.org"
			}
		},
		{
			"_source": {
				"message": "2026-10-14T10:01:14.123456+00:00 mail01 postfix/smtpd[2012]: connect from unknown[203.0.113.65]"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:02:01 mail01 postfix/smtpd[2013]: warning: Connection rate limit exceeded: 31 from unknown[203.0.113.66] for service smtp"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:02:02 mail01 postfix/smtpd[2014]: warning: unknown[203.0.113.67]: SASL LOGIN authentication failed: UGFzc3dvcmQ6, sasl_username=info@example.org"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:02:03 mail01 postfix/smtpd[2015]: connect from unknown[203.0.113.68]"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:02:04 mail01 postfix/smtpd[2016]: NOQUEUE: reject: RCPT from unknown[203.0.113.69]: 554 5.7.1 Service unavailable; Client host [203.0.113.69] blocked using zen.spamhaus.org; https://www.spamhaus.org/query/ip/203.0.113.69; from=<x@example.net> to=<user@example.org> proto=ESMTP helo=<x.example.net>"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:02:05 mail01 postfix/smtp[7005]: 4F2A1B3C4D: to=<user@example.org>, relay=127.0.0.1[127.0.0.1]:10024, delay=1.2, delays=0.1/0/0/1.1, dsn=2.0.0, status=sent (250 2.0.0 from MTA(smtp:[127.0.0.1]:10025): 250 2.0.0 Ok: queued as 5B6C7D8E9F)"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:02:06 mail01 postfix/smtpd[2017]: Trusted TLS connection established from mail.example.net[198.51.100.10]:41234: TLSv1.3 with cipher TLS_AES_256_GCM_SHA384 (256/256 bits) key-exchange X25519 server-signature RSA-PSS (2048 bits) server-digest SHA256 client-signature ECDSA (P-256) client-digest SHA256"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:02:07.123456 mail01 postfix/smtpd[2018]: connect from mail.example.net[198.51.100.10]"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:03:01 mail01 postfix/smtp[7006]: Verified TLS connection established to mx.example.org[203.0.113.5]:25: TLSv1.3 with cipher TLS_AES_256_GCM_SHA384 (256/256 bits) key-exchange X25519MLKEM768 server-signature ECDSA (P-384) server-digest SHA384"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:03:02 mail01 postfix/smtpd[2019]: 4F2A1B3C4D: client=unknown[203.0.113.70], sasl_method=PLAIN, sasl_username=alice@example.org"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:03:03 mail01 postfix/smtpd[2020]: disconnect from unknown[203.0.113.71] ehlo=1 auth=0/1 rset=1 quit=1 commands=2/4"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:03:04 mail01 postfix/postscreen[9001]: PASS NEW [203.0.113.72]:51234"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:03:05 mail01 postfix/cleanup[6004]: 4F2A1B3C4D: milter-reject: END-OF-MESSAGE from mail.example.net[198.51.100.10]: 5.7.1 Spam message rejected; from=<a@example.net> to=<user@example.org> proto=ESMTP helo=<mail.example.net>"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:03:06 mail01 postfix/postscreen[9001]: CONNECT from [203.0.113.72]:51234 to [192.0.2.25]:25"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:03:07 mail01 postfix/bounce[9301]: 4F2A1B3C4D: sender non-delivery notification: 5B6C7D8E9F"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:03:08 mail01 postfix/pickup[9401]: 4F2A1B3C4D: uid=0 from=<root>"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:03:09 mail01 postfix/master[1]: daemon started -- version 3.9.1, configuration /etc/postfix"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:03:10 mail01 postfix/cleanup[6005]: 4F2A1B3C4D: message-id=<abc123@example.org>"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:03:11 mail01 postfix/submission/smtpd[2030]: connect from laptop.example.net[198.51.100.40]"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:03:12 mail01 postfix/postfix-script[9501]: starting the Postfix mail system"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:03:13 mail01 postfix/trivial-rewrite[9601]: warning: do not list domain example.org in BOTH mydestination and virtual_mailbox_domains"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:04:01 mail01 postfix/smtpd[2021]: warning: hostname mail.example.net does not resolve to address 198.51.100.20: Name or service not known"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:04:02 mail01 postfix/smtpd[2022]: warning: 198.51.100.21: hostname mail.example.net verification failed: Name or service not known"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:04:03 mail01 postfix/smtpd[2023]: warning: non-SMTP command from unknown[203.0.113.73]: GET / HTTP/1.1"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:04:04 mail01 postfix/local[4003]: 4F2A1B3C4D: to=<alice@example.org>, relay=local, delay=0.02, delays=0.01/0/0/0.01, dsn=2.0.0, status=sent (forwarded as 5B6C7D8E9F)"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:04:05 mail01 postfix/qmgr[23456]: 4F2A1B3C4D: to=<user@example.org>, relay=none, delay=0.1, delays=0.1/0/0/0, dsn=4.3.0, status=deferred (mail transport unavailable)"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:04:06 mail01 postfix/tlsproxy[9101]: Anonymous TLS connection established from [203.0.113.74]:40000: TLSv1.3 with cipher TLS_AES_128_GCM_SHA256 (128/128 bits)"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:04:07 mail01 postfix/trivial-rewrite[9201]: table hash:/etc/postfix/transport(0,lock|fold_fix) has changed -- restarting"
			}
		},
		{
			"_source": {
				"message": "connect from unknown[203.0.113.75]",
				"process": {
					"name": "postfix/smtpd"
				}
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:00:00 mail01 postfix/smtpd[2024]: connect from unknown[203.0.113.76]",
				"event": {
					"timezone": "America/New_York"
				}
			}
		},
		{
			"_source": {
				"message": "Dec 31 23:59:58 mail01 postfix/smtpd[2025]: connect from unknown[203.0.113.77]"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:04:12 mail01 policyd-spf[5501]: prepend Received-SPF: Pass (mailfrom) identity=mailfrom; client-ip=198.51.100.30; helo=mail.example.net; envelope-from=user@example.net; receiver=<UNKNOWN>"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:04:13 mail01 policyd-spf[5501]: None; identity=helo; client-ip=198.51.100.31; helo=mail.example.net; envelope-from=user@example.net; receiver=<UNKNOWN>"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:04:14 mail01 policyd-spf[5501]: 550 5.7.23 Message rejected due to: SPF fail - not authorized. Please see http://www.openspf.net/Why?s=mfrom;id=user@example.net;ip=198.51.100.32;r=<UNKNOWN>"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:04:15 mail01 opendmarc[5601]: 4F2A1B3C4D: example.net pass"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:04:16 mail01 opendmarc[5601]: 4F2A1B3C4D: SPF(mailfrom): example.net pass"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:04:17 mail01 opendmarc[5601]: OpenDMARC Filter v1.4.2 starting (args: -c /etc/opendmarc.conf)"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:05:01 mail01 postfix/smtp[7007]: 4F2A1B3C4D: to=<nobody@example.org>, relay=mx.example.org[203.0.113.5]:25, delay=1.3, delays=0.1/0/0.6/0.6, dsn=5.1.1, status=bounced (host mx.example.org[203.0.113.5] said: 550 5.1.1 <nobody@example.org>: Recipient address rejected: User unknown in virtual mailbox table (in reply to RCPT TO command))"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:05:02 mail01 postfix/smtp[7008]: 4F2A1B3C4D: to=<user@example.net>, relay=none, delay=30, delays=0.1/0/30/0, dsn=4.4.1, status=deferred (connect to mx.example.net[198.51.100.25]:25: Connection timed out)"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:05:03 mail01 postfix/smtp[7009]: 4F2A1B3C4D: to=<full@example.org>, relay=mx.example.org[203.0.113.5]:25, delay=900, delays=899/0/0.5/0.5, dsn=4.2.2, status=deferred (host mx.example.org[203.0.113.5] said: 452-4.2.2 The recipient's inbox is out of storage space. 452 4.2.2 Please try again later (in reply to RCPT TO command))"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:05:04 mail01 postfix/qmgr[23456]: 4F2A1B3C4D: from=<sender@example.net>, status=expired, returned to sender"
			}
		},
		{
			"_source": {
				"message": "Oct 14 10:04:18 mail01 postfix/smtpd[2027]: something postfix has never logged"
			}
		}
	]
}
```

What to check in the response:

- No document has a `tags` field except the last one, which should be tagged `_grok_postfix_nomatch`. (On a new cluster, geoip may add `_geoip_database_unavailable_*` tags until its databases finish downloading.)
- The null-sender document (`from=<>`) has `postfix.null_sender: true` and no `email.from.address`.
- The `NOQUEUE: reject` documents have no `email.local_id`, and clients logged as `unknown[...]` have no `source.domain`.
- Local, pipe and error deliveries fill `postfix.local_response`, `postfix.pipe_response` and `postfix.error_response`; only smtp/lmtp fill `postfix.smtp_response`.
- The `disconnect ... xforward=1` document has `postfix.cmd_xforward: 1` and `event.type: ["connection", "end"]`.
- The header_checks `reject:` document has `postfix.cleanup_action: reject`, `event.type: ["denied"]` and `event.outcome: failure`.
- The relay to the content filter and the `.forward` delivery both have `postfix.queued_as: 5B6C7D8E9F`.
- `postfix.activity` is `queued` on the `queue active` lines, `sent`/`deferred`/`bounced`/`expired` on delivery lines, `rejected` on the smtpd, header_checks and milter rejects, and `held` on the `hold:` line. Lines that aren't about a message (connect, TLS, warnings) have none.
- The `status=bounced` document has `postfix.reason` starting `host mx.example.org[203.0.113.5] said: 550 5.1.1`, `postfix.reason_category: Unknown or disabled recipient`, `postfix.status_code: 550`, `postfix.status_code_enhanced: 5.1.1` and `postfix.smtp_stage: RCPT TO`.
- The `452-4.2.2` deferral has `postfix.status_code: 452` and `postfix.reason_category: Mailbox full`.
- The `relay=none` deferral with `connect to mx.example.net[198.51.100.25]:25: Connection timed out` has `destination.domain: mx.example.net`, `destination.ip: 198.51.100.25` and `postfix.reason_category: Connection timed out`.
- The `status=expired` document has `postfix.reason: returned to sender` and `postfix.reason_category: Expired in queue`.
- The `Relay access denied` reject has `postfix.reason_category: Relay denied`, and the `zen.spamhaus.org` reject has `Blocklisted or poor reputation`.
- The `event.timezone: America/New_York` document has `@timestamp` `…T10:00:00.000-04:00`, and the `Dec 31` line is dated the previous year.

## Notes

- The pipeline preserves raw input in `event.original`.
- Postfix patterns are chosen by daemon: the text after the last `/` in the syslog program name. Service names like `postfix/submission/smtpd` and multi-instance names like `postfix-out/smtp` work. Warnings and `table ... has changed` lines are parsed for any daemon.
- Unmatched Postfix, OpenDKIM, OpenDMARC and policyd-spf messages are tagged `_grok_*_nomatch`. The follow-up parsers for `client=`, `relay=`, `delays=` and the disconnect command counters tag failures (for example `_grok_postfix_relay_nomatch`) instead of aborting the pipeline.
- `@timestamp` is replaced with the syslog header time. BSD (`Oct 14 10:00:00`, optionally with fractional seconds) and ISO 8601 headers are supported. Times are read in `event.timezone` when the shipper sets it (Filebeat and Elastic Agent do with `add_locale`), otherwise as UTC.
- BSD timestamps have no year, so the current year is assumed. A result more than two days in the future is moved back a year, which handles December lines ingested in January.
- If the shipper already removed the syslog header, the program is taken from `process.name` or `log.syslog.appname`.
- Empty envelope senders (common in bounces and DSNs) are handled by setting `postfix.null_sender: true` and omitting `email.from.address`, rather than substituting a placeholder address.
- `NOQUEUE` is not stored as `email.local_id`, and `unknown` (no reverse DNS) is not stored as `source.domain`.
- `event.action` is the delivery status (`sent`, `deferred`, ...) or the access/header/milter action (`reject`, `hold`, `milter-reject`, ...), or `connect`/`disconnect` for smtpd sessions. `event.type` is `denied` for rejects, `connection` with `start`/`end` for sessions, and `info` otherwise. `event.outcome` follows the delivery status, and rejects count as `failure`.
- `postfix.activity` says in one word what happened to the mail, for filtering: the delivery status in lower case (`sent`, `deferred`, `bounced`, `expired`, and the address-verification results `deliverable`/`undeliverable`), `rejected` (smtpd, postscreen, header/body checks, milter and proxy rejects), `discarded`, `held`, or `queued` for the `queue active` line that carries the size. A deferred message logs a new `queue active` line each time it is retried.
- `postfix.reason` is the response text of anything that wasn't a success, whichever daemon logged it: the text in parentheses after `status=deferred`/`status=bounced`, or the reply given to a rejected client. The per-daemon fields (`postfix.smtp_response`, `postfix.error_response`, `postfix.status_message` and so on) are still set.
- For a failed delivery, `postfix.status_code`, `postfix.status_code_enhanced` and `postfix.smtp_stage` are the remote server's SMTP reply (`host ... said: 550 5.1.1 ... (in reply to RCPT TO command)`); for a reject they are this server's reply to the client. `postfix.dsn` is the status Postfix itself assigned to the delivery.
- When a failed delivery logs `relay=none`, `destination.domain`, `destination.ip` and `destination.port` are taken from the response text (`connect to mx.example.org[203.0.113.5]:25: ...`) if it names a server.
- `postfix.reason_category` groups `postfix.reason` into common causes (unknown recipient, mailbox full, greylisted, rate limited, blocklisted, relay denied, spam or content policy, sender authentication, connection timed out/refused/lost, DNS failure, TLS failure, message too large, expired in queue and a few more, else `Other`). It is a keyword match on the response text and DSN code where the first matching rule wins, so it is a guide for dashboards, not an exact classification. The rules are in the `postfix_reason_category` script processor.
- `postfix.queued_as` holds the next hop's queue ID (`queued as ...` in an smtp/lmtp reply, or `forwarded as ...` from local), for following a message through content filters, relays and `.forward` files.
- The OpenDMARC and policyd-spf (Python version) patterns are written from their usual log formats and haven't been checked against a wide range of real logs. Check `_grok_opendmarc_nomatch` and `_grok_policyd_spf_nomatch` after rollout.
- `postfix_elastic_pipeline.json` uses Kibana Console triple-quote string syntax. It must be pasted directly into Kibana Dev Tools; it is **not** a standalone JSON file that can be loaded by the Elasticsearch REST API or other tooling without first converting the triple-quoted strings to standard JSON escaped strings.
- `postfix_component_template.json` is standard JSON and can be applied with any Elasticsearch client in addition to Kibana Dev Tools. Apply it before indexing so explicit mappings are used rather than dynamic field inference.
