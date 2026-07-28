# 司忆 v8.0.3 本地版本说明

## 变更

- 主仓库收敛为 Windows PC/Tauri 单一产品路径。
- 移除 Vite PWA 插件、manifest、service worker 构建配置和 PWA 图标。
- 移除浏览器 API 地址、页面内存访问令牌与网页密钥降级界面。
- 后端部署模式固定为 `desktop_local`，移除 `local_web`、`web_control` 和 `cloud_executor`。
- 非 PC 端原始实现归档到相邻的 `../Agent非PC端`。
- 保留桌面启动时清理历史 PWA 缓存的兼容逻辑，避免旧缓存干扰 PC 升级。

## 状态

本次按用户要求不运行测试、不构建安装包、不推送 GitHub。v8.0.3 是本地源码版本，不构成经过验证的分发发布。
