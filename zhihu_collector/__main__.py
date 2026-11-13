"""命令入口。

直接运行打开界面。
`python -m zhihu_collector login` 打开可见浏览器登录。
`python -m zhihu_collector poc <答主主页>` 只采集第一页回答并写出 JSON。
`python -m zhihu_collector --version` 显示版本号。
"""

from __future__ import annotations

import sys


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else "gui"
    if command == "--pdf-worker" and len(sys.argv) == 3:
        from zhihu_collector.exporters.pdf_process import worker_main
        return worker_main(sys.argv[2])
    if command == "login":
        from zhihu_collector.browser.auth_manager import AuthManager

        status = AuthManager().login_interactive()
        print(status.message)
        return 0 if status.logged_in else 1
    if command == "poc":
        from zhihu_collector.poc import main as poc_main

        return poc_main(sys.argv[2:])
    if command in {"-h", "--help", "help"}:
        print("用法：python -m zhihu_collector [gui|login|poc <答主主页>|--version]")
        return 0
    if command in {"-V", "--version", "version"}:
        from zhihu_collector import __app_name__, __version__

        print(f"{__app_name__} {__version__}")
        return 0
    if command != "gui":
        print(f"未知命令：{command}")
        return 2
    from zhihu_collector.app import main as gui_main

    return gui_main()


if __name__ == "__main__":
    raise SystemExit(main())
