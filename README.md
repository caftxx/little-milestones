# Little Milestones

[![Tests](https://github.com/caftxx/little-milestones/actions/workflows/tests.yml/badge.svg)](https://github.com/caftxx/little-milestones/actions/workflows/tests.yml)

Support using local AI vision models to analyze photos, extract people-related information, generate monthly reports, and process photos from Immich albums.

## Setup

Install dependencies:

```bash
uv sync --extra dev
```

Prepare a provider config file such as `providers.json`:

```json
{
  "providers": [
    {
      "name": "vision-a",
      "base_url": "http://127.0.0.1:1234/v1",
      "api_key": "local",
      "vision_model": "your-local-vision-model",
      "max_inflight": 2
    }
  ]
}
```

If you use Immich, export the API key first:

```bash
export IMMICH_API_KEY=your-immich-api-key
```

## local

Describe photos in a local directory:

```bash
uv run littlems local describe \
  --input ./photos \
  --output ./descriptions.json \
  --provider-config ./providers.json
```

Generate a monthly report from local photos:

```bash
uv run littlems local report \
  --input ./photos \
  --from 2026-03-01 \
  --to 2026-03-31 \
  --birth-date 2025-12-20 \
  --baby-name Xiaoman \
  --output ./report.md \
  --provider-config ./providers.json
```

## immich

Describe photos from an Immich album:

```bash
uv run littlems immich describe \
  --album-name "Baby 2026-03" \
  --output ./immich-descriptions.json \
  --immich-url http://immich.lan/api \
  --provider-config ./providers.json
```

Generate a monthly report from an Immich album:

```bash
uv run littlems immich report \
  --album-name "Baby 2026-03" \
  --birth-date 2025-12-20 \
  --baby-name Xiaoman \
  --output ./report.md \
  --immich-url http://immich.lan/api \
  --provider-config ./providers.json
```
