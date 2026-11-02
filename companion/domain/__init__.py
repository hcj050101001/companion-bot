"""纯领域层（方案 §4/§6）：无 IO、无 companion 内部依赖。

各层（services / infra / adapters / apps）都可向下依赖本包；本包不
依赖任何其他 companion 模块，import 边界检查据此守护。
"""
