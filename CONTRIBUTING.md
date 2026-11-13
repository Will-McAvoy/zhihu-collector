# 参与开发

先阅读 [README](README.md) 和 [测试说明](docs/TESTING.md)。当前支持并验收的桌面平台为 Windows。

1. Fork 后创建分支，安装 `requirements.lock` 及 Playwright Chromium。
2. 使用合成回答、临时数据库和临时输出目录复现问题，不提交真实账号或采集内容。
3. 运行 `python -m pytest tests -q --tb=short`；GUI 测试可设置 `QT_QPA_PLATFORM=offscreen`。
4. PR 描述说明触发条件、修改后的行为和实际验证结果。

修改采集流程时保留来源身份、分页归属、正文完整性及任务终态校验。达到采集上限、登录/验证阻断和部分失败不得置为全量成功。

修改线程、浏览器或 PDF 进程生命周期时，检查暂停、停止、异常退出及既有文件保护；不要使用结束整台机器所有浏览器的方式清理进程。

升级 Playwright 时，除常规回归外还需验证同步等待桥和子进程回收。打包脚本不内置 Chromium；分发第三方运行库时需保留其许可要求。
