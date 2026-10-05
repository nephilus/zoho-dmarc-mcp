# Zoho DMARC MCP

Downloads DMARC attachments from an explicitly configured Zoho folder into SQLite
and exposes bounded read-only reporting through OpenAI Secure MCP Tunnel. Raw
attachments and message bodies are discarded. The application and chart are
available as release `0.1.3`; real-account and host acceptance are operator checks.

## Development

Use the checked-in devcontainer with Python 3.13 and the locked MCP SDK. On Podman,
select the intended connection in the current shell and pass `--docker-path podman`
to the devcontainer CLI:

```sh
devcontainer up --docker-path podman --workspace-folder .
devcontainer exec --docker-path podman --workspace-folder . uv run --frozen python -m pytest -q
```

The runtime image uses a separate non-root user. Development dependencies live in
a named volume rather than a Windows virtual environment.

## Connectivity proof

`poc/hello.py` exposes only `dmarc_hello` and `/healthz`. Its Streamable HTTP endpoint
is `/mcp`, bound to loopback for an official tunnel-client sidecar.

`poc/deployment.yaml` requires replacing `PROOF_IMAGE` with the loaded proof image
and creating the `openai-tunnel` Secret first. It creates no public Service,
Ingress, or database listener. Both image dependencies are pinned by digest.

The host-only helper `python ops/configure_tunnel.py` opens a one-time loopback
form for the tunnel ID and runtime key. It creates a Secret directly through
kubectl stdin, without saving credential files or using shell arguments. It does
not overwrite an existing Secret. Close the form after creation. Never commit
private values, actual reports, or credential material.

The gate passes only when the intended ChatGPT/dot account discovers and calls
`dmarc_hello`; local protocol tests alone do not establish account connectivity.

## Install

Use a rootful Podman machine on Windows and select its connection explicitly with
`CONTAINER_CONNECTION`. Availability requires an awake host and owner sign-in.

```sh
minikube start -p zoho-dmarc --driver=podman --container-runtime=cri-o --cpus=4 --memory=4096 --keep-context
helm repo add zoho-dmarc https://nephilus.github.io/zoho-dmarc-mcp/
helm repo update
helm upgrade --install zoho-dmarc zoho-dmarc/zoho-dmarc-mcp --version 0.1.3 --kube-context zoho-dmarc --namespace zoho-dmarc --create-namespace -f private-values.yaml --wait
```

OCI alternative: substitute `oci://ghcr.io/nephilus/charts/zoho-dmarc-mcp` for the
chart argument. Release packages contain the application digest; source charts
require an explicit `image.digest`. Both application and tunnel images are pinned.
The chart deploys one Pod with collector, MCP server and official tunnel containers,
`Recreate` updates and a retained 10 GiB RWO PVC. No Service or Ingress is needed.

## Private setup

Create existing Kubernetes Secrets in the release namespace:

| Secret | Keys | Access |
|---|---|---|
| zoho-oauth | client_id, client_secret, refresh_token | Collector only |
| zoho-dmarc-config | config.json | Collector only |
| openai-tunnel | api_key, tunnel_id | Tunnel only |

Example `config.json` (replace privately):

```json
{"owner":"owner@example.test","account":"123","folder":"456","region":"com","domains":["example.test"],"scan_seconds":1800}
```

`private-values.yaml` contains Secret references, never credential values:

```yaml
zoho:
  secretName: zoho-oauth
config:
  secretName: zoho-dmarc-config
  remediationUtc: ""
tunnel:
  secretName: openai-tunnel
```

Create a Self Client in the [Zoho API Console](https://api-console.zoho.com/) as the
mailbox owner. Request only `ZohoMail.accounts.READ,ZohoMail.messages.READ`. Run
`python ops/configure_zoho.py --region com`, enter client ID, secret and a freshly
generated authorization code in its expiring loopback form. It exchanges the code
in memory and creates the Secret through stdin without displaying or writing tokens.
[Zoho Self Client instructions](https://www.zoho.com/developer/oauth/self-client/authorization-code-flow.html)

Create a workspace-associated OpenAI tunnel and restricted runtime key with
**Tunnels Read + Use**. Run `python ops/configure_tunnel.py` for local entry. Enable
developer mode in the intended ChatGPT workspace and create a private Tunnel plugin.
[OpenAI tunnel guide](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)

## Tools and counting

`dmarc_summary(start,end,page_size,cursor)` and `dmarc_failures(...)` require explicit
UTC bounds ending in `Z` or `+00:00`, a positive window no longer than 365 days,
and pages of 1–100 items. `dmarc_report_details(report_id,...)` exposes bounded rows
and provenance. `dmarc_health()` reports freshness, incomplete scans, retries,
rejections, conflicts and backup export status. Responses are capped at 256 KiB.

Counts cover whole reports overlapping the requested window, disclose their actual
periods, and are never prorated. Receipt times remain separate. Failures mean both
aligned DKIM and SPF failed. Logical identity is reporter, report ID, domain and
period. Identical XML replay is idempotent. Different XML fingerprints create a
visible conflict and exclude all variants from totals; formatting changes also
create conflicts deliberately. An optional private remediation boundary labels
pre-change, post-change and spanning reports without implying causality.

Coverage describes observed reporters and uncovered periods only. Unknown reporters
cannot be inferred, and gaps do not prove delivery failure. Coverage truncation is
explicit; reduce the window or page size when the response limit is reached. Stale
history remains queryable. No DNS changes or enforcement recommendations are made.

## Collection and parser limits

Initial collection backfills the folder. Hourly scans apply a seven-day receipt
overlap locally; weekly reconciliation checks all available history. Pagination
continues to an empty page and a second full inventory must agree before completion.
Changes, deadlines and pending retries leave scans incomplete. Discovered work is
durable, and ingestion commits atomically with completion. Retries are bounded with
capped exponential backoff. Transient incomplete scans retry after one minute, doubling
up to 15 minutes; successful scans resume hourly polling. Credential and ownership
errors retain hourly polling. Rejections retain reasons and provenance, not contents.

Mailbox calls are GET-only; OAuth exchange uses POST. Account ownership is validated
before collection. XML, gzip and ZIP containing XML are supported, including legacy
and [RFC 9990](https://www.rfc-editor.org/rfc/rfc9990.html) DMARC namespaces.
Foreign namespace extensions cannot replace core fields. Unsafe paths,
symlinks, nested archives, DTDs/entities, invalid fields and unapproved domains are
rejected. Defaults: 10 MiB input, 50 MiB expansion, 16 entries, 100:1 ratio, depth
32, 100,000 records and a 30-second worker deadline, with Linux CPU/memory limits.
The allowlist applies to published policy domains; header-from subdomains of an
allowed policy domain are accepted.

Collector holds an exclusive writer lock and persistent WAL connection. MCP mounts
SQLite read-only and receives no Zoho credentials. Containers use UID 10001,
read-only root filesystems, dropped capabilities and no service-account token.

## Backups and operations

Collector creates consistent daily backups with SQLite's backup API and integrity
checks. On Windows, run `pwsh -File ops/windows_host.ps1 -RegisterTask` once. The
single owner logon/hourly task starts only the designated machine/profile, exports
completed backups with `kubectl cp`, verifies SHA256 and retains host copies for
30 days under `.private/host-backups`. Export failures are recorded separately,
locally and in MCP health when cluster access permits.

Operator commands are separate from MCP:

```sh
python -m zoho_dmarc backup-list
python -m zoho_dmarc restore --source /private/dmarc-TIMESTAMP.sqlite --target /private/restored.sqlite
python -m zoho_dmarc backup
python -m zoho_dmarc reconcile
```

Reconciliation requires exclusive writer ownership: stop the collector first.
Operator backup opens the live source read-only and uses SQLite's backup API.
Restore requires a matching `.sha256` file and a new target and checks integrity.
Verify restoration in a disposable instance before relying on backups. Helm
uninstall retains the PVC; cluster deletion does not. Verify a host backup before
deleting the cluster.

## CI and releases

CI uses the development container, tests parser/collector/storage, smoke-tests the
runtime image, checks Helm boundaries, and deploys synthetic Zoho/tunnel fixtures
to disposable Kind, including Pod replacement and persistence. Fixtures are mounted
only during CI and are excluded from the production image.
Manual CI input `published_install=true` with `release_version` performs fresh Helm installs
from both the Pages index and GHCR OCI chart using synthetic fixtures. These tests
use Helm 3 executable post-renderers; the production chart also works with Helm 4.

Trusted `v*` tags publish `ghcr.io/nephilus/zoho-dmarc-mcp` and the packaged chart to
`oci://ghcr.io/nephilus/charts/zoho-dmarc-mcp`. The identical package is added to
the retained GitHub Pages Helm index. Evidence records commit, chart version, both
image digests and checks. Actions are SHA-pinned; permissions default to read-only,
with publishing rights only in the trusted release job. PRs receive no publishing
credentials. Real account, reboot and backup acceptance are operator checks.
The retained `gh-pages` branch can be republished independently with the
`Republish Helm Pages` workflow, without rebuilding or replacing releases.



