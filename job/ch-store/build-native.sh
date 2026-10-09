#!/usr/bin/env bash
# Compile one `chstore/native/*.cpp` on the dev node in a disposable compiler
# layer of the job image (the serving image and host packages stay unchanged):
#   build-native.sh /data/ch-native/NEW-DIR SOURCE-STEM   # e.g. hot_frequency, catalog_delta
# The binary is the stem with hyphens (`catalog-delta`). Output must be new.
set -euo pipefail
BUILD_DIR=${1:?provide a new absolute build directory under /data/ch-native/}
STEM=${2:?provide the native source stem, e.g. catalog_delta}
case "$BUILD_DIR" in
  /data/ch-native/*) ;;
  *) printf '%s\n' 'build directory must be under /data/ch-native/' >&2; exit 2 ;;
esac
case "$BUILD_DIR" in
  *'/../'*|*'/..'|*'/./'*|*'/.'|*'//'*) printf '%s\n' 'build directory must be canonical' >&2; exit 2 ;;
esac
if [[ ! $STEM =~ ^[a-z][a-z0-9_]*$ ]]; then
  printf '%s\n' 'source stem must match [a-z][a-z0-9_]*' >&2
  exit 2
fi
if [ -e "$BUILD_DIR" ]; then
  printf '%s\n' 'build directory already exists; refusing to overwrite' >&2
  exit 2
fi
BUILD_IMAGE=$(cat /data/image)
SOURCE=/data/src/dt_cloud/chstore/native/$STEM.cpp
BINARY=${STEM//_/-}
test -f "$SOURCE"
mkdir -p "$BUILD_DIR"
sha256sum "$SOURCE" > "$BUILD_DIR/source.sha256"
cp "$SOURCE" "$BUILD_DIR/source.cpp"
docker image inspect --format '{{.Id}}' "$BUILD_IMAGE" > "$BUILD_DIR/image-id.txt"
docker run --rm -v "$BUILD_DIR:/build" --entrypoint bash "$BUILD_IMAGE" -c "
    set -euo pipefail
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends g++
    g++ --version > /build/compiler.txt
    g++ -O3 -DNDEBUG -std=c++17 -Wall -Wextra -Werror -static-libstdc++ -static-libgcc /build/source.cpp -o /build/$BINARY
    sha256sum /build/$BINARY > /build/binary.sha256
  "
printf '%s\n' "$BUILD_DIR/$BINARY"
