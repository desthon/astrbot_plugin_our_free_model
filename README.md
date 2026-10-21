# astrbot_plugin_our_free_model

把本机 **Our Free Model（OFM）独立服务** 接进 AstrBot 的运维接入插件。

它**不是**一个 provider 适配器，也**不改写** AstrBot 的任何配置文件：

* 只通过 OFM 的外部契约工作（`/health`、`/v1/models`、`/api/management/*`、数据目录里的 `settings.json` / `stats.json` / `feed.json`）；
* `/ofm setup` 只把「新建提供商」需要的 4 个字段打印出来，由你手工粘贴；
* 全部命令**仅管理员**可用，且不提供 `llm_tool`（否则非管理员可以经 LLM 侧触发命令，属于权限旁路）。

| 形态 | 端口 | 插件支持情况 |
| --- | --- | --- |
| A. 同机 standalone（`npm run start:standalone`） | 18900（持久化在 `settings.json` 的 `standalonePort`） | **支持**：`/v1` + 全量 management API + 统计 + 公告 |
| B. dsh 插件网关（18899） | 18899 | **识别并降级**：只能聊天，管理面被 trust fence 挡住，`/ofm usage` 回落 `stats.json` |

---

## 1. 安装

环境要求：AstrBot `>=4.13.0,<5`，Python 3.12+。**不新增任何第三方依赖**（`httpx` 是 AstrBot 主依赖）。

```bash
# 1) 把整个目录放进 AstrBot 的插件目录
cp -r astrbot_plugin_our_free_model <AstrBot 根目录>/data/plugins/

# 2) 目录结构应是这样
# <AstrBot>/data/plugins/astrbot_plugin_our_free_model/
# ├── metadata.yaml
# ├── _conf_schema.json
# ├── main.py
# ├── README.md
# └── core/{__init__,endpoints,secrets,ofm_client,catalog,session,stats,feed}.py

# 3) 重启 AstrBot（或在 WebUI 的插件页重载）
```

装好之后的验证顺序：`/ofm doctor` → `/ofm models` → `/ofm use <序号>` → 正常对话。

前置条件（很重要，缺一样都会 503/401）：

1. OFM 独立服务在跑：`npm run start:standalone`（默认 `127.0.0.1:18900`，只监听回环）；
2. OFM 里 **设置 → 本地转发** 已打开并保存——否则除 `/health` 外一律 503；
3. 打开转发之后 `settings.json` 才会出现顶层 `forwardKey`（惰性铸造），插件自动读它。

如果 OFM 的数据目录不在默认位置（`$OFM_HOME` 或 `~/.our-free-model`），启动 AstrBot 时把 `OFM_HOME` 指过去：

```bash
OFM_HOME=/srv/our-free-model python3 main.py   # 或写进 systemd service 的 Environment=
```

---

## 2. 配置项（WebUI → 插件 → 配置）

| 键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `base_url` | string | 空 | 手动指定网关地址（`http://127.0.0.1:18900`，不要带 `/v1`）。留空 = 读 `settings.json` 的 `standalonePort` 自动发现，并向后扫 10 个端口 |
| `api_key` | string（`secret`） | 空 | 手动覆盖 `forwardKey`。留空 = 读 `settings.json` |
| `request_timeout` | int | 120 | 访问 OFM 的超时（秒） |
| `diagnose_timeout` | int | 3 | 端点探测 / health 检查的单次超时 |
| `models_ttl` | int | 600 | 模型目录缓存（秒） |
| `stats_ttl` | int | 60 | 用量缓存（秒） |
| `default_model` | string | `combo:recommended-free` | 目录为空时 `/ofm setup` 的兜底模型 |
| `models_page_limit` | int | 30 | `/ofm models` 一屏最多列出的条数 |
| `announce_limit` | int | 3 | `/ofm announce` 默认条数（上限 10） |
| `show_api_key_in_setup` | bool | true | `/ofm setup` 是否打印完整 key |

> ⚠️ `secret: true` 只是 WebUI 的**界面遮罩**，**不会加密落盘**：key 仍以明文存在 AstrBot 的插件配置里。介意这一点就把 `api_key` 留空，让插件每次去读 OFM 的 `settings.json`（那个文件本身是 0600）。

---

## 3. 把 OFM 接成 AstrBot 的提供商（4 个粘贴字段）

先跑 `/ofm setup`，然后把输出填进 **WebUI → 配置 → 提供商 → 新建提供商**：

| 字段 | 填什么 |
| --- | --- |
| 提供商类型 | `OpenAI 兼容`（`openai_chat_completion`） |
| API Base | `<base_url>/v1`，例如 `http://127.0.0.1:18900/v1` |
| Key | OFM 的 `forwardKey` 原值（`/ofm setup` 会打印；群聊场景只贴掩码） |
| 模型 | 当前会话模型，或 `/ofm setup` 推荐的 `combo:` / 首选免费道 |

`/ofm setup` 示例输出：

```
把下面 4 个字段填到 AstrBot WebUI → 配置 → 提供商 → 新建提供商：

提供商类型：OpenAI 兼容（openai / openai_chat_completion）
API Base：http://127.0.0.1:18900/v1
Key：ofm-9f2c1a7b3d5e4f60
模型：combo:recommended-free

说明：
· Key 就是 OFM 的 forwardKey；群聊场景请只贴掩码 ofm…4f60，完整值在 /home/you/.our-free-model/settings.json 的 forwardKey 字段。
· 若 OFM 还没开本地转发，先到 OFM → 设置 → 本地转发 打开并保存，否则所有请求都会 503。
· 想按会话切模型 / 档位用 /ofm use，不用在 WebUI 里建多个 provider。
```

建好之后正常对话走默认模型；想按会话换模型用 `/ofm use`（见下节），**不要**为每个模型都建一个 provider。

---

## 4. 命令参考（全部仅管理员）

命令组前缀 `/ofm`。所有命令都带 `PermissionType.ADMIN`，非管理员不会看到任何回执。

### `/ofm status`

模式、地址、key 指纹、`/health` 版本、模型总数与分组计数、本会话覆盖。

```
Our Free Model 接入状态
模式：standalone（独立服务，管理面可用）
地址：http://127.0.0.1:18900
密钥：ofm…4f60
数据目录：/home/you/.our-free-model
/health：✔ service=our-free-model-standalone version=2.5.8
模型：共 37 个（免费道 21、降级链（combo） 5、聚合渠道 9、Kilo 免费池 1、未分类 1；渠道为推断）
本会话覆盖：combo:recommended-free(deep)
```

### `/ofm models [关键词]`

分渠道列出模型，一行一个 `id · 上下文 · 档位 · 渠道`；超过 `models_page_limit`（默认 30）条时截断，并提示用关键词过滤。

```
OFM 模型目录（匹配 37 / 共 37）
【免费道】21 个
 1. deepseek-v4.1-flash-free · 上下文 1M · 档位 disabled/low/high/max（默认 high） · 渠道 free
 2. qwen3-coder-free · 上下文 256K · 档位 未声明 · 渠道 free
【降级链（combo）】5 个
22. combo:recommended-free · 上下文 125K · 档位 light/balanced/deep（默认 balanced） · 渠道 combo
【聚合渠道】9 个
27. acme/gpt-4o-mini · 上下文 128K · 档位 low/high · 渠道 aggregate
【Kilo 免费池】1 个
36. kilo-auto/free · 上下文 未知 · 档位 未声明 · 渠道 kilo
（只显示前 30 条：用 /ofm models <关键词> 过滤，例如 /ofm models combo。）
渠道是按模型 id 形态推断的，不代表服务端真实标记；能不能用仍以实际请求为准。
用 /ofm use <序号|模型id> [--effort <level>] 切换。
```

`/ofm models combo` 只列降级链；`/ofm models free` 会同时命中 `*-free` 与 `kilo-auto/free`（子串匹配）。

### `/ofm use <模型id|序号> [--effort <level>]`

三种写法：上一屏列表里的序号、完整 id、唯一子串。歧义时会列候选并要求写全。

```
/ofm use 22
→ 已切换本会话模型：combo:recommended-free
  下一轮对话生效，只影响本会话（群聊与私聊互相隔离）。
  可用档位：light、balanced、deep（默认 balanced）

/ofm use deepseek-v4.1-flash-free --effort high
→ 已切换本会话模型：deepseek-v4.1-flash-free(high)
  下一轮对话生效，只影响本会话（群聊与私聊互相隔离）。
  可用档位：disabled、low、high、max（默认 high）

/ofm use combo --effort high
→ 「combo」匹配到 5 个模型，请写完整 id：
  · combo:recommended-free
  · combo:cheap-free
  …
```

档位会按该模型的 `x_ofm_efforts` **强校验**——OFM 对非法档位是静默回落默认档、不报错，所以插件必须自己拦：

```
/ofm effort boom
→ boom 不是 deepseek-v4.1-flash-free 的合法档位（OFM 对非法档位是静默回落，不会报错）。
  可选：disabled、low、high、max（默认 high）。
```

### `/ofm effort <level>`

只改强度，基于当前会话模型校验。`clear` / `off` / `none` / `默认` 清空档位。

```
/ofm effort deep
→ 已设置本会话思考强度：deep
  下一轮请求会以 combo:recommended-free(deep) 下发（尾缀形式，OFM 会 strip 后再查目录。）

/ofm effort            # 不带参数 = 查看当前档位与合法值
思考强度
当前模型：combo:recommended-free
当前档位：deep
合法档位：light、balanced、deep（默认 balanced）
清空档位：/ofm effort clear
```

### `/ofm reset`

```
/ofm reset
→ 已清除本会话覆盖（原为 combo:recommended-free(deep)），回到 provider 默认模型。
```

### `/ofm doctor`

逐项 ✔/✘，每项失败都给下一步动作；任何异常都不会冒泡成栈。

```
Our Free Model 体检
✔ 数据目录：/home/you/.our-free-model
✔ settings.json：可读（/home/you/.our-free-model/settings.json）
✔ forwardKey：存在（ofm…4f60）
✔ 独立服务端口：18900（settings.standalonePort）
✔ 本地转发：已开启（forward.port=18899）
✔ /health：http://127.0.0.1:18900 正常（service=our-free-model-standalone，模式=standalone）
✔ /v1/models：可达，37 个模型
✔ 鉴权：Bearer forwardKey 通过（ofm…4f60）
✔ 管理 API：summary / stats 可用
✔ stats.json：可读（记录 1,284 次请求）
体检完成：✘ 项按箭头提示处理，修完重跑 /ofm doctor。
```

### `/ofm usage`

```
OFM 用量（来源：管理 API（samples 最多 200 条））
请求：1,284（成功 1,251 / 失败 33）
成功率：97.4%
tokens：输入 1.82M / 输出 402.5K（按样本合计，不是全量）
平均速度：41.75 tok/s（样本内有 tps 的请求）
统计只读：插件不写 stats.json。
```

管理 API 不可用（插件网关模式 / 跨机）时自动回落 `stats.json`，来源行显示 `本地 stats.json（samples 最多 400 条环形）`。

### `/ofm setup`

见第 3 节。

### `/ofm announce [n]`

读 `<data_dir>/feed.json` 的最近 n 条公告（默认 `announce_limit`=3，上限 10），正文剥成纯文本。

```
[1] 免费车道抖动（warn · 2024-06-02 11:04）
部分 free 车道 & 上游返回 429，请稍后重试。
[2] v2.5.8 已发布（info · 2024-06-01 08:00）
修复了 combo: 降级链的超时处理。
链接：https://example.invalid/releases
```

没有后台轮询、没有主动推送：公告只在你敲这条命令时读取。

---

## 5. 请求覆盖是怎么生效的

`on_llm_request` 钩子读本会话的 `{model, effort}`，**只在有覆盖时**改写 `req.model`：

```python
req.model = "combo:recommended-free(deep)"   # 尾缀形式
```

原因：AstrBot 的 `extra_body` 没有请求级入口，而 OFM 在协议层就认 `model` 尾缀 `id(level)`（服务端 strip 后查目录，并在 `reasoning_effort` 缺省时用它）。没有覆盖时钩子直接返回，模型保持 provider 默认值；钩子里任何异常只写 `logger.warning`，不影响正常对话。

会话隔离键是 `event.unified_msg_origin`：群 A 的 `/ofm use` 不影响群 B 和私聊。

---

## 6. 限额与已知限制

1. **渠道全是推断**。`/v1/models` 只有 `id/object/created/owned_by/context_window/x_ofm_efforts/x_ofm_effort_default`，没有 name、vision、channel 字段。插件按 id 形态猜：`*-free`→免费道、`combo:`→降级链、`kilo-auto/…`→Kilo、含 `/` 的 `<provider>/<model>`→聚合渠道（EAC 也是 `<org>/<model>`，**无法与聚合严格区分**，所以一律归聚合，不单列 `eac`）、其余→未分类。这只是展示分类，不代表服务端真实标记。
2. **`created` 是每次请求现算的** `Date.now()`，插件从不拿它当缓存键（缓存键是 `base_url`）。
3. **token / 平均速度是按样本合计**。管理 API 的 `samples` 只回 200 条，`stats.json` 的 `samples` 是 400 条环形数组，所以这两项是「最近样本」而不是全量；`requests / failedRequests` 用统计文件顶层的全量计数（缺失时才退回样本计数）。
4. **`secret` 不加密落盘**（见第 2 节）。
5. **只支持 A 形态**。插件网关（18899）只能识别 + 降级：管理面被 trust fence 挡住，`/ofm usage` 回落 `stats.json`，`/ofm doctor` 会明确写「此模式不暴露管理面」。跨机 / 容器同理——management 不转发，只能读挂载进来的数据目录。
6. **不做区域判断**。region-limited 模型和普通模型混在同一个数组里且无标记，能不能用只以实际请求的 `404 model "X" not found` / `Model is unavailable` 为准。
7. **非法档位 OFM 不报错**。它静默回落默认档，所以插件侧按 `x_ofm_efforts` 强校验；`x_ofm_efforts` 为空的模型一律拒绝 `--effort`（设了也不会生效）。
8. **没有后台任务**：不轮询、不推送公告、不主动探活，所有网络动作都由一条命令或一轮对话触发。
9. **不写任何 OFM 数据**：`settings.json` / `stats.json` / `feed.json` 只读；`stats.json` 的版本迁移由 OFM 自己负责。
10. 模型目录缓存 `models_ttl`（默认 600 秒）。改过 OFM 配置后想立刻看到新目录，重启插件或等缓存过期。

---

## 7. 故障排查

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 命令回「OFM 的本地转发没开（非 /health 路径返回 503）」 | OFM 的 `forward.enabled=false`，端口没 bind | OFM → 设置 → 本地转发 打开并保存 |
| 命令回「鉴权失败」（401） | `forwardKey` 与 OFM 当前用的不一致（重开转发会重新铸造，或你把旧 key 填进了 `api_key`） | 重开一次本地转发，把新的 `settings.json` 的 `forwardKey` 粘到配置，或清空 `api_key` 让它读文件；`/ofm doctor` 会显示当前 key 指纹 |
| 命令回「连不上 OFM 网关」 | 服务没起 / 端口漂移 / `base_url` 指错 | `/ofm doctor` 看 `/health` 项；OFM 端口被占会**向后顺延**，实际端口只在 `settings.json` 的 `standalonePort` 里，插件默认会扫 11 个端口，仍失败就在配置里手填 `base_url` |
| `/ofm doctor` 报「forwardKey 不存在」 | 从没打开过本地转发（key 惰性铸造） | 打开一次转发并保存，然后重跑 |
| `/ofm doctor` 报「管理 API 不可用」 | 你接的是插件网关（18899）或跨机，管理面不转发 | 只影响 `/ofm usage` 的精度，聊天不受影响；要完整统计就改用 standalone |
| `/ofm usage` 回「不可用」 | 管理 API 不通且数据目录里没有 `stats.json` | 检查 `OFM_HOME` / 数据目录；插件网关模式下必须有可读的 `stats.json` |
| 切了档位但回复没变化 | 该模型没声明档位，或档位写错被 OFM 静默回落 | `/ofm effort` 不带参数看合法档位；`x_ofm_efforts` 为空的模型设了也没用 |
| 对话里模型没变成你选的那个 | 会话覆盖没生效 / 别的会话 | `/ofm status` 看「本会话覆盖」；覆盖按 `unified_msg_origin` 隔离，群聊与私聊互不影响 |
| 插件加载失败 | Python 版本或 AstrBot 版本不满足 | 需要 Python 3.12+、AstrBot `>=4.13.0,<5` |

## 仓库名与插件目录名

仓库名是 `astrbot_plugin_our_free.model`（含点号，仅作仓库名），但 **AstrBot 的插件目录名必须是合法 Python 标识符**，
因为 `metadata.yaml` 的 `name` 就是被 `import` 的模块名。克隆后请把目录改成下划线版本：

```bash
git clone https://github.com/desthon/astrbot_plugin_our_free.model.git \
  <AstrBot 根目录>/data/plugins/astrbot_plugin_our_free_model
```

`metadata.yaml` 里已经写死 `name: astrbot_plugin_our_free_model`，目录名与它不一致时 AstrBot 会按目录名找模块并报找不到。

## 许可与第三方声明

- 本仓库：**MIT**，见 [`LICENSE`](LICENSE)。
- 本插件不包含 [Our Free Model](https://github.com/Ebony-Vinyl/dsh-our-free-model)（MIT，`Copyright (c) 2026 Ebony-Vinyl`）或
  [AstrBot](https://github.com/AstrBotDevs/AstrBot)（**AGPL-3.0**）的任何源代码；前者只通过其 HTTP 接口 `/v1`、`/api/management/*`
  与数据目录文件交互，后者只 `import` 其公开插件 API。
- 若你把本插件与 AstrBot **一并再分发**，AstrBot 部分仍需履行 AGPL-3.0 义务；仅自用部署时本仓库的 MIT 条款覆盖插件自身。
- 逐项说明（含「未复制代码」的核对方式）见 [`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md)。
