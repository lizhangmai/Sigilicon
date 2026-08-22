# AGENTS.md — Sigilicon Python package

适用于本目录及全部子目录。Sigilicon 只拥有可跨项目复用的 EDA flow 模型、adapter、
安全检查和编排；具体 IP（含层次化 composite IP）、PDK、qualification 门槛与仓库
布局由调用项目拥有。全部设计层次通过 IP component/integration seam 表达。

- 公共 import namespace 是 `sigilicon`，实现采用 `src/sigilicon` layout。
- 库内 workflow 接收显式 `ProjectContext`，或接收显式 project root 后只读取该 root
  的项目 contract；不得搜索 cwd、Pixi 环境或相邻仓库。只有 CLI 可从 cwd 发现
  `sigilicon.toml`。
- `virtuoso_bridge` 只能由 `sigilicon.virtuoso` adapter 直接导入。顶层
  `import sigilicon` 和 CLI help 不得要求 Bridge 或 Virtuoso 可用。
- 测试不得启动真实 EDA；需要 Bridge 的测试使用 fake client 或已有离线 helper。
- 完成修改至少运行 package tests、wheel build、clean-wheel import/CLI smoke 与
  `git diff --check`（目录属于 Git 仓库时）。不执行真实 OA 写入或仿真。
