# Security policy

HermesCloak's job is to keep real personal data out of cloud-bound LLM requests. A report that a real
value can reach the cloud is the most important kind of report this project can get.

## Reporting a vulnerability

Please report privately through GitHub: **Security → Report a vulnerability** on this repository
(private vulnerability reporting). Don't open a public issue for a leak path.

A useful report includes:

- the hermes-agent version and HermesCloak version (`plugin.yaml`);
- the `MODE` (`shadow` / `enforce`) and which path the value took (request, stream, tool call, auxiliary call);
- a **synthetic** input that reproduces it. Never send real personal data, real keys or audit logs from a live deployment.

## What counts

- a real value detected by a recognizer that still reaches a cloud-bound request;
- a request sent in clear in `enforce` mode when an interception point is missing or the plugin failed to load;
- a token that is not restored before a tool runs or the conversation is persisted;
- vault contents readable by other local users, or written in clear when encryption is configured.

Detection misses on names that are on neither the gazetteer nor found by NER are known limits
(see "Honest limits" in the README), but reports with synthetic examples are still welcome as issues.

## Supported versions

Fixes land on the latest release only.
