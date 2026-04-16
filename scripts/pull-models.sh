#!/bin/sh
# ============================================================
# Pull default Ollama models
# Designed to run inside the ollama/ollama container.
# Uses only the `ollama` CLI — no curl, no python3, no bash-isms.
# ============================================================

set -e

OLLAMA_HOST="${OLLAMA_HOST:-http://finhouse-ollama:11434}"

echo "=== FinHouse model puller ==="
echo "OLLAMA_HOST: $OLLAMA_HOST"

# Wait for Ollama server to respond to `ollama list`
echo "Waiting for Ollama server..."
MAX_WAIT=300
ELAPSED=0
until ollama list >/dev/null 2>&1; do
    if [ "$ELAPSED" -ge "$MAX_WAIT" ]; then
        echo "ERROR: Ollama did not become ready after ${MAX_WAIT}s"
        exit 1
    fi
    sleep 5
    ELAPSED=$((ELAPSED + 5))
    if [ $((ELAPSED % 30)) -eq 0 ]; then
        echo "  ...still waiting (${ELAPSED}s)"
    fi
done
echo "Ollama server is ready."

# Models to pull
MODELS="qwen2.5:14b llama3.1:8b"

for model in $MODELS; do
    echo ""
    echo "=== Pulling $model ==="

    # Skip if already present
    if ollama list | awk '{print $1}' | grep -qx "$model"; then
        echo "  already present, skipping"
        continue
    fi

    # ollama pull streams progress to stderr automatically
    if ollama pull "$model"; then
        echo "  ✓ $model pulled successfully"
    else
        echo "  ✗ Failed to pull $model (will continue with other models)"
    fi
done

echo ""
echo "=== All pull operations complete ==="
echo "Available models:"
ollama list