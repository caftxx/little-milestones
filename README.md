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

- 网页界面
- 云端识图服务
- 视频处理

当前支持的图片格式：

- `.jpg`
- `.jpeg`
- `.png`
- `.webp`
- `.heif`
- `.heic`
- `.dng`

## 项目简介

很多成长瞬间都藏在照片里，但如果后续想做月报、纪念册或成长总结，第一步不是排版，而是先把照片变成可消费的数据。

Little Milestones v1 的目标，就是把这条链路先跑通：

- 读取照片
- 提取 EXIF 里的时间、设备、GPS 等元信息
- 如果 EXIF 缺少拍摄时间，则先尝试从文件名解析；仍然缺失时再回退到文件时间
- 如果 EXIF 没有 GPS，则回退到默认坐标 `30.346701,120.002066`
- 调用本地 OpenAI 兼容视觉模型生成动作、表情、场景、高光等描述
- 输出统一 JSON，供后续月报生成使用

其中 `.heif`、`.heic` 和 `.dng` 需要额外的解码依赖。项目已经在默认依赖里包含 `pillow-heif` 和 `rawpy`，正常通过 `uv sync` 安装即可。

## 安装与运行

项目使用 `uv` 管理依赖。

```bash
uv sync --extra dev
```

先准备一个 provider 配置文件。仓库里已经提供了示例 [`providers.example.json`](providers.example.json)，可以直接复制一份改成自己的配置。

示例内容如下：

```json
{
  "providers": [
    {
      "name": "vision-a",
      "base_url": "http://127.0.0.1:1234/v1",
      "api_key": "local",
      "vision_model": "your-local-vision-model",
      "max_inflight": 2
    },
    {
      "name": "vision-b",
      "base_url": "http://192.168.1.20:1234/v1",
      "api_key": "local",
      "vision_model": "your-other-vision-model",
      "max_inflight": 1
    }
  ]
}
```

运行命令：

```bash
uv run littlems describe \
  --input ./photos \
  --output ./descriptions.json \
  --provider-config ./providers.json
```

生成温馨中文月报：

```bash
uv run littlems generate-report \
  --input ./descriptions.json \
  --month 2026-03 \
  --birth-date 2025-12-20 \
  --baby-name 小满 \
  --output ./report.md \
  --provider-config ./providers.json
```

如果还想保留一份调试用 JSON，方便回看模型输入摘要和实际使用的 provider，可以额外加上：

```bash
uv run littlems generate-report \
  --input ./descriptions.json \
  --month 2026-03 \
  --birth-date 2025-12-20 \
  --baby-name 小满 \
  --output ./report.md \
  --json-output ./report-debug.json \
  --provider-config ./providers.json
```

如果想先验证配置文件本身是否可用，可以先运行：

```bash
uv run littlems validate-config --provider-config ./providers.json
```

如果还想顺便逐个 provider 做一次轻量接口探测，可以加上 `--probe`：

```bash
uv run littlems validate-config --provider-config ./providers.json --probe
```

带 `--probe` 时，终端会按 provider 输出 `OK` 或 `FAIL` 摘要，便于快速定位是哪台机器或哪个模型配置有问题。
失败摘要会额外给出一个简短的 `kind` 分类，例如 `timeout`、`unauthorized`、`not_found`、`rate_limited`。

可选参数：

- `--recursive`：递归扫描子目录
- `--provider-config`：provider 配置文件路径，必填
- `--log-path`：覆盖日志文件输出路径
- `--log-level`：设置日志级别，排查问题时可用 `DEBUG`

如果未提供 `--provider-config`，CLI 会直接报错。

默认情况下，日志会写入当前工作目录下的 `log/littlems.log`，不会打印到终端；终端只显示处理进度条。

`describe` 命令会把结果增量写入 `--output` 指定的 JSON 文件。也就是说，处理过程中这个文件始终保持可读取状态；如果 CLI 因异常、手动中断或机器重启而提前退出，下一次用同一个 `--output` 重新执行时，会自动读取已有结果并恢复任务：

- 已成功写入 `records` 的文件会直接跳过
- 已写入 `failures` 的文件会重新尝试处理
- 输出文件的输入目录、递归参数或 provider 列表不一致时，会拒绝恢复，避免混写到同一个结果文件

调试时可以这样运行：

```bash
uv run littlems describe \
  --input ./photos \
  --output ./descriptions.json \
  --provider-config ./providers.json \
  --log-level DEBUG
```

不同 provider 的实际并发由各自的 `max_inflight` 控制；当某个 provider 达到上限后，调度器会优先把任务派发到其他仍有余量的 provider。

如果你想自定义日志文件位置，可以显式传入：

```bash
uv run littlems describe \
  --input ./photos \
  --output ./descriptions.json \
  --provider-config ./providers.json \
  --log-path ./runtime/littlems-debug.log
```

## 输出内容

输出 JSON 顶层包含：

- 输出格式版本
- 任务状态
- 任务生成时间
- 最近一次更新时间
- 输入目录
- 模型信息
- provider 使用统计
- 总文件数、成功数、失败数、跳过数、剩余数
- `records`
- `failures`
- `run_state`

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
- `provider_name`
- `provider_base_url`
- `provider_model`
- `provider_elapsed_ms`

其中：

- `gps` 优先来自 EXIF
- `captured_at` 优先来自 EXIF；若缺失则尝试从文件名解析，例如 `IMG_20260223_222426.dng`
- 如果文件名也无法解析时间，则回退到文件时间
- `timezone` 优先来自 EXIF；若缺失则跟随文件名解析结果或文件时间推断本地时区偏移
- 如果 EXIF 不包含 GPS，则使用默认值 `30.346701,120.002066`
- `metadata_source` 会标记字段来源，例如 `exif`、`default_gps`
- `provider_elapsed_ms` 表示这张图片在 provider 处理阶段累计消耗的毫秒数
  它是单图片的 provider attempt 累计值，不等于整次任务或某个 provider 的墙钟时间

顶层 `provider_stats` 中还会额外包含：

- `wall_clock_ms`
- `wall_clock_ms_avg`

它们分别表示：

- `wall_clock_ms`: 每个 provider 从第一条 attempt 开始到最后一条 attempt 结束的占用窗口，单位毫秒
- `wall_clock_ms_avg`: 每个 provider 各次 attempt 的单次墙钟耗时平均值，单位毫秒

顶层 `summary` 中还包含：

- `wall_clock_ms`
- `skipped`
- `remaining`

它表示本次 `describe_directory()` 从开始扫描到生成结果文档的整次墙钟耗时，单位毫秒。

其中：

- `skipped`：恢复执行时直接复用、未重新处理的成功文件数
- `remaining`：当前输出文件快照里仍未成功也未失败落盘的文件数；任务完成后会变为 `0`

顶层还新增了：

- `version`: 当前输出格式版本，现为 `2`
- `status`: `running` 或 `completed`
- `updated_at`: 当前文件最近一次写盘时间
- `run_state.completed_files`: 已成功文件的绝对路径，用于断点续跑时跳过
- `run_state.failed_files`: 当前仍处于失败状态的文件绝对路径

出于恢复能力考虑，程序会在处理过程中反复重写整个 JSON 文件，但采用同目录临时文件加原子替换的方式，避免半写坏正式输出文件。

## 月报生成

`generate-report` 会读取 `describe` 生成的 `descriptions.json`，结合宝宝出生日期和指定月份，调用本地 OpenAI 兼容模型，生成一篇温馨的中文月报 Markdown。

这个命令需要 `--provider-config`，因为月报正文不是规则模板拼接，而是由大模型基于当月素材来写作。

必填参数：

- `--input`：`describe` 生成的 JSON 文件
- `--month`：目标月份，格式为 `YYYY-MM`
- `--birth-date`：宝宝出生日期，格式为 `YYYY-MM-DD`
- `--baby-name`：宝宝姓名，会作为月报生成的重要上下文
- `--output`：月报 Markdown 输出路径
- `--provider-config`：provider 配置文件路径

可选参数：

- `--json-output`：调试 JSON 输出路径
- `--log-path`
- `--log-level`

月报生成时会先整理出：

- 当月时间线
- 代表照片摘要
- 高频动作、表情、场景
- 候选新技能
- 宝宝当月月龄
- 宝宝姓名

然后把这些事实素材交给模型，生成一篇更自然、更有情感温度的中文月报。

例如，模型产出的月报可能会像这样：

```md
# 2026年3月宝宝成长月报

这个月，宝宝已经到了满2到3个月的阶段。照片里能感觉到她慢慢从安静地躺着，变成会主动回应世界的小人儿。

最让人惊喜的是，三月里第一次清晰地记录到了她趴卧时抬头、看向镜头的样子。那种努力支起小脑袋、认真望过来的瞬间，会让人一下子意识到，成长真的是悄悄发生的。

除了这些新本领，这个月还有很多温柔的日常：被抱在怀里时安静的神情，躺在床上时松弛的小表情，还有一点点更稳定、更有力的身体动作。
```

## 测试

```bash
uv run --extra dev pytest
```
