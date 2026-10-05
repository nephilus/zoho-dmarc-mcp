#!/bin/sh
set -eu
version=v4.3.0
archive="helm-${version}-linux-amd64.tar.gz"
directory="$(mktemp -d)"
trap 'rm -rf "$directory"' EXIT
python - "$directory" "$archive" <<'PY'
import hashlib, pathlib, sys, urllib.request
directory, name = pathlib.Path(sys.argv[1]), sys.argv[2]
base = 'https://get.helm.sh/' + name
data = urllib.request.urlopen(base, timeout=60).read()
expected = urllib.request.urlopen(base + '.sha256sum', timeout=30).read().decode().split()[0]
if hashlib.sha256(data).hexdigest() != expected:
    raise SystemExit('Helm checksum mismatch')
(directory / name).write_bytes(data)
PY
tar -xzf "$directory/$archive" -C "$directory"
mkdir -p "$HOME/.local/bin"
cp "$directory/linux-amd64/helm" "$HOME/.local/bin/helm"
