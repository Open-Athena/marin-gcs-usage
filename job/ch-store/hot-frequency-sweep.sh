#!/usr/bin/env bash
# Node-side serial census coordinator. Run detached on the host (Docker access required).
# Does not sync source, replace images, publish catalogs, or touch serving containers.
set -euo pipefail

fail() { printf '%s\n' "$*" >&2; exit 2; }
first_tag='' run_prefix='' target='' data_root=/data wall_seconds=5400 poll_seconds=5 grace_seconds=30
dates=()
while (($#)); do
  case $1 in
    --first-tag|--run-prefix|--target|--data-root|--wall-seconds|--poll-seconds|--grace-seconds|--date)
      (($# >= 2)) || fail "missing value for $1"
      key=$1 value=$2
      shift 2
      case $key in
        --first-tag) first_tag=$value;; --run-prefix) run_prefix=$value;; --target) target=$value;;
        --data-root) data_root=$value;; --wall-seconds) wall_seconds=$value;;
        --poll-seconds) poll_seconds=$value;; --grace-seconds) grace_seconds=$value;;
        --date) dates+=("$value");;
      esac
      ;;
    *) fail "unknown argument: $1";;
  esac
done
[[ $first_tag =~ ^[a-z0-9][a-z0-9_-]{0,62}$ ]] || fail 'invalid --first-tag (lowercase safe tag, 1..63 characters)'
[[ $run_prefix =~ ^[a-z0-9][a-z0-9_-]{0,39}$ ]] || fail 'invalid --run-prefix (lowercase safe tag, 1..40 characters)'
[[ $target =~ ^[a-z_][a-z0-9_]*$ ]] || fail 'invalid --target (database identifier)'
[[ $data_root == /* && -d $data_root ]] || fail '--data-root must be an existing absolute non-root directory'
data_root=$(cd "$data_root" && pwd -P)
[[ $data_root != / ]] || fail '--data-root must be an existing absolute non-root directory'
[[ $wall_seconds =~ ^[1-9][0-9]*$ && $grace_seconds =~ ^[1-9][0-9]*$ ]] || fail 'wall/grace seconds must be positive integers'
[[ $poll_seconds =~ ^[0-9]+([.][0-9]+)?$ && $poll_seconds =~ [1-9] ]] || fail 'poll seconds must be positive'
((${#dates[@]})) || dates=(2026-10-05 2026-10-04)
seen=' '
for scan in "${dates[@]}"; do
  [[ $scan =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || fail 'invalid --date (YYYY-MM-DD required)'
  [[ $seen != *" $scan "* ]] || fail 'duplicate --date'
  seen+="$scan "
done
image=$(<"$data_root/image")
[[ -n $image && $image != *$'\n'* ]] || fail 'image file must contain one nonempty image reference'
first_id=$(docker inspect --format '{{.Id}}' "ch-job-$first_tag")
[[ $first_id =~ ^[a-f0-9]{64}$ ]] || fail 'first container did not resolve to an immutable Docker ID'
for scan in "${dates[@]}"; do
  name="ch-job-$run_prefix-$scan"
  if docker inspect --format '{{.Id}}' "$name" >/dev/null 2>&1; then fail "container already exists: $name"; fi
done
mkdir -p "$data_root/hot-frequency-sweeps"
run_dir="$data_root/hot-frequency-sweeps/$run_prefix"
mkdir "$run_dir" 2>/dev/null || fail 'cannot create new sweep directory (existing or inaccessible); nothing overwritten'
active="$run_dir/active-container"
deadline="$run_dir/wall-budget-expired"
: > "$active"

interrupt_owned() {
  local id
  [[ -s $active ]] || return 0
  id=$(<"$active")
  [[ $id =~ ^[a-f0-9]{64}$ ]] || return 0
  if mkdir "$run_dir/interrupted-$id" 2>/dev/null; then
    printf 'interrupt-owned %s\n' "$id" >&2
    docker kill --signal SIGINT "$id" >/dev/null || true
  fi
}
(
  sleeper=''
  trap 'if [[ -n $sleeper ]]; then kill "$sleeper" 2>/dev/null || true; fi; exit 0' TERM INT
  sleep "$wall_seconds" & sleeper=$!
  wait "$sleeper"
  : > "$deadline"
  printf '%s\n' 'sweep wall budget expired' >&2
  interrupt_owned
) & watchdog=$!
cleanup() {
  trap - EXIT INT TERM
  kill "$watchdog" 2>/dev/null || true
  wait "$watchdog" 2>/dev/null || true
  interrupt_owned
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

check_deadline() {
  if [[ -e $deadline ]]; then
    interrupt_owned
    if [[ -s $active ]]; then
      local id end state
      id=$(<"$active") end=$((SECONDS + grace_seconds))
      while ((SECONDS < end)); do
        state=$(docker inspect --format '{{.State.Running}} {{.State.ExitCode}}' "$id")
        [[ $state == false\ * ]] && break
        sleep "$poll_seconds"
      done
    fi
    printf '%s\n' 'sweep stopped at wall budget; retained jobs/artifacts are not auto-resumed' >&2
    exit 124
  fi
}
wait_success() {
  local id=$1 state
  while :; do
    check_deadline
    state=$(docker inspect --format '{{.State.Running}} {{.State.ExitCode}}' "$id")
    case $state in
      true\ *) sleep "$poll_seconds";;
      'false 0') check_deadline; return 0;;
      false\ *) printf 'census failed: %s (%s)\n' "$id" "$state" >&2; return 1;;
      *) printf '%s\n' 'invalid Docker state; sweep stopped' >&2; return 1;;
    esac
  done
}

printf 'wait-first ch-job-%s\n' "$first_tag" >&2
wait_success "$first_id"
printf '%s\n' 'first census succeeded' >&2
for scan in "${dates[@]}"; do
  check_deadline
  name="ch-job-$run_prefix-$scan"
  out="$run_dir/$scan-t100k-k16.json"
  queries="$run_dir/$scan-t100k-k16.queries.jsonl"
  [[ ! -e $out && ! -e $queries ]] || fail 'census artifacts already exist; nothing overwritten'
  printf 'start %s\n' "$name" >&2
  id=$(docker run -d --name "$name" --log-opt max-size=10m --log-opt max-file=2 \
    --network host -v "$data_root:$data_root" -e "PYTHONPATH=$data_root/src" \
    --entrypoint python3 "$image" -u -m dt_cloud.cli ch-hot-frequency-census "$target" \
    -d "$scan" -t 100000 -h 300000 -h 1000000 -k 16 -c 500000 \
    -m 8 -s 8 -w 600 -o "$out" -q "$queries")
  [[ $id =~ ^[a-f0-9]{64}$ ]] || fail 'new container did not return an immutable Docker ID'
  printf '%s\n' "$id" > "$active"
  if wait_success "$id"; then
    : > "$active"
    [[ -f $out && -f $queries ]] || fail 'successful census container did not produce both artifacts'
    printf 'complete %s\n' "$name" >&2
  else
    : > "$active"
    exit 1
  fi
done
printf '%s\n' 'sweep complete' >&2
