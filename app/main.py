# =================================================================
#       
# =================================================================
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
@@ -25,14 +28,14 @@
import sys
import pathlib
import os

# 设置模板目录
BASE_DIR = pathlib.Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(limit="50M")

# --------------- CORS 中间件 (已修改，强制开启) ---------------
# <--- 关键修复 1: 我们不再依赖环境变量，直接写死，确保它一定生效。
# --------------- CORS 中间件 (强制开启) ---------------
app.add_middleware(
CORSMiddleware,
allow_origins=["*"],
@@ -43,63 +46,43 @@

# --------------- 全局实例 ---------------
load_settings()
# 初始化API密钥管理器
key_manager = APIKeyManager()

# 创建全局缓存字典，将作为缓存管理器的内部存储
response_cache = {}

# 初始化缓存管理器，使用全局字典作为存储
response_cache_manager = ResponseCacheManager(
expiry_time=settings.CACHE_EXPIRY_TIME,
max_entries=settings.MAX_CACHE_ENTRIES,
cache_dict=response_cache
)

# 活跃请求池 - 将作为活跃请求管理器的内部存储
active_requests_pool = {}

# 初始化活跃请求管理器
active_requests_manager = ActiveRequestsManager(requests_pool=active_requests_pool)

SKIP_CHECK_API_KEY = os.environ.get("SKIP_CHECK_API_KEY", "").lower() == "true"

# --------------- 工具函数 ---------------
async def check_remaining_keys_async(keys_to_check: list, initial_invalid_keys: list):
    """
    在后台异步检查剩余的 API 密钥。
    """
local_invalid_keys = []
    found_valid_keys =False

    found_valid_keys = False
log('info', f" 开始在后台检查剩余 API Key 是否有效")
for key in keys_to_check:
is_valid = await test_api_key(key)
if is_valid:
            if key not in key_manager.api_keys: # 避免重复添加
            if key not in key_manager.api_keys:
key_manager.api_keys.append(key)
found_valid_keys = True
else:
local_invalid_keys.append(key)
log('warning', f" API Key {key[:8]}... 无效")
        
        await asyncio.sleep(0.05) # 短暂休眠，避免请求过于密集

        await asyncio.sleep(0.05)
if found_valid_keys:
        key_manager._reset_key_stack() # 如果找到新的有效key，重置栈

        key_manager._reset_key_stack()
combined_invalid_keys = list(set(initial_invalid_keys + local_invalid_keys))
current_invalid_keys_str = settings.INVALID_API_KEYS or ""
current_invalid_keys_set = set(k.strip() for k in current_invalid_keys_str.split(',') if k.strip())
new_invalid_keys_set = current_invalid_keys_set.union(set(combined_invalid_keys))

if new_invalid_keys_set != current_invalid_keys_set:
settings.INVALID_API_KEYS = ','.join(sorted(list(new_invalid_keys_set)))
save_settings()

log('info', f"密钥检查任务完成。当前总可用密钥数量: {len(key_manager.api_keys)}")

# 设置全局异常处理
sys.excepthook = handle_exception

# --------------- 事件处理 ---------------
@@ -108,20 +91,16 @@ async def startup_event():
load_settings()
import app.vertex.config as vertex_config
vertex_config.reload_config()
    
credential_manager_instance = CredentialManager()
app.state.credential_manager = credential_manager_instance
    
await init_vertex_ai(credential_manager=credential_manager_instance)
schedule_cache_cleanup(response_cache_manager, active_requests_manager)
await check_version()
    
initial_keys = key_manager.api_keys.copy()
key_manager.api_keys = []
first_valid_key = None
initial_invalid_keys = []
keys_to_check_later = []

for index, key in enumerate(initial_keys):
is_valid = await test_api_key(key)
if is_valid:
@@ -134,7 +113,6 @@ async def startup_event():
else:
log('warning', f"密钥 {key[:8]}... 无效")
initial_invalid_keys.append(key)
    
if not first_valid_key:
log('error', "启动时未能找到任何有效 API 密钥！")
keys_to_check_later = []
@@ -145,7 +123,6 @@ async def startup_event():
log('info', f"使用密钥 {first_valid_key[:8]}... 加载可用模型成功")
except Exception as e:
log('warning', f"使用密钥 {first_valid_key[:8]}... 加载可用模型失败",extra={'error_message': str(e)})

if not SKIP_CHECK_API_KEY:
if keys_to_check_later:
asyncio.create_task(check_remaining_keys_async(keys_to_check_later, initial_invalid_keys))
@@ -157,31 +134,16 @@ async def startup_event():
settings.INVALID_API_KEYS = ','.join(sorted(list(new_invalid_keys_set)))
save_settings()
log('info', f"更新初始无效密钥列表完成，总无效密钥数: {len(new_invalid_keys_set)}")

else:
log('info',"跳过 API 密钥检查")
key_manager.api_keys.extend(keys_to_check_later)
key_manager._reset_key_stack()

init_router(
        key_manager,
        response_cache_manager,
        active_requests_manager,
        SAFETY_SETTINGS,
        SAFETY_SETTINGS_G2,
        first_valid_key,
        settings.FAKE_STREAMING,
        settings.FAKE_STREAMING_INTERVAL,
        settings.PASSWORD,
        settings.MAX_REQUESTS_PER_MINUTE,
        settings.MAX_REQUESTS_PER_DAY_PER_IP
    )
    init_dashboard_router(
        key_manager,
        response_cache_manager,
        active_requests_manager,
        credential_manager_instance
        key_manager, response_cache_manager, active_requests_manager, SAFETY_SETTINGS, SAFETY_SETTINGS_G2,
        first_valid_key, settings.FAKE_STREAMING, settings.FAKE_STREAMING_INTERVAL, settings.PASSWORD,
        settings.MAX_REQUESTS_PER_MINUTE, settings.MAX_REQUESTS_PER_DAY_PER_IP
)
    init_dashboard_router(key_manager, response_cache_manager, active_requests_manager, credential_manager_instance)

# --------------- 异常处理 ---------------
@app.exception_handler(Exception)
@@ -194,16 +156,26 @@ async def global_exception_handler(request: Request, exc: Exception):

# --------------- 路由 ---------------

# <--- 关键修复 2: 添加缺失的 /v1/models 接口，以兼容各种客户端和插件。
# <--- 最终修正: 使用动态加载的 AVAILABLE_MODELS，并按你的要求设置了备用模型
@app.get("/v1/models")
async def list_models():
"""
   返回一个符合 OpenAI 格式的模型列表。
   """
    model_data = [
        {"id": model, "object": "model", "owned_by": "google"}
        for model in settings.SUPPORTED_MODELS
    ]
    if not GeminiClient.AVAILABLE_MODELS:
        # 如果启动时动态加载模型列表失败，返回一个你指定的备用列表
        log('warning', "动态模型列表为空，返回指定的备用模型列表。")
        default_models = ["gemini-2.5-pro", "gemini-pro"]
        model_data = [
            {"id": model, "object": "model", "owned_by": "google"}
            for model in default_models
        ]
    else:
        # 正常情况下，返回动态加载的真实模型列表
        model_data = [
            {"id": model, "object": "model", "owned_by": "google"}
            for model in GeminiClient.AVAILABLE_MODELS
        ]
return {"object": "list", "data": model_data}

app.include_router(router)
