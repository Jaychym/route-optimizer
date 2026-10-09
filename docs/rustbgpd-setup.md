# Install and configure rustbgpd for Route Optimizer

This is the tested **single-VM / single-UniFi-peer** deployment pattern.
rustbgpd is the only BGP backend supported by Route Optimizer v3.4.
The optimizer VM advertises eligible public /24 destination routes;
**UniFi**, not the VM, forwards and NATs traffic. The gateway must accept
the WAN gateway as a third-party BGP next hop.

rustbgpd is upstream **public alpha** software. Pin a release; check its
changelog before upgrading and validate configuration each time.
Useful references:

- [rustbgpd upstream project](https://github.com/lance0/rustbgpd)
- [Releases](https://github.com/lance0/rustbgpd/releases)
- [Upstream deployment guide](https://github.com/lance0/rustbgpd/blob/main/docs/how-to/deployment.md)
- [Upstream configuration reference](https://github.com/lance0/rustbgpd/blob/main/docs/reference/configuration.md)
- [Route Optimizer sample TOML](../examples/rustbgpd.toml.example)
- [UniFi sample BGP configuration](../examples/unifi-bgp.conf.example)
- [Legacy migration and independent probe routing](rustbgpd-migration.md)

## 1. Requirements and topology

Use Debian Linux with root access, x86_64 (amd64) or aarch64 (arm64),
and glibc >= 2.31. The examples below use **documentation IPs only**.

- UniFi management/BGP peer: `192.0.2.1`, ASN `65050`.
- Optimizer VM management/BGP address: `192.0.2.50`, ASN `65050`.
- Have a working management path and the UniFi inbound public-prefix
  route-map and ideally a maximum-prefix safeguard.
- Do not run another BGP daemon peering with the same UniFi neighbor.

## 2. Install pinned release (example: v0.75.0)

The commands below install verified release tarball binaries and the
upstream systemd unit. **Run only on a fresh install**, not blindly over
a running BGP daemon. Verify the latest supported upstream version
and adjust the explicit tag after testing.

```bash
set -eu
VERSION=v0.75.0
ARCH="$(dpkg --print-architecture)"
case "$ARCH" in
  amd64|arm64) ;;
  *) echo "Unsupported architecture: $ARCH" >&2; exit 1 ;;
esac

mkdir -p "$HOME/rustbgpd-install"
cd "$HOME/rustbgpd-install"
TARBALL=rustbgpd-linux-$ARCH.tar.gz
BASE=https://github.com/lance0/rustbgpd/releases/download/$VERSION
curl -fL "$BASE/$TARBALL" -o "$TARBALL"
curl -fL "$BASE/checksums-linux-$ARCH.txt" -o "checksums-linux-$ARCH.txt"
awk -v f="$TARBALL" '$2==f || $2=="./"f {print}' "checksums-linux-$ARCH.txt" | sha256sum -c -
tar -xzf "$TARBALL"
sudo install -m 0755 rustbgpd rbgp /usr/local/bin/
sudo useradd --system --user-group --home-dir /var/lib/rustbgpd \
  --shell /usr/sbin/nologin rustbgpd 2>/dev/null || true
sudo install -d -m 0755 /etc/rustbgpd
sudo install -m 0644 share/systemd/rustbgpd.service \
  /etc/systemd/system/rustbgpd.service
sudo systemctl daemon-reload

rustbgpd --version
rbgp --version
```

The checksum command must report `OK`. Inspect failed checksum output
and stop rather than continuing. These commands do not start BGP.

## 3. Configure the UniFi peer

From a checked-out Route Optimizer repository:

```bash
sudo install -m 0640 examples/rustbgpd.toml.example /etc/rustbgpd/config.toml
sudo chown root:rustbgpd /etc/rustbgpd/config.toml
sudoedit /etc/rustbgpd/config.toml
sudo rustbgpd --check --strict /etc/rustbgpd/config.toml
```

Set `asn`, `router_id`, `listen_addresses`, neighbor `address`
and `remote_asn` to your topology. Leave `config_epoch = 2`,
`ebgp_requires_policy = true`, `import_chain`, `export_chain`,
and `runtime_state_dir` consistent with the shipped sample unless you
understand the implications. For this control-plane-only VM, **do not
configure `[[fib_tables]]`**. Keep the gRPC API on its owner-only
local Unix socket, never open it to the LAN.

`config OK` means syntax and strict-validation pass, **not** that the
gateway is reachable.

## 4. Confirm UniFi-side BGP policy

Configure the UniFi gateway with its own local BGP ASN and neighbor
pointing to the VM management address. Start from the
[UniFi FRR example](../examples/unifi-bgp.conf.example), replacing
the example addresses, router ID, protected WAN-connected subnets
and prefix limit. Do **not** upload that file unmodified. An import
might replace other FRR/OSPF configuration; review the gateway's supported
import workflow and preserve unrelated routing sections.

The gateway must accept only eligible public destinations, deny
its own WAN-connected networks, and preserve the announced WAN
next hop when installing the route. Ensure BGP TCP/179 connectivity.
Do not proceed to optimizer live mode until the filters and next-hop
behavior are verified.

## 5. Start and verify rustbgpd

```bash
sudo systemctl enable --now rustbgpd.service
sudo systemctl status rustbgpd --no-pager
sudo rbgp health
sudo rbgp summary
sudo rbgp --json neighbor 192.0.2.1
sudo rbgp rib sent 192.0.2.1
```

Replace `192.0.2.1` with your real UniFi neighbor.
Expected results: `healthy`, neighbor `Established`, and **zero
advertised routes** before first optimizer activation. On UniFi:

```bash
vtysh -c 'show bgp ipv4 unicast summary'
vtysh -c 'show ip route bgp'
```

## 6. Verify route injection and independent probes

In a controlled window, choose a **real, low-impact, globally routed
public /24 that was observed by IPFIX**, and one correct next-hop from
the UniFi WAN table. Do not test using the documentation addresses above,
private subnets, a connected WAN subnet, or a critical destination.

With the optimizer **stopped / dry-run** and no other route owner:

```bash
# Replace both placeholders with real values.
sudo rbgp rib add <PUBLIC_PREFIX_24> --next-hop <REAL_WAN_GATEWAY>
sudo rbgp summary
sudo rbgp rib sent <UNIFI_PEER_IP>

# On UniFi, verify selected BGP route and actual kernel FIB:
vtysh -c 'show ip route bgp'
ip -4 route show table all

# Withdraw immediately after the short forwarding test:
sudo rbgp rib delete <PUBLIC_PREFIX_24>
sudo rbgp rib sent <UNIFI_PEER_IP>
```

Those commands are illustrative: substitute all bracketed values before
running. **Local acceptance does not guarantee the gateway FIB.**

Critically, install UniFi probe-source/ingress-interface policy rules
that run **ahead of** its main routing-table rule. Otherwise an
injected BGP /24 may make both supposedly independent probe sources
exit the same WAN. Check `ip -4 rule show`, use `ip -4 route get
<DEST> from <PROBE_IP> iif <PROBE_VLAN>`, and confirm physical WAN
egress plus proper NAT with packet captures while the test route is
installed. See the [probe isolation procedure](rustbgpd-migration.md#critical-probe-routing-isolation-on-unifi).

Custom UniFi rules may disappear on reprovisioning, firmware upgrade,
or reboot. **Test rule persistence after reboot separately**; a watchdog
that repairs one deleted rule is not proof of reboot persistence.

## 7. Start Route Optimizer safely

Use the [README installation guide](../README.md#installation-new-deployment)
and the sample `systemd/route-optimizer.service`.
The default service is **DRY-RUN** and does **not** include
`--apply`. Let it run, inspect IPFIX and both WAN measurements, and
verify the dashboard before enabling live routing.

For an existing install, preserve its active `--state` file and
rollback steps. Stopping the optimizer does not automatically withdraw
persisted advertisements. To withdraw tracked routes, stop the
optimizer then run the same optimizer binary, site config, and state
path with `--withdraw-all --apply`, while rustbgpd remains Established.
Finally verify `rbgp rib sent <PEER>` and UniFi's FIB.

## Limitations

This workflow was field-tested on a UCG-Ultra with rustbgpd v0.75.0.
It is not a guarantee for other UniFi models or firmware revisions.
The UniFi boot-service/rule persistence test was still outstanding in
the recorded field deployment; treat that as an explicit prerequisite
for unattended live service.
