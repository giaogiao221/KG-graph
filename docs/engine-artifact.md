# Engine deployment artifact

This curated release includes readable extraction engine source under `engine/` and the existing `engine-artifact.tar.gz` used by the unchanged backend Dockerfile. The artifact contains compiled engine modules, runtime configuration and engine tests; it is not a substitute for the included source code.

To regenerate the artifact after modifying source, use Bash and Docker from the repository root:

```console
bash tools/build-engine-artifact.sh engine engine-artifact.tar.gz
```

Docker is recommended because the bytecode must match the backend image's Python 3.12 interpreter. Local standalone engine instructions currently specify Python 3.11.

The artifact is included in this release directory so a downloader does not need a private engine package. The source tree and artifact have not been rebuilt or changed during export; their original application behavior is preserved.

The adapter layout remains defined by `deploy/docker-compose.yml`: `LEGACY_ENGINE_SOURCE_ROOT=/legacy-engine`, `LEGACY_ENGINE_SUBDIR=.`, and `LEGACY_ENGINE_CONFIG_SUBDIR=src/config`.

Real model credentials and uploaded source documents must never be added to an artifact. If rebuilding from an engine directory that has been used for extraction, first use a clean copy without `config.env`, books, generated runs or caches; the existing build helper does not filter every category of local runtime data.
