# Security Policy

## Scope

`comms` is a local-first coordination tool. The CLI reads and appends to a
per-machine JSONL log under a file lock. The optional `comms ui` dashboard binds
an HTTP server to **`127.0.0.1` only** (loopback), with same-origin guards on its
state-mutating endpoints. It is not designed to be exposed on a public interface;
do not bind it to a routable address.

## Reporting a vulnerability

Please report security issues **privately** — do not open a public issue for a
suspected vulnerability.

- Use GitHub's private vulnerability reporting: the **Security** tab →
  **Report a vulnerability** on this repository.

Include a description, affected version/commit, and reproduction steps. We aim to
acknowledge reports within a few days and will coordinate a fix and disclosure
timeline with you.

## Supported versions

This project is pre-1.0; fixes land on the latest `main`. Please reproduce
against a recent build before reporting.
