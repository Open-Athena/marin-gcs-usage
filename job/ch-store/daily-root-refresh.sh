#!/usr/bin/env bash
# Run detached on the node host with Docker access. Serial offline work only:
# wait -> select -> build -> selected full-source checks -> private publication.
# Reserve a dedicated nonexistent publication root for this sole coordinator.
# Does not activate serving, schedule work, delete artifacts, or sync source.
# Required: -j source-tag -m source-manifest -r registry -b native-binary
# -s selection-out -a artifact-out -p proof-out -g fresh-publication-root
# -l logical-store -B bucket-path (repeat for the complete scope).
# Optional: --data-root /data, -t 3600|1800, -w 1..7200 (source wait only).
set -euo pipefail
umask 077

fail() { printf '%s\n' "$*" >&2; exit 2; }
source_tag='' source_manifest='' registry='' binary='' selection='' artifact='' proof=''
publication='' logical_store='' data_root=/data timeout=3600 wait_seconds=7200
buckets=()
while (($#)); do
  (($# >= 2)) || fail "missing value for $1"
  key=$1 value=$2
  shift 2
  case $key in
    -j|--source-tag) source_tag=$value;; -m|--source-manifest) source_manifest=$value;;
    -r|--registry) registry=$value;; -b|--binary) binary=$value;;
    -s|--selection) selection=$value;; -a|--artifact) artifact=$value;;
    -p|--proof) proof=$value;; -g|--publication-root) publication=$value;;
    -l|--logical-store) logical_store=$value;; -B|--bucket-path) buckets+=("$value");;
    -t|--timeout-seconds) timeout=$value;; -w|--wait-seconds) wait_seconds=$value;;
    --data-root) data_root=$value;; *) fail "unknown argument: $key";;
  esac
done
[[ $source_tag =~ ^[a-z0-9][a-z0-9_-]{0,62}$ ]] || fail 'invalid source tag (1..63 safe lowercase characters)'
[[ $logical_store =~ ^[a-z][a-z0-9_]*$ ]] || fail 'invalid logical store identifier'
[[ $timeout == 1800 || $timeout == 3600 ]] || fail 'native timeout must be 1800 or 3600 seconds'
[[ $wait_seconds =~ ^[1-9][0-9]{0,3}$ ]] && ((wait_seconds <= 7200)) || fail 'source wait must be 1..7200 seconds'
[[ $data_root == /* && -d $data_root && ! -L $data_root ]] || fail 'data root must be an existing absolute non-root directory'
data_root=$(cd "$data_root" && pwd -P)
[[ $data_root != / ]] || fail 'data root must be an existing absolute non-root directory'
paths=("$source_manifest" "$registry" "$binary" "$selection" "$artifact" "$proof" "$publication")
seen=()
for path in "${paths[@]}"; do
  [[ $path == "$data_root/"* && $path != *$'\n'* && $path != */../* && $path != */./* && $path != */.. && $path != */. && $path != */ && ! -L $path ]] || fail 'all paths must be distinct owned paths within data root'
  [[ -d ${path%/*} ]] || fail 'all path parents must already exist'
  parent=$(cd "${path%/*}" && pwd -P)
  [[ $parent == "$data_root" || $parent == "$data_root/"* ]] || fail 'path parent escapes data root'
  for previous in "${seen[@]}"; do [[ $previous != "$parent/${path##*/}" ]] || fail 'all paths must be distinct owned paths within data root'; done
  seen+=("$parent/${path##*/}")
done
[[ -f $registry && -f $binary && -x $binary && -f $data_root/image && ! -L $data_root/image ]] || fail 'registry, executable binary, and image file are required'
((${#buckets[@]} >= 1 && ${#buckets[@]} <= 6)) || fail 'declare the complete 1..6 bucket paths'
seen=()
for bucket in "${buckets[@]}"; do
  [[ $bucket =~ ^[a-z0-9][a-z0-9._-]*$ && $bucket != . && $bucket != .. ]] || fail 'invalid bucket path'
  for previous in "${seen[@]}"; do [[ $previous != "$bucket" ]] || fail 'duplicate bucket path'; done
  seen+=("$bucket")
done
fresh() { [[ ! -e $1 && ! -L $1 ]] || fail 'output or publication root already exists; nothing overwritten'; }
for path in "$selection" "$artifact" "$proof" "$publication"; do fresh "$path"; done
image=$(<"$data_root/image")
[[ -n $image && $image != *$'\n'* && $image != -* && $image != *[[:space:]]* ]] || fail 'image file must contain one nonempty image reference'
source_id=$(docker inspect --format '{{.Id}}' "ch-job-$source_tag")
[[ $source_id =~ ^[a-f0-9]{64}$ ]] || fail 'source job must resolve to an immutable Docker ID'
printf '%s\n' 'wait source' >&2
started=$(command date +%s) delay=5
while :; do
  state=$(docker inspect --format '{{.State.Status}} {{.State.Running}} {{.State.ExitCode}} {{.State.OOMKilled}}' "$source_id")
  case $state in
    'exited false 0 false') break;;
    running\ true\ *\ false) [[ $state =~ ^running\ true\ [0-9]+\ false$ ]] || fail 'invalid source Docker state';;
    exited\ false\ *|*\ true) printf '%s\n' 'source job failed or was OOM-killed; no downstream work' >&2; exit 1;;
    *) fail 'invalid source Docker state';;
  esac
  now=$(command date +%s)
  remaining=$((wait_seconds - (now - started)))
  ((now >= started && remaining > 0)) || { printf '%s\n' 'source wait deadline exceeded; no downstream work' >&2; exit 124; }
  pause=$delay
  ((pause <= remaining)) || pause=$remaining
  sleep "$pause"
  delay=$((delay * 2))
  ((delay <= 60)) || delay=60
done
[[ -f $source_manifest && ! -L $source_manifest ]] || fail 'successful source job did not produce a manifest file'
size=$(wc -c < "$source_manifest")
((size > 0 && size <= 65536)) || fail 'source manifest must be nonempty and at most 64 KiB'
container=(docker run --rm --network host -v "$data_root:$data_root" -e "PYTHONPATH=$data_root/src" --entrypoint python3 "$image")
run_cli() { "${container[@]}" -u -m dt_cloud.cli "$@" >/dev/null; }
fresh "$selection"
printf '%s\n' 'select registry' >&2
run_cli ch-hot-registry-select -i "$source_manifest" -l "$logical_store" -o "$selection" -r "$registry"
printf '%s\n' 'validate selected literals and bucket scope' >&2
"${container[@]}" -c 'from pathlib import Path
from sys import argv
from dt_cloud.chstore.hot_registry_selection import load
pinned = load(*(Path(path) for path in argv[1:4]))
if any(pattern not in pinned.patterns for pattern in (".json", ".npy", "zarr.json", "zarr")):
    raise ValueError("all four selected oracle literals must be registered")
if pinned.logical_store != argv[4] or sorted(row[2] for row in pinned.buckets) != sorted(argv[5:]):
    raise ValueError("complete source bucket scope or logical store differs")
' "$selection" "$registry" "$source_manifest" "$logical_store" "${buckets[@]}" >/dev/null
fresh "$artifact"
printf '%s\n' 'build dated root catalog' >&2
run_cli ch-dated-hot-l1-build -b "$binary" -i "$source_manifest" -m 8 -o "$artifact" -r "$registry" -s "$selection" -t "$timeout" -T 8
fresh "$proof"
printf '%s\n' 'check four selected literals against full source' >&2
run_cli ch-dated-hot-l1-check -m 4 -n .json -n .npy -n zarr.json -n zarr -o "$proof" -t 600 "$artifact"
fresh "$publication"
publish_args=(ch-dated-hot-l1-publish -a "$artifact" -l "$logical_store" -p "$proof")
for bucket in "${buckets[@]}"; do publish_args+=(-b "$bucket"); done
printf '%s\n' 'publish new private generation' >&2
run_cli "${publish_args[@]}" "$publication"
printf '%s\n' 'private publication complete; serving activation remains manual' >&2
