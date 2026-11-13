# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project follows [Semantic Versioning](https://semver.org/).

## [0.1.0] - Unreleased

First public release.

- `ET43` client for EastTester ET43x LCR meters over a serial port
  (`pyserial`), with auto-discovery and exclusive port locking on POSIX.
- Typed settings: `mode`, `secondary`, `equivalent`, `frequency`, `level_mv`,
  `bias_mv`, `speed`, `measurement_range`, `relative_mode`, `display_page`.
- `fetch()` returning a `Reading(primary, secondary)` named tuple.
- `auto_remote()` context manager that always returns the instrument to local
  mode and closes the port.
- `calibrate()` for open/short correction.
