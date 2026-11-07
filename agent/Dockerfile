# The image of the runs: unchanged Claude Code in a slim Node image.
# The container runs with the UID of the service, so no user of its own is created here.
FROM node:26-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates git ripgrep \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g @anthropic-ai/claude-code \
 && npm cache clean --force

ENV DISABLE_AUTOUPDATER=1 \
    CLAUDE_CODE_DISABLE_AUTO_MEMORY=1 \
    USE_BUILTIN_RIPGREP=0

WORKDIR /company
