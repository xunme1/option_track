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
