# 架构说明

这份文档只描述当前准备公开的 `qlit-flet/` 仓库范围，也就是“QLIT养成教育助手”本身。

## 1. 模块划分

### GUI

- `app.py`
  - Flet 桌面界面入口
  - 负责设置读写、session 状态展示、任务调度、日志输出

### 登录凭据抓取

- `qlit_auth_core.py`
  - 启动 mitmdump
  - 写入临时 addon 脚本
  - 轮询结果文件，读取抓到的 `JSESSIONID`

- `qlit_auth_proxy.py`
  - 处理平台相关操作
  - 包括查找 `mitmdump`、安装 CA 证书、启用和恢复系统代理、检测微信进程

### 养成教育自动填写

- `yangcheng_auto.py`
  - 用 student 域 `JSESSIONID` 走完整 SSO 鉴权链
  - 查询学期、分类、已填记录
  - 生成或回退文本
  - 提交或覆盖记录

- `session_utils.py`
  - 用 `admin.jsp` 探活 session 是否过期

## 2. 数据流

### 抓取 student JSESSIONID

```text
用户在 GUI 点击抓取
  -> qlit_auth_core.capture_session()
  -> qlit_auth_proxy 开启系统代理
  -> mitmdump 加载临时 addon
  -> 用户在微信里完成校园系统登录
  -> addon 从 admin.jsp 请求头里的 Cookie 提取 JSESSIONID
  -> 主进程读取结果文件
  -> app.py 保存到 ~/.campus_auth/session.txt
```

### 自动填写养成教育

```text
app.py / yangcheng_auto.py
  -> student JSESSIONID
  -> /student/mobile/sso_pt_yangchengjy/index.jsp
  -> 短 JWT
  -> /yangchengjiaoyu/RemoteAnswer.do?lk=15461423
  -> 长 JWT + ycj JSESSIONID
  -> /yangchengjiaoyu/SSOServerHelper.do?FUNNAME=getAccess
  -> Access token
  -> 查询已填记录 / 提交新记录
```

## 3. 运行时文件

默认运行时状态不写回仓库：

- `~/.campus_auth/session.txt`
- `~/.campus_auth/settings.json`

仓库内可能出现但不应该提交的本地产物：

- `.venv/`
- `build/`
- `dist/`
- `mitmproxy.app/`
- `__pycache__/`
- `.DS_Store`

## 4. 打包思路

### macOS

- `build_macos.sh` 使用 PyInstaller + 自定义 `build.spec`
- `mitmproxy.app` 在打包后再嵌入 `.app/Contents/Resources/`
- 这样能保留 mitmproxy 自身 bundle 结构和签名层级

### Windows

- `build_windows.bat` 使用 `flet pack`
- 依赖系统里已安装的 `mitmdump`

## 5. 当前开源范围

当前仓库只准备公开“养成教育助手”能力。

与课堂回放相关的本地脚本和下载内容，当前不在这个仓库的公开范围内，也已经从 README 和默认 Git 提交范围里排除。
