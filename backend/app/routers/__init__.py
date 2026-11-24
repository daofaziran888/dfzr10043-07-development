"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import backfill, resource

# 专用路径（如 /api/schedule_plan/{id}/gaps）必须先于通用资源路由注册，
# 否则动态的 /api/{module}/{entry_id} 会抢先匹配。
ROUTERS = [backfill, resource]
