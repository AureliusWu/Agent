# Character Asset Guidelines

## 夏目心角色图

- 仓库默认资源：`desktop/frontend/src/assets/characters/kokoro-placeholder.svg`
- 仓库、Git 历史、安装源码和测试 Artifact 禁止包含私人角色图片。
- 管理员自定义图片存放在本机 AppData 的 `Siyi/resources/user-assets/`，不得复制回源码目录。
- 推荐格式：带真实透明通道的 PNG。
- 推荐尺寸：`1200 × 1600 px`。
- 推荐比例：`3:4`。
- 构图：半身或 2/3 身，完整保留头顶、头发和身体主体。

组件通过 `characterAssets.ts` 集中引用可公开再分发的默认占位资源。管理员本地覆盖由桌面运行时从 AppData 解析，源码和前端构建不得把本地图片打包进安装程序。

安全区建议：头顶、发梢、肩部和身体两侧不要紧贴画布边缘。不要使用带棋盘格的伪透明图片、JPEG、横图、过度留白、只包含面部的近景或需要 `cover` 才能填满容器的构图。

本地覆盖不存在、未授权或加载失败时必须回退到占位 SVG，并保留原卡片比例；开发环境可以输出不含本机绝对路径的资源加载错误。
