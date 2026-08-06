#!/usr/bin/env bash
# perf-snap.sh - unattended perf capture of a long-running process, one file per phase.
#
#   sudo ./perf-snap.sh --name serened               watch it, capture whenever the phase changes
#   sudo ./perf-snap.sh --pid 4909 --window 100      100s captures, like the hand-run ones
#   sudo ./perf-snap.sh --pid 4909 --every 600       also capture every 10 min regardless
#   sudo ./perf-snap.sh --pid 4909 --periodic        ONLY periodic, no phase detection
#   sudo ./perf-snap.sh --name serened --disk /mnt/data/oracle/serene-clean
#   ./perf-snap.sh --list                            what has been captured so far
#
# Start it detached so it outlives the terminal:
#   sudo setsid nohup ./perf-snap.sh --name serened > /dev/null 2>&1 &
#
# ## What this replaces
#
# Running this by hand, ten times over fourteen hours, guessing when something interesting was
# happening:
#
#   sudo perf record -F 199 -g --call-graph fp -p 4909 -o /tmp/commit-now9.data -- sleep 100 \
#     && sudo chown $USER /tmp/commit-now9.data
#
# Three problems with that, all of which cost something real:
#
#   * the name carries no information. `commit-now7.data` does not say what the process was doing,
#     so the phase has to be reconstructed later from timestamps and memory. Here the filename IS
#     the observation: `snap-007-233214-t1-c98-w0-spin.data`.
#   * you have to be awake. The most valuable capture of the whole run - the index build - lasted
#     about twelve minutes at the end of a fourteen-hour load, and was caught by luck.
#   * the chown is a separate command, so it gets forgotten. 643 MB of captures from an interrupted
#     run ended up root-owned 0600: unreadable, and unmovable, because rename(2) on a root-owned
#     directory needs write permission on the directory itself.
#
# ## What a "phase" is here, and why this generalises past SereneDB
#
# A phase is not a database concept. It is a period during which a process's observable behaviour is
# steady, and a phase change is when that behaviour steps to something else. Everything needed to see
# that is in /proc, for any process, with no instrumentation and no knowledge of what the program is:
#
#   runnable threads   /proc/<pid>/task/*/stat. Parallel work and serial work look completely
#                      different here, and the difference is the single most informative bit. A
#                      2.79M-row load ran on ONE runnable thread for thirteen hours and then used
#                      nine for the last twelve minutes; those are two different programs, in effect.
#   cpu rate           /proc/<pid>/stat utime+stime. Distinguishes busy from blocked, which threads
#                      alone cannot: one runnable thread at 100% and one at 3% are not the same phase.
#   context switches   /proc/<pid>/task/*/status, voluntary vs involuntary, per cpu-second. THIS is
#                      what separates a spin from honest work, and it is the signal that would have
#                      identified the feeder spin on day one. A loop polling a buffer burns CPU
#                      without yielding: very few voluntary switches per cpu-second. A worker that
#                      blocks on IO or a lock yields constantly. Same cpu%, same thread count,
#                      opposite meaning.
#   io                 /proc/<pid>/io write_bytes. Buffering, writing and merging differ here even
#                      when cpu and thread count do not.
#   memory             VmRSS + VmSwap. Growing, flat or shrinking. Not RSS alone - under swap RSS
#                      falls while the process still holds everything, which read as "finishing"
#                      three separate times.
#
# Each is bucketed coarsely and joined into a signature like `t1-c98-w0-spin-grow`. When the
# signature holds a NEW value for STABLE consecutive polls, that is a phase change, and that is when
# recording starts. Coarse buckets on purpose: fine ones would retrigger on noise, and the point is
# to spend the disk on transitions rather than on a uniform sample of a mostly-uniform run.
#
# Edge-triggered beats fixed-interval for the same reason a scheduled window could not catch the
# index build: a phase that lasts twelve minutes inside fourteen hours is 1.4% of the run, so a
# uniform sampler either misses it or averages it away. `--every N` is still available as a floor,
# so a run that never changes phase still produces a baseline.
#
# ## What it does NOT do
#
# It opens no connection to the target, runs no query, and reads nothing but /proc. A `count(*)`
# probe against a loaded server is what wedged a session and left a core spinning for 3.6 hours.
set -uo pipefail

TARGET_PID=""
TARGET_NAME=""
CONTAINER=""
DISK_DIR=""
WINDOW="${WINDOW:-30}" # seconds of recording per capture
POLL="${POLL:-5}"      # seconds between /proc samples
STABLE="${STABLE:-2}"  # consecutive polls a new signature must hold before it counts
EVERY="${EVERY:-0}"    # also capture every N seconds; 0 = phase changes only
FREQ="${FREQ:-199}"    # perf -F
MAX_CAPTURES="${MAX_CAPTURES:-200}"
MAX_MB="${MAX_MB:-8192}" # stop before the captures fill the disk
PERIODIC_ONLY=0
LIST=0

while [ "$#" -gt 0 ]; do
	case "$1" in
	--pid)
		TARGET_PID="${2:-}"
		shift 2
		;;
	--container)
		# Resolve the pid from a container rather than being handed one. A pid is only valid until
		# the process restarts, and the thing being profiled here is a database that may restart
		# under a watcher that outlives it; the container name does not change.
		CONTAINER="${2:-}"
		shift 2
		;;
	--name)
		TARGET_NAME="${2:-}"
		shift 2
		;;
	--disk)
		DISK_DIR="${2:-}"
		shift 2
		;;
	--window)
		WINDOW="${2:-}"
		shift 2
		;;
	--every)
		EVERY="${2:-}"
		shift 2
		;;
	--poll)
		POLL="${2:-}"
		shift 2
		;;
	--freq)
		FREQ="${2:-}"
		shift 2
		;;
	--out)
		OUT_DIR="${2:-}"
		shift 2
		;;
	--periodic)
		PERIODIC_ONLY=1
		EVERY="${EVERY:-900}"
		[ "$EVERY" -eq 0 ] && EVERY=900
		shift
		;;
	--list)
		LIST=1
		shift
		;;
	-h | --help)
		sed -n '2,20p' "$0"
		exit 0
		;;
	*)
		echo "unknown argument: $1" >&2
		exit 1
		;;
	esac
done

owner="${SUDO_USER:-$(id -un)}"
owner_home="$(getent passwd "$owner" | cut -d: -f6)"
[ -n "$owner_home" ] || owner_home="$HOME"
OUT_DIR="${OUT_DIR:-${SERENEDASH_PERF_DIR:-$owner_home/.cache/serenedash/perf}}"

if [ "$LIST" -eq 1 ]; then
	printf '  %-52s %8s  %s\n' CAPTURE SIZE 'PHASE (from the name)'
	find "$OUT_DIR" -name 'snap-*.data' 2>/dev/null | sort | while read -r f; do
		b="$(basename "$f" .data)"
		printf '  %-52s %8s  %s\n' "$b" "$(du -h "$f" | cut -f1)" "${b#snap-*-*-}"
	done
	exit 0
fi

[ "$(id -u)" -eq 0 ] || {
	echo "needs root for perf:  sudo $0 $*" >&2
	exit 1
}

if [ -z "$TARGET_PID" ] && [ -n "${CONTAINER:-}" ]; then
	TARGET_PID="$(docker inspect -f '{{.State.Pid}}' "$CONTAINER" 2>/dev/null)"
	case "$TARGET_PID" in '' | 0 | *[!0-9]*) TARGET_PID="" ;; esac
fi
if [ -z "$TARGET_PID" ] && [ -n "$TARGET_NAME" ]; then
	TARGET_PID="$(pgrep -x "$TARGET_NAME" | head -1)"
fi
# An if, not `A && B || C`: with the latter, C runs whenever A && B is false, which is what is
# wanted here but reads as if-then-else and is not. shellcheck flags it (SC2015) for that reason.
if ! { [ -n "$TARGET_PID" ] && [ -d "/proc/$TARGET_PID" ]; }; then
	echo "no such process (use --pid, --name or --container)" >&2
	exit 1
fi

COMM="$(tr -d '\n' <"/proc/$TARGET_PID/comm" 2>/dev/null)"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$OUT_DIR/snap-$COMM-$STAMP"
mkdir -p "$OUT" || {
	echo "cannot create $OUT" >&2
	exit 1
}

# Hand the output back on every exit path, not once at the end. perf writes .data at 0600 as root;
# an interrupted run that never chowns leaves captures its owner can neither read nor move.
hand_back() {
	chown -R "$owner" "$OUT" 2>/dev/null
	return 0
}
# The pid of the capture in flight, so a signal can stop it rather than leaving perf writing into a
# file nobody will read.
capture_pid=""

# EXIT hands the output back and nothing else - it runs on every path including the signal one.
# INT and TERM hand it back and LEAVE, which is the whole point: a handler that returns normally
# resumes the script, and this one used to, so ^C chowned the captures and went round the loop
# again. Sixteen of them did not stop a three-and-a-half-hour run.
on_signal() {
	trap - INT TERM # a second ^C is immediate, whatever this handler is doing
	say "interrupted - stopping after $n capture(s)"
	if [ -n "$capture_pid" ]; then
		kill -TERM "$capture_pid" 2>/dev/null
		wait "$capture_pid" 2>/dev/null
	fi
	hand_back
	exit 130
}

trap hand_back EXIT
trap on_signal INT TERM
hand_back

MANIFEST="$OUT/manifest.tsv"
printf 'n\twall\telapsed_s\tsignature\ttrigger\tthreads\tcpu_pct\twrite_mbs\tvol_per_cpus\tcommitted_gb\tfile\n' >"$MANIFEST"
LOG="$OUT/snap.log"
say() { printf '%s  %s\n' "$(date '+%H:%M:%S')" "$*" | tee -a "$LOG"; }

HZ="$(getconf CLK_TCK)"

# ── the cheap /proc signals ─────────────────────────────────────────────────────────────────────
cpu_ticks() { awk '{print $14+$15}' "/proc/$TARGET_PID/stat" 2>/dev/null; }
runnable() { grep -lc '^[0-9]* ([^)]*) R' /proc/"$TARGET_PID"/task/*/stat 2>/dev/null | wc -l; }
committed_kb() { awk '/^VmRSS:|^VmSwap:/{s+=$2} END{print s+0}' "/proc/$TARGET_PID/status" 2>/dev/null; }
written_kb() { awk '/^write_bytes:/{print int($2/1024)}' "/proc/$TARGET_PID/io" 2>/dev/null; }
# Summed over every thread. The process-level counters in /proc/<pid>/status cover the group leader
# only on some kernels, and the leader is usually the one thread that is NOT doing the work.
switches() {
	awk '/^voluntary_ctxt_switches:/{v+=$2} /^nonvoluntary_ctxt_switches:/{n+=$2}
       END{print v+0, n+0}' /proc/"$TARGET_PID"/task/*/status 2>/dev/null
}
disk_mb() { [ -n "$DISK_DIR" ] && du -sm "$DISK_DIR" 2>/dev/null | cut -f1 || echo 0; }

# ── bucketing ───────────────────────────────────────────────────────────────────────────────────
# Coarse on purpose. Fine buckets retrigger on noise and spend the disk on nothing; these are the
# boundaries where the BEHAVIOUR is actually different, not where the number crosses a round figure.
bucket_threads() { # serial / a few / parallel
	case "${1:-0}" in
	0 | 1) echo "t1" ;;
	2 | 3) echo "t3" ;;
	4 | 5 | 6 | 7 | 8) echo "t8" ;;
	*) echo "t9+" ;;
	esac
}
bucket_cpu() {
	awk -v c="${1:-0}" 'BEGIN{
    if (c < 20) print "c0"; else if (c < 150) print "c100";
    else if (c < 400) print "c300"; else print "c900" }'
}
bucket_write() { # MB/s of actual block writes
	awk -v w="${1:-0}" 'BEGIN{
    if (w < 1) print "w0"; else if (w < 50) print "w10"; else print "w100" }'
}
# Voluntary switches per cpu-second. A spin loop burns CPU without yielding, so this collapses toward
# zero; anything that blocks on IO, a lock or a condvar yields constantly. Same cpu%, same thread
# count, completely different phase - and no other cheap signal separates them.
bucket_mode() {
	awk -v s="${1:-0}" 'BEGIN{
    if (s < 50) print "spin"; else if (s < 2000) print "work"; else print "block" }'
}
bucket_mem() {
	awk -v d="${1:-0}" 'BEGIN{
    if (d > 0.5) print "grow"; else if (d < -0.5) print "drop"; else print "flat" }'
}

say "watching pid $TARGET_PID ($COMM), window ${WINDOW}s, poll ${POLL}s"
say "output $OUT"
[ "$EVERY" -gt 0 ] && say "periodic floor: every ${EVERY}s"
[ "$PERIODIC_ONLY" -eq 1 ] && say "periodic ONLY - phase detection off"
[ -n "$DISK_DIR" ] && say "also watching disk $DISK_DIR"

capture() { # capture SIGNATURE TRIGGER THREADS CPU WRITE VOL COMMITTED
	local sig="$1" trig="$2" thr="$3" cpu="$4" wr="$5" vol="$6" com="$7"
	n=$((n + 1))
	local f
	f="$(printf '%s/snap-%03d-%s-%s.data' "$OUT" "$n" "$(date +%H%M%S)" "$sig")"
	say "capture $n [$trig] $sig - ${thr} runnable, cpu ${cpu}%, write ${wr}MB/s, ${vol} vol/cpu-s"
	# Backgrounded and waited on, NOT run in the foreground: bash does not run a trap while a
	# foreground child is executing, so an interrupt sat unhandled until perf finished. With `wait`
	# the handler runs at once and can stop the capture.
	perf record -F "$FREQ" -g --call-graph fp -p "$TARGET_PID" -o "$f" -- sleep "$WINDOW" >/dev/null 2>&1 &
	capture_pid=$!
	wait "$capture_pid" 2>/dev/null
	capture_pid=""
	# A symbol summary next to each capture, so the directory is readable without perf. Cycle-weighted
	# via read-perf.sh where it exists: this box is a hybrid CPU and a plain `perf report | head` reads
	# the E-core table only, which understated one symbol as 35.60% when it was 93.39% on the P-cores.
	if [ -x "$owner_home/Projects/oracle/read-perf.sh" ]; then
		"$owner_home/Projects/oracle/read-perf.sh" "$f" 2>/dev/null | tail -n +3 >"${f%.data}.symbols.txt"
	else
		perf report -i "$f" --stdio --sort symbol --no-children -g none 2>/dev/null |
			grep -E '^ +[0-9]+\.[0-9]+%' | head -15 >"${f%.data}.symbols.txt"
	fi
	printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
		"$n" "$(date '+%H:%M:%S')" "$(($(date +%s) - t0))" "$sig" "$trig" \
		"$thr" "$cpu" "$wr" "$vol" "$com" "$(basename "$f")" >>"$MANIFEST"
	say "  top: $(head -1 "${f%.data}.symbols.txt" 2>/dev/null | sed 's/^ *//' | cut -c1-64)"
	hand_back
	notify_dashboard
}

# Tell a running serenedash that a new capture is ready.
#
# The dashboard rescans its perf directory on every tick anyway, so this only removes the
# latency between a capture landing and it appearing. Deliberately best-effort: a stale pid
# file, no dashboard running, or a dashboard owned by another user all fail silently, because
# a profiler that stops profiling when nobody is watching would be a worse tool.
notify_dashboard() {
	local pf="$OUT_DIR/.serenedash.pid" pid
	[ -r "$pf" ] || return 0
	read -r pid <"$pf" 2>/dev/null || return 0
	case "$pid" in '' | *[!0-9]*) return 0 ;; esac
	kill -USR1 "$pid" 2>/dev/null && say "  signalled serenedash (pid $pid)"
	return 0
}

t0=$(date +%s)
n=0
last_sig=""
cand_sig=""
cand_run=0
last_periodic=$t0
prev_ticks="$(cpu_ticks)"
read -r -a prev_sw <<<"$(switches)"
prev_wr="$(written_kb)"
prev_com="$(committed_kb)"
prev_t=$t0

while :; do
	# Same reason as the capture: a foreground sleep defers the trap until it returns.
	sleep "$POLL" &
	capture_pid=$!
	wait "$capture_pid" 2>/dev/null
	capture_pid=""

	kill -0 "$TARGET_PID" 2>/dev/null || {
		say "target exited after $(($(date +%s) - t0))s, $n captures"
		exit 0
	}

	now=$(date +%s)
	dt=$((now - prev_t))
	[ "$dt" -lt 1 ] && dt=1

	ticks="$(cpu_ticks)"
	read -r -a sw <<<"$(switches)"
	wr="$(written_kb)"
	com="$(committed_kb)"
	thr="$(runnable)"

	cpu=$(awk -v a="${prev_ticks:-0}" -v b="${ticks:-0}" -v s="$dt" -v hz="$HZ" \
		'BEGIN{printf "%.0f", (b-a)/hz/s*100}')
	wr_mbs=$(awk -v a="${prev_wr:-0}" -v b="${wr:-0}" -v s="$dt" 'BEGIN{printf "%.1f", (b-a)/1024/s}')
	com_gb=$(awk -v k="${com:-0}" 'BEGIN{printf "%.1f", k/1048576}')
	com_d=$(awk -v a="${prev_com:-0}" -v b="${com:-0}" 'BEGIN{printf "%.2f", (b-a)/1048576}')
	# Voluntary switches per cpu-SECOND, not per wall-second: a process using nine cores yields nine
	# times as often for the same behaviour, and dividing by wall time would call that a phase change.
	vol_rate=$(awk -v a="${prev_sw[0]:-0}" -v b="${sw[0]:-0}" -v ta="${prev_ticks:-0}" -v tb="${ticks:-0}" \
		-v hz="$HZ" 'BEGIN{ cs=(tb-ta)/hz; if (cs<0.01) cs=0.01; printf "%.0f", (b-a)/cs }')

	prev_ticks="$ticks"
	prev_sw=("${sw[@]}")
	prev_wr="$wr"
	prev_com="$com"
	prev_t="$now"

	sig="$(bucket_threads "$thr")-$(bucket_cpu "$cpu")-$(bucket_write "$wr_mbs")"
	sig="$sig-$(bucket_mode "$vol_rate")-$(bucket_mem "$com_d")"

	# Stop before the captures become the problem. A continuous frame-pointer record of one long run
	# was 669 MB on its own; a watcher left running for days without a cap is a disk-full incident.
	used_mb=$(du -sm "$OUT" 2>/dev/null | cut -f1)
	if [ "${used_mb:-0}" -ge "$MAX_MB" ] || [ "$n" -ge "$MAX_CAPTURES" ]; then
		say "budget reached (${used_mb}MB, $n captures) - stopping"
		exit 0
	fi

	if [ "$PERIODIC_ONLY" -eq 0 ]; then
		if [ "$sig" != "$last_sig" ]; then
			if [ "$sig" = "$cand_sig" ]; then
				cand_run=$((cand_run + 1))
			else
				cand_sig="$sig"
				cand_run=1
			fi
			# Hold the new signature for STABLE polls before believing it. Without this every
			# momentary blip between two steady phases produces its own capture.
			if [ "$cand_run" -ge "$STABLE" ]; then
				trig="phase"
				[ -z "$last_sig" ] && trig="first"
				capture "$sig" "$trig" "$thr" "$cpu" "$wr_mbs" "$vol_rate" "$com_gb"
				last_sig="$sig"
				cand_run=0
				last_periodic=$(date +%s)
				prev_ticks="$(cpu_ticks)"
				prev_t=$(date +%s)
			fi
		else
			cand_run=0
		fi
	fi

	if [ "$EVERY" -gt 0 ] && [ $((now - last_periodic)) -ge "$EVERY" ]; then
		capture "$sig" "periodic" "$thr" "$cpu" "$wr_mbs" "$vol_rate" "$com_gb"
		last_sig="$sig"
		last_periodic=$(date +%s)
		prev_ticks="$(cpu_ticks)"
		prev_t=$(date +%s)
	fi
done
