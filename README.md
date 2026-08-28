# 期权监控（Ubuntu 部署版）

这是原项目的独立 Linux 迁移目录，包含期权链采集、期货价格、异常判定、长图、阿里云 OSS、钉钉推送和 OpenVLab 排名快照。它不包含任何密钥、SQLite 运行数据、图表或浏览器登录态。

## Ubuntu 22.04 / 24.04 安装

```bash
sudo apt update
sudo apt install -y python3 python3-venv git fonts-noto-cjk
cd /opt/option-monitor
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install --with-deps chromium
cp .env.example .env
chmod 600 .env
```

编辑 `.env`，填写 Orange、RQData、钉钉及 OSS 的值。`ALIYUN_OSS_PREFIX` 是图表在 Bucket 内的对象前缀，例如 `option-monitor/charts`。`fonts-noto-cjk` 为中文图表和 OpenVLab 合图提供默认字体；若使用自定义字体，可填写 `OPTION_MONITOR_FONT_PATH`，或以 `OPENVLAB_FONT_PATH` 单独覆盖 OpenVLab。请勿提交 `.env`。

## 手工验证

```bash
.venv/bin/python scripts/run_options_monitor.py --root . --dry-run
```

正式验证（会上传 OSS、并向钉钉群投递）使用 Linux 投递脚本：

```bash
.venv/bin/python scripts/run_and_deliver_options_monitor.py --root . --force-anomaly-report --force-all-products --require-full-coverage
```

## OpenVLab 登录与截图

OpenVLab 依赖 Linux 本机的持久化 Chromium 配置 `state/openvlab-browser-profile`。Windows 的 Edge 配置或 Cookie 不可迁移；请在服务器上重新登录。

首次登录需要能看到浏览器界面：可在带桌面的服务器控制台运行，或使用临时 VNC/X11 转发。登录并看到排名表后关闭浏览器：

```bash
.venv/bin/python scripts/open_openvlab_login.py --root .
```

验证截图：

```bash
.venv/bin/python scripts/capture_openvlab_ranking.py --root . --output state/openvlab-ranking-test.png
```

后续定时任务使用无头 Chromium。默认使用 Playwright 安装的 Chromium；若想改用系统 Chrome，可在 `.env` 设置 `OPENVLAB_BROWSER_CHANNEL=chrome`。

## 仅输出推送文字（供龙虾调用）

`scripts/output_monitor_message_text.py` 只读取已经生成的 `alerts.json`，把 DingTalk Markdown 正文输出到标准输出。它不采集数据、不出图、不访问 OSS、不发送钉钉。

```bash
# 指定本次运行的正文；默认只输出 Markdown text，不输出标题或任何状态信息
.venv/bin/python scripts/output_monitor_message_text.py --root . --run-id 20260825T111500+0800

# 也可由龙虾直接传入 alerts.json 路径
.venv/bin/python scripts/output_monitor_message_text.py --root . --payload state/outbox/20260825T111500+0800/alerts.json
```

需由龙虾接管发送时，先运行 `scripts/run_options_monitor.py` 生成 ready 清单，再调用本脚本取得正文；不要再对同一清单调用 `run_and_deliver_options_monitor.py`，以免两个发送方重复投递。标准 systemd 任务仍由 `run_and_deliver_options_monitor.py` 负责发送。

## 龙虾即时单品种四格图

`scripts/render_instant_option_chart.py` 供龙虾回答“甲醇的期权情况”等即时查询。每一次调用都会直接请求行情和期权接口，不依赖此前的监控任务、SQLite 快照或已生成的图；它只在调用方指定的位置写出一张 PNG，不会上传 OSS、不会发送钉钉。

龙虾必须传入以下两个参数：

```text
--product <品种中文名或代码>    必填。例如：甲醇、MA、黄金、au。
--output <PNG 输出路径>         必填。由龙虾为每次请求生成唯一的绝对路径，必须以 .png 结尾。
--contract <指定期货合约>       可选。例如：MA609、IF2609。传入后严格按该合约采集。
```

未传 `--contract` 时，脚本先取得当前主力期货合约，再解析对应期权链；传入 `--contract` 时，脚本严格使用该期货合约，不能找到有效、未到期的期权链就返回失败，绝不改用主力或临近合约。郑商所合约可使用 Orange 识别的三位年月形式（如 `MA609`）；中金所等通常为四位年月（如 `IF2609`）。品种代码必须与合约匹配。

其他可选参数：`--root <项目根目录>`（默认脚本所在项目）、`--trading-day YYYYMMDD`（默认北京时间当天；只在需指定交易日的回放/排障时传入）。支持的品种以 `option_monitor/settings.py` 的 `PRODUCTS` 为准。

龙虾调用示例（输出路径由龙虾自行决定，示例不应原样固化）：

```bash
sudo -u optionmonitor -H /opt/option-monitor/.venv/bin/python /opt/option-monitor/scripts/render_instant_option_chart.py \
  --root /opt/option-monitor \
  --product 甲醇 \
  --output /var/tmp/lobster/option-ma-request-001.png
```

指定合约的示例：

```bash
sudo -u optionmonitor -H /opt/option-monitor/.venv/bin/python /opt/option-monitor/scripts/render_instant_option_chart.py \
  --root /opt/option-monitor \
  --product MA \
  --contract MA609 \
  --output /var/tmp/lobster/option-ma609-request-001.png
```

成功时标准输出为机器可读的三行：`IMAGE_PATH=<绝对路径>`、`PRODUCT_CODE=<代码>`、`UNDERLYING=<实际期货合约>`。退出码为 `0` 才可读取并发送 `IMAGE_PATH`；非 `0` 时不应发送旧图或猜测结果。运行依赖 `.env` 中的 `ORANGE_API_TOKEN`；`RQDATA_API_KEY` 可选（缺失或不可用时自动尝试东方财富期货报价）。四格依次为日内期货涨跌、ATM IV、Call/Put 较昨持仓变化、RR25 偏度。即时图的 ΔRR25 为当前值减去本地 `state/option_monitor.sqlite3` 中上一交易日 14:30 基线；基线尚未由监控任务成功写入时显示 `--`。ΔIV 与 RR25 十日排名不计算或展示。

Linux 需安装 `fonts-noto-cjk`；脚本会自动寻找 Noto Sans CJK 或文泉驿字体。若系统字体位置不同，在 `.env` 填写 `OPTION_MONITOR_FONT_PATH=/实际/字体文件.ttc`。

## systemd 定时任务

将仓库部署在 `/opt/option-monitor` 后，调整 `deploy/systemd/option-monitor.service` 的 `User`、`Group` 和目录（如有需要），然后执行：

```bash
sudo cp deploy/systemd/option-monitor.service /etc/systemd/system/
sudo cp deploy/systemd/option-monitor.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now option-monitor.timer
systemctl list-timers option-monitor.timer
```

任务在周一至周五的北京时间 10:15、14:30 运行。它会采集、上传 OSS，并以钉钉签名 Webhook 投递 ready 状态的报告；投递成功后会记录幂等状态，防止重试重复发送。查看结果：

```bash
sudo systemctl status option-monitor.service
sudo journalctl -u option-monitor.service -n 100 --no-pager
```

## Git 中转

此目录已是独立 Git 仓库。创建 GitHub 空仓库后：

```bash
git remote add origin <你的 GitHub 仓库地址>
git branch -M main
git add .
git commit -m "Initial Ubuntu deployment"
git push -u origin main
```
