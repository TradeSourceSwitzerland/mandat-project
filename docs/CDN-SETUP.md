# CDN Setup – Cloudflare R2

## Overview

All video assets are hosted on **Cloudflare R2** and are no longer stored in this repository. This reduces the repository size and improves CI/CD performance.

## Cloudflare R2 Bucket

| Property | Value |
|----------|-------|
| Provider | Cloudflare R2 |
| Public bucket URL | `https://pub-ff9f401ec1c64d3eacc311260b62049a.r2.dev` |

## Video URLs

| File | CDN URL |
|------|---------|
| Cinematic Video | `https://pub-ff9f401ec1c64d3eacc311260b62049a.r2.dev/Cinematic%20Video%20Final.mp4` |
| Demo Video | `https://pub-ff9f401ec1c64d3eacc311260b62049a.r2.dev/Demo%20Video%20Final.mov` |

## Usage in Webflow

The videos are embedded directly in the Webflow frontend using the CDN URLs above. No backend or server-side handling is required.

## PageSpeed Improvements

Removing the video files from Git and serving them via CDN provides the following benefits:

- Faster repository clones and CI/CD pipelines (~8 MB smaller repository)
- Videos are served from Cloudflare's global edge network, reducing latency
- No large binary files in Git history going forward

## Adding New Videos

1. Upload the video to the Cloudflare R2 bucket.
2. Copy the public URL and reference it directly in Webflow.
3. Do **not** commit video files to this repository (`.gitignore` enforces this).
