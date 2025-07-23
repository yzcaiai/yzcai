# =================================================================
#               最终、经过仔细检查和修改的完整代码
# =================================================================
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from app.models.schemas import ErrorResponse
from app.services import GeminiClient
from app.utils import (
    APIKeyManager, 
    test_api_key, 
    ResponseCacheManager,
    ActiveRequestsManager,
    check_version,
    schedule_cache_cleanup,
    handle_exception,
    log
)
from app.config.persistence import save_settings, load_settings
from app.api import router, init_router, dashboard_router, init_dashboard_router
from app.vertex.vertex_ai_init import init_vertex_ai
from app.vertex.credentials_manager import CredentialManager
import app.config.settings as settings
from app.config.safety import SAFETY_SETTINGS, SAFETY_SETTINGS_G2
import asyncio
import sys
import pathlib
import os

# 设置模板目录
BASE_DIR = pathlib.Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(limit="50M")

# --------------- CORS 中间件 (强制开启) ---------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --------------- 全局实例 ---------------
load_settings()
key_manager = APIKeyManager()
response_cache = {}
response_cache_manager = ResponseCacheManager(
    expiry_time=settings.CACHE_EXPIRY_TIME,
    max_entries=settings.MAX_CACHE_ENTRIES,
    cache_dict=response_cache
)
active_requests_pool = {}
active_requests_manager = ActiveRequestsManager(requests_pool=active_requests_pool)
SKIP_CHECK_API_KEY = os.environ.get("SKIP_CHECK_API_KEY", "").lower() == "true"

# --------------- 工具函数 ---------------
async def check_remaining_keys_async(keys_to_check: list, initial_invalid_keys: list):
    local_invalid_keys = []
    found_valid_keys = False
    log('info', f" 开始在后台检查剩余 API Key 是否有效")
    for key in keys_to_check:
        is_valid = await test_api_key(key)
        if is_valid:
            if key not in key_manager.api_keys:
                key_manager.api_keys.append(key)
                found_valid_keys = True
        else:
            local_invalid_keys.append(key)
            log('warning', f" API Key {key[:8]}... 无效")
        await asyncio.sleep(0.05)
    if found_valid_keys:
        key_manager._reset_key_stack()
    combined_invalid_keys = list(set(initial_invalid_keys + local_invalid_keys))
    current_invalid_keys_str = settings.INVALID_API_KEYS or ""
    current_invalid_keys_set = set(k.strip() for k in current_invalid_keys_str.split(',') if k.strip())
    new_invalid_keys_set = current_invalid_keys_set.union(set(combined_invalid_keys))
    if new_invalid_keys_set != current_invalid_keys_set:
        settings.INVALID_API_KEYS = ','.join(sorted(list(new_invalid_keys_set)))
        save_settings()
    log('info', f"密钥检查任务完成。当前总可用密钥数量: {len(key_manager.api_keys)}")

sys.excepthook = handle_exception

# --------------- 事件处理 ---------------
@app.on_event("startup")
async def startup_event():
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
            log('info', f"找到第一个有效密钥: {key[:8]}...")
            first_valid_key = key
            key_manager.api_keys.append(key)
            key_manager._reset_key_stack()
            keys_to_check_later = initial_keys[index + 1:]
            break
        else:
            log('warning', f"密钥 {key[:8]}... 无效")
            initial_invalid_keys.append(key)
    if not first_valid_key:
        log('error', "启动时未能找到任何有效 API 密钥！")
        keys_to_check_later = []
    else:
        try:
            all_models = await GeminiClient.list_available_models(first_valid_key)
            GeminiClient.AVAILABLE_MODELS = [model.replace("models/", "") for model in all_models]
            log('info', f"使用密钥 {first_valid_key[:8]}... 加载可用模型成功")
        except Exception as e:
            log('warning', f"使用密钥 {first_valid_key[:8]}... 加载可用模型失败",extra={'error_message': str(e)})
    if not SKIP_CHECK_API_KEY:
        if keys_to_check_later:
            asyncio.create_task(check_remaining_keys_async(keys_to_check_later, initial_invalid_keys))
        else:
            current_invalid_keys_str = settings.INVALID_API_KEYS or ""
            current_invalid_keys_set = set(k.strip() for k in current_invalid_keys_str.split(',') if k.strip())
            new_invalid_keys_set = current_invalid_keys_set.union(set(initial_invalid_keys))
            if new_invalid_keys_set != current_invalid_keys_set:
                 settings.INVALID_API_KEYS = ','.join(sorted(list(new_invalid_keys_set)))
                 save_settings()
                 log('info', f"更新初始无效密钥列表完成，总无效密钥数: {len(new_invalid_keys_set)}")
    else:
        log('info',"跳过 API 密钥检查")
        key_manager.api_keys.extend(keys_to_check_later)
        key_manager._reset_key_stack()
    init_router(
        key_manager, response_cache_manager, active_requests_manager, SAFETY_SETTINGS, SAFETY_SETTINGS_G2,
        first_valid_key, settings.FAKE_STREAMING, settings.FAKE_STREAMING_INTERVAL, settings.PASSWORD,
        settings.MAX_REQUESTS_PER_MINUTE, settings.MAX_REQUESTS_PER_DAY_PER_IP
    )
    init_dashboard_router(key_manager, response_cache_manager, active_requests_manager, credential_manager_instance)

# --------------- 异常处理 ---------------
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    from app.utils import translate_error
    error_message = translate_error(str(exc))
    extra_log_unhandled_exception = {'status_code': 500, 'error_message': error_message}
    log('error', f"Unhandled exception: {error_message}", extra=extra_log_unhandled_exception)
    return JSONResponse(status_code=500, content=ErrorResponse(message=str(exc), type="internal_error").dict())

# --------------- 路由 ---------------

# <--- 最终修正: 使用动态加载的 AVAILABLE_MODELS，并按你的要求设置了备用模型
@app.get("/v1/models")
async def list_models():
    """
    返回一个符合 OpenAI 格式的模型列表。
    """
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
app.include_router(dashboard_router)

# 挂载静态文件目录
app.mount("/assets", StaticFiles(directory="app/templates/assets"), name="assets")

# 设置根路由路径
dashboard_path = f"/{settings.DASHBOARD_URL}" if settings.DASHBOARD_URL else "/"

@app.get(dashboard_path, response_class=HTMLResponse)
async def root(request: Request):
    """
    根路由 - 返回静态 HTML 文件
    """
    base_url = str(request.base_url).replace("http", "https")
    api_url = f"{base_url}v1" if base_url.endswith("/") else f"{base_url}/v1"
    return templates.TemplateResponse(
        "index.html", {"request": request, "api_url": api_url}
    )
