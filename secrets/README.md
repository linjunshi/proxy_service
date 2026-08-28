# Runtime secrets

`vnc_password.txt` must exist in this directory before starting the stack —
Compose refuses to start when a secret's source file is missing. Its contents
decide whether the GUI asks for a password:

- Exactly eight printable ASCII characters: VNC authentication is on. Anything
  else non-empty fails the boot, so a typo can never downgrade to no password.
- Empty, or nothing but whitespace: authentication is off and the container
  logs a warning at every boot.

VNC authentication only protects the host-local display endpoint, and with an
empty file there is no protection beyond the binding itself. The GUI port must
remain bound to `127.0.0.1`; do not expose it to a LAN or the Internet.

This file is intentionally ignored by Git.
