# Node identity policy

## Rule

The project uses one strict rule:

**Initialize node identity once, then reuse it forever.**

The identity is bound to the Railway Persistent Volume mounted at `/data` (or the path supplied by `RAILWAY_VOLUME_MOUNT_PATH`).

## Identity files

The persistent identity set is:

- `uuid.txt`
- `reality_private_key.txt`
- `reality_public_key.txt`
- `reality_short_ids.json`
- `subscription_token.txt`
- `.node-identity-initialized`

## First deployment

A new, empty Persistent Volume has no identity marker and no identity files. `identity-init.py` creates the complete identity set exactly once.

## Later startup / redeploy / restart

When the marker exists, the initializer does not generate or replace anything. It validates that the complete identity set still exists and then returns `NODE_IDENTITY=REUSED`.

If the marker exists but any identity file is missing or malformed, startup fails closed. The application does **not** generate a replacement identity.

If there is a partial identity on an uninitialized volume, startup also fails closed rather than mixing old and newly generated identity material.

## Runtime artifacts

`runtime.json`, `state.json`, `manifest.json`, `runtime-manifest.json`, `subscription.txt`, and `config.json` are derived runtime artifacts. They may be regenerated from the current Railway deployment environment and must never become the source of node identity.

## External configuration boundary

`RAILWAY_TOKEN`, Node 5 Cloudflare variables, Railway networking, and region/country remain external configuration. The application does not rewrite them to preserve identity.

Deleting or replacing the Persistent Volume intentionally destroys the stored identity and therefore starts a new identity lifecycle.