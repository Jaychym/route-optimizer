# Route Optimizer

A **self-hosted, experimental dual-WAN outbound route optimizer** for a UniFi gateway using **IPFIX, active path measurements and BGP**. Includes a read-only, dark-themed web dashboard with historical ISP quality, optimizer health and route-change history.

> **Caution:** Research / homelab software, **not an officially supported UniFi feature or a drop-in replacement for Noction/SD-WAN**. A BGP next-hop change can reset existing sessions because NAT exits through a different public IP. **Dry-run is the default**. Review routing safeguards and a rollback plan before `--apply`.

## What it does

- Receives exported flow records with `nfcapd` / `nfdump` (IPFIX v10), identifies visited public IPv4 `/24` networks and measures up to three representative hosts per prefix via two separately policy-routed probe VLANs.
- Scores response time, jitter and probe loss, with consecutive wins, a quiet-flow interval, route hold-down and inactivity expiration to avoid unnecessary changes.
- Uses **rustbgpd** via the local `rbgp` CLI to inject eligible public `/24` routes into the gateway with a WAN gateway as the third-party next-hop. The optimizer VM is **not** inline and does not forward user traffic.
- Exposes a **read-only** web UI with IPFIX and BGP health, active optimizer-managed routes, destination decisions and quality graphs (HTTP on port 8099).
- Keeps records across restarts in local JSON state; maintains bounded quality and route-event history.

Current implementation uses two stable internal ISP identifiers, `ISPA` and `ISPB`. To display your actual provider names in the dashboard and console, set the optional `isp_labels` mapping in your **private** `/etc/route-optimizer/config.json`, for example `"isp_labels": {"ISPA": "Primary WAN", "ISPB": "Backup WAN"}`. Keep the `isps` keys unchanged; routing decisions, state and telemetry still use the generic identifiers. Never commit your real site config. Internet traffic must be NATed by the gateway on each selected WAN.

## Architecture

```mermaid
flowchart TB
  LAN[LAN and IoT clients] --> UCG[UniFi gateway / NAT]
  UCG --> WA[WAN A]
  UCG --> WB[WAN B]
  UCG -- IPFIX / UDP 2055 --> VM[Debian optimizer VM]
  VM -- BGP / TCP 179 --> UCG
  VM -- Source A probes --> VA[Probe VLAN A]
  VM -- Source B probes --> VB[Probe VLAN B]
  VA --> UCG
  VB --> UCG
  VM --> WEB[Read-only dashboard / port 8099]
```

## Prerequisites

- A UniFi gateway with IPFIX export and an accessible FRR/BGP implementation supporting **third-party next-hop** routes as demonstrated in our test environment. **Do not assume other gateway models/firmware work**; prove route acceptance and forwarding on your model before enabling automatic route changes.
- Debian or equivalent Linux VM with management IP, two dedicated probe interfaces/VLANs and policy routes steering each probe source out a different ISP. Apply a **WAN policy route with a kill switch** per probe VLAN to avoid accidentally sampling the wrong provider during failures.
- `python3` 3.11+, `nfdump` (including `nfcapd`), `iputils-ping`, `tcpdump` for troubleshooting, and a pinned **rustbgpd** release with `/usr/local/bin/rbgp`. Only standard-library Python packages are required by the optimizer and dashboard.
- An **Established rustbgpd iBGP session** with UniFi, an inbound deny-first BGP route-map/prefix-list rejecting unsafe routes, and a strongly recommended neighbor maximum-prefix above the optimizer cap (e.g. 75 for a 50-route limit).
- Independent SSH/console recovery before changing routes. **UniFi must route the two probe-source VLANs using rules before its main-table lookup**, because installed BGP `/24`s can otherwise hijack the other WAN's probes. Confirm physical egress/NAT via packet captures *while a test BGP route exists*. UniFi reloads or firmware updates can remove custom rules; verify repair and reboot persistence on your gateway.

**Performance warning:** Enabling NetFlow/IPFIX export can disable hardware acceleration on some UniFi gateways and noticeably reduce throughput. Benchmark wired throughput and CPU usage before relying on continuous export. Never point the collector to the Internet.

## Installation (new deployment)

1. Set up a **static management address** on the Debian VM, two probe VLAN interfaces, their source-routing rules and UniFi VLAN→WAN policy routes. Start from [`examples/network-interfaces.example`](examples/network-interfaces.example), adapting the interface names and topology. Check `ip rule` and both policy tables after reboot.
2. Configure UniFi IPFIX v10 to export your production LAN/VLAN flows to the VM UDP 2055. Exclude the probe VLANs if possible to avoid feedback. Confirm `tcpdump -ni <management-interface> udp port 2055` receives packets.
3. Install a **pinned rustbgpd** release following the [upstream deployment guide](https://github.com/lance0/rustbgpd/blob/main/docs/how-to/deployment.md), customize [`examples/rustbgpd.toml.example`](examples/rustbgpd.toml.example) and validate with `sudo rustbgpd --check --strict /etc/rustbgpd/config.toml`. Install its upstream systemd service, start it, then verify `sudo rbgp summary` reports the UniFi neighbor **Established**. Do not run another BGP speaker against the same peer.
4. Customize [`examples/unifi-bgp.conf.example`](examples/unifi-bgp.conf.example) and upload/import it using your UniFi gateway's supported BGP/FRR configuration-file workflow. **Do not upload unchanged**: replace the sample ASN, router ID and optimizer peer IP, and add explicit deny entries for **each real WAN-connected subnet** before the public-prefix permit. The example provides an inbound route-map accepting only eligible public `/24` destinations plus a `maximum-prefix 75` backstop (for a 50-route optimizer ceiling). If the UniFi importer replaces the full routing configuration, merge these BGP statements into your existing FRR config first so OSPF or unrelated BGP settings are preserved. Validate import behavior on your gateway model/firmware; the example alone does not establish compatibility.
5. Copy and customize the **untracked** site configuration; **do not publish actual gateway/public WAN addresses**:

```bash
sudo mkdir -p /etc/route-optimizer /opt/route-optimizer /var/lib/route-optimizer
sudo install -m 0600 examples/config.example.json /etc/route-optimizer/config.json
sudoedit /etc/route-optimizer/config.json
sudo install -m 0755 src/route-optimizer.py /opt/route-optimizer/route-optimizer.py
sudo install -m 0755 src/route-optimizer-web.py /opt/route-optimizer/route-optimizer-web.py
sudo install -m 0644 systemd/route-optimizer.service /etc/systemd/system/route-optimizer.service
sudo install -m 0644 systemd/route-optimizer-web.service /etc/systemd/system/route-optimizer-web.service
sudo systemctl daemon-reload
sudo systemctl enable --now route-optimizer route-optimizer-web
```

The example JSON uses **RFC 5737 documentation addresses** (`198.51.100.0/24`, `203.0.113.0/24`) as pretend WANs. **Replace every address and subnet** with your own site values. `wan_connected_networks` must include each ISP next-hop and is also excluded from possible route announcements. Source IPs must match your actual probe interfaces. `own_ips` must include the optimizer management and both probe IPs. The internal networks list should cover all production VLANs included in IPFIX.

To view logs:

```bash
sudo journalctl -u route-optimizer -f
sudo systemctl status route-optimizer route-optimizer-web --no-pager
```

The dashboard listens on `127.0.0.1:8099` by default. Tunnel with `ssh -L 8099:127.0.0.1:8099 user@optimizer-host`, or bind it to a **trusted management address** behind an access-controlled reverse proxy (edit the systemd web unit via a drop-in). It has **no built-in authentication**; don't expose it publicly.

## Dry-run, live mode and rollback

The default `systemd/route-optimizer.service` uses **rustbgpd exclusively**, **does not contain `--apply`**, and never announces routes until you explicitly enable `--apply`. Run the engine manually in dry-run mode for several hours before live routing:

```bash
sudo /opt/route-optimizer/route-optimizer.py --config /etc/route-optimizer/config.json --watch 60 --top 50
```

**Before live:** Verify the rustbgpd BGP session, UniFi inbound prefix filter (plus preferably maximum-prefix), protected site subnets, no stale routes, and correct independent source-policy and NAT egress for **both** WAN probes while a test BGP route is installed. Gateway policy rules must take precedence over the main lookup; verify persistence/recovery after UniFi reboot before relying on unattended operation. Be aware that switching a `/24` can terminate existing TCP/UDP/QUIC connections. Quiet-flow detection uses delayed flow exports, not authoritative conntrack state.

To enable, create a systemd override for `route-optimizer.service` to add `--apply` to `ExecStart`, then `systemctl daemon-reload && systemctl restart route-optimizer`. Do **not** enable route changes via the web service. Watch `show ip route bgp` on the gateway and audit the dashboard's route history.

**Emergency rollback** (stops new changes, withdraws only prefixes tracked in optimizer state):

```bash
sudo systemctl stop route-optimizer
sudo /opt/route-optimizer/route-optimizer.py --config /etc/route-optimizer/config.json --withdraw-all --apply
# Then verify on UCG: vtysh -c 'show ip route bgp'
```

Routes manually announced outside the optimizer state file are **not** removed by `--withdraw-all`. Keep rustbgpd active for withdrawals, verify `sudo rbgp rib sent <PEER_IP>` and `vtysh -c 'show ip route bgp'` on UniFi afterward, and retain state until withdrawal succeeds. Untagged legacy state with installed routes must **not** be reused for rustbgpd live mode.

## Limitations and safety

- **Outbound IPv4 only**; does not influence inbound routing or IPv6. IPv4 `/24` is a deliberate choice and may affect multiple services at once.
- Path quality is measured against currently visited hosts. ICMP/TCP probing cannot perfectly model application performance, long-lived sessions, UDP or QUIC.
- Only compares WANs that the **UniFi policy routes** actually send probes over. If the gateway checks the main table before probe policy tables, an optimized BGP `/24` can force both probe VLANs onto one WAN and invalidate measurements. Higher-priority source/iif rules and independent physical egress verification are essential. Recheck custom rule persistence following UniFi reboot or reprovisioning.
- The app is not an authoritative inventory of the UCG FIB; "installed" indicates locally tracked/acknowledged optimizer BGP announcements. Compare to `vtysh` for actual installation.
- Local JSON state, IPFIX records and traffic history may contain private network usage metadata. Keep them out of Git; `config.json`, `nfcapd.*`, JSON state files and logs are ignored.
- If the WAN gateway changes dynamically, update site config, safe prefix filters and next-hop handling before attempting live optimization.
- BGP maximum-prefix is a **backstop** and may reset the neighbor if exceeded; do not treat it as a normal route-management mechanism.

## Tests

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile src/route-optimizer.py src/route-optimizer-web.py
```

GitHub Actions runs standard-library tests on push and pull request. A UCG-Ultra field test with rustbgpd v0.75.0 confirmed BGP peer establishment, third-party next-hop installation in UniFi's FIB, subsequent withdrawal, and independent WAN probe forwarding/NAT using packet captures. **Custom gateway policy-rule persistence across reboot has not yet been tested.** Broader firmware and gateway testing remains necessary.

## License

MIT. See [LICENSE](LICENSE). Contributions, issues and constructive testing reports are welcome.

## Migrating an older deployment

See [`docs/rustbgpd-migration.md`](docs/rustbgpd-migration.md) for withdrawing routes from a legacy BGP speaker and moving safely to rustbgpd. The engine now supports **rustbgpd only**; `--bgp-backend rustbgpd` is still accepted for compatibility with existing v3.4 systemd units. The GitHub-ready code deliberately **removed hardcoded site IPs**. It is therefore **not a drop-in replacement for a running v3.3 service** until you have created `/etc/route-optimizer/config.json` with your actual local WAN gateway IPs, probe IPs, export address and protected subnets. Do not copy your private config, IPFIX captures, current state or log files into the public repository. Back up the working script and state before any upgrade. Once the local config exists, validate dry-run first; keep your original systemd and reverse-proxy overrides until the new code behaves correctly.