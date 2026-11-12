import json
import requests
import time
import uuid
import random
import os
import copy

# ================= 配置区域 =================
COMFYUI_SERVER = "127.0.0.1:8188"
WORKFLOW_FILE = "moss_workflow_api.json"

# 👇 你要保存图片的绝对路径（比如你的桌面或者D盘）
# 注意：路径里的斜杠建议用双斜杠 \\ 或者正斜杠 /
CUSTOM_SAVE_PATH = r"C:\Users\takanashikotori\Desktop\code\py\ai\lt\generated_jpgs"

# 节点 ID 配置
NODE_ID_PROMPT = "4"    # 正向提示词节点
NODE_ID_SEED = "7"      # K采样器节点
NODE_ID_SAVE = "30"     # 你的新保存节点 ID
# ============================================

def load_workflow():
    if not os.path.exists(WORKFLOW_FILE):
        raise FileNotFoundError(f"❌ 找不到工作流文件: {WORKFLOW_FILE}")
    with open(WORKFLOW_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def queue_prompt(workflow):
    prompt_id = str(uuid.uuid4())
    payload = {"prompt": workflow, "client_id": prompt_id}
    req = requests.post(f"http://{COMFYUI_SERVER}/prompt", json=payload)
    if req.status_code != 200:
        raise Exception(f"提交工作流失败: {req.text}")
    return prompt_id

def wait_for_completion(prompt_id, timeout=300):
    start_time = time.time()
    while time.time() - start_time < timeout:
        res = requests.get(f"http://{COMFYUI_SERVER}/history/{prompt_id}")
        if res.status_code == 200:
            history = res.json()
            if prompt_id in history:
                return history[prompt_id]
        time.sleep(2)
    raise TimeoutError("❌ 生成超时！请检查 ComfyUI 后台是否有报错。")

def generate_image(prompt):
    # 1. 读取工作流
    original_workflow = load_workflow()
    workflow = copy.deepcopy(original_workflow)
    
    # 2. 注入提示词和随机种子
    if NODE_ID_PROMPT in workflow:
        workflow[NODE_ID_PROMPT]["inputs"]["text"] = prompt
    if NODE_ID_SEED in workflow:
        workflow[NODE_ID_SEED]["inputs"]["seed"] = random.randint(0, 999999999999999)

    # 3. 设置自定义保存路径
    if NODE_ID_SAVE in workflow:
        # 确保路径是存在的，ComfyUI 遇到不存在的路径可能会报错
        os.makedirs(CUSTOM_SAVE_PATH, exist_ok=True)
        workflow[NODE_ID_SAVE]["inputs"]["custom_path"] = CUSTOM_SAVE_PATH

    # 4. 提交任务并等待
    print(f"\n🚀 正在提交任务... 提示词: {prompt[:30]}...")
    try:
        prompt_id = queue_prompt(workflow)
        print("⏳ 等待 ComfyUI 生成中...")
        wait_for_completion(prompt_id)
        
        print(f"✅ 生成成功！请前往该目录查看：{CUSTOM_SAVE_PATH}")
    except Exception as e:
        print(f"❌ 发生错误: {e}")


if __name__ == "__main__":
    prompt = "A beautiful landscape with a river, cinematic lighting, masterpiece, best quality."
    generate_image(prompt)