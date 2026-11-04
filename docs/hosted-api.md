# Hosted API and remote MCP

One service owns login jobs and encrypted sessions. The deployed API uses one
Uvicorn process. A database transaction allows one browser job at a time; up to
16 jobs wait globally and four per client. Independent clients send requests to
this same service. HTTP queries run concurrently (eight upstream requests max).
Do not start API replicas until renewal locking is distributed too.

All `/v1/*` and `/mcp` requests require `Authorization: Bearer <client key>`.
Keys identify separate owners: an ID from another client returns 404. Only
`GET /healthz` is public. TLS terminates at Caddy; the container port binds to
host loopback. Access logs are disabled in the application.

| REST operation | Input / behavior |
|---|---|
| `GET /v1/catalog` | Allowed read-only official paths and parameters |
| `POST /v1/auth/jobs` | JSON `cpf`, `password`; optional `Idempotency-Key` header; returns 202 and `job_id` |
| `GET /v1/auth/jobs/{job_id}` | `queued`, `running`, `succeeded` with `session_id`, or `failed` with sanitized `code` |
| `GET /v1/sessions/{session_id}` | Validity, benefit list and eligible medical reports; no tokens |
| `POST /v1/sessions/{session_id}/renew` | Silent SSO reauthorization through HTTP; may require new login |
| `POST /v1/sessions/{session_id}/request` | JSON `path`, optional `query`; upstream JSON or a PDF |
| `POST /v1/sessions/{session_id}/credentials` | API headers/validity, only with `export_credentials=true`; no SSO cookies |
| `DELETE /v1/sessions/{session_id}` | Delete local session; does not revoke already exported INSS tokens |

Keep `{cpf}` **literal** in paths; the service fills it from the session.
For credits, substitute dates using DD-MM-YYYY and request at most 366 days per
call. For PDFs, substitute an NB from session metadata. Requests cannot change
the host, method, headers or supply arbitrary query parameters. Redirects are
not followed. Successful JSON responses omit authentication token fields.

Example request body (no credentials):

```json
{"path":"/hiscreServices/historicocreditos/{cpf}/01-01-2026/31-10-2026","query":{}}
```

JSON responses contain `upstream_status` and `data`; non-200 upstream statuses
return a sanitized error, preserving the upstream status as metadata. One failed
query does not discard another query or the session. PDFs stream back as a
bounded binary response (8 MB max; JSON 2 MB max).

MCP Streamable HTTP is at `/mcp`, with the same client Bearer header. Tools:
`inss_catalog`, `inss_auth_start`, `inss_auth_status`, `inss_session_status`,
`inss_session_renew`, `inss_session_close`, `inss_request`. MCP never exposes the
credential export operation. PDFs return as embedded PDF resources. Claude Code
and other clients supporting authenticated Streamable HTTP can connect directly.
OAuth discovery/authorization for browser-only connector catalogs is not included.

Queued credentials and sessions are encrypted with Fernet in SQLite. The queue
expires after ten minutes; passwords are removed from storage at claim and
discarded from the isolated worker at completion. Failed logins are not retried.
A worker is limited to 240 seconds. Session retention is capped at six hours;
this is a storage limit, **not** a promise of upstream SSO validity. Retained
cookies permit HTTP renewal while the upstream SSO session remains valid. No
password is retained for reauthentication. Authentication rejection (401/403)
permits one coordinated renewal and one retry of the read-only request.

Deploy using `deploy/Dockerfile`, `deploy/compose.yml` and `deploy/seccomp.json`.
Set real configuration in the protected server file `secrets/service.env`; the
example contains no usable credentials. Back up that encryption key securely if
sessions must survive recovery; losing it makes stored sessions unreadable.
The browser remains native Google Chrome 156.0.8078.79 through ManagedBrowser/CDP,
default context, Xvfb and the reference font files. Fast interactions and image
batching with per-round fallback remain enabled. The container keeps the Chrome
sandbox; the supplied seccomp profile enables its namespace syscalls.
The seccomp file is the [Playwright 1.64.1 profile](https://github.com/microsoft/playwright/blob/v1.64.1/utils/docker/seccomp_profile.json).

## Validation on 2026-11-04

The ARM64 service passed real queued login, REST queries and the MCP SDK's
Streamable HTTP initialization, tool listing and three tool calls using the same
session. Three simultaneous authenticated REST requests returned HTTP 200.
The encrypted session survived restarting only the API container. Inspection
inside its PID namespace found no Chrome, Xvfb or Node processes after login.
Exported API credentials were accepted by a separate Windows HTTP client (200).
Closing the test session made subsequent session lookup return 404.

Two full hosted logins succeeded (42.032 s and 35.297 s measured at the client);
one stopped with `captcha_selection_not_confirmed`, without automatic replay.
These are individual observations, not a latency or reliability benchmark.
One hosted HTTP renewal succeeded in 6.083 s and issued new usable credentials.
Further renewals returned `sso_interaction_required` (409); the old API credentials
remained usable. The service reports that condition rather than launching a
browser or retaining a password to log in again. Renewal beyond actual access
token expiry and production load/capacity remain unverified. PDF transport and
response limits are covered offline; the hosted live check requested JSON only.
