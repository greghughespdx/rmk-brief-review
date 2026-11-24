#!/usr/bin/env bash
# scheduled-review.sh - the 05:00 independent review of the Decisions document
# (mc-mi8xx). Renders today's document the same way the 06:00 job would, has
# an independent reviewer read it, and writes the review receipt the 06:00
# push refuses to ship without.
#
# The problem (mc-mi8xx): the review-receipt gate (mc-iacbw) is correct and
# stays exactly as strict as it is - nothing is pushed without a receipt from
# a reviewer who did not write the cards, judging each one against the reader's
# three tests (complete context, answer forms that fit the question, plain
# English), bound by hash to the exact PDF and manifest being pushed. But
# nothing produced that review before 06:00, so push-decisions.sh withheld
# the document every morning (2026-09-17, 2026-09-18) and the orchestrator reviewed by
# hand after the fact.
#
# This script is that review, run early enough to matter. It never weakens
# the gate and never invents a verdict: it renders the document, hands the
# rendered pages and manifest to an independent headless reviewer (a separate
# `codex exec` run, not the process that wrote the cards), and that reviewer
# is the one who calls `review-receipt.py write`. This script only calls
# `review-receipt.py check` afterward, to confirm what the reviewer actually
# wrote is a receipt that matches the bytes and passes every card.
#
# Isolation (mc-mi8xx hardening, the cross-reviewer's MISMATCH review): the reviewer
# cannot be allowed to alter what it judges. The real PDF and manifest are
# hashed before the reviewer runs; the reviewer only ever sees COPIES (mode
# 444, which guards against accidental writes but does not stop their owner
# from changing them) in an isolated staging directory outside this repo, and codex's
# workspace-write sandbox is rooted at that staging directory, never at the
# archive. After the reviewer exits, the real PDF and manifest are re-hashed
# and must be byte-identical to before; the receipt the reviewer wrote is
# then checked against the REAL PDF and manifest (not the staged copies), so
# a tampered staged copy or a tampered real file both fail the gate. Nothing
# is ever written into the archive (OUT_DIR) until that check passes, and
# even then it lands atomically (write-temp, then rename) and is re-checked
# once more in place. On any failure - a real "no", a missing card, a hash
# mismatch, a timeout, a nonzero reviewer exit - nothing is published, and
# every failure path alerts the orchestrator on the bus through one fail() function. It
# never fabricates a passing receipt, and it never pushes anything to the
# device itself.
#
# Usage:
#   scheduled-review.sh [--date YYYY-MM-DD]
#
# Exit codes:
#   0  a valid review receipt exists for the date (already present, produced
#      by this run, or there was nothing to ask today - see rc 5 below)
#   1  no valid receipt exists after this run (bad usage aside, every failure
#      path - verify/render failure, a hash mismatch, a reviewer timeout or
#      nonzero exit, a real review "no" - alerts the orchestrator on the bus and exits
#      here); the 06:00 job will see the same withheld state it always has
#   2  usage (including a malformed --date); nothing is touched or removed
#
# Every external dependency is overridable so this can be exercised hermetically
# against fakes (remarkable/test-scheduled-review.sh).
#
# mc-0kpzk (logging-only, does not change what fails or how): two back-to-back
# 05:00/03:30 timeouts on 2026-09-28 left no evidence of where the reviewer's
# time went, because its raw transcript was never kept past the run. This
# script now logs the reviewer's start/finish timestamps and elapsed seconds
# on every run, and on a reviewer-related failure (timeout, nonzero exit, or
# a bad receipt) copies the transcript to $SCHEDULED_REVIEW_REVIEWER_KEEP_PATH
# (default $TMP/scheduled-review-$DATE-reviewer.log), mode 0600 - it may carry
# Decisions document text, never secrets, but is kept non-world-readable
# regardless. Both the raw transcript (created under a temporary umask 077)
# and the kept copy (written into a 0600 mktemp file, then renamed into
# place) are private from the moment they are created, never briefly at a
# wider mode (the cross-reviewer's cross-review of bd3f1b21).

set -uo pipefail

DIR_RMW="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Load the config into RMW_* variables. A copy of this script outside the
# repository (a test harness) falls back to RMW_PROJECT_ROOT, then to the
# built-in defaults below.
if [ -f "$DIR_RMW/rmw-env.sh" ]; then
  # shellcheck source=rmw-env.sh
  . "$DIR_RMW/rmw-env.sh"
elif [ -n "${RMW_PROJECT_ROOT:-}" ] && [ -f "$RMW_PROJECT_ROOT/remarkable/rmw-env.sh" ]; then
  # shellcheck source=rmw-env.sh
  . "$RMW_PROJECT_ROOT/remarkable/rmw-env.sh"
fi
# The same defaults rmw_config.py uses, for a run with no config loaded.
: "${RMW_PATHS_HOME:=$HOME/.local/share/remarkable-workflow}"
: "${RMW_ARCHIVE:=$RMW_PATHS_HOME/archive}"
: "${RMW_DECISIONS_ARCHIVE:=$RMW_ARCHIVE/decisions}"
: "${RMW_PATHS_QUEUE:=$RMW_PATHS_HOME/decisions-queue.md}"
: "${RMW_PATHS_REGISTRY:=$HOME/.remarkable-today-pages.json}"
: "${RMW_RENDER_PYTHON:=python3}"
: "${RMW_WRITER_IDENTITY:=writer}"

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MC="$(cd "$DIR/../.." && pwd)"

DATE="$(date +%F)"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --date) DATE="$2"; shift 2 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

# ---------------------------------------------------------------------------
# DATE must be validated before ANY path is built or anything is touched
# (mc-mi8xx hardening B6). A malformed date must never reach a path, a rm -rf,
# or a codex invocation.
# ---------------------------------------------------------------------------
case "$DATE" in
  [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]) ;;
  *)
    echo "Usage: $(basename "$0") [--date YYYY-MM-DD]" >&2
    echo "  --date must match YYYY-MM-DD; got: $DATE" >&2
    exit 2
    ;;
esac

TMP="${SCHEDULED_REVIEW_TMP:-/tmp}"
ARCHIVE_ROOT="${REMARKABLE_TODAY_ARCHIVE:-$RMW_DECISIONS_ARCHIVE}"
VERIFY_QUEUE="${SCHEDULED_REVIEW_VERIFY_QUEUE:-$DIR/verify-queue.py}"
PUSH_DECISIONS="${SCHEDULED_REVIEW_PUSH_DECISIONS:-$DIR/push-decisions.sh}"
RECEIPT_TOOL="${SCHEDULED_REVIEW_RECEIPT_TOOL:-$DIR/review-receipt.py}"
BUS_ALERT="${SCHEDULED_REVIEW_BUS_ALERT:-$DIR/hooks/alert.sh}"
# Prefer the codex inside Codex.app: the Homebrew cask binary (and a private
# xattr-free copy of it) is held at _dyld_start when a launchd job starts it,
# while the app-bundled binary starts normally (verified 2026-11-08 for the
# capability-matrix codex-quota probe, commit 64da816a; the 2026-11-08 03:30
# preflight stalled on both the cask binary and its private copy).
CODEX_APP_BIN="/Applications/Codex.app/Contents/Resources/codex-cli/bin/codex"
if [ -z "${SCHEDULED_REVIEW_CODEX:-}" ] && [ -x "$CODEX_APP_BIN" ]; then
  SCHEDULED_REVIEW_CODEX="$CODEX_APP_BIN"
fi
CODEX_BIN="${SCHEDULED_REVIEW_CODEX:-$(command -v codex 2>/dev/null || echo /opt/homebrew/bin/codex)}"
PDFTOPPM_BIN="${SCHEDULED_REVIEW_PDFTOPPM:-$(command -v "${RMW_RENDER_PDFTOPPM:-pdftoppm}" 2>/dev/null || echo /opt/homebrew/bin/pdftoppm)}"
PROMPT_FILE="${SCHEDULED_REVIEW_PROMPT:-$DIR/reviewer-prompt.md}"
PYTHON_BIN="${SCHEDULED_REVIEW_PYTHON:-${RMW_RENDER_PYTHON:-python3}}"
LOCK_DIR="${SCHEDULED_REVIEW_LOCK:-/tmp/scheduled-review.lock}"
LOG_FILE="${SCHEDULED_REVIEW_LOG:-/tmp/scheduled-review.log}"
REVIEW_MODEL="${SCHEDULED_REVIEW_MODEL:-}"   # empty = the Codex default model
REVIEWER_ID="${RMW_REVIEWER_IDENTITY:-scheduled-codex-reviewer}"
PRODUCER="${REMARKABLE_DOC_PRODUCER:-$RMW_WRITER_IDENTITY}"

# Timeout: the codex reviewer must never be allowed to run into the 06:00
# job (mc-mi8xx hardening B2). macOS has no timeout(1); this script runs
# codex in the background, polls, and kills it (TERM, then KILL after
# SCHEDULED_REVIEW_KILL_GRACE_SECONDS) if it outlives its budget.
TIMEOUT_SECONDS="${SCHEDULED_REVIEW_TIMEOUT_SECONDS:-${RMW_REVIEWER_TIMEOUT_SECONDS:-2400}}"
KILL_GRACE_SECONDS="${SCHEDULED_REVIEW_KILL_GRACE_SECONDS:-15}"

# mc-vg49y: the reviewer (codex exec) was twice observed stuck before
# main() on 2026-09-30 (03:30 and 05:00 runs) - sample(1) showed only
# _dyld_start, physical footprint 112K, 0% CPU, no children, no sockets, for
# 40 minutes, where a manual launch takes 0.04s. See the private-copy
# functions below (resolve_reviewer_bin et al.) for the suspected cause
# (Gatekeeper path-state caching on a Caskroom-installed, quarantined
# binary - upstream openai/codex#23802) and the launch-watch/relaunch logic
# further down for the mitigation.
REVIEWER_COPY_ROOT="${SCHEDULED_REVIEW_REVIEWER_COPY_ROOT:-$HOME/.local/share/remarkable-workflow/codex-reviewer}"
LAUNCH_WATCH_SECONDS="${SCHEDULED_REVIEW_LAUNCH_WATCH_SECONDS:-60}"
STUCK_RSS_KB="${SCHEDULED_REVIEW_STUCK_RSS_KB:-1024}"
STUCK_PROBE="${SCHEDULED_REVIEW_STUCK_PROBE:-}"
# The cross-reviewer's cross-review of 502c0b60 (mc-vg49y, finding 1): building a new
# private copy (hash + cp + xattr-clear) has no time bound of its own.
# Never start building one with less than this much budget left before the
# cutoff; reusing an already-verified copy is unaffected (that is just a
# hash check, always allowed).
COPY_MIN_REMAINING_SECONDS="${SCHEDULED_REVIEW_COPY_MIN_REMAINING_SECONDS:-600}"

# Absolute cutoff/handoff (mc-mi8xx hardening, the cross-reviewer's second review): a
# launchd job can start late after the Mac sleeps, so a relative timeout
# alone can still overlap the 06:00 push. CUTOFF is the last wall-clock time
# a run may START; HANDOFF is when the 06:00 job takes over. Between cutoff
# and handoff, this script refuses to start rather than risk running into
# 06:00. Before cutoff, its time budget is clipped to whatever is left before
# cutoff. At or after handoff (a manual daytime run, after 06:00 already
# ran), the relative timeout alone applies. "Now" is overridable
# (SCHEDULED_REVIEW_NOW_EPOCH) so this is testable without waiting for a
# clock.
CUTOFF_HHMM="${SCHEDULED_REVIEW_CUTOFF:-${RMW_REVIEWER_CUTOFF:-05:40}}"
HANDOFF_HHMM="${SCHEDULED_REVIEW_HANDOFF:-${RMW_SCHEDULE_DELIVERY:-06:00}}"
NOW_EPOCH="${SCHEDULED_REVIEW_NOW_EPOCH:-$(date +%s)}"
START_REAL_EPOCH="$(date +%s)"
SHASUM_BIN="${SCHEDULED_REVIEW_SHASUM:-shasum}"
TOO_LATE_TO_START=0
EFFECTIVE_TIMEOUT_SECONDS="$TIMEOUT_SECONDS"
# IS_SCHEDULED_RUN (the cross-reviewer's re-review of c08a627a): decided ONCE, from the
# run's INITIAL now (NOW_EPOCH), never from a later recheck. A run that
# itself started before the cutoff IS a scheduled run for its entire life,
# even if a later recheck's fresh "now" has since crept past HANDOFF_EPOCH
# (a slow render or a slow private-copy build eating the whole window) -
# every later cutoff recheck below gates on THIS flag, never on comparing
# the fresh "now" to HANDOFF_EPOCH again (that comparison is exactly the
# bug: it let a scheduled run that overran all the way past 06:00 read as
# "manual", skip the cutoff fail, and launch during or after the push). Only
# a genuinely manual run - one whose own start was already at or after
# HANDOFF_EPOCH - gets the old "relative timeout only" treatment.
IS_SCHEDULED_RUN=0
TODAY_YMD="$(date -j -f %s "$NOW_EPOCH" +%F 2>/dev/null || true)"
if [ -n "$TODAY_YMD" ]; then
  CUTOFF_EPOCH="$(date -j -f "%Y-%m-%d %H:%M" "$TODAY_YMD $CUTOFF_HHMM" +%s 2>/dev/null || true)"
  HANDOFF_EPOCH="$(date -j -f "%Y-%m-%d %H:%M" "$TODAY_YMD $HANDOFF_HHMM" +%s 2>/dev/null || true)"
  if [ -n "${CUTOFF_EPOCH:-}" ] && [ -n "${HANDOFF_EPOCH:-}" ]; then
    if [ "$NOW_EPOCH" -lt "$HANDOFF_EPOCH" ] && [ "$NOW_EPOCH" -ge "$CUTOFF_EPOCH" ]; then
      TOO_LATE_TO_START=1
    elif [ "$NOW_EPOCH" -lt "$CUTOFF_EPOCH" ]; then
      IS_SCHEDULED_RUN=1
      SECONDS_UNTIL_CUTOFF=$(( CUTOFF_EPOCH - NOW_EPOCH ))
      if [ "$SECONDS_UNTIL_CUTOFF" -lt "$EFFECTIVE_TIMEOUT_SECONDS" ]; then
        EFFECTIVE_TIMEOUT_SECONDS="$SECONDS_UNTIL_CUTOFF"
      fi
    fi
    # else: now >= handoff (a manual/daytime run) - IS_SCHEDULED_RUN stays
    # 0, the relative timeout alone applies; EFFECTIVE_TIMEOUT_SECONDS is
    # already TIMEOUT_SECONDS.
  fi
fi

log() { printf '[%s] %s\n' "$(date '+%F %T')" "$*" >> "$LOG_FILE"; }

# sha256_of <file>: prints the hex digest, or prints nothing and returns 1 if
# the hash command fails or yields anything but 64 hex characters (the cross-reviewer's
# review of 47e3074a: an unchecked pipeline could leave both hashes empty and
# equal, silently disabling the integrity gate).
sha256_of() {
  local out digest
  out="$("$SHASUM_BIN" -a 256 "$1" 2>/dev/null)" || return 1
  digest="${out%% *}"
  printf '%s' "$digest" | grep -Eq '^[0-9a-f]{64}$' || return 1
  printf '%s' "$digest"
}

# ---------------------------------------------------------------------------
# Private reviewer binary (mc-vg49y). A private, extended-attribute-free
# copy of the codex binary's Caskroom package directory, used instead of
# the cask install directly, to sidestep the Gatekeeper quarantine/
# translocation path-state cache implicated in the stuck-at-launch bug
# (upstream openai/codex#23802: same symptom, same root cause, on a
# Homebrew-cask binary on macOS 26). Never modifies or deletes anything in
# the real Caskroom install; only ever reads from it. Falls back to the
# original CODEX_BIN on any failure - this is a mitigation, not a new
# dependency the run can fail on.
# ---------------------------------------------------------------------------

# resolve_real_path <path> - the fully dereferenced real path of <path>
# (macOS ships no portable `readlink -f`; this walks symlinks by hand).
# Prints nothing and returns 1 if any step cannot be resolved.
resolve_real_path() {
  local p="$1" target dir
  [ -e "$p" ] || return 1
  while [ -L "$p" ]; do
    target="$(readlink "$p")" || return 1
    case "$target" in
      /*) p="$target" ;;
      *) dir="$(cd "$(dirname "$p")" 2>/dev/null && pwd)" || return 1; p="$dir/$target" ;;
    esac
  done
  dir="$(cd "$(dirname "$p")" 2>/dev/null && pwd)" || return 1
  printf '%s/%s' "$dir" "$(basename "$p")"
}

# caskroom_package_dir_for <real_path> - prints "<package_dir> <rel_path>"
# (space-separated) if <real_path> lives under a Homebrew Cask package
# directory (.../Caskroom/<formula>/<version>/...); empty + rc 1 otherwise.
# <rel_path> is <real_path>'s path relative to <package_dir> (e.g.
# "bin/codex"), so the caller can find the same binary inside a private
# copy of the whole package dir without hardcoding "bin/codex".
caskroom_package_dir_for() {
  local real="$1" prefix after formula rest version
  case "$real" in
    */Caskroom/*/*/*) ;;
    *) return 1 ;;
  esac
  prefix="${real%%/Caskroom/*}"
  after="${real#*/Caskroom/}"        # formula/version/rest...
  formula="${after%%/*}"
  rest="${after#*/}"                 # version/rest...
  version="${rest%%/*}"
  [ -n "$formula" ] && [ -n "$version" ] || return 1
  printf '%s/Caskroom/%s/%s %s\n' "$prefix" "$formula" "$version" "${rest#*/}"
}

# REVIEWER_COPY_MARKER_NAME - written into a copy directory ONLY after that
# copy has been built AND hash-verified (resolve_reviewer_bin, below). This
# is the ownership proof prune_reviewer_copies requires before it will ever
# delete anything (the cross-reviewer's cross-review of 502c0b60, finding 3): a
# directory without this marker was not built by this mitigation and is
# never touched, no matter what else it looks like.
REVIEWER_COPY_MARKER_NAME=".rmw-reviewer-copy"

# looks_like_version_dirname <name> - a conservative "this looks like a
# Caskroom version string" check (at least one digit; nothing but
# letters/digits/dot/underscore/hyphen/comma - no path separators or other
# shell-meaningful characters). A second, independent gate alongside the
# marker file: prune_reviewer_copies requires BOTH before deleting anything.
looks_like_version_dirname() {
  case "$1" in
    ""|.|..) return 1 ;;
    *[0-9]*) ;;
    *) return 1 ;;
  esac
  case "$1" in
    */*) return 1 ;;
    *[!A-Za-z0-9._,-]*) return 1 ;;
  esac
  return 0
}

# prune_reviewer_copies <keep_version> - removes sibling directories
# directly inside $REVIEWER_COPY_ROOT, but ONLY ones this function can prove
# it owns: the directory's name must look like a version AND it must
# contain $REVIEWER_COPY_MARKER_NAME (written by resolve_reviewer_bin right
# after a successful, hash-verified install - never written on a failed or
# fallback path). The copy currently in use (<keep_version>) is never
# touched. Never touches anything outside $REVIEWER_COPY_ROOT.
prune_reviewer_copies() {
  local keep="$1" entry base
  [ -d "$REVIEWER_COPY_ROOT" ] || return 0
  for entry in "$REVIEWER_COPY_ROOT"/*/; do
    [ -d "$entry" ] || continue
    base="$(basename "$entry")"
    [ "$base" = "$keep" ] && continue
    case "$base" in
      .tmp-*) continue ;;  # an in-flight build from a concurrent/aborted run
    esac
    looks_like_version_dirname "$base" || continue
    [ -f "$entry/$REVIEWER_COPY_MARKER_NAME" ] || continue
    rm -rf "$entry" 2>/dev/null || true
  done
}

# resolve_reviewer_bin - sets REVIEWER_BIN (global) to the binary this run
# should actually exec. Defaults to CODEX_BIN unchanged; replaced with a
# private, xattr-free copy when CODEX_BIN resolves into a Caskroom package
# directory and a verified copy can be made or already exists. Every path
# through here logs which binary is used and why. Never fails the run.
REVIEWER_BIN="$CODEX_BIN"
resolve_reviewer_bin() {
  local real pkg_info pkg_dir rel version src_hash dst_hash tmp_dir final_dir post_mv_hash

  real="$(resolve_real_path "$CODEX_BIN")" || {
    log "reviewer binary: could not resolve the real path of $CODEX_BIN; using it directly"
    return 0
  }
  pkg_info="$(caskroom_package_dir_for "$real")" || {
    log "reviewer binary: $CODEX_BIN ($real) is not a Caskroom package path; using it directly"
    return 0
  }
  pkg_dir="${pkg_info%% *}"
  rel="${pkg_info#* }"
  version="$(basename "$pkg_dir")"
  final_dir="$REVIEWER_COPY_ROOT/$version"

  src_hash="$(sha256_of "$real")" || {
    log "reviewer binary: could not hash $real; using $CODEX_BIN directly"
    return 0
  }

  if [ -x "$final_dir/$rel" ]; then
    dst_hash="$(sha256_of "$final_dir/$rel")" || dst_hash=""
    if [ -n "$dst_hash" ] && [ "$dst_hash" = "$src_hash" ]; then
      REVIEWER_BIN="$final_dir/$rel"
      log "reviewer binary: reusing private copy $REVIEWER_BIN (version $version, hash verified)"
      prune_reviewer_copies "$version"
      return 0
    fi
    log "reviewer binary: private copy at $final_dir/$rel exists but does not match $real by hash; rebuilding"
  fi

  # The cross-reviewer's cross-review of 502c0b60, finding 1: building a NEW copy (cp
  # of a whole package dir, xattr-clear, hashing) has no time bound of its
  # own, and nothing previously rechecked the cutoff after it ran. Refuse to
  # even START building one this close to the cutoff - EFFECTIVE_TIMEOUT_SECONDS
  # is the freshest known remaining budget at this point in the script
  # (already clipped to the cutoff just before this function is called).
  # Reusing an existing, hash-verified copy (above) is unaffected - that is
  # a fast hash check, not an unbounded copy.
  if [ "$EFFECTIVE_TIMEOUT_SECONDS" -lt "$COPY_MIN_REMAINING_SECONDS" ]; then
    log "reviewer binary: only ${EFFECTIVE_TIMEOUT_SECONDS}s of budget remain (< ${COPY_MIN_REMAINING_SECONDS}s margin); skipping a new private copy, using $CODEX_BIN directly"
    return 0
  fi

  if ! mkdir -p "$REVIEWER_COPY_ROOT" 2>/dev/null; then
    log "reviewer binary: could not create $REVIEWER_COPY_ROOT; using $CODEX_BIN directly"
    return 0
  fi
  tmp_dir="$(mktemp -d "$REVIEWER_COPY_ROOT/.tmp-$version-XXXXXX" 2>/dev/null || true)"
  if [ -z "$tmp_dir" ]; then
    log "reviewer binary: could not create a temp dir under $REVIEWER_COPY_ROOT; using $CODEX_BIN directly"
    return 0
  fi
  # -X: do not copy extended attributes (com.apple.quarantine included) -
  # the whole point of the private copy. -R: the package dir may contain
  # sibling resources (codex-path/, codex-resources/, etc.) the binary
  # locates relative to itself.
  if ! cp -R -X "$pkg_dir/." "$tmp_dir/" 2>/dev/null; then
    log "reviewer binary: copy from $pkg_dir to $tmp_dir failed; using $CODEX_BIN directly"
    rm -rf "$tmp_dir" 2>/dev/null || true
    return 0
  fi
  # Belt and suspenders: `cp -X` stops cp from COPYING extended attributes,
  # but observed live, a freshly written executable can still end up with
  # an extended attribute of its own (e.g. a freshly assigned
  # com.apple.quarantine, not byte-identical to any source value - macOS
  # evaluating a newly created unsigned executable, independent of what cp
  # did or did not copy). Explicitly clear every extended attribute on the
  # whole copied tree so "does not carry extended attributes" is actually
  # true of the result, not just of what cp was asked to do. Never fails
  # the run: if xattr itself is unavailable, the hash check below still
  # gates whether this copy is trusted for content, and quarantine (if any
  # survives) only recreates the ORIGINAL problem, which is also closed by
  # the launch-watch/relaunch mitigation further down.
  xattr -rc "$tmp_dir" 2>/dev/null || true
  if [ ! -x "$tmp_dir/$rel" ]; then
    log "reviewer binary: $tmp_dir/$rel missing or not executable after copy; using $CODEX_BIN directly"
    rm -rf "$tmp_dir" 2>/dev/null || true
    return 0
  fi
  dst_hash="$(sha256_of "$tmp_dir/$rel")" || {
    log "reviewer binary: could not hash the fresh copy at $tmp_dir/$rel; using $CODEX_BIN directly"
    rm -rf "$tmp_dir" 2>/dev/null || true
    return 0
  }
  if [ "$dst_hash" != "$src_hash" ]; then
    log "reviewer binary: fresh copy at $tmp_dir/$rel does not match $real by hash ($dst_hash != $src_hash); using $CODEX_BIN directly"
    rm -rf "$tmp_dir" 2>/dev/null || true
    return 0
  fi

  # The cross-reviewer's cross-review of 502c0b60, finding 3: if $final_dir already
  # exists and cannot be removed, `mv "$tmp_dir" "$final_dir"` would NOT
  # overwrite it - mv moves tmp_dir INSIDE the still-existing final_dir
  # instead (dst/basename(src)), so REVIEWER_BIN="$final_dir/$rel" would
  # point at a file that was never actually verified. Abort the install
  # instead of ever reaching that silent-wrong-success state.
  if [ -e "$final_dir" ]; then
    rm -rf "$final_dir" 2>/dev/null || true
    if [ -e "$final_dir" ]; then
      log "reviewer binary: could not remove the existing $final_dir to install the verified copy; using $CODEX_BIN directly"
      rm -rf "$tmp_dir" 2>/dev/null || true
      return 0
    fi
  fi
  if ! mv "$tmp_dir" "$final_dir" 2>/dev/null; then
    log "reviewer binary: could not install private copy at $final_dir; using $CODEX_BIN directly"
    rm -rf "$tmp_dir" 2>/dev/null || true
    return 0
  fi

  # Verify once more AFTER the mv, against the actual installed path - not
  # just the pre-move tmp_dir - before this copy is ever trusted (the cross-reviewer's
  # cross-review of 502c0b60, finding 3).
  if [ ! -x "$final_dir/$rel" ]; then
    log "reviewer binary: $final_dir/$rel missing or not executable after install; using $CODEX_BIN directly"
    rm -rf "$final_dir" 2>/dev/null || true
    return 0
  fi
  post_mv_hash="$(sha256_of "$final_dir/$rel")" || {
    log "reviewer binary: could not hash $final_dir/$rel after install; using $CODEX_BIN directly"
    rm -rf "$final_dir" 2>/dev/null || true
    return 0
  }
  if [ "$post_mv_hash" != "$src_hash" ]; then
    log "reviewer binary: $final_dir/$rel does not match $real by hash after install ($post_mv_hash != $src_hash); using $CODEX_BIN directly"
    rm -rf "$final_dir" 2>/dev/null || true
    return 0
  fi

  # The marker is written ONLY here, after a successful, hash-verified
  # install - it is prune_reviewer_copies's sole proof of ownership
  # (the cross-reviewer's cross-review of 502c0b60, finding 3). Best-effort: a failure
  # to write it never un-does the install above, it just means this copy
  # will not be eligible for pruning later (safe direction).
  printf 'version=%s\nsha256=%s\n' "$version" "$post_mv_hash" > "$final_dir/$REVIEWER_COPY_MARKER_NAME" 2>/dev/null || true

  REVIEWER_BIN="$final_dir/$rel"
  log "reviewer binary: built and verified private copy $REVIEWER_BIN (version $version, sha256 $post_mv_hash)"
  prune_reviewer_copies "$version"
}

# ---------------------------------------------------------------------------
# Launch watch (mc-vg49y). Independently of the private-copy mitigation
# above (defense in depth - the private copy may not be possible, or may
# not be the actual cause on a given run), a reviewer that is still alive
# but has near-zero RSS and zero CPU time, or only the initial stdin notice
# (mc-cldya), at the existing watch is killed and relaunched exactly
# once with the same arguments. If the relaunch is ALSO stuck, this logic
# never fires a third time - the existing timeout/kill handling runs
# unchanged against the second attempt.
# ---------------------------------------------------------------------------

# is_reviewer_stuck <pid> - returns 0 (stuck) if RSS is below STUCK_RSS_KB
# AND cpu time (`ps -o time=`) is still 0:00.00; 1 otherwise. Wholesale
# overridable via STUCK_PROBE (SCHEDULED_REVIEW_STUCK_PROBE) - a script
# given the pid as $1 whose own exit code is used verbatim - so tests can
# simulate stuck/not-stuck without needing a real near-zero-RSS process.
is_reviewer_stuck() {
  local pid="$1" rss time_str
  if [ -n "$STUCK_PROBE" ]; then
    "$STUCK_PROBE" "$pid"
    return $?
  fi
  rss="$(ps -o rss= -p "$pid" 2>/dev/null | tr -d ' ')"
  time_str="$(ps -o time= -p "$pid" 2>/dev/null | tr -d ' ')"
  [ -n "$rss" ] && [ -n "$time_str" ] || return 1
  [ "$rss" -lt "$STUCK_RSS_KB" ] 2>/dev/null || return 1
  [ "$time_str" = "0:00.00" ] || return 1
  return 0
}

# Match the complete notice, not its size: healthy starts print it too, then
# add a header. This reuses the one-shot watch, not another deadline.
reviewer_log_is_startup_notice() {
  [ -f "$REVIEWER_LOG" ] || return 1
  cmp -s "$REVIEWER_LOG" <(printf '%s\n' 'Reading additional input from stdin...')
}

# launch_reviewer <attempt_number> - launches $REVIEWER_BIN with the fixed
# argument set in the background (umask 077 for the duration, exactly as
# before), sets REVIEWER_PID, and logs the attempt. Used for both the
# initial launch and the one allowed relaunch so both attempts go through
# identical code and both get a timed log line.
# A configured reviewer.command (RMW_REVIEWER_COMMAND, a JSON argv list) replaces
# the built-in Codex invocation. Placeholders: {stage_dir}, {prompt},
# {prompt_file}, {last_message_file} and {images}, which expands to every page
# image, each preceded by reviewer.image_flag when that is set. The command must
# exit 0 and must have run the `write` command it was given; the receipt check
# below is what decides whether the review counts.
custom_reviewer_argv() {
  "$PYTHON_BIN" - "${RMW_REVIEWER_COMMAND:-[]}" "$STAGE_DIR" "$REVIEW_PROMPT_FILE" \
    "$TMP/scheduled-review-reviewer-last-$DATE.txt" "${RMW_REVIEWER_IMAGE_FLAG:-}" \
    "$STAGE_PAGES"/page*.png <<'PY'
import json, pathlib, sys
argv = json.loads(sys.argv[1])
stage, prompt_file, last, flag = sys.argv[2:6]
images = sys.argv[6:]
prompt = pathlib.Path(prompt_file).read_text(encoding="utf-8")
out = []
for token in argv:
    if token == "{images}":
        for image in images:
            if flag:
                out.append(flag)
            out.append(image)
        continue
    out.append(token.replace("{stage_dir}", stage).replace("{prompt_file}", prompt_file)
                    .replace("{last_message_file}", last).replace("{prompt}", prompt))
sys.stdout.write("\0".join(out))
PY
}

launch_reviewer() {
  local attempt="$1" umask_before
  umask_before="$(umask)"
  umask 077
  if [ "${RMW_REVIEWER_COMMAND:-[]}" != "[]" ] && [ -n "${RMW_REVIEWER_COMMAND:-}" ]; then
    local custom=() arg
    while IFS= read -r -d '' arg; do custom+=("$arg"); done < <(custom_reviewer_argv; printf '\0')
    REVIEWER_BIN="${custom[0]}"
    "${custom[@]}" </dev/null >"$REVIEWER_LOG" 2>&1 &
    REVIEWER_PID=$!
    umask "$umask_before"
    log "$DATE: reviewer attempt $attempt launched (pid $REVIEWER_PID) using configured reviewer.command ($REVIEWER_BIN)"
    return 0
  fi
  "$REVIEWER_BIN" exec --ephemeral --skip-git-repo-check --ignore-user-config \
    -c approval_policy="never" \
    -s workspace-write -C "$STAGE_DIR" \
    "${MODEL_ARGS[@]+"${MODEL_ARGS[@]}"}" \
    -o "$TMP/scheduled-review-reviewer-last-$DATE.txt" \
    "${PAGE_ARGS[@]}" \
    -- "$REVIEW_PROMPT" \
    </dev/null >"$REVIEWER_LOG" 2>&1 &
  REVIEWER_PID=$!
  umask "$umask_before"
  log "$DATE: reviewer attempt $attempt launched (pid $REVIEWER_PID) using $REVIEWER_BIN"
}

mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true
log "=== scheduled-review run for $DATE ==="

# keep_reviewer_log() - mc-0kpzk. The reviewer's raw transcript is normally
# transient ($REVIEWER_LOG under $TMP); on a reviewer-related failure
# (timeout, nonzero exit, or a bad receipt) it is copied next to whatever
# other artifacts this run keeps, mode 0600 (it may carry Decisions document
# text, never treated as public), so the next incident has evidence of where
# the time went instead of a log that is gone by the time anyone looks.
# Logging-only: never changes what fails or how. The destination is
# overridable so scheduled-review-preflight.sh can redirect it outside its
# own scratch dir, which is otherwise removed on exit regardless of outcome.
REVIEWER_KEEP_PATH="${SCHEDULED_REVIEW_REVIEWER_KEEP_PATH:-$TMP/scheduled-review-$DATE-reviewer.log}"
# keep_reviewer_log's copy must never be briefly world-readable (the cross-reviewer's
# review of bd3f1b21): a plain `cp` then `chmod 600` creates the copy at the
# umask's default mode first, and if the chmod itself then failed the
# world-readable copy would be left behind. mktemp(1) creates its file at
# 0600 regardless of umask (verified on this host), and `cp` onto an
# existing file preserves that file's mode rather than reapplying the
# umask (verified on this host) - so writing into a 0600 mktemp file, in
# the SAME directory as the final path (guaranteeing `mv` is an atomic
# same-filesystem rename, never a cross-device copy that could reintroduce
# a wider mode), and renaming into place, never exposes a wider-than-0600
# copy at any point. Any failure removes the temp file rather than leaving
# it behind.
keep_reviewer_log() {
  if [ -n "${REVIEWER_LOG:-}" ] && [ -f "$REVIEWER_LOG" ]; then
    local keep_dir keep_tmp
    keep_dir="$(dirname "$REVIEWER_KEEP_PATH")"
    mkdir -p "$keep_dir" 2>/dev/null || true
    keep_tmp="$(mktemp "$keep_dir/.scheduled-review-reviewer.XXXXXX" 2>/dev/null || true)"
    if [ -n "$keep_tmp" ] && cp "$REVIEWER_LOG" "$keep_tmp" 2>/dev/null && mv -f "$keep_tmp" "$REVIEWER_KEEP_PATH" 2>/dev/null; then
      log "$DATE: reviewer log kept at $REVIEWER_KEEP_PATH"
    else
      [ -n "$keep_tmp" ] && rm -f "$keep_tmp" 2>/dev/null
      log "$DATE: could not keep reviewer log at $REVIEWER_KEEP_PATH"
    fi
  fi
}

STAGE_DIR=""

# ---------------------------------------------------------------------------
# fail() - the one path every failure routes through (mc-mi8xx hardening
# B4). Logs the reason, cleans up any staging directory, alerts the orchestrator on
# the bus, and exits 1. Nothing is ever published on the way through here.
# The only two exceptions to "every failure calls fail()" are usage (exit 2,
# above, before anything is touched) and "nothing to ask today" (exit 0,
# below - not a failure).
# ---------------------------------------------------------------------------
fail() {
  local reason="$*"
  log "$DATE: FAILURE: $reason"
  if [ -n "$STAGE_DIR" ]; then
    rm -rf "$STAGE_DIR" 2>/dev/null || true
  fi
  local subject="Scheduled review FAILED for $DATE"
  local body="scheduled-review.sh (05:00) could not produce a passing review receipt for $DATE. The 06:00 job will withhold the document as usual until this is resolved.

$reason"
  if "$BUS_ALERT" "$subject" "$body" "scheduled-review-failed" >>"$LOG_FILE" 2>&1; then
    log "$DATE: operator alert sent"
  else
    log "$DATE: operator alert failed to send; see log above"
  fi
  exit 1
}

# ---------------------------------------------------------------------------
# Lock. Two runs of this job must never overlap: a second run mid-render or
# mid-review would race the first over the same PDF/manifest/receipt paths.
# macOS ships no flock(1), so this is the same mkdir-as-atomic-lock pattern
# rmapi-retry.sh uses. A lock is only ever taken over if the PID that holds
# it is no longer alive (mc-mi8xx hardening B5) - never by age alone, which
# would let this job kick a genuinely slow-but-live sibling out from under
# itself.
# ---------------------------------------------------------------------------
# write_lock_pid: record this run as the lock holder. If the PID cannot be
# written, release the directory rather than leave a lock that looks held
# but has no live owner to check (the cross-reviewer's review of 47e3074a).
write_lock_pid() {
  if printf '%s\n' "$$" > "$LOCK_DIR/pid" 2>/dev/null && [ "$(cat "$LOCK_DIR/pid" 2>/dev/null)" = "$$" ]; then
    return 0
  fi
  rm -rf "$LOCK_DIR" 2>/dev/null || true
  return 1
}

acquire_lock() {
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    write_lock_pid || return 2
    return 0
  fi
  local holder_pid=""
  if [ -f "$LOCK_DIR/pid" ]; then
    holder_pid="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
  fi
  case "$holder_pid" in
    ''|*[!0-9]*) holder_pid="" ;;
  esac
  if [ -n "$holder_pid" ] && kill -0 "$holder_pid" 2>/dev/null; then
    return 1
  fi
  log "stale lock at $LOCK_DIR (holder pid '${holder_pid:-unknown}' not alive); removing and re-acquiring"
  rm -rf "$LOCK_DIR" 2>/dev/null || true
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    write_lock_pid || return 2
    return 0
  fi
  return 1
}

acquire_lock
LOCK_RC=$?
if [ "$LOCK_RC" -eq 2 ]; then
  fail "could not record this run's PID in $LOCK_DIR; lock released, nothing done"
fi
if [ "$LOCK_RC" -ne 0 ]; then
  fail "another run holds $LOCK_DIR; exiting without doing anything"
fi
trap 'rm -rf "$LOCK_DIR" 2>/dev/null || true' EXIT

OUT_DIR="$ARCHIVE_ROOT/$DATE"
PDF="$OUT_DIR/decisions-$DATE.pdf"
MANIFEST="$OUT_DIR/decisions-$DATE.manifest.json"
RECEIPT="$OUT_DIR/review-receipt.json"

# ---------------------------------------------------------------------------
# (a) Already reviewed. An overnight agent (or a prior run of this job) may
# already have produced a receipt that matches today's exact rendered bytes.
# Re-reviewing would only re-render and risk invalidating a good receipt for
# no reason; check first and do nothing if it already holds.
# ---------------------------------------------------------------------------
if [ -f "$RECEIPT" ] && [ -f "$PDF" ] && [ -f "$MANIFEST" ]; then
  if "$PYTHON_BIN" "$RECEIPT_TOOL" --archive-root "$ARCHIVE_ROOT" check \
      --date "$DATE" --pdf "$PDF" --manifest "$MANIFEST" --receipt "$RECEIPT" \
      --producer "$PRODUCER" >>"$LOG_FILE" 2>&1; then
    log "receipt for $DATE already valid (reviewed earlier); nothing to do"
    exit 0
  fi
  log "an existing receipt for $DATE does not match today's rendered document; continuing"
fi

# ---------------------------------------------------------------------------
# (a.5) Cutoff. Only gates actually STARTING new work; an already-valid
# receipt above always short-circuits regardless of the time.
# ---------------------------------------------------------------------------
if [ "$TOO_LATE_TO_START" -eq 1 ]; then
  fail "started too late to finish before 06:00 (now $NOW_EPOCH, cutoff $CUTOFF_HHMM, handoff $HANDOFF_HHMM)"
fi

# ---------------------------------------------------------------------------
# (b) Render. Same freshness pass and render command the 06:00 job uses
# (morning-dashboard.sh run_decisions), WITHOUT --push: push-decisions.sh
# without --push renders and prints "Generated locally. Add --push to send
# and register it." then exits 0 before it ever reaches the review gate or
# any device-writing code (push-decisions.sh lines 118-121 exit before the
# review gate at lines 123-135 and delivery at lines 141+). Nothing is sent
# to the reMarkable from this script, ever.
# ---------------------------------------------------------------------------
mkdir -p "$ARCHIVE_ROOT"
VQUEUE="$TMP/verified-queue-$DATE.md"
VREPORT="$ARCHIVE_ROOT/verify-$DATE.json"

if ! "$PYTHON_BIN" "$VERIFY_QUEUE" --out-queue "$VQUEUE" --report "$VREPORT" --render-date "$DATE" ${PHONE_CARDS_URL:+--live-cards "$PHONE_CARDS_URL"} >>"$LOG_FILE" 2>&1; then
  fail "verify-queue.py failed for $DATE; document withheld from review; no reviewer run"
fi

RENDER_LOG="$TMP/scheduled-review-render-$DATE.log"
: > "$RENDER_LOG"
TODAY_EXTRACTOR_PYTHON="${TODAY_EXTRACTOR_PYTHON:-$RMW_RENDER_PYTHON}" \
REMARKABLE_TODAY_ARCHIVE="$ARCHIVE_ROOT" \
  "$PUSH_DECISIONS" --date "$DATE" --queue "$VQUEUE" --verify-report "$VREPORT" \
    >"$RENDER_LOG" 2>&1
RENDER_RC=$?
cat "$RENDER_LOG" >> "$LOG_FILE"

case "$RENDER_RC" in
  0)
    log "document for $DATE rendered locally (unreviewed, unpushed)"
    ;;
  5)
    log "$DATE: no active queue rows and no cards (nothing to ask today); no review needed"
    exit 0
    ;;
  2)
    fail "GENERATION FAILED (push-decisions.sh rc=2) rendering $DATE; no reviewer run"
    ;;
  3)
    fail "EMPTY-DOC TRIPWIRE (push-decisions.sh rc=3) rendering $DATE; no reviewer run"
    ;;
  *)
    fail "push-decisions.sh exited $RENDER_RC rendering $DATE; no reviewer run"
    ;;
esac

if [ ! -f "$PDF" ] || [ ! -f "$MANIFEST" ]; then
  fail "render reported success but $PDF or $MANIFEST is missing; no reviewer run"
fi

# ---------------------------------------------------------------------------
# (c) Stage read-only copies for the reviewer (mc-mi8xx hardening B1). The
# reviewer must not be able to alter what it judges: it never sees the real
# PDF or manifest, only copies in an isolated staging directory outside this
# repo (mode 444 guards against accidental writes only; the real boundary is
# the real-file re-hash and the check against the real bytes below). Record
# the real files' hashes now, before the reviewer
# ever runs, so any change to the REAL files during the review window - by
# the reviewer or anything else - is caught below.
# ---------------------------------------------------------------------------
REAL_PDF_SHA_BEFORE="$(sha256_of "$PDF")" || fail "could not hash the real PDF at $PDF before review"
REAL_MANIFEST_SHA_BEFORE="$(sha256_of "$MANIFEST")" || fail "could not hash the real manifest at $MANIFEST before review"

STAGE_DIR="$TMP/scheduled-review-stage-$DATE"
# A fresh, empty staging dir every run, checked: a leftover receipt from an
# earlier run must never survive into this one (the cross-reviewer's review of 47e3074a).
rm -rf "$STAGE_DIR" 2>/dev/null
if [ -e "$STAGE_DIR" ]; then
  fail "could not clear the old staging dir $STAGE_DIR"
fi
mkdir "$STAGE_DIR" || fail "could not create staging dir $STAGE_DIR"
STAGE_PDF="$STAGE_DIR/decisions-$DATE.pdf"
STAGE_MANIFEST="$STAGE_DIR/decisions-$DATE.manifest.json"
STAGE_RECEIPT="$STAGE_DIR/review-receipt.json"
STAGE_PAGES="$STAGE_DIR/pages"

cp "$PDF" "$STAGE_PDF" || fail "could not copy the PDF into the review staging dir"
cp "$MANIFEST" "$STAGE_MANIFEST" || fail "could not copy the manifest into the review staging dir"
chmod 444 "$STAGE_PDF" "$STAGE_MANIFEST" || fail "could not mark the staged copies read-only"
mkdir "$STAGE_PAGES" || fail "could not create the page-image dir $STAGE_PAGES"

# ---------------------------------------------------------------------------
# (d) Render every page of the STAGED PDF to an image so the reviewer judges
# the pages exactly as the reader will see them. If the pages cannot be rendered,
# nothing is reviewed (fail closed).
# ---------------------------------------------------------------------------
if ! "$PDFTOPPM_BIN" -r 110 -png "$STAGE_PDF" "$STAGE_PAGES/page" >>"$LOG_FILE" 2>&1; then
  fail "could not render PDF pages for the reviewer; no review"
fi
PAGE_ARGS=()
for page in "$STAGE_PAGES"/page*.png; do
  [ -f "$page" ] && PAGE_ARGS+=(-i "$page")
done
if [ "${#PAGE_ARGS[@]}" -eq 0 ]; then
  fail "PDF rendered to zero page images; no review"
fi
PAGE_COUNT=$(( ${#PAGE_ARGS[@]} / 2 ))

# ---------------------------------------------------------------------------
# (e) The independent reviewer: a separate headless Codex run (codex exec).
# Codex did not write the cards, and it is a different model family from the
# Claude sessions that do, which is the independence the reader's rule asks for.
#
# Why Codex, not `claude -p` (verified 2026-09-23 with one-shot launchd
# probes): under launchd the Claude CLI reports "OAuth session expired and
# could not be refreshed" (and "Not logged in" under env -i), so a 05:00 job
# cannot use it without a long-lived token only the reader can mint. `codex exec`
# authenticates from its own file store and answered under launchd.
#
# Isolation (mc-mi8xx hardening B1/B3): the reviewer is given ONLY the
# staged copies - its prompt names the staged PDF, staged manifest, and a
# receipt path inside the staging dir, never the real archive paths - and
# codex's own workspace-write sandbox is rooted at the staging dir (-C, -s
# workspace-write), which is outside any git repository, so no project
# .codex/config.toml loads. --ignore-user-config additionally skips
# $CODEX_HOME/config.toml (auth still comes from CODEX_HOME - verified live
# 2026-09-23, see the commit message / task report), and
# -c approval_policy="never" makes the no-prompts policy explicit rather
# than relying on config.toml defaults that --ignore-user-config just
# dropped.
# ---------------------------------------------------------------------------
WRITE_CMD="$PYTHON_BIN $RECEIPT_TOOL write"
CHECK_CMD="$PYTHON_BIN $RECEIPT_TOOL check"

REVIEW_PROMPT="$(cat "$PROMPT_FILE")

---
Today's date is $DATE.
All $PAGE_COUNT pages of the rendered Decisions PDF are attached to this message as images, in page order. The PDF itself is at: $STAGE_PDF
Its manifest is at: $STAGE_MANIFEST
Write the receipt to: $STAGE_RECEIPT

Use exactly this command to write your verdicts, adding one --verdict flag
per card in the manifest (card_id:complete_context,forms_fit,plain_english[:note]):

$WRITE_CMD --date $DATE --reviewer $REVIEWER_ID --pdf $STAGE_PDF --manifest $STAGE_MANIFEST --receipt $STAGE_RECEIPT --verdict CARD_ID:yes|no,yes|no,yes|no[:note] [--verdict CARD_ID2:...]

Use exactly this command to confirm the receipt you wrote:

$CHECK_CMD --date $DATE --pdf $STAGE_PDF --manifest $STAGE_MANIFEST --receipt $STAGE_RECEIPT --producer $PRODUCER

Read the manifest first for the full list of card ids, then look at every attached page image, then write your verdicts, then check. Report the check result."

REVIEW_PROMPT_FILE="$STAGE_DIR/review-prompt.txt"
printf '%s\n' "$REVIEW_PROMPT" > "$REVIEW_PROMPT_FILE" || fail "could not write the reviewer prompt into the staging dir"

MODEL_ARGS=()
[ -n "$REVIEW_MODEL" ] && MODEL_ARGS=(-m "$REVIEW_MODEL")

# Recompute the budget at launch (the cross-reviewer's review of 47e3074a): verify and
# render can take real time, so the budget computed at start may no longer
# fit before the cutoff. "Now" at launch is the start time plus real elapsed
# seconds, overridable for tests.
#
# Gated on IS_SCHEDULED_RUN, not on "LAUNCH_NOW_EPOCH < HANDOFF_EPOCH"
# (the cross-reviewer's re-review of c08a627a): the old gate let a SCHEDULED run that
# overran all the way past HANDOFF_EPOCH read as if it were a manual
# daytime run and skip the cutoff fail entirely - exactly backwards, since
# an overrun scheduled run is the one case this fail() exists to catch. A
# run decided to be scheduled at its own start stays gated on the cutoff
# for its whole life, however far past handoff "now" has since drifted.
LAUNCH_NOW_EPOCH="${SCHEDULED_REVIEW_LAUNCH_NOW_EPOCH:-$(( NOW_EPOCH + $(date +%s) - START_REAL_EPOCH ))}"
if [ "$IS_SCHEDULED_RUN" -eq 1 ] && [ -n "${CUTOFF_EPOCH:-}" ]; then
  if [ "$LAUNCH_NOW_EPOCH" -ge "$CUTOFF_EPOCH" ]; then
    fail "the render finished after the $CUTOFF_HHMM cutoff; no time left to review before $HANDOFF_HHMM"
  fi
  REMAINING_TO_CUTOFF=$(( CUTOFF_EPOCH - LAUNCH_NOW_EPOCH ))
  if [ "$REMAINING_TO_CUTOFF" -lt "$EFFECTIVE_TIMEOUT_SECONDS" ]; then
    EFFECTIVE_TIMEOUT_SECONDS="$REMAINING_TO_CUTOFF"
  fi
fi
log "$DATE: reviewer budget ${EFFECTIVE_TIMEOUT_SECONDS}s"

REVIEWER_LOG="$TMP/scheduled-review-reviewer-$DATE.log"
# workspace-write rooted at the isolated staging dir: the reviewer can write
# the receipt there and nowhere else. Run in the background so it can be
# timed out and killed (macOS has no timeout(1)); the wait budget is
# EFFECTIVE_TIMEOUT_SECONDS, already clipped to the 06:00 cutoff above.
#
# mc-vg49y: resolve_reviewer_bin runs once, before the first launch, and
# applies to both the initial attempt and the one allowed relaunch below -
# a relaunch never re-resolves (same REVIEWER_BIN both times, "same
# arguments" per the mitigation's own contract).
resolve_reviewer_bin

# The cross-reviewer's cross-review of 502c0b60, finding 1: resolve_reviewer_bin itself
# (hashing, copying, xattr-clearing, pruning) has no time bound, and the
# cutoff was last checked BEFORE it ran. Recheck here, against a fresh
# "now", before the reviewer is ever launched - a slow resolution must
# never be allowed to push the actual launch past the 06:00 handoff. Same
# overridable-epoch pattern as the LAUNCH_NOW_EPOCH recheck above (real
# wall-clock elapsed, added to the overridable NOW_EPOCH, so this is
# testable without waiting for a clock).
#
# Gated on IS_SCHEDULED_RUN, not on "POST_COPY_NOW_EPOCH < HANDOFF_EPOCH"
# (the cross-reviewer's re-review of c08a627a - the same bug shape as the pre-copy
# check above, material here because the copy itself can genuinely run long
# enough to cross HANDOFF_EPOCH): a scheduled run that overran all the way
# past 06:00 must still fail here, not read as a manual run and launch
# during or after the push.
POST_COPY_NOW_EPOCH="${SCHEDULED_REVIEW_POST_COPY_NOW_EPOCH:-$(( NOW_EPOCH + $(date +%s) - START_REAL_EPOCH ))}"
if [ "$IS_SCHEDULED_RUN" -eq 1 ] && [ -n "${CUTOFF_EPOCH:-}" ]; then
  if [ "$POST_COPY_NOW_EPOCH" -ge "$CUTOFF_EPOCH" ]; then
    fail "resolving the reviewer binary finished after the $CUTOFF_HHMM cutoff; no time left to review before $HANDOFF_HHMM"
  fi
  REMAINING_AFTER_COPY=$(( CUTOFF_EPOCH - POST_COPY_NOW_EPOCH ))
  if [ "$REMAINING_AFTER_COPY" -lt "$EFFECTIVE_TIMEOUT_SECONDS" ]; then
    EFFECTIVE_TIMEOUT_SECONDS="$REMAINING_AFTER_COPY"
  fi
fi
log "$DATE: reviewer budget after binary resolution ${EFFECTIVE_TIMEOUT_SECONDS}s"

# mc-0kpzk (the cross-reviewer's review of bd3f1b21): the shell's own `>"$REVIEWER_LOG"`
# redirect creates that file at the umask's default mode (0644 under the
# common 022 umask, observed live), before anything here gets a chance to
# chmod it. It may carry Decisions document text, so it is made private at
# creation instead: umask 077 for exactly this one backgrounded launch (the
# forked reviewer process, and everything it itself creates - including its
# own -o output file - inherits that umask across exec), restored
# immediately after so nothing else in this script is affected. (Enforced
# inside launch_reviewer, which both the initial launch and the one allowed
# relaunch below go through.)
REVIEWER_START_EPOCH="$(date +%s)"
# The cross-reviewer's cross-review of 502c0b60, finding 2: a SINGLE absolute deadline
# epoch for the WHOLE reviewer phase (launch watch, relaunch, and the final
# timeout all measure against this same point), computed once here from a
# real wall-clock start plus the just-finalized budget. The poll loop below
# compares against the wall clock (`date +%s`) every iteration rather than
# counting loop ticks, so iteration overhead (sleep taking a little over a
# second, kill-grace waits, etc.) can never let the real elapsed time drift
# past the nominal budget the way counting ticks could.
REVIEWER_DEADLINE_EPOCH=$(( REVIEWER_START_EPOCH + EFFECTIVE_TIMEOUT_SECONDS ))
launch_reviewer 1

TIMED_OUT=0
STUCK_CHECKED=0
while kill -0 "$REVIEWER_PID" 2>/dev/null; do
  NOW_WALL_EPOCH="$(date +%s)"
  if [ "$NOW_WALL_EPOCH" -ge "$REVIEWER_DEADLINE_EPOCH" ]; then
    TIMED_OUT=1
    kill -TERM "$REVIEWER_PID" 2>/dev/null || true
    KWAIT=0
    while [ "$KWAIT" -lt "$KILL_GRACE_SECONDS" ] && kill -0 "$REVIEWER_PID" 2>/dev/null; do
      sleep 1
      KWAIT=$((KWAIT + 1))
    done
    if kill -0 "$REVIEWER_PID" 2>/dev/null; then
      kill -KILL "$REVIEWER_PID" 2>/dev/null || true
    fi
    break
  fi
  # mc-vg49y launch watch: checked at most once per run, LAUNCH_WATCH_SECONDS
  # (wall-clock) after the FIRST launch. If the reviewer is still alive but
  # looks stuck at launch, kill it, THEN recheck the one shared deadline
  # before deciding to relaunch (the cross-reviewer's cross-review of 502c0b60, finding
  # 2): no relaunch if the deadline has already passed, or is within one
  # kill-grace window of passing - there would be no real time left for a
  # second attempt anyway. In that case this is treated exactly like the
  # ordinary deadline case above (TIMED_OUT=1, same fail() message) rather
  # than inventing a new failure shape. A stuck second attempt is never
  # checked again here - it falls through to the ordinary deadline handling
  # above, against whichever pid is current.
  if [ "$STUCK_CHECKED" -eq 0 ] && [ "$(( NOW_WALL_EPOCH - REVIEWER_START_EPOCH ))" -ge "$LAUNCH_WATCH_SECONDS" ]; then
    STUCK_CHECKED=1
    STUCK_REASON=""
    if is_reviewer_stuck "$REVIEWER_PID"; then
      STUCK_REASON="rss below ${STUCK_RSS_KB}KB, cpu time 0:00.00"
    elif reviewer_log_is_startup_notice; then
      STUCK_REASON="no progress beyond startup notice"
    fi
    if [ -n "$STUCK_REASON" ]; then
      STUCK_DETECTED_ELAPSED=$(( NOW_WALL_EPOCH - REVIEWER_START_EPOCH ))
      log "$DATE: reviewer attempt 1 (pid $REVIEWER_PID) looks stuck at launch after ${STUCK_DETECTED_ELAPSED}s ($STUCK_REASON); killing"
      kill -TERM "$REVIEWER_PID" 2>/dev/null || true
      KWAIT=0
      while [ "$KWAIT" -lt "$KILL_GRACE_SECONDS" ] && kill -0 "$REVIEWER_PID" 2>/dev/null; do
        sleep 1
        KWAIT=$((KWAIT + 1))
      done
      if kill -0 "$REVIEWER_PID" 2>/dev/null; then
        kill -KILL "$REVIEWER_PID" 2>/dev/null || true
      fi
      log "$DATE: reviewer attempt 1 (pid $REVIEWER_PID) killed after ${STUCK_DETECTED_ELAPSED}s stuck at launch"
      cat "$REVIEWER_LOG" >> "$LOG_FILE" 2>/dev/null || true
      mv -f "$REVIEWER_LOG" "$TMP/scheduled-review-reviewer-$DATE-attempt1.log" 2>/dev/null || true
      # The cross-reviewer's re-review of c08a627a: this relaunch recheck does not
      # compare RECHECK_EPOCH to CUTOFF_EPOCH/HANDOFF_EPOCH directly - it
      # does not need to. For a scheduled run, REVIEWER_DEADLINE_EPOCH was
      # already bounded to never exceed CUTOFF_EPOCH (the IS_SCHEDULED_RUN
      # gates above), so RECHECK_EPOCH >= CUTOFF_EPOCH implies
      # REMAINING_AFTER_KILL <= 0 here already, which the KILL_GRACE_SECONDS
      # comparison below always catches. Relying on the one shared deadline,
      # rather than re-deriving a second cutoff comparison here, is what
      # keeps this and the ordinary ${REVIEWER_DEADLINE_EPOCH} timeout path
      # (above) a single source of truth for "is there still time".
      RECHECK_EPOCH="$(date +%s)"
      REMAINING_AFTER_KILL=$(( REVIEWER_DEADLINE_EPOCH - RECHECK_EPOCH ))
      if [ "$REMAINING_AFTER_KILL" -le "$KILL_GRACE_SECONDS" ]; then
        # Not relaunching: deliberately do NOT reap attempt 1's pid here -
        # REVIEWER_PID is left pointing at it, unchanged, so the single
        # trailing `wait "$REVIEWER_PID"` after the loop (same as the
        # ordinary deadline path above) is the one and only reap. A second
        # `wait` on an already-reaped pid would error instead of returning
        # the real exit status.
        log "$DATE: not relaunching - only ${REMAINING_AFTER_KILL}s left before the deadline (kill grace ${KILL_GRACE_SECONDS}s)"
        TIMED_OUT=1
        break
      fi
      # Relaunching: reap attempt 1's zombie now, since REVIEWER_PID is
      # about to be reassigned to attempt 2 by launch_reviewer and the
      # trailing wait after the loop will only ever see attempt 2's pid.
      wait "$REVIEWER_PID" 2>/dev/null
      launch_reviewer 2
    fi
  fi
  sleep 1
done
wait "$REVIEWER_PID" 2>/dev/null
REVIEWER_RC=$?
REVIEWER_END_EPOCH="$(date +%s)"
REVIEWER_ELAPSED_SECONDS=$(( REVIEWER_END_EPOCH - REVIEWER_START_EPOCH ))
cat "$REVIEWER_LOG" >> "$LOG_FILE" 2>/dev/null || true
rm -rf "$STAGE_PAGES" 2>/dev/null || true

# mc-0kpzk: reviewer start/finish/elapsed, logged on every run (pass or
# fail) so a future timeout has timing evidence even when everything else
# about the run looks unremarkable.
log "$DATE: reviewer started $(date -r "$REVIEWER_START_EPOCH" '+%F %T'), finished $(date -r "$REVIEWER_END_EPOCH" '+%F %T'), elapsed ${REVIEWER_ELAPSED_SECONDS}s (budget ${EFFECTIVE_TIMEOUT_SECONDS}s, timed_out=$TIMED_OUT, rc=$REVIEWER_RC)"

if [ "$TIMED_OUT" -eq 1 ]; then
  keep_reviewer_log
  fail "independent reviewer (codex exec) timed out after ${EFFECTIVE_TIMEOUT_SECONDS}s and was killed"
fi
# A nonzero exit is a hard failure even if the reviewer already wrote a
# passing receipt (mc-mi8xx hardening, the cross-reviewer's second review): a reviewer
# that crashes, is interrupted, or otherwise does not return cleanly is not
# trusted just because a well-formed receipt happens to be sitting there.
if [ "$REVIEWER_RC" -ne 0 ]; then
  keep_reviewer_log
  fail "independent reviewer (codex exec) exited $REVIEWER_RC; not trusting anything it wrote"
fi

# ---------------------------------------------------------------------------
# (f) The gate, run by this script, never by the reviewer's own say-so.
# First, re-hash the REAL PDF and manifest: if either changed while the
# reviewer was running - the reviewer reaching outside its sandbox, or
# anything else - refuse regardless of what the receipt says. Then check the
# STAGED receipt the reviewer wrote against the REAL PDF and manifest (not
# the staged copies): the receipt binds pdf_sha256 and manifest_sha256, so a
# receipt written against a tampered staged copy will not match the real
# bytes either. Only after that passes does anything land in the archive.
# ---------------------------------------------------------------------------
REAL_PDF_SHA_AFTER="$(sha256_of "$PDF")" || fail "could not hash the real PDF at $PDF after review"
REAL_MANIFEST_SHA_AFTER="$(sha256_of "$MANIFEST")" || fail "could not hash the real manifest at $MANIFEST after review"
if [ "$REAL_PDF_SHA_AFTER" != "$REAL_PDF_SHA_BEFORE" ]; then
  fail "the real PDF at $PDF changed during the review window; refusing to publish"
fi
if [ "$REAL_MANIFEST_SHA_AFTER" != "$REAL_MANIFEST_SHA_BEFORE" ]; then
  fail "the real manifest at $MANIFEST changed during the review window; refusing to publish"
fi

if [ ! -f "$STAGE_RECEIPT" ]; then
  keep_reviewer_log
  fail "the independent reviewer exited 0 but wrote no receipt"
fi

CHECK_LOG="$TMP/scheduled-review-check-$DATE.log"
if ! "$PYTHON_BIN" "$RECEIPT_TOOL" --archive-root "$ARCHIVE_ROOT" check \
    --date "$DATE" --pdf "$PDF" --manifest "$MANIFEST" --receipt "$STAGE_RECEIPT" \
    --producer "$PRODUCER" >"$CHECK_LOG" 2>&1; then
  CHECK_OUT="$(cat "$CHECK_LOG")"
  cat "$CHECK_LOG" >> "$LOG_FILE"
  keep_reviewer_log
  fail "review receipt did not pass check against the real document:
$CHECK_OUT"
fi
cat "$CHECK_LOG" >> "$LOG_FILE"

# ---------------------------------------------------------------------------
# (g) Publish. Only now, after a passing check against the real bytes, does
# anything land in OUT_DIR - atomically (write a temp name in OUT_DIR, then
# rename), and re-checked once more on the final path before this script
# calls it done.
# ---------------------------------------------------------------------------
PUBLISH_TMP="$(mktemp "$OUT_DIR/.review-receipt.json.XXXXXX" 2>/dev/null || true)"
if [ -z "$PUBLISH_TMP" ]; then
  fail "could not create a temp file in $OUT_DIR to publish the receipt"
fi
if ! cp "$STAGE_RECEIPT" "$PUBLISH_TMP"; then
  rm -f "$PUBLISH_TMP" 2>/dev/null || true
  fail "could not copy the staged receipt into $OUT_DIR"
fi
mv -f "$PUBLISH_TMP" "$RECEIPT"

if ! "$PYTHON_BIN" "$RECEIPT_TOOL" --archive-root "$ARCHIVE_ROOT" check \
    --date "$DATE" --pdf "$PDF" --manifest "$MANIFEST" --receipt "$RECEIPT" \
    --producer "$PRODUCER" >>"$LOG_FILE" 2>&1; then
  rm -f "$RECEIPT" 2>/dev/null || true
  keep_reviewer_log
  fail "the published receipt at $RECEIPT failed re-check after publish; removed it"
fi

rm -rf "$STAGE_DIR" 2>/dev/null || true
log "$DATE: review receipt VALID; scheduled review complete, the 06:00 job may push --no-generate"
exit 0
