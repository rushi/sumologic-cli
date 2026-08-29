#!/usr/bin/env bash
# Downloads the latest sumologic binary for this platform into ~/.local/bin.
#   curl -fsSL https://raw.githubusercontent.com/rushi/sumologic-cli/main/install.sh | bash
set -euo pipefail

REPO="rushi/sumologic-cli"
INSTALL_DIR="${SUMOLOGIC_INSTALL_DIR:-$HOME/.local/bin}"

os="$(uname -s)"
arch="$(uname -m)"

case "$os" in
  Darwin) os_tag="darwin" ;;
  Linux) os_tag="linux" ;;
  *) echo "Unsupported OS: $os" >&2; exit 1 ;;
esac

case "$arch" in
  arm64 | aarch64) arch_tag="arm64" ;;
  x86_64 | amd64) arch_tag="x86_64" ;;
  *) echo "Unsupported architecture: $arch" >&2; exit 1 ;;
esac

target="${os_tag}-${arch_tag}"
tag="$(curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" | grep -m1 '"tag_name"' | cut -d'"' -f4)"
if [ -z "$tag" ]; then
  echo "Could not determine the latest release of ${REPO}" >&2
  exit 1
fi

asset="sumologic-${target}.tar.gz"
url="https://github.com/${REPO}/releases/download/${tag}/${asset}"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

echo "Downloading ${asset} (${tag})"
curl -fsSL "$url" -o "$tmp/$asset"
curl -fsSL "${url}.sha256" -o "$tmp/${asset}.sha256"

(cd "$tmp" && (shasum -a 256 -c "${asset}.sha256" >/dev/null || sha256sum -c "${asset}.sha256" >/dev/null))

tar -xzf "$tmp/$asset" -C "$tmp"
mkdir -p "$INSTALL_DIR"
mv "$tmp/sumologic" "$INSTALL_DIR/sumologic"
chmod +x "$INSTALL_DIR/sumologic"

echo "Installed $INSTALL_DIR/sumologic"
case ":$PATH:" in
  *":$INSTALL_DIR:"*) ;;
  *) echo "Add $INSTALL_DIR to your PATH." ;;
esac
