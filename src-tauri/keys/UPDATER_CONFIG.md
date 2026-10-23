# Updater config fragment — for the config worker to merge into `tauri.conf.json`

This file is **not** configuration. It is the exact `plugins.updater`
block the config worker should merge into `src-tauri/tauri.conf.json`,
replacing the placeholder block currently there
(`"endpoints": ["https://example.invalid/releases/latest.json"]`,
`"pubkey": ""` — see `src-tauri/icons/README.txt`).

Merge target: `plugins.updater` in `tauri.conf.json`. Merge only this
block; do not change any other key in the file in the same edit.

## Exact JSON to merge

```json
{
  "plugins": {
    "updater": {
      "endpoints": [
        "https://updates.youtubeagent.app/releases/{{target}}/{{arch}}/{{current_version}}/latest.json",
        "https://github.com/youtube-agent/youtube-agent/releases/latest/download/latest.json"
      ],
      "pubkey": "dW50cnVzdGVkIGNvbW1lbnQ6IG1pbmlzaWduIHB1YmxpYyBrZXk6IEI2ODkxQjhCNkMzMDQ5NzMKUldSelNUQnNpeHVKdHB1VStFYnJ3Vm5GQ1RNZEVqWEhmdjNPQ2NXNitHOXdydmxsTnpMVWhFMS8K",
      "windows": {
        "installMode": "passive"
      }
    }
  }
}
```

Equivalently, the value of `plugins.updater` alone is:

```json
{
  "endpoints": [
    "https://updates.youtubeagent.app/releases/{{target}}/{{arch}}/{{current_version}}/latest.json",
    "https://github.com/youtube-agent/youtube-agent/releases/latest/download/latest.json"
  ],
  "pubkey": "dW50cnVzdGVkIGNvbW1lbnQ6IG1pbmlzaWduIHB1YmxpYyBrZXk6IEI2ODkxQjhCNkMzMDQ5NzMKUldSelNUQnNpeHVKdHB1VStFYnJ3Vm5GQ1RNZEVqWEhmdjNPQ2NXNitHOXdydmxsTnpMVWhFMS8K",
  "windows": {
    "installMode": "passive"
  }
}
```

## Notes for the config worker / reviewer

- **Pubkey:** the `pubkey` value above is copied verbatim from
  `src-tauri/keys/updater-pubkey.txt` (the public half of the Round 6
  **DEV** keypair — see `docs/UPDATER_KEY_RUNBOOK.md` §3). It is public
  material and safe to commit. Before the first public release it must
  be replaced with the production pubkey per that runbook §3; the
  private key is never in this repo.
- **Endpoints are placeholders until the release CDN exists.**
  - The primary endpoint uses the **named placeholder domain**
    `updates.youtubeagent.app` — it is a placeholder name for the
    future release CDN, following the dynamic-manifest pattern in
    Master Roadmap §11.4 (templated `{{target}}` / `{{arch}}` /
    `{{current_version}}`, so the server can do phased rollouts
    and rollbacks per §11.6). Do not treat it as a live, verified URL
    until the release pipeline actually serves a signed `latest.json`
    there.
  - The second endpoint is the GitHub Releases fallback pattern from
    §11.4 (primary CDN manifest first, GitHub Releases
    `latest/download/latest.json` as fallback; endpoints are tried in
    order). The `youtube-agent/youtube-agent` owner/repo path is
    likewise a placeholder until the real release repository exists.
  - **HTTPS only.** Both endpoints are `https://`; never merge an
    `http://` updater endpoint, for any reason.
  - The current in-config placeholder `https://example.invalid/...`
    (RFC 2606 `.invalid`, deliberately unroutable) **must be replaced**
    by this block when it is merged — do not keep `example.invalid`
    alongside the endpoints above, and do not ship a build whose
    endpoints still point at `example.invalid` as anything other than
    an explicit “updater not yet configured” state.
- **`windows.installMode: "passive"`** is the §11.4 decision: install
  progress is shown, no user interaction is required to apply the
  update once the (frontend-gated, idle-only) install is triggered.
- **Capabilities:** the frontend/WebView side already declares
  `updater:default` and `process:allow-restart` in
  `src-tauri/capabilities/default.json`; no capability change is part
  of this fragment.
- **Fail-closed reminder:** with the placeholder endpoints unreachable
  or an artefact that does not verify against `pubkey`, checks return
  an error and nothing installs — that is the designed behaviour
  (see `src-tauri/src/updater.rs`), not a bug to “fix” by loosening
  verification.
