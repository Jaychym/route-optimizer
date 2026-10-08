#!/usr/bin/env python3
"""Route Optimizer v3.4: IPFIX-driven dual-WAN /24 BGP optimization.

Dry-run by default. Keep v2 and its state file until all old /32s are withdrawn.
All route changes may reset existing TCP/UDP sessions; flow quietness is an
approximation, not conntrack. Requires UCG filter and BGP maximum-prefix safety.
"""

import argparse
import collections
import concurrent.futures
import fcntl
import ipaddress
import json
import math
import os
import re
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

# --- Site settings -----------------------------------------------------------
NFDUMP = "/usr/bin/nfdump"
EXABGP_CLI = "/usr/bin/exabgp-cli"
RBGPD_CLI = "/usr/local/bin/rbgp"
RBGPD_SOCKET = "unix:///var/lib/rustbgpd/grpc.sock"
BGP_BACKEND = "exabgp"  # Explicit opt-in to rustbgpd. Existing installs keep working.
RBGPD_RECONCILE_SECONDS = 300
FLOW_DIR = "/var/cache/nfdump"
STATE_FILE = Path("/var/lib/route-optimizer/state-v3.json")
STATUS_FILE = Path("/var/lib/route-optimizer/dashboard-v3.json")
HISTORY_FILE = Path("/var/lib/route-optimizer/quality-history-v3.json")
ROUTE_EVENTS_FILE = Path("/var/lib/route-optimizer/route-events-v3.json")
HISTORY_MAX_SAMPLES = 1440           # roughly 24 hours at 60-second cycles
ROUTE_EVENTS_MAX = 500              # persistent audit display, bounded size
LOCK_FILE = "/run/route-optimizer-v3.lock"
ACTIVE_STATE_PATH = STATE_FILE
# Site-specific settings are loaded from an untracked JSON file before running.
# Do not put WAN gateways, LAN addresses or real site configuration in Git.
UCG_EXPORTER = None
BGP_PEER = None
OWN_IPS = set()
INTERNAL_NETWORKS = ()
BASE_BLOCKED_NETWORKS = tuple(map(ipaddress.ip_network, [
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
    "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24",
    "192.0.2.0/24", "192.168.0.0/16", "198.18.0.0/15",
    "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4",
    "240.0.0.0/4",
]))
BLOCKED_NETWORKS = BASE_BLOCKED_NETWORKS
ISPS = {}
ISP_BY_NEXTHOP = {}
ISP_LABELS = {"ISPA": "ISPA", "ISPB": "ISPB"}


def display_isp(value):
    """Format opaque ISP identifiers for operator-facing text only.

    Stored route state, IPFIX measurements and command keys remain ISPA/ISPB.
    """
    return re.sub(r"\bISPA\b|\bISPB\b", lambda m: ISP_LABELS[m.group()], str(value))


def configure_site(cfg):
    """Validate site configuration and initialize module constants, fail-closed.

    Both BGP next-hops must be inside explicitly declared connected WAN
    networks. Examples use documentation addresses and are not real routes.
    """
    global UCG_EXPORTER, BGP_PEER, OWN_IPS, INTERNAL_NETWORKS
    global BLOCKED_NETWORKS, ISPS, ISP_BY_NEXTHOP, ISP_LABELS
    if not isinstance(cfg, dict):
        raise ValueError("site config must be a JSON object")
    def v4(value):
        obj = ipaddress.ip_address(value)
        if not isinstance(obj, ipaddress.IPv4Address):
            raise ValueError(f"IPv6 is not supported: {value}")
        return str(obj)
    def net(value):
        obj = ipaddress.ip_network(value, strict=True)
        if not isinstance(obj, ipaddress.IPv4Network):
            raise ValueError(f"IPv6 is not supported: {value}")
        return obj
    exporter = v4(cfg["ucg_exporter"])
    peer = v4(cfg.get("bgp_peer", cfg["ucg_exporter"]))
    internal = tuple(net(x) for x in cfg["internal_networks"])
    connected = tuple(net(x) for x in cfg["wan_connected_networks"])
    if not internal or len(connected) < 2:
        raise ValueError("internal_networks and both wan_connected_networks are required")
    own = {v4(x) for x in cfg["own_ips"]}
    expected = {"ISPA", "ISPB"}
    declared = cfg["isps"]
    if set(declared) != expected:
        raise ValueError("isps must have ISPA and ISPB keys")
    raw_labels = cfg.get("isp_labels", {})
    if not isinstance(raw_labels, dict) or not set(raw_labels) <= expected:
        raise ValueError("isp_labels must be an object containing only ISPA/ISPB keys")
    labels = {}
    for name in ("ISPA", "ISPB"):
        label = raw_labels.get(name, name)
        if (not isinstance(label, str) or not 1 <= len(label) <= 24
                or label != label.strip()
                or any(ord(ch) < 32 or ord(ch) == 127 for ch in label)):
            raise ValueError(f"isp_labels.{name} must be a printable 1–24 character name")
        labels[name] = label
    if labels["ISPA"].casefold() == labels["ISPB"].casefold():
        raise ValueError("ISP display names must be different")
    providers = {}
    for name in ("ISPA", "ISPB"):
        item = declared[name]
        providers[name] = {"source": v4(item["source"]),
                           "next_hop": v4(item["next_hop"])}
        if not any(ipaddress.ip_address(providers[name]["next_hop"]) in n
                   for n in connected):
            raise ValueError(f"{name} next_hop is outside wan_connected_networks")
        if providers[name]["source"] not in own:
            raise ValueError(f"{name} probe source must also be listed in own_ips")
    if len({x["source"] for x in providers.values()}) != 2 or len({x["next_hop"] for x in providers.values()}) != 2:
        raise ValueError("ISP source addresses and next-hops must be distinct")
    UCG_EXPORTER = exporter
    BGP_PEER = peer
    OWN_IPS = own
    INTERNAL_NETWORKS = internal
    BLOCKED_NETWORKS = BASE_BLOCKED_NETWORKS + internal + connected + tuple(
        net(x) for x in cfg.get("extra_blocked_networks", []))
    ISPS = providers
    ISP_BY_NEXTHOP = {v["next_hop"]: k for k, v in ISPS.items()}
    ISP_LABELS = labels


PREFIX_LENGTH = 24

# --- Guardrails / tuning -----------------------------------------------------
TRAFFIC_WINDOW = 600                  # 10 minutes for ranking
FLOW_LOOKBACK = 1800                 # 30 minutes for quiet detection
MIN_BYTES = 128 * 1024               # 128 KiB per /24 in 10 minutes
DEFAULT_TOP = 50                     # plus all pending and installed routes
DEFAULT_ROUTE_LIMIT = 50            # optional --max-routes 100 later
HARD_MAX_ROUTES = 100
PROBE_HOSTS_PER_PREFIX = 3           # only observed destinations, not sweeping
PROBE_ATTEMPTS = 5
PROBE_TIMEOUT = 1.5
PROBE_PAUSE = 0.10
MAX_WORKERS = 12
MIN_SCORE_IMPROVEMENT = 5.0
MIN_PERCENT_IMPROVEMENT = 15.0
WIN_STREAK_REQUIRED = 3
MIN_WIN_INTERVAL = 45                # seconds between qualifying cycles
MAX_WIN_GAP = 300                    # old wins must not persist across downtime
QUIET_SECONDS = 180                  # entire /24 must be quiet (3 minutes)
SINGLE_HOST_QUIET_SECONDS = 300      # sparse /24 requires 5 minutes quiet
SINGLE_HOST_MIN_SCORE = 8.0          # sparse /24 requires stronger evidence
SINGLE_HOST_MIN_PERCENT = 25.0
HOLD_DOWN = 600                     # 10 minutes
EXPIRE_AFTER = 900                  # 15 minutes without traffic
EMERGENCY_CONFIRMATIONS = 2          # two monitoring rounds
EMERGENCY_MIN_HOSTS = 2              # never fail over /24 based on one target
EMERGENCY_LOSS = 5.0
ALTERNATE_MAX_LOSS = 1.0
STATE_GC = 86400                     # keep inactive candidate state 1 day
LOSS_WEIGHT = 25.0
JITTER_WEIGHT = 2.0
CURRENT_WAN_CONFIDENCE = 0.80         # block mixed ISP /24s


def ts():
    return time.time()


def age_label(seconds):
    if seconds is None or math.isinf(seconds):
        return "unknown"
    sec = max(0, int(seconds))
    return f"{sec}s" if sec < 60 else f"{sec//60}m{sec%60:02d}s"


def parse_timestamp(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def is_internal(ip):
    try:
        addr = ipaddress.ip_address(ip)
        return isinstance(addr, ipaddress.IPv4Address) and any(addr in net for net in INTERNAL_NETWORKS)
    except ValueError:
        return False


def eligible_prefix(ip):
    """Use only globally routable /24s that do not overlap our blocklist."""
    try:
        addr = ipaddress.ip_address(ip)
        if not isinstance(addr, ipaddress.IPv4Address) or not addr.is_global:
            return None
        net = ipaddress.ip_network(f"{addr}/{PREFIX_LENGTH}", strict=False)
        if not net.network_address.is_global:
            return None
        if any(net.overlaps(blocked) for blocked in BLOCKED_NETWORKS):
            return None
        return str(net)
    except ValueError:
        return None


def command(cmd, timeout=25):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def new_state():
    return {"version": 3, "prefix_length": PREFIX_LENGTH, "routes": {}, "destinations": {}}


def load_state(path):
    if not path.exists():
        return new_state()
    with path.open(encoding="utf-8") as fh:
        state = json.load(fh)
    if state.get("version") != 3 or state.get("prefix_length") != PREFIX_LENGTH:
        raise ValueError(f"Wrong state version/prefix length in {path}; refusing to overwrite")
    if not isinstance(state.get("routes"), dict) or not isinstance(state.get("destinations"), dict):
        raise ValueError(f"Invalid state structure in {path}; refusing to overwrite")
    if len(state["routes"]) > HARD_MAX_ROUTES:
        raise ValueError("Recorded route count exceeds the hard safety ceiling")
    return state


def save_state(state, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=str(path.parent), prefix=".v3-state-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def write_status(payload):
    """Publish a world-readable *summary*, never the optimizer's state file.

    Writes are atomic so the dashboard does not observe half-written JSON.
    History is bounded and is cosmetic; any malformed old summary is ignored.
    """
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    old = {}
    try:
        with STATUS_FILE.open(encoding="utf-8") as fh:
            old = json.load(fh)
    except (FileNotFoundError, ValueError, OSError):
        pass
    history = old.get("events", []) if isinstance(old.get("events"), list) else []
    old_actions = {x.get("prefix"): x.get("action", "").split(" ")[0]
                   for x in old.get("rows", []) if isinstance(x, dict)}
    new_events = []
    for row in payload.get("rows", []):
        action = row["action"]
        category = action.split(" ")[0]
        if category in {"ROUTE", "WITHDRAW", "WOULD", "PENDING", "EMERGENCY", "FAILED"}:
            if category != old_actions.get(row["prefix"]) or category in {"ROUTE", "WITHDRAW", "FAILED"}:
                new_events.append({"timestamp": payload["timestamp"],
                                   "prefix": row["prefix"], "action": action})
    payload["events"] = (new_events + history)[:50]
    fd, name = tempfile.mkstemp(dir=str(STATUS_FILE.parent), prefix=".dashboard-v3-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            os.fchmod(fh.fileno(), 0o644)
            json.dump(payload, fh, allow_nan=False, separators=(",", ":"))
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(name, STATUS_FILE)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _load_public_list(path, max_bytes=2_000_000):
    try:
        if path.stat().st_size > max_bytes:
            raise ValueError(f"Monitoring file too large: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError(f"Monitoring file is not a list: {path}")
        return data
    except FileNotFoundError:
        return []


def _write_public_list(path, data):
    """Atomic public display-only file; never contains state, secrets, or actions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.stem}-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            os.fchmod(fh.fileno(), 0o644)
            json.dump(data, fh, allow_nan=False, separators=(",", ":"))
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def record_route_event(event, prefix, old_isp=None, new_isp=None, reason=""):
    """Log actual accepted BGP CLI actions, never hypothetical dry-run moves."""
    events = _load_public_list(ROUTE_EVENTS_FILE)
    events.insert(0, {
        "epoch": round(ts(), 3),
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "event": event,
        "prefix": prefix,
        "from": old_isp,
        "to": new_isp,
        "reason": reason[:500],
    })
    _write_public_list(ROUTE_EVENTS_FILE, events[:ROUTE_EVENTS_MAX])


def record_quality_sample(rows, timestamp=None):
    """Each ISP summarized from the SAME set of successfully paired prefixes.

    RTT/jitter: median across prefixes. Loss: average of observed probe-loss
    percentages. This is a sampled quality indicator, not an ISP-wide SLA.
    """
    measurements = []
    for row in rows:
        fr, sp = row.get("ispa"), row.get("ispb")
        if not isinstance(fr, dict) or not isinstance(sp, dict):
            continue
        if any(m.get("score") is None for m in (fr, sp)):
            continue
        measurements.append((fr, sp))
    epoch = ts() if timestamp is None else timestamp
    item = {"epoch": round(epoch, 3), "paired_prefixes": len(measurements)}
    for idx, isp in enumerate(("ispa", "ispb")):
        vals = [pair[idx] for pair in measurements]
        item[isp] = {
            "rtt_ms": round(statistics.median(v["latency"] for v in vals), 2) if vals else None,
            "jitter_ms": round(statistics.median(v["jitter"] for v in vals), 2) if vals else None,
            "loss_percent": round(statistics.mean(v["loss"] for v in vals), 2) if vals else None,
        }
    previous = _load_public_list(HISTORY_FILE)
    previous.append(item)
    _write_public_list(HISTORY_FILE, previous[-HISTORY_MAX_SAMPLES:])
    return item


def bgp_health():
    """Query the selected local BGP daemon; unknown is not the same as down."""
    try:
        if BGP_BACKEND == "rustbgpd":
            # `rbgp --json neighbor <ip>` has a documented `state` field.
            p = command([RBGPD_CLI, "-s", RBGPD_SOCKET, "--json", "neighbor", BGP_PEER], timeout=8)
            if p.returncode != 0:
                return {"status": "unknown", "detail": "rustbgpd neighbor query failed"}
            try:
                peer = json.loads(p.stdout)
            except (TypeError, ValueError):
                return {"status": "unknown", "detail": "Invalid rustbgpd neighbor JSON"}
            if not isinstance(peer, dict) or peer.get("address") != BGP_PEER:
                return {"status": "unknown", "detail": "rustbgpd peer identity mismatch"}
            state = str(peer.get("state", "")).lower()
            if state == "established":
                return {"status": "established", "detail": f"rustbgpd peer {BGP_PEER}"}
            if state in {"idle", "connect", "active", "opensent", "openconfirm", "stale"}:
                return {"status": "down", "detail": f"rustbgpd peer {BGP_PEER}: {state}"}
            return {"status": "unknown", "detail": "rustbgpd neighbor state unavailable"}
        p = command(["sudo", EXABGP_CLI, "show neighbor summary"], timeout=8)
        output = ((p.stdout or "") + "\n" + (p.stderr or ""))[:4096]
        if p.returncode != 0:
            return {"status": "unknown", "detail": "ExaBGP CLI query failed"}
        lines = [line.strip() for line in output.splitlines() if BGP_PEER in line]
        if not lines:
            return {"status": "unknown", "detail": "Neighbor not present in CLI summary"}
        status = "established" if any(re.search(r"\bestablished\b", line, re.I) for line in lines) else "down"
        return {"status": status, "detail": f"ExaBGP peer {BGP_PEER}"}
    except (OSError, subprocess.TimeoutExpired):
        return {"status": "unknown", "detail": f"{BGP_BACKEND} CLI unavailable"}


def validate_backend_state(state, apply):
    """Prevent old ExaBGP ownership from being replayed with a new daemon."""
    if not apply or not state["routes"]:
        return
    # Versions <=3.3 recorded no backend; all of their routes used ExaBGP.
    existing = state.get("bgp_backend", "exabgp")
    if existing != BGP_BACKEND:
        raise RuntimeError(
            f"Persisted routes belong to {existing}, not {BGP_BACKEND}. "
            "Withdraw them with the original backend and confirm on the UCG "
            "before changing backend; do not delete state blindly.")


def status_measurement(measurement):
    if not measurement:
        return None
    def number(value):
        try:
            return round(value, 2) if math.isfinite(value) else None
        except (TypeError, ValueError):
            return None
    return {k: number(measurement.get(k)) for k in ("latency", "jitter", "loss", "score")}


def destination_state(state, prefix):
    d = state["destinations"].setdefault(prefix, {})
    for key, default in (
        ("winner", None), ("streak", 0), ("last_win_at", None),
        ("emergency_winner", None), ("emergency_streak", 0),
        ("pending", None), ("last_seen", None), ("last_current", None),
        ("last_current_at", None),
        ("recent_hosts", []), ("updated", ts()),
    ):
        d.setdefault(key, default)
    return d


def read_flows():
    # nfdump -t -N is NOT a wall-clock lookback. It is a legacy interval
    # relative to the first/last flow in the selected capture series and can
    # return "No matching flows" even when fresh nfcapd files are present.
    # Select only recently rotated files by filename, then do the exact
    # wall-clock age check on the NDJSON "last" timestamp below.
    # Our collector rotates every 60 seconds; +120s covers boundary files.
    end = datetime.now()
    start = end - timedelta(seconds=FLOW_LOOKBACK + 120)
    first = f"nfcapd.{start:%Y%m%d%H%M}"
    last_file = f"nfcapd.{end:%Y%m%d%H%M}"
    file_range = f"{FLOW_DIR}/{first}:{last_file}"
    args = [NFDUMP, "-R", file_range,
            "-o", "ndjson", f"router ip {UCG_EXPORTER}"]
    result = command(args, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"nfdump failed (no BGP actions): {result.stderr.strip()}")
    cutoff = ts() - FLOW_LOOKBACK
    for line in result.stdout.splitlines():
        if not line.startswith("{"):
            continue
        try:
            flow = json.loads(line)
        except json.JSONDecodeError:
            continue
        if flow.get("type") != "FLOW":
            continue
        last = parse_timestamp(flow.get("last"))
        if last is not None and last >= cutoff:
            yield flow, last


def collect_flows(state, flows):
    """Aggregate *all* observed outbound/inbound traffic to /24 networks."""
    now = ts()
    out = {}
    for flow, last in flows:
        src, dst = flow.get("src4_addr"), flow.get("dst4_addr")
        if not src or not dst or src in OWN_IPS or dst in OWN_IPS:
            continue
        outbound = False
        if is_internal(src) and not is_internal(dst):
            remote = dst
            outbound = True
            port = flow.get("dst_port")
        elif is_internal(dst) and not is_internal(src):
            remote = src
            port = flow.get("src_port")
        else:
            continue
        prefix = eligible_prefix(remote)
        if not prefix:
            continue
        rec = out.setdefault(prefix, {
            "bytes": 0, "last_seen": 0.0,
            "hosts": {}, "next_hops": collections.Counter(),
        })
        rec["last_seen"] = max(rec["last_seen"], last)
        host = rec["hosts"].setdefault(remote, {
            "bytes": 0, "last_seen": 0.0, "ports": collections.Counter(),
        })
        host["last_seen"] = max(host["last_seen"], last)
        if last < now - TRAFFIC_WINDOW:
            continue
        size = max(0, int(flow.get("in_bytes") or 0))
        rec["bytes"] += size
        host["bytes"] += size
        try:
            protocol = int(flow.get("proto") or 0)
            port = int(port)
        except (TypeError, ValueError):
            protocol, port = 0, 0
        if protocol == 6 and 0 < port <= 65535:
            host["ports"][port] += max(size, 1)
        if outbound:
            nexthop = flow.get("ip4_next_hop")
            if nexthop in ISP_BY_NEXTHOP:
                rec["next_hops"][nexthop] += max(size, 1)

    for prefix, rec in out.items():
        ds = destination_state(state, prefix)
        ds["last_seen"] = max(ds.get("last_seen") or 0, rec["last_seen"])
        ds["updated"] = now
        active_hosts = sorted(rec["hosts"].items(), key=lambda it: it[1]["bytes"], reverse=True)
        best_hosts = []
        for host_ip, info in active_hosts:
            if info["bytes"] == 0:
                continue
            port = info["ports"].most_common(1)[0][0] if info["ports"] else None
            best_hosts.append({"ip": host_ip, "port": port, "bytes": info["bytes"]})
            if len(best_hosts) >= PROBE_HOSTS_PER_PREFIX:
                break
        if best_hosts:
            ds["recent_hosts"] = best_hosts
        if rec["next_hops"]:
            hop, weight = rec["next_hops"].most_common(1)[0]
            total = sum(rec["next_hops"].values())
            ds["last_current"] = ISP_BY_NEXTHOP[hop] if weight / total >= CURRENT_WAN_CONFIDENCE else "MIXED"
            ds["last_current_at"] = now
        elif rec["bytes"] and ds.get("last_current") not in ISPS:
            ds["last_current"] = "UNKNOWN"
    return out


def candidates(state, records, top):
    prefixes = sorted((p for p, r in records.items() if r["bytes"] >= MIN_BYTES),
                      key=lambda p: records[p]["bytes"], reverse=True)[:top]
    selected = set(prefixes) | set(state["routes"])
    selected |= {p for p, d in state["destinations"].items() if d.get("pending")}
    rows = []
    for p in selected:
        if eligible_prefix(p.split("/")[0]) != p:
            continue
        d = destination_state(state, p)
        rec = records.get(p, {})
        hosts = d.get("recent_hosts", [])
        if not hosts:
            continue
        rows.append({"prefix": p, "bytes": rec.get("bytes", 0),
                     "hosts": hosts[:PROBE_HOSTS_PER_PREFIX],
                     "current": d.get("last_current"), "last_seen": d.get("last_seen")})
    rows.sort(key=lambda row: row["bytes"], reverse=True)
    return rows


# --- Path probing ------------------------------------------------------------
def result_score(latency, jitter, loss, method):
    score = (latency + JITTER_WEIGHT * jitter + LOSS_WEIGHT * loss) if math.isfinite(latency) else math.inf
    return {"latency": latency, "jitter": jitter, "loss": loss, "score": score, "method": method}


def tcp_probe(ip, port, source):
    samples = []
    for _ in range(PROBE_ATTEMPTS):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(PROBE_TIMEOUT)
            try:
                sock.bind((source, 0))
                t0 = time.monotonic()
                sock.connect((ip, port))
                samples.append((time.monotonic() - t0) * 1000)
            except (OSError, socket.timeout):
                pass
        time.sleep(PROBE_PAUSE)
    loss = (PROBE_ATTEMPTS - len(samples)) / PROBE_ATTEMPTS * 100.0
    if not samples:
        return result_score(math.inf, math.inf, loss, "tcp")
    return result_score(statistics.median(samples),
                        statistics.pstdev(samples) if len(samples) > 1 else 0.0,
                        loss, "tcp")


def ping_probe(ip, source):
    proc = command(["/usr/bin/ping", "-4", "-n", "-I", source, "-c",
                    str(PROBE_ATTEMPTS), "-W", "1", "-i", "0.2", ip], timeout=12)
    loss = re.search(r"([\d.]+)% packet loss", proc.stdout)
    rtt = re.search(r"=\s*([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)", proc.stdout)
    if not loss or not rtt:
        return result_score(math.inf, math.inf, float(loss.group(1)) if loss else 100.0, "icmp")
    return result_score(float(rtt.group(2)), float(rtt.group(4)), float(loss.group(1)), "icmp")


def probe_host(host, isp):
    source = ISPS[isp]["source"]
    if host.get("port"):
        tcp = tcp_probe(host["ip"], int(host["port"]), source)
        if math.isfinite(tcp["score"]):
            return tcp
    return ping_probe(host["ip"], source)


def probe_all(rows):
    """Probe selected real hosts through both probe VLANs with bounded parallelism."""
    probes = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        pending = {}
        for row in rows:
            for host in row["hosts"]:
                for isp in ISPS:
                    fut = pool.submit(probe_host, host, isp)
                    pending[fut] = (row["prefix"], host["ip"], isp)
        for fut in concurrent.futures.as_completed(pending):
            prefix, host, isp = pending[fut]
            try:
                measurement = fut.result()
            except Exception as exc:
                print(f"WARNING probe {host} {isp}: {exc}", file=sys.stderr)
                measurement = result_score(math.inf, math.inf, 100.0, "error")
            probes.setdefault(prefix, {}).setdefault(host, {})[isp] = measurement
    return probes


def aggregate_measurements(row, individual):
    """Compare same probe method per host. Reject insufficient or conflicting probes."""
    pairs = []
    incomplete = 0
    for host in row["hosts"]:
        vals = individual.get(host["ip"], {})
        ispa, ispb = vals.get("ISPA"), vals.get("ISPB")
        if not ispa or not ispb or ispa["method"] != ispb["method"]:
            incomplete += 1
            continue
        pairs.append((ispa, ispb))
    # At least 2 of 3 or 2 of 2, or 1 of 1 observed targets must be valid
    required = math.ceil(len(row["hosts"]) * (2 / 3))
    if len(pairs) < required:
        return None, "INSUFFICIENT COMPARABLE PROBES"

    results = {}
    for idx, isp in enumerate(ISPS):
        metrics = [pair[idx] for pair in pairs]
        if any(not math.isfinite(item["score"]) for item in metrics):
            results[isp] = result_score(math.inf, math.inf, 100.0, "aggregate")
        else:
            results[isp] = result_score(
                statistics.mean(item["latency"] for item in metrics),
                statistics.mean(item["jitter"] for item in metrics),
                statistics.mean(item["loss"] for item in metrics), "aggregate")

    # Record disagreements across member hosts; conflicting /24s must stay put.
    opposing = set()
    for fr, sp in pairs:
        if not math.isfinite(fr["score"]) or not math.isfinite(sp["score"]):
            continue
        if fr["score"] + MIN_SCORE_IMPROVEMENT <= sp["score"]:
            opposing.add("ISPA")
        if sp["score"] + MIN_SCORE_IMPROVEMENT <= fr["score"]:
            opposing.add("ISPB")
    if len(opposing) > 1:
        return None, "CONFLICTING MEMBER HOSTS"

    results["_host_count"] = len(pairs)
    results["_pairs"] = pairs
    return results, None


# --- BGP action helpers ------------------------------------------------------
def bgp(action, prefix, isp, apply):
    """Perform one route mutation using the explicitly selected BGP backend.

    `--apply` is the sole switch that authorizes writes. No route updates are
    exposed to the dashboard and no BGP daemon is changed in dry-run.
    """
    nh = ISPS[isp]["next_hop"]
    if not apply:
        return True, f"DRY-RUN ({BGP_BACKEND}): {action} {prefix} next-hop {nh}"
    if BGP_BACKEND == "rustbgpd":
        # Fail closed: local API admission does not prove advertisement to UCG.
        if bgp_health()["status"] != "established":
            return False, "rustbgpd peer not confirmed Established"
        if action == "announce":
            cmd = [RBGPD_CLI, "-s", RBGPD_SOCKET, "rib", "add", prefix,
                   "--next-hop", nh]
        elif action == "withdraw":
            cmd = [RBGPD_CLI, "-s", RBGPD_SOCKET, "rib", "delete", prefix]
        else:
            return False, f"unsupported BGP action {action}"
    elif BGP_BACKEND == "exabgp":
        if action not in {"announce", "withdraw"}:
            return False, f"unsupported BGP action {action}"
        cmd = ["sudo", EXABGP_CLI, f"{action} route {prefix} next-hop {nh}"]
    else:
        return False, f"unknown BGP backend {BGP_BACKEND}"
    try:
        proc = command(cmd, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{BGP_BACKEND} CLI error: {exc}"
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "BGP CLI failed").strip()[:500]
    # CLI success is local API acceptance, NOT verified gateway FIB installation.
    return True, (proc.stdout or "command accepted locally").strip()[:500]


def announce(prefix, isp, state, apply, limit, reason):
    if eligible_prefix(prefix.split("/")[0]) != prefix or isp not in ISPS:
        return False, "unsafe prefix or ISP"
    routes = state["routes"]
    if prefix not in routes and len(routes) >= limit:
        return False, f"route limit {limit} reached"
    old = routes.get(prefix)
    if old and old["isp"] == isp:
        return True, "already installed"
    ok, msg = bgp("announce", prefix, isp, apply)
    if ok and apply:
        routes[prefix] = {"isp": isp, "next_hop": ISPS[isp]["next_hop"],
                          "changed_at": ts(), "reason": reason}
        state["bgp_backend"] = BGP_BACKEND
        save_state(state, ACTIVE_STATE_PATH)
        try:
            record_route_event("move" if old else "announce", prefix,
                               old.get("isp") if old else None, isp, reason)
        except Exception as exc:
            print(f"WARNING route history write failed: {exc}", file=sys.stderr)
    return ok, msg


def withdraw(prefix, state, apply, reason="unspecified withdrawal"):
    old = state["routes"].get(prefix)
    if not old:
        return True, "not managed"
    ok, msg = bgp("withdraw", prefix, old["isp"], apply)
    if ok and apply:
        state["routes"].pop(prefix, None)
        save_state(state, ACTIVE_STATE_PATH)
        try:
            record_route_event("withdraw", prefix, old.get("isp"), None, reason)
        except Exception as exc:
            print(f"WARNING route history write failed: {exc}", file=sys.stderr)
    return ok, msg


def reconcile_routes(state, apply):
    if not apply:
        return
    validate_backend_state(state, apply)
    for prefix, route in sorted(state["routes"].items()):
        if eligible_prefix(prefix.split("/")[0]) != prefix or route.get("isp") not in ISPS:
            raise RuntimeError(f"Invalid persisted route {prefix}; aborting")
        ok, msg = bgp("announce", prefix, route["isp"], True)
        if not ok:
            raise RuntimeError(f"Cannot reconcile {prefix}: {msg}")
        print(f"RESTORED {prefix} -> {route['isp']}", flush=True)


def hold_left(prefix, state):
    route = state["routes"].get(prefix)
    if not route:
        return 0
    return max(0, HOLD_DOWN - (ts() - float(route.get("changed_at", 0))))


def update_streak(ds, desired, qualifies):
    now = ts()
    if not qualifies or desired is None:
        ds["winner"], ds["streak"], ds["last_win_at"] = None, 0, None
        return
    last = ds.get("last_win_at")
    if ds.get("winner") != desired or not last or now - last > MAX_WIN_GAP:
        ds["winner"], ds["streak"], ds["last_win_at"] = desired, 1, now
    elif now - last >= MIN_WIN_INTERVAL:
        ds["streak"] += 1
        ds["last_win_at"] = now


def reset_pending(ds):
    ds["pending"] = None
    update_streak(ds, None, False)


def loss_failover_allowed(results):
    """Require failure confirmation at >=2 distinct member IPs, not just a /24 average."""
    pairs = results.get("_pairs", [])
    if len(pairs) < EMERGENCY_MIN_HOSTS:
        return None
    required = max(EMERGENCY_MIN_HOSTS, math.ceil(len(pairs) * 2 / 3))
    for candidate in ISPS:
        candidate_index = 0 if candidate == "ISPA" else 1
        bad_index = 1 - candidate_index
        failures = 0
        for pair in pairs:
            healthy, bad = pair[candidate_index], pair[bad_index]
            if not math.isfinite(healthy["score"]) or healthy["loss"] > ALTERNATE_MAX_LOSS:
                continue
            if not math.isfinite(bad["score"]) or bad["loss"] >= EMERGENCY_LOSS:
                failures += 1
        if failures >= required:
            return candidate
    return None


def choose_action(row, measurements, state, apply, limit):
    prefix = row["prefix"]
    ds = destination_state(state, prefix)
    ds["updated"] = ts()
    last_seen = ds.get("last_seen")
    age = max(0, ts() - last_seen) if last_seen else math.inf
    route = state["routes"].get(prefix)
    # Fail closed for mixed WAN or stale outbound next-hop observations.
    current = route["isp"] if route else row["current"]
    current_at = ds.get("last_current_at")
    if not route and (not current_at or ts() - float(current_at) > 600):
        current = "UNKNOWN"

    # Route age-out must take precedence over a normal new announcement.
    if age >= EXPIRE_AFTER:
        reset_pending(ds)
        if route and hold_left(prefix, state) == 0:
            ok, msg = withdraw(prefix, state, apply, reason="inactive destination")
            return ("WITHDRAW" if apply else "WOULD WITHDRAW") + (f" inactive {age_label(age)}" if ok else f" FAILED {msg}")
        if route:
            return f"EXPIRING; hold {age_label(hold_left(prefix, state))}"
        return "INACTIVE / NO ROUTE"

    if not measurements:
        reset_pending(ds)
        ds["emergency_winner"], ds["emergency_streak"] = None, 0
        return "NO COMPARABLE PROBES"
    if current not in ISPS:
        reset_pending(ds)
        return f"OBSERVE ONLY ({current or 'unknown'} WAN)"
    fr, sp = measurements["ISPA"], measurements["ISPB"]
    wins = []
    for isp in ISPS:
        if math.isfinite(measurements[isp]["score"]):
            wins.append(isp)
    if not wins:
        reset_pending(ds)
        return "UNPROBEABLE"
    best = min(wins, key=lambda isp: measurements[isp]["score"])

    # Two independent rounds to avoid moving entire /24 on transient loss.
    emergency = loss_failover_allowed(measurements)
    if emergency != current:
        if ds["emergency_winner"] == emergency and emergency:
            ds["emergency_streak"] += 1
        else:
            ds["emergency_winner"] = emergency
            ds["emergency_streak"] = 1 if emergency else 0
    else:
        ds["emergency_winner"], ds["emergency_streak"] = None, 0
    if emergency and emergency != current and ds["emergency_streak"] >= EMERGENCY_CONFIRMATIONS:
        ok, msg = announce(prefix, emergency, state, apply, limit, "emergency loss/failure")
        if ok:
            reset_pending(ds)
            return f"{'ROUTE' if apply else 'WOULD ROUTE'} -> {emergency} (EMERGENCY {ds['emergency_streak']} rounds)"
        return f"EMERGENCY FAILED: {msg}"

    if not math.isfinite(measurements[current]["score"]):
        reset_pending(ds)
        return "CURRENT PATH FAILED; waiting emergency confirmation"

    other = "ISPB" if current == "ISPA" else "ISPA"
    old_score, new_score = measurements[current]["score"], measurements[other]["score"]
    diff = old_score - new_score
    pct = diff / old_score * 100 if old_score > 0 else 0.0
    single_host = measurements["_host_count"] == 1
    min_score = SINGLE_HOST_MIN_SCORE if single_host else MIN_SCORE_IMPROVEMENT
    min_percent = SINGLE_HOST_MIN_PERCENT if single_host else MIN_PERCENT_IMPROVEMENT
    required_quiet = SINGLE_HOST_QUIET_SECONDS if single_host else QUIET_SECONDS
    qualifies = math.isfinite(new_score) and diff >= min_score and pct >= min_percent
    update_streak(ds, other, qualifies)
    if not qualifies:
        ds["pending"] = None
        return f"KEEP {current}"
    if ds["streak"] < WIN_STREAK_REQUIRED:
        return f"CANDIDATE {other} {ds['streak']}/{WIN_STREAK_REQUIRED} ({pct:.1f}% better)"
    ds["pending"] = other
    if hold_left(prefix, state) > 0:
        return f"PENDING {other}; hold {age_label(hold_left(prefix, state))}"
    if age < required_quiet:
        return f"PENDING {other}; quiet {age_label(age)}/{age_label(required_quiet)}"
    ok, msg = announce(prefix, other, state, apply, limit, f"{pct:.1f}% better, quiet {int(age)}s")
    if not ok:
        return f"ROUTE FAILED: {msg}"
    reset_pending(ds)
    return f"{'ROUTE' if apply else 'WOULD ROUTE'} -> {other} ({pct:.1f}% better; quiet {age_label(age)})"


def expiry_sweep(state, records, handled, apply):
    """Expire installed routes even if no representative host probe was possible."""
    messages = []
    for prefix in list(state["routes"]):
        if prefix in handled:
            continue
        ds = destination_state(state, prefix)
        last = ds.get("last_seen")
        if not last or ts() - last < EXPIRE_AFTER or hold_left(prefix, state) > 0:
            continue
        ok, msg = withdraw(prefix, state, apply, reason="inactive destination sweep")
        messages.append(f"{'WITHDRAW' if apply else 'WOULD WITHDRAW'} {prefix} (inactive)" if ok else f"WITHDRAW FAILED {prefix}: {msg}")
    return messages


def fmt_probe(measurement):
    if not measurement:
        return "-"
    if not math.isfinite(measurement["score"]):
        return "FAIL"
    return f"{measurement['latency']:.1f}ms/{measurement['loss']:.0f}%(s{measurement['score']:.1f})"


def cycle(state, args):
    started = time.monotonic()
    latest_flow_at = None
    flow_count = 0
    def monitored_flows():
        nonlocal latest_flow_at, flow_count
        for flow, last in read_flows():
            flow_count += 1
            latest_flow_at = max(latest_flow_at or 0, last)
            yield flow, last
    records = collect_flows(state, monitored_flows())
    rows = candidates(state, records, args.top)
    probes = probe_all(rows) if rows else {}
    print(f"\nRoute Optimizer v3.4 [{BGP_BACKEND} {'APPLY' if args.apply else 'DRY-RUN'}] "
          f"{datetime.now().isoformat(timespec='seconds')}", flush=True)
    print(f"Prefix /{PREFIX_LENGTH} | top {args.top} | win {WIN_STREAK_REQUIRED} | "
          f"quiet {QUIET_SECONDS}s | hold {HOLD_DOWN}s | "
          f"routes {len(state['routes'])}/{args.max_routes}")
    print(f"{'Prefix':<19} {'MiB':>8} {'Hosts':>5} {'Observed':<9} {'Route':<9} "
          f"{ISP_LABELS['ISPA']:>20} {ISP_LABELS['ISPB']:>20}  Action")
    print("-" * 149)
    handled = set()
    dashboard_rows = []
    for row in rows:
        prefix = row["prefix"]
        handled.add(prefix)
        measurements, reason = aggregate_measurements(row, probes.get(prefix, {}))
        if reason:
            ds = destination_state(state, prefix)
            reset_pending(ds)
            ds["emergency_winner"], ds["emergency_streak"] = None, 0
        action = choose_action(row, measurements, state, args.apply, args.max_routes) if not reason else reason
        if reason and prefix in state["routes"]:
            # Expiry is still mandatory even when probes are unusable.
            age = ts() - (destination_state(state, prefix).get("last_seen") or ts())
            if age >= EXPIRE_AFTER and hold_left(prefix, state) <= 0:
                ok, msg = withdraw(prefix, state, args.apply, reason="inactive; probes unusable")
                action = f"{'WITHDRAW' if args.apply else 'WOULD WITHDRAW'} inactive" if ok else f"WITHDRAW FAILED: {msg}"
        installed = state["routes"].get(prefix, {}).get("isp", "-")
        print(f"{prefix:<19} {row['bytes']/1048576:8.2f} {len(row['hosts']):5d} "
              f"{display_isp(row['current'] or '-'):9.9} {display_isp(installed):9.9} "
              f"{fmt_probe(measurements.get('ISPA') if measurements else None):>20} "
              f"{fmt_probe(measurements.get('ISPB') if measurements else None):>20}  {display_isp(action)}", flush=True)
        dashboard_rows.append({
            "prefix": prefix, "mib": round(row["bytes"] / 1048576, 3),
            "hosts": len(row["hosts"]), "representatives": [h["ip"] for h in row["hosts"]],
            "observed": row["current"] or "UNKNOWN", "route": installed,
            "ispa": status_measurement(measurements.get("ISPA") if measurements else None),
            "ispb": status_measurement(measurements.get("ISPB") if measurements else None),
            "action": action, "last_seen": row.get("last_seen"),
        })
    sweep_messages = expiry_sweep(state, records, handled, args.apply)
    for msg in sweep_messages:
        print(display_isp(msg), flush=True)
    for prefix, ds in list(state["destinations"].items()):
        if prefix not in state["routes"] and not ds.get("pending") and ts() - float(ds.get("updated", 0)) > STATE_GC:
            del state["destinations"][prefix]
    save_state(state, args.state)
    cycle_epoch = ts()
    flow_age = round(max(0, cycle_epoch - latest_flow_at), 1) if latest_flow_at else None
    ipfix_status = ("no_data" if flow_age is None else
                    "fresh" if flow_age <= 180 else "stale")
    failing_prefixes = sum(1 for r in dashboard_rows if (
        r["ispa"] is None or r["ispb"] is None or
        r["ispa"].get("score") is None or r["ispb"].get("score") is None))
    bgp_status = bgp_health()
    try:
        record_quality_sample(dashboard_rows, cycle_epoch)
    except Exception as exc:
        print(f"WARNING quality history write failed: {exc}", file=sys.stderr)
    snapshot = {
        "version": "3.4", "epoch": cycle_epoch,
        "isp_labels": ISP_LABELS.copy(),
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": "APPLY" if args.apply else "DRY-RUN", "error": None,
        "cycle_seconds": round(time.monotonic() - started, 2),
        "config": {"prefix_length": PREFIX_LENGTH, "top": args.top,
                   "max_routes": args.max_routes, "bgp_backend": BGP_BACKEND,
                   "traffic_window": TRAFFIC_WINDOW,
                   "min_bytes": MIN_BYTES, "quiet_seconds": QUIET_SECONDS,
                   "single_host_quiet_seconds": SINGLE_HOST_QUIET_SECONDS},
        "route_count": len(state["routes"]),
        "routes": [{"prefix": p, "isp": r.get("isp", "UNKNOWN"),
                    "changed_at": r.get("changed_at"), "reason": r.get("reason")}
                   for p, r in sorted(state["routes"].items())],
        "rows": dashboard_rows,
        "health": {
            "ipfix": {"status": ipfix_status, "age_seconds": flow_age,
                      "flow_count": flow_count, "exporter": UCG_EXPORTER},
            "bgp": bgp_status,
            "probes": {"tested_prefixes": len(dashboard_rows),
                       "failing_prefixes": failing_prefixes,
                       "paired_prefixes": len(dashboard_rows) - failing_prefixes},
        },
        "sweep_messages": sweep_messages,
    }
    write_status(snapshot)
    print(flush=True)


def main():
    global ACTIVE_STATE_PATH, BGP_BACKEND, RBGPD_CLI, RBGPD_SOCKET
    parser = argparse.ArgumentParser(description="Dual-WAN /24 route optimizer (dry-run by default)")
    parser.add_argument("--config", type=Path, default=Path("/etc/route-optimizer/config.json"),
                        help="site-specific JSON config (required)")
    parser.add_argument("--apply", action="store_true", help="enable actual BGP route changes")
    parser.add_argument("--bgp-backend", choices=("exabgp", "rustbgpd"), default="exabgp",
                        help="BGP daemon to use (default: exabgp for backwards compatibility)")
    parser.add_argument("--rbgp-cli", default=RBGPD_CLI, help="path to rustbgpd rbgp CLI")
    parser.add_argument("--rbgp-socket", default=RBGPD_SOCKET, help="local rustbgpd gRPC UDS URI")
    parser.add_argument("--watch", metavar="SECONDS", type=int, default=0, help="repeat every N seconds")
    parser.add_argument("--top", type=int, default=DEFAULT_TOP)
    parser.add_argument("--max-routes", type=int, default=DEFAULT_ROUTE_LIMIT)
    parser.add_argument("--withdraw-all", action="store_true", help="withdraw only v3-managed /24 routes")
    parser.add_argument("--state", type=Path, default=STATE_FILE)
    args = parser.parse_args()
    try:
        configure_site(json.loads(args.config.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(f"invalid --config {args.config}: {exc} (copy examples/config.example.json)")
    ACTIVE_STATE_PATH = args.state
    BGP_BACKEND = args.bgp_backend
    RBGPD_CLI = args.rbgp_cli
    RBGPD_SOCKET = args.rbgp_socket
    if BGP_BACKEND == "rustbgpd" and not RBGPD_SOCKET.startswith("unix:///"):
        parser.error("rustbgpd requires a local unix:/// gRPC socket (no network listener)")
    if not (1 <= args.max_routes <= HARD_MAX_ROUTES):
        parser.error(f"--max-routes must be between 1 and {HARD_MAX_ROUTES}")
    if args.top < 1 or args.watch < 0:
        parser.error("--top must be positive and --watch nonnegative")
    # Keep two v3 processes from managing the same routes/state concurrently.
    with open(LOCK_FILE, "w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("another route-optimizer-v3 process is already running")
        try:
            state = load_state(args.state)
            validate_backend_state(state, args.apply)
            if len(state["routes"]) > args.max_routes and not args.withdraw_all:
                raise RuntimeError("state already exceeds --max-routes; raise limit or withdraw")
            if args.withdraw_all:
                failures = 0
                for prefix in list(state["routes"]):
                    ok, msg = withdraw(prefix, state, args.apply, reason="operator withdraw-all")
                    print(f"{'WITHDRAW' if args.apply else 'WOULD WITHDRAW'} {prefix}: {msg}")
                    if not ok:
                        failures += 1
                save_state(state, args.state)
                return 1 if failures else 0
            reconcile_routes(state, args.apply)
            last_reconcile = time.monotonic()
            while True:
                start = time.monotonic()
                # rustbgpd local injected routes can disappear after its restart.
                # Re-announce the small, tracked route set periodically in live
                # mode, without altering hold-downs or generating move events.
                if (BGP_BACKEND == "rustbgpd" and args.apply and state["routes"]
                        and start - last_reconcile >= RBGPD_RECONCILE_SECONDS):
                    reconcile_routes(state, True)
                    last_reconcile = time.monotonic()
                cycle(state, args)
                if not args.watch:
                    return 0
                time.sleep(max(1, args.watch - (time.monotonic() - start)))
        except KeyboardInterrupt:
            print("\nStopped.")
            return 130
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            try:
                write_status({
                    "version": "3.4", "epoch": ts(),
                    "isp_labels": ISP_LABELS.copy(),
                    "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "mode": "APPLY" if args.apply else "DRY-RUN", "error": str(exc),
                    "cycle_seconds": None,
                    "config": {"max_routes": args.max_routes, "top": args.top,
                               "prefix_length": PREFIX_LENGTH},
                    "route_count": len(state.get("routes", {})),
                    "routes": [{"prefix": p, "isp": r.get("isp", "UNKNOWN")}
                               for p, r in sorted(state.get("routes", {}).items())],
                    "rows": [], "sweep_messages": [],
                    "health": {"ipfix": {"status": "unknown", "age_seconds": None},
                               "bgp": {"status": "unknown"},
                               "probes": {"tested_prefixes": 0, "failing_prefixes": 0}},
                })
            except Exception as write_error:
                print(f"ERROR reporting status: {write_error}", file=sys.stderr)
            return 1


if __name__ == "__main__":
    sys.exit(main())