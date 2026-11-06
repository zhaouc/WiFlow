# WiFlow 1.0.0

Windows 与 Ubuntu 桌面校园网客户端，包含桌面界面、内嵌认证页及独立的有线 / Wi-Fi 自动认证。
Ubuntu 另提供命令行入口，点击“立即认证”会填写并提交已保存的账号密码。

## 目录

- `Windows/source/`：Windows 源码及必需资源。
- `Ubuntu/`：Ubuntu 源码、打包脚本和回归测试。
- `.github/workflows/main.yml`：Windows / Ubuntu 自动构建流程。

此包不包含用户账号、密码、配置、备份、缓存、运行日志或历史构建产物。
Windows 的 `models/` 为程序所需资源，保留完整；本源码包未附带 Windows EXE。

## Ubuntu 安装与使用

配套安装包单独提供：普通 x64 电脑使用 `wiflow_1.0.0_amd64.deb`，ARM64 使用对应文件。

```bash
sudo apt install ./wiflow_1.0.0_$(dpkg --print-architecture).deb
wiflow gui
wiflow-cli --help
```

源码构建：

```bash
cd Ubuntu
/usr/bin/python3 -B -m unittest discover -p 'test_*.py' -q
/usr/bin/python3 package.py all
```

生成安装包在 `Ubuntu/dist/`。安装依赖包含 GTK3、WebKitGTK、NetworkManager、Secret Service 和托盘绑定。
不依赖“高级网络配置”；系统连接在 Ubuntu 系统设置中配置好后由 WiFlow 复用。
具体终端选项见 `Ubuntu/README-CLI.md`。

## Windows 构建

需要 Windows 10/11、.NET 10 SDK 和 NuGet 网络访问：

```powershell
cd Windows
.\package-portable.ps1 -Runtime win-x64
```

生成文件为 `Windows/dist/WiFlow-Windows-x64-Portable-1.0.0.zip`。
目标电脑需有 Microsoft Edge WebView2 Runtime。解压运行时保留 `models/` 和原生运行库文件。
也可以将目录作为 GitHub 仓库，运行自带 Actions 流程生成两平台安装包。

## 验证范围

Ubuntu 101 项单元测试、主窗口内嵌网页和单击自动提交已通过。
已验证配置的学校认证接口及实际有线 / 无线自动认证。
ARM64 包完成打包与内容检查，未做 ARM64 实机运行；Windows 构建由上述脚本或 Actions 执行。
