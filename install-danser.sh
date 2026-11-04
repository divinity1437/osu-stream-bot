#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

version="0.12.1"
archive="/tmp/opencode/danser-${version}-linux.zip"
url="https://github.com/Wieku/danser-go/releases/download/${version}/danser-${version}-linux.zip"
expected="b3aa87e9cd2b8697d643c4834dd9b671682cb9800843733122e4dce1ad3794d0"

mkdir -p /tmp/opencode tools/danser
curl -fL --retry 3 -o "$archive" "$url"

actual="$(sha256sum "$archive" | awk '{print $1}')"
if [[ "$actual" != "$expected" ]]; then
  echo "danser archive checksum mismatch" >&2
  exit 1
fi

unzip -q -o "$archive" -d tools/danser
chmod +x tools/danser/danser tools/danser/danser-cli
if [[ -d tools/danser/ffmpeg ]]; then
  bundled="tools/danser/ffmpeg-bundled"
  if [[ -e "$bundled" ]]; then
    bundled="tools/danser/ffmpeg-bundled-${version}-$(date +%s)"
  fi
  mv tools/danser/ffmpeg "$bundled"
fi
mkdir -p tools/danser/settings tools/danser/songs tools/danser/skins tools/danser/replays tools/danser/videos
cp danser-stream.json tools/danser/settings/stream.json
cp danser-stream.json tools/danser/settings/stream-cpu.json

echo "danser ${version} installed in tools/danser"
