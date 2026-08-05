"""pytest 共享配置：确保项目根目录在 sys.path 上。

无论以 `python -m pytest tests/`（cwd 已在 sys.path）还是直接运行
`pytest`，都保证 ``from curator_views.core import ...`` 可以导入。
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
