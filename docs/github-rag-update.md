# 政策 RAG：本次 GitHub 更新清单

本次修改位于 `xiaoru-wardrobe`，没有自动暂存、提交或推送。这个仓库原先已有大量暂存内容和其他未提交更改；发布前请逐项核对，不能把现有全部文件都视为本次 RAG 新增。

## 本次要纳入版本的文件

- `agent/rag/__init__.py`、`agent/rag/retriever.py`、`agent/rag/answer.py`：本地向量检索、受限证据选择与回退。
- `agent/controller.py`、`agent/tools/policy_tool.py`：对接客服路由与政策工具。
- `scripts/setup_policy_rag.py`、`scripts/evaluate_policy_rag.py`：准备模型和对照评测。
- `test/test_policy_rag.py`、`test/test_policy_rag_routing.py`、`test/fixtures/policy_rag_cases.json`：测试与样本。
- `requirements.txt`、`scripts/test_agent.ps1`：模型依赖与回归测试入口。`requirements.lock.txt` 已含对应依赖，本次未修改。
- `README.md`、`agent/README.md`、`docs/policy-rag.md`、`docs/langgraph-styling.md`、`docs/github-rag-update.md`：使用说明和更新记录。

这些文件可能同时包含此前其他尚未提交的改动；使用 `git diff` 和 `git diff --cached` 检查完整内容后再按自己的发布范围提交。`docs/langgraph-styling.md` 是仓库中尚未跟踪的已有说明，本次仅更新相关描述。

## 不要提交

模型权重和 Hugging Face 缓存、虚拟环境、`config.json`、`system.conf`、API 密钥、用户照片、数据库、日志、`cache_data/` 中的运行报告及私有商品资源。已暂存文件不会因为后来加入 `.gitignore` 而自动移除，发布前也要检查暂存区和历史。图像、商品及数字人资源需核实许可。

## 验证范围

真实 CPU 向量检索、对照评测、Fay 文本适配层样例及自动回归已执行。在线模型调用返回 HTTP 429，真实在线证据选择仍待恢复额度后测试；未声称语音/数字人播放全链路人工验收。具体数字和局限见 `docs/policy-rag.md`。

## 后续穿搭与数字人修复（2026-11-20）

同日后续还增加 `test/test_styling_planner_diagnostics.py`、`test/test_styling_required_items.py`，并更新穿搭 planner/workflow、偏好、引擎、Controller 与测试入口：区分模型失败原因，严格处理保留上衣和指定白色裤装。真实模型与 1009 件本机目录的该原话样例已通过；旧截图那一次的异常码无历史记录，未凭空认定原因。`cache_data/` 中临时诊断脚本和运行日志不提交。

另需核对：`gui/avatar_assets.py`、`test/test_release_assets.py`、`agent/styling/preferences.py`、`agent/styling/engine.py`、`agent/styling/service.py`、`agent/styling/planner.py`、`agent/styling/workflow.py`、`agent/tools/styling_tool.py`、`gui/templates/index.html`、`gui/static/css/styling.css`、`test/test_styling_color_constraints.py`、`test/test_styling_revision.py`、`docs/styling-runtime-check.md`，以及共享的 `agent/controller.py`、`README.md`、`scripts/test_agent.ps1`。这些文件也可能包含之前未提交内容，提交前检查差异。修复与验证见 `docs/styling-runtime-check.md`。
