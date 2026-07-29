# v9.6.0 视觉数据可靠性审计

结论：`PASS WITH LIMITATIONS`。

- 图片来源限定在规范化工作区路径；签名与实际解码共同验证，扩展名不能单独决定类型。
- 发送前重新编码并确认 EXIF 清空；审计只保存哈希、尺寸、处理动作和 Provider，不保存图片正文或 base64。
- 模型输出合同区分 observations、inferences、uncertainties 和 confidence；非结构化输出明确标记未知置信度。
- 无视觉模型返回 `unsupported/degraded`，不会用占位文本或代码存在性伪造视觉结论。
- GLM-4.6V 真实场景已验证可见颜色、形状、文字、图表数值/趋势及 UI 错误信息。
- 限制：Provider 精确 Token 与费用没有透传到本次测试报告，记为“无法准确计算”，不得估算。
