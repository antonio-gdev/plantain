# Configure network access

Most users running tests against public HTTPS services do not need to change
Plantain’s network policy. The default standard mode validates the URL, hostname,
DNS answers, and public destination before allowing supported outbound access.

Change network policy only when your approved environment requires a stricter
allowlist or an explicitly governed private target.

## Where network policy is configured

Network policy is not configurable in the dashboard. Set it in the process
environment or in the project’s local `.env` before Plantain starts.

When you launch with `--no-dotenv`, only values already present in the process are
used. Restart the dashboard after changing process-level network policy.
Session-entered agent and result settings do not modify these controls.

## Start with the destination

Classify the target before changing settings:

| Destination | Typical action |
| --- | --- |
| Public HTTPS or WSS | Use standard mode; add a deny rule only when required |
| Public target under restricted egress | Use exact allowed hosts and real egress enforcement |
| Private or reserved target | Add exact approval and explicit private-network opt-in |
| Loopback HTTP for local development | Use the limited local insecure-HTTP exception |
| Database | Use the separate exact database `host:port` policy |
| Private Jira / Zephyr | Use the Zephyr-specific private-network controls |

Do not solve a blocked request by broadly allowing private networks or wildcard
hosts.

## Standard mode

`PLANTAIN_NETWORK_MODE=standard` permits supported public HTTPS and WSS targets
after safety validation.

Plantain still:

- applies blocked-host rules first;
- resolves and checks every destination address;
- rejects private, reserved, link-local, and otherwise unsafe addresses;
- validates each redirect;
- strips credentials when a redirect changes origin;
- blocks HTTPS-to-HTTP downgrade;
- enforces request, response, redirect, timeout, and connection limits.

An optional API-method allowlist can further restrict which supported HTTP methods
may run.

## Restricted mode

Restricted mode requires exact allowed-host rules. It also requires a real
deployment egress firewall or proxy and the corresponding attestation:

```text
PLANTAIN_NETWORK_MODE=restricted
PLANTAIN_ALLOWED_HOSTS=api.example.com
PLANTAIN_EGRESS_CONTROL_ENFORCED=true
```

`PLANTAIN_EGRESS_CONTROL_ENFORCED=true` reports that the external control exists.
It does not create, configure, or verify a firewall by itself.

Use a `*.` host pattern only when the complete subdomain boundary has been reviewed.
Deny rules always win over allow rules.

## Private application targets

A private or reserved UI/API target requires both:

- an exact approved host rule;
- `PLANTAIN_ALLOW_PRIVATE_NETWORKS=true`.

Restricted environments also retain the real egress-enforcement requirement.
Plantain performs fresh DNS and address checks rather than trusting an earlier
resolution indefinitely.

Private access is an explicit exception. Do not enable it merely because a
hostname failed validation.

## Local insecure HTTP

Plain HTTP or WS is limited to explicitly approved loopback development. The
runtime must have:

- `PLANTAIN_ENV=local`;
- the exact loopback host in the allowlist;
- private-network opt-in;
- `PLANTAIN_ALLOW_INSECURE_LOCAL_HTTP=true`.

This exception is not available for a remote application host.

The Plantain dashboard’s own loopback launcher is separate: it always binds the
local dashboard to `127.0.0.1`.

## Database destinations

Database access does not reuse the HTTP host allowlist.

Authorize each database endpoint as an exact `host:port` value through:

```text
PLANTAIN_DB_ALLOWED_TARGETS
```

Private or loopback database addresses additionally require:

```text
PLANTAIN_DB_ALLOW_PRIVATE_NETWORKS=true
```

Non-local database access retains deployment egress requirements. Plantain
revalidates DNS for each pool acquisition and preserves driver-specific TLS
verification, connection timeouts, bounded pools, and rollback-on-return.

The limited insecure database TLS exception applies only to loopback in a
local/development/test environment and must be explicitly enabled. Never use it
for a remote database.

## Zephyr destinations

Zephyr has separate private-network and insecure-HTTP controls:

- `PLANTAIN_ZEPHYR_ALLOW_PRIVATE_NETWORKS`;
- `PLANTAIN_ZEPHYR_ALLOW_INSECURE_HTTP`.

Private Server or Data Center hosts require explicit approval and deployment
egress controls. Prefer verified HTTPS.

See [Configure results](integrations.md) for the user workflow and supported
Zephyr edition.

## Browser navigation

UI testing applies target policy to page navigation and network behavior. Plantain
blocks unsupported private targets, unsafe redirects, and service-worker bypass
rather than allowing the browser to escape the validated route.

Browser tracing stays disabled by default because trace archives can retain page
and network data.

## Swagger and OpenAPI references

Schema documents are subject to URL, DNS, byte, and parsing limits. External schema
references are allowed only within the supported and approved policy surface.

If a schema depends on a blocked or unsupported external reference, Plantain fails
closed instead of silently ignoring that part of the contract.

## When a request is blocked

1. Confirm the intended URL and environment reference.
2. Determine whether the target is public, private, loopback, database, or Zephyr.
3. Read the sanitized policy error.
4. Ask the network or security owner for the narrowest exact approval.
5. Configure the matching policy family.
6. In restricted deployments, verify the real firewall or proxy exists before
   setting its attestation.
7. Retry as a new run.

Do not disable TLS verification, use an IP-address shortcut, embed credentials in
the URL, or add an unreviewed wildcard.

## Review checklist

- Is the destination necessary for this test?
- Is the scheme secure?
- Is the host rule exact?
- Are private-network flags disabled unless required?
- Does a real external egress control support restricted mode?
- Are database and Zephyr rules configured in their own policy families?
- Will redirects and external schema references stay inside approved boundaries?

For the broader data boundary, read
[Security and privacy](../operations/security.md).
