#!/usr/bin/env bash
# Compile on the dev node in a disposable compiler layer of the job image.
# The serving image and host packages remain unchanged. Output must be new.
set -euo pipefail
BUILD_DIR=${1:?provide a new absolute build directory under /data/ch-native/}
case "$BUILD_DIR" in
  /data/ch-native/*) ;;
  *) printf '%s\n' 'build directory must be under /data/ch-native/' >&2; exit 2 ;;
esac
case "$BUILD_DIR" in
  *'/../'*|*'/..'|*'/./'*|*'/.'|*'//'*) printf '%s\n' 'build directory must be canonical' >&2; exit 2 ;;
esac
if [ -e "$BUILD_DIR" ]; then
  printf '%s\n' 'build directory already exists; refusing to overwrite' >&2
  exit 2
fi
BUILD_IMAGE=$(cat /data/image)
SOURCE=/data/src/dt_cloud/chstore/native/hot_l1_stream.cpp
test -f "$SOURCE"
mkdir -p "$BUILD_DIR"
sha256sum "$SOURCE" > "$BUILD_DIR/source.sha256"
docker image inspect --format '{{.Id}}' "$BUILD_IMAGE" > "$BUILD_DIR/image-id.txt"
docker run --rm -v /data/src:/source:ro -v "$BUILD_DIR:/build" \
  --entrypoint bash "$BUILD_IMAGE" -c '
    set -euo pipefail
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends g++
    g++ --version > /build/compiler.txt
    g++ -O3 -DNDEBUG -std=c++17 -Wall -Wextra -Werror \
      -static-libstdc++ -static-libgcc \
      /source/dt_cloud/chstore/native/hot_l1_stream.cpp -o /build/hot-l1-stream
    sha256sum /build/hot-l1-stream > /build/binary.sha256
  '
printf '%s\n' "$BUILD_DIR/hot-l1-stream"
