# astrbot_plugin_our_free_model

把本机 **Our Free Model（OFM）独立服务** 零配置接进 AstrBot。

**两步就能用：装插件 → 起 OFM standalone。** 不用在 WebUI 里建提供商、不用粘贴 key、不用敲任何命令：

* AstrBot 启动完成后，插件自动把 OFM 接成 provider（两个托管条目，见 [第 2 节](#2-它改了什么配置零配置的代价)），
  并在**全局默认模型为空**时把默认模型指向它；你已有的默认、或你在 WebUI / `/provider` 里显式选过的，一律不覆盖；
* OFM 比 AstrBot 晚起来也不算故障：后续对话里按 300 秒一次、最多 3 次自愈，也可以 `/ofm provision` 立刻重跑；
* 它**不是** provider 适配器：只通过 OFM 的外部契约工作（`/health`、`/v1/models`、`/api/management/*`、数据目录里的 `settings.json` / `stats.json` / `feed.json`），OFM 的任何数据文件都只读；
* 全部命令**仅管理员**可用，且不提供 `llm_tool`（否则非管理员可以经 LLM 侧触发命令，属于权限旁路）。

| 形态 | 端口 | 插件支持情况 |
| --- | --- | --- |
| A. 同机 standalone（`npm run start:standalone`） | 18900（持久化在 `settings.json` 的 `standalonePort`） | **支持**：`/v1` + 全量 management API + 统计 + 公告 |
| B. dsh 插件网关（18899） | 18899 | **识别并降级**：只能聊天，管理面被 trust fence 挡住，`/ofm usage` 回落 `stats.json` |

---

## 1. 安装（两步）

环境要求：AstrBot `>=4.13.0,<5`，Python 3.12+。**不新增任何第三方依赖**（`httpx` 是 AstrBot 主依赖）。

```bash
# 第 1 步：把整个目录放进 AstrBot 的插件目录，然后重启 AstrBot（或 WebUI 插件页重载）
cp -r astrbot_plugin_our_free_model <AstrBot 根目录>/data/plugins/

# 第 2 步：起 OFM standalone（默认 127.0.0.1:18900）
npm run start:standalone
```

插件目录结构：

```
<AstrBot>/data/plugins/astrbot_plugin_our_free_model/
├── metadata.yaml
├── _conf_schema.json
├── main.py
├── README.md
└── core/{__init__,endpoints,secrets,ofm_client,catalog,session,stats,feed,provision}.py
```

AstrBot 打印 `AstrBot started` 之后，插件会自己完成接入；日志里会看到一行：

```
[ofm] 自动接入完成：provider=ofm-free（新建 provider_sources 条目、新建 provider 条目、接管了空的全局默认模型、model=combo:recommended-free）
```

然后直接聊天即可。想确认状态：`/ofm doctor`（看「自动接入」块）→ `/ofm provision`（手动重跑一次）→ 正常对话。

前置条件（很重要，缺一样都会 503/401）：

1. OFM 独立服务在跑：`npm run start:standalone`（默认 `127.0.0.1:18900`，只监听回环）；
2. OFM 里 **设置 → 本地转发** 已打开并保存——否则除 `/health` 外一律 503；
3. 打开转发之后 `settings.json` 才会出现顶层 `forwardKey`（惰性铸造），插件自动读它。

如果 OFM 的数据目录不在默认位置（`$OFM_HOME` 或 `~/.our-free-model`），启动 AstrBot 时把 `OFM_HOME` 指过去：

```bash
OFM_HOME=/srv/our-free-model python3 main.py   # 或写进 systemd service 的 Environment=
```

---

## 2. 它改了什么配置（零配置的代价）

自动接入只碰 AstrBot 默认配置文件 `<AstrBot 根目录>/data/cmd_config.json`（内存里是
`provider_manager.acm.default_conf`，插件通过 `save_config_async()` 落盘）里的**三处**，别的地方一行都不动：

| 位置 | 条目 id | 内容 | 谁会写它 |
| --- | --- | --- | --- |
| `provider_sources[]` | `ofm-standalone` | 连接信息：`type=openai_chat_completion`、`api_base=http://127.0.0.1:<端口>/v1`、`key=[<forwardKey>]`、`timeout` | 插件（端口 / key 变了会就地更新） |
| `provider[]` | `ofm-free` | 模型选择：`provider_source_id=ofm-standalone`、`model=<default_model>`、`enable=true` | 插件 |
| `agent_runner.config.model.provider_id` | — | 全局默认模型。**只有原本为空时**才写成 `ofm-free` | 插件（用户接管后不再写） |

明确的边界：

* **不覆盖你的默认**：`provider_id` 非空时插件什么都写不进去，`default_set=False`；
* **不覆盖显式选择**：你在 WebUI 里换过默认、或用 `/provider` / 会话级 `/ofm use` 选过模型，插件都不会抢回来；
* **不改别的 provider**：`provider_sources` / `provider` 里其他条目原样保留；
* **不写 OFM 的数据**：`settings.json` / `stats.json` / `feed.json` 全程只读；
* **网关没起来 / 没有 forwardKey 时一个字段都不写**：宁可降级并留一句话，也不给 AstrBot 留下指向死端口的配置
  （否则每一轮对话都会撞在坏 provider 上）；
* **幂等**：第二次运行不重复建条目、不改没变化的字段（真实保存次数不增加）。

清理：`/ofm unprovision` 删掉上面两个托管条目；如果默认值是本插件设的，会顺带清空 `provider_id`。它同时会把 `auto_provision` 写回 `false`（即「别再自动装回来」），所以重启后不会重新接入；想恢复就 `/ofm provision`，它会重新把开关打开。
手工清理等价于在 WebUI 的提供商页删掉 `ofm-standalone` 与 `ofm-free`，再把 `agent_runner.config.model.provider_id` 置空。

想彻底不要这套行为：把插件配置里的 `auto_provision` 关掉（见下一节）。

---

## 3. OFM 后启动怎么办

AstrBot 先起来、OFM 后起来是常见顺序（尤其 systemd 里两个服务）。这不算故障：

1. 启动钩子会因为「连不上网关」降级，只写一行 `logger.warning`，**不阻塞启动、不影响其他插件**；
2. 之后每条消息经过 `on_llm_request` 时检查一次：上次失败 / 没有记录、且距上次尝试超过 **300 秒**、且 `auto_provision` 开着，
   就 `asyncio.create_task` 丢一个后台重试（**不 await**，这条消息照常回），失败只更新 KV，不给你发消息打扰；
3. 前 **3 次**按 300 秒间隔重试；用尽后**不是永久放弃**，降到每 **30 分钟**试一次（OFM 隔几小时才起来也照样能自愈）；成功则日志一行 `[ofm] 自动接入已恢复`，并把默认模型按需接管；
4. 不想等：OFM 起来后直接敲 `/ofm provision`，立刻重跑一次（`auto_provision` 关着也照跑）。

**已经接入成功之后 OFM 换了端口**（被占用时 OFM 会顺延端口，`standalonePort` 会变）：成功以后同样每 **300 秒**
复检一次端点，一旦发现解析出的 `base_url` 与上次记录的不同，就地把 `api_base` / `key` 改掉并重建 provider 实例，
日志一行 `[ofm] OFM 端点变化已重接：旧 → 新`。300 秒内不会重复探测，所以不会变成「每条消息都打一次网关」。
不用重启 AstrBot。

状态都在 `/ofm doctor` 的「自动接入」块里：开关、两个托管条目在不在配置里、实例是否已加载、**实际选中的模型与它的来源**、
默认 `provider_id` 是谁、上一次 ensure 的时间与结果。

### 托管 provider 用哪个模型

接入前会先 `GET /v1/models` 看网关目录，按固定优先级挑一个（确定性，不会随机变）：

1. 插件配置的 `default_model`（默认 `combo:recommended-free`）在目录里 → 用它；
2. `combo:recommended-free` 在目录里 → 用它；
3. 第一个 `combo:*` → 4. 第一个以 `-free` 结尾的 → 5. 目录里的第一个；
6. 目录拉不到（老版本网关 / 接口异常）→ 仍用配置值接入，但 `/ofm doctor` 会标「未校验」。

这么做的原因：组合模型通常是用户自建的，新装环境里往往**不存在** `combo:recommended-free`；写死它会让首条对话直接 404。
已经接入的模型如果之后从目录里消失，下次复检会自动换成新的选择，并在日志与结果里标 `updated_model`。

---

## 4. 配置项（WebUI → 插件 → 配置）

| 键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `auto_provision` | bool | `true` | 装好即自动接入：启动时创建两个托管条目，并在默认模型为空时接管。关掉 = 插件不碰配置，只用命令与请求覆盖（等价于旧版行为） |
| `base_url` | string | 空 | 手动指定网关地址（`http://127.0.0.1:18900`，不要带 `/v1`）。留空 = 读 `settings.json` 的 `standalonePort` 自动发现，并向后扫 10 个端口 |
| `api_key` | string（`secret`） | 空 | 手动覆盖 `forwardKey`。留空 = 读 `settings.json` |
| `request_timeout` | int | 120 | 访问 OFM 的超时（秒） |
| `diagnose_timeout` | int | 3 | 端点探测 / health 检查的单次超时 |
| `models_ttl` | int | 600 | 模型目录缓存（秒） |
| `stats_ttl` | int | 60 | 用量缓存（秒） |
| `default_model` | string | `combo:recommended-free` | 托管 provider `ofm-free` 使用的模型；目录读不到时 `/ofm setup` 也拿它兜底 |
| `models_page_limit` | int | 30 | `/ofm models` 一屏最多列出的条数 |
| `announce_limit` | int | 3 | `/ofm announce` 默认条数（上限 10） |
| `show_api_key_in_setup` | bool | true | `/ofm setup` 是否打印完整 key |

> ⚠️ `secret: true` 只是 WebUI 的**界面遮罩**，**不会加密落盘**：key 仍以明文存在 AstrBot 的插件配置里。介意这一点就把 `api_key` 留空，让插件每次去读 OFM 的 `settings.json`（那个文件本身是 0600）。

---

## 5. 手工兜底：自己建一个提供商（正常情况下不需要）

自动接入能跑通就不用看这一节。只在下面这些情况下才需要手工建：`auto_provision` 被关掉了、
宿主版本不兼容（拿不到 `ProviderManager`、`/ofm doctor` 的「自动接入」块报 ✘）、或者你就是要自己管理提供商。

先跑 `/ofm setup`，然后把输出填进 **WebUI → 配置 → 提供商 → 新建提供商**：

| 字段 | 填什么 |
| --- | --- |
| 提供商类型 | `OpenAI 兼容`（`openai_chat_completion`） |
| API Base | `<base_url>/v1`，例如 `http://127.0.0.1:18900/v1` |
| Key | OFM 的 `forwardKey` 原值（`/ofm setup` 会打印；群聊场景只贴掩码） |
| 模型 | 当前会话模型，或 `/ofm setup` 推荐的 `combo:` / 首选免费道 |

`/ofm setup` 示例输出：

```
正常情况不需要这一步：插件启动时会自动创建提供商（ofm-standalone / ofm-free），
这里是自动接入失败时的手工兜底。把下面 4 个字段填到 WebUI → 配置 → 提供商 → 新建：

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

## 6. 命令参考（全部仅管理员）

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

自动接入：
✔ auto_provision：开
✔ 托管 source：ofm-standalone（在 provider_sources 里）
✔ 托管 provider：ofm-free（在 provider 里）
✔ provider 实例：已加载（inst_map 命中）
✔ 全局默认模型：provider_id=ofm-free
✔ 上次 ensure：06-02 11:04：新建 provider_sources 条目、新建 provider 条目、接管了空的全局默认模型、model=combo:recommended-free
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

### `/ofm provision`

手动重跑一次自动接入（`auto_provision` 关着也照跑）。回复里写清楚新建 / 更新了什么、默认模型有没有被接管、失败时降级原因：

```
自动接入完成：
· provider：ofm-free（新建）
· source：ofm-standalone（新建）
· 模型：combo:recommended-free
· 全局默认模型：原来是空的，已设成 ofm-free
· 实例：已加载，直接对话即可
```

端点漂移（OFM 换端口、重开转发换了 key）后也用它：source 的 `api_base` / `key` 就地更新，并 `reload` 让实例换掉旧地址。

### `/ofm unprovision`

删掉 `provider_sources` 里的 `ofm-standalone` 与 `provider` 里的 `ofm-free`；默认值是本插件设的还会清空 `provider_id`。
回复逐条列出删了什么。注意 `auto_provision` 仍开着时，**下次 AstrBot 重启会重新接入**；要长期关掉请改配置。

### `/ofm setup`

见第 5 节（自动接入失败时的手工兜底）。

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

## 7. 请求覆盖是怎么生效的

`on_llm_request` 钩子读本会话的 `{model, effort}`，**只在有覆盖时**改写 `req.model`：

```python
req.model = "combo:recommended-free(deep)"   # 尾缀形式
```

原因：AstrBot 的 `extra_body` 没有请求级入口，而 OFM 在协议层就认 `model` 尾缀 `id(level)`（服务端 strip 后查目录，并在 `reasoning_effort` 缺省时用它）。没有覆盖时钩子直接返回，模型保持 provider 默认值；钩子里任何异常只写 `logger.warning`，不影响正常对话。

会话隔离键是 `event.unified_msg_origin`：群 A 的 `/ofm use` 不影响群 B 和私聊。

---

## 8. 限额与已知限制

1. **渠道全是推断**。`/v1/models` 只有 `id/object/created/owned_by/context_window/x_ofm_efforts/x_ofm_effort_default`，没有 name、vision、channel 字段。插件按 id 形态猜：`*-free`→免费道、`combo:`→降级链、`kilo-auto/…`→Kilo、含 `/` 的 `<provider>/<model>`→聚合渠道（EAC 也是 `<org>/<model>`，**无法与聚合严格区分**，所以一律归聚合，不单列 `eac`）、其余→未分类。这只是展示分类，不代表服务端真实标记。
2. **`created` 是每次请求现算的** `Date.now()`，插件从不拿它当缓存键（缓存键是 `base_url`）。
3. **token / 平均速度是按样本合计**。管理 API 的 `samples` 只回 200 条，`stats.json` 的 `samples` 是 400 条环形数组，所以这两项是「最近样本」而不是全量；`requests / failedRequests` 用统计文件顶层的全量计数（缺失时才退回样本计数）。
4. **`secret` 不加密落盘**（见第 2 节）。
5. **只支持 A 形态**。插件网关（18899）只能识别 + 降级：管理面被 trust fence 挡住，`/ofm usage` 回落 `stats.json`，`/ofm doctor` 会明确写「此模式不暴露管理面」。跨机 / 容器同理——management 不转发，只能读挂载进来的数据目录。
6. **不做区域判断**。region-limited 模型和普通模型混在同一个数组里且无标记，能不能用只以实际请求的 `404 model "X" not found` / `Model is unavailable` 为准。
7. **非法档位 OFM 不报错**。它静默回落默认档，所以插件侧按 `x_ofm_efforts` 强校验；`x_ofm_efforts` 为空的模型一律拒绝 `--effort`（设了也不会生效）。
8. **不做后台轮询**：不推送公告、不主动探活。唯一的后台动作是自动接入自愈的重试任务（`asyncio.create_task`，
   300 秒一次、最多 3 次、只在有对话时触发），其余网络动作都由一条命令或一轮对话触发。
9. **不写任何 OFM 数据**：`settings.json` / `stats.json` / `feed.json` 只读；`stats.json` 的版本迁移由 OFM 自己负责。
10. 模型目录缓存 `models_ttl`（默认 600 秒）。改过 OFM 配置后想立刻看到新目录，重启插件或等缓存过期。

---

## 9. 故障排查

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
| `/ofm doctor` 的「自动接入」块全 ✘ | 拿不到 `ProviderManager`（宿主版本不兼容）或 `auto_provision` 关着 | 升级 AstrBot 到 `>=4.13.0,<5`；或打开 `auto_provision`；或按第 5 节手工建提供商 |
| 日志 `[ofm] 自动接入未完成：…` | OFM 还没起来 / 还没打开本地转发（没有 forwardKey）/ 端口漂移 | OFM 起来后等下一轮对话自愈（300 秒一次），或直接 `/ofm provision` |
| 想把插件装的入口全清掉 | 换 provider / 迁机器 | `/ofm unprovision`（会同时把 `auto_provision` 关掉并落盘，重启不会自己装回来） |
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
