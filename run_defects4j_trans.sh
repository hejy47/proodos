#!/usr/bin/env bash
# Batch-checkout Defects4J-Trans bugs and run the Proodos pipeline.
#
# Parallelism is computed from this machine unless --jobs is set:
#   physical cores (Threadripper 3970X: 32) 
#   and MemAvailable, leaving 16 GiB free, at 6 GiB per job.
# The JVM default on this host is about a 30 GiB heap and 43 GC threads, so
# each job also caps heap and compiler/GC threads. 32 jobs is the maximum
# that fits both the 32 cores and ~237 GiB available RAM.
#
# Usage:
#   ./run_defects4j_trans.sh [--jobs N] [--dry-run] [Project [bug_id]]
#
# Examples:
#   ./run_defects4j_trans.sh
#   ./run_defects4j_trans.sh --dry-run
#   ./run_defects4j_trans.sh Closure
#   ./run_defects4j_trans.sh Closure 1

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE="$(cd "$ROOT/.." && pwd)"
DATASETS_DIR="${DATASETS_DIR:-$BASE/datasets}"
RESULT_DIR="${RESULT_DIR:-$BASE/output}"
LOG_DIR="$RESULT_DIR/batch_logs"
LOCK_DIR="$LOG_DIR/locks"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"

JOBS=""
DRY_RUN=0
FILTER_PROJECT=""
FILTER_BUG=""

usage() {
  cat >&2 <<'EOF'
Usage: ./run_defects4j_trans.sh [--jobs N] [--dry-run] [Project [bug_id]]

  --jobs N    Concurrent pipeline jobs. Default: min(physical cores, RAM/6GiB).
  --dry-run   Print the bug queue and exit.
  Project     Optional Defects4J-Trans project id, for example Closure.
  bug_id      Optional bug id. Requires Project.
EOF
  exit 2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --jobs)
      [[ $# -ge 2 ]] || usage
      JOBS="$2"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      usage
      ;;
    --)
      shift
      break
      ;;
    -*)
      echo "Unknown option: $1" >&2
      usage
      ;;
    *)
      if [[ -z "$FILTER_PROJECT" ]]; then
        FILTER_PROJECT="$1"
      elif [[ -z "$FILTER_BUG" ]]; then
        FILTER_BUG="$1"
      else
        echo "Unexpected argument: $1" >&2
        usage
      fi
      shift
      ;;
  esac
done

if [[ -n "$FILTER_BUG" && -z "$FILTER_PROJECT" ]]; then
  usage
fi

command -v defects4j >/dev/null 2>&1 || {
  echo "defects4j is not on PATH" >&2
  exit 1
}
if [[ "$DRY_RUN" -eq 0 && ! -x "$PYTHON" ]]; then
  echo "Python not found: $PYTHON" >&2
  exit 1
fi

physical_cores="$(lscpu -p=Core,Socket | grep -v '^#' | sort -u | wc -l)"
mem_avail_kb="$(awk '/MemAvailable:/ {print $2}' /proc/meminfo)"
reserve_kb=$((16 * 1024 * 1024))
per_job_kb=$((6 * 1024 * 1024))
usable_kb=$((mem_avail_kb - reserve_kb))
if (( usable_kb < per_job_kb )); then
  usable_kb=$per_job_kb
fi
mem_jobs=$((usable_kb / per_job_kb))
if [[ -z "$JOBS" ]]; then
  JOBS=$physical_cores
  if (( mem_jobs < JOBS )); then
    JOBS=$mem_jobs
  fi
fi
if (( JOBS < 1 )); then
  echo "--jobs must be >= 1" >&2
  exit 1
fi

gc_threads=$((physical_cores / JOBS))
if (( gc_threads < 1 )); then
  gc_threads=1
fi
ci_threads=$gc_threads
if (( ci_threads < 2 )); then
  ci_threads=2
fi

export PYTHONHASHSEED=0
export JAVA_TOOL_OPTIONS="${JAVA_TOOL_OPTIONS:--Xmx4g -XX:ParallelGCThreads=${gc_threads} -XX:CICompilerCount=${ci_threads} -XX:ActiveProcessorCount=${gc_threads}}"
export ANT_OPTS="${ANT_OPTS:--Xmx4g -XX:ParallelGCThreads=${gc_threads} -XX:CICompilerCount=${ci_threads}}"
export MAVEN_OPTS="${MAVEN_OPTS:--Xmx4g -XX:ParallelGCThreads=${gc_threads} -XX:CICompilerCount=${ci_threads}}"

mkdir -p "$DATASETS_DIR" "$LOG_DIR" "$LOCK_DIR"

declare -a PROJECTS=()
declare -A BIDS=()

if [[ -n "$FILTER_PROJECT" ]]; then
  PROJECTS=("$FILTER_PROJECT")
else
  mapfile -t PROJECTS < <(defects4j pids)
fi

max_bugs=0
for project in "${PROJECTS[@]}"; do
  [[ -n "$project" ]] || continue
  if [[ -n "$FILTER_BUG" ]]; then
    BIDS["$project"]="$FILTER_BUG"
    max_bugs=1
    continue
  fi
  mapfile -t ids < <(defects4j bids -p "$project")
  BIDS["$project"]="${ids[*]}"
  if (( ${#ids[@]} > max_bugs )); then
    max_bugs=${#ids[@]}
  fi
done

declare -a QUEUE=()
for ((i = 0; i < max_bugs; i++)); do
  for project in "${PROJECTS[@]}"; do
    [[ -n "${BIDS[$project]:-}" ]] || continue
    # shellcheck disable=SC2206
    ids=(${BIDS[$project]})
    if (( i < ${#ids[@]} )); then
      QUEUE+=("$project ${ids[$i]}")
    fi
  done
done

if (( ${#QUEUE[@]} == 0 )); then
  echo "No bugs selected" >&2
  exit 1
fi

echo "jobs=${#QUEUE[@]} parallel=$JOBS physical_cores=$physical_cores mem_jobs=$mem_jobs"
echo "datasets=$DATASETS_DIR"
echo "results=$RESULT_DIR"
echo "java_tool_options=$JAVA_TOOL_OPTIONS"

if (( DRY_RUN == 1 )); then
  printf '%s\n' "${QUEUE[@]}"
  exit 0
fi

note() {
  local msg
  msg="$(date +%H:%M:%S) $*"
  printf '%s\n' "$msg" >>"$LOG_DIR/batch.log"
  if [[ -w /dev/tty ]]; then
    printf '%s\n' "$msg" >/dev/tty
  else
    printf '%s\n' "$msg" >&2
  fi
}

run_one() {
  local project="$1"
  local bug="$2"
  local workdir="$DATASETS_DIR/${project}-${bug}"
  local log="$LOG_DIR/${project}-${bug}.log"
  local rc_file="$LOG_DIR/${project}-${bug}.rc"
  local patch="$RESULT_DIR/results/defects4j/${project,,}-${bug}.patch"
  local started ended rc

  if [[ ! "$project" =~ ^[A-Za-z0-9_]+$ || ! "$bug" =~ ^[0-9]+$ ]]; then
    note "FAIL $project-$bug (invalid id)"
    printf 'invalid\n' >"$rc_file"
    return 0
  fi

  if [[ -f "$patch" ]]; then
    note "SKIP $project-$bug (patch exists)"
    printf 'skip\n' >"$rc_file"
    return 0
  fi

  renice -n 5 -p "$BASHPID" >/dev/null 2>&1 || true
  started=$(date +%s)
  note "START $project-$bug"
  set +e
  (
    set -euo pipefail
    echo "=== checkout $project-$bug ==="
    flock "$LOCK_DIR/${project}.lock" bash -c '
      set -euo pipefail
      workdir="$1"
      project="$2"
      bug="$3"
      if [[ -f "$workdir/defects4j.build.properties" ]]; then
        exit 0
      fi
      rm -rf "$workdir"
      defects4j checkout -p "$project" -v "${bug}b" -w "$workdir"
    ' bash "$workdir" "$project" "$bug"
    echo "=== pipeline $project-$bug ==="
    cd "$ROOT"
    "$PYTHON" main.py \
      --dataset defects4j \
      --project_path "$workdir" \
      --project_id "$project" \
      --bug_id "$bug" \
      --result_dir "$RESULT_DIR" \
      --stage all
  ) >"$log" 2>&1
  rc=$?
  set -e
  ended=$(date +%s)
  printf '%s\n' "$rc" >"$rc_file"
  if (( rc == 0 )); then
    note "OK $project-$bug ($((ended - started))s)"
  else
    note "FAIL $project-$bug rc=$rc ($((ended - started))s) log=$log"
  fi
  return 0
}

active=0
for spec in "${QUEUE[@]}"; do
  # shellcheck disable=SC2086
  run_one $spec &
  active=$((active + 1))
  if (( active >= JOBS )); then
    wait -n || true
    active=$((active - 1))
  fi
done
wait || true

ok=0
fail=0
skip=0
for spec in "${QUEUE[@]}"; do
  read -r project bug <<<"$spec"
  rc="$(cat "$LOG_DIR/${project}-${bug}.rc" 2>/dev/null || echo missing)"
  case "$rc" in
    0) ok=$((ok + 1)) ;;
    skip) skip=$((skip + 1)) ;;
    *) fail=$((fail + 1)) ;;
  esac
done

echo "done total=${#QUEUE[@]} ok=$ok skip=$skip fail=$fail"
echo "logs=$LOG_DIR"
if (( fail > 0 )); then
  exit 1
fi
