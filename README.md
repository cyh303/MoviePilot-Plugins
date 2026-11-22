# MoviePilot-Plugins

个人自用的 [MoviePilot](https://github.com/jxxghp/MoviePilot) 插件仓库。

本仓库分为两类：

- **原创插件**：独立开发，版权归本仓库。
- **二次开发插件**：基于上游作者的原版做了**本地补丁修复**，版权与主要贡献归**原作者**，
  本仓库只维护补丁与后续修改。每个二次开发插件的源码文件头部、`package` 描述与下表均标注了上游来源。

---

## 来源与致谢

| 插件 | 上游仓库 | 原作者 | 上游版本 |
| --- | --- | --- | --- |
| `P115StrmHelper` | [DDSRem-Dev/MoviePilot-Plugins](https://github.com/DDSRem-Dev/MoviePilot-Plugins) | [DDSRem](https://github.com/DDSRem) | 3.0.2 |
| `P115Disk` | [DDSRem-Dev/MoviePilot-Plugins](https://github.com/DDSRem-Dev/MoviePilot-Plugins) | [DDSRem](https://github.com/DDSRem) | 3.0.3 |
| `EmbyReverseProxy` | [DDSRem-Dev/MoviePilot-Plugins](https://github.com/DDSRem-Dev/MoviePilot-Plugins) | [DDSRem](https://github.com/DDSRem) | 3.0.0 |
| `SehuaSignIn` | 原创 | cyh303 | — |

上游插件的功能设计、主体实现与文档均由原作者完成，感谢其开源。

> 关于 `author` 字段：`package.v3.json` 与源码里的 `plugin_author` 写的是本仓库维护者 `cyh303`，
> 因为本仓库发布的是打过补丁的**衍生版本**，与上游原版并非同一份代码。
> 上游署名统一放在上表、源码文件头注释与各插件 `description` 中。

---

## 插件列表

### V3 布局（`package.v3.json` + `plugins.v3/`）

| 插件 ID | 名称 | 版本 | 上游基线 | 说明 |
| --- | --- | --- | --- | --- |
| `P115StrmHelper` | 115网盘STRM助手 | **3.0.2.2** | 3.0.2 | 目录上传漏传巡检 + 同步删除兜底匹配 |
| `P115Disk` | 115网盘储存 | **3.0.3.4** | 3.0.3 | 目录 ID 解析修复 + SHA1 计算提速 + 缓存键纳秒化 |
| `EmbyReverseProxy` | Emby 302 反向代理 | **3.0.2** | 3.0.0 | 监听地址下拉（全部 / IPv4 / IPv6）+ 双栈监听修复 |

### V2 布局（`package.v2.json` + `plugins.v2/`）

| 插件 ID | 名称 | 版本 | 说明 |
| --- | --- | --- | --- |
| `SehuaSignIn` | 98堂自动签到 | **2.1.0** | 原创插件，色花堂/98堂 Discuz 论坛每日自动签到 |

---

## 安装

在 MoviePilot 中把本仓库加入插件市场：

```
设置 → 插件市场 → 添加仓库地址
https://github.com/cyh303/MoviePilot-Plugins
```

随后在「插件市场 → 未安装」里找到对应插件安装即可。V3 与 V2 两套索引同时存在，
MoviePilot 会按自身版本自动选择。

> **首次安装会较慢**：本仓库未上传 GitHub Release 产物，MoviePilot 会先尝试 Release，
> 报告「插件发布包不存在」后**自动回退为逐文件下载**。`P115StrmHelper` 共 436 个文件
> （其中包含多平台 wheel，合计约 29 MB），完整安装通常需要 **4–8 分钟**。
> 期间界面可能显示失败或超时，**请不要重复点击安装**，等到插件列表出现该插件再继续。

---

## 插件说明

### P115StrmHelper 3.0.2.2

115 网盘 STRM 生成一条龙：目录监控上传、STRM 生成、302 跳转播放、整理接管、同步删除等。

基于上游 [DDSRem-Dev/MoviePilot-Plugins](https://github.com/DDSRem-Dev/MoviePilot-Plugins) 的 `3.0.2`，叠加以下本地补丁：

**目录上传漏传巡检**

修复「文件已入库但从未上传到 115」的问题：

- 周期巡检（默认 10 分钟）：扫描监控目录，用与写入端**完全相同的规则**推导预期 STRM 路径，
  发现「本地有视频但无同名 STRM」即重新提交上传，兜住队列卡死、插件重载/重启、监控中断造成的丢事件。
- 上传队列在停止超时后留下空队列且 worker 已退出时**自动重建**，不再永久拒绝入队。
- 上传后 pickcode 解析兼容 CloudDrive2 复合文件 ID（`<父目录id>/<文件名>` 形式），
  校验失败或 STRM 生成失败时**保留本地文件**等待重试，而不是留下无 STRM 的孤儿视频。
- 新增 `tests/test_v3_directory_upload_rescan.py`（11 个用例）。

**同步删除兜底匹配**

修复「在 Emby 里删了媒体，115 上的文件却没删」：

- 根因：Emby 上报的 `Container` 可能与网盘真实扩展名不一致（实测 `.mov` 被识别为 `mp4`），
  插件按该值拼出的路径在 115 上不存在 → 判定「媒体不存在」直接跳过。
- 现在精确路径查不到时，按**文件名主干**在父目录中匹配真实文件，命中后再删除。
- 新增 `tests/test_mediasyncdel.py` 3 个回归用例。

**其他**

- 日志轮转，避免长期运行把单个日志文件撑到数百 MB。

### P115Disk 3.0.3.4

115 网盘存储模块，按 fileid 操作，替代路径式网盘挂载。

基于上游 `3.0.3`，本地补丁：

- **目录 ID 解析**：命中目录后写入路径缓存；`web` / `app` 接口改为 **app 优先、web 仅作回退**，
  修掉 web 版偶发 `405` 导致列目录间隔性返回空的问题；115 返回的字符串 `id` 统一归一化为 `int`。
- **SHA1 计算提速**：整文件哈希用于 115 秒传，改为 8 MB 分块 + `posix_fadvise` 顺序读提示，
  并新增整文件 SHA1 缓存。实测 11.10 GB 文件 335s → 150s。
- **缓存键纳秒化**：缓存键由「路径 + 大小 + 秒级 mtime」改为
  「路径 + 大小 + 纳秒 mtime + 纳秒 ctime + inode」，避免同路径同大小、同秒内被替换时命中旧哈希。
- 新增 `tests/test_p115_api_get_pid.py`、`tests/test_p115_api_sha1.py`。

> 注意：`P115Disk` 的 `list` / `move` / `rename` / `copy` 各有限速（0.5 QPS），
> 批量整理会比路径式挂载慢，这是防 115 风控的设计取舍。

### EmbyReverseProxy 3.0.2

Emby 302 反向代理：自动代理 HTTP 链接、解析重定向到最终地址、强制 DirectPlay、
注入外部播放器按钮。

基于上游 `3.0.0`，本地补丁：

- **监听地址改为下拉选择**：`全部（IPv4 + IPv6）` / `仅 IPv4` / `仅 IPv6`，按需只监听指定协议。
- **双栈修复**：`uvicorn` 经 `asyncio.create_server` 绑定 IPv6 地址时会强制 `IPV6_V6ONLY=1`，
  导致 `host=::` 时只剩 IPv6、局域网 IPv4 与 `127.0.0.1` 全部不可达。
  现在「全部」模式自行创建 `IPV6_V6ONLY=0` 的 socket 交给 uvicorn，真正双栈。
- `stop_service()` 与启动异常分支会关闭该 socket，避免重载后端口残留。
- 旧配置无 `listen_mode` 时按原 `host` 自动推断等价选项，无需手动迁移。

### SehuaSignIn 2.1.0

色花堂 / 98堂（Discuz 论坛）每日自动签到。原创插件。

- 内嵌浏览器通过 Cloudflare 与年龄确认页，账号密码 + 安全提问登录。
- 自动解答四种验证码：`rotate`（旋转）、`slide`（拖动滑块）、`drag`（拖拽贴图）已实现；
  `click`（点选）未实现，采用重试规避。
- 支持定时执行、手动运行、执行记录与通知。
- v2.0.0 起**移除论坛浏览与回复功能**，只保留签到。

> 需自备可用的代理与账号（账号、密码、安全提问答案仅存于本机插件配置，不在源码内）。

---

## 版本号规则

```
上游版本            本仓库版本
3.0.0        →      3.0.0.1  （第一个本地补丁）
             →      3.0.0.2  （第二个本地补丁）
```

- 前三段与上游保持一致，方便对照。
- 第四段仅在**本仓库打了补丁**时递增；无补丁的插件直接沿用上游版本号。
- 插件自身版本（`version.py` / `plugin_version`）与 `package.v3.json` 中的 `version` 始终同步。

---

## 目录结构

```
.
├── package.v3.json          # V3 插件索引
├── package.v2.json          # V2 插件索引
├── plugins.v3/
│   ├── p115strmhelper/      # → MoviePilot 3.0+
│   ├── p115disk/
│   └── embyreverseproxy/
└── plugins.v2/
    └── sehuasignin/         # → MoviePilot 3.0+ / 2.x
```

---

## 常见问题

**装完版本号没变？**
`package.v3.json` 的 `version` 与插件自身 `version.py` / `plugin_version` 必须同时更新，
只改一处会出现「索引显示新版本、运行时仍是旧版本」。本仓库已保持同步。

**升级会不会丢配置？**
安装只替换代码，配置保存在 MoviePilot 数据库，不受影响。仍建议升级前在
`设置 → 插件 → 备份` 或手动留一份配置快照。

**能跟踪上游更新吗？**
可以，但需要手动合并。本仓库的更新流程是：拉取上游新版本源码 → 覆盖本地补丁文件 →
提升版本号 → 推送到本仓库 → 再从本仓库安装。直接订阅上游仓库会失去本地补丁。

**为什么没有 Release 包？**
本仓库按需构建、不发布 Release 产物，安装走文件列表回退，因此较慢（见上文）。

---

## 免责声明

- 本仓库插件为个人自用与问题修复目的维护，**不对任何使用后果负责**。
- 二次开发插件的版权与主要贡献归原作者（见[来源与致谢](#来源与致谢)）。
- 请遵守所在地区法律法规及所使用站点的用户协议。
