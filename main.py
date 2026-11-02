"""Uvicorn 应用入口。"""

from companion.ingress import create_app
from companion.logging_setup import configure_companion_logging

# 必须在 create_app() 之前：装配期（load_settings、vision 接线等）
# 就会写 companion.* 日志；uvicorn 的默认日志配置只管 uvicorn.*
# 自身，不装配这棵树，迟装会让启动期 INFO 仍被丢弃（见
# companion/logging_setup.py 的背景说明）。
configure_companion_logging()

app = create_app()
