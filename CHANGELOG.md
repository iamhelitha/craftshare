# Changelog

## [0.3.1](https://github.com/iamhelitha/craftshare/compare/v0.3.0...v0.3.1) (2026-10-18)


### Bug Fixes

* **hub:** run sshd -t by absolute path, create /run/sshd and show the real error when it fails ([4623c10](https://github.com/iamhelitha/craftshare/commit/4623c1024965382818991cbcd61d4ab63dc4fee5))
* **tests:** update admin token in test_hub.py to use a more secure test secret ([f33030b](https://github.com/iamhelitha/craftshare/commit/f33030b2860436239ad7b65c960354277b6a6657))
* **tests:** use one ADMIN constant in test_hub.py (rename was half applied) ([b52a359](https://github.com/iamhelitha/craftshare/commit/b52a359a4ccaf0403499e038ce559c2f2fd9d9bf))

## [0.3.0](https://github.com/iamhelitha/craftshare/compare/v0.2.0...v0.3.0) (2026-10-18)


### Features

* **hub:** mc-hub admin command, HTTPS with pinned certificate, safe updates and full uninstall ([8e7fd7a](https://github.com/iamhelitha/craftshare/commit/8e7fd7a9f75931482eac3c26d91067bbeeb5ac58))
* **hub:** rate limits against brute force and floods; installer asks before changing the firewall ([8c89fa4](https://github.com/iamhelitha/craftshare/commit/8c89fa4a1788993bf34578367bac93fd6119045c))
* **manager:** in-app updates with checksum, test start, restart and automatic rollback ([a6107e6](https://github.com/iamhelitha/craftshare/commit/a6107e6cf96700fcfb4829c813a3848454832901))

## [0.2.0](https://github.com/iamhelitha/craftshare/compare/v0.1.0...v0.2.0) (2026-10-18)


### Features

* monorepo layout, GPL-3.0, release-please versioning and exe release builds ([55bc0e0](https://github.com/iamhelitha/craftshare/commit/55bc0e0ab0229d0efca569aa0f9cfb52ad4709ee))


### Bug Fixes

* clearer checksum check in install.sh (shellcheck) ([3122c6b](https://github.com/iamhelitha/craftshare/commit/3122c6be6b7ca1a1ae172e9236e81bf068c0de47))
