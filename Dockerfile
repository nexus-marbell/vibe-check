FROM python:3.12-slim

# System dependencies: git for cloning, nodejs for pyright
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        git \
        curl \
        ca-certificates \
        gnupg && \
    mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Python analysis tools
RUN pip install --no-cache-dir \
    pyscn \
    deepcsim \
    wily \
    lizard \
    radon \
    ruff

# Pyright (Node.js-based type checker) + TypeScript analysis tools
RUN npm install -g \
    pyright \
    eslint \
    typescript \
    jscpd \
    @typescript-eslint/parser \
    @typescript-eslint/eslint-plugin

# Create workspace directory for cloned repos
RUN mkdir /workspace

COPY .eslintrc.json /app/.eslintrc.json
COPY vibe_check.py /app/vibe_check.py

WORKDIR /app

ENTRYPOINT ["python3", "vibe_check.py"]
