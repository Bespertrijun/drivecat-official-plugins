# DriveCat 官方插件源

DriveCat 官方维护的插件集合，同时也是插件开发的参考模板。

## 项目结构

```
plugins/
  _shared/               ← 共享资源
    sdk.js               ← 插件 UI 通信 SDK
  rename/                ← 示例：批量重命名插件
    manifest.json        ← 插件清单（version 由 git tag 自动注入）
    main.py              ← 入口（实现 PluginInterface）
    rename_engine.py     ← 8 种规则引擎
    rename_manager.py    ← 预览 / 并发执行 / 流式进度
    ui/
      index.html         ← 3 步向导骨架
      style.css          ← 样式（DriveCat 主题变量）
      app.js             ← 交互逻辑（选文件 / 配规则 / SSE 执行）
dist/                    ← 发布目录（自动生成，勿手动修改）
  index.json
  packages/
scripts/
  build.py               ← 打包 + 签名脚本
requirements.txt         ← 开发依赖
```

## 插件开发教程

### 1. 创建插件目录

在 `plugins/` 下新建目录，至少包含 `manifest.json` 和入口文件：

```
plugins/
  my-plugin/
    manifest.json
    main.py
```

### 2. 编写 manifest.json

```json
{
  "name": "我的插件",
  "author": "YourName",
  "description": "插件功能描述",
  "hooks": ["before_rename", "after_rename"],
  "permissions": ["drive.list", "drive.rename", "fs.read", "fs.write"],
  "source": "official",
  "source_url": "https://github.com/Bespertrijun/drivecat-official-plugins",
  "entry": "main.MyPlugin"
}
```

> **注意**：`version` 和 `changelog` 不要写在 manifest.json 里，由 `build.py` 自动生成。版本号写在 `version.py` 中。

**字段说明：**

| 字段 | 说明 |
|------|------|
| `name` | 插件显示名称 |
| `version` | 语义化版本号 |
| `hooks` | 本插件需要监听的钩子列表 |
| `permissions` | 声明需要的权限（决定 `DriveProxy` 可调用的方法） |
| `entry` | 入口，格式 `模块名.类名` |

**可用权限与方法参考：**

#### `drive.list` — 列出 / 查询

**`list_files(parent_id) → List[FileInfo]`**

列出目录下的文件和子目录。

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `parent_id` | `str` | `"0"` | 目录 ID，`"0"` 表示根目录 |

**返回值：** `List[FileInfo]` — 该目录下所有文件和子目录的列表。

**`resolve_path(remote_path) → Optional[str]`**

将远程路径解析为目录 ID。若中间目录不存在会自动创建（类似 `mkdir -p`）。

| 参数 | 类型 | 说明 |
|------|------|------|
| `remote_path` | `Path` | 远程目录路径，如 `Path("/photos/2026")` |

返回最终目录 ID，失败返回 `None`。

**`get_quota() → dict`**

获取空间配额信息，无参数。

**返回值：** `dict` — 包含 `used`（已用字节）、`total`（总容量字节）等字段。

---

#### `drive.rename` — 重命名

**`rename(file_id, new_name) → bool`**

重命名文件或目录。

| 参数 | 类型 | 说明 |
|------|------|------|
| `file_id` | `str` | 文件/目录 ID |
| `new_name` | `str` | 新名称 |

**返回值：** `bool` — `True` 成功，`False` 失败。

---

#### `drive.upload` — 上传

**`upload_file(local_path, remote_parent_id, progress_callback?) → Optional[FileInfo]`**

上传本地文件到网盘。

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `local_path` | `str` | — | 本地文件路径 |
| `remote_parent_id` | `str` | `"0"` | 上传到的目标目录 ID |
| `progress_callback` | `Callable[[int, int], None]` | `None` | 进度回调 `(已传字节, 总字节)` |

返回上传后的 `FileInfo`，失败返回 `None`。

**`mkdir(parent_id, name) → Optional[str]`**

在指定目录下创建子目录。

| 参数 | 类型 | 说明 |
|------|------|------|
| `parent_id` | `str` | 父目录 ID |
| `name` | `str` | 新目录名称 |

返回新目录 ID，失败返回 `None`。

---

#### `drive.download` — 下载

**`download_file(file_id, local_path) → bool`**

下载文件到本地。

| 参数 | 类型 | 说明 |
|------|------|------|
| `file_id` | `str` | 文件 ID |
| `local_path` | `str` | 保存到的本地路径 |

**返回值：** `bool` — `True` 成功，`False` 失败。

---

#### `drive.delete` — 删除

**`delete(file_id) → bool`**

删除文件或目录。

| 参数 | 类型 | 说明 |
|------|------|------|
| `file_id` | `str` | 文件/目录 ID |

**返回值：** `bool` — `True` 成功，`False` 失败。

---

#### `drive.sync` — 增量同步

**`get_changes_start_token() → Optional[str]`**

获取变更追踪的起始 token，无参数。

**返回值：** `Optional[str]` — 起始 token，`None` 表示该网盘不支持 Changes API。

**`list_changes(token, root_path) → Tuple[List[ChangeItem], Optional[str]]`**

列出自上次 token 以来的文件变更。

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `token` | `str` | — | 上一次获取的变更 token |
| `root_path` | `str` | `"/"` | 限定监听的根路径 |

**返回值：** `Tuple[List[ChangeItem], Optional[str]]` — `(变更列表, 新 token)`。`ChangeItem` 定义见下方数据模型。

---

#### `fs.read` / `fs.write` — 文件存储

通过 `context.get_fs()` 获取 `FileProxy`。所有操作限制在 `plugin_data/{plugin_id}/` 目录内，防止路径遍历。

需在 manifest 中声明 `fs.read` 和/或 `fs.write` 权限。

| 方法 | 所需权限 | 说明 |
|------|---------|------|
| `read_bytes(path)` | `fs.read` | 读取文件（二进制） |
| `read_text(path, encoding?)` | `fs.read` | 读取文件（文本，默认 UTF-8） |
| `list_dir(path?)` | `fs.read` | 列出目录内容，返回文件名列表 |
| `exists(path)` | `fs.read` | 检查文件/目录是否存在 |
| `write_bytes(path, data)` | `fs.write` | 写入文件（二进制），自动创建父目录 |
| `write_text(path, data, encoding?)` | `fs.write` | 写入文件（文本），自动创建父目录 |
| `mkdir(path)` | `fs.write` | 创建目录（含父目录） |
| `delete(path)` | `fs.write` | 删除文件（不允许删除目录） |
| `delete_dir(path)` | `fs.write` | 递归删除目录（不能删根目录） |
| `root` | 无 | 属性，返回插件数据目录的绝对路径 |

```python
# 示例：用 FileProxy 存储 JSON 配置
fs = context.get_fs()
fs.write_text("config.json", json.dumps({"key": "value"}))
data = json.loads(fs.read_text("config.json"))
```

---

#### `db.read` / `db.write` — 数据库

通过 `context.get_db()` 获取 `DbProxy`。需在 manifest 中声明 `db.read` 和/或 `db.write` 权限。

| 方法 | 所需权限 | 说明 |
|------|---------|------|
| `query(*args)` | `db.read` | 查询数据库，用法同 SQLAlchemy `session.query()` |
| `add(instance)` | `db.write` | 添加记录 |
| `delete(instance)` | `db.write` | 删除记录 |
| `commit()` | `db.write` | 提交事务 |
| `rollback()` | 无 | 回滚事务，始终可用 |
| `close()` | 无 | 关闭连接，始终可用 |

> **推荐**：大多数插件应优先使用 `FileProxy` (`fs.read/fs.write`) 存储自身数据（如模板、配置），仅在需要查询宿主数据表时才使用 `DbProxy`。

#### 数据模型

**`FileInfo`** — 文件/目录信息，`list_files` / `upload_file` 等方法的返回类型。

| 字段 | 类型 | 必选 | 说明 |
|------|------|------|------|
| `id` | `str` | ✅ | 文件/目录的唯一 ID |
| `name` | `str` | ✅ | 文件名或目录名 |
| `size` | `int` | ✅ | 文件大小（字节），目录为 `0` |
| `is_dir` | `bool` | ✅ | `True` 表示目录 |
| `mime_type` | `str` | ❌ | MIME 类型，如 `"image/png"` |
| `parent_id` | `str` | ❌ | 父目录 ID |
| `modified_at` | `float` | ❌ | 最后修改时间（Unix 时间戳） |
| `target_id` | `str` | ❌ | 快捷方式指向的实际文件 ID（Google Drive 专用） |

**`ChangeItem`** — 文件变更记录，`list_changes` 的返回类型。

| 字段 | 类型 | 必选 | 说明 |
|------|------|------|------|
| `file_id` | `str` | ✅ | 文件 ID |
| `name` | `str` | ✅ | 文件名 |
| `relative_path` | `str` | ✅ | 相对于同步根路径的路径 |
| `size` | `int` | ✅ | 文件大小（字节），默认 `0` |
| `modified_at` | `float` | ❌ | 最后修改时间（Unix 时间戳） |
| `removed` | `bool` | ✅ | `True` 表示文件被删除，默认 `False` |

### 3. 实现 PluginInterface

```python
# main.py
import sys
from pathlib import Path

# 让同目录模块可被导入（必须，因为框架用 spec_from_file_location 加载）
_plugin_dir = str(Path(__file__).parent)
if _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)

from app.plugin.base import HookContext, PluginContext, PluginInterface, PluginMeta
```

插件类必须继承 `PluginInterface` 并实现以下方法：

| 方法 | 签名 | 说明 |
|------|------|------|
| `get_meta` | `() → PluginMeta` | 返回插件元信息 |
| `on_load` | `(context: PluginContext) → None` | 插件启用时调用，在此注册钩子和路由 |
| `on_unload` | `() → None` | 插件禁用/卸载时调用，清理自身资源 |

```python
import json
from pathlib import Path

class MyPlugin(PluginInterface):

    def __init__(self):
        self._context = None
        # 从 manifest.json 读取元数据（唯一真相源）
        manifest_path = Path(__file__).parent / "manifest.json"
        with open(manifest_path, "r", encoding="utf-8") as f:
            self._meta = PluginMeta(**json.load(f))

    def get_meta(self) -> PluginMeta:
        return self._meta

    async def on_load(self, context: PluginContext) -> None:
        self._context = context

        # 注册钩子
        context.hooks.register(
            "before_rename",
            self._on_before_rename,
            plugin_id=context.plugin_id,
        )

        # 注册路由（可选）
        from fastapi import APIRouter
        router = APIRouter()

        @router.get("/hello")
        async def hello():
            return {"message": "hello from my plugin"}

        context.register_router(router, prefix="/my-plugin", tags=["我的插件"])
        context.logger.info("[MyPlugin] Loaded")

    async def on_unload(self) -> None:
        if self._context:
            self._context.logger.info("[MyPlugin] Unloaded")
        self._context = None

    @staticmethod
    async def _on_before_rename(ctx: HookContext):
        # 在此处理钩子逻辑
        return ctx
```

### 4. 关键 API

#### context.hooks — 钩子注册

```python
# 注册
context.hooks.register(hook_name, handler, plugin_id=context.plugin_id, priority=100)

# handler 签名
async def handler(ctx: HookContext) -> Optional[HookContext]:
    # ctx.data 是可读写的字典
    # 返回 ctx 表示修改了数据，返回 None 表示不修改
    ...
```

**可用钩子：**

| 钩子 | 触发时机 | 所需权限 |
|------|---------|---------|
| `before_upload` / `after_upload` | 上传前后 | `drive.upload` |
| `before_rename` / `after_rename` | 重命名前后 | `drive.rename` |
| `before_sync` / `after_sync` | 同步前后 | `drive.sync` |
| `on_file_detected` | 检测到文件 | `file.read` |
| `on_startup` / `on_shutdown` | 系统启停 | 无 |
| `on_error` | 错误发生 | 无 |

#### context.get_drive() — 获取网盘实例

```python
# 返回 DriveProxy，只允许调用 manifest 中声明权限对应的方法
drive = await context.get_drive(drive_config_id)

# 例：声明了 drive.list + drive.rename
await drive.list_files(parent_id)   # ✅
await drive.rename(file_id, name)   # ✅
await drive.delete(file_id)         # ❌ PermissionError
```

#### context.register_router() — 注册 API 路由

```python
context.register_router(router, prefix="/my-route", tags=["标签"])
# 最终路径: /api/plugins/{plugin_id}/my-route/...
```

#### context.get_fs() — 获取文件存储代理

```python
# 需声明 fs.read 和/或 fs.write 权限
fs = context.get_fs()

# 读写 JSON
if fs.exists("templates.json"):
    data = json.loads(fs.read_text("templates.json"))
fs.write_text("templates.json", json.dumps(data, ensure_ascii=False))

# 存储路径：plugin_data/{plugin_id}/templates.json
```

#### context.get_db() — 获取数据库代理

```python
# 需声明 db.read 和/或 db.write 权限
db = context.get_db()
results = db.query(SomeModel).all()  # 需 db.read
db.add(record)                       # 需 db.write
db.commit()
db.close()
```

### 5. UI 插件开发

除了后端钩子和路由，插件还可以提供前端 UI，嵌入到 DriveCat 的文件浏览器等界面中。

#### 5.1 manifest.json 中的 `ui` 字段

```json
{
  "name": "我的插件",
  "version": "1.0.0",
  "author": "YourName",
  "description": "插件功能描述",
  "hooks": ["before_rename"],
  "permissions": ["drive.list", "drive.rename"],
  "source": "official",
  "source_url": "https://github.com/Bespertrijun/drivecat-official-plugins",
  "changelog": "v1.0.0: 初始版本",
  "entry": "main.MyPlugin",
  "ui": {
    "mode": "iframe",
    "entry": "ui/index.html",
    "hooks": [
      {
        "position": "file.context_menu",
        "label": "我的操作",
        "icon": "CreateOutline",
        "match": {}
      }
    ]
  }
}
```

| 字段 | 说明 |
|------|------|
| `ui.mode` | 目前只支持 `"iframe"` |
| `ui.entry` | 入口 HTML 文件，相对于插件目录 |
| `ui.hooks` | UI 挂载点列表 |
| `ui.hooks[].position` | 挂载位置，如 `"file.context_menu"` (右键菜单)、`"dashboard.widget"` |
| `ui.hooks[].label` | 菜单/按钮文字 |
| `ui.hooks[].icon` | ionicons5 图标名，如 `"CreateOutline"` |
| `ui.hooks[].match` | 过滤条件，如 `{"is_dir": true}` 只在目录上显示。空对象 `{}` 表示不过滤 |

#### 5.2 通信协议 (`drivecat.plugin.v1`)

插件 UI 运行在 iframe 沙箱中，通过 `postMessage` 与宿主通信。协议名：`drivecat.plugin.v1`。

**宿主 → 插件：`host.init`**

插件加载完成后，宿主会发送初始化消息：

```js
{
  protocol: "drivecat.plugin.v1",
  type: "host.init",
  pluginId: "abc123...",
  payload: {
    token: "eyJ...",           // 受限 JWT，30 分钟有效
    theme: "dark",             // "dark" 或 "light"
    cssVars: {                 // 宿主当前 CSS 变量
      "--dc-primary": "#6c63ff",
      "--dc-bg-card": "#16213e",
      "--dc-text-primary": "#e0e0e0",
      // ... 完整列表见下方
    },
    context: {                 // 业务上下文
      plugin_id: "abc123...",
      drive_id: 1,             // 当前网盘 ID
      parent_id: "0",          // 当前目录 ID
      selected_file: {         // 选中的文件/目录（可能为 null）
        id: "file_id",
        name: "文件名",
        is_dir: false,
      }
    }
  }
}
```

**宿主注入的 CSS 变量：**

| 变量 | 用途 |
|------|------|
| `--dc-primary` | 主色 |
| `--dc-primary-hover` | 主色悬停态 |
| `--dc-bg-card` | 卡片背景 |
| `--dc-bg-elevated` | 提升层背景 |
| `--dc-bg-surface` | 页面底色 |
| `--dc-text-primary` | 主文本色 |
| `--dc-text-secondary` | 次要文本色 |
| `--dc-text-tertiary` | 辅助文本色 |
| `--dc-border` | 边框色 |
| `--dc-error` | 错误色 |
| `--dc-success` | 成功色 |
| `--dc-warning` | 警告色 |

**插件 → 宿主：**

| 消息类型 | payload | 说明 |
|---------|---------|------|
| `plugin.toast` | `{ message, type }` | 在宿主显示 toast。`type` 可选 `"info"` / `"success"` / `"error"` / `"warning"` |
| `plugin.resize` | `{ height }` | 通知宿主调整 iframe 高度 |
| `plugin.close` | — | 通知宿主关闭插件面板 |

#### 5.3 使用共享 SDK

本仓库提供了 `plugins/_shared/sdk.js`，封装了上述通信协议。引入后即可使用：

```html
<script src="../_shared/sdk.js"></script>
<script>
  // 初始化回调
  DriveCat.onInit(function (ctx) {
    console.log(ctx.drive_id, ctx.parent_id, ctx.selected_file)
  })

  // API 调用（自动带 token 鉴权）
  DriveCat.api('POST', '/my-plugin/action', { key: 'value' })
    .then(function (res) { /* ... */ })

  // 宿主 toast
  DriveCat.toast('操作成功', 'success')

  // 调整 iframe 高度
  DriveCat.resize()

  // 关闭插件面板
  DriveCat.close()
</script>
```

SDK 会自动处理：
- `host.init` 握手和 token 管理
- CSS 变量注入（宿主主题同步）
- API 请求鉴权（Bearer token）

#### 5.4 插件 API 路由

插件在 `on_load` 中通过 `context.register_router()` 注册的路由，最终路径为：

```
/api/plugins/{plugin_id}/{prefix}/...
```

UI 端通过 `DriveCat.api(method, path, body)` 调用，`path` 只需写 `/{prefix}/...` 部分。

### 6. 注意事项

- **禁止相对导入** — 框架用 `spec_from_file_location` 加载入口模块，无包身份，`from .xxx import` 会报错。同目录模块用绝对导入 `from xxx import` 即可（前提是已将插件目录加入 `sys.path`）。
- **权限最小化** — 只声明实际需要的权限，未声明的方法调用会抛 `PermissionError`。
- **数据存储优先用 FileProxy** — 插件自身数据（模板、配置等）用 `context.get_fs()` 存在 `plugin_data/{plugin_id}/` 下，安全隔离。仅在需要查询宿主数据表时才用 `context.get_db()`。
- **沙箱执行** — 钩子 handler 在进程沙箱中执行，连续失败 3 次会被自动禁用。
- **卸载自动清理** — 框架会自动注销钩子和删除路由，`on_unload` 中只需清理插件自身资源。
- **元数据唯一来源** — `manifest.json` 是插件元数据的唯一真相源。`get_meta()` 应直接读取 manifest，避免硬编码导致版本号、描述等在两处漂移。
## 本地开发与测试（Plugin Dev Runtime）

插件开发者无需安装 DriveCat 主服务，使用内置的 **Plugin Dev Runtime** 即可在本地测试插件 UI 和后端逻辑。

```bash
pip install -r requirements.txt
python devrt/server.py plugins/rename
# → 🐱 Plugin Dev Runtime — http://localhost:9000
```

打开浏览器即可看到：
- **中央**：插件 UI（iframe 嵌入，自动注入 DriveCat 暗色主题变量）
- **DevTools 面板**：实时显示 Toast、Resize、API 调用日志（默认桌面展开、移动端折叠，header 上的 `DevTools` 按钮随时切换）
- **顶部**：入口模式切换（独立入口 / 右键入口 / 仪表盘卡片）+ 主题切换（深色 / 浅色）

### 入口模式（对应宿主三种 plugin position）

| 模式 | 模拟的宿主位置 | 容器形态 | 适用场景 |
|------|---------------|---------|---------|
| **独立入口（全屏）** | `PluginRuntimeView` (`/plugin-runtime/:id`) | 全宽 iframe，圆角 16px | 用户从插件列表点击进入插件主页面 |
| **右键入口（弹窗）** | `FileBrowser` 的 `n-modal` | 800×85vh modal，圆角 16px | 用户在文件上右键触发 `file.context_menu` |
| **仪表盘卡片** | `dashboard.widget`（前瞻模拟） | 400×320 卡片，圆角 10px，3px 渐变顶条 | 仪表盘上的小组件 |

> 容器尺寸、圆角、阴影、CSS 变量与真实 DriveCat 主应用一一对齐，验证插件 UI 在三种宿主形态下的适配效果。`dashboard.widget` 后端已定义但 DriveCat 前端尚未实装，DevRT 提前模拟便于插件作者预先适配。

### Dev Runtime 提供的能力

| 能力 | 说明 |
|------|------|
| Mock Drive | 内存虚拟文件系统，含动漫/电影示例文件 |
| FileProxy | 真实文件 I/O 沙箱（`devrt_data/{plugin_id}/`） |
| 路由注册 | 插件的 FastAPI 路由正常挂载 |
| SSE 流式 | 支持 `StreamingResponse` |
| iframe 协议 | 完整的 `host.init` / `plugin.toast` / `plugin.resize` / `plugin.close` |
| DevTools 浮层 | 可切换显隐，桌面右侧固定面板、移动端底部抽屉，不挤压 iframe 空间 |
| 响应式断点 | `≤768px` 移动端布局；`≤380px` 小屏（如 iPhone SE）进一步紧凑 |

```bash
# 自定义端口
python devrt/server.py plugins/rename --port 8080
```

> **注意**：`devrt_data/` 已被 `.gitignore` 忽略，不会进入版本控制。

## 构建与发布

```bash
# 正式构建（生成 dist/，_shared/ 自动打入每个插件 zip）
python scripts/build.py

# 本地开发（将 _shared/ 同步到每个插件目录，使 iframe 相对路径可用）
python scripts/build.py --dev

# 签名（可选）
# 设置环境变量 SIGNING_KEY 或放置 keys/private.pem
```

构建后提交并推送，GitHub Pages 自动部署。

> **注意**：`--dev` 生成的 `plugins/*/_shared/` 副本已被 `.gitignore` 忽略，不会进入版本控制。

### 版本号（基于 Git Tag）

版本号完全由 git tag 驱动，**不需要** 在 manifest 中写 `version` 字段。

**Tag 命名规范**：`{插件目录名}/v{semver}`

```bash
# 发布 rename 插件 1.0.0
git tag rename/v1.0.0

# 发布 1.1.0
git tag rename/v1.1.0

# 推送 tag
git push origin --tags
```

| 操作 | Tag | 构建版本 |
|------|-----|---------|
| `git tag rename/v1.0.0` | `rename/v1.0.0` | `1.0.0` |
| `git tag rename/v1.0.1` | `rename/v1.0.1` | `1.0.1` |
| `git tag rename/v1.1.0` | `rename/v1.1.0` | `1.1.0` |

**没有 tag 的插件会被跳过**，build.py 会提示创建 tag。

构建时 zip 包内的 `manifest.json` 会被自动写入 tag 对应的版本号，运行时 `get_meta()` 返回的版本与市场一致。

### Changelog 自动生成

`build.py` 的 changelog 生成逻辑：

1. `manifest.json` 中手写 `"changelog"` → **最高优先级**（手动 override）
2. 否则 → **自动从两个相邻 tag 之间的 git log 生成**

```
# 假设有两个 tag：rename/v1.0.0 和 rename/v1.1.0
# changelog 内容 = git log rename/v1.0.0..rename/v1.1.0 -- plugins/rename/

- feat: 支持文件夹批量重命名
- fix: 修复预览区不刷新
- refactor: 模板存储改用 FileProxy
```

如果只有一个 tag（首次发布），changelog 包含该 tag 之前的所有 commits。

`index.json` 中还会生成 `versions` 数组，包含所有历史版本及各自的 changelog，供市场前端展示版本选择器。