# octet-voice

Experimental native realtime voice extension for [octet](https://github.com/skaft-software/octet).
Standalone source repository; not an official release or a production qualification.

Optional **bidirectional realtime voice**, not dictation followed by text-to-speech:
live microphone audio, conversational spoken responses, and delegation into the
active coding conversation. The voice model never executes tools itself. The
ordinary octet agent still owns coding, tools, effects and cancellation policy.
Requires the v0.10.0 candidate host. The needed Python SDK snapshot is vendored
under `vendor/` with its license, provenance and regression tests.
Not in the official release catalog; no automatic enablement or recording.

## Controls

- **Ctrl+F8** or `/voice`: start/stop. Nothing opens the microphone on startup.
- **Ctrl+F9** or `/voice mute`: mute/unmute. Capture is drained and discarded while
  muted; queued outgoing WebRTC samples are cleared. The capture device remains
  open. **Stop** to close it.
- **Ctrl+F10** or `/voice stop`: stop, including while coding via the shortcut.
  If your terminal intercepts these modified function keys, use slash commands while idle. Stopping
  voice **does not cancel already admitted coding work**; use native Escape/Ctrl+C.
- `/voice status`, `/voice devices`, `/voice settings`; options in `/extensions`.
- `/voice backend codex|openai`, `/voice model NAME`, `/voice speaker NAME`.
  Preferences change only while stopped and apply to the next conversation.

### Live voice strip

A persistent strip above the composer shows connection state, elapsed time,
microphone mute, and separate microphone/output waveforms measured from actual
PCM (RMS, -60..0 dBFS), with bounded 2.4-second histories. Muting clears the
microphone meter; stopped/stalled streams decay to silence. The output meter
measures PCM written to playback, not a hardware loopback. Final speech captions
replace one ephemeral strip line; they do not append transcript messages.
The strip resizes without taking focus or replacing the normal composer, and
uses terminal palette colors (`NO_COLOR` disables styling). It disappears on
stop, failure, session replacement or shutdown. Routine start/mute/stop actions
are silent; actionable failures still appear in octet. Explicit `/voice status`
and settings/device queries retain their requested reports.

Use **headphones**. There is no local acoustic echo cancellation, so open speakers
can feed replies back into the microphone. Server VAD handles interruption; local
playback queues are flushed on speech-start/interrupt. Codex's current frameless
sideband has no response-linked RTP timestamps or reliable media-restart markers:
newly decoded RTP after interruption cannot safely be classified as old versus
new. Server cancellation is responsible for stopping that media. This is not a
claim of a fully qualified native Codex audio implementation.

## Authentication and backends

Default: `codex`, model `gpt-live-1-codex`, voice `cove`. Uses the native host's
**one reviewed provider/model credential**, from the selected `codex` coding
model. The same native Codex OAuth login is used on Linux, macOS and Windows;
there is no platform-specific voice login or credential-file workaround. Native octet owns sign-in/refresh; this extension never reads credentials
files, refresh tokens, arbitrary headers or ambient API keys. A Codex subscription
and realtime entitlement are required. Backend availability is not guaranteed by
ordinary coding-model access. No silent fallback to another backend or dictation.

Codex source inspected at `cb1bc0ae4ed996c67a6e118bf79a7435f0d3f0d2`:

- `codex-rs/codex-api/src/endpoint/realtime_call.rs`: OAuth WebRTC call creation
  at `https://chatgpt.com/backend-api/codex/realtime/calls?intent=quicksilver&architecture=avas`.
- `codex-rs/core/src/client.rs`, `realtime_conversation.rs`: authenticated control
  sideband `wss://api.openai.com/v1/live/{call_id}`, `openai-alpha: quicksilver=v2`.
- `codex-rs/codex-api/src/endpoint/realtime_websocket/`: frameless V3
  `delegation.created`, `delegation.context.append`, `session.context.append`.

The separate `openai` backend uses public GA Realtime WebSocket
`wss://api.openai.com/v1/realtime`, default `gpt-realtime-1.5` / `marin`, audio
PCM16 mono 24 kHz, server VAD and function calls for coding handoff. It requires
an API key configured **in octet's native OpenAI provider** and incurs ordinary
Realtime API charges. A Codex OAuth token is never sent to that public path.
Select a matching native coding provider, or set `credential_model` to the exact
registered native coding-model ID whose credential should be resolved. It is
not the realtime model name. Sign in using native octet controls first.

Credential destinations are fixed, redirects and environment proxies disabled;
Location is parsed only as a bounded opaque call ID, not followed. WebRTC media
uses the authenticated SDP's negotiated remote media addresses (no custom
STUN/TURN servers). Network/provider error bodies, tokens and SDP are not logged.
Connections do not reconnect or replay ambiguous handoffs automatically.

## Conversation and handoff semantics

Voice speaks for the **shared active conversation**, including typed prompts and
successful final agent answers, not a separate hidden executor. Only turns
observed after voice starts are shared; existing history is not uploaded.
Reasoning and raw tool output are not forwarded. Spoken answers over 3960 UTF-8
bytes get a short notice directing you to the native answer.

A handoff enters `session/send_user_message` with `deliver_as: steer`, the
retained native resource owner and the voice-start request's parent ID. Idle
input starts a normal run; active input waits at the next steering boundary.
A visible `[Voice handoff to coding agent]` source marker prevents reflecting an
idle handoff back into realtime as a new typed request. Typed context is marked
already submitted and must not be delegated again.

The current host acknowledges **input admission only**, not execution completion,
and does not issue an input-to-run correlation receipt. Live steering bypasses
`before_prompt`. Therefore this extension does **not** fabricate per-handoff
completion: admission gets an explicitly nonfinal acknowledgement; successful
native final answers become standalone shared conversation updates, never a
made-up completion for a specific handoff. Multiple voiced/typed turns may be
coalesced by the native coding run. Final-answer/lifecycle events are not an
independent proof that any particular request succeeded. This is a deliberate
parity boundary until the host exposes durable correlated input receipts.

Duplicate handoffs are never executed twice in a session. Pending delegation and
media have independent bounded queues so host admission cannot stall playback or
barge-in. Ambiguous ACKs, overflow, audio failure or disconnect stop voice with
no retries. Owner/generation changes fence further injection; session settlement,
shutdown and extension process-tree supervision close media. A conversation is
limited to one hour; there is no reliable realtime spend meter in this version.

## Local install

Audio backend selection is native to the machine running octet:

- **Linux:** little-endian PCM and PipeWire (`pw-record`, `pw-play`, `wpctl`). No
  PortAudio library or sounddevice package is required. Capture continuously sends
  fixed-duration PCM frames (including silence), with clock-corrected pacing,
  a 60 ms playback jitter prebuffer and an 80 ms output lead. Packet fragments
  are assembled without padding each packet; interruption clears queued media.
- **macOS:** sounddevice/PortAudio callbacks using Core Audio. Grant microphone
  permission to the terminal/application hosting octet when the OS requests it.
- **Windows:** sounddevice/PortAudio callbacks using the native default audio
  devices. Allow microphone access for desktop applications in Windows privacy
  settings if needed. No PipeWire/POSIX shell is used.

The macOS and Windows paths are experimental implementations, **not live-device
qualification claims**. Headphones remain required for reliable duplex use;
there is no local acoustic echo cancellation or remote-client microphone tunnel.

Install `uv` and make an existing Python 3.11+ bootstrap interpreter available on
PATH (or explicitly set up octet's private Python runtime). The manifest points
at `launcher.py`: the existing host already supports native Python-script launch
on all three platforms; no host change, shell or argument expansion is needed.
The stdlib launcher selects only the preprovisioned Python 3.14 voice venv below,
never falls back to global dependencies, and never downloads or installs anything.
On Windows it queries the native profile directory because the host sanitizes
`USERPROFILE`. Keep `uv` and dependency acquisition in explicit setup only.

From this repository (explicit setup; dependency acquisition may use the network):

```sh
uv venv --python 3.14 ~/.local/share/octet-voice/venv
uv pip sync --python ~/.local/share/octet-voice/venv/bin/python \
  --require-hashes requirements.lock
python3 build-package.py /tmp/octet-voice-stage \
  --archive /tmp/octet-voice-release/octet-voice-0.10.0.tar.gz
octet extension install --path /tmp/octet-voice-release/octet-voice-0.10.0.tar.gz
octet --enable-extension octet-voice
```

On Windows, the same private environment is under
`%USERPROFILE%\.local\share\octet-voice\venv`; provision it explicitly in PowerShell:

```powershell
uv venv --python 3.14 "$HOME/.local/share/octet-voice/venv"
uv pip sync --python "$HOME/.local/share/octet-voice/venv/Scripts/python.exe" --require-hashes requirements.lock
python build-package.py "$env:TEMP/octet-voice-stage" --archive "$env:TEMP/octet-voice-0.10.0.tar.gz"
octet extension install --path "$env:TEMP/octet-voice-0.10.0.tar.gz"
octet --enable-extension octet-voice
```

Use the POSIX setup commands above on macOS as well as Linux. Setup may download
Python/packages; ordinary discovery, installation and extension initialization do
not. The stdlib package builder works without Bash on all three platforms;
review its source-only archive before installing it on the target.
The explicit staging allowlist includes both audio backends, launcher and vendored
SDK, never a venv, cached bytecode or local credentials.

The staging directory must be new. Review capability declarations first:
network (OpenAI/Codex), process (private Python and Linux PipeWire), unrestricted filesystem (local
preferences and private interpreter) and provider-credential disclosure.
These declarations describe authority, **not a sandbox**. Installation and
persistent enablement remain separate; the latter is an explicit user decision.
The strip itself works on the unmodified candidate through negotiated `remote_ui`;
SDK changes are vendored into the staged extension, not globally installed.
For fully quiet controls, `patches/quiet-shortcuts.patch` contains the optional
generic native-host change that removes unconditional shortcut-dispatch notices.
The unmodified candidate works but still emits that host notice. See
[HOST-INTEGRATION.md](HOST-INTEGRATION.md); no host patch is applied by installation.

Preferences are atomically written at `~/.config/octet/voice.json` (no secrets).
Files use mode 0600 on POSIX; Windows uses the inherited user-profile ACL
(no automatic ACL changes):

```json
{
  "backend": "codex",
  "model": "gpt-live-1-codex",
  "voice": "cove",
  "input_target": null,
  "output_target": null,
  "credential_model": null
}
```

Null device targets use native defaults. `/voice devices` lists devices; Linux
accepts stable PipeWire `node.name` strings rather than changing numeric IDs.
macOS/Windows accept the native device names reported by that command. The extension
subprocess environment is sanitized by the host. If `XDG_RUNTIME_DIR` is absent,
only an owned, mode-0700 nonsymlink `/run/user/<uid>` is used. No desktop/session
or system audio settings are modified. Device choice and gain remain user-owned.
Live macOS/Windows hardware, remote-terminal client microphones, Bluetooth
latency, acoustic speaker feedback and native Codex feature gates are not qualified.
No desktop/session settings, device routing, gain or system audio configuration
are changed automatically.

### Dependency review

`requirements.in` pins aiortc 1.14.0, aiohttp 3.13.3 and sounddevice 0.5.6;
sounddevice is marked only for Darwin/Windows. The universal Python 3.14 lock
contains SHA-256 hashes for the complete resolved dependency set. Development-only
lock refresh: `uv pip compile requirements.in --generate-hashes
--universal --python-version 3.14 --output-file requirements.lock`.
No package installation is performed by that command.

sounddevice 0.5.6 PyPI metadata declares MIT, Python >=3.7, and CFFI (already in
the WebRTC closure); NumPy is an unselected extra. The reviewed macOS universal2
and Windows x64/ARM64 wheels contain PortAudio and the MIT license; downloaded
wheel bytes were checked against PyPI SHA-256 metadata. PortAudio is MIT-licensed;
retain upstream license notices with separately provisioned binary dependencies.
The source bundle vendors no sounddevice/PortAudio binary; only the octet SDK is
vendored, with its MIT `SDK-LICENSE` and source provenance. Metadata/hash review
is not a security audit or platform/hardware qualification.

To disable, use `/extensions` or remove `octet-voice` from `enabled_extensions`.
`octet extension remove octet-voice` removes the bundle, not the private venv or
preferences. There is no background recorder outside the extension process tree.

## Verification

Provider-free tests never open devices, call providers or spend money:

```sh
PYTHONPATH=vendor ~/.local/share/octet-voice/venv/bin/python \
  -Werror::ResourceWarning -m unittest discover -s . -q
PYTHONPATH=vendor:tests/sdk ~/.local/share/octet-voice/venv/bin/python \
  -Werror::ResourceWarning -m unittest discover -s tests/sdk -q
```

On Windows PowerShell, set `$env:PYTHONPATH = "vendor;tests/sdk"` and use
`& "$HOME/.local/share/octet-voice/venv/Scripts/python.exe"` for the interpreter.
Run the same two `-m unittest discover` commands (without the POSIX assignments).

Fixtures cover duplex media, V2/V3 wire frames, OAuth signaling/sideband header
containment, malformed/oversized events, mute queue invalidation, silence pacing,
response serialization, bounded audio backpressure, slow admission/barge-in,
ambiguous ACKs without replay, handoff deduplication, source reflection prevention,
shared-answer attribution, owner fences, credential/start cancellation races,
shortcuts, lifecycle teardown, measured metering, quiet UI frames and private preferences. Live backend and hardware
qualification are separate from fixture success.
