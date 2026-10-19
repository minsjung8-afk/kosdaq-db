#!/bin/sh
# 최초 1회: npm install -g wrangler && wrangler login
cd "$(dirname "$0")" && python3 run.py site && wrangler pages deploy site --project-name kosdaq-db --commit-dirty=true
