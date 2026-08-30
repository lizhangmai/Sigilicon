# AGENTS.md — Sigilicon Python package

适用于本目录及全部子目录。Sigilicon 只拥有可跨项目复用的 EDA flow 模型、adapter、
安全检查和编排；具体 IP（含层次化 composite IP）、PDK、qualification 门槛与仓库
布局由调用项目拥有。全部设计层次通过 IP component/integration seam 表达。

- 本仓库只维护 `AGENTS.md` 作为 agent 术语、ownership 和工作规则入口；不得新增或
  更新 `CONTEXT.md`。架构事实优先由 source、类型、测试和 ADR 表达。
- 公共 import namespace 是 `sigilicon`，实现采用 `src/sigilicon` layout。
- 需要 owner、catalog 或 artifact inventory 的 domain loader 与 workflow 接收同一个显式
  `Project`；只需路径和直接工具操作的窄接口接收显式 `ProjectContext`。最外层 composition
  入口把显式 project root 解析成 `Project`，内部接口不接受 project root 代替 `Project`；
  只有 CLI 可从 cwd 发现 `sigilicon.toml`。
- `virtuoso_bridge` 只能由 `sigilicon.virtuoso` adapter 直接导入。顶层
  `import sigilicon` 和 CLI help 不得要求 Bridge 或 Virtuoso 可用。
- 测试不得启动真实 EDA；需要 Bridge 的测试使用 fake client 或已有离线 helper。
- 修改 physical-design、P&R、materialization、physical verification 或 closure campaign
  时，先完整阅读 `docs/pnr-development.md` 及相关 ADR。保持 `FlowEngine` 为单轮确定
  DAG executor，跨轮 closure 由独立 workflow Module 拥有；阶段只交换严格 typed
  artifacts，不从 report metric 或任意 dict 恢复状态。只有 closed result 可生成
  executable Materialization Plan；DRC/LVS clean 必须同时具有 checked identity、已执行
  backend、已解析报告和零退出码。Closure feedback 只有经过独立 repair compiler 与显式
  project-owned typed policy 才能生成 immutable Repair Plan；Campaign 只消费该 Interface，
  不拥有修复算法。测试用 offline/fake Adapter 只能产生非结论状态。
- 完成修改至少运行 package tests、wheel build、clean-wheel import/CLI smoke 与
  `git diff --check`（目录属于 Git 仓库时）。不执行真实 OA 写入或仿真。
