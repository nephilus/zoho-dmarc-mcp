# Zoho DMARC MCP

Implementation is currently at the private tunnel connectivity gate. The collector,
SQLite reporting tools, release pipeline, and Helm chart have not been implemented
or published yet. The hello proof reads no email and has no database access.

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
