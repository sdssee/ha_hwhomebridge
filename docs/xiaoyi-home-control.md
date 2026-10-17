# 小艺家庭控制（1.1.0）

这条路径使用小艺 Skill → HTTPS 网关 → HA webhook → HA 原生服务，与现有 HiLink 灯光桥接并行。窗帘不需要新增华为 PID，也不会生成智慧生活的原生窗帘卡片。

## 接入范围

- 灯光：HA `/light` 页的普通照明灯，要求有区域，排除隐藏、禁用、配置/诊断实体及禁用设备。自动跟随 HA 实体注册表和状态变化。
- 窗帘与晾晒：读取配置目录内 YAML 仪表盘的指定 view（默认 `family-overview.yaml` 中的 `curtains`）。只读取实际卡片的 `entity`，使用卡片显示名，不读取操作按钮中隐藏的 service target。
- 因此方向已校正的模板晾衣架会保留，底层原始晾衣架不会重复加入。窗帘页面新增/删除卡片后，下一次请求即更新范围。缺失仪表盘会拒绝控制，避免意外扩大范围。
- 新建房间/设备或重命名后，下次 catalog 会返回新名称；旧名称不做猜测匹配，可使用 HA 实体别名。

## 启用

集成配置 → 小艺家庭控制 → 启用，填写仪表盘文件和窗帘 view 路径。保存后集成会重载。重新进入该页面可查看私有 webhook 路径。默认不启用，不在日志打印地址。

在现有 HTTPS 网关增加一个精确、保密路径，转发到此 webhook；仅允许 GET、POST，不公开 HA 令牌。GET 只返回无设备信息的健康状态，POST 才能查询目录或控制。不要将整个 `/api` 转发到此网关。

复制 `xiaoyi_skill/close-desk-lamp` 到小艺 Skill 工作区，将 `settings.example.json` 复制为 `settings.json`，只在私有 settings 中填写 HTTPS 网关地址。该文件已被 git 忽略，不能提交凭据。也可用 `HWHOMEBRIDGE_HOME_ENDPOINT` 环境变量提供地址。

保留 `close-desk-lamp` 标识可更新现有测试组。上传、扫描通过后，在原测试用户组重新发布测试版本；无需正式上架。

## JSON 协议

查询全部：`{"command":"catalog"}`。支持 domain、kind、area 筛选，返回名称、实体 ID、区域、支持动作和当前状态。

单灯调光：

```json
{"command":"control","entity_id":"light.example","action":"brightness","value":50,"request_id":"unique-request-001"}
```

房间关灯：

```json
{"command":"control","domain":"light","area":"客厅","all":true,"action":"turn_off","request_id":"unique-request-002"}
```

全部窗帘停止（不含晾衣架）：

```json
{"command":"control","domain":"cover","kind":"curtain","all":true,"action":"stop","request_id":"unique-request-003"}
```

开合度：`action=position,value=0..100`，0全关100全开。方向已校正晾衣架同样是0最低、100最高。开关型灯不会假装支持调光。色温使用 Kelvin，并校验实体实际上下限。

状态：`{"command":"status","entity_id":"cover.example"}`。control 返回 `accepted` 仅表示服务已接受，不能表示窗帘已经移动到位。批量操作逐设备报告不支持/离线/服务失败；无效数值在开始执行前整体拒绝。

同一接口实例中，同一 request_id 的相同请求在10分钟内返回已有结果（最多缓存256条），不同内容拒绝。集成重载、HA重启或缓存过期后不提供持久去重保证。客户端不会自动重发POST；超时先查询状态。

## CLI

```sh
python3 scripts/close_lamp.py --command catalog
python3 scripts/close_lamp.py --command control --entity-id light.example --action brightness --value 50
python3 scripts/close_lamp.py --command control --domain light --area 客厅 --all --action turn_off
python3 scripts/close_lamp.py --command status --entity-id cover.example
```

无参数仍对应关闭名为“台灯”的灯，多台同名时会拒绝并返回候选。

当前跨网环境启用 TLS 1.2 兼容和持久连接；证书和主机名验证保持开启。最多重试3次无控制GET，仅在健康检查成功且连接可复用后，在同一连接发送一次POST。普通网络可在 settings 中设置 `tls12_compat=false` 使用系统默认TLS协商。

## 验证与回滚

先检查 catalog 与实际页面一致，再查询状态、检查越界/歧义请求被拒绝。真实控制先告知使用者，使用单个设备验证并恢复初始状态，不以全屋开关作为测试。

禁用“小艺家庭控制”即可注销 webhook；移除网关对应精确路径可关闭公网入口。原 HiLink 桥接和旧台灯自动化是独立路径。文件部署前保留组件、配置条目和网关配置备份，回滚只恢复本次改动，避免覆盖其他配置。
