"""Fay-Ecommerce-Agent v1 总调度器。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .execution import MiniReActExecutor, ReActAction, ReActDecision
from .memory import SessionMemory
from .privacy import redact_sensitive_arguments
from .product_context import normalize_product_context_id
from .reflection import ReflectionGuardrail
from .router import RouteResult, Router
from .service_rules import needs_large_order_review
from .tools import ToolRegistry


@dataclass
class AgentResponse:
    handled: bool
    text: str = ""
    intent: str = "general"
    strategy: str = "fallback"
    tool_calls: list[dict] = field(default_factory=list)
    requires_confirmation: bool = False
    audit: list[str] = field(default_factory=list)
    presentation: dict = field(default_factory=dict)


class AgentController:
    """读取上下文、路由任务、调用工具并审核最终回复。"""

    _confirmation_pattern = re.compile(
        r"^\s*确认处理\s+((?:(?:DEMO|FX)-\d{8}-[A-Fa-f0-9]{8})|(?:[A-Za-z]\d{5,}))\s*[。.!！]?$",
        re.IGNORECASE,
    )

    def __init__(
        self,
        *,
        router: Router | None = None,
        registry: ToolRegistry | None = None,
        memory: SessionMemory | None = None,
        reflection: ReflectionGuardrail | None = None,
        styling_planner=None,
    ) -> None:
        self.router = router or Router()
        self.registry = registry or ToolRegistry()
        self.memory = memory or SessionMemory()
        self.reflection = reflection or ReflectionGuardrail()
        self.styling_planner = styling_planner

    @staticmethod
    def _tool_call(name: str, arguments: dict, result: dict) -> dict:
        return {
            "tool": name,
            "arguments": redact_sensitive_arguments(arguments),
            "ok": bool(result.get("ok")),
            "error": result.get("error"),
        }

    @staticmethod
    def _memory_user_id(user_id: str, demo_session_id: str | None) -> str:
        if not demo_session_id:
            return user_id
        return f"demo:{len(demo_session_id)}:{demo_session_id}:{user_id}"

    def _remember_route(self, user_id: str, route: RouteResult) -> None:
        # 未经工具核验的订单号/商品号不能进入上下文，避免不存在或越权实体污染后续请求。
        self.memory.remember(user_id, route.intent)

    def _reuse_recent_product(
        self,
        text: str,
        user_id: str,
        route: RouteResult,
    ) -> RouteResult:
        if route.intent not in {"product_query", "size_recommendation"}:
            return route
        if route.entities.get("product_id"):
            return route
        recent_product_id = self.memory.get_last_product_id(user_id)
        if not recent_product_id:
            return route
        if route.intent == "size_recommendation" and any(
            marker in (text or "")
            for marker in (
                "什么衣服", "什么裤", "什么上衣", "什么外套", "什么穿搭",
                "怎么搭配", "怎么穿搭", "推荐衣服", "推荐裤", "推荐上衣",
                "推荐外套", "推荐几款", "推荐几件", "推荐几条",
            )
        ):
            return route
        is_follow_up = route.intent == "size_recommendation" or any(
            marker in (text or "")
            for marker in ("这个", "这款", "它", "刚才", "上面", "该商品", "这件")
        )
        if not is_follow_up:
            return route
        entities = dict(route.entities)
        entities["product_id"] = recent_product_id
        return RouteResult(route.intent, route.strategy, route.confidence, entities)

    def verified_product_context_id(self, value: object) -> str | None:
        """Accept only an ID present in the local public snapshot or demo catalog."""
        try:
            product_id = normalize_product_context_id(value)
        except ValueError:
            return None
        product_tool = self.registry.get("query_product")
        if product_tool is None:
            return None
        if product_id.startswith("YZREF-"):
            provider = getattr(product_tool, "public_provider", None)
            return product_id if provider and provider.get_product(product_id) else None
        return product_id if any(
            str(item.get("product_id", "")).upper() == product_id
            for item in getattr(product_tool, "products", [])
        ) else None

    def _resolve_order_id(self, user_id: str, route: RouteResult) -> str | None:
        # Stage 1 尚无可靠的服务端客户会话 ID。为避免匿名顾客串单，订单/物流查询
        # 每次都要求当前请求显式提供订单号，不复用进程级最近订单上下文。
        return route.entities.get("order_id")

    def _finalize(
        self,
        *,
        route: RouteResult,
        text: str,
        tool_calls: list[dict] | None = None,
        requires_confirmation: bool = False,
        evidence: dict | None = None,
        audit: list[str] | None = None,
        presentation: dict | None = None,
    ) -> AgentResponse:
        reviewed = self.reflection.review(text, intent=route.intent, evidence=evidence)
        checks = list(audit or [])
        if reviewed != text:
            checks.append("Reflection 已修正过度承诺或未经证实的操作结果")
        else:
            checks.append("Reflection 检查通过")
        return AgentResponse(
            handled=True,
            text=reviewed,
            intent=route.intent,
            strategy=route.strategy,
            tool_calls=tool_calls or [],
            requires_confirmation=requires_confirmation,
            audit=checks,
            presentation=presentation or {},
        )

    def _handle_styling(self, text, user_id, owner, attachment_id=None, conversation_id=None, model_only_followup=False):
        from .styling.service import get_styling_service
        from .styling.engine import parse_feedback, aesthetic_advice, normalize_styling_text
        from .styling.vision import StylingError
        service = get_styling_service()
        text = normalize_styling_text(text)
        route = RouteResult('styling', 'react' if attachment_id else 'plan_and_solve', 0.97, {})
        def finish(response):
            response.presentation.update(conversation_id=conversation_id, username=user_id)
            service.publish(owner, response)
            return response

        with service.turn_lock(owner):
            state = service.conversation(owner) or {}
            calls = []
            current = service.latest(owner)
            if any(word in text for word in ('比较', '对比', '哪个更', '哪件更')) and len(re.findall(r'\bYZREF-[A-Z0-9]+', text.upper())) >= 2:
                result = self.registry.execute('recommend_outfit', query=text, demo_session_id=owner)
                return self._finalize(route=route, text=result['message'], evidence=result.get('data'),
                                      tool_calls=[self._tool_call('recommend_outfit', {'query': text}, result)])
            options = dict(state.get('options') or (current or {}).get('result', {}).get('options') or service.preferences.get(owner))
            observation = state['observation'] if 'observation' in state else (current or {}).get('result', {}).get('observation')
            if current and (current['job_id'] != state.get('plan_job_id') or current.get('revision') != state.get('plan_revision')):
                options = dict(current['result']['options'])
                observation = current['result'].get('observation')
                state['personal_asked'] = True
            if any(word in text for word in ('取消搭配', '结束穿搭', '不搭了', '先不买', '暂时不买', '不想买', '先看看', '先不选')):
                service.update_conversation(owner, active=False, waiting=[])
                response = self._finalize(route=route, text='当然，先穿已有衣服也很好。搭配任务已暂停，什么时候想继续再叫我。', presentation={'kind': 'styling', 'suggestions': ['重新开始穿搭']})
                return finish(response)
            if '重新开始穿搭' in text:
                options, observation, state = {}, None, {}
                service.update_conversation(owner, options={}, observation=None, waiting=[], personal_asked=False, keep_product_ids=[])
            if any(word in text for word in ('不根据照片', '不用照片', '忽略照片', '不看照片')):
                observation = None
                service.update_conversation(owner, observation=None)
            try:
                options = parse_feedback(text, options)
                waiting = state.get('waiting', [])
                bare = re.fullmatch(r'\s*(\d{2,5}(?:\.\d+)?)\s*(?:元|块)?\s*[。！]?\s*', text)
                if bare and waiting == ['budget']:
                    options['budget'] = float(bare[1])
                elif bare and 'profile' in waiting and 120 <= float(bare[1]) <= 220:
                    options['height_cm'] = float(bare[1])
                from .styling.preferences import clean_preferences
                amount = re.search(r'(?<!\d)(\d{2,5}(?:\.\d+)?)\s*(?:元|块)(?!以内|预算)', text)
                if amount and ('budget' in waiting or 'scene' in options) and 'budget' not in parse_feedback(text):
                    options['budget'] = float(amount[1])
                # A season correction replaces the old estimated temperature unless actual degrees are supplied.
                if 'season' in parse_feedback(text) and not re.search(r'度|℃', text):
                    options.pop('temperature', None)
                options = clean_preferences(options)
            except StylingError as exc:
                response = self._finalize(route=route, text=str(exc), tool_calls=calls, presentation={'kind': 'styling', 'suggestions': ['预算300']})
                return finish(response)
            skip = any(word in text for word in ('跳过', '不提供', '不想提供', '不想说', '不方便', '直接推荐', '你决定', '你看着办', '按默认', '随便', '都行'))
            if skip:
                options.setdefault('scene', '日常')
                options.setdefault('budget', 300)
            opinion_only = any(word in text for word in ('只评价', '只看配色', '不推荐商品', '好看吗'))
            from .styling.workflow import StylingWorkflow
            outcome = StylingWorkflow(self.registry, self.styling_planner).run(
                text=text, owner=owner, attachment_id=attachment_id, options=options,
                observation=observation, current=current, state=state, skip=skip, opinion_only=opinion_only,
                model_only_followup=model_only_followup)
            options, observation = outcome.options, outcome.observation
            calls = [self._tool_call(name, arguments, result) for name, arguments, result in outcome.executions]
            workflow_audit = outcome.audit
            metadata = {'decision_mode': 'model' if outcome.used_model else 'rule',
                        'planner_fallback': outcome.fallback, 'fallback_reason': outcome.fallback_reason,
                        'workflow_engine': outcome.workflow_engine, 'workflow_nodes': outcome.graph_nodes}
            route = RouteResult('styling', 'model_react' if outcome.used_model else
                                ('react' if attachment_id else 'plan_and_solve'), route.confidence, {})
            if outcome.failed:
                return finish(self._finalize(route=route, text=outcome.result['message'], tool_calls=calls,
                    audit=workflow_audit, presentation={'kind': 'styling', 'phase': 'failed', 'plan': None,
                                                       'suggestions': ['先用文字推荐穿搭'], **metadata}))
            if outcome.action == 'handoff':
                service.update_conversation(owner, active=False)
                return AgentResponse(handled=False, audit=workflow_audit)
            if outcome.action == 'pause':
                service.update_conversation(owner, active=False, waiting=[])
                return finish(self._finalize(route=route, text='好的，先看看也可以。需要时我再继续帮你选。',
                    audit=workflow_audit, presentation={'kind': 'styling', 'phase': 'paused', **metadata}))
            missing = [key for key in outcome.waiting if key in ('scene', 'budget')]
            ask_profile = outcome.waiting == ['profile']
            advice = aesthetic_advice(observation, options)
            suggestions = []
            if outcome.action == 'opinion':
                service.update_conversation(owner, options=options, observation=observation, waiting=[], active=True)
                response = self._finalize(route=route, text=('\n'.join(advice) + '\n这是对服装搭配的建议；如果我看错了颜色或款式，告诉我就能调整。' if observation else
                    '可以直接在聊天框旁上传穿着照或衣服照，我再给具体的配色建议；还没看到照片时我不会猜测。'),
                    tool_calls=calls, presentation={'kind': 'styling', 'aesthetic_advice': advice,
                                                  'suggestions': ['用已有衣服帮我搭配', '给我通勤穿搭，预算300']})
            elif missing:
                question = ('你主要想在什么场合穿，整套预算大约多少？比如“通勤，300元”。' if len(missing) == 2 else
                            '整套预算大约多少？告诉我一个金额就好。' if missing == ['budget'] else '主要是通勤、日常还是约会穿？')
                suggestions = ['通勤，预算300', '日常，预算200', '直接推荐'] if len(missing) == 2 else ['300元', '200元', '直接推荐'] if missing == ['budget'] else ['通勤', '日常', '约会']
                service.update_conversation(owner, options=options, observation=observation, waiting=missing, active=True, collected=True)
                answer = (advice[0] + '\n' if attachment_id and advice else '我来帮你一起挑。' if not state else '') + question
                response = self._finalize(route=route, text=answer, tool_calls=calls,
                    audit=['Agent 发现场景或预算缺失，暂不调用商品搭配工具'],
                    presentation={'kind': 'styling', 'phase': 'collecting', 'suggestions': suggestions, 'aesthetic_advice': advice})
            elif ask_profile:
                service.update_conversation(owner, options=options, observation=observation, waiting=['profile'], active=True, personal_asked=True)
                response = self._finalize(route=route, text='这套按什么季节或气温穿？也可以告诉我身高、你自己描述的体型，或喜欢宽松还是合身；不想提供可以说“跳过”。',
                    tool_calls=calls, presentation={'kind': 'styling', 'phase': 'collecting', 'suggestions': ['秋季，喜欢宽松', '夏季，喜欢合身', '跳过，直接推荐'], 'aesthetic_advice': advice})
            else:
                service.update_conversation(owner, options=options, observation=observation, waiting=[], active=True, personal_asked=True)
                result = outcome.result
                data = result.get('data') or {}
                if result.get('ok'):
                    remembered = service.latest(owner)
                    service.update_conversation(owner, options=data['options'], observation=data.get('observation'),
                                                plan_job_id=remembered['job_id'] if remembered else None,
                                                plan_revision=remembered.get('revision') if remembered else None,
                                                keep_product_ids=outcome.keep_ids)
                # A task with enough information executes immediately instead of forcing a questionnaire.
                response = self._finalize(route=route, text=result['message'], tool_calls=calls, evidence=data,
                    audit=['Agent 按视觉证据与会话需求调用搭配工具，再审核预算、商品事实及尺码依据'],
                    presentation={'kind': 'styling', 'phase': 'recommended' if result.get('ok') else 'failed',
                                  'plan': data if result.get('ok') else None,
                                  'suggestions': ['太正式了', '便宜一点', '保留裤子', '先不买']})
            response.audit = workflow_audit + response.audit
            response.presentation.update(metadata)
            return finish(response)

    def _handle_product(self, text: str, user_id: str, route: RouteResult) -> AgentResponse:
        args = {"query": text, "product_id": route.entities.get("product_id")}
        result = self.registry.execute("query_product", **args)
        data = result.get("data") or {}
        if isinstance(data, dict) and data.get("product_id"):
            self.memory.remember(user_id, route.intent, product_id=data["product_id"])
        return self._finalize(
            route=route,
            text=result["message"],
            tool_calls=[self._tool_call("query_product", args, result)],
            evidence=data if isinstance(data, dict) else {},
        )

    def _handle_size_recommendation(self, text: str, route: RouteResult) -> AgentResponse:
        args = {
            key: route.entities.get(key)
            for key in (
                "product_id",
                "height_cm",
                "weight_kg",
                "chest_cm",
                "shoulder_cm",
                "waist_cm",
                "hip_cm",
                "fit_preference",
            )
            if route.entities.get(key) is not None
        }
        if not route.entities.get("product_id"):
            args["query"] = text
        result = self.registry.execute("recommend_size", **args)
        return self._finalize(
            route=route,
            text=result["message"],
            tool_calls=[self._tool_call("recommend_size", args, result)],
            evidence=result.get("data") or {},
        )

    def _handle_order(
        self,
        user_id: str,
        route: RouteResult,
        demo_session_id: str | None,
        memory_user_id: str,
    ) -> AgentResponse:
        order_id = self._resolve_order_id(user_id, route)
        args = {
            "order_id": order_id,
            "user_id": user_id,
            "demo_session_id": demo_session_id,
        }
        result = self.registry.execute("query_order", **args)
        if result.get("ok") and order_id:
            self.memory.remember(memory_user_id, route.intent, order_id=order_id)
        observation = (
            f"订单核验成功，状态={((result.get('data') or {}).get('status') or 'unknown')}"
            if result.get("ok")
            else f"订单核验失败（{result.get('error') or 'unknown_error'}），执行已停止"
        )
        return self._finalize(
            route=route,
            text=result["message"],
            tool_calls=[self._tool_call("query_order", args, result)],
            evidence=result.get("data") or {},
            audit=["Action: query_order", f"Observation: {observation}"],
        )

    def _handle_logistics(
        self,
        user_id: str,
        route: RouteResult,
        demo_session_id: str | None,
        memory_user_id: str,
    ) -> AgentResponse:
        order_id = self._resolve_order_id(user_id, route)
        args = {
            "order_id": order_id,
            "user_id": user_id,
            "demo_session_id": demo_session_id,
        }
        def summarize_observation(action: ReActAction, result: dict) -> str:
            if not result.get("ok"):
                return f"{action.tool} 失败（{result.get('error') or 'unknown_error'}），执行已停止"
            if action.tool == "query_order":
                status = ((result.get("data") or {}).get("status") or "unknown")
                return f"订单归属核验通过，状态={status}"
            return "物流信息查询成功"

        def decide_next(action: ReActAction, result: dict) -> ReActDecision:
            if action.tool == "query_logistics":
                return ReActDecision(final_message=result["message"])

            order_data = result.get("data") or {}
            status = order_data.get("status") if isinstance(order_data, dict) else None
            if status in {"demo_created", "awaiting_demo_fulfillment", "pending_payment"}:
                detail = "订单尚未发货，暂无物流轨迹。"
            elif status == "cancelled":
                detail = "订单已取消，当前无需查询物流。"
            elif status in {"shipped", "in_transit", "delivered", "closed", "completed"}:
                return ReActDecision(
                    next_action=ReActAction(
                        tool="query_logistics",
                        arguments=args,
                        thought_summary="订单归属已核验且状态允许查询物流，读取演示物流进展",
                    )
                )
            else:
                detail = "暂无法根据订单状态确认是否已发货，请转接人工核对物流。"
            return ReActDecision(final_message=f"{result['message']}\n{detail}")

        executor = MiniReActExecutor(self.registry, max_steps=2)
        outcome = executor.run(
            ReActAction(
                tool="query_order",
                arguments=args,
                thought_summary="先核验订单归属与当前状态，避免越权或无效的物流查询",
            ),
            decide_next=decide_next,
            summarize_observation=summarize_observation,
        )
        calls = [self._tool_call(action.tool, action.arguments, result) for action, result in outcome.executions]
        if outcome.final_result.get("ok") and order_id:
            self.memory.remember(memory_user_id, route.intent, order_id=order_id)
        return self._finalize(
            route=route,
            text=outcome.final_message,
            tool_calls=calls,
            evidence=outcome.final_result.get("data") or {},
            audit=outcome.audit,
        )

    def _handle_policy(self, text: str, route: RouteResult) -> AgentResponse:
        args = {"query": text, "concise": True}
        result = self.registry.execute("query_policy", **args)
        return self._finalize(
            route=route,
            text=result["message"],
            tool_calls=[self._tool_call("query_policy", args, result)],
            evidence=result.get("data") or {},
        )

    def _handle_after_sales(
        self,
        text: str,
        user_id: str,
        route: RouteResult,
        demo_session_id: str | None,
    ) -> AgentResponse:
        calls: list[dict] = []
        audit = [
            "Plan: 固定规则规划（非 LLM 自动规划）",
            "Plan: 步骤1：如提供订单号，核验当前用户或演示会话的订单归属",
            "Plan: 步骤2：根据售后诉求选择并查询对应政策",
        ]
        order_id = self._resolve_order_id(user_id, route)
        order_result = None
        if order_id:
            order_args = {
                "order_id": order_id,
                "user_id": user_id,
                "demo_session_id": demo_session_id,
            }
            order_result = self.registry.execute("query_order", **order_args)
            calls.append(self._tool_call("query_order", order_args, order_result))
            audit.append("Action: query_order")
            audit.append(
                "Observation: 订单核验成功"
                if order_result.get("ok")
                else f"Observation: 订单核验失败（{order_result.get('error') or 'unknown_error'}）"
            )
        else:
            audit.append("Observation: 未提供订单号，跳过订单核验并仅回答通用政策")

        order_data = (order_result or {}).get("data") or {}
        large_return = (
            bool(order_result and order_result.get("ok"))
            and "退货" in text
            and needs_large_order_review(order_data)
        )
        quality_issue = any(word in text for word in ("坏了", "破损", "损坏", "故障", "瑕疵", "质量问题", "错发", "漏发"))
        policy_args = {"query": text, "concise": True}
        if quality_issue:
            policy_args["policy_id"] = "quality_after_sales"
        elif large_return:
            policy_args["policy_id"] = "large_order_return_review"
        elif "退货" in text or "无理由" in text:
            policy_args["policy_id"] = "seven_day_no_reason"
        policy_result = self.registry.execute("query_policy", **policy_args)
        calls.append(self._tool_call("query_policy", policy_args, policy_result))
        audit.append("Action: query_policy")
        audit.append(
            "Observation: 售后政策查询成功"
            if policy_result.get("ok")
            else f"Observation: 售后政策查询失败（{policy_result.get('error') or 'unknown_error'}）"
        )

        parts = []
        if order_result and order_result.get("ok"):
            parts.append(f"已核对订单 {order_id}（演示）。")
        elif order_result:
            parts.append(order_result["message"])
        parts.append(policy_result["message"])
        if large_return and quality_issue:
            parts.append("这单也达到演示大单阈值，退货须人工复核；不能因此拒绝质量售后。")
        if not order_id:
            parts.append("如需核对具体售后，请提供订单号；不要发送验证码或支付密码。")

        return self._finalize(
            route=route,
            text="\n".join(parts),
            tool_calls=calls,
            evidence={"order": order_data, "policy": policy_result.get("data")},
            audit=audit,
        )

    def _handle_high_risk_action(
        self,
        text: str,
        user_id: str,
        route: RouteResult,
        demo_session_id: str | None,
        memory_user_id: str,
    ) -> AgentResponse:
        order_id = self._resolve_order_id(user_id, route)
        pending = self.memory.get_pending_action(memory_user_id)
        confirmation_match = self._confirmation_pattern.fullmatch((text or "").strip())
        operation = next((word for word in ("退货", "退款", "取消", "地址") if word in text), None)
        operation = operation or (pending or {}).get("operation", "退款")

        if confirmation_match and pending and pending.get("order_id"):
            confirmed_order = confirmation_match.group(1).upper()
            pending_order = (pending.get("order_id") or "").upper()
            if not pending_order or confirmed_order != pending_order:
                return self._finalize(
                    route=route,
                    text=f"确认订单号与待处理订单不一致。请明确回复“确认处理 {pending_order or '订单号'}”。",
                    requires_confirmation=True,
                    audit=["二次确认失败：订单号不匹配"],
                )

            order_args = {
                "order_id": confirmed_order,
                "user_id": user_id,
                "demo_session_id": demo_session_id,
            }
            order_result = self.registry.execute("query_order", **order_args)
            call = self._tool_call("query_order", order_args, order_result)
            if not order_result.get("ok"):
                return self._finalize(route=route, text=order_result["message"], tool_calls=[call])

            self.memory.set_pending_action(memory_user_id, None)
            message = (
                f"已记录你对订单 {confirmed_order} 的明确确认。"
                "当前 Stage 1 使用模拟数据，尚未接入真实退款、取消订单或修改地址接口，因此我不会声称操作已完成。"
                "请转接人工客服执行真实操作。"
            )
            return self._finalize(
                route=route,
                text=message,
                tool_calls=[call],
                requires_confirmation=False,
                evidence={"write_operation_confirmed": False},
                audit=["已完成二次确认，但未执行真实交易写操作"],
            )

        if not order_id:
            message = (
                "退款、取消订单或修改地址会改变交易状态，需要先核对订单并获得你的明确确认。"
                "请先提供订单号；不要发送支付密码、短信验证码或完整银行卡信息。"
            )
            self.memory.set_pending_action(memory_user_id, {"action": route.intent, "order_id": "", "operation": operation})
            return self._finalize(
                route=route,
                text=message,
                requires_confirmation=True,
                audit=["高风险操作：缺少订单号，未执行"],
            )

        order_args = {
            "order_id": order_id,
            "user_id": user_id,
            "demo_session_id": demo_session_id,
        }
        order_result = self.registry.execute("query_order", **order_args)
        calls = [self._tool_call("query_order", order_args, order_result)]
        if not order_result.get("ok"):
            return self._finalize(route=route, text=order_result["message"], tool_calls=calls)

        if operation == "退货" and needs_large_order_review(order_result.get("data") or {}):
            self.memory.set_pending_action(memory_user_id, None)
            return self._finalize(
                route=route,
                text=(
                    f"已核对订单 {order_id}。这单达到演示大单阈值，退货需人工复核，"
                    "但不会因此拒绝七日无理由或质量售后。当前未接入真实人工渠道，"
                    "请通过原店铺联系人工处理；我没有执行退款。"
                ),
                tool_calls=calls,
                evidence=order_result.get("data") or {},
                audit=["演示大单退货：已核验订单并提示人工复核，未执行交易操作"],
            )

        self.memory.set_pending_action(memory_user_id, {"action": route.intent, "order_id": order_id, "operation": operation})
        message = (
            f"我已核对到订单 {order_id}。这是会改变交易状态的操作，请你再次明确回复“确认处理 {order_id}”。"
            "当前 Stage 1 只会记录确认并转人工，不会执行真实退款、取消或地址修改。"
        )
        return self._finalize(
            route=route,
            text=message,
            tool_calls=calls,
            requires_confirmation=True,
            evidence=order_result.get("data") or {},
            audit=["高风险操作：已核对订单，等待二次确认"],
        )

    def handle(
        self,
        text: str,
        *,
        user_id: str = "User",
        demo_session_id: str | None = None,
        product_id: str | None = None,
        attachment_id: str | None = None,
        conversation_id: str | None = None,
    ) -> AgentResponse:
        route = self.router.route(text)
        model_only_followup = False
        from .styling.service import get_styling_service
        styling_owner = demo_session_id or f'voice:{user_id}'
        style_service = get_styling_service() if attachment_id or route.intent in {'styling', 'general', 'product_query', 'size_recommendation'} else None
        state = style_service.conversation(styling_owner) if style_service else None
        if attachment_id:
            route = RouteResult('styling', 'react', 0.99, route.entities)
        elif state and state.get('active') and route.intent in {'general', 'product_query', 'size_recommendation'} and not (product_id or route.entities.get('product_id')):
            from .styling.engine import parse_feedback
            try:
                relevant = bool(parse_feedback(text))
            except ValueError:
                relevant = True
            relevant = relevant or bool(re.fullmatch(r'\s*(?:\d{2,5}(?:\.\d+)?|[一二两三四五六七八九十百千零]+)\s*(?:元|块)?[。！]?\s*', text))
            from .styling.workflow import item_edits, CHEAPER
            relevant = relevant or any(item_edits(text)) or bool(re.search(CHEAPER, text))
            relevant = relevant or any(word in text for word in ('跳过', '不提供', '不想说', '不方便', '直接推荐', '你决定', '你看着办', '按默认', '先不买', '先看看', '先不选', '取消搭配', '结束穿搭', '不搭了', '这套', '好看吗', '这身', '只评价', '只看配色', '配色怎么样'))
            if relevant or self.styling_planner is not None:
                model_only_followup = not relevant
                route = RouteResult('styling', 'plan_and_solve', 0.96, route.entities)
            else:
                style_service.update_conversation(styling_owner, active=False)
        # Resolve concise follow-ups only when this browser has a completed outfit.
        if demo_session_id and route.intent in {"general", "product_query", "size_recommendation"} and not (
            product_id or route.entities.get("product_id")
        ) and (any(word in text for word in ("预算", "便宜一点", "便宜点", "不喜欢", "换成", "宽松", "通勤", "面试", "约会", "旅行", "运动", "简约", "街头", "复古", "先不买", "暂时不买"))
               or re.search(r"(?:不要|喜欢|偏好|避开|排除)[黑白灰蓝棕米绿红粉紫黄]", text)):
            from .styling.service import get_styling_service
            if get_styling_service().latest(demo_session_id):
                route = RouteResult("styling", "plan_and_solve", 0.95, route.entities)
        memory_user_id = self._memory_user_id(user_id, demo_session_id)
        if product_id is not None and route.intent in {"product_query", "size_recommendation"}:
            verified_id = self.verified_product_context_id(product_id)
            if verified_id is None:
                return AgentResponse(
                    handled=True,
                    text="当前商品不在已核验的本地商品目录中，请刷新商品列表后重试。",
                    intent=route.intent,
                    strategy=route.strategy,
                    audit=["忽略无效或已失效的商品卡上下文"],
                )
            if not route.entities.get("product_id"):
                entities = dict(route.entities)
                entities["product_id"] = verified_id
                route = RouteResult(route.intent, route.strategy, route.confidence, entities)
        route = self._reuse_recent_product(text, memory_user_id, route)
        pending = self.memory.get_pending_action(memory_user_id)
        confirmation_match = self._confirmation_pattern.fullmatch((text or "").strip())
        if pending and (text or "").strip().rstrip("。！!") in {"取消本次操作", "取消本次申请", "不退了", "算了", "停止处理"}:
            self.memory.set_pending_action(memory_user_id, None)
            return AgentResponse(handled=True, text="已取消本次待确认申请，没有执行交易操作。", intent="after_sales_action", strategy="plan_and_solve")
        if pending and confirmation_match:
            entities = dict(route.entities)
            if pending.get("order_id") and "order_id" not in entities:
                entities["order_id"] = pending["order_id"]
            route = RouteResult("after_sales_action", "plan_and_solve", 0.99, entities)
        elif pending and route.entities.get("order_id") and re.fullmatch(
            r"\s*(?:订单号?|是|我的订单号是|订单号是)?\s*[:：]?\s*" + re.escape(route.entities["order_id"]) + r"[。！!]?\s*", text, re.IGNORECASE
        ):
            route = RouteResult("after_sales_action", "plan_and_solve", 0.99, route.entities)
        elif pending and any(word in (text or "") for word in ("确认", "确定", "同意继续")):
            pending_order = pending.get("order_id") or "订单号"
            return AgentResponse(
                handled=True,
                text=f"我没有把这句话视为交易确认。若要继续，请单独明确回复“确认处理 {pending_order}”。",
                intent="after_sales_action",
                strategy="plan_and_solve",
                requires_confirmation=True,
                audit=["拒绝了不符合格式或含义不明确的二次确认"],
            )
        self._remember_route(memory_user_id, route)

        if route.intent == "styling":
            return self._handle_styling(text, user_id, styling_owner, attachment_id, conversation_id, model_only_followup)
        if route.intent == "product_query":
            return self._handle_product(text, memory_user_id, route)
        if route.intent == "size_recommendation":
            return self._handle_size_recommendation(text, route)
        if route.intent == "order_query":
            return self._handle_order(user_id, route, demo_session_id, memory_user_id)
        if route.intent == "logistics_query":
            return self._handle_logistics(user_id, route, demo_session_id, memory_user_id)
        if route.intent == "policy_query":
            return self._handle_policy(text, route)
        if route.intent == "after_sales":
            return self._handle_after_sales(text, user_id, route, demo_session_id)
        if route.intent == "after_sales_action":
            return self._handle_high_risk_action(text, user_id, route, demo_session_id, memory_user_id)

        return AgentResponse(
            handled=False,
            intent=route.intent,
            strategy=route.strategy,
            audit=["未命中确定性电商路由，交还 Fay LLM 链路"],
        )
