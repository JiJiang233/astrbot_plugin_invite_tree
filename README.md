# astrbot_plugin_invite_tree

一个面向 QQ（aiocqhttp / OneBot v11）的 AstrBot 邀请关系可视化插件。插件会自动识别好友申请者的来源群，记录用户邀请机器人加入群聊的后续链路，并将关系数据长期保存在本地。

例如，当群 A 的用户添加机器人为好友，之后又邀请机器人加入群 B 时，插件会记录为：

```text
机器人 → 群 A → 用户 → 群 B
```

管理员发送 `/关系树` 后，插件会生成带 QQ 头像的树状关系图：

- 蓝色圆环：用户；
- 粉色圆环：群聊；
- 绿色圆环：机器人根节点。
- 文字统一使用插件内置的 `font/MiSans-Medium.ttf`。
- 节点之间使用平滑的三次贝塞尔曲线连接。

MiSans 字体版权及相关知识产权归小米科技有限责任公司所有。字体说明与完整许可协议见 [`font/README.md`](font/README.md)。

## 安装要求

- AstrBot `>= 4.9.2`；
- 平台适配器：`aiocqhttp`；
- 协议端需支持 OneBot v11 的请求和通知事件；
- Python 依赖见 `requirements.txt`。

插件目录必须放在 AstrBot 的 `data/plugins/astrbot_plugin_invite_tree` 下。AstrBot 会根据 `requirements.txt` 安装 Pillow。

## 命令

| 命令 | 权限 | 说明 |
| --- | --- | --- |
| `/关系树` | AstrBot 管理员 | 生成并发送完整关系树图片 |
| `/邀请树` | AstrBot 管理员 | `/关系树` 的别名 |
| `/好友树` | AstrBot 管理员 | `/关系树` 的别名 |
| `/关系树状态` | AstrBot 管理员 | 查看节点、群和关系数量及数据文件位置 |

完整关系可能涉及用户与群聊隐私，因此查看命令默认仅开放给 AstrBot 管理员。

## 关系规则

插件以机器人为根节点，按事件构建这些关系：

```text
机器人
├── 群 A
│   └── 用户甲（从群 A 添加机器人好友）
│       └── 群 B（用户甲后来邀请机器人加入群 B）
└── 用户乙（无法判断来源群的好友）
    └── 群 C（用户乙邀请机器人加入群 C）
```

好友申请的来源群按以下顺序判断：

1. 读取协议端可能提供的 `source_group_id`、`from_group_id`、`from_group` 或 `group_id` 扩展字段；
2. 识别类似“我是来自某群的某人”的验证消息，并按群名匹配；
3. 调用 `get_group_list` 与 `get_group_member_info`，在机器人已加入的群中查询申请者。

标准 OneBot v11 的好友申请事件本身不保证包含来源群。如果申请者同时存在于多个群，插件会保存所有候选来源关系；图片为避免重复节点和环路，会显示一棵稳定的生成树。

群邀请同时监听：

- `request_type=group, sub_type=invite` 的群邀请请求；
- `notice_type=group_increase` 且 `user_id=self_id` 的机器人入群通知，作为协议端漏发请求事件时的兜底。

普通成员加入群聊不会写入关系树。

## 持久化与缓存

所有运行时文件都位于 AstrBot 数据目录，不写入插件源码目录：

```text
data/plugin_data/astrbot_plugin_invite_tree/
├── relationship_tree.json   # 长期关系数据，原子写入
├── avatars/                 # QQ 头像缓存
└── generated/               # 最近生成的关系树图片
```

插件重载、升级后关系数据仍会保留。`relationship_tree.json` 使用版本化结构，并保留事件时间、关系类型和来源。

## 配置

可在 AstrBot 插件配置页面调整：

- `infer_source_group`：是否在缺少来源字段时查询共同群；
- `source_scan_group_limit`：每次好友申请最多扫描的群数；
- `source_scan_concurrency`：成员查询并发数；
- `download_avatars`：是否下载 QQ 头像；
- `avatar_cache_hours`：头像缓存有效时间。

头像下载失败不会影响关系记录，图片会自动使用“人 / 群 / 机”占位头像。

## 兼容性说明

- 本插件只读取事件和资料，不自动同意或拒绝好友/群邀请，不会干扰审批类插件。
- 它不会调用 `event.stop_event()`，可与 `astrbot_plugin_relationship` 等关系管理插件同时使用。
- 来源群扫描在后台异步执行，不阻塞好友审批插件处理请求。
- 协议端若禁止 `get_group_member_info` 或查询受限，无法识别的好友会直接挂在机器人根节点下。
- QQ 头像来自公开 qlogo 地址；如运行环境无法访问外网，可关闭头像下载。

## 开源许可

除字体资源外，本插件代码与文档采用 [MIT License](LICENSE) 开源。

`font/MiSans-Medium.ttf` 不适用 MIT License。MiSans 字体版权及相关知识产权归小米科技有限责任公司所有，其使用受 [`font/README.md`](font/README.md) 中《MiSans 字体知识产权许可协议》约束。

## 参考

事件兼容设计参考了本地提供的 MIT 插件 `astrbot_plugin_relationship` 对 OneBot 好友申请、群邀请和机器人入群通知的处理方式。本插件没有复制其业务代码，功能聚焦于关系持久化和树图生成。
