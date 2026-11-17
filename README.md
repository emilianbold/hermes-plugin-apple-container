# hermes-plugin-apple-container

An [Hermes Agent](https://hermes-agent.nousresearch.com/docs) plugin that adds
Apple's [`container`](https://github.com/apple/container) CLI as a **terminal
backend**. Hermes keeps running on your Mac exactly as it does today; the
agent's shell commands, file tools and `execute_code` run inside an Apple
container — a lightweight Linux VM — so the agent cannot touch your Mac
beyond the folders you explicitly share.

Requires an Apple silicon Mac, macOS 26+, and the `container` CLI (1.2.2 was
used for development). On any other machine the backend reports itself
unavailable. Not related to, and sharing no code with, the separate idea of
running all of Hermes inside a container.

## What it does

- One long-lived container per sandbox, reused across tool calls, Hermes
  processes and sessions. Commands run via `container exec ... bash -c`, so
  installed packages, background processes and state persist between calls.
  The VM has an init process (orphans are reaped) and its keep-alive does not
  depend on the image's own ENTRYPOINT.
- **Shares nothing of your Mac by default.** No host folder is mounted, no
  SSH agent is forwarded, no ports are published, and no host environment
  variables are passed to the sandbox beyond `TZ`. That includes Hermes' own
  files: its skills, cache and credential files stay on the Mac. Sharing is
  opt-in, per folder, read-write or read-only.
- Hermes' own files can be shared read-only at `/root/.hermes` when you want
  the agent to run skill scripts or open files you attached in chat
  (`apple_container_mount_hermes_files`, see settings). This differs from the
  built-in Docker backend, which mounts them always. Credential files are
  never mounted from where they are: a private copy of each is (see
  "Credential files").
- Approval prompts for dangerous commands are skipped only while nothing of
  the Mac is reachable from the sandbox. Any share turns them back on: a
  folder (however it is written), the launch directory, Hermes' own files, a
  mount or `--ssh` passed through the extra arguments, or a setting the
  plugin cannot classify. The decision and the mounting use one parser, so
  they cannot disagree.
- `read_file`, `write_file`, `patch` and `search_files` operate inside the
  sandbox through the same exec channel (the plugin contract's
  `uses_host_filesystem` is False): the file tools see exactly what the
  terminal sees.
- Fails closed: if the `container` CLI is missing, the system service is
  down, a setting is refused, or the container cannot start, the tool call
  fails with a clear message (a failed start quotes what the CLI said). It
  never falls back to running on your Mac.
- Only ever stops or deletes containers and volumes it created (labelled
  `hermes-agent=1` + `hermes-backend=apple_container`), including when
  cleaning up leftovers from crashed sessions.

## Install

```bash
git clone https://github.com/emilianbold/hermes-plugin-apple-container
hermes plugins install file://$PWD/hermes-plugin-apple-container --yes-deps --enable
hermes config set terminal.backend apple_container
```

For development, a symlink works too:

```bash
mkdir -p ~/.hermes/plugins && ln -s "$HOME/hermes-plugin-apple-container" ~/.hermes/plugins/apple_container
hermes plugins enable apple_container
```

The container system service must be running (the plugin never starts it for
you):

```bash
container system status          # must print status: running
container system start --enable-kernel-install   # if it is not
```

The default image (`nousresearch/hermes-sandbox:desktop`, the same sandbox
image the Docker backend uses — Python, Node, shell, desktop stack) is pulled
automatically on first use (~3 GB). Any image needs `bash`, `env` and `sleep`.

A terminal backend runs in-process: `plugins.isolation: in_process` is
required (Hermes refuses the backend under `plugins.isolation: host` with a
message saying so).

## Settings

### Where a setting is written

A setting can be written in two places of `config.yaml`. Both work. One
function reads them (`resolve` in `settings.py`), and the sandbox, the
approval decision, the cache path Hermes is told about and the health checks
all use its answer.

| Place | Key | How to edit it |
|---|---|---|
| the plugin's own settings | `plugins.entries.apple_container.settings.<key>` | the form in Hermes Desktop (Settings > Plugins > Apple Container), or `hermes config set plugins.entries.apple_container.settings.<key> <value>` |
| the `terminal:` section (the keys of 0.1.0 and 0.2.0) | `terminal.apple_container_<key>` | a text editor, or `hermes config set terminal.apple_container_<key> <value>` (Hermes prints a notice that it does not know the key; the value is saved) |

`<key>` is the same word in both: `volumes` in the form is
`apple_container_volumes` under `terminal:`. The form has every setting of
the table below except `backend` and `apple_container_binary`, which exist
under `terminal:` only. One field is special: `container_persistent` ("One
sandbox for all sessions") is a Hermes core key, not an `apple_container_`
one — the form wins over `terminal.container_persistent` when both are set,
and there is no `terminal.apple_container_container_persistent`.

- **Which place wins.** Per key, a value saved in the plugin's own settings
  wins, also an empty one (an emptied list of shared folders shares nothing).
  A key that is not saved there is read from `terminal:`. Otherwise the
  default applies. When the two places hold different values for one key,
  Hermes' log gets a warning that names both.
- **What the form shows.** The form shows what is saved in the plugin's own
  settings and, for every other field, the default. It does not show a value
  that is set under `terminal:`. With `terminal.apple_container_volumes` set
  and nothing saved in the form, "Shared folders" shows `[]` while the folder
  is shared. `hermes doctor` shows what is in effect and where each share is
  written (the "Apple Container shares" line). For a form that shows
  everything, save your settings there and remove the
  `terminal.apple_container_*` keys. Hermes Desktop saves only the fields you
  changed.
- **When a change applies.** The approval decision follows a saved value at
  once. The sandbox follows when Hermes next creates its environment (after
  the idle stop, or after a restart of Hermes); a changed share, network or
  image then replaces the sandbox, and its volumes are kept.
- **A value that cannot be read is never skipped.** The sandbox does not
  start, the error names the key and the place, and approval prompts stay on.
  This covers a value of the wrong type in the plugin's own settings (Hermes
  itself only logs a warning for those), a key the backend does not have, in
  either place, a key with no value, `mount_cwd` or `keep_running` that is
  not `true` or `false`, a refused share or extra argument, and a
  `config.yaml` that cannot be parsed.
- **The word `none` on the command line.** `hermes config set <key> none`
  stores an empty value, not the text `none` (measured on Hermes 0.23.1). To
  cut the network, type `none` in the form, or write
  `apple_container_network: none` in `config.yaml` yourself. An empty value is
  refused, so the mistake shows.

Lists and the `env` mapping are JSON fields in the form: `["/Users/you/project:/project:ro"]`.

### The settings

Keys as written under `terminal:`; in the plugin's own settings, drop the
`apple_container_` prefix.

| Key | Default | Meaning |
|---|---|---|
| `backend` | — | set to `apple_container` to select this backend. In Hermes Desktop, the plain Settings dropdown for `terminal.backend` lists built-in backends only (a hardcoded list in the app); use the backend picker under **Capabilities → Toolsets → Terminal**, `hermes setup`, or the command line |
| `apple_container_image` | `nousresearch/hermes-sandbox:desktop` | image reference; `TERMINAL_APPLE_CONTAINER_IMAGE` env var is the fallback. Short and registry-qualified names are the same image (`debian` = `docker.io/library/debian:latest`); with another default registry configured in the CLI, write the full reference |
| `apple_container_volumes` | `[]` | opt-in shares, a list of `"source:/path/in/sandbox[:ro]"`. `source` is an absolute Mac folder (`~` is expanded) or a bare name for a named volume. See "Rules for shares" |
| `apple_container_mount_cwd` | `false` | mount the directory Hermes was launched from into `/workspace` (the `docker_mount_cwd_to_workspace` equivalent); with persistent volumes on, the workspace volume is skipped in favour of it |
| `apple_container_forward_env` | `[]` | allow-list of variable names forwarded into the sandbox, taken from Hermes' environment or, if absent there, from the Hermes `.env` (values never appear on any command line). Must be a list; names are trimmed; anything else is ignored with a warning |
| `apple_container_env` | `{}` | literal `NAME: value` pairs set inside the sandbox; a key that is not a variable name is dropped with a warning |
| `apple_container_mount_hermes_files` | `false` | share Hermes' own files read-only at `/root/.hermes`: `true` for all, or a list of `skills`, `cache` (files attached in chat, screenshots), `credentials` (files your skills declare; staged, see below). Off by default; without `cache`, a file you attach in chat is not visible to commands in the sandbox |
| `apple_container_shm_size` | `1g` | `/dev/shm` size (the runtime's default 64 MB is too small for browsers) |
| `apple_container_user` | *(root)* | `container exec -u` user for commands |
| `apple_container_network` | `default` | container network name. `none` gives the sandbox **no network at all** (only the loopback interface; measured). For no internet but a reachable Mac, create a host-only network yourself (`container network create --internal <name>`) and name it here. A change is applied to an existing sandbox (it is replaced; volumes are kept) |
| `apple_container_keep_running` | `false` | `true` leaves a persistent sandbox running when Hermes is done with it, for a server you keep running inside. By default an idle sandbox is stopped (not deleted) and started again by the next command; see "Idle stop". Must be `true` or `false`; anything else stops the sandbox from starting |
| `apple_container_extra_args` | `[]` | extra flags for `container run`, one string per argument, placed right before the image. Validated, see "Rules for extra arguments" |
| `apple_container_binary` | *(found on PATH)* | path of the `container` CLI. Wins over the `HERMES_APPLE_CONTAINER_BINARY` variable; a path that is not an executable file means "not found" (no fallback to another binary) |

Shared container settings from Hermes also apply: `container_cpu` (default 1,
whole numbers), `container_memory` (MB, default 5120), `container_disk` (MB,
default 51200 — the size of each persistent volume) and
`container_persistent` (default `true`: one sandbox shared by every session
of the home; `false`: a fresh container per session, deleted when the session
closes, nothing carried over — sessions and sub-agent sessions are then
isolated from each other). `container_persistent` can also be set in the form
(field "One sandbox for all sessions"); a value saved there wins over the
core key.

### Rules for shares

The CLI is more permissive than it looks; these were measured on 1.2.2 and
the plugin refuses what would share more than you wrote. A refused setting
stops the sandbox from starting, with an error naming the entry.

- **Absolute paths only.** `./proj`, `../proj`, `proj/`, `.` are folders of
  the Mac to the CLI, resolved against whatever directory Hermes was started
  in. They are refused, not guessed at. `~/proj` is expanded. A bare name
  (`data:/data`) is a named volume and shares no Mac folder — but a named
  volume is a disk image and **cannot be attached to two running sandboxes at
  once**: with `container_persistent: false`, a second conversation starting
  while another one holds the volume fails with
  `The storage device attachment is invalid.` (measured; the start error says
  so). Share a Mac folder instead when several sandboxes run at the same
  time.
- **Only `:ro` and `:rw`** as suffix. The CLI mounts `:readonly`, `:RO` and
  any other suffix READ-WRITE without a word; they are refused.
- **Folders, not single files.** Under a single-file mount the runtime shares
  the file's whole parent folder with the VM (guest root with `CAP_SYS_ADMIN`
  can mount that share and read the siblings), and a folder shared together
  with one of its own files loses the folder mount. A source that is a file
  is refused: put the file in a folder of its own and share that folder.
- **One mount per path.** Two of your mounts on the same path in the sandbox
  are refused (the later would hide the earlier). A mount of yours on `/root`
  or `/workspace` replaces the backend's own volume there; it also wins over
  the launch directory and Hermes' file shares.
- An entry with an empty source or a relative target, an entry that is not a
  string, or a setting that is not a list is refused (the CLI would create an
  anonymous volume for `:/path`).
- Sharing a folder that is, contains or lies inside `~/.ssh`, `~/.aws`,
  `~/.gnupg`, `~/.kube`, `~/.docker`, `~/.config/gcloud`, the Keychains
  folder and the like, or the Hermes home, logs a warning.

### Rules for extra arguments

`apple_container_extra_args` is passed to `container run`, so it can undo the
isolation. The plugin reads it with a table of the CLI's flags:

- It must be a list of strings (`["--network", "none"]`, numbers quoted). A
  single string, a non-string entry or a control character is refused, not
  dropped or split.
- Refused: `--name`, `--rm`, `--entrypoint`, `-i`, `-t`, `--`, `--help`,
  `--version`, a label in the `hermes-` namespace, and a bare word (it would
  be taken as the image).
- A mount (`-v`, `--volume=`, `--mount`, also inside combined short flags
  like `-iv`) follows the rules for shares and counts as a host share for
  approvals; so does `--ssh`. Write absolute paths there (`~` is not
  expanded).
- `--network` there replaces `apple_container_network` (the CLI refuses
  `none` together with another network).
- A flag the table does not know is passed through, and approvals stay on.
- Flags that only tighten the sandbox (`--read-only`, `--cap-drop`,
  `--tmpfs`, `--network none`) keep approvals off.
- A change of the extra arguments is applied to an existing sandbox (it is
  replaced; volumes are kept).

### Credential files

With `credentials` in `apple_container_mount_hermes_files`, each credential
file registered by your skills (or listed in `terminal.credential_files`) is
copied to
`<HERMES_HOME>/plugin-data/apple_container/staged/<container>/<key>/<name>`
(folders `0700`, file `0600`, one folder per file) and that copy is mounted
read-only at the file's usual path under `/root/.hermes`. What the runtime
shares underneath is therefore a folder holding that one file, never the
Hermes home with its `.env` and `auth.json`. `/root/.hermes` itself stays
writable in the sandbox.

- The copies are refreshed at every sandbox start or attach (a token your
  skill refreshed on the Mac reaches a running sandbox at the next session,
  not instantly). They are rewritten in place: replacing a mounted file makes
  it vanish in a running guest.
- A file no longer registered loses its copy; turning the setting off removes
  them all; a session sandbox takes its copies with it; copies whose sandbox
  is gone are removed at the next start.
- A credential file that cannot be copied stops the sandbox from starting.

## Persistence and lifecycle

- `container_persistent: true` (default): two named volumes (an ext4 disk
  image each — no host folder) are mounted at `/root` and `/workspace`; the
  container is reused by name across Hermes restarts. Packages you install
  and files under `/root` and `/workspace` survive container recreation.
  An idle sandbox is **stopped, not deleted** (next section).
- `container_persistent: false`: every session (chat, gateway conversation,
  TUI session) gets its own fresh container, deleted when the session closes.
  Nothing carries over between sessions.
- An existing sandbox is reused only while it matches the settings: image,
  mounts (including read-only versus read-write), network and extra
  arguments. Otherwise it is replaced by a fresh one (volumes are kept; what
  you installed outside `/root` and `/workspace` is lost).
- A sandbox that was deleted or stopped behind Hermes' back is recreated or
  restarted by the next command.
- Removal (non-persistent sandboxes only): `container stop`, then `container
  delete`; if the VM will not stop, it is killed and deleted with `--force`,
  so cleanup does not leave it running.
- When several environments share one non-persistent container, it is
  removed by the last one that uses it (each holds a lease file under
  `plugin-data/apple_container/leases/`).
- A leftover container from a crashed Hermes is cleaned up at the next
  sandbox start in the same Hermes home and profile: session containers whose
  owner process is gone and stopped non-persistent shared containers older
  than 15 minutes are removed; a persistent sandbox that is still running is
  stopped. Ours only, by label, never one a living process still uses, never
  another home's. Persistent containers are never deleted.
- Volumes are never deleted automatically; remove them with
  `container volume rm hermes-acv-…` when you want the state gone.

### Idle stop

A running sandbox holds its memory (`container_memory`, 5 GB by default). A
persistent sandbox is therefore stopped with `container stop` when Hermes is
done with it and no other living Hermes process uses it.

- **When.** Hermes tells the backend it is done with a sandbox after
  `terminal.lifetime_seconds` without activity (default 300; its idle check
  runs once a minute) and when the Hermes process exits; both were observed
  with real Hermes turns. Its session teardown calls the same cleanup. Per
  its code (not tested here), a sandbox is not idle while a command is in
  flight or a background process Hermes tracks is running.
- **What a stop keeps.** The container and its volumes: files anywhere in the
  sandbox (also outside `/root` and `/workspace`, for example packages
  installed with `apt`), and the settings it was made with.
- **What a stop loses.** Every process running in the sandbox (a server
  started with `nohup … &`, a `tmux` session) and what is in `/dev/shm`.
- **The next command** starts the sandbox again and runs; the agent sees no
  error. Measured: about 0.5 s for `container start`, 0.7 to 0.9 s until a
  new environment has run its first command; through real Hermes turns, a
  turn that had to start the sandbox took 2.2 s against 1.4 s for the same
  kind of turn on a running one. The same happens when the sandbox was
  stopped underneath a living session (the command is run once: the CLI
  refuses a command on a stopped container before running any of it).
- **Shared use.** One persistent sandbox serves every session of a Hermes
  home (one per workspace with `apple_container_mount_cwd`). Every
  environment that uses it holds a lease file
  (`plugin-data/apple_container/leases/<container>/<pid>-<token>`); the
  sandbox is stopped only when no lease of a living process is left. The
  check and the stop happen under a lock per sandbox
  (`plugin-data/apple_container/locks/`), which an environment that attaches
  also takes, so one that arrives during a stop waits and then starts the
  sandbox.
- **After a crash.** A Hermes process that was killed leaves its sandbox
  running; nothing on the Mac watches for that. The next time a Hermes
  process of the same home and profile starts a sandbox, it stops (never
  deletes) persistent sandboxes of that home that no living process holds.
  The one it is about to use is taken over as it is.
- **Opt-out.** `apple_container_keep_running: true` restores the earlier
  behaviour: the sandbox keeps running after cleanup and no leftover is
  stopped at start. Stop it yourself with `container stop hermes-ac-…` when
  you want its memory back.
- A lease belongs to a process id. If a crashed Hermes' process id has been
  given to another program by the time of the next start, the leftover is
  taken to be in use and left running until that program ends.

## What it protects against — and what it does not

**Protected:** the agent's commands and file tools run inside a Linux VM with
its own filesystem. Nothing on your Mac is reachable unless you shared it, and
shared folders can be read-only (`:ro` is enforced by virtiofs). No host
environment, SSH agent or ports leak in by default. Dangerous-command
approval prompts stay ON whenever anything of the Mac is shared (they are
skipped when nothing is shared, matching the Docker backend's behaviour).

**Not protected (be aware):**

- **Network (by default)**: the sandbox has NAT network access, and your Mac
  is reachable from the VM at the network's gateway address (192.168.64.1 by
  default). A model-authored command can talk to services on your Mac or on
  the internet. To cut that: `apple_container_network: none` gives a sandbox
  with no network at all — only the loopback interface, no route (measured on
  the real runtime; the model itself runs in Hermes on the Mac and is not
  affected, but `pip install`, `git clone` and the like fail in the sandbox).
  In between, an internal network (`container network create --internal …`,
  named in `apple_container_network`) has no internet egress but your Mac is
  still reachable on it.
- **Shared folders, read-write**: anything the agent does there is real.
  Files written in a share belong to your Mac user on the Mac, whether the
  guest process was root or not (measured with both).
- A timed-out command is stopped in the guest (an in-guest `timeout`
  backstop plus a targeted `pkill`), but a command whose process tree
  detaches into its own session could in principle outlive a kill on images
  without `timeout`; the default image has GNU `timeout`.
- If you turn on `apple_container_mount_hermes_files`, those files are
  readable in the sandbox, and with network access a command can send them
  elsewhere. `credentials` is the sensitive one; share it only if a skill
  needs it. Approval prompts stay on while any of them is shared.
- An idle persistent sandbox is stopped, which ends whatever was left
  running inside it; set `apple_container_keep_running: true` if you keep a
  server there. With that setting, or while a session is active, the sandbox
  holds its memory (`container_memory`).
- Concurrent commands share one container (like the Docker backend); heavy
  parallel use was not stress-tested.

## Changes in 0.4.0 that you may notice

- `container_persistent` ("One sandbox for all sessions") can now be set in
  the Desktop form. A value saved there wins over Hermes' own
  `terminal.container_persistent` key; a value that is not `true` or `false`
  is refused. There is no `terminal.apple_container_container_persistent`
  key — write the core key or use the form.

## Changes in 0.3.0 that you may notice

- **Settings have a form in Hermes Desktop**, stored in the plugin's own
  settings. The `terminal.apple_container_*` keys keep working; see "Where a
  setting is written" for which place wins and what the form does not show.
- **Settings that used to be skipped now stop the sandbox from starting**,
  with an error that names the key: a `terminal.apple_container_*` key the
  backend does not have (a misspelt key was ignored, so a misspelt
  `apple_container_network: none` left the network on); a key with no value
  (what `hermes config set terminal.apple_container_network none` writes: it
  was read as "not set", and the network stayed on); and an
  `apple_container_mount_cwd` that is not `true` or `false` (the text
  `"false"` used to share the launch directory).
- `hermes doctor` has two more lines: the settings error, if there is one,
  and what of the Mac is shared and where that is written.
- **A persistent sandbox no longer keeps running when Hermes is done with
  it.** It is stopped (kept, with its volumes) and started again by the next
  command, which then takes about a second longer. Processes you left
  running inside are gone after a stop. `apple_container_keep_running: true`
  brings the earlier behaviour back.
- At its first sandbox start, a Hermes process stops running persistent
  sandboxes of its home that no living Hermes process holds. A Hermes still
  running plugin 0.2.0 holds no lease on its persistent sandbox: restart it
  after updating, or its sandbox is stopped under it by a newer process (its
  next command starts it again).
- Fixed: with several environments sharing one non-persistent container, a
  cleanup running while another Hermes home was in effect (one gateway
  serving several profiles) looked for the other users in the wrong home and
  could delete the container they were using.

## Changes in 0.2.0 that you may notice

Settings that used to be accepted and now stop the sandbox from starting,
with an error that names the entry:

- a volume with a relative source (`./d`, `d/`, `.`), an empty source or a
  relative target; a suffix other than `:ro` / `:rw`; a source that is a
  single file; two mounts on one path; a `apple_container_volumes` value that
  is not a list of strings (such entries used to be skipped, or — relative
  sources — mounted read-write while approvals were off);
- `apple_container_extra_args` that is not a list of strings, or contains
  `--name`, `--rm`, `--entrypoint`, `-i`, `-t`, `--`, a `hermes-` label or a
  bare word.

Other changes: approval prompts now also stay on for mounts and `--ssh` in
the extra arguments and for `~` sources; a sandbox is no longer rebuilt at
every new session because of the image name, and is rebuilt when the network
or extra arguments change; `container run` gets `--init --entrypoint env`
(existing sandboxes pick this up when they are next replaced); credential
files are staged; `apple_container_forward_env` given as a string is ignored
(it used to forward one-letter names) and falls back to the Hermes `.env`.

## What was checked, and with which versions

Verified against the **real runtime** (macOS 26.7.1, Apple silicon,
`container` CLI 1.2.2, Hermes v0.23.1 at commit 3b4215785):

- The facts the rules are built on: how `-v` reads relative, `~`, empty and
  suffixed specs; what `inspect` reports for images, mounts and networks; the
  error texts for missing and stopped containers; `kill` and `delete --force`;
  `--init --entrypoint env` on an image with its own ENTRYPOINT; the
  single-file share behaviours; how a running guest sees a rewritten versus a
  replaced file.
- `container run IMAGE args` keeps the image ENTRYPOINT and passes args as
  CMD (Docker semantics) — which is why the backend sets the entrypoint.

Checked only against a scripted stand-in for the CLI (not the real runtime):

- The exact argv of every CLI call (labels, mounts, resources, ports).
- Cleanup when `container stop` fails (the fallback's commands were each run
  by hand on the real CLI; a real VM that refuses to stop was not produced).
- An environment attaching while another process is in the middle of
  stopping the sandbox (the lock).
- Refresh of a staged credential while a real sandbox runs (measured by hand
  on the CLI, see above; not in a script).
- Images *without* GNU timeout (the pkill-fallback path).
- The platform gate on a non-Mac (mocked).

Not checked:

- The form as drawn by the Hermes Desktop app. What was run is the gateway
  method the app calls (`plugins.manage`: the list with this plugin's 12
  fields, and a save), and the app's source was read (it renders `str` as a
  text field, `bool` as a switch, `list` and `dict` as a JSON text area, and
  its filter for that page keeps a plugin installed by the user). The app
  itself was not opened.
- A change saved in the form while a sandbox is in use: that the sandbox
  follows at the next environment is what the code does (settings are read
  when an environment is created); only the approval decision following at
  once is tested.
- An internal network (`container network create --internal`): that it has
  no internet egress but reaches the Mac is what the CLI's help says
  ("Restrict to host-only network"), not measured here.
- A default registry other than `docker.io` configured in the CLI.
- Bot Desktop / `computer_use` / the browser actually placing their screen
  inside the sandbox (`supports_streams` + `exec_prefix()` are implemented
  and unit-tested; a real Bot Desktop session was not run).
- Concurrent/parallel commands under load; `container cp`; Rosetta.
- Intel Macs, other macOS versions, other `container` CLI versions.
- The `hermes plugins install file://…` path was validated with
  `hermes plugins validate` but a packaged install was not round-tripped.

## License

MIT — see LICENSE (Copyright © 2026 Emilian Bold).
