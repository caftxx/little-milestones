# Little Milestones

[![Tests](https://github.com/caftxx/little-milestones/actions/workflows/tests.yml/badge.svg)](https://github.com/caftxx/little-milestones/actions/workflows/tests.yml)

**AI-powered monthly growth reports for babies and pets**

Little Milestones 是一个围绕成长记录整理的小工具项目。

当前第一版先只做一件事：

输入一批照片，优先解析照片自带的 EXIF 元信息，再调用本地视觉大模型补充图片内容描述，最终输出结构化 JSON。

这个 JSON 会为下一阶段的“宝宝月报生成器”提供稳定输入。

## 当前范围

- 输入：照片目录
- 元信息来源：优先使用 EXIF
- 内容理解：本地视觉大模型
- 输出：结构化 JSON

当前版本不包含：

- 月报生成
- 网页界面
- 云端识图服务
- 视频处理

## 项目简介

很多成长瞬间都藏在照片里，但如果后续想做月报、纪念册或成长总结，第一步不是排版，而是先把照片变成可消费的数据。

Little Milestones v1 的目标，就是把这条链路先跑通：

- 读取照片
- 提取 EXIF 里的时间、设备、GPS 等元信息
- 如果 EXIF 没有 GPS，则回退到默认坐标 `30.346701,120.002066`
- 调用本地 OpenAI 兼容视觉模型生成动作、表情、场景、高光等描述
- 输出统一 JSON，供后续月报生成使用

## 安装与运行

项目使用 `uv` 管理依赖。

```bash
uv sync --extra dev
```

可以通过环境变量配置本地模型：

```powershell
$env:OPENAI_BASE_URL="http://127.0.0.1:1234/v1"
$env:OPENAI_API_KEY="local"
$env:VISION_MODEL="your-local-vision-model"
```

运行命令：

```bash
uv run littlems describe --input ./photos --output ./descriptions.json
```

也可以直接通过命令行传入，且命令行参数优先级高于环境变量：

```bash
uv run littlems describe \
  --input ./photos \
  --output ./descriptions.json \
  --openai-base-url http://127.0.0.1:1234/v1 \
  --openai-api-key local \
  --vision-model your-local-vision-model
```

可选参数：

- `--recursive`：递归扫描子目录
- `--openai-base-url`：覆盖 `OPENAI_BASE_URL`
- `--openai-api-key`：覆盖 `OPENAI_API_KEY`
- `--vision-model`：覆盖 `VISION_MODEL`
- `--log-level`：设置日志级别，排查问题时可用 `DEBUG`

如果命令行参数和环境变量都未提供，CLI 会直接报错并提示需要补齐配置。

调试时可以这样运行：

```bash
uv run littlems describe \
  --input ./photos \
  --output ./descriptions.json \
  --log-level DEBUG
```

## 输出内容

输出 JSON 顶层包含：

- 任务生成时间
- 输入目录
- 模型信息
- 总文件数、成功数、失败数
- `records`
- `failures`

每条照片记录至少包含这些字段：

- `file_name`
- `file_path`
- `captured_at`
- `timezone`
- `location`
- `gps`
- `device`
- `summary`
- `baby_present`
- `actions`
- `expressions`
- `scene`
- `objects`
- `highlights`
- `uncertainty`
- `metadata_source`

其中：

- `gps` 优先来自 EXIF
- 如果 EXIF 不包含 GPS，则使用默认值 `30.346701,120.002066`
- `metadata_source` 会标记字段来源，例如 `exif`、`default_gps`

## 测试

```bash
uv run --extra dev pytest
```