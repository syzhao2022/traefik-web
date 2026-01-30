# cat main.py 
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
import asyncio
from kubernetes import client, config, watch
from kubernetes.client.rest import ApiException
import json
from datetime import datetime
import logging
import threading
import signal
import sys
import os
import socket
from contextlib import asynccontextmanager
import time

# ========== 1. 日志配置（极致详细，定位Watch启动） ==========
logging.basicConfig(
    level=logging.DEBUG,  # 改为DEBUG，打印所有细节
    format="%(asctime)s - %(name)s - %(levelname)s - %(lineno)d - %(threadName)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# ========== 2. 全局变量（简化+精准） ==========
SHUTDOWN_FLAG: bool = False
LOOP: Optional[asyncio.AbstractEventLoop] = None
WATCH_THREAD: Optional[threading.Thread] = None  # 改用线程直接启动Watch
WATCH_LOCK: threading.Lock = threading.Lock()
K8S_CLIENT: Optional[client.CustomObjectsApi] = None  # 全局K8s客户端

# ========== 3. WebSocket管理器 ==========
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []
        self.lock = threading.Lock()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        with self.lock:
            self.active_connections.append(websocket)
        logger.info(f"新WebSocket连接，当前连接数: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        try:
            with self.lock:
                self.active_connections.remove(websocket)
            logger.info(f"WebSocket断开，当前连接数: {len(self.active_connections)}")
        except ValueError:
            pass

    async def send_personal_message(self, message: dict, websocket: WebSocket):
        try:
            await websocket.send_json(message)
        except Exception as e:
            logger.error(f"发送单播消息失败: {e}")
            self.disconnect(websocket)

    async def broadcast(self, message: dict):
        with self.lock:
            connections = self.active_connections.copy()
        for connection in connections:
            try:
                await connection.send_json(message)
            except Exception as e:
                logger.error(f"广播消息失败: {e}")
                self.disconnect(connection)

    async def close_all_connections(self):
        with self.lock:
            connections = self.active_connections.copy()
            self.active_connections.clear()
        for connection in connections:
            try:
                await connection.close(code=1001, reason="Server shutdown")
                logger.info("WebSocket连接已关闭")
            except Exception as e:
                logger.warning(f"关闭WebSocket失败: {e}")

manager = ConnectionManager()

# ========== 4. 信号处理 ==========
def handle_shutdown_signal(signum, frame):
    global SHUTDOWN_FLAG
    if not SHUTDOWN_FLAG:
        logger.info(f"\n收到退出信号 ({signum})，开始优雅退出...")
        SHUTDOWN_FLAG = True
        # 主动停止Watch线程
        if WATCH_THREAD and WATCH_THREAD.is_alive():
            logger.info("通知K8s Watch线程停止...")
    else:
        logger.warning("强制退出！")
        os._exit(0)

# ========== 5. K8s客户端初始化（独立函数，可重试） ==========
def init_k8s_client() -> Optional[client.CustomObjectsApi]:
    """初始化K8s客户端，支持重试"""
    global K8S_CLIENT
    if K8S_CLIENT:
        return K8S_CLIENT
    try:
        # 优先集群内配置
        config.load_incluster_config()
        logger.info("✅ 成功加载K8s集群内配置")
    except Exception as e1:
        logger.debug(f"集群内配置加载失败: {e1}")
        try:
            # 回退到本地配置
            config.load_kube_config()
            logger.info("✅ 成功加载K8s本地配置 (~/.kube/config)")
        except Exception as e2:
            logger.error(f"❌ K8s客户端初始化失败: {e2}")
            return None
    # 创建CustomObjectsApi客户端
    K8S_CLIENT = client.CustomObjectsApi()
    return K8S_CLIENT

# ========== 6. K8s Watch核心逻辑（独立线程，必启动） ==========
def run_k8s_watch():
    """独立线程运行K8s Watch，确保阻塞监听"""
    logger.info("📌 K8s Watch线程已启动，开始初始化...")
    reconnect_delay = 5
    watch_obj = None
    resource_version = ""  # 记录断点，实现resume
    is_first_run = True    # 首次全量拉取标记

    # Watch主循环（直到收到退出信号）
    while not SHUTDOWN_FLAG:
        # 确保K8s客户端已初始化
        k8s_client = init_k8s_client()
        if not k8s_client:
            logger.warning(f"❌ K8s客户端未就绪，{reconnect_delay}秒后重试...")
            for _ in range(reconnect_delay):
                if SHUTDOWN_FLAG:
                    break
                time.sleep(1)
            continue

        # 创建Watch对象
        if not watch_obj:
            watch_obj = watch.Watch()
            logger.info("✅ K8s Watch对象已创建")

        try:
            logger.info("🔍 开始K8s Watch监听 TraefikService...")
            # 核心：阻塞式监听K8s资源变化
            for event in watch_obj.stream(
                func=k8s_client.list_namespaced_custom_object,
                group="traefik.containo.us",
                version="v1alpha1",
                namespace="kube-system",
                plural="traefikservices",
                resource_version = resource_version,
                timeout_seconds=0,  # 1分钟超时，平衡实时性和重连
                #_request_timeout=30   # 请求超时保护
            ):
                # 更新最新resourceVersion，断连后从这里续传
                res_meta = event["object"].get("metadata", {})
                if res_meta.get("resourceVersion"):
                    resource_version = res_meta["resourceVersion"]

                # 首次全量只打ADDED，后续增量正常输出（对齐kubectl）
                event_type = event["type"]
                if is_first_run:
                    event_type = "ADDED"
                res_name = res_meta.get("name", "unknown")
                print(f"[{event_type}] {res_name} (rv:{resource_version})")


                # 检测退出信号，立即停止
                if SHUTDOWN_FLAG:
                    logger.info("🛑 收到退出信号，停止Watch stream")
                    break
                # 处理事件（异步推送到FastAPI）
                logger.info(f"📥 收到K8s事件: {event['type']} - {event['object'].get('metadata', {}).get('name', 'unknown')}")
                # 异步广播事件到WebSocket
                if LOOP and not LOOP.is_closed():
                    asyncio.run_coroutine_threadsafe(
                        handle_k8s_event(event),
                        LOOP
                    )
            is_first_run = False  # 首次全量完成，后续都是增量
            # 正常退出stream循环
            if not SHUTDOWN_FLAG:
                logger.info("⌛ Watch stream超时，准备重连...")
        except ApiException as e:
            #logger.error(f"❌ K8s Watch API错误: {e.status} - {e.reason} (路径: {e.path})")
            logger.error(f"❌ K8s Watch API错误: {e.status}")
        except Exception as e:
            logger.error(f"❌ Watch异常: {str(e)[:200]}", exc_info=True)
            #logger.error("❌ Watch异常")
        finally:
            # 清理Watch对象
            if watch_obj:
                try:
                    watch_obj.stop()
                    logger.info("✅ Watch对象已停止")
                except:
                    pass
                watch_obj = None

        # 重连前等待（检测退出信号）
        if not SHUTDOWN_FLAG:
            logger.info(f"⏳ {reconnect_delay}秒后尝试重连K8s Watch...")
            for _ in range(reconnect_delay):
                if SHUTDOWN_FLAG:
                    break
                time.sleep(1)

    # Watch线程退出
    logger.info("📌 K8s Watch线程已正常退出")

async def handle_k8s_event(event: Dict[str, Any]):
    """异步处理K8s事件，广播到所有WebSocket客户端"""
    try:
        message_type = "update" if event["type"] == "MODIFIED" else "full"
        event_data = {
            "type": message_type,
            "data": [{
                "service": event["object"].get("metadata", {}).get("name", "unknown"),
                "status": "online",
                "backends": [],
                "totalTraffic": "100",
                "updatedAt": datetime.now().isoformat()
                }]
        }

        backends_spec = event.get("object").get("spec").get("weighted")
        if backends_spec is not None:
            for backend in backends_spec.get("services"):
                event_data["data"][0]["backends"].append(
                        {
                            "name": backend.get("name"),
                            "ratio": backend.get("weight"),
                            "namespace": backend.get("namespace"),
                            "port": backend.get("port")
                            }

                        )

        print("============================")
        print(event_data)
        print("============================")
        await manager.broadcast(event_data)
        logger.info("📤 K8s事件已广播到WebSocket客户端")
    except Exception as e:
        logger.error(f"❌ 广播K8s事件失败: {e}")

# ========== 7. Lifespan生命周期（确保Watch线程启动） ==========
@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI生命周期：启动Watch线程，不阻塞监听"""
    global LOOP, SHUTDOWN_FLAG, WATCH_THREAD
    SHUTDOWN_FLAG = False
    LOOP = asyncio.get_running_loop()

    # 1. 注册信号处理
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGINT, handle_shutdown_signal)
        signal.signal(signal.SIGTERM, handle_shutdown_signal)
        logger.info("✅ 信号处理已注册")

    # 2. 启动K8s Watch线程（核心修复：独立线程，必启动）
    with WATCH_LOCK:
        if not WATCH_THREAD or not WATCH_THREAD.is_alive():
            # 启动独立线程运行Watch（避免阻塞事件循环）
            WATCH_THREAD = threading.Thread(
                target=run_k8s_watch,
                name="K8sWatchThread",
                daemon=True  # 守护线程，主进程退出时自动终止
            )
            WATCH_THREAD.start()
            logger.info("✅ K8s Watch线程已启动（线程ID: %d）", WATCH_THREAD.ident)
        else:
            logger.info("✅ K8s Watch线程已在运行")

    # 3. FastAPI进入监听状态（关键：yield前无阻塞逻辑）
    logger.info("=== 🚀 FastAPI服务启动完成，开始监听请求 ===")
    yield

    # 4. 优雅关闭阶段
    logger.info("=== 🛑 开始优雅关闭FastAPI服务 ===")
    # 关闭所有WebSocket连接
    await manager.close_all_connections()
    # 等待Watch线程退出（最多10秒）
    if WATCH_THREAD and WATCH_THREAD.is_alive():
        logger.info("等待K8s Watch线程退出...")
        WATCH_THREAD.join(timeout=10)
        if WATCH_THREAD.is_alive():
            logger.warning("K8s Watch线程超时未退出，强制终止")
    # 清理全局变量
    global K8S_CLIENT
    K8S_CLIENT = None
    WATCH_THREAD = None
    logger.info("=== ✅ FastAPI服务已优雅关闭 ===")

# ========== 8. FastAPI应用初始化 ==========
app = FastAPI(
    title="Traefik Service Manager",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc"
)

INDEX_HTML_PATH = "/home/traefik-dashboard/dist/index.html"
# 挂载静态文件（忽略错误，不影响核心功能）
try:
    #app.mount("/static", StaticFiles(directory="traefik-dashboard/dist/"), name="static")
    app.mount("/asserts", StaticFiles(directory="/home/traefik-dashboard/dist/", html=True), name="static")
    #app.mount("/assets",StaticFiles(directory="/home/traefik-dashboard/dist/assets/"),name="frontend_assets")
    #templates = Jinja2Templates(directory="traefik-dashboard/dist/")
except Exception as e:
    logger.warning(f"静态文件挂载失败: {e}")
    templates = None

# ========== 9. 数据模型 ==========
class Backend(BaseModel):
    name: str
    namespace: str
    ratio: int
    port: int

class TrafficConfigRequest(BaseModel):
    service_name: str
    backends: List[Backend]


@app.get("/api/health", summary="健康检查（核心验证）")
async def health_check():
    """验证服务状态+Watch线程状态"""
    return {
        "status": "running",
        "timestamp": datetime.now().isoformat(),
        "k8s_client_init": K8S_CLIENT is not None,
        "watch_thread_alive": WATCH_THREAD.is_alive() if WATCH_THREAD else False,
        "shutdown_flag": SHUTDOWN_FLAG,
        "event_loop_running": LOOP.is_running() if LOOP else False
    }









@app.get("/api/traefik-services", summary="获取TraefikService列表")
async def get_traefik_services():
    """获取K8s TraefikService（无K8s则返回模拟数据）"""
    services = []
    k8s_client = init_k8s_client()
    if k8s_client:
        try:
            resp = k8s_client.list_namespaced_custom_object(
                group="traefik.containo.us",
                version="v1alpha1",
                namespace="kube-system",
                plural="traefikservices"
            )
            for item in resp.get("items", []):
                service_spec = {
                    "service": item["metadata"]["name"],
                    "status": "online",
                    "backends": [],
                    "totalTraffic": "100",
                    "updatedAt": item["metadata"].get("creationTimestamp", datetime.now().isoformat())
                    }
                backends_spec = item.get("spec").get("weighted")
                if backends_spec is not None:
                    for backends in backends_spec.get("services"):
                        service_spec["backends"].append(
                                {
                                    "name": backends.get("name"),
                                    "ratio": backends.get("weight"),
                                    "port": backends.get("port"),
                                    "namespace": backends.get("namespace")
                                    }
                                )
                services.append(service_spec)
            logger.info(f"获取到{len(services)}个TraefikService")
        except Exception as e:
            logger.error(f"获取TraefikService失败: {e}")
            services = get_fallback_services()
    else:
        services = get_fallback_services()

    print(services)
    return {"code": 200, "message": "success", "type": "full", "data": services}


# ========== 10. 测试/健康检查接口 ==========
@app.get("/{full_path:path}", response_class=HTMLResponse, include_in_schema=False)
async def serve_frontend(full_path: str):
    """
    前端兜底路由：处理所有非API/WebSocket的请求，返回index.html
    兼容前端路由（如/dashboard、/setting），由前端Vue/React解析
    full_path:path 表示匹配所有路径（通配符）
    """
    # 检查index.html是否存在
    if not os.path.exists(INDEX_HTML_PATH):
        logger.error(f"❌ 前端入口文件不存在：{INDEX_HTML_PATH}")
        return HTMLResponse(content="<h1>前端文件未找到</h1>", status_code=404)
    # 读取并返回index.html（确保前端能加载，且静态资源路径正确）
    with open(INDEX_HTML_PATH, "r", encoding="utf-8") as f:
        html_content = f.read()
    return HTMLResponse(content=html_content)


# ========== 11. WebSocket接口 ==========
@app.websocket("/ws/traefik-services")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket实时接收K8s事件"""
    await manager.connect(websocket)
    try:
        # 推送初始数据
        init_data = await get_traefik_services()
        await manager.send_personal_message(init_data, websocket)
        # 保持连接（检测退出信号）
        while not SHUTDOWN_FLAG:
            try:
                # 非阻塞接收前端消息
                data = await asyncio.wait_for(websocket.receive_text(), timeout=2.0)
                logger.info(f"收到前端消息: {data[:50]}")
                await get_traefik_services()
                #await manager.send_personal_message(resdata, websocket)
                await manager.send_personal_message({"type": "echo", "data": data}, websocket)
            except asyncio.TimeoutError:
                continue
            except WebSocketDisconnect:
                logger.info("WebSocket客户端主动断开")
                break
            except Exception as e:
                logger.error(f"WebSocket异常: {e}")
                break
    finally:
        manager.disconnect(websocket)




# HTTP接口：更新流量配置
@app.post("/api/update-traffic-config")
async def update_traffic_config(request: TrafficConfigRequest):
    try:
        # 1. 校验总比例是否为100%
        total_ratio = sum([b.ratio for b in request.backends])
        if abs(total_ratio - 100) > 0.1:
            raise HTTPException(status_code=400, detail="流量比例总和必须为100%")

        # 2. 更新K8s TraefikService资源
        success = update_traefik_service_in_k8s(request.service_name, request.backends)

        if success:
            # 3. 推送更新到所有WebSocket客户端
            updated_services = get_traefik_services()
            #await manager.broadcast(updated_services)
            #await manager.broadcast({
            #    "type": "update",
            #    "data": updated_services
            #})
            return {
                "code": 200,
                "message": "流量配置更新成功",
                "data": {"service_name": request.service_name}
            }
        else:
            raise HTTPException(status_code=500, detail="更新K8s TraefikService失败")

    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"服务器异常: {str(e)}")





def update_traefik_service_in_k8s(service_name: str, backends: List[Backend]) -> bool:
    """
    更新K8s中的TraefikService资源
    TraefikService CRD格式参考：https://doc.traefik.io/traefik/routing/providers/kubernetes-crd/#traefikservice
    """
    k8s_custom_objects_api = init_k8s_client()
    namespace = "kube-system"  # 根据实际情况修改命名空间
    traefik_service_api_version = "traefik.containo.us/v1alpha1"
    traefik_service_plural = "traefikservices"

    try:
        # 1. 获取现有TraefikService资源
        traefik_service = k8s_custom_objects_api.get_namespaced_custom_object(
            group="traefik.containo.us",
            version="v1alpha1",
            namespace=namespace,
            plural=traefik_service_plural,
            name=service_name
        )
        print("========================================")
        print(traefik_service)
        print("========================================")

        # 2. 构建新的权重配置（ratio比例转weight权重）
        weighted_services = []
        for backend in backends:
            weighted_services.append({
                "name": backend.name,
                "namespace": backend.namespace,
                "port": backend.port,
                "weight": int(backend.ratio)  # Traefik权重为整数，比例直接转权重
            })

        # 3. 更新TraefikService的权重配置
        traefik_service["spec"] = {
            "weighted": {
                "services": weighted_services
            }
        }

        # 4. 应用更新到K8s
        k8s_custom_objects_api.replace_namespaced_custom_object(
            group="traefik.containo.us",
            version="v1alpha1",
            namespace=namespace,
            plural=traefik_service_plural,
            name=service_name,
            body=traefik_service
        )

        print(f"成功更新K8s TraefikService: {service_name}")
        return True

    except ApiException as e:
        print(f"更新K8s TraefikService失败: {e}")
        return False
    except Exception as e:
        print(f"更新异常: {e}")
        return False










# ========== 12. 模拟数据 ==========
def get_fallback_services() -> List[Dict[str, Any]]:
    """无K8s时的模拟数据"""
    return [
        {
            "name": "web-service",
            "status": "online",
            "totalTraffic": 1256.8,
            "updatedAt": datetime.now().isoformat(),
            "backends": [
                {"id": 1, "name": "backend-1", "ratio": 60.0},
                {"id": 2, "name": "backend-2", "ratio": 30.0},
                {"id": 3, "name": "backend-3", "ratio": 10.0}
            ]
        }
    ]

# ========== 13. 启动服务（前置校验） ==========
def check_port_available(port: int) -> bool:
    """检查端口是否可用"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", port))
            return True
        except OSError as e:
            logger.error(f"❌ 端口 {port} 被占用: {e}")
            return False

if __name__ == "__main__":
    import uvicorn
    import time  # 补充缺失的time导入
    PORT = 8001

    # 前置校验
    if not check_port_available(PORT):
        sys.exit(1)

    # 启动FastAPI（强制单进程、禁用热重载）
    uvicorn.run(
        app="main:app",
        host="0.0.0.0",
        port=PORT,
        log_level="info",
        reload=False,
        workers=1,
        access_log=True
    )
