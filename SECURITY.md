# Security and privacy

This experimental extension is trusted executable code, not a sandbox. Review its
manifest before installing or enabling it. Network, processes, unrestricted local
filesystem access, and disclosure of one native provider credential are declared.

The microphone opens only after an explicit start and remote connection. Muting
keeps the device open but drains/discards captured audio; stop closes it. Voice
sends microphone audio, new typed prompts and successful final agent answers to
the selected OpenAI/Codex realtime service. Do not enable it for conversations you
cannot share with that provider. Headphones are recommended; no local acoustic
echo cancellation is implemented.

Credentials are resolved through octet's existing provider flow, retained only in
memory, and sent only to fixed authenticated endpoints. Redirects and environment
proxies are disabled for those requests. Error bodies, tokens, SDP and raw media
are not logged or recorded. Device settings contain no credentials and stay in
the user's private configuration directory, outside this repository.

Playback/capture, UI observations and coding handoffs use bounded queues. Session
ownership and generation fence injection. On connection/audio/UI failure, voice
stops rather than invisibly capturing or automatically replaying ambiguous work.
The ordinary coding agent still owns tool permissions and effect confirmation.

Dependency installation is an explicit setup operation using hash-locked packages
in a private virtual environment. Runtime discovery and package building do not
install or download dependencies. Hashes establish artifact integrity, not safety;
upstream dependency updates still require review. The vendored SDK is source only
with retained license/provenance. CI never needs provider credentials or audio hardware.

Do not attach credentials, recordings, private prompts, or unsanitized logs to bug
reports. For a sensitive issue, use GitHub's private vulnerability reporting when
available, or contact the Skaft maintainers privately before public disclosure.
