import json
import requests
import time
import uuid

# 1. 加载 API 格式的工作流
with open('workflow_api.json', 'r', encoding='utf-8') as f:
    workflow = json.load(f)

# 2. 动态修改工作流中的参数（例如：修改提示词）
# 假设你的提示词节点 ID 是 "6"，字段是 "text"
workflow["6"]["inputs"]["text"] = "a majestic dragon, fantasy art"

# 3. 构建请求
server_address = "127.0.0.1:8188"
prompt_id = str(uuid.uuid4())
payload = {"prompt": workflow, "client_id": prompt_id}

# 4. 发送生成请求
response = requests.post(f"http://{server_address}/prompt", json=payload)
print(f"任务已提交，ID: {prompt_id}")

# 5. 轮询检查任务状态（可选，用于等待完成）
while True:
    history_resp = requests.get(f"http://{server_address}/history/{prompt_id}")
    if history_resp.status_code == 200 and history_resp.json():
        print("✅ 图片生成完成！")
        # 图片通常保存在 ComfyUI 的 output 目录下
        break
    time.sleep(2)