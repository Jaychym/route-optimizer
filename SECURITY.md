# Security

Route Optimizer works with sensitive network telemetry and can issue BGP announcements. Deploy it only on a trusted, segmented management network. The web UI is **read-only** but has **no built-in login**, so put it behind trusted access controls; never port-forward the raw port to the public Internet.

- Keep `/etc/route-optimizer/config.json` local, root-readable, and untracked.
- Keep `state-*.json`, `dashboard-*.json`, route history, `nfcapd.*` and logs out of public repositories and bug reports.
- Require inbound BGP prefix filtering and a separate maximum-prefix ceiling on the gateway. A probe policy route should have a kill switch so failed WANs cannot silently reroute probes.
- Protect the ExaBGP CLI and Unix service permissions. Keep `--apply` disabled during installation and troubleshooting.
- Use TLS + authentication on any dashboard reverse proxy.

For sensitive vulnerabilities, avoid including exploit details or real network data in public issues; contact repository maintainers privately once a secure contact method is provided.