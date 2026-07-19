# Character Asset Guidelines

## 夏目心角色图

- 固定路径：`desktop/frontend/src/assets/characters/natsume-kokoro.png`
- 格式：带真实透明通道的 PNG
- 标准尺寸：`1200 × 1600 px`
- 标准比例：`3:4`
- 构图：半身或 2/3 身，完整保留头顶、头发和身体主体

替换角色图时，使用同名、同尺寸的透明 PNG 覆盖上述文件即可，不需要修改 React 组件或 CSS。组件通过 `characterAssets.ts` 集中引用资源，并使用稳定的 `3:4` 容器、`object-fit: contain` 和 `object-position: center bottom`，不会裁切人物主体。

安全区建议：头顶、发梢、肩部和身体两侧不要紧贴画布边缘。不要使用带棋盘格的伪透明图片、JPEG、横图、过度留白、只包含面部的近景或需要 `cover` 才能填满容器的构图。

图片加载失败时会保留原卡片比例并显示占位状态；开发环境同时输出资源加载错误。
