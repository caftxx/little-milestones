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

先准备一个 provider 配置文件。仓库里已经提供了示例 [`providers.example.json`](/mnt/c/Users/caft/Desktop/code/little-milestones/providers.example.json)，可以直接复制一份改成自己的配置。

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

- 任务生成时间
- 输入目录
- 模型信息
- provider 使用统计
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
- `provider_name`
- `provider_base_url`
- `provider_model`
- `provider_elapsed_ms`

其中：

- `gps` 优先来自 EXIF
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

它表示本次 `describe_directory()` 从开始扫描到生成结果文档的整次墙钟耗时，单位毫秒。

## 测试

```bash
uv run --extra dev pytest
```
