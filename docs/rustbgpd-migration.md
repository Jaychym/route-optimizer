# rustbgpd deployment and legacy migration (v3.4)

**rustbgpd is the only supported BGP daemon.** The optimizer uses the
`rbgp` CLI over rustbgpd's local Unix gRPC socket. Pin a tested release;
the daemon is experimental outside its [stable v1
contract](https://github.com/lance0/rustbgpd/blob/main/docs/reference/v1-stable-contract.md).

## New deployment

1. Back up UniFi's complete FRR/BGP configuration, optimizer scripts,
   systemd units and route state; preserve independent management access.
2. Install pinned rustbgpd using [the upstream deployment
   guide](https://github.com/lance0/rustbgpd/blob/main/docs/how-to/deployment.md).
   Customize `examples/rustbgpd.toml.example` to use your private AS,
   management IP and UniFi peer. Validate with
   `sudo rustbgpd --check --strict /etc/rustbgpd/config.toml`. Install
   the upstream rustbgpd systemd unit and use its local Unix socket.
3. Ensure the UniFi inbound BGP prefix list/route map blocks internal,
   special-use and WAN-connected destinations. Use a maximum-prefix limit
   greater than `--max-routes` where supported. Do not replace other
   BGP/OSPF sections when importing a routing configuration.
4. Start rustbgpd, confirm `sudo rbgp summary` is Established, and
   ensure a single speaker serves the peer. The default
   `systemd/route-optimizer.service` is a **dry-run** unit.
5. Configure the local **untracked**
   `/etc/route-optimizer/config.json`. The required `isps` keys stay
   `ISPA`/`ISPB`; use optional `isp_labels` for friendly names.
   Confirm flow freshness, both probes, BGP health, and dashboard metrics.
6. With a rollback procedure ready, test one eligible public `/24`:
   announce using `rbgp rib add PREFIX --next-hop REAL_WAN_GATEWAY`,
   confirm UniFi BGP RIB and Linux FIB use the real WAN gateway, capture
   physical egress and NAT, then withdraw using `rbgp rib delete PREFIX`.
   Local CLI acceptance alone does not prove a UniFi route is installed.
7. Only enable `--apply` after those checks; start with a low route cap,
   watch route-event history and verify actual UniFi-installed routes.

## Critical probe-routing isolation on UniFi

The optimizer VM must route each probe source to the correct probe VLAN,
but **VM-side source rules alone are insufficient**.

When UniFi installs an optimizer BGP `/24` in its main table, that
specific route can take precedence over UniFi's normal WAN policy routing
and send *both* probes through the same WAN. This falsifies path-quality
comparisons. On UniFi, add narrowly scoped rules matching the probe
source **and ingress interface**, evaluated before `from all lookup main`:

```text
from <PROBE_A_SOURCE_IP> iif <PROBE_A_VLAN> lookup <WAN_A_ROUTE_TABLE>
from <PROBE_B_SOURCE_IP> iif <PROBE_B_VLAN> lookup <WAN_B_ROUTE_TABLE>
```

Choose actual table names and free rule priorities from
`ip -4 rule show` and `ip -4 route show table all`. Check both with
`ip -4 route get <PUBLIC_IP> from <PROBE_IP> iif <VLAN_INTERFACE>`
**while a test BGP route is installed**. Prove independent physical
egress and return NAT with simultaneous captures on both WAN interfaces.
Do not assume a normal UI traffic-route rule precedes the BGP route.

Custom rules are not guaranteed to survive UniFi reprovisioning, firmware
updates or reboots. Provide a local boot-time restore and watchdog,
test restoration after deletion, and **separately** verify persistence
after reboot before using unattended live routing. If WAN-specific
tables lose their default routes, probe policy should fail closed
rather than fall through to the main table.

### Field-validation status

On a UCG-Ultra test environment using UniFi OS 5.1.33 and rustbgpd
v0.75.0, the following were observed:

- rustbgpd peer Established; one eligible public `/24` announced,
  selected and installed in UniFi's FIB using its actual WAN gateway.
- The test route was withdrawn successfully.
- Source/iif routing lookups selected different WAN tables.
- Separate WAN packet captures confirmed three transmitted ICMP probes
  and three replies through **each** physical WAN with the proper NAT
  source addresses even with an installed BGP route.
- A periodic watchdog restored a deliberately deleted probe policy rule.

**Not yet validated:** persistence of the custom UniFi boot service/rules
after a full gateway reboot. These field tests do not guarantee
compatibility with other models/firmware.

## Migrating from a legacy ExaBGP installation

ExaBGP is no longer an engine dependency or available routing backend.
The legacy tool is needed **only if you must withdraw older routes before
the cutover**:

1. Stop the *old* optimizer, withdraw legacy-advertised routes using its
   original tooling/state, and inspect the UCG's RIB/FIB. Old `/32`
   routes may also need clearing. **Do not simply delete route state.**
2. Stop/disable the legacy BGP daemon. Start rustbgpd using the new
   configuration and verify the session Established.
3. Keep a backup of old state and use a new state file for the rustbgpd
   deployment where appropriate. New live mode refuses to replay an
   untagged/legacy state with installed routes.
4. Run the new optimizer in dry-run, then verify manual route
   injection/withdrawal and independent WAN egress as above.
5. Enable live routing only after verifying probe rules remain present
   and your chosen recovery mechanism works.

The legacy `--bgp-backend rustbgpd` option remains **accepted** so
existing rustbgpd v3.4 services can be upgraded without rewriting
their command lines. Other `--bgp-backend` values are rejected.

## Commands

```bash
sudo rbgp summary
sudo rbgp rib sent <UCG_PEER_IP>

# One dry-run cycle with no BGP changes:
sudo /opt/route-optimizer/route-optimizer.py \
  --config /etc/route-optimizer/config.json --top 10 --max-routes 10

# Controlled rollback for a default-path new installation:
sudo systemctl stop route-optimizer
sudo /opt/route-optimizer/route-optimizer.py \
  --config /etc/route-optimizer/config.json \
  --state /var/lib/route-optimizer/state-v3.json \
  --withdraw-all --apply

# Then verify peer advertisements and gateway FIB before restoring dry-run.
```

**For custom deployments:** replace the service and state paths above with
the paths actually used. Never start a second optimizer with a shared
state file while the live one is running. Keep rustbgpd Established
during withdrawal; a failed CLI withdrawal requires manual investigation.

## Known limitations

Changes to a destination `/24` can reset NAT-backed TCP/UDP/QUIC
connections. Route state tracks locally accepted BGP commands, not an
authoritative UniFi FIB inventory. Managed routes are reasserted on
startup and every five minutes while live. This is experimental
self-hosted software, not a supported SD-WAN product.
