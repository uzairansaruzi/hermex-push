# hermex-push

Push notifications for [Hermex](https://github.com/uzairansaruzi/hermex), the iPhone client for a self-hosted Hermes agent. Two pieces, one repo:

- `plugin/` — a `hermes-agent` platform plugin (`hermex`). On each turn boundary it posts a sealed, content-free-by-default event to the relay. Previews are encrypted on the host with AES-256-GCM; plaintext never leaves the machine. Install identifier: `https://github.com/uzairansaruzi/hermex-push.git/plugin`.
- `relay/` — an open-source relay on a Cloudflare Worker with KV. It fans events out to paired iPhones over APNs and stores devices under a hash of the install key, never the key or any plaintext. Self-hostable; Uzair runs the default instance.

Design and decisions live in the Hermex issue tracker: the push epic is [hermex#490](https://github.com/uzairansaruzi/hermex/issues/490). The plugin is [hermex#555](https://github.com/uzairansaruzi/hermex/issues/555) and the relay is [hermex#556](https://github.com/uzairansaruzi/hermex/issues/556).

MIT licensed.
