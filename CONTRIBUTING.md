# Contributing

Bug reports, compatibility tests, documentation, and pull requests are welcome.

- Start with an issue describing your UniFi model, firmware/software versions, and whether your BGP third-party next-hop test passed.
- Do **not** include your real public WAN addresses, site-local IP addresses, hostnames, MAC addresses, customer traffic, IPFIX dumps, or router credentials in a public issue, screenshot, trace, or PR.
- Keep `--apply` opt-in. Route-prefix validation, maximum route caps, stateful hold-down, and emergency rollback are safety-critical.
- Run `python3 -m unittest discover -s tests -v` and `python3 -m py_compile src/*.py` before submitting.
- Include a reproducible dry-run example or synthetic flow fixture if you modify network/decision logic.

Note: This is experimental software and route changes can interrupt active connections.