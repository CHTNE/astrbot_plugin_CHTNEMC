# CHTNEMC Minecraft 服务器助手

一个面向 AstrBot 4 的 Minecraft 群聊助手。插件通过异步 RCON 查询和管理服务器，并集成中文 Minecraft Wiki 与 Dynmap。

## 功能

- `/mchelp [指令]`：查看总帮助或具体指令帮助。
- `/j`：显示在线人数、玩家名、维度、坐标和附近 Dynmap marker。
- `/mctime`：显示主世界时间、时钟和时间状态。
- `/mcbond <玩家名>`：按聊天平台 UID 绑定 MC 玩家名。
- `/mcwiki <关键词>`：模糊搜索中文 Minecraft Wiki；使用 `/mcwiki <序号>` 选择候选。
- `/mcmap [视图] [x] [z] [缩放]`：渲染配置的 Dynmap 视图；`/mcmap list` 查看视图名。
- `/tpa <玩家>`、`/tpahere <玩家>`：在聊天插件中发起传送请求，目标可使用群聊艾特。
- `/tpaccept`、`/tpdeny`：目标成员接受或拒绝传送请求；仅接受后才通过 RCON 执行原版 `/tp`。
- `/tp`、`/tell`、`/msg`、`/seed`、`/ping`：代理服务器指令，参数中的群聊艾特会转换为成员绑定的玩家名。
- `/mccmdadd <指令> [admin]`：管理员添加允许的 RCON 指令。省略 `admin` 时群成员可用。
- `/mccmddel <指令>`：管理员删除额外指令。

## 安装

将本目录放入 AstrBot 的 `data/plugins/astrbot_plugin_chtnemc`，安装依赖后在 WebUI 重载插件。插件要求 AstrBot `>=4.9.2,<5`。

在 `server.properties` 中启用 RCON：

```properties
enable-rcon=true
rcon.port=25575
rcon.password=请使用足够长的随机密码
```

不要将 RCON 端口直接暴露到公网。建议 AstrBot 与 Minecraft 位于同一内网，并通过防火墙限制来源。

## 配置要点

安装后在 AstrBot WebUI 的插件配置中至少填写：

1. `rcon_host`、`rcon_port`、`rcon_password`。
2. `enabled_groups`（留空表示全部群聊）和可选的 `admin_uids`。
3. 如需地图功能，填写 `dynmap_url`，并按实际世界名调整 `dynmap_worlds` 与 `dynmap_views`。

`dynmap_views` 中每个视图的值格式为 `world|mapname`。例如 `world|flat` 是俯视图，`world|surface` 通常是伪 3D 视图，实际 mapname 以你的 Dynmap 配置为准。配置项使用 `overworld`、`overworld_3d`、`nether`、`end` 作为稳定键，聊天指令中显示为“主世界”“伪3D”“下界”“末地”。

`tpa` 和 `tpahere` 的请求、绑定校验、会话隔离、超时及审批全部由本插件完成，不依赖 EssentialsX 等服务端传送插件。审批者接受后，插件只向服务器发送原版 `tp <玩家> <目标>`。`ping` 仍可能需要服务端提供相应指令；未绑定时 `/ping` 仅测量 RCON 往返延迟。

绑定默认要求目标玩家在线，因为原版 RCON 无法可靠验证所有离线玩家。可关闭 `verify_player_on_bind` 允许离线绑定。

## 指令示例

```text
/mcbond M42_Thyphein
/tp @某位已绑定的群成员
/tell @某位已绑定的群成员 今晚一起下矿吗？
/mcwiki 红石比较器
/mcmap 伪3D -120 -235 2
/mccmdadd weather admin
```

`/j` 输出示例：

```text
当前在线 1/20 人：
- M42_Thyphein 主世界：-120 68 -235（刷怪塔）
```

## 测试

```bash
python -m unittest discover -s tests -v
```
