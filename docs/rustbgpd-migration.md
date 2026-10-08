# Experimental rustbgpd backend (v3.4)

Route Optimizer can now call the `rbgp` CLI instead of `exabgp-cli`. **Existing
installations continue using ExaBGP by default.** This is an integration
candidate, not yet an end-to-end UniFi forwarding test. The API-first daemon
is alpha outside its [narrow v1 contract](https://github.com/lance0/rustbgpd/blob/main/docs/reference/v1-stable-contract.md).
Pin a tested rustbgpd release instead of tracking `main` for unattended use.

## What changes

- **Unchanged:** IPFIX/nfdump, probes, prefix scoring, 3-win/quiet/hold-down
  safety policy, read-only dashboard, UniFi inbound BGP prefix filters.
- **Changed:** BGP peer is served by rustbgpd; `rbgp rib add` / `rib delete`
  perform route changes through a local Unix gRPC socket.
- **State ownership:** v3.3 state with installed routes implicitly belongs to
  ExaBGP. Live rustbgpd refuses to reuse it until those routes are withdrawn.
- **Route admission:** successful `rbgp` commands indicate **local daemon
  acceptance**, not proof of UniFi's BGP RIB, FIB, or actual WAN forwarding.
- **Restarts:** startup and periodic (5-minute) reconciliation re-announces
  persisted routes while the rustbgpd peer is confirmed Established. Disruption
  and loss of connectivity are still possible when BGP restarts.

## Prepare (no production cutover yet)

1. Verify UniFi has your existing inbound public-prefix filters and
   `maximum-prefix` set safely above `--max-routes` (75 for 50 managed routes).
   Keep your current gateway BGP config backed up.
2. Install a pinned rustbgpd release following its official
   [deployment documentation](https://github.com/lance0/rustbgpd/blob/main/docs/how-to/deployment.md)
   on the *same* VM as the optimizer. The `rbgp` executable must be at
   `/usr/local/bin/rbgp`, unless `--rbgp-cli` overrides it.
3. Customize [`examples/rustbgpd.toml.example`](../examples/rustbgpd.toml.example)
   to your own AS, management IP and UCG BGP peer; validate it with
   `rustbgpd --check --strict /etc/rustbgpd/config.toml`. The local API is an
   owner-only UDS by default; do NOT expose it on a LAN TCP interface.
4. Keep your running optimizer service in **dry-run**. The file
   [`systemd/route-optimizer-rustbgpd.service.example`](../systemd/route-optimizer-rustbgpd.service.example)
   is a reference unit, not a replacement auto-installed by the project.
   Do **not** launch two optimizer instances sharing `/var/lib/route-optimizer`
   or the same probe/source routing configuration.
5. After the BGP session has transitioned to rustbgpd, run the read-only CLI:

   ```bash
   rbgp -s unix:///var/lib/rustbgpd/grpc.sock --json neighbor <UCG_BGP_IP>
   rbgp -s unix:///var/lib/rustbgpd/grpc.sock rib
   ```

   The first command must show an `Established` neighbor state.

## Controlled cutover checklist

**Do this only in a scheduled test window after a rollback plan.** Migrating
BGP daemons restarts the iBGP session, even when the UCG configuration remains
unchanged. Routes must not be left installed under the former daemon.

1. Back up `/opt/route-optimizer`, `/var/lib/route-optimizer` and the gateway BGP
   configuration. Confirm console/SSH access independent of the WAN being moved.
2. Before stopping ExaBGP, stop the optimizer service and use its **old**
   `--bgp-backend exabgp --withdraw-all --apply` invocation with the real
   site config and state file. Inspect the UCG route table and confirm the old
   optimizer `/24` routes are gone. **Do not simply erase the state file.**
   If routes were previously created with a pre-v3.3 script, verify old `/32`
   routes are gone as well.
3. Stop/disable `exabgp.service` and start `rustbgpd.service` with the customized
   TOML. Inspect `rbgp --json neighbor <UCG_IP>` and the UCG BGP neighbor state.
   Confirm only *one* BGP speaker is bound to port 179 / peered with the UCG.
4. Install the new optimizer script and use `--bgp-backend rustbgpd` in **dry-run**;
   verify BGP health, IPFIX age, representative probes, and the dashboard.
5. In a controlled manual test, inject **one already observed eligible public**
   `/24` using `rbgp rib add <PUBLIC_PREFIX> --next-hop <WAN_GATEWAY_IP>`.
   Verify in UCG FRR **and** the kernel FIB that the next hop is the real WAN
   gateway rather than the optimizer VM, then confirm forwarding egress and
   connectivity. Remove with `rbgp rib delete <PUBLIC_PREFIX>`. Never test
   this with documentation, local, or connected WAN prefixes.
6. Only after manual forwarding works, add `--apply` to the rustbgpd optimizer
   service. Prefer a temporarily reduced `--max-routes` and watch route events.
   Do not use the dashboard as a route-control API.

## Commands

```bash
# Read-only neighbor health on the local Unix socket:
rbgp -s unix:///var/lib/rustbgpd/grpc.sock --json neighbor <UCG_BGP_IP>

# Example use with documentation IPs only (NOT to be run unchanged):
rbgp -s unix:///var/lib/rustbgpd/grpc.sock rib add 203.0.113.0/24 --next-hop 198.51.100.1
rbgp -s unix:///var/lib/rustbgpd/grpc.sock rib delete 203.0.113.0/24

# Dry-run on the optimizer VM after stopping the original optimizer process:
sudo /opt/route-optimizer/route-optimizer.py \
  --config /etc/route-optimizer/config.json \
  --bgp-backend rustbgpd --watch 60
```

The UCG BGP configuration example remains
[`examples/unifi-bgp.conf.example`](../examples/unifi-bgp.conf.example).
Its router ID and peer IP must be edited for this topology; it doesn't depend
on whether ExaBGP or rustbgpd is used on the optimizer VM.

## Rollback

Stop the rustbgpd-mode optimizer. While rustbgpd is operational and its BGP
peer is Established, run `--bgp-backend rustbgpd --withdraw-all --apply` with
the correct state path. Verify routes are withdrawn from the UCG before
stopping rustbgpd. Restore ExaBGP, then the old working optimizer service.
If the daemon/session is already down, use UCG-side verification and remove
stale advertisements or reset the peer as appropriate; do not assume deleting
local state withdraws remote BGP routes.

## Known limits

The current engine tracks BGP CLI success and state, but cannot guarantee the
UCG applied the route. It does not compare rustbgpd's advertised route table
against the UCG forwarding table. A `rbgp` CLI or neighbor JSON contract change
could require an adapter update. Route flips can reset active NAT connections.