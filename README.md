# drivecat-official-plugins

DriveCat 官方插件源。

## 结构

```
plugins/
  rename/           ← 批量重命名插件
    manifest.json
    main.py
    rename_engine.py
    rename_manager.py
dist/                ← GitHub Pages 发布目录（自动生成）
  index.json
  packages/
    rename/1.0.0/plugin.zip
scripts/
  build.py           ← 打包脚本
```

## 开发

### 添加新插件

1. 在 `plugins/` 下创建新目录
2. 编写 `manifest.json` 和插件代码
3. 运行 `python scripts/build.py` 生成 `dist/`
4. 提交并推送，GitHub Pages 自动部署

### 本地构建

```bash
python scripts/build.py
```