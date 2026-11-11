# 小如的衣橱 Agent Core（商品目录 + 本地演示交易）

本目录把电商业务逻辑与 Fay 的 ASR、TTS、WebSocket 和数字人展示链路分离。

## 当前调用链

```text
用户语音/文字
→ Fay ASR/输入层
→ AgentController
→ Router
→ Product / Order / Logistics / Policy Tool
→ Reflection Guardrail
→ Fay 流式输出
→ TTS / Live2D
```

未命中确定性电商路由的问题会回退到 Fay 原有 LLM 链路，但使用新的电商客服系统提示词。

数字人对话式穿搭现在由 LangGraph StateGraph 编排：配置好的 Fay 文本模型理解语义、补充条件并选择
看图/推荐工具；工具结果返回后，模型可继续决定下一步。商品候选、价格、预算、保留单品和
照片归属仍由程序核验。每轮最多三个决策、两个工具；模型超时或输出无效时在同一张图内回退到规则。
图节点为 decide、execute_tool、step_limit 和 finalize，规则模式也经过该图；当前不启用
checkpointer、自动重试或跨进程恢复。返回卡片元数据包含 workflow_engine 和 workflow_nodes，
用于验证实际编排路径，不暴露完整内部图状态。详见[迁移说明](../docs/langgraph-styling.md)。
这项升级接在原聊天、语音和商品卡链路中，概要与启动步骤见[项目 README](../README.md)。

## 目录说明

- `controller.py`：总调度、工具编排、高风险二次确认。物流查询采用最多两步的确定性观察/行动循环：核验订单归属后，按状态决定是否查询物流；失败立即停止。
- `router.py`：商品、订单、物流、政策、售后和通用问题分流。
- `tools/`：本地 JSON 工具注册与执行。
- `reflection.py`：规则式回复审核，修正物流必达、未经证实的交易成功等表达（不是另一次 LLM 自我反思）。
- `memory.py`：只保存最小化进程内客服状态，不保存完整敏感信息。
- `prompts.py`：电商客服身份和 LLM 回退提示词。
- `providers/`：统一商品接口，本机导入目录存在时优先读取；否则使用随仓库分发的开放许可服装小样。
- `data/products_public_fashion.json`：没有本机目录时的回退，8件真实服装照片支持试衣；价格、SKU、库存和尺码为演示参数。
- `data/public_fashion_sources.json`：公开小样的逐图作者、来源、CC0/CC BY 许可和处理记录。
- `../scripts/import_local_fashion.py`：从已有快照导入可试穿服装，不修改源目录；导入 JSON、图片与来源映射留在本机并被 Git 忽略，命令见[项目 README](../README.md#可选导入已有本机服装目录)。
- `data/products.json`：保留兼容旧测试与工具的数据，不作为工作台默认服装库。
- `data/*.json`：本地演示订单、物流和政策数据。
- `data/knowledge/*.md`：本地电商知识文件；可供后续 RAG 索引，目前政策工具直接检索 JSON。
- `fay_adapter.py`：接回 Fay 输入输出链路的薄适配层。
- `tryon/`：虚拟试衣 Provider、安全图片校验、会话隔离和临时结果清理。
- `styling/planner.py`：复用 Fay 文本模型，输出受校验的穿搭决策。
- `styling/workflow.py`：真实 LangGraph 有界决策图、条件边、多轮单品保留和局部替换、失败回退。
- `styling/engine.py`：确定性商品筛选、预算与真实降价约束，不让模型虚构商品。

## AI 虚拟试衣（可选）

本机校验导入的服装与仓库8件公开小样均有真实照片并支持试衣；需配置 FASHN Provider 与相应服务。启动器会检测已安装本地模型并尝试复用；未配置图像模型时，接口明确返回“服务尚未配置”，不会用拼图冒充结果。

PowerShell 中可使用 FASHN Provider：

```powershell
$env:FAY_TRYON_PROVIDER = "fashn"
$env:FASHN_API_KEY = "在本机环境变量中填写，不要提交到 Git"
.\.venv\python.exe main.py start
```

### 本地 FASHN VTON v1.5（独立 GPU 环境）

本地模型不会安装进项目的 `.venv`，权重也不随源码分发。请先按 FASHN
仓库的说明在独立环境安装依赖并下载权重。以下 PowerShell 示例中的三个路径需要替换
成你本机的项目目录、FASHN 仓库目录和推理环境 Python 路径：

```powershell
$projectRoot = "D:\path\to\xiaoru-wardrobe"
$fashnRoot = "D:\path\to\fashn-vton-1.5"
$fashnPython = "D:\path\to\fashn-vton-env\Scripts\python.exe"
& (Join-Path $projectRoot "scripts\setup_fashn_local.ps1") `
  -FashnRoot $fashnRoot `
  -EnvironmentRoot "D:\path\to\fashn-vton-env" `
  -WeightsRoot (Join-Path $fashnRoot "weights")
```

脚本完成后，在此独立 PowerShell 窗口中启动本地 HTTP 包装服务。服务会持续运行；
之后请另开一个窗口启动 Web 工作台：

```powershell
$projectRoot = "D:\path\to\xiaoru-wardrobe"
$fashnRoot = "D:\path\to\fashn-vton-1.5"
$fashnPython = "D:\path\to\fashn-vton-env\Scripts\python.exe"
Set-Location $fashnRoot
$env:PYTHONPATH = $projectRoot
$env:FAY_TRYON_LOCAL_TOKEN = "仅在本机环境变量中填写的随机值" # 可选
& $fashnPython `
  -m agent.tryon.local_service --weights-dir .\weights
```

从新的 PowerShell 窗口启动小如时，`scripts/start_fay.ps1` 可以自动拉起本地服务；它会先检查
`/health`，已运行的服务会复用；应用退出不结束共享模型，需要时执行 `scripts/stop_shared_model.ps1`：

```powershell
$env:FAY_TRYON_PROVIDER = "fashn_local"
$env:FAY_TRYON_LOCAL_URL = "http://127.0.0.1:7860/tryon"
$env:FAY_TRYON_LOCAL_TOKEN = "与上一个窗口相同的值" # 若服务启用了 token
$env:FAY_TRYON_LOCAL_STEPS = "20" # 5070 8GB 建议先用 20
$env:FAY_FASHN_VTON_ROOT = "D:\path\to\fashn-vton-1.5"
$env:FAY_FASHN_VTON_PYTHON = "D:\path\to\fashn-vton-env\Scripts\python.exe"
$env:FAY_FASHN_VTON_WEIGHTS_DIR = "D:\path\to\fashn-vton-1.5\weights"
Set-Location "D:\path\to\xiaoru-wardrobe"
.\scripts\start_fay.ps1
```

本地模型和推理环境路径可通过下面的环境变量指定：
`FAY_FASHN_VTON_ROOT`、`FAY_FASHN_VTON_PYTHON`、`FAY_FASHN_VTON_WEIGHTS_DIR`。
仅需启动小如而不启动本地换衣服务时，在项目根目录使用 `.\scripts\start_fay.ps1 -NoLocalTryOn`。

服务提供 `GET http://127.0.0.1:7860/health` 健康检查。工作台只向本地服务发送已经
校验过的人像图片，以及已登记的本地服装图片或可信 HTTPS 商品图；服务加载 `TryOnPipeline` 后以 base64 JPEG 返回
结果。推理请求按顺序执行，避免 8GB 显存同时加载多张任务。停止本地服务不会影响云端
`fashn` Provider 或开发占位 Provider。

本地联调上传、下载、删除流程时，可以将 `FAY_TRYON_PROVIDER` 设为
`development_placeholder`。占位结果会带有明显的 `NOT AI TRY-ON` 水印，不能用于产品演示。

- 上传仅接受经过实际解码验证的 JPEG、PNG、WebP，单张最大 64MB（64×1024×1024 字节）、2400 万像素；送入模型前会保持比例缩至最长边 2048 像素并重编码，适配本地服务的图片大小限制。
- 试衣商品须来自当前目录并使用已登记、通过校验的服装图；本机导入的原来源映射保留在本机，不能将其当作公开 CC 小样分发。价格、库存和尺码不从照片推断。
- 原始照片会在 Provider 请求结束前删除；结果默认保留 30 分钟，也可由客户立即删除。
- 不保存 EXIF、定位、相机信息，不写入长期记忆或知识库。
- FASHN 使用 `return_base64=true`，避免依赖长期公开结果链接；密钥只从环境变量读取。
- “轻度美化”仅做极轻微光线/对比度优化，不瘦身、不改变身体几何或商品颜色。

## 演示问题

```text
中性深灰基础圆领短袖T恤有哪些尺码？
我身高170厘米、体重60公斤，深灰短袖建议什么尺码？
查询订单 DEMO-20260927-XXXXXXXX
这个 DEMO 订单现在到哪里了？
七天无理由退货的运费谁承担？
商品有质量问题，超过7天还能申请售后吗？
```

## 安全边界

- 随仓库分发的服装照片为开放许可实拍，业务参数为演示数据；本机导入保留快照已有参考价与规格，不补造缺失尺码，不代表实时在售。
- 购物车、`DEMO-` 订单、物流和退货均为本地演示数据，不代表真实交易。
- 项目没有连接真实商家后台。公开小样链接是素材许可记录，不是购买页；本机导入的原来源保存在被忽略的映射中，不在前台冒充自有店铺。
- 每次订单和物流查询都必须显式提供订单号，并校验订单归属。
- 本地演示订单支持购物车、订单、物流和退货流程，但不执行真实支付、退款、取消或地址修改。
- 交易类对话不写入旧语义长期记忆，避免订单和物流信息被发送给 embedding 服务。
- RAG 只开放查询工具；索引重建和统计工具不提供给 LLM。

## 下一阶段

接入真实平台前，需要先实现可信的客户身份和会话 ID、Tool 写操作审计、确认状态 TTL、幂等键、人工工单系统和真实订单 API 适配。当前电商售后 `plan_and_solve` 只是按固定规则组合订单与政策查询，不是 LLM 自动规划；物流 `react` 是有上限的状态驱动工具循环。未命中电商路由的原 Fay LLM 链路另有基于 MCP 工具结果进行下一步决策的模型循环，不能将其等同于所有电商请求都经过 ReAct。

生产适配器默认启用穿搭的 `model_react`；它与上述确定性订单、物流、售后分支分别工作。
将环境变量 `FAY_STYLING_PLANNER` 设为 `rule` 可显式切回规则穿搭流程；独立构造
`AgentController()` 时默认使用规则，测试或其他集成若需要模型，应显式传入 planner。
这里的 rule 仅关闭文本模型决策，不绕过 LangGraph。不要将继承的通用 LLM 文件里保留的
旧图定义，作为本次业务图实际运行的证据；本次入口是 Controller → StylingWorkflow.run → graph.invoke。

## 服装视觉模型适配与评测

数字人聊天调用的 `LocalVision` 与离线评测、训练数据准备共用服装观察契约。
默认继续使用本机 Ollama CPU 看图；配置 `openai_local` 后，可将独立部署的
原始或领域适配视觉模型接回同一照片工具，无需另建客服界面。

- `styling/vision_contract.py`：统一提示词版本、服装属性枚举与输出白名单。
- `styling/vision_evaluation.py`：显式标注数据校验、分组防泄漏和服装观察指标。
- `../scripts/evaluate_styling_vision.py`：原模型、提示词对照及后续微调模型评测。
- `../scripts/prepare_styling_vision_sft.py`：导出外部训练框架的数据，保留测试集隔离。

数据校验、评测与导出参数可分别运行对应脚本的 `--help` 查看。该能力不自动收集客户图片，也不代表已经完成训练或得到识别率提升。
