# Host integration

Target: octet v0.10.0 candidate, base `47389ad1`. This repository does not fork or
replace octet's authentication, process supervisor, UI protocol, or coding agent.

- The complete extension is at the repository root. `build-package.py` packages
  an explicit source allowlist, including the reviewed Python SDK under `vendor/`.
- The SDK snapshot has unreleased generic API 0.4 convenience surfaces for static
  shortcuts, credential resolution, input injection, request timeouts, and cached
  remote UI. See `SDK-PROVENANCE.txt` and `tests/sdk/`. No globally installed SDK
  is modified; the native host protocol is unchanged.
- `patches/quiet-shortcuts.patch` is an optional generic host patch. It removes
  the two unconditional shortcut-dispatch transcript notices, preserving actual
  results/errors. It is not applied or included in installed extension bundles.
  Review it against the matching octet source and run the host's shortcut tests
  before building your own host. The extension works without it.
- Codex credentials come from native `provider/credentials`, using the same
  native OAuth login and refresh as coding. No second sign-in or credential-file
  discovery is implemented. Public OpenAI Realtime uses the native OpenAI provider
  instead, only when explicitly selected.

## Qualification boundaries

Provider-free tests cover protocol, UI, lifecycle, queues and platform selection.
Linux local Codex OAuth connection, mute, 50-second idle, resize and clean stop
have been exercised using the installed standalone bundle. An independent
107-second output-only synthetic-silence soak reported zero PipeWire underruns,
no playback error and at most 2880 queued bytes. These are not speech-fidelity
or multi-hour reliability qualifications. macOS and Windows backends have
fake-stream coverage only, not real-device qualification.
Neither provider-free tests nor a successful connection prove audible quality.
There is no acoustic echo cancellation, no recording, and no automatic reconnect
or ambiguous handoff replay. Native handoff acknowledgement is admission only.

This repository intentionally excludes native octet history, credentials, user
preferences, virtual environments, audio recordings, diagnostic transcripts and
workstation paths. Source publication is separate from releasing production binaries.
