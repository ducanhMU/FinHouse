#!/bin/bash
# ============================================================
# Pull default Ollama models
# Run after: docker compose up -d finhouse-ollama
# ============================================================

set -e

OLLAMA_HOST="${OLLAMA_HOST:-http://localhost:21434}"

echo "⏳ Waiting for Ollama to be ready..."
until curl -sf "$OLLAMA_HOST/" > /dev/null 2>&1; do
    sleep 2
done
echo "✅ Ollama is ready"

MODELS=("qwen2.5:14b" "llama3.1:8b")

# Fetch the list of already-downloaded models once
EXISTING=$(curl -s "$OLLAMA_HOST/api/tags")

for model in "${MODELS[@]}"; do
    echo ""
    already=$(echo "$EXISTING" | python3 -c "
import sys, json
data = json.load(sys.stdin)
names = [m['name'] for m in data.get('models', [])]
print('yes' if '$model' in names else 'no')
" 2>/dev/null || echo "no")

    if [ "$already" = "yes" ]; then
        echo "✅ $model already present, skipping"
        continue
    fi

    echo "📦 Pulling $model ..."
    curl -s "$OLLAMA_HOST/api/pull" -d "{\"name\": \"$model\"}" | while read -r line; do
        status=$(echo "$line" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status',''))" 2>/dev/null || echo "$line")
        echo "   $status"
    done
    echo "✅ $model pulled"
done

echo ""
echo "🎉 All models ready!"
echo "Available models:"
curl -s "$OLLAMA_HOST/api/tags" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for m in data.get('models', []):
    print(f\"  - {m['name']} ({m.get('size',0)//1024//1024//1024}GB)\")
"
