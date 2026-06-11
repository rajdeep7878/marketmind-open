# Audio fixtures

This directory holds binary audio fixtures used by opt-in
transcription integration tests (marked `@pytest.mark.integration`).
Files are not generated automatically by CI — generate them locally
before running `uv run pytest -m integration` if you need them.

## silence_10s.m4a

Ten seconds of digital silence, AAC-in-MP4 (`.m4a`). Generate with:

```sh
ffmpeg -f lavfi -i anullsrc=channel_layout=stereo:sample_rate=44100 \
  -t 10 -c:a aac -b:a 64k -y tests/fixtures/audio/silence_10s.m4a
```

Requires `ffmpeg` on `PATH` (`brew install ffmpeg`). The file is
intentionally **not** committed because we can't reliably regenerate
it from CI without bundling a binary; the integration test that uses
it skips with a clear message when it's missing.
