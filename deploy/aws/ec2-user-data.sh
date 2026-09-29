#!/bin/bash
# =====================================================================
# EC2 "User data" script (Amazon Linux 2023). Paste it into
# EC2 → Launch instance → Advanced details → User data.
# It installs Docker + Docker Compose + unzip + git. Output: /var/log/cloud-init-output.log
# =====================================================================
set -euxo pipefail
dnf update -y
dnf install -y docker git unzip
systemctl enable --now docker
usermod -aG docker ec2-user

# Docker Compose v2 and Buildx plugins (needed for "docker compose up --build")
mkdir -p /usr/local/lib/docker/cli-plugins
curl -sSL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-$(uname -m)" \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
BUILDX_VERSION=v0.19.3
ARCH=$( [ "$(uname -m)" = "aarch64" ] && echo arm64 || echo amd64 )
curl -sSL "https://github.com/docker/buildx/releases/download/${BUILDX_VERSION}/buildx-${BUILDX_VERSION}.linux-${ARCH}" \
  -o /usr/local/lib/docker/cli-plugins/docker-buildx
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose /usr/local/lib/docker/cli-plugins/docker-buildx

docker --version
docker compose version
echo "EC2 bootstrap complete ✓"
