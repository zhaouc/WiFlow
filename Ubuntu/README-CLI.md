# WiFlow Ubuntu 1.0.0：桌面与终端

桌面界面与 Windows 版共用布局、设置项和认证状态。学校网页内嵌在主窗口；
Wi-Fi 与有线开关独立生效，关闭窗口后可从托盘恢复或退出。
已识别的 ProbeSocket 校园门户直接按网卡提交认证并检查普通 HTTP 网页是否被拦截，
启动、新建连接和“立即检测认证”都会实际执行登录；普通探测页可访问不会跳过这些触发。
PPPoE、802.1X 复用 NetworkManager 配置，专用客户端选择 Ubuntu 可执行程序。

一个 DEB 同时提供桌面程序和 SSH/终端登录。两种入口共用当前用户的配置与系统密钥环。

## 安装与启动

```bash
sudo apt install ./wiflow_1.0.0_$(dpkg --print-architecture).deb
wiflow gui              # 桌面程序，也可从应用列表启动 WiFlow
wiflow-cli --help       # 终端命令列表，不需要 DISPLAY
wiflow-cli status       # 无线、有线连接和默认上网接口
wiflow-cli status --json
wiflow-cli diagnose
```

`wiflow` 不带参数仍打开桌面；`wiflow status` 等价于 `wiflow-cli status`。
桌面自启动及原有 `--diagnose`、`--smoke-test` 参数保留。

## 配置、连接、认证

```bash
wiflow-cli configure --mode wifi --ssid '学校Wi-Fi' \
  --portal-url 'https://学校认证地址/' --username '学号'
wiflow-cli connect --mode wifi
wiflow-cli login --mode wifi

wiflow-cli configure --mode wired --ethernet-name 'eno1' \
  --portal-url 'https://学校认证地址/' --username '学号'
wiflow-cli connect --mode wired
wiflow-cli login --mode wired
```

校园网密码在登录时优先读取系统密钥环，未找到时以隐藏输入方式询问。
`connect` 的密码是 Wi-Fi 密码，`login` 的密码是校园网账号密码。
如需保存密码，可在交互终端运行 `configure --mode wifi --set-password` 或
`configure --mode wifi --set-wifi-password`。保存需要已解锁的桌面密钥环；SSH 环境
可在每次登录时输入密码，无需将密码持久化。

非交互环境可用 `login --password-stdin` 或 `connect --password-stdin` 从标准输入
读取一行密码。建议由密码管理器提供输入，避免把密码字面值写入 shell 命令或历史。
程序不支持命令行密码参数，不把密码写入配置、日志或进程参数。
CLI 修改配置后请重新打开已有的桌面程序，以读取新配置。

## 登录行为

Ubuntu 自动认证不设置认证冷却。当前目标网络已连接时，立即连接不会重复激活系统网络配置。

已支持的 ProbeSocket 门户在终端中直接认证；其他门户在私有 Xvfb 显示器中运行 WebKitGTK，可以处理延迟渲染的常见登录表单，
无需已有的图形桌面会话。安装包会自动安装 Xvfb 和 xauth 依赖。
提交前检查实际 SSID、有线配置和默认路由，避免通过其他网络提交账号。
无线、有线同时连接时，已支持的 ProbeSocket 门户分别绑定接口认证；其他网页门户使用默认上网接口。程序不自动切换系统路由。

默认最多等待 45 秒，可使用 `--timeout 90`（范围 5–300 秒）。已能访问互联网时
跳过提交；如需主动重新登录，可用 `--force`。
“互联网已连通”是网络探测结果，不能单独证明学校服务器端的认证会话。
验证码、短信验证、SSO、弹窗或特殊客户端协议可能需要桌面交互。

桌面自动认证支持已配置的 HTTP/HTTPS 门户；HTTP 会明文传输密码。
HTTPS 证书必须可信。学校使用自签名证书时，可提供经独立核实的服务器 PEM 证书：

```bash
wiflow-cli login --mode wifi --trust-cert /path/to/school-server.pem
```

只对指定认证来源放行与该文件完全一致的服务器证书，不关闭全局 TLS 检查。
学校门户确实仅使用 HTTP 时，可显式添加 `--allow-http`；此时密码以明文传输。

退出码：0 为当前网络已连通；2 为参数、配置或运行环境错误；3 为网络/路由不匹配；
4 为需要交互；5 为加载失败或超时；6 为证书错误；130 为用户取消。

## 开发与验证

```bash
/usr/bin/python3 -m compileall -q .
/usr/bin/python3 -m unittest discover -p 'test_*.py' -v
xvfb-run -a /usr/bin/python3 web_main.py --smoke-test
xvfb-run -a /usr/bin/python3 verify_terminal_login.py
/usr/bin/python3 package.py all
```

终端集成测试使用临时本地 HTTPS 门户和虚构账号，通过实际 WebKit 填写、提交表单，
不会向真实学校门户发送凭据。需要 openssl 命令。ARM64 包仍需在对应硬件上验证。
