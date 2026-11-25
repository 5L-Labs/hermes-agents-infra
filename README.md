# Hermes agents infrastructure

An nginx HTTPS wrapper and rootless Podman Quadlet examples for independently networked Hermes gateways. This packages an applied two-instance deployment; it does not fork or patch Hermes application code.

## Public routing contract

| Public route | Private upstream |
| --- | --- |
| `/` | Redirect to `/hermes-1/` |
| `/hermes-1/` and `/hermes-3/` | Corresponding gateway Webapp on 9120, including its own `/api` |
| Other canonical `/hermes-N/` routes | Legacy dashboard on 9199 until that instance is migrated |
| `/hermes-N/external-api/` | Separate API server on 8680 |

Canonical instance numbers are 1–99: 1 maps to `hermes-01`, 9 to `hermes-09`, 10 to `hermes-10`. Leading zeros and out-of-range routes are rejected. Routing does not provision containers, networks or services. A new instance also needs its own network, an nginx network attachment/DNS resolver, authenticated listeners, and an explicit Webapp selection in the port map.

The proxy strips the public mount, preserves the original Host including port and forwards the HTTPS scheme and mount prefix. It supports WebSocket upgrades, disables response buffering/cache, and allows long-running responses. There is no root `/assets/` fallback. Authentication belongs to Hermes: TLS and network separation are not substitutes for authenticated upstreams.

## Files and private inputs

- `nginx.conf`: applied routing/TLS behavior, with example Podman DNS addresses substituted.
- `quadlet/`: nginx image/container, separate networks and minimal Hermes container examples.
- `examples/`: nonsecret environment/config snippets to merge into private deployment files.
- `tests/validate.py`: disposable real-nginx tests with synthetic backends; publishes no host ports.

The repository deliberately excludes exact deployment addresses, hostnames, usernames, production paths and private profile data. Do not import a live profile or the original deployment snapshot into Git. Keep populated configuration, provider credentials, certificates, backups, conversations and databases outside this checkout:

```text
~/.config/hermes-gateway/
  hermes-01.env                 # private credentials/settings; mode 0600
  hermes-03.env
~/.local/share/hermes-gateway/
  tls/{cert.pem,key.pem,dhparams4096.pem}
  hermes-01/                   # private /opt/data profile, config.yaml, .env and state
  hermes-03/
~/.config/containers/systemd/hermes-gateway/
  nginx.conf                   # deployment-specific copy, not a symlink into this repo
  *.container, *.network, *.image
```

Never commit real passwords even to example files. The `.gitignore` is a guardrail, not a secret scanner. Set ownership/permissions for the container's Hermes UID and your rootless user namespace before starting. Do not recursively change ownership on an existing profile without understanding that mapping.

## Adapt the examples locally

> **Keep this checkout out of every Quadlet search path.** Do not clone or keep it anywhere reachable from `~/.config/containers/systemd` (including through symlinked folders). Quadlet scans recursively, so on the next `daemon-reload` these example units can replace or shadow live units of the same name. A Hermes outage was caused exactly this way: the example `hermes-03.container` replaced the live unit and failed on its missing envfile.

Requires Linux with rootless Podman, Quadlet and systemd user services. The examples were generation-checked with Podman 5.7 and validated against Hermes image `10dc4e18cee7` (`docker.io/nousresearch/hermes-agent:latest` at the time). nginx must support `http2 on` (1.25.1+) and `sub_filter`; the test uses `nginx:alpine`. Review/pin image digests for your own release policy; the examples retain upstream `latest`/`alpine` references rather than introducing an image-update mechanism.

1. Copy the Quadlets and `nginx.conf` into the systemd directory above, not into an already-running installation without reviewing the differences. Prepare the private profiles, credentials and TLS files first. An example username/password of `replace-locally` must never be deployed.
2. Select unused subnets. Public examples use **10.203.1.0/24** and **10.203.3.0/24**; these are illustrative, not the original deployment. The network gateways `.1` supply Podman DNS, so update `nginx.conf` resolvers to match. nginx has per-network example addresses **10.203.1.10** and **10.203.3.10**; use those exact addresses in each private profile's `dashboard.trusted_proxies`.
3. Merge `examples/webapp.yaml` into each profile's existing config. Set your own HTTPS origin and the correct `/hermes-1` or `/hermes-3` public URL. Bind Webapp to `0.0.0.0:9120` inside the container network. Preserve unrelated profile settings. Set the password-auth environment variables using `examples/hermes.env.example` in the private envfile.
4. Configure the optional separate API on `0.0.0.0:8680` in Hermes if you use `/external-api/`; nginx does not start or configure that server. Keep any API authentication configured independently. Enable the legacy dashboard only if you need rollback on 9199.
5. Verify certificate/key/DH-file mounts and the readonly nginx configuration path. nginx publishes **9143 only**; the internal HTTP80 listener is not host-published. The minimal Hermes examples publish no host ports. Add unrelated messaging/voice ports or mounts separately only if required.
6. Check generation before starting:

```sh
QUADLET_UNIT_DIRS="$PWD/quadlet" /usr/libexec/podman/quadlet -dryrun -user
```

The Quadlet binary path varies by distribution.

Preflight before stopping or replacing a live unit:

- Confirm every `EnvironmentFile=` and `Volume=` source path in the installed units exists.
- Run `/usr/libexec/podman/quadlet -dryrun -user` and check each unit's `SourcePath` is the file you intend, with nothing loaded from this checkout.

Once private inputs are ready and you have approved starting the deployment:

```sh
systemctl --user daemon-reload
systemctl --user start hermes-01.service hermes-03.service nginx-hermes.service
podman exec nginx-hermes nginx -t
```

`EnvironmentFile=` under **[Container]** passes settings into Hermes. A **[Service]** environment file alone is not equivalent. Quadlet creates network/image dependencies from the file references. The example uses valid systemd `Restart=always`, rather than copying a Podman restart-policy name into the service section.

The static nginx IPs are a reproducibility improvement in these examples. The applied deployment used its observed per-network nginx IPs and bounded trust, so trust needed review after recreation. Do not copy example addresses into a live network without provisioning them.

## Isolation and security boundary

Backend instances have separate networks. Only nginx joins both; IPv4 and IPv6 forwarding are disabled. This avoids making every backend a peer on one shared frontend network, but **a compromised nginx process can still reach both backends**. This is not a hard tenant-isolation boundary. Keep upstream authentication, bounded proxy trust and normal container/host controls.

Nginx has a read-only root filesystem, writable tmpfs runtime/cache/log paths, no-new-privileges and a PID limit. Certificates and config are read-only mounts. Auth cookies were verified to be prefix-scoped in the live deployment; some browser preferences remain origin-global, so this is not a claim of complete browser-storage isolation.

## Validation

Run from a checkout with Python 3, OpenSSL and Docker or Podman:

```sh
python3 tests/validate.py
```

The test starts uniquely named disposable containers and a network, generates a temporary self-signed certificate, and runs real nginx. It verifies syntax, canonical single/double-digit routes and invalid boundaries, query-preserving redirects, Webapp/external-API separation, forwarded headers, WebSocket101, incremental streaming and the rollback filter's scope. It removes its own containers/network afterward, even on test failure. `CONTAINER_ENGINE`, `TEST_NGINX_IMAGE` and `TEST_BACKEND_IMAGE` can override defaults; the backend image needs `python3` with the standard library. Test TLS material is temporary and never production material.

Live acceptance of the original deployment separately covered authenticated browser login/render/reload, correctly prefixed assets, gateway WebSockets, cross-instance cookie rejection and both external API health routes. One instance's authorized model/tool/iMessage flow was also user-confirmed. These results are not reproduced by the synthetic test and do not replace acceptance on a new installation.

## Legacy dashboard and rollback

The applied deployment kept standalone dashboards on 9199 for rollback. The port map selects Webapp9120 only for migrated instances1/3. The `react-vendor-R7IuHd60.js` response-filter workaround is retained **only for legacy9199 responses**; it is build-hash-specific, not a general Hermes patch. Webapp responses must remain unchanged. Revisit/remove the workaround when upgrading or retiring the dashboard.

For route-only rollback, remove the affected instance's explicit9120 entry, run `nginx -t` and reload nginx. This works only while that instance's authenticated dashboard9199 is still running. Do not replace current user data with old profile/database backups.

```sh
podman exec nginx-hermes nginx -t && podman exec nginx-hermes nginx -s reload
```

For gateway config changes, check activity before restarting. The installed Hermes build used during migration supported **SIGUSR1 restart-when-idle** and an s6-supervised gateway relaunch; verify that contract in your version. A container restart/SIGTERM is not equivalent and can interrupt active conversations, background work or queued deliveries. Do not automate forced restarts as part of nginx updates.

Retiring dashboards, deleting the legacy filter, switching the default UI map to9120 and removing preexisting backend host publications are explicit follow-up changes. They have not been performed by this package. This repository's minimal examples omit the original deployment's unrelated mounts, DNS overrides, messaging integrations and preexisting published ports; it is not a drop-in backup of the entire host.

Follow-up: add hermes-02 so the examples cover 01–03, and consider replacing the per-instance Quadlets/port-map entries with one template plus a small per-instance config for what differs (instance number, subnet, nginx IP, Webapp vs legacy port).
