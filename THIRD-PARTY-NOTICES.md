# 第三方声明（Third-Party Notices）

本仓库（`astrbot_plugin_our_free.model`）自身以 **MIT** 许可发布，见 `LICENSE`。
它**不包含**下列任何项目的源代码，仅通过公开接口或公开插件 API 与之交互；为清晰起见逐项说明。

## 1. Our Free Model（`Ebony-Vinyl/dsh-our-free-model`）

- 许可：**MIT**，`Copyright (c) 2026 Ebony-Vinyl`
- 关系：本插件是该项目的**外部客户端**。所有模型请求与数据读取都经由其 HTTP 接口完成：
  - `GET /health`、`GET /v1/models`
  - `GET /api/management/summary`、`GET /api/management/stats`、`POST /api/management/models/refresh`
  - 读取其数据目录下的 `settings.json`（取 `forwardKey` / `standalonePort`）、`stats.json`、`feed.json`
- 未复制代码：仓库内没有任何来自该项目的源文件片段。插件中的模型 id 形态推断（`*-free`、`combo:`、`provider/model`、`kilo-auto/free`）、思考强度尾缀 `id(level)` 等，来自对其**公开 HTTP 行为与 README** 的观察，而非代码搬运。
- 若你日后把该项目的代码片段复制进本仓库，必须保留其 MIT 版权声明与许可全文（MIT 第 1 条）。上游 MIT 全文见
  <https://github.com/Ebony-Vinyl/dsh-our-free-model/blob/main/LICENSE>。

## 2. AstrBot（`AstrBotDevs/AstrBot`）

- 许可：**GNU AGPL-3.0**
- 关系：本插件是运行在 AstrBot 进程内的第三方插件，仅 `import` 其公开插件 API：
  `astrbot.api.star`（`Star`、`Context`）、`astrbot.api.event`（`AstrMessageEvent`、`filter`）、`astrbot.api.logger`。
- 未复制代码：仓库内没有任何来自 AstrBot 的源文件片段；命令组、钩子、配置 schema 的写法遵循其公开文档（`docs/zh/dev/star/*`）与官方示例的调用约定。
- 许可边界（据实说明，非法律意见）：
  - 本插件与 AstrBot 是**分别发布**的两个作品，本仓库以 MIT 授权；
  - 但如果你把本插件与 AstrBot 一起**再分发**，或分发你修改过的 AstrBot，那么对 AstrBot 部分（以及可能的组合作品）仍需履行 AGPL-3.0 的义务（提供完整对应源码、保留许可与声明、向网络用户提供源码获取方式）。
  - 只在服务器上自用、不与 AstrBot 一并分发本插件时，本仓库的 MIT 条款即可覆盖本插件自身。

## 3. 运行时依赖

插件不引入额外 Python 依赖。运行时使用的 `httpx` 由 AstrBot 自身依赖提供（BSD-3-Clause），版权归其各自作者。

## 4. 测试依赖

`tests/` 使用 `pytest`（MIT）与真实 `httpx`；`tests/stubs/astrbot/` 是为测试而**独立编写**的最小桩，不复用 AstrBot 代码。
