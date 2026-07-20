# QLIT养成教育助手

一个基于 Flet 的桌面工具，用于抓取 `pass.qlit.edu.cn` 登录后的 `JSESSIONID`，并批量填写“养成教育”记录。

## 项目状态

这是一个个人研究性质的桌面工具，不是学校官方工具，也不是通用校园平台 SDK。

请只在你自己的账号和你明确理解的平台规则范围内使用。

## 主要功能

- Flet GUI，本版本只支持macos
- 用 mitmproxy 抓取微信 OAuth 登录后的 student 域 `JSESSIONID`
- 自动检测 session 是否过期
- 按日期范围补填养成教育记录
- 可选调用 OpenAI 兼容接口生成“行为记录 / 总结反思”

## 目录结构

```text
qlit-flet/
├── app.py                  # Flet GUI 入口
├── qlit_auth_core.py       # student JSESSIONID 抓取主流程
├── qlit_auth_proxy.py      # 系统代理 / CA 证书 / 平台差异封装
├── yangcheng_auto.py       # 养成教育鉴权、AI 生成、提交逻辑
├── session_utils.py        # session 探活
├── assets/                 # 图标、字体等资源
├── build.spec              # macOS PyInstaller 配置
├── build_macos.sh          # macOS 打包脚本
├── build_windows.bat       # Windows 打包脚本
└── requirements.txt
```

## 本地开发

### 环境要求

- Python 3.10+
- `mitmproxy`
- 微信桌面版

### 安装依赖

```bash
cd qlit-flet
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 运行 GUI

```bash
flet run app.py
# 或
python3 app.py
```

### 运行命令行工具

```bash
python3 qlit_auth_core.py
python3 session_utils.py
python3 yangcheng_auto.py 2026-06-01 2026-06-30
```

## 运行时文件

为了避免污染仓库，运行时状态默认统一写到用户目录下：

- `~/.campus_auth/session.txt`
- `~/.campus_auth/settings.json`

兼容旧版本时，部分脚本仍会回退读取仓库内旧的 `session.txt`，但不再把它作为默认写入位置。

## 打包

### macOS

```bash
./build_macos.sh
```

说明：

- 本地需要安装 mitmproxy，脚本会从 Homebrew Cask 目录复制 `mitmproxy.app`
- `build.spec` 使用 PyInstaller 的 onedir + bundle 方案，避免内嵌 `.app` 签名问题

## License

本项目采用 `GPL-3.0` 许可证，见 [LICENSE](LICENSE)。

补充说明：

- 许可证只适用于本仓库中的代码、文档和你有权授权的资源
- 它不额外授予任何对学校平台、账号数据、商标、服务或接口的使用权
- 使用者仍需自行遵守学校规则、平台条款与适用法律
