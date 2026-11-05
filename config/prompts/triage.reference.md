# 分级 prompt 的参考实现原文（留档）
#
# 来源：albertorsesc/pain-discovery — pain_discovery/scanner.py:triage_signal
#
# ⚠️ **不要直接使用这份 prompt。** 它的分级定义与我们不同：
#
#   参考实现          我们的定义
#   ---------------   -----------------------------------
#   C = VENTING ONLY  C = 隐含需求/含蓄抱怨
#   D = PROFESSIONAL  D = 弱信号（仅情绪表达）
#   （专业讨论）       （行业讨论归入 E 噪音）
#
# 参考实现让 LLM 只回一个字母；我们改成 JSON 输出（多了 pain_score /
# is_pain / pain_type / target_user / willingness_to_pay / reason），
# 校验交给 validator.py。
#
# 实际使用的版本见同目录的 triage.md。

Classify this post. Reply with ONLY one letter.

POST TITLE: {title}
POST BODY: {body}

A = ACTIONABLE PAIN: Person has a specific problem they'd pay to solve — spending decision, broken workflow, tool frustration, manual process needing automation, scaling bottleneck, bad software, or unmet need
B = INFO SEEKING: Wants to learn or compare but no strong frustration or urgency
C = VENTING ONLY: Frustrated about the past, not seeking solutions or making decisions
D = PROFESSIONAL DISCUSSION: Industry worker discussing work abstractly, no personal pain
E = NOISE: Casual question, meme, hobby chat, no business opportunity signal

Reply with ONLY the letter (A/B/C/D/E):
