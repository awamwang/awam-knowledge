# SPDX-License-Identifier: MIT
"""awam-knowledge 常驻 HTTP 服务入口（AFM 集成）。

serve 契约见 design/afm-http-service-design.md；AFM 经 managed pack spawn 本产物，注入
KM_SERVICE_TOKEN（来自 AFM 设置 power.awamKnowledgeServiceToken）。索引根沿用
index_root 约定（AWAM_INDEX_ROOT → KM_DATA_ROOT/KM_SOURCE_ID → settings.yaml），拒绝真源。
fail-fast：无 KM_SERVICE_TOKEN 不启动。
"""
from __future__ import annotations

import sys

from awam_knowledge.http_service import main

if __name__ == "__main__":
    sys.exit(main())
