# elastic-postfix

Elasticsearch ingest pipeline for parsing Postfix and OpenDKIM syslog messages.

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

## Validate with _simulate

Run a quick simulation with representative messages before rollout:

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
		}
	]
}
```

The last document uses a null envelope sender (`from=<>`), as produced by bounces and DSNs. Verify that the result contains `postfix.null_sender: true` and no `email.from.address` field.

## Notes

- The pipeline preserves raw input in `event.original`.
- Unmatched Postfix/OpenDKIM variants no longer fail the entire pipeline; unmatched messages receive a `_grok_*_nomatch` tag instead.
- Syslog timestamps are parsed as UTC unless the `@timestamp` field is already set upstream by the shipper.
- Empty envelope senders (common in bounces and DSNs) are handled by setting `postfix.null_sender: true` and omitting `email.from.address`, rather than substituting a placeholder address.
- `postfix_elastic_pipeline.json` uses Kibana Console triple-quote string syntax. It must be pasted directly into Kibana Dev Tools; it is **not** a standalone JSON file that can be loaded by the Elasticsearch REST API or other tooling without first converting the triple-quoted strings to standard JSON escaped strings.
- `postfix_component_template.json` is standard JSON and can be applied with any Elasticsearch client in addition to Kibana Dev Tools. Apply it before indexing so explicit mappings are used rather than dynamic field inference.
