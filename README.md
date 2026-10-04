# Valyzen SDK

Client libraries for the [Valyzen Negotiation Gateway](https://www.valyzen.ai): open a
negotiation, exchange offers, and verify the signed receipt offline.

| Language | Package | Install |
|---|---|---|
| Python | [`valyzen`](https://pypi.org/project/valyzen/) | `pip install valyzen` |
| TypeScript / JavaScript | [`@valyzen/sdk`](https://www.npmjs.com/package/@valyzen/sdk) | `npm install @valyzen/sdk` |

**Status: name reservation.** Version 0.0.1 contains no client yet; the first usable
release (with `valyzen try`) follows shortly.

## Releasing

Releases publish from `.github/workflows/release.yml` through PyPI and npm trusted
publishing (GitHub OIDC). No registry tokens are stored in this repository.

## License

Apache-2.0 — see [LICENSE](LICENSE).
