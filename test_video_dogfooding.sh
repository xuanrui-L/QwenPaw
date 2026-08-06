#!/bin/bash
# DogFooding Proxy 视频生成测试脚本
# 使用 Base64 编码的参考图（通过临时文件发送，避免命令行参数过长）

API_KEY="sk-as-d_yF3BuykWXmi-HPuHLFx_B34P1CnEP0kv7NfJUyMBw"
IMAGE_B64_FILE="/Users/saint-yin/projects/CloudPaw/QwenPaw/storyboard.b64"
IMAGE_B64=$(cat "$IMAGE_B64_FILE")
IMAGE_DATA_URL="data:image/png;base64,${IMAGE_B64}"

PROMPT_WAN="8s fairy tale video, Disney style, forest. Hunter in brown armor walks with princess in blue-yellow dress. He raises bow then hesitates with guilt. She pleads with tears. He lowers bow and points her to safety. Warm golden forest light."

PROMPT_HH="8s fairy tale video, Disney style, forest. Hunter in brown armor walks with princess in blue-yellow dress. He raises bow then hesitates with guilt. She pleads with tears. He lowers bow and points her to safety. Warm golden forest light."

# ============================================================
# 1. wan3.0-video-DogFooding
# ============================================================
cat > /tmp/wan_payload.json <<EOF
{
  "model": "wan3.0-video-DogFooding",
  "input": {
    "prompt": "${PROMPT_WAN}",
    "media": [
      {
        "type": "reference_image",
        "url": "${IMAGE_DATA_URL}"
      }
    ]
  },
  "parameters": {
    "resolution": "720P",
    "duration": 8
  }
}
EOF

echo "=== wan3.0-video-DogFooding ==="
curl -sS -X POST "http://proxy.agentscope.design/v1/video/generations/jobs" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d @/tmp/wan_payload.json

echo ""

# ============================================================
# 2. happyhorse-1.1-r2v-DogFooding
# ============================================================
cat > /tmp/hh_payload.json <<EOF
{
  "model": "happyhorse-1.1-r2v-DogFooding",
  "input": {
    "prompt": "${PROMPT_HH}",
    "media": [
      {
        "type": "reference_image",
        "url": "${IMAGE_DATA_URL}"
      }
    ]
  },
  "parameters": {
    "resolution": "720P",
    "ratio": "16:9",
    "duration": 8
  }
}
EOF

echo "=== happyhorse-1.1-r2v-DogFooding ==="
curl -sS -X POST "http://proxy.agentscope.design/v1/video/generations/jobs" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d @/tmp/hh_payload.json
echo ""
echo "Done."
